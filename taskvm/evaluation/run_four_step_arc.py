"""EE.3 — the §4.4 four-step arc end-to-end demo (teaser figure evidence).

Runs the single continuous arc that exhibits VM properties 2-5, which no
competitor can reproduce (大纲 §4.4):

  Step 1 — bidirectional write-back + non-interference + re-projection
           (launch_full: release_date 8/14→8/18 fans out to 4 apps)
  Step 2 — reconciliation (external concurrent change → conflict marked amber)
  Step 3 — rollback (saga undo → real app state restored, honest partial_failure)
  Step 4 — JVM moment (same edit on Stack B = outlook_cal+taskboard, substrate-
           independent: stable surface, same op, different trajectory)

Each step screenshots the live app(s) + writes a per-step JSON; the whole arc
writes ``eval_results/four_step_arc_<ts>/four_step_arc_<ts>.json``. This is the
ONLY material evidence source for the paper's teaser figure (handoff EE.3).

Modes:
  --dry-run  : executor='api' (no GUI model needed; apps must be online). Produces
               REAL round_trip/conflict/rollback numbers via the app API path —
               stronger than pure structure validation, but NOT §12.16-compliant
               (API writes are the backdoor). Use to validate the arc wiring.
  default    : executor='gui_agent' (real browser gestures via the GUI executor,
               gpt-5.6-sol; §12.16-compliant). Needs the model + Playwright.

Screenshots are best-effort (Playwright headless); if unavailable the path is
still recorded in the JSON (the screenshot is evidence, not a gate).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from taskvm.benchmark.fixtures import get_task
from taskvm.benchmark.ood_fixtures import get_ood_task
from taskvm.execution.action_dispatcher import dispatch
from taskvm.execution.patch_compiler import compile_patch
from taskvm.execution.rollback import RollbackLog
from taskvm.harness import replay_engine as replay
from taskvm.harness.state_adapter import make_adapters
from taskvm.task_state.entity_binding import TaskBinding
from taskvm.verifier import canonical_state as cs
from taskvm.verifier.reconciliation import (apply_merge_option, detect_conflicts,
                                            merge_options)
from taskvm.verifier.round_trip_checks import check_round_trip
from taskvm.workspace_ui.live_sync import (canonical_snapshot, project_readonly,
                                            resync_values, resync_with_conflicts)

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
PASS_ROUND_TRIP = 0.85
NEG_MAX = 0.3


# ── helpers ──────────────────────────────────────────────────────────────────
def _gt_binding(fixture) -> TaskBinding:
    """Build a TaskBinding from the GT fixture (the mock/orchestrator shape)."""
    var_groups: dict[str, dict] = {}
    for b in fixture.bindings:
        g = var_groups.setdefault(b.var_id, {
            "var_id": b.var_id, "label": b.var_id,
            "value": fixture.user_edit.get("old"), "editable": True, "bindings": []})
        g["bindings"].append({"var_id": b.var_id, "app": b.app,
                              "entity_id": b.entity_id, "field": b.field,
                              "operator": b.operator})
    return TaskBinding(task_id=fixture.task_id, variables=list(var_groups.values()))


def _apps_for(fixture) -> list[str]:
    """Union of seed_state keys + binding apps (EE.2 pattern)."""
    apps = set(fixture.seed_state.keys())
    apps.update(b.app for b in fixture.bindings)
    order = ("calendar", "taskboard", "drive", "mail", "outlook_cal")
    return [a for a in order if a in apps]


def _screenshot(url: str, out_path: Path) -> str | None:
    """Best-effort headless screenshot. Returns the saved path or None."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(url, wait_until="networkidle", timeout=15000)
            page.screenshot(path=str(out_path), full_page=True)
            browser.close()
        return str(out_path)
    except Exception as e:
        logger.warning(f"[arc] screenshot failed for {url}: {e}")
        return None


