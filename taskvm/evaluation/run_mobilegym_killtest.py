"""MobileGym kill-test orchestrator — the honesty-based rollback gate (E8/E9.2).

Lands a PERSISTED, independently re-checkable JSON artifact at
``eval_results/mobilegym_killtest_<ts>.json`` — the E8 debt: prior "GATE PASS
score=1.0 neg=0.3" claims lived only in chat + Claude memory (which a fresh
agent session / a reviewer cannot see). This file is the repo artifact anyone
can re-audit. (Memory is NOT a substitute — see .mrules E9.1.)

Mirrors ``run_w1_killtest`` (standalone ``python -m`` → JSON, health-check +
exit if unreachable, ≥N samples, neg-control ≤0.3) but tests the MobileGym
substrate's THREE honest dimensions (the prior claim reported only ONE — the
write-path score — and OMITTED the rollback honest-failure, which is the
whole point of the "honesty-based rollback" construct):

  - **round_trip (write-happened)**: the real-gesture ``send_message`` really
    delivered the text to the wechat 黄勇 chat. Score = 0.5·changed +
    0.3·untouched + 0.2·resynced, non-interference hard gate. Expected ~1.0
    (this is the "score=1.0" the prior claim meant).
  - **honest_irreversibility (rollback gate)**: ``undo_saga`` on the sent
    message HONESTLY fails (the bridge raises HTTP 409 — MobileGym's wechat has
    no delete/recall UI; no ``set_state`` backdoor to fake a restore). The
    saga marks ``partial_failure=True``, ``n_reverted=0``. The verifier
    INDEPENDENTLY confirms: rollback fidelity = 0.0 (the message is NOT
    restored — the saga's failure claim is TRUE, not a false alibi), the
    message is STILL THERE post-undo (no backdoor delete), and
    non-interference-on-rollback holds (the failed undo didn't clobber
    neighbors). This is the HONEST FAILURE — do NOT report it as "reversible
    compensation succeeded" (E9.3: those are different technical claims).
  - **binding_discovery (task-4)**: a frontier model (gpt-5.6-sol) discovers
    the alipay→wechat binding from the RENDERED observation (the bridge's
    ``html_view`` DOM, captured via ``GET /<sid>`` — read-path-is-GUI, no GT)
    alone. Previously the binding was GT-given; this measures whether the
    model can find it. Reported as f1 vs GT (a diagnostic, NOT a gate — the
    round-trip gate uses GT binding to isolate the write+verify arc).
  - **neg_control ≤ 0.3**: broken dispatcher (noop) must score ≤0.3.

PASS (honest) = round_trip ≥0.85 ∧ honest_irreversibility (partial_failure ∧
fidelity==0.0 ∧ message_still_there ∧ non_interference_on_rollback) ∧
neg_control ≤0.3. NOTE: PASS here means "the write works AND we honestly
proved the rollback CANNOT undo it" — NOT "we achieved reversibility". The
report's ``verdict`` string states this plainly.

Usage:
    # bridge (:3019) + Vite (:3000) must be running first (see demo runbook):
    python -m taskvm.evaluation.run_mobilegym_killtest --samples 3
    python -m taskvm.evaluation.run_mobilegym_killtest --samples 3 --model gpt-5.6-sol
    python -m taskvm.evaluation.run_mobilegym_killtest --neg-control   # neg only
    python -m taskvm.evaluation.run_mobilegym_killtest --no-binding-discovery  # skip model (GT binding only)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import requests

from taskvm.benchmark import model_client
from taskvm.benchmark.cost_model import CostModel
from taskvm.benchmark.mobilegym_fixtures import (
    MOBILEGYM_TASKS, all_mobilegym_tasks, get_mobilegym_task)
from taskvm.execution.action_dispatcher import dispatch
from taskvm.execution.gui_driver import make_task_adapters
from taskvm.execution.patch_compiler import compile_patch
from taskvm.execution.rollback import RollbackLog
from taskvm.harness import replay_engine as replay
from taskvm.harness.observations import StepObservation, TraceFixture
from taskvm.substrate.mobilegym.evaluation import MobileGymEvaluationEnvironment
from taskvm.task_state.compiler import compile_binding
from taskvm.task_state.entity_binding import TaskBinding
from taskvm.verifier import canonical_state as cs
from taskvm.verifier.rollback_verify import check_rollback_fidelity
from taskvm.verifier.round_trip_checks import check_round_trip, binding_accuracy
from taskvm.evaluation.render_check import (parse_compiler_output,
                                            validate_binding)

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
PASS_SCORE = 0.85
NEG_CONTROL_MAX = 0.3
SAMPLES_FOR_PASS = 2   # ≥2/3 samples (mirrors W1/W3)
BRIDGE_PORT = 3019
VITE_PORT = 3000
MOBILEGYM_APPS = ["wechat", "alipay"]


def _mg_envs(sid: str, host: str) -> dict[str, MobileGymEvaluationEnvironment]:
    """Per-sid evaluation-plane environments (Agent B / substrate isolation:
    reset/seed/oracle live on the EVALUATION plane; the runtime task adapters
    only WRITE via the bridge operator routes, which wrap the injected CUA
    loop = real gestures — the two planes are physically separate)."""
    bridge = f"http://{host}:{BRIDGE_PORT}"
    return {app: MobileGymEvaluationEnvironment(app, sid, bridge)
            for app in MOBILEGYM_APPS}


# ── MobileGym observation capture (combined page → per-app obs) ──────────────
def _capture_mobilegym_obs(host: str, sid: str) -> dict[str, StepObservation]:
    """Capture the rendered-GUI observation from the bridge's combined
    ``GET /<sid>`` page (which serves BOTH wechat chats + alipay txs in one
    HTML — read-path-is-GUI, no GT). Split by id-attribute kind
    (``data-chat-id`` → wechat, ``data-transaction-id`` → alipay) into per-app
    observations. Each app's ``dom_html`` is that app's rows only (so
    ``assert_obs_matches_state`` compares apples-to-apples vs the per-app
    ``read_canonical``); the compiler sees the per-app a11y + DOM."""
    bridge_url = f"http://{host}:{BRIDGE_PORT}"  # wechat + alipay share :3019
    r = requests.get(f"{bridge_url}/{sid}", timeout=10.0)
    r.raise_for_status()
    combined_dom = r.text
    typed = replay.parse_dom_entities_typed(combined_dom)
    split = replay.split_entities_by_app(typed, MOBILEGYM_APPS)
    obs: dict[str, StepObservation] = {}
    for app in MOBILEGYM_APPS:
        entities = split.get(app, {})
        kind = replay._KIND_MAP.get(app, app)
        # re-serialize this app's entities into a per-app DOM (the parsed
        # entities are what matter; parse_dom_entities round-trips identically).
        # Skip None values so a field absent from the canonical read is also
        # absent from the DOM (assert_obs_matches_state compares per-field).
        rows = "".join(
            f'<tr data-{kind}-id="{_eid}">' + "".join(
                f'<td data-field="{fn}">{_fv}</td>'
                for fn, _fv in e.items() if _fv is not None) + "</tr>"
            for _eid, e in entities.items())
        per_app_dom = f'<table><tbody>{rows or "<tr><td>none</td></tr>"}</tbody></table>'
        a11y = replay.synthesize_a11y(app, entities)
        obs[app] = StepObservation(app=app, step=0, dom_html=per_app_dom,
                                   a11y_text=a11y, screenshot_path=None)
    return obs


def _mg_field_eq(cval: Any, dval: Any) -> bool:
    """MobileGym-local field equality (avoids replay_engine._field_eq's
    ``str(x or '')`` falsy trap: ``str(0 or '')`` → '' so an empty chat's
    ``n_messages=0`` would mismatch the DOM's ``'0'``). We compare None→''
    but keep 0/False as their string form. Lists (wechat ``messages`` is a
    joined string, not a list, so this rarely triggers) compared as joined."""
    if cval is None and (dval is None or dval == ""):
        return True
    if isinstance(cval, list):
        cstr = ", ".join(str(x) for x in cval)
        return _strip(dval) == cstr.lower() or _strip(dval) == "".join(str(x) for x in cval)
    return _strip(cval) == _strip(dval)


def _strip(v: Any) -> str:
    from html import unescape
    import re
    s = unescape(re.sub(r"<[^>]+>", "", str(v if v is not None else "")))
    return " ".join(s.split()).lower()


def _assert_mobilegym_obs_matches_state(envs: dict, sid: str,
                                        obs: dict[str, StepObservation]) -> None:
    """Per-app live-state assert (replay_engine.assert_obs_matches_state's
    MobileGym variant): the DOM-parsed entities must match the evaluation
    plane's ``oracle_state`` per app. Catches a stale/detached compiler input.
    Uses ``_mg_field_eq`` (local; the shared ``_field_eq`` has a falsy-0 trap
    on wechat n_messages)."""
    mismatches: list[str] = []
    for app, ad in envs.items():
        canonical = ad.oracle_state(sid)
        canon_e = canonical["entities"]
        dom_e = replay.parse_dom_entities(obs[app].dom_html)
        if set(dom_e) != set(canon_e):
            mismatches.append(f"{app}: entity-id set mismatch DOM={sorted(set(dom_e))} "
                              f"canonical={sorted(set(canon_e))}")
            continue
        for eid, cf in canon_e.items():
            df = dom_e.get(eid, {})
            for fn, cval in cf.items():
                if not _mg_field_eq(cval, df.get(fn)):
                    mismatches.append(f"{app}.{eid}.{fn}: DOM={df.get(fn)!r} canonical={cval!r}")
    if mismatches:
        raise AssertionError("mobilegym obs/state mismatch (live-state anchor):\n  "
                             + "\n  ".join(mismatches))
    logger.info(f"[mobilegym killtest] obs matches state for sid={sid}")


# ── GT binding (the round-trip gate isolates write+verify, not binding) ───────
def _gt_task_binding(fixture) -> TaskBinding:
    """Build a TaskBinding from the GT fixture (for the round-trip + neg-control
    gates — these isolate the write/verify/rollback arc, NOT binding
    discovery). The binding itself is verifier-only GT; the compiler never sees
    it (task-4 binding discovery captures obs from the rendered GUI, not GT)."""
    var_groups: dict[str, dict] = {}
    for b in fixture.bindings:
        g = var_groups.setdefault(b.var_id, {
            "var_id": b.var_id, "label": b.var_id,
            "value": fixture.user_edit.get("old"), "editable": True, "bindings": []})
        g["bindings"].append({"var_id": b.var_id, "app": b.app,
                              "entity_id": b.entity_id, "field": b.field,
                              "operator": b.operator})
    return TaskBinding(task_id=fixture.task_id, variables=list(var_groups.values()))


def run_round_trip_and_rollback(fixture, adapters: dict, *, sample_i: int,
                                host: str = "localhost") -> dict:
    """One sample of the write-happened + honest-irreversibility arc.
    reset+seed → capture obs → assert → GT binding → dispatch (real gestures)
    → check_round_trip (write-happened score) → undo_saga (honest 409) →
    check_rollback_fidelity (verifier independently confirms the message was
    NOT restored). Returns the per-sample record."""
    sid = f"{fixture.task_id}_mg_s{sample_i}_{int(time.time()*1000) % 100000}"
    envs = _mg_envs(sid, host)   # evaluation plane; adapters = runtime write plane
    for env in envs.values():
        env.reset(sid)
    replay.seed_apps(fixture, envs, sid)
    obs = _capture_mobilegym_obs(host, sid)
    _assert_mobilegym_obs_matches_state(envs, sid, obs)
    tb = _gt_task_binding(fixture)
    pre = cs.snapshot(envs, sid)   # BEFORE dispatch (the state to restore)
    rlog = RollbackLog()

    # WRITE: dispatch the GT patch via real GUI gestures (bridge send_message).
    ops = compile_patch(fixture.user_edit, tb)
    dispatch_report = dispatch(ops, adapters, sid, broken=None, rollback_log=rlog)
    post_dispatch = cs.snapshot(envs, sid)
    rt = check_round_trip(sid, fixture, envs, pre)

    # ROLLBACK (honest irreversibility): undo the saga → 409 → partial_failure.
    saga_id = rlog.latest_saga_id()
    sres = rlog.undo_saga(saga_id, sid, adapters) if saga_id else None
    post_undo = cs.snapshot(envs, sid)
    rf = (check_rollback_fidelity(pre, post_undo, envs, sid, sres)
          if sres is not None else None)

    # the message-still-there proof (no set_state backdoor delete on the 409):
    msg_after_undo = cs.entity_value(post_undo, "wechat",
                                     fixture.bindings[0].entity_id, "messages")
    expected_msg = fixture.user_edit["new"]
    message_still_there = (str(msg_after_undo or "").strip().lower()
                           == str(expected_msg).strip().lower())

    rec = {
        "task_id": fixture.task_id, "sample": sample_i, "sid": sid,
        "round_trip": {
            "score": rt.score, "changed_fraction": rt.changed.fraction,
            "untouched_fraction": rt.untouched.fraction,
            "resynced_fraction": rt.resynced.fraction,
            "non_interference_passed": rt.non_interference_passed,
            "hard_fail": rt.hard_fail,
        },
        "dispatch": dispatch_report.to_dict() if dispatch_report else None,
        "honest_irreversibility": {
            "saga_id": (sres.saga_id if sres else None),
            "n_targets": (sres.n_targets if sres else 0),
            "n_reverted": (sres.n_reverted if sres else 0),
            "fully_reverted": (sres.fully_reverted if sres else False),
            "partial_failure": (sres.partial_failure if sres else False),
            "saga_errors": (sres.errors if sres else []),
            "rollback_fidelity_score": (rf.score if rf else None),
            "fidelity": (rf.fidelity if rf else None),
            "non_interference_on_rollback": (rf.non_interference_on_rollback if rf else None),
            "message_still_there_post_undo": message_still_there,
            "n_restored": (rf.n_restored if rf else None),
            "not_restored": (rf.not_restored if rf else None),
        },
    }
    # cleanup
    for env in envs.values():
        env.reset(sid)
    return rec


def run_binding_discovery(fixture, *, sample_i: int,
                           model: str | None, cost_model: CostModel | None,
                           temperature: float | None = None,
                           host: str = "localhost") -> dict:
    """task-4: a frontier model discovers the alipay→wechat binding from the
    RENDERED observation alone (no GT). Captures obs via the bridge's html_view
    (``GET /<sid>``, split per-app), builds a TraceFixture, calls
    ``compile_binding``, and scores the discovered binding vs GT
    (``binding_accuracy``). Diagnostic, NOT a gate."""
    sid = f"{fixture.task_id}_mg_bd_s{sample_i}_{int(time.time()*1000) % 100000}"
    envs = _mg_envs(sid, host)   # evaluation plane (no runtime writes here)
    for env in envs.values():
        env.reset(sid)
    replay.seed_apps(fixture, envs, sid)
    obs = _capture_mobilegym_obs(host, sid)
    _assert_mobilegym_obs_matches_state(envs, sid, obs)
    observed_ids = {app: set(replay.parse_dom_entities(o.dom_html).keys())
                   for app, o in obs.items()}
    trace = TraceFixture(task_id=fixture.task_id, goal=fixture.goal, final_obs=obs)
    try:
        compiled = compile_binding(trace, observed_ids, model=model,
                                   temperature=temperature, cost_model=cost_model,
                                   binding_only=True)
    except Exception as e:
        # model API flakiness (429/quota/timeout on the Meituan proxy) must NOT
        # crash the whole kill-test — the core gate (round-trip + honest-
        # irreversibility + neg) is model-free. Report the binding-discovery
        # sample as model-unavailable, honestly.
        for env in envs.values():
            env.reset(sid)
        return {
            "task_id": fixture.task_id, "sample": sample_i, "sid": sid,
            "model": model or model_client.TASKVM_DEFAULT_MODEL,
            "compile_ok": False, "compile_error": f"model_unavailable: {type(e).__name__}: {e}",
            "binding_valid": False, "binding_errors": [str(e)],
            "binding_accuracy": binding_accuracy(None, fixture),
            "raw_compiler_output": None,
        }
    raw = compiled.get("raw")
    parsed = compiled.get("parsed")
    binding = parse_compiler_output(raw, parsed)
    valid, bind_errs = (False, ["no binding parsed"]) if binding is None \
        else validate_binding(binding, observed_ids, fixture.task_id)
    bacc = binding_accuracy(binding, fixture) if binding else binding_accuracy(None, fixture)
    for env in envs.values():
        env.reset(sid)
    return {
        "task_id": fixture.task_id, "sample": sample_i, "sid": sid,
        "model": model or model_client.TASKVM_DEFAULT_MODEL,
        "compile_ok": compiled["ok"], "compile_error": compiled.get("error"),
        "binding_valid": valid, "binding_errors": bind_errs,
        "binding_accuracy": bacc,
        "raw_compiler_output": (raw[:4000] if raw else None),
    }


def run_neg_control(fixture, adapters: dict, *, sample_i: int = 0,
                    host: str = "localhost") -> dict:
    """Negative control: GT binding + broken (noop) dispatcher → MUST score
    ≤0.3. If it doesn't, the verifier is dishonest."""
    sid = f"{fixture.task_id}_mg_neg_{int(time.time()*1000) % 100000}"
    envs = _mg_envs(sid, host)   # evaluation plane; adapters = runtime write plane
    for env in envs.values():
        env.reset(sid)
    replay.seed_apps(fixture, envs, sid)
    tb = _gt_task_binding(fixture)
    pre = cs.snapshot(envs, sid)
    ops = compile_patch(fixture.user_edit, tb)
    dispatch_report = dispatch(ops, adapters, sid, broken="noop")
    res = check_round_trip(sid, fixture, envs, pre)
    for env in envs.values():
        env.reset(sid)
    return {
        "task_id": fixture.task_id, "neg_control": True, "broken": "noop",
        "sid": sid, "score": res.score,
        "changed_fraction": res.changed.fraction,
        "untouched_fraction": res.untouched.fraction,
        "resynced_fraction": res.resynced.fraction,
        "passed": res.score <= NEG_CONTROL_MAX,
        "dispatch": dispatch_report.to_dict(),
    }


# ── summary + verdict ────────────────────────────────────────────────────────
def summarize(samples: list[dict], neg: dict,
              binding_samples: list[dict] | None) -> dict:
    n = len(samples)
    rt_scores = [s["round_trip"]["score"] for s in samples]
    n_rt_pass = sum(1 for s in rt_scores if s >= PASS_SCORE)
    # honest-irreversibility: EVERY sample must honestly fail the rollback
    hi = [s["honest_irreversibility"] for s in samples]
    n_hi_honest = sum(1 for h in hi if (h.get("partial_failure") and
                                        h.get("fidelity") == 0.0 and
                                        h.get("message_still_there_post_undo") and
                                        h.get("non_interference_on_rollback")))
    neg_ok = neg["passed"]
    # PASS = write works AND rollback honestly-failed-and-verified ∧ neg ≤0.3
    passed = (n_rt_pass >= SAMPLES_FOR_PASS and n_hi_honest == n and n > 0
              and neg_ok)
    out: dict[str, Any] = {
        "n_samples": n,
        "round_trip_scores": rt_scores,
        "round_trip_mean": round(sum(rt_scores) / n, 4) if n else 0.0,
        "round_trip_pass_count": n_rt_pass,
        "round_trip_pass_threshold": PASS_SCORE,
        "honest_irreversibility_pass_count": n_hi_honest,
        "honest_irreversibility_mean_fidelity": (
            round(sum(h.get("fidelity") or 0 for h in hi) / n, 4) if n else 0.0),
        "neg_control_score": neg["score"],
        "neg_control_passed": neg_ok,
        "neg_control_max": NEG_CONTROL_MAX,
        "PASS": passed,
    }
    if binding_samples:
        f1s = [b["binding_accuracy"]["f1"] for b in binding_samples
               if b.get("binding_accuracy")]
        f1v = [b["binding_accuracy"]["f1_varid_semantic"] for b in binding_samples
               if b.get("binding_accuracy")]
        f1t = [b["binding_accuracy"]["f1_triples"] for b in binding_samples
               if b.get("binding_accuracy")]
        out["binding_discovery"] = {
            "n_samples": len(binding_samples),
            "f1_mean": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
            "f1_varid_semantic_mean": round(sum(f1v) / len(f1v), 4) if f1v else 0.0,
            "f1_triples_mean": round(sum(f1t) / len(f1t), 4) if f1t else 0.0,
            "f1s": f1s, "f1_varid_semantic": f1v, "f1_triples": f1t,
            "note": "diagnostic, NOT a gate (round-trip uses GT binding to isolate write+verify)",
        }
    return out


def _verdict(summary: dict, has_binding: bool) -> str:
    """An honest one-liner. The critical E9.3 distinction: PASS here means
    'write works + rollback honestly failed+verified', NOT 'reversible
    compensation succeeded'."""
    if summary["PASS"]:
        v = (f"PASS — write-happened (round_trip mean {summary['round_trip_mean']}, "
             f"≥{SAMPLES_FOR_PASS}/{summary['n_samples']} samples ≥{PASS_SCORE}) AND "
             f"rollback honestly-failed+verified (fidelity {summary['honest_irreversibility_mean_fidelity']}, "
             f"partial_failure on {summary['honest_irreversibility_pass_count']}/{summary['n_samples']}, "
             f"message-still-there confirmed) AND neg≤{NEG_CONTROL_MAX}. "
             f"This proves HONEST IRREVERSIBILITY, NOT reversible compensation.")
    else:
        v = (f"FAIL — round_trip mean {summary['round_trip_mean']} "
             f"({summary['round_trip_pass_count']}/{summary['n_samples']} ≥{PASS_SCORE}); "
             f"honest-irreversibility {summary['honest_irreversibility_pass_count']}/{summary['n_samples']}; "
             f"neg={summary['neg_control_score']}")
    if has_binding and "binding_discovery" in summary:
        bd = summary["binding_discovery"]
        v += (f" Binding-discovery (diagnostic, NOT a gate): f1={bd['f1_mean']} "
              f"(varid-semantic {bd['f1_varid_semantic_mean']}, triples {bd['f1_triples_mean']}).")
    return v


# ── main ─────────────────────────────────────────────────────────────────────
def main(argv=None):
    parser = argparse.ArgumentParser(description="TaskVM MobileGym kill-test (honesty-based rollback gate)")
    parser.add_argument("--task", default="top3_expense_to_wechat",
                        help="mobilegym task_id (default top3_expense_to_wechat)")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--model", default=None,
                        help="frontier model for binding discovery (default gpt-5.6-sol)")
    parser.add_argument("--temperature", type=float, default=None,
                        help="sampling temperature (default None: reasoning models reject non-default)")
    parser.add_argument("--no-binding-discovery", action="store_true",
                        help="skip the model binding-discovery diagnostic (GT binding only)")
    parser.add_argument("--neg-control", action="store_true",
                        help="run only the negative control (verifier honesty check)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    # Agent B (substrate isolation): runtime WRITE adapters (GUI-only — the
    # bridge operator routes wrap the injected CUA loop = real gestures) and
    # the EVALUATION environments (reset/seed/oracle) are physically separate.
    adapters = make_task_adapters(apps=MOBILEGYM_APPS, host=args.host)
    # health-check bridge + Vite (E8: a kill-test against unreachable services
    # is not honest — exit loudly, don't fabricate scores)
    for app, env in _mg_envs("healthcheck", args.host).items():
        try:
            h = env.health()
            if h.get("status") != "ok":
                logger.error(f"{app} (bridge) not healthy: {h}"); sys.exit(2)
        except Exception as e:
            logger.error(f"{app} (bridge :{BRIDGE_PORT}) not reachable: {e}\n"
                         f"  start the stack first: Vite (:3000) + bridge (:3019) "
                         f"(demo runbook)"); sys.exit(2)
    try:
        r = requests.get(f"http://{args.host}:{VITE_PORT}", timeout=4)
        if r.status_code != 200:
            logger.error(f"Vite :{VITE_PORT} returned {r.status_code}"); sys.exit(2)
    except Exception as e:
        logger.error(f"Vite :{VITE_PORT} not reachable: {e} (the bridge drives the sim served here)"); sys.exit(2)
    logger.info(f"bridge :{BRIDGE_PORT} + Vite :{VITE_PORT} healthy")

    fixture = get_mobilegym_task(args.task)
    cost_model = CostModel()
    ts = time.strftime("%Y%m%d_%H%M%S")

    if args.neg_control:
        neg = run_neg_control(fixture, adapters, host=args.host)
        out_path = Path(args.out) if args.out else EVAL_DIR / f"mobilegym_negcontrol_{ts}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps([neg], ensure_ascii=False, indent=2))
        print(json.dumps([neg], ensure_ascii=False, indent=2))
        print(f"\nNEG-CONTROL {'PASS' if neg['passed'] else 'FAIL'} "
              f"(score={neg['score']} ≤{NEG_CONTROL_MAX})")
        print(f"Wrote {out_path}")
        return 0 if neg["passed"] else 1

    rt_samples = []
    for i in range(args.samples):
        logger.info(f"--- sample {i+1}/{args.samples} (round-trip + rollback) ---")
        s = run_round_trip_and_rollback(fixture, adapters, sample_i=i,
                                        host=args.host)
        logger.info(f"sample {i+1}: round_trip={s['round_trip']['score']} "
                    f"partial_failure={s['honest_irreversibility']['partial_failure']} "
                    f"fidelity={s['honest_irreversibility']['fidelity']} "
                    f"msg_still_there={s['honest_irreversibility']['message_still_there_post_undo']}")
        rt_samples.append(s)
    neg = run_neg_control(fixture, adapters, host=args.host)
    logger.info(f"[neg-control] score={neg['score']} → {'PASS' if neg['passed'] else 'FAIL'}")

    binding_samples = []
    if not args.no_binding_discovery:
        for i in range(args.samples):
            logger.info(f"--- sample {i+1}/{args.samples} (binding discovery) ---")
            b = run_binding_discovery(fixture, sample_i=i,
                                      model=args.model, cost_model=cost_model,
                                      temperature=args.temperature,
                                      host=args.host)
            logger.info(f"binding sample {i+1}: compile_ok={b['compile_ok']} "
                        f"f1={b['binding_accuracy']['f1']} "
                        f"f1_triples={b['binding_accuracy']['f1_triples']}")
            binding_samples.append(b)
    else:
        logger.info("binding discovery skipped (--no-binding-discovery; GT binding only)")

    summary = summarize(rt_samples, neg, binding_samples or None)
    verdict = _verdict(summary, bool(binding_samples))
    report = {
        "ts": ts, "task_id": args.task,
        "model": args.model or model_client.TASKVM_DEFAULT_MODEL,
        "n_samples": args.samples,
        "cost": cost_model.summary(),
        "summary": summary,
        "verdict": verdict,
        "round_trip_and_rollback_samples": rt_samples,
        "binding_discovery_samples": binding_samples,
        "neg_control": neg,
        "honest_framing": {
            "what_PASS_means": "write-happened (round_trip≥0.85) AND rollback honestly-failed+verified "
                               "(partial_failure ∧ fidelity==0.0 ∧ message_still_there ∧ non_interference_on_rollback) "
                               "AND neg≤0.3. This is HONEST IRREVERSIBILITY, NOT reversible compensation.",
            "mobilegym_line_is": "a stress test of the abstraction's generality on a new write-restricted "
                                 "substrate — NOT a positive proof of any VM property 1-5 (see .mrules E9.3).",
        },
    }
    out_path = Path(args.out) if args.out else EVAL_DIR / f"mobilegym_killtest_{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_path}")
    print(f"\n=== MOBILEGYM KILL-TEST VERDICT: {'PASS' if summary['PASS'] else 'FAIL'} ===")
    print(f"  round_trip (write-happened): mean={summary['round_trip_mean']} "
          f"({summary['round_trip_pass_count']}/{summary['n_samples']} ≥{PASS_SCORE})")
    print(f"  honest-irreversibility: {summary['honest_irreversibility_pass_count']}/{summary['n_samples']} "
          f"(mean fidelity={summary['honest_irreversibility_mean_fidelity']})")
    print(f"  neg-control: {summary['neg_control_score']} (≤{NEG_CONTROL_MAX} → "
          f"{'PASS' if summary['neg_control_passed'] else 'FAIL'})")
    if binding_samples:
        bd = summary["binding_discovery"]
        print(f"  binding-discovery (DIAGNOSTIC): f1={bd['f1_mean']} "
              f"(varid-sem {bd['f1_varid_semantic_mean']}, triples {bd['f1_triples_mean']})")
    print(f"\n{verdict}")
    return 0 if summary["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
