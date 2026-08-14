"""EE.5 — reconciliation killtest (3 merge strategies, Reconciliation Accuracy).

Quantifies the Reconciliation Accuracy metric (大纲 §6) that SaC names as future
work ("frontend state synchronisation", §3.5) and TaskVM claims to close. Three
merge strategies, each exercised on a real concurrent-external-change conflict:

  Scenario A — accept_underlying: user edits release_date 8/14→8/18 (T1/T2→8/18,
               E1→8/18); external injects T2.deadline=8/20; user ACCEPTS the
               underlying → T2=8/20, T1=8/18 (kept), E1=8/18.
  Scenario B — keep_projected:    same setup; user KEEPS their projected 8/18 →
               T2=8/18 (re-dispatched Y overwrites the external 8/20), T1=8/18, E1=8/18.
  Scenario C — merge:             same setup; user MERGES with resolved_value
               2026-08-19 → T2=2026-08-19 (re-dispatched Z), T1=8/18, E1=8/18.

Each scenario: seed → dispatch user edit → inject external change → detect
conflict → apply merge option → verify post-merge state matches the strategy's
expected outcome. RECONCILIATION_PASS iff all 3 scenarios' post-merge state is
correct + non-interference holds (E2/T3 untouched throughout).

External inject uses the taskboard app's OWN mutate API directly (bypasses
TaskVM's adapter/dispatcher — a genuine external change, EE.3 pattern). Merge
execution uses ``verifier/reconciliation.apply_merge_option`` (already built).

Usage:
    python -m taskvm.evaluation.run_reconciliation_killtest --dry-run
    python -m taskvm.evaluation.run_reconciliation_killtest --samples 2
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import requests

from taskvm.benchmark.fixtures import get_task
from taskvm.execution.action_dispatcher import dispatch
from taskvm.execution.patch_compiler import compile_patch
from taskvm.harness import replay_engine as replay
from taskvm.execution.gui_driver import make_task_adapters
from taskvm.substrate.builtin_web.evaluation import (
    make_evaluation_environments, make_evaluation_environment,
)
from taskvm.verifier import canonical_state as cs
from taskvm.verifier.reconciliation import apply_merge_option
from taskvm.workspace_ui.live_sync import resync_values, resync_with_conflicts

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")

# the 3 merge strategies (option names from verifier/reconciliation.merge_options)
SCENARIOS = [
    {"name": "A_accept_underlying", "option": "accept_underlying",
     "resolved_value": None,
     "expected": {"taskboard": {"T2": {"deadline": "2026-08-20"}}},
     "desc": "user accepts the external 8/20; T1+E1 stay at user's 8/18"},
    {"name": "B_keep_projected", "option": "keep_projected",
     "resolved_value": None,
     "expected": {"taskboard": {"T2": {"deadline": "2026-08-18"}}},
     "desc": "user keeps their 8/18; re-dispatch overwrites external 8/20"},
    {"name": "C_merge", "option": "merge",
     "resolved_value": "2026-08-19",
     "expected": {"taskboard": {"T2": {"deadline": "2026-08-19"}}},
     "desc": "user merges to 8/19; re-dispatch Z"},
]


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


def _inject_external(host: str, sid: str, tid: str, value: str) -> dict:
    """External concurrent edit AS IF another actor changed taskboard behind
    TaskVM's back. Agent B: injected through the EVALUATION plane's
    ``force_write`` (exam-room power) — the runtime has no API write path."""
    env = make_evaluation_environment("taskboard", host=host)
    try:
        env.force_write(sid, tid, "set_deadline", value)
        return {"injected": True, "tid": tid, "value": value}
    except Exception as e:
        return {"injected": False, "error": str(e)[:200]}


def _read_field(envs, sid, app, eid, field):
    ent = (cs.snapshot(envs, sid).get(app, {}).get("entities", {}) or {}).get(eid) or {}
    return ent.get(field)


def run_scenario(scenario: dict, host: str, *, sample_i: int) -> dict:
    """Run one merge-strategy scenario. Returns the per-scenario record.

    Agent B: GUI-only write drivers (``adapters``) + evaluation environments
    (``envs``) — there is no ``executor`` knob anymore."""
    fixture = get_task("release_reschedule")   # T1/T2 deadline + E1 date, 2 apps
    adapters = make_task_adapters(apps=["calendar", "taskboard"], host=host)
    envs = make_evaluation_environments(["calendar", "taskboard"], host=host)
    sid = f"recon_{scenario['name']}_s{sample_i}_{int(time.time()) % 100000}"
    for env in envs.values():
        env.reset(sid)
    replay.seed_apps(fixture, envs, sid)
    tb = _gt_binding(fixture)

    # 1. user edits release_date 8/14→8/18 (T1/T2→8/18, E1→8/18)
    ops = compile_patch(fixture.user_edit, tb)
    dispatch(ops, adapters, sid, broken=None)
    projected = resync_values(tb, envs, sid)   # Y = what user sees (8/18)

    # 2. external injects T2.deadline=8/20
    inj = _inject_external(host, sid, "T2", "2026-08-20")

    # 3. detect conflict (re-read canonical, diff vs projected)
    updated_proj, recon = resync_with_conflicts(tb, projected, envs, sid)
    t2_conflict = next((c for c in recon.conflicts
                        if c.entity_id == "T2" and c.field == "deadline"), None)

    # 4. apply the user's merge choice
    merge_result = {"applied": False}
    if t2_conflict is not None:
        merge_result = apply_merge_option(t2_conflict, scenario["option"],
                                          scenario["resolved_value"],
                                          adapters, sid, tb)
        merge_result["applied"] = True

    # 5. verify post-merge state matches the strategy's expected outcome
    post = cs.snapshot(adapters, sid)
    checks = {}
    for app, ents in scenario["expected"].items():
        for eid, fields in ents.items():
            for f, exp in fields.items():
                actual = ((post.get(app, {}).get("entities", {}) or {}).get(eid) or {}).get(f)
                checks[f"{app}.{eid}.{f}"] = {"expected": exp, "actual": actual,
                                              "ok": str(actual).strip().lower() == str(exp).strip().lower()}
    # invariants across all scenarios: T1=8/18 (user's, unaffected by T2 merge),
    # E1=8/18 (user's), E2/T3 untouched (non-interference)
    t1 = _read_field(envs, sid, "taskboard", "T1", "deadline")
    e1 = _read_field(envs, sid, "calendar", "E1", "date")
    e2 = _read_field(envs, sid, "calendar", "E2", "date")
    t3_status = _read_field(envs, sid, "taskboard", "T3", "status")
    invariants = {
        "T1.deadline_is_8/18": str(t1) == "2026-08-18",
        "E1.date_is_8/18": str(e1) == "2026-08-18",
        "E2.untouched_8/12": str(e2) == "2026-08-12",
        "T3.untouched_done": str(t3_status).lower() == "done",
    }
    for env in envs.values():
        env.reset(sid)
    all_ok = all(c["ok"] for c in checks.values()) and all(invariants.values()) \
        and t2_conflict is not None and merge_result.get("applied")
    return {
        "scenario": scenario["name"], "option": scenario["option"],
        "resolved_value": scenario["resolved_value"],
        "sample": sample_i, "sid": sid,
        "injected": inj,
        "conflict_detected": t2_conflict is not None,
        "conflict": (t2_conflict.to_dict() if t2_conflict else None),
        "merge_result": {k: v for k, v in merge_result.items()
                         if k != "response"},   # response has app-internal detail
        "post_merge_checks": checks,
        "invariants": invariants,
        "pass": all_ok,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="EE.5 reconciliation killtest")
    parser.add_argument("--samples", type=int, default=2, help="samples per scenario")
    parser.add_argument("--host", default="localhost")
    # Agent B: the legacy --dry-run / --execution-mode 'api' backdoor is
    # DELETED — writes go through real GUI gestures (make_task_adapters);
    # oracle reads + external-change injection go through the evaluation
    # environments (force_write is the exam-room power).
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    ts = time.strftime("%Y%m%d_%H%M%S")
    records = []
    for sc in SCENARIOS:
        for i in range(args.samples):
            logger.info(f"\n=== Scenario {sc['name']} sample {i+1}/{args.samples} ===")
            r = run_scenario(sc, args.host, sample_i=i)
            logger.info(f"[{sc['name']}] conflict={r['conflict_detected']} "
                        f"merge_applied={r['merge_result'].get('applied')} "
                        f"checks={ {k:v['ok'] for k,v in r['post_merge_checks'].items()} } "
                        f"pass={r['pass']}")
            records.append(r)

    n_pass = sum(1 for r in records if r["pass"])
    recon_pass = n_pass == len(records)
    by_scenario = {}
    for r in records:
        by_scenario.setdefault(r["scenario"], []).append(r["pass"])
    report = {
        "ts": ts, "test": "reconciliation_killtest",
        "mode": "gui_only",
        "n_samples_per_scenario": args.samples,
        "n_scenarios": len(SCENARIOS),
        "scenarios": [{"name": s["name"], "option": s["option"],
                       "resolved_value": s["resolved_value"],
                       "expected": s["expected"], "desc": s["desc"]}
                      for s in SCENARIOS],
        "results": records,
        "n_pass": n_pass, "n_total": len(records),
        "by_scenario_pass": {k: all(v) for k, v in by_scenario.items()},
        "RECONCILIATION_PASS": recon_pass,
        "honest_framing": (
            "Tests the 3 merge strategies (accept_underlying / keep_projected / "
            "merge) on a real external-concurrent-change conflict (T2.deadline "
            "injected via the evaluation plane's force_write). Reconciliation "
            "Accuracy = post-merge state matches the strategy's expected outcome "
            "AND non-interference holds (E2/T3 untouched). Writes (user edit + "
            "merge re-dispatch) drive real GUI gestures; the merge logic itself "
            "is rule-based (apply_merge_option)."
        ),
    }
    out_path = Path(args.out) if args.out else (
        EVAL_DIR / f"reconciliation_killtest_{ts}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_path}")
    print(f"\n=== RECONCILIATION: {'PASS' if recon_pass else 'FAIL'} ===")
    print(f"  {n_pass}/{len(records)} scenario-samples passed")
    for sc, passes in by_scenario.items():
        print(f"  {sc}: {sum(passes)}/{len(passes)}")
    return 0 if recon_pass else 1


if __name__ == "__main__":
    sys.exit(main())