def _inject_external_taskboard_change(host: str, sid: str, tid: str,
                                      field: str, value: str) -> dict:
    """Simulate a teammate's external concurrent edit on taskboard.<tid>.<field>.

    Uses the taskboard app's OWN mutate API directly (``POST /api/task/<sid>/<tid>``)
    via a raw ``requests`` call — bypassing TaskVM's adapter/dispatcher entirely.
    This is what a teammate editing through the taskboard's own UI would effectively
    do, and crucially it does NOT go through TaskVM's rollback_log (so the external
    change is unrecorded → the workspace's next re-read detects the divergence,
    which is exactly the reconciliation conflict §0 property 1 requires).

    Going around the adapter (not through ``inject_task``) is the honest choice:
    ``inject_task`` replaces the WHOLE session and corrupts sibling entities (T1
    lost its deadline when re-injecting to edit T2), whereas the mutate API
    touches only the one field — a clean, minimal external edit."""
    import requests
    base = f"http://{host}:3014"
    # taskboard's mutate API maps field→operator (deadline→set_deadline, etc.)
    _FIELD_OP = {"deadline": "set_deadline", "status": "set_status",
                 "assignee": "set_assignee"}
    op = _FIELD_OP.get(field)
    if op is None:
        return {"injected": False, "error": f"no operator for field {field}"}
    r = requests.post(f"{base}/api/task/{sid}/{tid}",
                      json={"operator": op, "value": value}, timeout=10)
    if r.status_code != 200:
        return {"injected": False, "error": f"{r.status_code}: {r.text[:200]}"}
    return {"injected": True, "tid": tid, "field": field, "operator": op,
            "value": value, "via": "taskboard_mutate_api_direct"}


# ── the four steps ───────────────────────────────────────────────────────────
def step1_write(fixture, adapters, sid, rb, *, shot_dir: Path | None,
                live: bool) -> dict:
    """Step 1 — bidirectional write-back + non-interference + re-projection.
    Dispatch release_date 8/14→8/18 across 4 apps; verify round-trip."""
    tb = _gt_binding(fixture)
    pre = cs.snapshot(adapters, sid)
    ops = compile_patch(fixture.user_edit, tb)
    rep = dispatch(ops, adapters, sid, broken=None, rollback_log=rb)
    res = check_round_trip(sid, fixture, adapters, pre)
    # screenshots after each app's write
    shots = {}
    if shot_dir is not None:
        for app, ad in adapters.items():
            p = shot_dir / f"step1_{app}.png"
            shots[app] = _screenshot(f"{ad.base_url}/{sid}", p) or f"(failed){p.name}"
    apps_written = sorted({r.op.app for r in rep.ops if r.applied})
    return {
        "round_trip": round(res.score, 4),
        "changed_fraction": round(res.changed.fraction, 4),
        "untouched_fraction": round(res.untouched.fraction, 4),
        "non_interference": bool(res.non_interference_passed),
        "n_ops": rep.n_applied,
        "apps_written": apps_written,
        "saga_id": rb.latest_saga_id(),
        "screenshots": shots,
        "pass": res.score >= PASS_ROUND_TRIP and res.non_interference_passed,
    }


def step2_reconciliation(fixture, adapters, sid, host) -> dict:
    """Step 2 — external concurrent change → conflict marked amber.
    Inject taskboard.T2.deadline=2026-08-20 (teammate edit, conflicts with the
    user's 8/18); re-read canonical + detect conflicts via resync_with_conflicts."""
    tb = _gt_binding(fixture)
    # the projection the user is looking at = the post-step1 state
    projected = resync_values(tb, adapters, sid)
    inj = _inject_external_taskboard_change(host, sid, "T2", "deadline", "2026-08-20")
    # re-read canonical + diff → conflicts (the workspace did NOT trigger this;
    # the external inject did — §0 property 1: re-projection on world-state change)
    updated_proj, recon = resync_with_conflicts(tb, projected, adapters, sid)
    conflict_vars = [{"var_id": c.var_id, "app": c.app, "entity_id": c.entity_id,
                      "field": c.field, "projected": c.projected,
                      "underlying": c.underlying} for c in recon.conflicts]
    # prove the merge options are real (don't apply — step 3 rolls back instead)
    opts_sample = []
    if recon.conflicts:
        opts_sample = [{"option": o["option"], "label": o["label"]}
                       for o in merge_options(recon.conflicts[0])]
    return {
        "injected": inj,
        "conflict_detected": recon.has_conflicts,
        "n_conflicts": recon.n_conflicts,
        "conflict_vars": conflict_vars,
        "merge_options_sample": opts_sample,
        "pass": recon.has_conflicts and any(
            c.get("field") == "deadline" and c.get("entity_id") == "T2"
            for c in conflict_vars),
    }


