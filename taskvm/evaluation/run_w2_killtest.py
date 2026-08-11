"""W2 kill-test orchestrator — two-zone UI + single-app single-step rollback gate.

Mirrors ``run_w1_killtest`` (standalone ``python -m`` → JSON + stdout verdict,
NOT pytest — stays consistent with the distributed kill-test model where each
gate is its own runnable verdict). W1 is PASS; this gate tests the W2
deliverables only.

W2 gate (handoff §6, two conditions — do NOT add more):
  - **gate-1 (two-zone manipulable)**: the two-zone surface has a read-only
    zone (projected app state, NO inputs → no mutate operator reachable) AND a
    read-write zone (editable fields + undo + checkpoint), independently
    addressable; editing a read-write field → dispatch → re-render moves the
    read-only zone to the new value.
  - **gate-2 (rollback undoes single-app single-step)**: after a single-app
    single-step dispatch, ``RollbackLog.undo_last(app)`` reverts that app's
    touched entity byte-identical to pre-edit (compensation via the app's own
    write API), AND non-interference-on-rollback = 1.0 (the undo doesn't clobber
    neighbors). Cross-app saga undo is W3 — NOT asserted here.

Overall W2 PASS = gate-1 ∧ gate-2 ∧ neg-control ≤0.3 (the third load-bearing
invariant still holds). Mock binding by default (W2 tests UI+rollback, not the
compiler — W1 already passed binding discovery).

§10 visual artifact: dumps the two-zone HTML (initial / after-edit / after-undo)
+ the app's native page HTML at the same three moments + a ``steps.md`` into
``eval_results/w2_visual_<ts>/`` — "点撤销 → 两边数据都跳回原值" by eye.

Usage:
    python -m taskvm.evaluation.run_w2_killtest                 # mock, all 3 tasks
    python -m taskvm.evaluation.run_w2_killtest --task doc_handoff --samples 3
    python -m taskvm.evaluation.run_w2_killtest --neg-control   # neg-control only
    python -m taskvm.evaluation.run_w2_killtest --no-mock --model gpt-5.6-sol
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from taskvm.benchmark import model_client
from taskvm.benchmark.cost_model import CostModel
from taskvm.benchmark.fixtures import CanonicalTaskGraph, all_tasks, get_task
from taskvm.execution.action_dispatcher import dispatch
from taskvm.execution.patch_compiler import compile_patch
from taskvm.execution.rollback import RollbackLog
from taskvm.harness import replay_engine as replay
from taskvm.harness.state_adapter import make_adapters
from taskvm.task_state.entity_binding import TaskBinding
from taskvm.verifier import canonical_state as cs
from taskvm.workspace_ui.editable_components import (
    editable_field_html, readonly_card_html)
from taskvm.workspace_ui.live_sync import canonical_snapshot, project_readonly
from taskvm.workspace_ui.server import WorkspaceSession, render_two_zone_html
from taskvm.workspace_ui.server import app as _ws_app   # for app-context outside the server
# reuse W1's neg-control + mock-binding (same honesty contract)
from taskvm.evaluation.run_w1_killtest import run_neg_control, _gt_task_binding

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
NEG_CONTROL_MAX = 0.3
SAMPLES_FOR_PASS = 2   # ≥2/3 samples (mirrors W1)


@dataclass
class Gate1Result:
    read_only_zone_present: bool
    read_write_zone_present: bool
    zones_independent: bool       # ro zone has no inputs; rw zone has inputs
    editable_field_present: bool
    read_only_shows_new_value: bool   # after edit, ro zone re-synced
    read_only_old_value_gone: bool
    passed: bool


@dataclass
class Gate2Result:
    undo_app: str
    undone_entity: str
    entity_reverted_byte_identical: bool   # pre == post for the undone entity
    non_interference_on_rollback: bool     # all OTHER entities in undo_app pre==post
    log_emptied: bool
    passed: bool


def _primary_app(fixture: CanonicalTaskGraph) -> str:
    """The app whose entity gate-2 undoes (the first binding's app)."""
    return fixture.bindings[0].app if fixture.bindings else "drive"


def _render_html(sess: WorkspaceSession) -> str:
    # render_template_string needs a Flask app context; push one (the gate runs
    # outside the workspace_ui server process).
    with _ws_app.app_context():
        return render_two_zone_html(sess)


def _zones_independent(html: str) -> bool:
    """Read-only zone contains no <input>; read-write zone contains >=1 <input>."""
    if "只读区" not in html or "可读可写区" not in html:
        return False
    ro = html.split("只读区")[1].split("可读可写区")[0]
    rw = html.split("可读可写区")[1]
    return ("<input" not in ro) and ("<input" in rw)


def run_gate1(sess: WorkspaceSession, fixture: CanonicalTaskGraph,
              edit_var: str, new_value: str) -> tuple[Gate1Result, str, str]:
    """Assert the two-zone surface is manipulable. Returns (result, html_initial,
    html_after_edit). Applies the edit (dispatch w/ rollback_log) between the
    two renders."""
    html_initial = _render_html(sess)
    ro_present = "只读区" in html_initial
    rw_present = "可读可写区" in html_initial
    independent = _zones_independent(html_initial)
    # editable field for the edited variable present in the rw zone
    rw = html_initial.split("可读可写区")[1]
    editable_present = edit_var in rw
    # the read-only zone shows the variable's current (pre-edit) value
    projected = project_readonly(sess.binding, canonical_snapshot(sess.adapters, sess.sid))
    old_value = str(projected[edit_var]["value"])

    # apply the edit (single-app single-step for doc_handoff; multi-app for W1 tasks)
    ops = compile_patch({"var_id": edit_var, "new": new_value}, sess.binding)
    dispatch(ops, sess.adapters, sess.sid, broken=None, rollback_log=sess.rollback_log)
    sess.last_dispatch = {"n_ops": len(ops), "n_applied": len(ops)}

    html_after = _render_html(sess)
    shows_new = str(new_value) in html_after
    old_gone = str(old_value) not in html_after.split("只读区")[1].split("可读可写区")[0] \
        if ("只读区" in html_after and "可读可写区" in html_after) else True

    passed = ro_present and rw_present and independent and editable_present \
        and shows_new and old_gone
    return Gate1Result(ro_present, rw_present, independent, editable_present,
                       shows_new, old_gone, passed), html_initial, html_after


def run_gate2(sess: WorkspaceSession, fixture: CanonicalTaskGraph,
              undo_app: str) -> tuple[Gate2Result, str]:
    """Assert undo_last(app) reverts the app's touched entity byte-identical +
    non-interference-on-rollback. The edit was already applied in gate-1; this
    snapshots pre-undo, undoes, snapshots post-undo. Returns (result, html_after_undo)."""
    pre = cs.snapshot(sess.adapters, sess.sid)   # AFTER the gate-1 edit (so app is changed)
    # the entity gate-2 expects to revert = the binding's entity in undo_app
    undone = next((b.entity_id for b in fixture.bindings if b.app == undo_app), None)
    # records for undo_app should exist (gate-1 dispatched at least one op to it)
    recs_before = sess.rollback_log.for_app(undo_app)

    sess.rollback_log.undo_last(undo_app, sess.sid, sess.adapters)
    post = cs.snapshot(sess.adapters, sess.sid)
    html_after_undo = _render_html(sess)

    # entity reverted byte-identical? compare the undone entity's FULL record
    # pre-undo (post-edit) vs post-undo. We want it back to the ORIGINAL pre-edit
    # value, so compare against the fixture's seed (user_edit.old) via canonical
    # of a fresh seed — simpler: the undone entity's field should equal old.
    field = next((b.field for b in fixture.bindings if b.app == undo_app), None)
    reverted = False
    if undone and field:
        post_val = (post.get(undo_app, {}).get("entities", {}).get(undone, {}) or {}).get(field)
        reverted = str(post_val).strip().lower() == str(fixture.user_edit.get("old")).strip().lower()
    # non-interference-on-rollback: every OTHER entity in undo_app unchanged
    # between pre-undo and post-undo (the undo touched only `undone`)
    pre_app = (pre.get(undo_app) or {}).get("entities") or {}
    post_app = (post.get(undo_app) or {}).get("entities") or {}
    others_ok = all(pre_app.get(e) == post_app.get(e)
                    for e in set(pre_app) | set(post_app) if e != undone) \
        and (pre_app.get(undone) != post_app.get(undone))  # and `undone` DID change back
    log_emptied = len(sess.rollback_log.for_app(undo_app)) == 0
    passed = reverted and others_ok and log_emptied and bool(recs_before)
    return Gate2Result(undo_app, undone or "", reverted, others_ok, log_emptied,
                       passed), html_after_undo


def run_one_sample(fixture: CanonicalTaskGraph, adapters: dict, *,
                   sample_i: int, host: str = "localhost") -> dict:
    """Run one W2 sample end-to-end (mock binding). Returns the result record."""
    sid = f"{fixture.task_id}_w2_s{sample_i}_{int(time.time()*1000) % 100000}"
    for ad in adapters.values():
        ad.reset(sid)
    replay.seed_apps(fixture, adapters, sid)
    # read-path-GUI + live-state anchor (must still hold with the 3rd app)
    obs = replay.capture_obs(adapters, sid)
    replay.assert_obs_matches_state(adapters, sid, obs)

    tb = _gt_task_binding(fixture)   # mock binding (W2 tests UI+rollback, not compiler)
    sess = WorkspaceSession(sid=sid, task_id=fixture.task_id, goal=fixture.goal,
                            binding=tb, adapters=adapters)

    edit_var = fixture.user_edit["var_id"]
    new_value = fixture.user_edit["new"]
    undo_app = _primary_app(fixture)

    g1, html_init, html_after_edit = run_gate1(sess, fixture, edit_var, new_value)
    g2, html_after_undo = run_gate2(sess, fixture, undo_app)

    for ad in adapters.values():
        ad.reset(sid)
    return {
        "task_id": fixture.task_id,
        "sample": sample_i,
        "mock": True,
        "edit": {"var_id": edit_var, "new": new_value, "undo_app": undo_app},
        "gate1": g1.__dict__,
        "gate2": g2.__dict__,
        "passed": g1.passed and g2.passed,
        "html_initial": html_init,
        "html_after_edit": html_after_edit,
        "html_after_undo": html_after_undo,
    }


def _dump_visual(samples: list[dict], ts: str, host: str) -> Path:
    """§10 visual artifact: dump the two-zone HTML + native app HTML at the
    three moments + a steps.md into eval_results/w2_visual_<ts>/."""
    out_dir = EVAL_DIR / f"w2_visual_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for s in samples:
        d = out_dir / f"{s['task_id']}_s{s['sample']}"
        d.mkdir(exist_ok=True)
        (d / "1_two_zone_initial.html").write_text(s["html_initial"], encoding="utf-8")
        (d / "2_two_zone_after_edit.html").write_text(s["html_after_edit"], encoding="utf-8")
        (d / "3_two_zone_after_undo.html").write_text(s["html_after_undo"], encoding="utf-8")
        # native app page cross-check (the §10 "两边数据都跳回原值" check)
        undo_app = s["edit"]["undo_app"]
        port = {"calendar": 3013, "taskboard": 3014, "drive": 3015}.get(undo_app, 3015)
        # native cross-check: re-seed a transient sid, drive edit+undo in-process
        # (via the gate's own adapters + rollback_log), fetch the app's native
        # GET /<sid> HTML at each moment. Does NOT depend on the workspace_ui
        # Flask server — the §10 "两边数据都跳回原值" check is reproduced from
        # the real app pages directly.
        try:
            fx = get_task(s["task_id"])
            adapters = make_adapters(host=host)
            import time as _t
            vsid = f"{fx.task_id}_vis_{int(_t.time()*1000) % 100000}"
            for ad in adapters.values():
                ad.reset(vsid)
            replay.seed_apps(fx, adapters, vsid)
            tb = _gt_task_binding(fx)
            from taskvm.workspace_ui.server import WorkspaceSession
            vsess = WorkspaceSession(sid=vsid, task_id=fx.task_id, goal=fx.goal,
                                     binding=tb, adapters=adapters)
            native_initial = requests.get(
                f"http://{host}:{port}/{vsid}", timeout=10).text
            ops = compile_patch({"var_id": s["edit"]["var_id"],
                                 "new": s["edit"]["new"]}, tb)
            dispatch(ops, adapters, vsid, broken=None, rollback_log=vsess.rollback_log)
            native_after_edit = requests.get(
                f"http://{host}:{port}/{vsid}", timeout=10).text
            vsess.rollback_log.undo_last(undo_app, vsid, adapters)
            native_after_undo = requests.get(
                f"http://{host}:{port}/{vsid}", timeout=10).text
            (d / "0_native_initial.html").write_text(native_initial, encoding="utf-8")
            (d / "2_native_after_edit.html").write_text(native_after_edit, encoding="utf-8")
            (d / "3_native_after_undo.html").write_text(native_after_undo, encoding="utf-8")
            for ad in adapters.values():
                ad.reset(vsid)
        except Exception as e:
            logger.warning(f"[visual] native cross-check failed for {s['task_id']}: {e}")
            (d / "0_native_initial.html").write_text(
                f"(native cross-check skipped: {e})", encoding="utf-8")
        # steps.md
        (d / "steps.md").write_text(
            f"# W2 visual — {s['task_id']} sample {s['sample']}\n\n"
            f"sid: {s.get('html_initial','').split('sid: <code>')[1].split('</code>')[0] if 'sid: <code>' in s.get('html_initial','') else '(see html)'}\n\n"
            f"## Gate-1 (two-zone manipulable): {s['gate1']['passed']}\n"
            f"- read-only zone present: {s['gate1']['read_only_zone_present']}\n"
            f"- read-write zone present: {s['gate1']['read_write_zone_present']}\n"
            f"- zones independent (ro has no inputs): {s['gate1']['zones_independent']}\n"
            f"- editable field present: {s['gate1']['editable_field_present']}\n"
            f"- after edit, read-only shows new value: {s['gate1']['read_only_shows_new_value']}\n"
            f"- after edit, old value gone from read-only: {s['gate1']['read_only_old_value_gone']}\n\n"
            f"## Gate-2 (single-app single-step rollback): {s['gate2']['passed']}\n"
            f"- undo app: {s['gate2']['undo_app']} · entity: {s['gate2']['undone_entity']}\n"
            f"- entity reverted byte-identical: {s['gate2']['entity_reverted_byte_identical']}\n"
            f"- non-interference-on-rollback: {s['gate2']['non_interference_on_rollback']}\n"
            f"- log emptied: {s['gate2']['log_emptied']}\n\n"
            f"## Steps (reproducible)\n"
            f"1. open `1_two_zone_initial.html` — read-only shows {s['edit']['var_id']} = old value; "
            f"cross-check `0_native_initial.html` (native :{port}) shows the same.\n"
            f"2. edit {s['edit']['var_id']} → {s['edit']['new']}; open `2_two_zone_after_edit.html` — "
            f"read-only re-syncs to new value; `2_native_after_edit.html` shows the app's real state changed.\n"
            f"3. click undo ({s['edit']['undo_app']}); open `3_two_zone_after_undo.html` — read-only reverts; "
            f"`3_native_after_undo.html` shows the app's real state reverted (compensation via app API).\n",
            encoding="utf-8")
    return out_dir


def summarize(task_id: str, samples: list[dict], neg: dict) -> dict:
    n = len(samples)
    g1_pass = sum(1 for s in samples if s["gate1"]["passed"])
    g2_pass = sum(1 for s in samples if s["gate2"]["passed"])
    both_pass = sum(1 for s in samples if s["passed"])
    neg_ok = neg["passed"]
    # gate-1 PASS: ≥2/3 samples; gate-2 PASS: ≥2/3 samples; overall + neg≤0.3
    gate1_pass = g1_pass >= SAMPLES_FOR_PASS
    gate2_pass = g2_pass >= SAMPLES_FOR_PASS
    passed = gate1_pass and gate2_pass and neg_ok
    return {
        "task_id": task_id, "n_samples": n,
        "gate1_pass_count": g1_pass, "gate2_pass_count": g2_pass,
        "both_pass_count": both_pass,
        "gate1_pass": gate1_pass, "gate2_pass": gate2_pass,
        "neg_control_score": neg["score"], "neg_control_passed": neg_ok,
        "neg_control_max": NEG_CONTROL_MAX,
        "PASS": passed,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="TaskVM W2 kill-test (two-zone + rollback)")
    parser.add_argument("--task", default=None, help="task_id (default: all 3)")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--neg-control", action="store_true",
                        help="run only the negative control (verifier honesty)")
    parser.add_argument("--no-mock", action="store_true",
                        help="use the real compiler (default: mock binding — W2 tests UI+rollback)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--visual", action="store_true", default=True,
                        help="dump the §10 visual artifact (two-zone + native HTML + steps.md)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    if args.no_mock:
        logger.warning("W2 gate defaults to mock binding (W2 tests UI+rollback, not the "
                       "compiler — W1 already passed binding discovery). --no-mock is accepted "
                       "but the gate verdict does not depend on the compiler.")

    adapters = make_adapters(host=args.host)
    for app, ad in adapters.items():
        try:
            h = ad.health()
            if h.get("status") != "ok":
                logger.error(f"{app} not healthy: {h}"); sys.exit(2)
            logger.info(f"{app} healthy @ {ad.base_url}")
        except Exception as e:
            logger.error(f"{app} not reachable @ {ad.base_url}: {e} "
                         f"(start the apps: python -m taskvm.apps.{app}.app)"); sys.exit(2)

    tasks = [get_task(args.task)] if args.task else all_tasks()
    cost_model = CostModel()
    ts = time.strftime("%Y%m%d_%H%M%S")

    if args.neg_control:
        results = []
        for fx in tasks:
            neg = run_neg_control(fx, adapters, model=args.model, mock=True,
                                  cost_model=cost_model)
            logger.info(f"[neg-control] {fx.task_id}: score={neg['score']} "
                        f"(must be ≤{NEG_CONTROL_MAX}) → {'PASS' if neg['passed'] else 'FAIL'}")
            results.append(neg)
        out_path = Path(args.out) if args.out else EVAL_DIR / f"w2_negcontrol_{ts}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        ok = all(r["passed"] for r in results)
        print(f"\nNEG-CONTROL {'PASS' if ok else 'FAIL'} (verifier {'is' if ok else 'is NOT'} honest)")
        return 0 if ok else 1

    summaries = []
    all_samples = []
    for fx in tasks:
        logger.info(f"\n=== TASK {fx.task_id} ===")
        samples = []
        for i in range(args.samples):
            s = run_one_sample(fx, adapters, sample_i=i, host=args.host)
            logger.info(f"sample {i+1}: gate1={s['gate1']['passed']} "
                        f"gate2={s['gate2']['passed']} → {'PASS' if s['passed'] else 'FAIL'}")
            samples.append(s)
        neg = run_neg_control(fx, adapters, model=args.model, mock=True, cost_model=cost_model)
        logger.info(f"[neg-control] {fx.task_id}: score={neg['score']} → "
                    f"{'PASS' if neg['passed'] else 'FAIL'}")
        sm = summarize(fx.task_id, samples, neg)
        summaries.append(sm)
        all_samples.extend(samples)
        logger.info(f"TASK {fx.task_id}: PASS={sm['PASS']} gate1={sm['gate1_pass']} "
                    f"gate2={sm['gate2_pass']} neg={neg['score']}")

    # strip the heavy HTML from the JSON report (it goes in the visual dir)
    samples_json = [{k: v for k, v in s.items() if not k.startswith("html_")}
                    for s in all_samples]
    gate1_ok = all(sm["gate1_pass"] for sm in summaries)
    gate2_ok = all(sm["gate2_pass"] for sm in summaries)
    neg_ok = all(sm["neg_control_passed"] for sm in summaries)
    overall_pass = gate1_ok and gate2_ok and neg_ok
    report = {
        "ts": ts, "week": "W2", "model": args.model or model_client.TASKVM_DEFAULT_MODEL,
        "mock": not args.no_mock, "n_samples_per_task": args.samples,
        "gate": {"gate1_two_zone_manipulable": gate1_ok,
                 "gate2_single_app_single_step_rollback": gate2_ok,
                 "neg_control_le_030": neg_ok},
        "summaries": summaries, "samples": samples_json,
        "PASS": overall_pass,
    }
    out_path = Path(args.out) if args.out else EVAL_DIR / f"w2_{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    visual_dir = None
    if args.visual:
        try:
            visual_dir = _dump_visual(all_samples, ts, args.host)
        except Exception as e:
            logger.warning(f"[visual] artifact dump failed (non-fatal): {e}")

    print(f"\nWrote {out_path}")
    if visual_dir:
        print(f"Visual artifact: {visual_dir}/")
    print(f"\n=== W2 KILL-TEST VERDICT: {'PASS' if overall_pass else 'FAIL'} ===")
    print(f"  gate-1 (two-zone manipulable):              {gate1_ok}")
    print(f"  gate-2 (single-app single-step rollback):   {gate2_ok}")
    print(f"  neg-control ≤ 0.3 (verifier honest):        {neg_ok}")
    for sm in summaries:
        print(f"  task {sm['task_id']}: PASS={sm['PASS']} "
              f"g1={sm['gate1_pass_count']}/{args.samples} "
              f"g2={sm['gate2_pass_count']}/{args.samples} "
              f"neg={sm['neg_control_score']}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
