"""taskvm.evaluation.cli — the unified benchmark entry point.

Usage (handoff 07):

    python -m taskvm.evaluation.cli list
    python -m taskvm.evaluation.cli run --suite smoke
    python -m taskvm.evaluation.cli run --suite final --condition taskvm
    python -m taskvm.evaluation.cli run --task goalpivot-review \
        --condition direct-cua --condition taskvm --seeds 3
    python -m taskvm.evaluation.cli report --input eval_results/<run_id>
    python -m taskvm.evaluation.cli compare --config configs/paper_matrix.json

No phase/gate/killtest vocabulary, one report schema, one runner. The
``--substrate`` flag accepts only ``world`` today: builtin_web is an
honest pending dependency (substrate.md transitional debt register),
never a silent stub.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence

from taskvm.benchmark.registry import (
    all_conditions, condition_of, get_suite, list_suites,
)
from taskvm.benchmark.tasks import all_tasks, get_task
from taskvm.evaluation.aggregation import (
    report_from_trials, render_paper_tables,
)
from taskvm.evaluation.runner import BUDGET_PRESETS, BenchmarkRunner


def _conditions_from(names: Sequence[str] | None):
    if not names:
        return list(all_conditions())
    return [condition_of(n) for n in names]


def _tasks_from(args: argparse.Namespace):
    if args.task:
        return [get_task(t) for t in args.task]
    return list(get_suite(args.suite).tasks())


# ── subcommands ─────────────────────────────────────────────────────────────

def cmd_list(args: argparse.Namespace) -> int:
    print("suites:")
    for s in list_suites():
        print(f"  {s.suite_id:12s} {len(s.task_ids):2d} tasks — "
              f"{s.description}")
    print("\ntasks:")
    for t in all_tasks():
        print(f"  {t.task_id:20s} family={t.family.value:16s} "
              f"split={t.split.value:18s} "
              f"injections={len(t.injections)} "
              f"witness={len(t.witness)}")
    print("\nconditions:")
    for c in all_conditions():
        print(f"  {c.value}")
    if args.what and args.what != "all":
        pass
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    tasks = _tasks_from(args)
    conditions = _conditions_from(args.condition)
    budget = BUDGET_PRESETS[args.budget]
    runner = BenchmarkRunner(out_dir=args.out, substrate=args.substrate)
    note = (f"suite={args.suite}" if not args.task
            else f"tasks={','.join(args.task)}")
    report = runner.run(
        tasks, conditions, seeds=args.seeds, budget=budget,
        run_id=args.run_id, keep_traces=not args.no_traces, note=note,
        progress=lambda m: print(m, file=sys.stderr))
    print(render_paper_tables(report))
    run_dir = os.path.join(args.out, report["config"]["run_id"])
    print(f"\nrun dir: {run_dir} "
          f"(report.json / report.md / trials/*.json)")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    if os.path.isdir(args.input):
        records = BenchmarkRunner.load_trials(args.input)
        cfg_path = os.path.join(args.input, "config.json")
        cfg = {}
        if os.path.isfile(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        elif os.path.isfile(os.path.join(args.input, "report.json")):
            with open(os.path.join(args.input, "report.json"),
                      encoding="utf-8") as f:
                cfg = json.load(f).get("config", {})
        report = report_from_trials(cfg, records)
    else:
        with open(args.input, encoding="utf-8") as f:
            report = json.load(f)
    if args.format == "json":
        print(json.dumps(report, indent=1, sort_keys=True))
    else:
        print(render_paper_tables(report))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            if args.format == "json":
                json.dump(report, f, indent=1, sort_keys=True)
            else:
                f.write(render_paper_tables(report))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    with open(args.config, encoding="utf-8") as f:
        if args.config.endswith(".json"):
            cfg = json.load(f)
        else:
            try:
                import yaml                     # optional dependency
                cfg = yaml.safe_load(f)
            except ImportError as e:
                raise SystemExit(
                    "YAML config needs pyyaml; use --config with .json") from e
    tasks = ([get_task(t) for t in cfg.get("tasks", [])]
             or list(get_suite(cfg.get("suite", "final")).tasks()))
    conditions = _conditions_from(cfg.get("conditions"))
    budget = BUDGET_PRESETS[cfg.get("budget", "paper")]
    runner = BenchmarkRunner(out_dir=cfg.get("out", "eval_results"),
                             substrate=cfg.get("substrate", "world"))
    report = runner.run(
        tasks, conditions, seeds=int(cfg.get("seeds", 3)), budget=budget,
        run_id=cfg.get("run_id"), note=f"compare config={args.config}",
        progress=lambda m: print(m, file=sys.stderr))
    print(render_paper_tables(report))
    return 0


# ── the parser ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m taskvm.evaluation.cli",
        description="The TaskVM final benchmark (unified runner).")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list suites, tasks, conditions")
    p_list.add_argument("--what", choices=["all", "suites", "tasks",
                                           "conditions"], default="all")
    p_list.set_defaults(fn=cmd_list)

    p_run = sub.add_parser("run", help="run a benchmark matrix")
    p_run.add_argument("--suite", default="smoke",
                       help="suite id (default: smoke)")
    p_run.add_argument("--task", action="append", default=None,
                       help="task id (repeatable; overrides --suite)")
    p_run.add_argument("--condition", action="append", default=None,
                       help="condition (repeatable; default: all)")
    p_run.add_argument("--seeds", type=int, default=1,
                       help="trials per (task, condition)")
    p_run.add_argument("--budget", choices=sorted(BUDGET_PRESETS),
                       default="smoke")
    p_run.add_argument("--substrate", choices=["world"], default="world",
                       help="builtin_web is a registered pending "
                            "dependency, not a silent stub")
    p_run.add_argument("--out", default="eval_results")
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--no-traces", action="store_true",
                       help="strip traces from persisted trials")
    p_run.set_defaults(fn=cmd_run)

    p_rep = sub.add_parser("report", help="(re-)render a report")
    p_rep.add_argument("--input", required=True,
                       help="run dir or report.json path")
    p_rep.add_argument("--format", choices=["paper", "json"],
                       default="paper")
    p_rep.add_argument("--out", default=None,
                       help="also write the rendering to this file")
    p_rep.set_defaults(fn=cmd_report)

    p_cmp = sub.add_parser("compare", help="run a paper matrix from config")
    p_cmp.add_argument("--config", required=True,
                       help="JSON (or YAML if pyyaml present) matrix config")
    p_cmp.set_defaults(fn=cmd_compare)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
