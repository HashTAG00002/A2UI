"""EE.6 — model ablation killtest (GroundingBackend hot-swap defense).

Runs the same task (release_reschedule) with different grounding backends to
produce the model-ablation table that defends against the "results only hold on
gpt-5.6-sol" reviewer attack (handoff EE.6). For each backend (gpt56sol, glm5v):
  - N samples of release_reschedule via the GUI executor
  - report binding_f1, round_trip, avg_steps (GUI calls), success_rate
  - output eval_results/model_ablation_<ts>.json (the paper's ablation table source)

UITarsBackend is interface-only (stub raises NotImplementedError) — listed in the
report as "pluggable, not run" (no weights downloaded), per handoff.

Usage:
    python -m taskvm.evaluation.run_model_ablation --samples 2
    python -m taskvm.evaluation.run_model_ablation --backends gpt56sol,glm5v --samples 2
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from taskvm.benchmark.cost_model import CostModel
from taskvm.benchmark.fixtures import get_task
from taskvm.evaluation.run_w1_killtest import run_one_sample
from taskvm.execution.gui_driver import make_task_adapters
from taskvm.execution.grounding_backend import make_grounding_backend
from taskvm.substrate.builtin_web.evaluation import (
    make_evaluation_environments,
)

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
PASS_SCORE = 0.85


def run_backend(task_id: str, backend_name: str, *, samples: int, host: str,
                cost_model: CostModel) -> dict:
    """Run N samples of one task with one grounding backend."""
    fixture = get_task(task_id)
    apps = sorted(set(fixture.seed_state.keys()) |
                  {b.app for b in fixture.bindings})
    gui_shot_dir = str(EVAL_DIR / f"model_ablation_{backend_name}_"
                                  f"{time.strftime('%Y%m%d_%H%M%S')}")
    adapters = make_task_adapters(apps=apps, host=host,
                                  screenshot_dir=gui_shot_dir,
                                  grounding_backend=backend_name)
    envs = make_evaluation_environments(apps, host=host)
    for app, env in envs.items():
        h = env.health()
        if h.get("status") != "ok":
            logger.error(f"{app} not healthy: {h}")
            sys.exit(2)
    # wire the backend's cost_model + ensure the executor singleton is built
    from taskvm.execution.gui_executor import get_executor
    backend = make_grounding_backend(backend_name, cost_model=cost_model)
    get_executor(backend=backend, cost_model=cost_model,
                 screenshot_dir=gui_shot_dir)
    sample_records = []
    for i in range(samples):
        s = run_one_sample(fixture, adapters, envs, model=None,
                           temperature=None, sample_i=i, mock=False,
                           cost_model=cost_model)
        sample_records.append(s)
        logger.info(f"[{backend_name}] sample {i+1}: score={s['round_trip']['score']} "
                    f"binding_f1={s['binding_accuracy']['f1']} "
                    f"steps={((s.get('dispatch') or {}).get('ops') or [{}]) and 'see trace'}")
        if i < samples - 1:
            logger.info(f"[{backend_name}] QPM refill: sleeping 60s")
            time.sleep(60)
    scores = [s["round_trip"]["score"] for s in sample_records]
    f1s = [s["binding_accuracy"]["f1"] for s in sample_records]
    # avg GUI steps: sum of trace.steps across samples that dispatched
    n_steps = []
    for s in sample_records:
        for op in ((s.get("dispatch") or {}).get("ops") or []):
            t = (op.get("response") or {}).get("trace") or {}
            if t.get("steps"):
                n_steps.append(t["steps"])
    return {
        "backend": backend_name,
        "task_id": task_id,
        "n_samples": samples,
        "binding_f1_mean": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
        "binding_f1_samples": f1s,
        "round_trip_mean": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "round_trip_samples": scores,
        "success_rate": round(sum(1 for s in scores if s >= PASS_SCORE) / len(scores), 4) if scores else 0.0,
        "avg_gui_steps": round(sum(n_steps) / len(n_steps), 2) if n_steps else None,
        "gui_steps_samples": n_steps,
        "screenshot_dir": gui_shot_dir,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="EE.6 model ablation killtest")
    parser.add_argument("--task", default="release_reschedule")
    parser.add_argument("--backends", default="gpt56sol,glm5v",
                        help="comma-separated backend names (gpt56sol/glm5v/uitars)")
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    backends = args.backends.split(",")
    ts = time.strftime("%Y%m%d_%H%M%S")
    cost_model = CostModel()
    results = []
    for bn in backends:
        bn = bn.strip()
        if bn == "uitars":
            # stub: interface-only, no weights → record as "pluggable, not run"
            logger.info(f"[uitars] stub backend — recording as interface-only (no weights)")
            results.append({"backend": "uitars", "task_id": args.task,
                            "n_samples": 0, "note": "stub (NotImplementedError); "
                            "interface-complete, not run — no UI-TARS weights downloaded",
                            "UITarsBackend": "pluggable"})
            continue
        logger.info(f"\n=== Backend: {bn} ({args.samples} samples) ===")
        r = run_backend(args.task, bn, samples=args.samples, host=args.host,
                        cost_model=cost_model)
        results.append(r)
        logger.info(f"[{bn}] binding_f1={r['binding_f1_mean']} round_trip={r['round_trip_mean']} "
                    f"success={r['success_rate']} avg_steps={r['avg_gui_steps']}")

    report = {
        "ts": ts, "test": "model_ablation", "task": args.task,
        "n_samples_per_backend": args.samples, "cost": cost_model.summary(),
        "results": results,
        "honest_framing": (
            "Model ablation: same task (release_reschedule) across grounding "
            "backends. gpt56sol is the E13/E15 baseline; glm5v tests whether the "
            "result holds on a different vision model (company-gateway glm-5v-turbo). "
            "UITarsBackend is interface-only (stub) — listed as pluggable, not run "
            "(no weights). This is the paper's ablation-table source."
        ),
    }
    out_path = Path(args.out) if args.out else EVAL_DIR / f"model_ablation_{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_path}")
    print(f"\n=== MODEL ABLATION ({args.task}) ===")
    for r in results:
        if r.get("note"):
            print(f"  {r['backend']}: {r['note']}")
        else:
            print(f"  {r['backend']}: binding_f1={r['binding_f1_mean']} round_trip={r['round_trip_mean']} "
                  f"success={r['success_rate']} avg_steps={r['avg_gui_steps']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
