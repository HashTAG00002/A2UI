"""FF.2 §3.2 — full-loop killtest: GenUI surface → /edit → dispatch → verify.

The one script that proves the load-bearing bidirectional chain end-to-end
through the REAL rendered surface (not the scripted shortcut):

    seed session
      → UISimDriver GETs /<sid>, BeautifulSoup-parses the editable <form>s,
        finds the target var_id form, POSTs /<sid>/edit {var_id,new_value,
        format=json}   ← exactly what a human filling the form would do
      → the server's /edit route compiles the patch + dispatches (via
        sess.adapters — executor=api OR gui_agent) → real app write
      → verifier reads canonical (check_round_trip + non_interference)
      → neg-control: a bogus-var_id POST must score ≤0.3

Metrics (§3.2): ui_parse_ok, form_submit_ok, dispatch_n_ops,
gui_exec_success_rate, round_trip_score, binding_f1, full_loop_pass.

Usage:
    python -m taskvm.evaluation.run_full_loop_killtest \
        --task release_reschedule --samples 1 --execution-mode api
    # gui_agent (real browser + model; FF.7 statistical run):
    python -m taskvm.evaluation.run_full_loop_killtest \
        --task release_reschedule --samples 3 --execution-mode gui_agent

Honesty: the binding is the GT (mock) binding here (seed_session uses
_gt_binding) — this killtest tests the UI→edit→dispatch→verify chain, NOT
binding discovery (that's run_w1_killtest's job). binding_f1 measures whether
the rendered surface faithfully exposes the binding's var_ids (GenUI→binding
wiring fidelity), not model discovery accuracy.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from taskvm.benchmark import model_client
from taskvm.benchmark.cost_model import CostModel
from taskvm.benchmark.fixtures import CanonicalTaskGraph, get_task
from taskvm.governance.governance_interpreter import GovernanceInterpreter
from taskvm.governance.ui_sim_driver import UISimDriver
from taskvm.governance.vm_state import VMStateSnapshot
from taskvm.harness.state_adapter import make_adapters
from taskvm.verifier import canonical_state as cs
from taskvm.verifier.round_trip_checks import check_round_trip
from taskvm.workspace_ui import server as wserver

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
PASS_SCORE = 0.85
NEG_CONTROL_MAX = 0.3


def _build_adapters(fixture: CanonicalTaskGraph, *, host: str,
                    execution_mode: str) -> dict:
    """FF.1 union pattern: build the adapter set from the task's seed_state ∪
    binding apps so multi-app tasks (launch_full needs mail) are wired."""
    apps = sorted(set(fixture.seed_state.keys())
                  | {b.app for b in fixture.bindings})
    _APP_ORDER = ("calendar", "taskboard", "drive", "mail",
                  "outlook_cal", "wechat", "alipay")
    app_list = [a for a in _APP_ORDER if a in apps]
    gui_dir = (str(EVAL_DIR / f"full_loop_gui_{time.strftime('%Y%m%d_%H%M%S')}")
               if execution_mode == "gui_agent" else None)
    return make_adapters(apps=app_list, host=host, executor=execution_mode,
                         gui_screenshot_dir=gui_dir)


def _seed(fixture: CanonicalTaskGraph, adapters: dict, *, host: str,
          use_genui: bool):
    """Seed a workspace session in-process (registers in wserver.user_sessions
    so the Flask test_client can GET/POST it). Returns the session."""
    sess = wserver.seed_session(fixture, adapters, host=host)
    sess.use_genui = use_genui
    return sess


def _round_trip(sid: str, fixture: CanonicalTaskGraph, adapters: dict,
                pre: dict) -> dict:
    res = check_round_trip(sid, fixture, adapters, pre)
    return {
        "score": res.score,
        "changed_fraction": res.changed.fraction,
        "untouched_fraction": res.untouched.fraction,
        "resynced_fraction": res.resynced.fraction,
        "non_interference_passed": res.non_interference_passed,
        "hard_fail": res.hard_fail,
        "info": res.info,
    }


def run_one_sample(fixture: CanonicalTaskGraph, *, execution_mode: str = "api",
                   sample_i: int = 0, host: str = "localhost",
                   use_genui: bool = False,
                   cost_model: CostModel | None = None) -> dict:
    """One full-loop sample: seed → UISimDriver GET/parse/POST → verify.
    Returns the metrics record (§3.2)."""
    if execution_mode == "gui_agent" and cost_model is not None:
        from taskvm.execution.gui_executor import get_executor
        get_executor().cost_model = cost_model
    adapters = _build_adapters(fixture, host=host, execution_mode=execution_mode)
    # health-check (warn, don't crash — caller decides)
    for app, ad in adapters.items():
        h = ad.health()
        if h.get("status") != "ok":
            logger.warning(f"{app} not healthy @ {ad.base_url}: {h}")
    # seed_session mints a sid + resets + seeds the apps under that sid; the
    # UISimDriver + verifier + cleanup all use THAT sid (single source of truth).
    sess = _seed(fixture, adapters, host=host, use_genui=use_genui)
    sid = sess.sid
    client = wserver.app.test_client()
    # interpreter (for verification-criterion construction — NOT re-dispatch;
    # the UISimDriver's POST already dispatched through the server)
    interp = GovernanceInterpreter(enable_llm_rollback_nl=False)
    vm_state = VMStateSnapshot(
        sid=sid, binding=sess.binding, adapters=adapters,
        rollback_log=sess.rollback_log, checkpoints=fixture.checkpoints)
    # capture the pre-snapshot BEFORE the UISimDriver POSTs (the POST dispatches)
    pre = cs.snapshot(adapters, sid)
    driver = UISimDriver(fixture, client, sid)
    ev = driver.next_event()
    subgoals = []
    if ev is not None:
        subgoals = interp.interpret(ev, vm_state, task=fixture)
    rt = _round_trip(sid, fixture, adapters, pre)
    lr = driver.last_response
    expected_var_ids = {fixture.user_edit.get("var_id", "")}
    found_var_ids = set(lr.get("found_var_ids", []))
    binding_f1 = (len(expected_var_ids & found_var_ids) / len(expected_var_ids)
                  if expected_var_ids else 1.0)
    n_ops = int(lr.get("n_ops", 0))
    n_applied = int(lr.get("n_applied", 0))
    gui_exec_success_rate = (n_applied / n_ops) if n_ops else 0.0
    ui_parse_ok = bool(lr.get("ui_parse_ok"))
    form_submit_ok = bool(lr.get("form_submit_ok"))
    round_trip_score = rt["score"]
    non_interf = bool(rt["non_interference_passed"])
    # cleanup
    for ad in adapters.values():
        ad.reset(sid)
    wserver.user_sessions.pop(sid, None)
    return {
        "task_id": fixture.task_id,
        "sample": sample_i,
        "sid": sid,
        "execution_mode": execution_mode,
        "use_genui": use_genui,
        "ui_parse_ok": ui_parse_ok,
        "form_submit_ok": form_submit_ok,
        "found_var_ids": sorted(found_var_ids),
        "expected_var_ids": sorted(expected_var_ids),
        "binding_f1": round(binding_f1, 4),
        "dispatch_n_ops": n_ops,
        "dispatch_n_applied": n_applied,
        "gui_exec_success_rate": round(gui_exec_success_rate, 4),
        "changed_vars": lr.get("changed_vars", []),
        "round_trip_score": round(round_trip_score, 4),
        "non_interference_passed": non_interf,
        "round_trip": rt,
        "n_subgoals": len(subgoals),
        "subgoals": [s.to_dict() for s in subgoals],
        "driver_error": lr.get("error"),
        "model": model_client.TASKVM_DEFAULT_MODEL,
    }


def run_neg_control(fixture: CanonicalTaskGraph, *, execution_mode: str = "api",
                    host: str = "localhost", use_genui: bool = False) -> dict:
    """Neg-control: POST /edit with a bogus var_id → compile_patch returns []
    → 0 ops → no state change → round_trip score MUST be ≤0.3 (the verifier
    correctly reports nothing changed). Mirrors run_w1_killtest's
    broken="noop" neg-control but through the REAL server /edit route."""
    adapters = _build_adapters(fixture, host=host, execution_mode=execution_mode)
    sess = _seed(fixture, adapters, host=host, use_genui=use_genui)
    sid = sess.sid
    client = wserver.app.test_client()
    pre = cs.snapshot(adapters, sid)
    # bogus var_id — NOT in the binding → compile_patch returns [] → 0 ops
    r = client.post(f"/{sid}/edit", data={
        "var_id": "__neg_control_bogus__", "new_value": "x", "format": "json"})
    try:
        data = r.get_json() or {}
    except Exception:
        data = {}
    rt = _round_trip(sid, fixture, adapters, pre)
    for ad in adapters.values():
        ad.reset(sid)
    wserver.user_sessions.pop(sid, None)
    return {
        "task_id": fixture.task_id,
        "neg_control": True,
        "n_ops": int(data.get("n_ops", 0)),
        "n_applied": int(data.get("n_applied", 0)),
        "round_trip_score": round(rt["score"], 4),
        "passed": rt["score"] <= NEG_CONTROL_MAX,
    }


def summarize(fixture: CanonicalTaskGraph, samples: list[dict], neg: dict) -> dict:
    n = len(samples)
    scores = [s["round_trip_score"] for s in samples]
    ui_parse = all(s["ui_parse_ok"] for s in samples)
    form_submit = all(s["form_submit_ok"] for s in samples)
    non_interf = all(s["non_interference_passed"] for s in samples)
    # full_loop_pass (§3.2): ui_parse + form_submit + round_trip ≥0.85 (majority)
    # + non_interference + neg ≤0.3. For 1-sample (acceptance), majority = 1/1.
    n_pass_score = sum(1 for s in scores if s >= PASS_SCORE)
    majority = n_pass_score >= max(1, (n // 2) + 1) if n else False
    full_loop_pass = (ui_parse and form_submit and majority and non_interf
                      and neg["passed"])
    return {
        "task_id": fixture.task_id,
        "n_samples": n,
        "round_trip_scores": scores,
        "mean_round_trip": round(sum(scores) / n, 4) if n else 0.0,
        "n_pass_score": n_pass_score,
        "pass_threshold": PASS_SCORE,
        "ui_parse_ok_all": ui_parse,
        "form_submit_ok_all": form_submit,
        "non_interference_ok_all": non_interf,
        "binding_f1_mean": round(sum(s["binding_f1"] for s in samples) / n, 4) if n else 0.0,
        "gui_exec_success_rate_mean": round(
            sum(s["gui_exec_success_rate"] for s in samples) / n, 4) if n else 0.0,
        "neg_control_score": neg["round_trip_score"],
        "neg_control_passed": neg["passed"],
        "full_loop_pass": full_loop_pass,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="FF.2 full-loop killtest")
    parser.add_argument("--task", default="release_reschedule")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--execution-mode", choices=["api", "gui_agent"],
                        default="api",
                        help="'api' (fast, requests.post) or 'gui_agent' "
                             "(real browser + model — FF.7 statistical run)")
    parser.add_argument("--genui", action="store_true",
                        help="render the rw-zone via the GenUI decoder (model "
                             "call). Default off (f-string rw-field forms, "
                             "which is what §3.1 parses).")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    fixture = get_task(args.task)
    cost_model = CostModel()
    samples = []
    for i in range(args.samples):
        logger.info(f"--- {args.task} sample {i+1}/{args.samples} ({args.execution_mode}) ---")
        s = run_one_sample(fixture, execution_mode=args.execution_mode,
                           sample_i=i, host=args.host, use_genui=args.genui,
                           cost_model=cost_model)
        logger.info(f"sample {i+1}: ui_parse={s['ui_parse_ok']} "
                    f"form_submit={s['form_submit_ok']} "
                    f"round_trip={s['round_trip_score']} "
                    f"binding_f1={s['binding_f1']} "
                    f"n_ops={s['dispatch_n_ops']} n_applied={s['dispatch_n_applied']}")
        samples.append(s)
        if args.execution_mode == "gui_agent" and i < args.samples - 1:
            logger.info("[gui_agent] QPM refill: sleeping 60s before next sample")
            time.sleep(60)
    neg = run_neg_control(fixture, execution_mode=args.execution_mode,
                          host=args.host, use_genui=args.genui)
    logger.info(f"[neg-control] {args.task}: score={neg['round_trip_score']} "
                f"(≤{NEG_CONTROL_MAX}) → {'PASS' if neg['passed'] else 'FAIL'}")
    sm = summarize(fixture, samples, neg)
    report = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "task": args.task, "execution_mode": args.execution_mode,
        "use_genui": args.genui, "n_samples": args.samples,
        "model": model_client.TASKVM_DEFAULT_MODEL,
        "cost": cost_model.summary(),
        "summary": sm, "samples": samples, "neg_control": neg,
    }
    out_path = Path(args.out) if args.out else (
        EVAL_DIR / f"full_loop_killtest_{time.strftime('%Y%m%d_%H%M%S')}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_path}")
    print(f"\n=== FULL-LOOP KILLTEST: {args.task} ===")
    print(f"  full_loop_pass: {sm['full_loop_pass']}")
    print(f"  ui_parse_ok: {sm['ui_parse_ok_all']}  form_submit_ok: {sm['form_submit_ok_all']}")
    print(f"  round_trip mean: {sm['mean_round_trip']}  binding_f1: {sm['binding_f1_mean']}")
    print(f"  non_interference: {sm['non_interference_ok_all']}  neg: {sm['neg_control_score']}")
    return 0 if sm["full_loop_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
