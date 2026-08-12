"""EE.9 — interaction compression killtest (大纲 §6 metric, RQ3 evidence).

Quantifies the Interaction Compression metric: TaskVM lets a user change ONE VM
variable (release_date 8/14→8/18) and the agent fans it out to N apps; in the
baseline (native-app) path the user must manually edit each app. Compression
Ratio = baseline_actions / taskvm_actions (≥4x for a 4-App task — handoff EE.9).

TaskVM path: 1 user action (one edit_field UserBehaviorEvent) → compile_patch
fans out to N PatchOps → dispatch writes all N apps. The user touches ONE field.

Baseline path (principled lower bound, documented): for each binding the user
must (1) navigate to the app, (2) locate the entity, (3) edit the field, (4)
submit. Same-app bindings share the navigate step, so:
  baseline_actions = n_apps (navigates) + 3 × n_bindings (locate + edit + submit)
This is a LOWER bound — real users take more (scrolling, re-confirmation, app
switching overhead). The handoff's "8 actions for 4-App" is an even more
conservative 2-actions/app floor; this model is more realistic + still ≥4x.

NOTE on frontier_shadow.py: the handoff suggested using it as the baseline
agent, but it's actually the main compiler wrapped as a baseline (A/B harness),
NOT a manual-action simulator. So the baseline is a principled action-count
model, not a shadow-agent run. This is documented in the report's honest_framing.

Usage:
    python -m taskvm.evaluation.run_interaction_compression
    python -m taskvm.evaluation.run_interaction_compression --task release_reschedule
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from taskvm.benchmark.fixtures import get_task
from taskvm.execution.action_dispatcher import dispatch
from taskvm.execution.patch_compiler import compile_patch
from taskvm.harness import replay_engine as replay
from taskvm.harness.state_adapter import make_adapters

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
# baseline action model: per binding = locate + edit + submit (3); per app = 1 navigate
LOCATE_EDIT_SUBMIT_PER_BINDING = 3
NAVIGATE_PER_APP = 1


def _gt_binding(fixture):
    var_groups: dict[str, dict] = {}
    for b in fixture.bindings:
        g = var_groups.setdefault(b.var_id, {
            "var_id": b.var_id, "label": b.var_id,
            "value": fixture.user_edit.get("old"), "editable": True, "bindings": []})
        g["bindings"].append({"var_id": b.var_id, "app": b.app,
                              "entity_id": b.entity_id, "field": b.field,
                              "operator": b.operator})
    from taskvm.task_state.entity_binding import TaskBinding
    return TaskBinding(task_id=fixture.task_id, variables=list(var_groups.values()))


def _baseline_actions(fixture) -> dict:
    """Principled lower bound on the manual (native-app) user action count."""
    n_bindings = len(fixture.bindings)
    n_apps = len({b.app for b in fixture.bindings})
    navigates = n_apps * NAVIGATE_PER_APP
    per_binding = n_bindings * LOCATE_EDIT_SUBMIT_PER_BINDING
    total = navigates + per_binding
    return {"n_apps": n_apps, "n_bindings": n_bindings,
            "navigate_actions": navigates,
            "per_binding_actions": per_binding,
            "total": total,
            "model": f"{n_apps} navigates + {n_bindings} bindings × "
                     f"{LOCATE_EDIT_SUBMIT_PER_BINDING} (locate+edit+submit) = {total} "
                     f"(lower bound — real users take more)"}


def run_taskvm_path(fixture, host: str, *, executor: str = "api") -> dict:
    """Run the TaskVM path: 1 user edit → dispatch → verify fanout. Returns the
    action count (always 1 user action) + the fanout proof (n_ops dispatched)."""
    apps = sorted(set(fixture.seed_state.keys()) |
                  {b.app for b in fixture.bindings})
    adapters = make_adapters(apps=apps, host=host, executor=executor)
    for ad in adapters.values():
        ad.health()
    sid = f"compress_{fixture.task_id}_{int(time.time()) % 100000}"
    for ad in adapters.values():
        ad.reset(sid)
    replay.seed_apps(fixture, adapters, sid)
    tb = _gt_binding(fixture)
    # THE user action: one edit_field event (release_date 8/14→8/18).
    # compile_patch fans this 1 edit into N PatchOps (the VM fanout).
    ops = compile_patch(fixture.user_edit, tb)
    rep = dispatch(ops, adapters, sid, broken=None)
    for ad in adapters.values():
        ad.reset(sid)
    return {"taskvm_user_actions": 1,   # one edit_field event
            "fanout_ops": rep.n_applied,
            "apps_written": sorted({r.op.app for r in rep.ops if r.applied}),
            "operators": sorted({r.op.operator for r in rep.ops if r.applied})}


def main(argv=None):
    parser = argparse.ArgumentParser(description="EE.9 interaction compression killtest")
    parser.add_argument("--task", default="launch_full",
                        help="task to measure (default launch_full = 4-App fanout)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--execution-mode", choices=["api", "gui_agent"],
                        default="api", help="api (default; structure) or gui_agent")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    fixture = get_task(args.task)
    baseline = _baseline_actions(fixture)
    taskvm = run_taskvm_path(fixture, args.host, executor=args.execution_mode)
    compression = round(baseline["total"] / taskvm["taskvm_user_actions"], 2)

    # the handoff's ≥4x threshold applies to a 4-App task
    ge_4x = compression >= 4.0
    report = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "test": "interaction_compression", "task": args.task,
        "execution_mode": args.execution_mode,
        "baseline_actions": baseline,
        "taskvm_actions": taskvm,
        "compression_ratio": compression,
        "threshold_ge_4x": ge_4x,
        "PASS": ge_4x and taskvm["fanout_ops"] >= 2,   # ≥4x AND real fanout (≥2 ops)
        "honest_framing": (
            f"Interaction Compression = baseline_actions / taskvm_actions = "
            f"{baseline['total']} / {taskvm['taskvm_user_actions']} = {compression}x. "
            f"The TaskVM user performs ONE edit (release_date 8/14→8/18); the agent "
            f"fans it out to {taskvm['fanout_ops']} ops across {len(taskvm['apps_written'])} "
            f"apps. The baseline is a principled LOWER bound (navigate + locate + edit "
            f"+ submit per binding; same-app bindings share navigation) — real users "
            f"take more (scrolling, app-switch overhead, re-confirmation). "
            f"frontier_shadow.py was NOT used as a baseline agent: it is the main "
            f"compiler wrapped as an A/B baseline, not a manual-action simulator. "
            f"The action-count model is documented in baseline_actions.model."
        ),
    }
    out_path = Path(args.out) if args.out else (
        EVAL_DIR / f"interaction_compression_{report['ts']}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_path}")
    print(f"\n=== INTERACTION COMPRESSION ({args.task}) ===")
    print(f"  TaskVM:   {taskvm['taskvm_user_actions']} user action → {taskvm['fanout_ops']} ops fanned out ({len(taskvm['apps_written'])} apps)")
    print(f"  Baseline: {baseline['total']} actions ({baseline['model']})")
    print(f"  Compression: {compression}x  (≥4x: {'PASS' if ge_4x else 'FAIL'})")
    return 0 if report["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
