"""FF.2 §3.2 — full-loop killtest: GenUI surface → /edit → dispatch → verify.

The one script that proves the load-bearing bidirectional chain end-to-end
through the REAL rendered surface (not the scripted shortcut):

    seed session
      → UISimDriver GETs /<sid>, BeautifulSoup-parses the editable <form>s,
        finds the target var_id form, POSTs /<sid>/edit {var_id,new_value,
        format=json}   ← exactly what a human filling the form would do
      → the server's /edit route compiles the patch + dispatches (via
        sess.adapters — GUI-only; Agent B deleted the API executor) → real
        app write
      → verifier reads canonical (check_round_trip + non_interference)
      → neg-control: a bogus-var_id POST must score ≤0.3

Metrics (§3.2): ui_parse_ok, form_submit_ok, dispatch_n_ops,
gui_exec_success_rate, round_trip_score, binding_f1, full_loop_pass.

Usage:
    python -m taskvm.evaluation.run_full_loop_killtest \
        --task release_reschedule --samples 1
    # GUI-only (real browser + model; FF.7 statistical run):
    python -m taskvm.evaluation.run_full_loop_killtest \
        --task release_reschedule --samples 3

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
from taskvm.execution.gui_driver import make_task_adapters
from taskvm.substrate.builtin_web.evaluation import (
    make_evaluation_environments,
)
from taskvm.verifier import canonical_state as cs
from taskvm.verifier.round_trip_checks import check_round_trip
from taskvm.workspace_ui import server as wserver

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
PASS_SCORE = 0.85
NEG_CONTROL_MAX = 0.3


def _build_planes(fixture: CanonicalTaskGraph, *,
                  host: str) -> tuple[dict, dict]:
    """FF.1 union pattern: build the adapter set from the task's seed_state ∪
    binding apps so multi-app tasks (launch_full needs mail) are wired.

    Agent B (substrate isolation): returns TWO planes — the GUI-only write
    adapters + the evaluation environments (reset/seed/oracle). API write
    executor deleted — GUI-only runtime."""
    apps = sorted(set(fixture.seed_state.keys())
                  | {b.app for b in fixture.bindings})
    _APP_ORDER = ("calendar", "taskboard", "drive", "mail",
                  "outlook_cal", "wechat", "alipay")
    app_list = [a for a in _APP_ORDER if a in apps]
    gui_dir = str(EVAL_DIR / f"full_loop_gui_{time.strftime('%Y%m%d_%H%M%S')}")
    adapters = make_task_adapters(apps=app_list, host=host,
                                  screenshot_dir=gui_dir)
    envs = make_evaluation_environments(apps=app_list, host=host)
    return adapters, envs


def _seed(fixture: CanonicalTaskGraph, adapters: dict, envs: dict, *,
          host: str, use_genui: bool):
    """Seed a workspace session in-process (registers in wserver.user_sessions
    so the Flask test_client can GET/POST it). Returns the session.

    Agent B: ``adapters`` = GUI-only write plane; ``envs`` = evaluation
    plane (seed_session resets+seeds the apps through it via ``oracle=``)."""
    sess = wserver.seed_session(fixture, adapters, oracle=envs, host=host)
    sess.use_genui = use_genui
    return sess


def _round_trip(sid: str, fixture: CanonicalTaskGraph, envs: dict,
                pre: dict) -> dict:
    """Agent B: canonical reads go through the evaluation environments."""
    res = check_round_trip(sid, fixture, envs, pre)
    return {
        "score": res.score,
        "changed_fraction": res.changed.fraction,
        "untouched_fraction": res.untouched.fraction,
        "resynced_fraction": res.resynced.fraction,
        "non_interference_passed": res.non_interference_passed,
        "hard_fail": res.hard_fail,
        "info": res.info,
    }


def _parse_edit_forms(html: str) -> dict[str, dict]:
    """Parse every ``<form>`` posting to ``/<sid>/edit``: its ``var_id``
    (hidden input) + whether it carries a ``new_value`` input. The old
    UISimDriver role is deleted (Agent C role collapse); this
    deterministic HTML scrape is kept INLINE — no model, no driver loop —
    so the killtest still measures what a user's browser round-trips
    through the SAME form contract (``name="var_id"`` +
    ``name="new_value"``, FF.2 §3.1)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:   # pragma: no cover — bs4 is a hard dep
        raise RuntimeError("full-loop killtest needs beautifulsoup4") from e
    soup = BeautifulSoup(html or "", "html.parser")
    forms: dict[str, dict] = {}
    for form in soup.find_all("form"):
        action = form.get("action", "") or ""
        if "edit" not in action:
            continue
        vid_in = form.find("input", {"name": "var_id"})
        if vid_in is None:
            continue
        vid = vid_in.get("value", "") or ""
        val_in = form.find("input", {"name": "new_value"})
        forms[vid] = {"var_id": vid,
                      "has_value_input": val_in is not None,
                      "action": action}
    return forms


def _edit_round(client, sid: str, fixture: CanonicalTaskGraph) -> dict:
    """One user edit round: GET page → parse rw-zone forms → POST /edit
    (format=json). The POST dispatches through the session's GUI write
    plane (real gestures); the returned metrics mirror FF.2 §3.2."""
    target_vid = fixture.user_edit.get("var_id", "")
    new_value = str(fixture.user_edit.get("new", ""))
    r_get = client.get(f"/{sid}")
    html = r_get.get_data(as_text=True)
    forms = _parse_edit_forms(html)
    ui_parse_ok = target_vid in forms
    r_post = client.post(f"/{sid}/edit", data={
        "var_id": target_vid, "new_value": new_value, "format": "json"})
    data: dict = {}
    try:
        data = r_post.get_json() or {}
    except Exception:
        data = {}
    changed_vars = data.get("changed_vars", []) or []
    n_ops = int(data.get("n_ops", 0))
    n_applied = int(data.get("n_applied", 0))
    return {
        "ui_parse_ok": ui_parse_ok,
        "form_submit_ok": bool(data.get("ok")) and bool(changed_vars),
        "found_var_ids": sorted(forms),
        "changed_vars": changed_vars,
        "n_ops": n_ops,
        "n_applied": n_applied,
        "http_status_edit": getattr(r_post, "status_code", None),
        "error": None,
    }