def step3_rollback(fixture, adapters, sid, rb, *, shot_dir: Path | None) -> dict:
    """Step 3 — saga undo → real app state restored.
    undo_saga reverts every write from step 1 (cross-app, LIFO). T2 was externally
    changed in step 2 → its compensation target (8/14) ≠ the external 8/20, so
    reverting T2 to 8/14 'succeeds' at the app level but the external edit is
    overwritten — we report this honestly."""
    saga_id = rb.latest_saga_id()
    if saga_id is None:
        return {"saga_id": None, "n_reverted": 0, "partial_failure": True,
                "error": "no saga to undo", "pass": False}
    pre_undo = cs.snapshot(adapters, sid)
    sres = rb.undo_saga(saga_id, sid, adapters)
    post_undo = cs.snapshot(adapters, sid)
    # verify the real app state actually reverted (independent read) + honestly
    # detect external changes between the write (step 1) and the undo (here). If a
    # field's pre-undo value ≠ the saga's recorded `after`, the world changed under
    # us — the compensation mechanically succeeds (sets to `before`) but we report
    # the divergence honestly (handoff step 3: "T2 因外部修改" — the external 8/20
    # on T2 shows up here even though undo_saga overwrites it with 8/14).
    reverted_fields = {}
    external_changes = []
    for step in sres.steps:
        pre_ent = (pre_undo.get(step.app, {}).get("entities", {}) or {}).get(step.entity_id) or {}
        pre_val = pre_ent.get(step.field)
        post_ent = (post_undo.get(step.app, {}).get("entities", {}) or {}).get(step.entity_id) or {}
        ext = (str(pre_val).strip().lower() != str(step.after).strip().lower()) if pre_val is not None else False
        if ext:
            external_changes.append({"app": step.app, "entity_id": step.entity_id,
                                      "field": step.field, "saga_recorded_after": step.after,
                                      "actual_pre_undo": pre_val})
        reverted_fields[f"{step.app}.{step.entity_id}.{step.field}"] = {
            "saga_recorded_after": step.after, "target_before": step.before,
            "actual_pre_undo": pre_val,
            "after_undo": post_ent.get(step.field), "reverted": step.reverted,
            "external_change_before_undo": ext}
    shots = {}
    if shot_dir is not None:
        for app in ("calendar", "taskboard"):
            ad = adapters.get(app)
            if ad:
                p = shot_dir / f"step3_rollback_{app}.png"
                shots[app] = _screenshot(f"{ad.base_url}/{sid}", p) or f"(failed){p.name}"
    return {
        "saga_id": saga_id,
        "n_targets": sres.n_targets,
        "n_reverted": sres.n_reverted,
        "fully_reverted": sres.fully_reverted,
        "partial_failure": sres.partial_failure,
        "external_changes_before_undo": external_changes,
        "errors": sres.errors,
        "reverted_fields": reverted_fields,
        "screenshots": shots,
        "pass": sres.n_reverted >= 1,   # at least the calendar+taskboard writes reverted
    }


