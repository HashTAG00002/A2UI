"""taskvm_bench.evaluation.cli — the unified benchmark entry point.

Usage (handoff 07):

    python -m taskvm_bench.evaluation.cli list
    python -m taskvm_bench.evaluation.cli run --suite smoke
    python -m taskvm_bench.evaluation.cli run --suite final --condition taskvm
    python -m taskvm_bench.evaluation.cli run --task goalpivot-review \
        --condition direct-cua --condition taskvm --seeds 3
    python -m taskvm_bench.evaluation.cli report --input eval_results/<run_id>
    python -m taskvm_bench.evaluation.cli compare --config configs/paper_matrix.json

RM-0.B (B-08): ``--substrate mobilegym`` routes to the MobileGym
factory over the real bridge/L1 oracle stack. Seed semantics are TWO
different concepts and are never conflated (re-prompt §B-05):

* ``--seeds``    — deterministic-matrix replicates for the builtin
                   ``world`` runner (backwards compatible, unchanged);
* ``--env-seed`` — the MobileGym ENVIRONMENT seed (world initialisation);
* ``--samples``  — real-model sample replicates (stochastic model
                   retries of the SAME environment).

The mobilegym branch passes the ``--condition`` string through VERBATIM
(no registry lookup): the real-model condition definitions (e.g.
``taskvm-real-full``) land with B-06 and this CLI treats them as opaque
identifiers, never re-defining them here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Sequence

from taskvm_bench.benchmark.registry import (
    all_conditions, condition_of, get_suite, list_suites,
)
from taskvm_bench.benchmark.tasks import all_tasks, get_task
from taskvm_bench.evaluation.aggregation import (
    report_from_trials, render_paper_tables,
)
from taskvm_bench.evaluation.runner import BUDGET_PRESETS, BenchmarkRunner


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
    if args.substrate == "mobilegym":
        return _run_mobilegym(args)
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


# ── the mobilegym branch (B-08) ─────────────────────────────────────────

def _run_mobilegym(args: argparse.Namespace) -> int:
    """RM-0.B MobileGym runs: the factory chain over the real bridge.

    The ``--condition`` string is passed through VERBATIM (B-06 owns the
    real-model condition definitions — nothing is re-defined here). Only
    the ``rm-smoke`` plumbing suite / explicit ``--task`` fixture ids are
    accepted: RM-1.0 open-scenario task design is NOT this wave.
    """
    from taskvm_bench.benchmark.mobilegym_fixtures import (
        TOP3_EXPENSE_TO_WECHAT, all_mobilegym_tasks,
    )
    from taskvm_bench.evaluation.mobilegym_factory import (
        MobileGymFactory, MobileGymTrialSpec,
    )
    from taskvm_bench.evaluation.results import RunDirectory

    if args.task:
        fixtures = [all_mobilegym_tasks()[t] for t in args.task]
    elif args.suite in ("rm-smoke", None):
        # the plumbing smoke: the SIMPLEST existing fixture (one binding,
        # one write surface) — no new task design in RM-0.
        fixtures = [TOP3_EXPENSE_TO_WECHAT]
    else:
        raise SystemExit(
            f"suite {args.suite!r} is not defined for --substrate "
            f"mobilegym; use --suite rm-smoke or --task <mobilegym "
            f"fixture id> (known: {sorted(all_mobilegym_tasks())})")

    condition = (args.condition or ["taskvm-real-full"])[0]
    samples = args.samples if args.samples is not None else 1

    # the projection public API server the UserOpDriver talks to (B-04:
    # the bench plane's ONLY handle on the session). Served BEFORE the
    # trials: the store is empty until bootstrap_real_full registers the
    # per-trial session inside run_trial — the first client call happens
    # only at driver time, after registration.
    from taskvm.projection.store import ProjectionSessionStore
    from taskvm.workspace_ui import serve as serve_projection
    from taskvm_bench.evaluation.projection_client import ProjectionClient
    from taskvm_bench.evaluation.user_ops import UserOpDriver
    store = ProjectionSessionStore()
    projection_port = _serve_projection(serve_projection(store),
                                         args.projection_port)
    base_url = f"http://127.0.0.1:{projection_port}"

    run_id = args.run_id or ("rm-mobilegym-" + time.strftime("%Y%m%d-%H%M%S"))
    run_dir = RunDirectory(run_id, root=args.out)
    factory = MobileGymFactory(
        bridge_port=args.bridge_port, connect_only=not args.spawn_bridge)
    trial_manifests: list[dict] = []
    try:
        for fixture in fixtures:
            for sample in range(samples):
                spec = MobileGymTrialSpec(
                    fixture=fixture, environment_seed=args.env_seed,
                    sample_index=sample, condition=condition,
                    model=args.model)
                print(f"trial {fixture.task_id}/e{args.env_seed}"
                      f"/s{sample} …", file=sys.stderr)
                driver = UserOpDriver(
                    ProjectionClient(base_url, spec.resolve_sid()))
                record = factory.run_trial(spec, driver=driver, store=store)
                run_dir.write_trial(record, sample)
                trial_manifests.append(factory.manifest_fields(spec))
                print(f"  → verdict={record.trial_verdict} "
                      f"ops={[o['verdict'] for o in record.user_ops]} "
                      f"eval_error={record.evaluation_error}",
                      file=sys.stderr)
        run_dir.write_manifest(
            substrate="mobilegym", condition=condition,
            model=args.model or "", environment_seed=args.env_seed,
            samples=samples, suite=args.suite or "rm-smoke",
            tasks=[f.task_id for f in fixtures],
            trials=trial_manifests,
        )
    finally:
        factory.close()
    print(f"\nrun dir: {run_dir.root} (manifest.json / trials/) — "
          f"development_only plumbing smoke, NOT an RM task result")
    return 0


def _serve_projection(app, port: int) -> int:
    """Serve the projection Flask app on a daemon thread; ``port=0``
    auto-picks a free one (returned)."""
    import threading
    if not port:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port,
                                threaded=True, debug=False,
                                use_reloader=False),
        daemon=True).start()
    return port


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
        prog="python -m taskvm_bench.evaluation.cli",
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
    p_run.add_argument("--substrate", choices=["world", "mobilegym"],
                       default="world",
                       help="world = the builtin deterministic matrix "
                            "runner; mobilegym = the RM-0.B factory over "
                            "the real bridge/L1 stack (B-08)")
    p_run.add_argument("--samples", type=int, default=None,
                       help="mobilegym only: real-model sample replicates "
                            "(stochastic retries of the SAME environment "
                            "seed; a --seeds alias would conflate two "
                            "concepts — env seed vs model sample)")
    p_run.add_argument("--env-seed", type=int, default=0,
                       help="mobilegym only: the ENVIRONMENT seed (world "
                            "initialisation), distinct from --samples")
    p_run.add_argument("--model", default=None,
                       help="mobilegym only: model id override (default: "
                            "the provider port's own default)")
    p_run.add_argument("--bridge-port", type=int, default=3019,
                       help="mobilegym only: the bridge port to connect to "
                            "or start")
    p_run.add_argument("--projection-port", type=int, default=3026,
                       help="mobilegym only: the projection public-API "
                            "port the UserOpDriver talks to (0 = auto)")
    p_run.add_argument("--spawn-bridge", action="store_true",
                       help="mobilegym only: let the factory SPAWN the "
                            "bridge subprocess (closed flag whitelist — "
                            "never a CUA-loop injection); default is to "
                            "connect to an already-running bridge")
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
