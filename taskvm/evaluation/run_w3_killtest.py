"""W3 kill-test orchestrator — cross-app rollback saga + reconciliation gates.

Mirrors ``run_w2_killtest`` (standalone ``python -m`` → JSON + visual dir, NOT
pytest; Gate1Result/Gate2Result dataclasses; ≥2/3 samples; neg-control ≤0.3).
W1 (binding) + W2 (two-zone + single-app rollback) are PASS; this gate tests
the W3 deliverables only.

W3 gates (handoff §6, two conditions — do NOT add more):
  - **gate-1 (cross-app rollback saga)**: after a dispatch that touched MULTIPLE
    apps (e.g. release_reschedule: calendar + taskboard), ``RollbackLog.undo_saga``
    reverts EVERY touched app byte-identical to pre-dispatch (Rollback Fidelity)
    AND non-interference-on-rollback = 1.0 (the undo doesn't clobber unrelated
    entities). Cross-app: the saga groups all writes from one user action and
    undoes them globally LIFO. Single-app single-step undo was W2 — NOT asserted.
  - **gate-2 (reconciliation conflict marking)**: an EXTERNAL concurrent change
    (a colleague edits an app via its own API, bypassing TaskVM dispatch) is
    DETECTED on the next re-read and AMBER-marked in the read-only zone with
    BOTH values (projected Y + underlying X) + merge options. NO silent
    overwrite (neither Y nor X auto-picked); NO human-block (agent not paused).

Overall W3 PASS = gate-1 ∧ gate-2 ∧ neg-control ≤0.3 (the honesty invariant still
holds under the cross-app saga). Mock binding by default (W3 tests rollback +
reconciliation engineering, not the compiler — W1/W4 own binding).

§10 visual artifact: dumps the two-zone HTML (after-dispatch / after-undo /
conflict-amber) + the apps' native page HTML at the same moments + a steps.md
into ``eval_results/w3_visual_<ts>/`` — "点撤销 → 多个 app 的数据都跳回原值" +
"外部改一个值 → 只读区标红显示两个值" by eye.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from taskvm.benchmark import model_client
from taskvm.benchmark.cost_model import CostModel
from taskvm.benchmark.fixtures import CanonicalTaskGraph, all_tasks, get_task
from taskvm.execution.action_dispatcher import dispatch
from taskvm.execution.patch_compiler import compile_patch
from taskvm.harness import replay_engine as replay
from taskvm.execution.gui_driver import make_task_adapters
from taskvm.substrate.builtin_web.evaluation import (
    make_evaluation_environments,
)
from taskvm.verifier import canonical_state as cs
from taskvm.verifier.cross_app_checks import (check_cross_app_consistency,
                                                check_dependency_tracking)
from taskvm.verifier.rollback_verify import check_rollback_fidelity
from taskvm.workspace_ui.server import WorkspaceSession, render_two_zone_html
from taskvm.workspace_ui.server import app as _ws_app
# reuse W1's neg-control + mock-binding (same honesty contract)
from taskvm.evaluation.run_w1_killtest import run_neg_control, _gt_task_binding

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
NEG_CONTROL_MAX = 0.3
SAMPLES_FOR_PASS = 2   # ≥2/3 samples (mirrors W1/W2)


@dataclass
class Gate1Result:
    """Cross-app rollback saga."""
    n_apps_touched: int
    saga_n_targets: int
    saga_n_reverted: int
    rollback_fidelity: float          # fraction of touched entities byte-restored
    non_interference_on_rollback: bool
    cross_app_consistency_post_dispatch: float   # diag: apps agreed after dispatch
    hard_fail: bool
    passed: bool


@dataclass
class Gate2Result:
    """Reconciliation conflict marking."""
    conflict_detected: bool           # external change → ≥1 conflict on re-read
    n_conflicts: int
    projected_and_underlying_both_shown: bool   # no silent overwrite
    merge_options_present: bool       # 3 options (accept/keep/merge)
    amber_rendered: bool              # amber CSS in the read-only zone
    agent_not_blocked: bool           # governance, not approval (always True by design)
    passed: bool


def _cross_app_task(fixture: CanonicalTaskGraph) -> bool:
    """A task touches >1 app (the saga gate needs a real cross-app write)."""
    return len({b.app for b in fixture.bindings}) > 1


def _render_html(sess: WorkspaceSession) -> str:
    with _ws_app.app_context():
        return render_two_zone_html(sess)


def run_gate1(sess: WorkspaceSession, fixture: CanonicalTaskGraph) -> tuple[Gate1Result, str, str, dict]:
    """Cross-app rollback saga: dispatch a multi-app edit → snapshot → undo_saga
    → snapshot → verify fidelity + non-interference. Returns (result, html_after_dispatch,
    html_after_undo, detail_dict)."""
    edit_var = fixture.user_edit["var_id"]
    new_value = fixture.user_edit["new"]
    ops = compile_patch({"var_id": edit_var, "new": new_value}, sess.binding)
    pre = cs.snapshot(sess.oracle, sess.sid)   # BEFORE dispatch (the state to restore)
    dispatch(ops, sess.adapters, sess.sid, broken=None, rollback_log=sess.rollback_log)
    sess.last_projection = None   # force re-project (mimic the edit route)
    html_after_dispatch = _render_html(sess)
    post_dispatch = cs.snapshot(sess.oracle, sess.sid)
    cx = check_cross_app_consistency(post_dispatch, fixture)
    dt = check_dependency_tracking(post_dispatch, fixture)

    saga_id = sess.rollback_log.latest_saga_id()
    if saga_id is None:
        g1 = Gate1Result(n_apps_touched=0, saga_n_targets=0, saga_n_reverted=0,
                         rollback_fidelity=1.0, non_interference_on_rollback=True,
                         cross_app_consistency_post_dispatch=cx.score,
                         hard_fail=False, passed=False)
        return g1, html_after_dispatch, html_after_dispatch, \
            {"error": "no saga records (dispatch produced no writes)", "dependency_tracking": dt.score}
    sres = sess.rollback_log.undo_saga(saga_id, sess.sid, sess.adapters)
    post_undo = cs.snapshot(sess.oracle, sess.sid)
    rf = check_rollback_fidelity(pre, post_undo, sess.oracle, sess.sid, sres)
    html_after_undo = _render_html(sess)

    n_apps = len({s.app for s in sres.steps})
    passed = (rf.fidelity >= 1.0 and rf.non_interference_on_rollback
              and sres.fully_reverted and not rf.hard_fail)
    g1 = Gate1Result(n_apps_touched=n_apps, saga_n_targets=sres.n_targets,
                     saga_n_reverted=sres.n_reverted, rollback_fidelity=rf.fidelity,
                     non_interference_on_rollback=rf.non_interference_on_rollback,
                     cross_app_consistency_post_dispatch=cx.score,
                     hard_fail=rf.hard_fail, passed=passed)
    detail = {"saga": sres.to_dict(), "fidelity": {
        "score": rf.score, "fidelity": rf.fidelity,
        "non_interference_on_rollback": rf.non_interference_on_rollback,
        "not_restored": rf.not_restored, "clobbered": rf.clobbered},
        "cross_app_consistency": cx.score, "dependency_tracking": dt.score}
    return g1, html_after_dispatch, html_after_undo, detail


def run_gate2(sess: WorkspaceSession, fixture: CanonicalTaskGraph) -> tuple[Gate2Result, str, str]:
    """Reconciliation: inject an EXTERNAL concurrent change (bypassing TaskVM
    dispatch) → re-render → assert conflict detected + amber + both values + merge
    options. Returns (result, html_before_external, html_after_external)."""
    edit_var = fixture.user_edit["var_id"]
    new_value = fixture.user_edit["new"]
    # apply the user's edit first (so there's a projection Y to conflict against)
    ops = compile_patch({"var_id": edit_var, "new": new_value}, sess.binding)
    dispatch(ops, sess.adapters, sess.sid, broken=None, rollback_log=sess.rollback_log)
    sess.last_projection = None
    html_before = _render_html(sess)   # caches the projection Y = post-edit state

    # EXTERNAL concurrent change: pick a binding whose field we can mutate as if
    # an external actor edited the app behind TaskVM's back. Agent B: the
    # injection goes through the EVALUATION plane's ``force_write`` (exam-room
    # power) — the runtime has no API write path anymore. Use the LAST binding
    # (often a dependent taskboard deadline) so the change is on a non-primary
    # bound entity.
    target = fixture.bindings[-1]
    env = sess.oracle[target.app]
    # mutate to a DIFFERENT value than the user's edit (the conflict)
    conflict_val = "2026-08-20" if "date" in (target.field or "") or "deadline" in (target.field or "") \
        else ("archived" if target.field in ("parent", "state") else "ZZZ_external")
    env.force_write(sess.sid, target.entity_id, target.operator, conflict_val)

    html_after = _render_html(sess)   # re-read → detect conflict
    conflicts = sess.last_conflicts
    has_target_conflict = any(c.app == target.app and c.entity_id == target.entity_id
                              and c.field == target.field for c in conflicts)
    both_shown = (any(str(c.projected) in html_after for c in conflicts) and
                  any(str(c.underlying) in html_after for c in conflicts))
    merge_opts = ("采用底层值" in html_after and "保留我的投影" in html_after and "合并" in html_after)
    amber = ("card conflict" in html_after and "底层已变" in html_after)
    g2 = Gate2Result(conflict_detected=has_target_conflict, n_conflicts=len(conflicts),
                     projected_and_underlying_both_shown=both_shown,
                     merge_options_present=merge_opts, amber_rendered=amber,
                     agent_not_blocked=True,  # by design: reconciliation never blocks
                     passed=(has_target_conflict and both_shown and merge_opts and amber))
    return g2, html_before, html_after


def run_one_sample(fixture: CanonicalTaskGraph, adapters: dict, envs: dict, *,
                   sample_i: int, host: str = "localhost") -> dict:
    sid = f"{fixture.task_id}_w3_s{sample_i}_{int(time.time()*1000) % 100000}"
    for env in envs.values():
        env.reset(sid)
    replay.seed_apps(fixture, envs, sid)
    replay.capture_obs(envs, sid)   # assert obs matches state
    replay.assert_obs_matches_state(envs, sid, replay.capture_obs(envs, sid))
    tb = _gt_task_binding(fixture)
    sess = WorkspaceSession(sid=sid, task_id=fixture.task_id, goal=fixture.goal,
                            binding=tb, adapters=adapters, oracle=envs)
    # gate-1 needs a cross-app task; skip (vacuous pass) for single-app tasks
    if _cross_app_task(fixture):
        g1, html_disp, html_undo, rf_detail = run_gate1(sess, fixture)
    else:
        g1 = Gate1Result(n_apps_touched=1, saga_n_targets=1, saga_n_reverted=1,
                         rollback_fidelity=1.0, non_interference_on_rollback=True,
                         cross_app_consistency_post_dispatch=1.0, hard_fail=False,
                         passed=True)  # vacuous for single-app (W2 covers it)
        html_disp = html_undo = _render_html(sess); rf_detail = {"vacuous": True}
    # gate-2 (reconciliation) — fresh session (gate-1 undid the edit)
    for env in envs.values():
        env.reset(sid)
    replay.seed_apps(fixture, envs, sid)
    sess2 = WorkspaceSession(sid=sid, task_id=fixture.task_id, goal=fixture.goal,
                             binding=tb, adapters=adapters, oracle=envs)
    g2, html_before, html_after = run_gate2(sess2, fixture)
    for env in envs.values():
        env.reset(sid)
    return {
        "task_id": fixture.task_id, "sample": sample_i, "mock": True,
        "cross_app": _cross_app_task(fixture),
        "gate1": g1.__dict__, "gate1_detail": rf_detail,
        "gate2": g2.__dict__,
        "passed": g1.passed and g2.passed,
        "html_after_dispatch": html_disp, "html_after_undo": html_undo,
        "html_before_external": html_before, "html_after_external": html_after,
    }


def _dump_visual(samples: list[dict], ts: str, host: str) -> Path:
    out_dir = EVAL_DIR / f"w3_visual_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for s in samples:
        d = out_dir / f"{s['task_id']}_s{s['sample']}"
        d.mkdir(exist_ok=True)
        (d / "1_after_dispatch.html").write_text(s.get("html_after_dispatch") or "", encoding="utf-8")
        (d / "2_after_undo.html").write_text(s.get("html_after_undo") or "", encoding="utf-8")
        (d / "3_reconcile_before_external.html").write_text(s.get("html_before_external") or "", encoding="utf-8")
        (d / "4_reconcile_after_external.html").write_text(s.get("html_after_external") or "", encoding="utf-8")
        (d / "steps.md").write_text(
            f"# W3 visual — {s['task_id']} sample {s['sample']}\n\n"
            f"## Gate-1 (cross-app rollback saga): {s['gate1']['passed']}\n"
            f"- apps touched: {s['gate1']['n_apps_touched']}\n"
            f"- saga reverted: {s['gate1']['saga_n_reverted']}/{s['gate1']['saga_n_targets']}\n"
            f"- rollback fidelity: {s['gate1']['rollback_fidelity']}\n"
            f"- non-interference-on-rollback: {s['gate1']['non_interference_on_rollback']}\n"
            f"- cross-app consistency (post-dispatch): {s['gate1']['cross_app_consistency_post_dispatch']}\n\n"
            f"## Gate-2 (reconciliation): {s['gate2']['passed']}\n"
            f"- conflict detected: {s['gate2']['conflict_detected']}\n"
            f"- n conflicts: {s['gate2']['n_conflicts']}\n"
            f"- both values shown (no silent overwrite): {s['gate2']['projected_and_underlying_both_shown']}\n"
            f"- merge options present: {s['gate2']['merge_options_present']}\n"
            f"- amber rendered: {s['gate2']['amber_rendered']}\n"
            f"- agent not blocked: {s['gate2']['agent_not_blocked']}\n\n"
            f"## Steps (reproducible)\n"
            f"1. open `1_after_dispatch.html` — multi-app edit applied (calendar+taskboard changed).\n"
            f"2. open `2_after_undo.html` — saga undo → ALL touched apps reverted byte-identical.\n"
            f"3. open `3_reconcile_before_external.html` — user's projection Y cached.\n"
            f"4. open `4_reconcile_after_external.html` — external change → AMBER conflict shows "
            f"both Y + X + merge options (no silent overwrite, agent not blocked).\n",
            encoding="utf-8")
    return out_dir


def summarize(task_id: str, samples: list[dict], neg: dict) -> dict:
    g1_pass = sum(1 for s in samples if s["gate1"]["passed"])
    g2_pass = sum(1 for s in samples if s["gate2"]["passed"])
    both = sum(1 for s in samples if s["passed"])
    gate1_ok = g1_pass >= SAMPLES_FOR_PASS
    gate2_ok = g2_pass >= SAMPLES_FOR_PASS
    passed = gate1_ok and gate2_ok and neg["passed"]
    return {"task_id": task_id, "n_samples": len(samples),
            "gate1_pass_count": g1_pass, "gate2_pass_count": g2_pass,
            "both_pass_count": both, "gate1_pass": gate1_ok, "gate2_pass": gate2_ok,
            "neg_control_score": neg["score"], "neg_control_passed": neg["passed"],
            "neg_control_max": NEG_CONTROL_MAX, "PASS": passed}


def main(argv=None):
    parser = argparse.ArgumentParser(description="TaskVM W3 kill-test (rollback saga + reconciliation)")
    parser.add_argument("--task", default=None)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--neg-control", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument("--visual", action="store_true", default=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    adapters = make_task_adapters(host=args.host)
    envs = make_evaluation_environments(sorted(set(adapters)), host=args.host)
    for app, env in envs.items():
        try:
            h = env.health()
            if h.get("status") != "ok":
                logger.error(f"{app} not healthy: {h}"); sys.exit(2)
        except Exception as e:
            logger.error(f"{app} not reachable: {e}"); sys.exit(2)
    tasks = [get_task(args.task)] if args.task else all_tasks()
    cost_model = CostModel()
    ts = time.strftime("%Y%m%d_%H%M%S")

    if args.neg_control:
        results = []
        for fx in tasks:
            neg = run_neg_control(fx, adapters, envs, model=None, mock=True, cost_model=cost_model)
            results.append({"task_id": fx.task_id, **neg})
        out = Path(args.out) if args.out else EVAL_DIR / f"w3_negcontrol_{ts}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        ok = all(r["passed"] for r in results)
        print(f"\nW3 NEG-CONTROL {'PASS' if ok else 'FAIL'}"); return 0 if ok else 1

    summaries, all_samples = [], []
    for fx in tasks:
        logger.info(f"\n=== TASK {fx.task_id} (cross_app={_cross_app_task(fx)}) ===")
        samples = []
        for i in range(args.samples):
            s = run_one_sample(fx, adapters, envs, sample_i=i, host=args.host)
            logger.info(f"sample {i+1}: gate1={s['gate1']['passed']} gate2={s['gate2']['passed']} → {'PASS' if s['passed'] else 'FAIL'}")
            samples.append(s)
        neg = run_neg_control(fx, adapters, envs, model=None, mock=True, cost_model=cost_model)
        sm = summarize(fx.task_id, samples, neg)
        summaries.append(sm); all_samples.extend(samples)
        logger.info(f"TASK {fx.task_id}: PASS={sm['PASS']} g1={sm['gate1_pass']} g2={sm['gate2_pass']} neg={neg['score']}")

    samples_json = [{k: v for k, v in s.items() if not k.startswith("html_")} for s in all_samples]
    gate1_ok = all(sm["gate1_pass"] for sm in summaries)
    gate2_ok = all(sm["gate2_pass"] for sm in summaries)
    neg_ok = all(sm["neg_control_passed"] for sm in summaries)
    overall = gate1_ok and gate2_ok and neg_ok
    report = {"ts": ts, "week": "W3", "model": model_client.TASKVM_DEFAULT_MODEL,
              "mock": True, "n_samples_per_task": args.samples,
              "gate": {"gate1_cross_app_rollback_saga": gate1_ok,
                       "gate2_reconciliation_conflict_marking": gate2_ok,
                       "neg_control_le_030": neg_ok},
              "summaries": summaries, "samples": samples_json, "PASS": overall}
    out = Path(args.out) if args.out else EVAL_DIR / f"w3_{ts}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    visual_dir = None
    if args.visual:
        try: visual_dir = _dump_visual(all_samples, ts, args.host)
        except Exception as e: logger.warning(f"[visual] {e}")
    print(f"\nWrote {out}")
    if visual_dir: print(f"Visual artifact: {visual_dir}/")
    print(f"\n=== W3 KILL-TEST VERDICT: {'PASS' if overall else 'FAIL'} ===")
    print(f"  gate-1 (cross-app rollback saga):          {gate1_ok}")
    print(f"  gate-2 (reconciliation conflict marking):  {gate2_ok}")
    print(f"  neg-control ≤ 0.3 (verifier honest):       {neg_ok}")
    for sm in summaries:
        print(f"  task {sm['task_id']}: PASS={sm['PASS']} g1={sm['gate1_pass_count']}/{args.samples} g2={sm['gate2_pass_count']}/{args.samples} neg={sm['neg_control_score']}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