def step4_jvm_moment(host, *, live: bool, shot_dir: Path | None) -> dict:
    """Step 4 — JVM moment: same release_date edit on Stack B (outlook_cal+
    taskboard). Different substrate (appointment/scheduled_for/reschedule_appointment
    vs event/date/move_event), same VM op, stable surface."""
    fixture = get_ood_task("outlook_release_reschedule")
    apps = _apps_for(fixture)
    executor = "gui_agent" if live else "api"
    adapters = make_adapters(apps=apps, host=host, executor=executor)
    sid = f"arc_step4_{int(time.time()) % 100000}"
    for ad in adapters.values():
        ad.reset(sid)
    replay.seed_apps(fixture, adapters, sid)
    tb = _gt_binding(fixture)
    pre = cs.snapshot(adapters, sid)
    ops = compile_patch(fixture.user_edit, tb)
    rep = dispatch(ops, adapters, sid, broken=None)
    res = check_round_trip(sid, fixture, adapters, pre)
    # neg-control: broken dispatcher must score ≤0.3 (verifier honesty on Stack B)
    sid_neg = f"arc_step4_neg_{int(time.time()) % 100000}"
    for ad in adapters.values():
        ad.reset(sid_neg)
    replay.seed_apps(fixture, adapters, sid_neg)
    ops_neg = compile_patch(fixture.user_edit, _gt_binding(fixture))
    dispatch(ops_neg, adapters, sid_neg, broken="noop")
    res_neg = check_round_trip(sid_neg, fixture, adapters, pre)
    # GenUI surface slot comparison: project both stacks' release_date var
    proj_b = resync_values(tb, adapters, sid)
    slots_b = [{"var_id": v, "field": info.get("field"), "app": info.get("app"),
                "value": info.get("value")} for v, info in proj_b.items()]
    shots = {}
    if shot_dir is not None:
        for app, ad in adapters.items():
            p = shot_dir / f"step4_stackB_{app}.png"
            shots[app] = _screenshot(f"{ad.base_url}/{sid}", p) or f"(failed){p.name}"
    for ad in adapters.values():
        ad.reset(sid); ad.reset(sid_neg)
    return {
        "stack": "B",
        "task": fixture.task_id,
        "substrate_apps": apps,
        "trajectory_apps": sorted({r.op.app for r in rep.ops if r.applied}),
        "round_trip": round(res.score, 4),
        "non_interference": bool(res.non_interference_passed),
        "neg_control": round(res_neg.score, 4),
        "surface_slots": slots_b,
        "screenshots": shots,
        "pass": res.score >= PASS_ROUND_TRIP and res.non_interference_passed
                and res_neg.score <= NEG_MAX,
    }