def run_one_sample(fixture: CanonicalTaskGraph, *, sample_i: int = 0,
                   host: str = "localhost",
                   use_genui: bool = False,
                   cost_model: CostModel | None = None) -> dict:
    """One full-loop sample: seed → render+parse the rw-zone form → POST
    /edit (dispatch through the GUI write plane) → verify on the evaluation
    plane. Returns the metrics record (§3.2)."""
    # Agent B (substrate isolation): GUI-only — wire the executor's cost so
    # GUI-grounding calls are attributed in the report's cost (honesty: the
    # FULL token cost, not just compile).
    if cost_model is not None:
        from taskvm.execution.gui_executor import get_executor
        get_executor().cost_model = cost_model
    adapters, envs = _build_planes(fixture, host=host)
    # health-check (warn, don't crash — caller decides) — evaluation plane
    for app, env in envs.items():
        h = env.health()
        if h.get("status") != "ok":
            logger.warning(f"{app} not healthy @ {env.base_url}: {h}")
    # seed_session mints a sid + resets + seeds the apps under that sid; the
    # verifier + cleanup all use THAT sid (single source of truth).
    sess = _seed(fixture, adapters, envs, host=host, use_genui=use_genui)
    sid = sess.sid
    client = wserver.app.test_client()
    # capture the pre-snapshot BEFORE the edit POST dispatches (the POST
    # dispatches through the GUI write plane) — evaluation plane
    pre = cs.snapshot(envs, sid)
    # the edit round (GET → parse → POST); a transport error must not
    # crash the sample — it lands in driver_error and the metrics show it
    try:
        lr = _edit_round(client, sid, fixture)
    except Exception as e:   # noqa: BLE001
        lr = {"ui_parse_ok": False, "form_submit_ok": False,
              "found_var_ids": [], "changed_vars": [], "n_ops": 0,
              "n_applied": 0, "http_status_edit": None,
              "error": f"{type(e).__name__}: {e}"}
        logger.warning("[full-loop] edit round failed: %s", e)
    rt = _round_trip(sid, fixture, envs, pre)
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
    # cleanup — evaluation plane (the GUI adapters have no reset)
    for env in envs.values():
        env.reset(sid)
    wserver.user_sessions.pop(sid, None)
    return {
        "task_id": fixture.task_id,
        "sample": sample_i,
        "sid": sid,
        "execution_mode": "gui_only",
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
        "driver_error": lr.get("error"),
        "model": model_client.TASKVM_DEFAULT_MODEL,
    }


def run_neg_control(fixture: CanonicalTaskGraph, *,
                    host: str = "localhost", use_genui: bool = False) -> dict:
    """Neg-control: POST /edit with a bogus var_id → compile_patch returns []
    → 0 ops → no state change → round_trip score MUST be ≤0.3 (the verifier
    correctly reports nothing changed). Mirrors run_w1_killtest's
    broken="noop" neg-control but through the REAL server /edit route."""
    # Agent B (substrate isolation): API write executor deleted — GUI-only runtime.
    adapters, envs = _build_planes(fixture, host=host)
    sess = _seed(fixture, adapters, envs, host=host, use_genui=use_genui)
    sid = sess.sid
    client = wserver.app.test_client()
    pre = cs.snapshot(envs, sid)
    # bogus var_id — NOT in the binding → compile_patch returns [] → 0 ops
    r = client.post(f"/{sid}/edit", data={
        "var_id": "__neg_control_bogus__", "new_value": "x", "format": "json"})
    try:
        data = r.get_json() or {}
    except Exception:
        data = {}
    rt = _round_trip(sid, fixture, envs, pre)
    for env in envs.values():
        env.reset(sid)
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
    # Agent B (substrate isolation): the legacy --execution-mode 'api' option
    # (fast requests.post writes — the §12.16 backdoor) is DELETED; the
    # runtime is GUI-only (make_task_adapters has no executor knob).
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
        logger.info(f"--- {args.task} sample {i+1}/{args.samples} (gui_only) ---")
        s = run_one_sample(fixture, sample_i=i, host=args.host,
                           use_genui=args.genui, cost_model=cost_model)
        logger.info(f"sample {i+1}: ui_parse={s['ui_parse_ok']} "
                    f"form_submit={s['form_submit_ok']} "
                    f"round_trip={s['round_trip_score']} "
                    f"binding_f1={s['binding_f1']} "
                    f"n_ops={s['dispatch_n_ops']} n_applied={s['dispatch_n_applied']}")
        samples.append(s)
        # GUI executor writes hit the grounding model's QPM limit — between
        # samples let the quota refill (runtime is GUI-only; applies always).
        if i < args.samples - 1:
            logger.info("[gui] QPM refill: sleeping 60s before next sample")
            time.sleep(60)
    neg = run_neg_control(fixture, host=args.host, use_genui=args.genui)
    logger.info(f"[neg-control] {args.task}: score={neg['round_trip_score']} "
                f"(≤{NEG_CONTROL_MAX}) → {'PASS' if neg['passed'] else 'FAIL'}")
    sm = summarize(fixture, samples, neg)
    report = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "task": args.task, "execution_mode": "gui_only",
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
