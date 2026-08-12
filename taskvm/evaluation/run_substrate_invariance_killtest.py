"""EE.4 — substrate-invariance killtest (JVM moment, VM property 3).

Quantifies that the SAME VM operation (release_date 8/14→8/18) is substrate-
independent: across Stack A (calendar+taskboard) and Stack B (outlook_cal+
taskboard), the binding-discovery F1 + round-trip fidelity are statistically
equivalent, the GenUI surface slots are semantically the same, and the
underlying trajectory (apps_written / operator) DIFFERS (proving it's not the
same app under a rename — it's a genuine substrate swap). This is the only
quantitative source for the paper's Table 1 substrate-invariance row (handoff
EE.4 + 大纲 §4.2 property 3).

Reuses ``run_w1_killtest.run_one_sample`` per stack so the per-sample execution
(compile → validate → dispatch → verify → binding_accuracy) is identical to the
W1 baseline — the ONLY things that vary across stacks are the fixture + adapter
set (the substrate). That's the controlled variable.

Metrics (per stack, N samples):
  - binding_f1_mean / round_trip_mean  (compiler + executor substrate-independence)
  - apps_written / operators           (trajectory_diff — must DIFFER across stacks)
  - genui_surface_semantic_sim         (optional LLM-judge; --genui, needs model)

PASS: |binding_f1_A - binding_f1_B| < 0.2 AND |round_trip_A - round_trip_B| < 0.15
      AND trajectory differs (apps_written_A ≠ apps_written_B).

Usage:
    python -m taskvm.evaluation.run_substrate_invariance_killtest --samples 1 --mock
    python -m taskvm.evaluation.run_substrate_invariance_killtest --samples 3
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from taskvm.benchmark.cost_model import CostModel
from taskvm.benchmark.fixtures import get_task
from taskvm.benchmark.ood_fixtures import get_ood_task
from taskvm.evaluation.run_w1_killtest import run_one_sample
from taskvm.harness.state_adapter import make_adapters

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
BINDING_F1_MAX_DIFF = 0.2
ROUND_TRIP_MAX_DIFF = 0.15

# The canonical Stack A / Stack B pair (handoff EE.4). Both edit release_date
# 8/14→8/18 + sync dependent taskboard deadlines; Stack A's meeting is a
# calendar event (move_event), Stack B's is an outlook_cal appointment
# (reschedule_appointment) — same semantics, renamed substrate.
DEFAULT_PAIR = ("release_reschedule", "outlook_release_reschedule")


def _apps_for(fixture) -> list[str]:
    apps = set(fixture.seed_state.keys())
    apps.update(b.app for b in fixture.bindings)
    order = ("calendar", "taskboard", "drive", "mail", "outlook_cal")
    return [a for a in order if a in apps]


def run_stack(task_id: str, *, samples: int, model: str | None, mock: bool,
              host: str, executor: str, cost_model: CostModel) -> dict:
    """Run N samples of one task on its substrate; return aggregate metrics."""
    fixture = (get_ood_task(task_id) if task_id.startswith(("outlook_", "send_",
              "set_mail")) else get_task(task_id))
    apps = _apps_for(fixture)
    adapters = make_adapters(apps=apps, host=host, executor=executor)
    for app, ad in adapters.items():
        h = ad.health()
        if h.get("status") != "ok":
            logger.error(f"{app} not healthy: {h}")
            sys.exit(2)
    sample_records = []
    for i in range(samples):
        s = run_one_sample(fixture, adapters, model=model, temperature=None,
                           sample_i=i, mock=mock, cost_model=cost_model)
        sample_records.append(s)
        logger.info(f"[{task_id}] sample {i+1}: score={s['round_trip']['score']} "
                    f"binding_f1={s['binding_accuracy']['f1']} "
                    f"f1_sem={s['binding_accuracy'].get('f1_varid_semantic')}")
    f1s = [s["binding_accuracy"]["f1"] for s in sample_records]
    f1_sems = [s["binding_accuracy"].get("f1_varid_semantic", 0.0) for s in sample_records]
    scores = [s["round_trip"]["score"] for s in sample_records]
    # trajectory: union of apps_written + operators across samples (deduped)
    _ops = [op for s in sample_records
            for op in ((s.get("dispatch") or {}).get("ops") or []) if op.get("applied")]
    apps_written = sorted({op["app"] for op in _ops})
    operators = sorted({op["operator"] for op in _ops})
    return {
        "task_id": task_id,
        "substrate_apps": apps,
        "n_samples": samples,
        "binding_f1_mean": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
        "binding_f1_semantic_mean": round(sum(f1_sems) / len(f1_sems), 4) if f1_sems else 0.0,
        "binding_f1_samples": f1s,
        "round_trip_mean": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "round_trip_samples": scores,
        "apps_written": apps_written,
        "operators": operators,
        "mock": mock,
    }


def genui_semantic_sim(stack_a_result: dict, stack_b_result: dict, *,
                       model: str | None, cost_model: CostModel) -> dict:
    """Optional LLM-judge: do the two stacks' GenUI surfaces expose the same
    editable slots (same var_id semantics)? Skipped unless --genui. Compares the
    surface_slots (var_id + field) — substrate-independent means the VM variable
    is the same even if the underlying field name differs (date vs scheduled_for)."""
    slots_a = [(s["var_id"]) for s in stack_a_result.get("surface_slots", [])]
    slots_b = [(s["var_id"]) for s in stack_b_result.get("surface_slots", [])]
    # structural match: same var_id set (the VM variable is substrate-independent)
    set_match = set(slots_a) == set(slots_b) and bool(slots_a)
    return {"slot_var_ids_A": slots_a, "slot_var_ids_B": slots_b,
            "structural_match": set_match, "semantic_sim": 1.0 if set_match else 0.0,
            "llm_judge": False, "note": "structural var_id match (LLM-judge deferred)"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="EE.4 substrate-invariance killtest")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--task-pair", default=",".join(DEFAULT_PAIR),
                        help="comma-separated Stack A,Stack B task ids")
    parser.add_argument("--model", default=None)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--mock", action="store_true",
                        help="GT-shaped binding (no API model) — structure validation")
    parser.add_argument("--execution-mode", choices=["api", "gui_agent"],
                        default="api", help="api (default, structure) or gui_agent (§12.16)")
    parser.add_argument("--genui", action="store_true",
                        help="compute GenUI surface semantic sim (optional)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    pair = args.task_pair.split(",")
    if len(pair) != 2:
        logger.error("--task-pair must be 'StackA,StackB'")
        sys.exit(2)
    cost_model = CostModel()
    if args.execution_mode == "gui_agent":
        from taskvm.execution.gui_executor import get_executor
        get_executor().cost_model = cost_model

    ts = time.strftime("%Y%m%d_%H%M%S")
    logger.info(f"\n=== Stack A: {pair[0]} ===")
    a = run_stack(pair[0], samples=args.samples, model=args.model, mock=args.mock,
                  host=args.host, executor=args.execution_mode, cost_model=cost_model)
    logger.info(f"[Stack A] binding_f1={a['binding_f1_mean']} round_trip={a['round_trip_mean']} "
                f"apps={a['apps_written']} ops={a['operators']}")

    logger.info(f"\n=== Stack B: {pair[1]} ===")
    b = run_stack(pair[1], samples=args.samples, model=args.model, mock=args.mock,
                  host=args.host, executor=args.execution_mode, cost_model=cost_model)
    logger.info(f"[Stack B] binding_f1={b['binding_f1_mean']} round_trip={b['round_trip_mean']} "
                f"apps={b['apps_written']} ops={b['operators']}")

    # surface slots (for the optional GenUI sim) — built from the binding directly
    # (var_id + field + app). No read_canonical needed: the structural match is
    # "do both stacks expose the same VM variable?" — the field/app differ by
    # design (date/calendar vs scheduled_for/outlook_cal) but the var_id (release_date)
    # is the substrate-independent VM variable.
    fa = get_task(pair[0]) if pair[0] in ("release_reschedule", "design_review_delay",
              "doc_handoff", "launch_full") else get_ood_task(pair[0])
    fb = get_ood_task(pair[1])
    a["surface_slots"] = [{"var_id": b.var_id, "field": b.field, "app": b.app}
                          for b in fa.bindings]
    b["surface_slots"] = [{"var_id": b.var_id, "field": b.field, "app": b.app}
                          for b in fb.bindings]

    genui = genui_semantic_sim(a, b, model=args.model, cost_model=cost_model) if args.genui else None

    binding_diff = abs(a["binding_f1_mean"] - b["binding_f1_mean"])
    round_trip_diff = abs(a["round_trip_mean"] - b["round_trip_mean"])
    traj_diff = set(a["apps_written"]) != set(b["apps_written"]) or \
                set(a["operators"]) != set(b["operators"])
    substrate_pass = (binding_diff < BINDING_F1_MAX_DIFF
                      and round_trip_diff < ROUND_TRIP_MAX_DIFF
                      and traj_diff)
    report = {
        "ts": ts, "test": "substrate_invariance_killtest",
        "model": args.model or "gpt-5.6-sol", "mock": args.mock,
        "execution_mode": args.execution_mode,
        "task_pair": {"stack_a": pair[0], "stack_b": pair[1]},
        "stack_a": a, "stack_b": b,
        "genui_surface_semantic_sim": genui,
        "binding_f1_diff": round(binding_diff, 4),
        "round_trip_diff": round(round_trip_diff, 4),
        "trajectory_differs": traj_diff,
        "thresholds": {"binding_f1_max_diff": BINDING_F1_MAX_DIFF,
                       "round_trip_max_diff": ROUND_TRIP_MAX_DIFF},
        "SUBSTRATE_INVARIANCE_PASS": substrate_pass,
        "honest_framing": (
            "mock mode: binding_f1=1.0 + round_trip=1.0 on both stacks (GT binding, "
            "api path) → diff 0 → PASS. This validates the killtest STRUCTURE + the "
            "trajectory_differs check (calendar vs outlook_cal apps_written). It does "
            "NOT validate model substrate-independence (that needs --mock off + real "
            "compiler). For the paper's Table 1 row, run WITHOUT --mock, --samples 3."
        ) if args.mock else (
            "real compiler mode: binding_f1 + round_trip reflect actual model "
            "performance on each substrate. SUBSTRATE_INVARIANCE_PASS iff the same "
            "VM op achieves statistically equivalent binding+round-trip across stacks "
            "with a differing trajectory — the JVM moment."
        ),
    }
    out_path = Path(args.out) if args.out else (
        EVAL_DIR / f"substrate_invariance_killtest_{ts}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_path}")
    print(f"\n=== SUBSTRATE INVARIANCE: {'PASS' if substrate_pass else 'FAIL'} ===")
    print(f"  Stack A ({pair[0]}): binding_f1={a['binding_f1_mean']} round_trip={a['round_trip_mean']} apps={a['apps_written']}")
    print(f"  Stack B ({pair[1]}): binding_f1={b['binding_f1_mean']} round_trip={b['round_trip_mean']} apps={b['apps_written']}")
    print(f"  binding_f1_diff={report['binding_f1_diff']} (<{BINDING_F1_MAX_DIFF})  round_trip_diff={report['round_trip_diff']} (<{ROUND_TRIP_MAX_DIFF})  trajectory_differs={traj_diff}")
    return 0 if substrate_pass else 1


if __name__ == "__main__":
    sys.exit(main())