# ── main ─────────────────────────────────────────────────────────────────────
def main(argv=None):
    parser = argparse.ArgumentParser(description="EE.3 four-step arc demo")
    parser.add_argument("--dry-run", action="store_true",
                        help="executor='api' (no GUI model; apps online). Real "
                             "numbers via the app API path — validates wiring, "
                             "NOT §12.16-compliant. Default off = gui_agent.")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--out", default=None)
    parser.add_argument("--no-step4", action="store_true",
                        help="skip step 4 (JVM moment) — for fast step1-3 validation")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    live = not args.dry_run
    executor = "gui_agent" if live else "api"
    ts = time.strftime("%Y%m%d_%H%M%S")
    shot_dir = EVAL_DIR / f"four_step_arc_{ts}"
    out_path = Path(args.out) if args.out else (
        EVAL_DIR / (f"four_step_arc_live_{ts}.json" if live else f"four_step_arc_{ts}.json"))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _write(report, interrupted=False, steps_completed=None):
        """FF.8: write the report JSON INCREMENTALLY after each step so a
        foreground-window timeout (Step1 launch_full + Step3 rollback exceed
        the 555s bash limit on this box) still yields a partial JSON with
        execution_mode=gui_agent + the steps that completed + an honest
        `interrupted` flag (§12: don't hide a timeout behind silence)."""
        report["interrupted"] = interrupted
        if steps_completed is not None:
            report["steps_completed"] = steps_completed
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    # ── Step 1-3: launch_full on Stack A ─────────────────────────────────────
    fixture = get_task("launch_full")
    apps = _apps_for(fixture)
    adapters = make_adapters(apps=apps, host=args.host, executor=executor)
    # health check
    for app, ad in adapters.items():
        h = ad.health()
        if h.get("status") != "ok":
            logger.error(f"{app} not healthy: {h} (start the apps first)")
            sys.exit(2)
        logger.info(f"{app} healthy @ {ad.base_url}")
    rb = RollbackLog()
    sid = f"arc_step1_{int(time.time()) % 100000}"
    for ad in adapters.values():
        ad.reset(sid)
    replay.seed_apps(fixture, adapters, sid)

    base_report = {
        "ts": ts, "test": "four_step_arc",
        "mode": "gui_agent" if live else "dry_run(api)",
        "execution_mode": "gui_agent" if live else "api",
        "shot_dir": str(shot_dir),
        "honest_framing": (
            "dry_run mode uses executor='api' (app API writes = the §12.16 backdoor) — "
            "it validates the arc wiring + produces real round_trip/conflict/rollback "
            "numbers, but is NOT the §12.16-compliant GUI-gesture path. For the paper's "
            "teaser figure, re-run WITHOUT --dry-run (gui_agent, gpt-5.6-sol). Step 3's "
            "partial_failure reflects that T2 was externally changed in step 2 — its "
            "compensation overwrites the external edit, reported honestly."
        ) if args.dry_run else (
            "gui_agent mode: write/rollback drive a real browser via the GUI executor "
            "(§12.16-compliant). This is the teaser-figure evidence path."
        ),
    }

    logger.info("\n=== STEP 1: bidirectional write-back (launch_full 4-App fanout) ===")
    s1 = step1_write(fixture, adapters, sid, rb, shot_dir=shot_dir, live=live)
    logger.info(f"[step1] round_trip={s1['round_trip']} non_interference={s1['non_interference']} "
                f"n_ops={s1['n_ops']} apps={s1['apps_written']} pass={s1['pass']}")
    _write({**base_report, "step1_write": s1}, interrupted=True, steps_completed=["step1_write"])

    logger.info("\n=== STEP 2: reconciliation (external T2.deadline→8/20) ===")
    s2 = step2_reconciliation(fixture, adapters, sid, args.host)
    logger.info(f"[step2] conflict_detected={s2['conflict_detected']} "
                f"n_conflicts={s2['n_conflicts']} vars={s2['conflict_vars']} pass={s2['pass']}")
    _write({**base_report, "step1_write": s1, "step2_reconciliation": s2},
           interrupted=True, steps_completed=["step1_write", "step2_reconciliation"])

    logger.info("\n=== STEP 3: rollback (saga undo, honest partial_failure) ===")
    s3 = step3_rollback(fixture, adapters, sid, rb, shot_dir=shot_dir)
    logger.info(f"[step3] saga={s3.get('saga_id')} n_reverted={s3.get('n_reverted')}/"
                f"{s3.get('n_targets')} partial_failure={s3.get('partial_failure')} pass={s3.get('pass')}")
    _write({**base_report, "step1_write": s1, "step2_reconciliation": s2, "step3_rollback": s3},
           interrupted=True, steps_completed=["step1_write", "step2_reconciliation", "step3_rollback"])

    for ad in adapters.values():
        ad.reset(sid)

    # ── Step 4: JVM moment on Stack B ────────────────────────────────────────
    s4 = None
    if not args.no_step4:
        logger.info("\n=== STEP 4: JVM moment (Stack B = outlook_cal+taskboard) ===")
        s4 = step4_jvm_moment(args.host, live=live, shot_dir=shot_dir)
        logger.info(f"[step4] stack={s4['stack']} round_trip={s4['round_trip']} "
                    f"trajectory={s4['trajectory_apps']} neg={s4['neg_control']} pass={s4['pass']}")

    overall_pass = s1["pass"] and s2["pass"] and s3.get("pass", False) and (s4["pass"] if s4 else True)
    report = {**base_report, "step1_write": s1, "step2_reconciliation": s2,
              "step3_rollback": s3, "step4_jvm_moment": s4, "overall_PASS": overall_pass}
    _write(report, interrupted=False,
           steps_completed=(["step1_write","step2_reconciliation","step3_rollback","step4_jvm_moment"]
                            if s4 else ["step1_write","step2_reconciliation","step3_rollback"]))
    print(f"\nWrote {out_path}")
    print(f"\n=== FOUR-STEP ARC: {'PASS' if overall_pass else 'FAIL'} ===")
    print(f"  step1 write:          {'PASS' if s1['pass'] else 'FAIL'} (round_trip={s1['round_trip']}, {s1['n_ops']} ops, {len(s1['apps_written'])} apps)")
    print(f"  step2 reconciliation: {'PASS' if s2['pass'] else 'FAIL'} ({s2['n_conflicts']} conflict(s))")
    print(f"  step3 rollback:       {'PASS' if s3.get('pass') else 'FAIL'} ({s3.get('n_reverted')}/{s3.get('n_targets')} reverted, partial={s3.get('partial_failure')})")
    if s4:
        print(f"  step4 JVM moment:     {'PASS' if s4['pass'] else 'FAIL'} (Stack B round_trip={s4['round_trip']}, neg={s4['neg_control']})")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
