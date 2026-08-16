"""taskvm_bench.evaluation.runner — the environment controller and trial runner.

Permission separation is PHYSICAL (handoff 07 §权限隔离):

* the SYSTEM under test (a harness) meets the world only through
  :class:`~taskvm_bench.evaluation.world.WorldSubstrate` — observe / act /
  capture / close. No reset, no seed, no oracle read;
* THIS module is the environment controller: it builds the
  :class:`~taskvm_bench.evaluation.world.BenchmarkWorld` from the frozen spec,
  takes the hidden pre-trial snapshot, fires ``after_writes=0``
  injections, routes governance-shaped events to the harness, tears down;
* the :class:`~taskvm_bench.evaluation.oracle.Oracle` grades from the hidden
  state. An oracle crash NEVER changes what the system did — the trial is
  marked ``evaluation_error`` and kept in the report (never dropped).

Determinism: the world has no clocks and no randomness (timestamp 0.0,
sorted rendering, sha256 fingerprints), so two trials of the same
(task, condition, seed) traverse the same timeline byte for byte. The
``seed`` is recorded per trial and plumbed through for the day stochastic
task generators land; today repeated trials are identical BY
CONSTRUCTION — that is the reproducibility guarantee, stated honestly
rather than via a seed that silently does nothing.

Persistence: every trial lands as one JSON file under
``<out>/<run_id>/trials/``; the aggregate report is computed FROM those
files (the raw verdicts stay authoritative; the summary is additive).
``eval_results/`` is git-ignored by project rule.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Sequence

from taskvm_bench.benchmark.registry import Condition
from taskvm_bench.benchmark.schema import TaskSpec
from taskvm_bench.evaluation.aggregation import (
    classify_failure, report_from_trials, render_paper_tables,
)
from taskvm_bench.evaluation.harness import (
    HarnessOutcome, TrialBudget, make_harness,
)
from taskvm_bench.evaluation.oracle import EvaluationError, Oracle
from taskvm_bench.evaluation.world import BenchmarkWorld, WorldSubstrate

__all__ = [
    "TrialRecord", "RunConfig", "BenchmarkRunner", "run_trial",
    "BUDGET_PRESETS",
]


#: Budget presets. ``paper`` raises the loop/round ceilings so the
#: bounded-loop and rollback families are not budget-starved; the caps
#: that guard against runaway repairs stay tight either way.
BUDGET_PRESETS: dict[str, TrialBudget] = {
    "smoke": TrialBudget(),
    "paper": TrialBudget(max_turns=96, max_rounds=32),
}


@dataclass(frozen=True)
class TrialRecord:
    """One graded trial, JSON-serializable (the persistence unit)."""

    run_id: str
    task_id: str
    family: str
    split: str
    condition: str
    seed: int
    stop_reason: str
    success: bool | None                    # None = evaluation error
    verdict: dict[str, Any] | None
    evaluation_error: str | None = None
    harness_crash: str | None = None
    failure_class: str = ""
    model_calls_by_role: dict[str, int] = field(default_factory=dict)
    model_tokens_by_role: dict[str, list[int]] = field(default_factory=dict)
    gui_actions: int = 0
    total_interactions: int = 0
    required_ops: int = 0
    elapsed_ms: float = 0.0
    system_writes: int = 0
    heartbeats: int | None = None          # taskvm conditions only
    model_heartbeats: int | None = None    # heartbeats that invoked a model
    observed_plane_mismatches: int | None = None   # round-trip projection
    goalpatch_reuse: dict | None = None    # {committed, total} after a GP
    injections_fired: list[str] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(d: dict[str, Any]) -> "TrialRecord":
        return TrialRecord(
            run_id=str(d["run_id"]), task_id=str(d["task_id"]),
            family=str(d["family"]), split=str(d["split"]),
            condition=str(d["condition"]), seed=int(d["seed"]),
            stop_reason=str(d["stop_reason"]),
            success=(None if d.get("success") is None
                     else bool(d["success"])),
            verdict=d.get("verdict"),
            evaluation_error=d.get("evaluation_error"),
            harness_crash=d.get("harness_crash"),
            failure_class=str(d.get("failure_class", "")),
            model_calls_by_role=dict(d.get("model_calls_by_role") or {}),
            model_tokens_by_role=dict(
                d.get("model_tokens_by_role") or {}),
            gui_actions=int(d.get("gui_actions", 0)),
            total_interactions=int(d.get("total_interactions", 0)),
            required_ops=int(d.get("required_ops", 0)),
            elapsed_ms=float(d.get("elapsed_ms", 0.0)),
            system_writes=int(d.get("system_writes", 0)),
            heartbeats=(None if d.get("heartbeats") is None
                        else int(d["heartbeats"])),
            model_heartbeats=(None if d.get("model_heartbeats") is None
                              else int(d["model_heartbeats"])),
            observed_plane_mismatches=(
                None if d.get("observed_plane_mismatches") is None
                else int(d["observed_plane_mismatches"])),
            goalpatch_reuse=d.get("goalpatch_reuse"),
            injections_fired=list(d.get("injections_fired") or []),
            trace=list(d.get("trace") or []),
            detail=str(d.get("detail", "")),
            extras=dict(d.get("extras") or {}),
        )


@dataclass(frozen=True)
class RunConfig:
    """Echoed verbatim into the report so a run directory is self-describing."""

    run_id: str
    tasks: tuple[str, ...]
    conditions: tuple[str, ...]
    seeds: int
    budget_preset: str
    substrate: str
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "tasks": list(self.tasks),
            "conditions": list(self.conditions), "seeds": self.seeds,
            "budget_preset": self.budget_preset,
            "substrate": self.substrate, "note": self.note,
        }


def run_trial(spec: TaskSpec, condition: Condition, *, seed: int,
              run_id: str, budget: TrialBudget,
              progress: Callable[[str], None] | None = None
              ) -> TrialRecord:
    """Execute + grade ONE trial. Never raises for system-side failures:
    a harness crash is recorded as ``harness_crash``; an oracle crash as
    ``evaluation_error`` — both stay in the report (never dropped)."""
    harness = make_harness(condition, spec=spec)
    world = BenchmarkWorld(spec, on_external=harness.route_external)
    substrate = WorldSubstrate(world)

    # ── environment controller: hidden snapshot, pre-start injections ──
    pre_snapshot = world.snapshot()
    world.begin_trial()

    # ── the system under test runs (its ONLY world face: substrate) ────
    t0 = time.perf_counter()
    crash: str | None = None
    outcome: HarnessOutcome | None = None
    try:
        outcome = harness.run(substrate, spec.goal, budget=budget)
    except Exception as e:                  # noqa: BLE001 — honest capture
        crash = f"{type(e).__name__}: {e}"
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # ── the oracle grades the hidden state (crash → evaluation_error) ──
    verdict_json: dict[str, Any] | None = None
    eval_error: str | None = None
    try:
        verdict_json = Oracle(spec).grade(world, pre_snapshot).to_json()
    except EvaluationError as e:
        eval_error = str(e)

    if outcome is None:
        stop_reason, detail = "harness_crash", crash or ""
        calls: dict[str, int] = {}
        toks: dict[str, list[int]] = {}
        gui_actions = 0
        trace: list[dict[str, Any]] = []
        extras: dict[str, Any] = {}
    else:
        stop_reason = outcome.stop_reason
        detail = outcome.detail
        calls = dict(outcome.model_calls_by_role)
        toks = {r: list(v) for r, v in
                (outcome.model_tokens_by_role or {}).items()}
        gui_actions = outcome.gui_actions
        trace = list(outcome.trace)
        extras = dict(outcome.extras)

    body: dict[str, Any] = dict(
        run_id=run_id, task_id=spec.task_id,
        family=spec.family.value, split=spec.split.value,
        condition=condition.value, seed=seed, stop_reason=stop_reason,
        success=(None if verdict_json is None
                 else bool(verdict_json.get("success"))),
        verdict=verdict_json, evaluation_error=eval_error,
        harness_crash=crash,
        model_calls_by_role=calls,
        model_tokens_by_role=toks,
        gui_actions=gui_actions,
        total_interactions=sum(calls.values()) + gui_actions,
        required_ops=max(1, len(spec.required_writes) + len(spec.witness)),
        elapsed_ms=round(elapsed_ms, 3),
        system_writes=world.system_writes(),
        injections_fired=[i.kind.value for i in world.fired_injections()],
        trace=trace, detail=detail, extras=extras,
    )
    # pure classification over the finished body (keeps the taxonomy a
    # function of the persisted record, not of in-memory side channels)
    body["failure_class"] = classify_failure(body)
    # sync-cost + goalpatch-reuse fields (None for text-only conditions,
    # which have no governance plane to instrument)
    body["heartbeats"] = extras.get("heartbeats")
    body["model_heartbeats"] = extras.get("model_heartbeats")
    body["goalpatch_reuse"] = extras.get("goalpatch_reuse")
    # round-trip projection correctness (eval-plane measurement): the
    # system-maintained observed plane (keyed by VISIBLE label) vs the
    # hidden canonical snapshot. A plane entry counts as matching when ANY
    # surface carries the same key with the same value (multi-surface
    # copies may legitimately differ — the authoritative register vs the
    # local copy); a mismatch means the system believes a value the world
    # nowhere holds. Relabelled keys simply stop matching any world key —
    # not a projection lie.
    plane = (extras or {}).get("observed_plane")
    if isinstance(plane, dict) and plane:
        world_snap = world.snapshot()
        mism = 0
        for lbl, val in plane.items():
            if not any(lbl in kv and str(kv[lbl]) == str(val)
                       for kv in world_snap.values()):
                mism += 1
        body["observed_plane_mismatches"] = mism
    rec = TrialRecord(**body)
    if progress:
        progress(f"{spec.task_id}/{condition.value}/s{seed}: "
                 f"stop={stop_reason} "
                 f"{'PASS' if rec.success else ('EVAL-ERR' if eval_error else 'FAIL')}")
    return rec


class BenchmarkRunner:
    """The matrix executor: (task × condition × seed), persisted + graded."""

    def __init__(self, *, out_dir: str = "eval_results",
                 substrate: str = "world") -> None:
        if substrate != "world":
            raise ValueError(
                f"substrate {substrate!r} is not landed yet — the honest "
                f"pending dependency is builtin_web (substrate.md debt "
                f"register); only 'world' ships today")
        self.out_dir = out_dir
        self.substrate = substrate

    def run(self, tasks: Sequence[TaskSpec],
            conditions: Sequence[Condition], *, seeds: int = 1,
            budget: TrialBudget | None = None,
            run_id: str | None = None,
            keep_traces: bool = True,
            note: str = "",
            progress: Callable[[str], None] | None = None,
            ) -> dict[str, Any]:
        budget = budget or BUDGET_PRESETS["smoke"]
        run_id = run_id or time.strftime("run-%Y%m%d-%H%M%S")
        cfg = RunConfig(
            run_id=run_id, tasks=tuple(t.task_id for t in tasks),
            conditions=tuple(c.value for c in conditions), seeds=seeds,
            budget_preset=("paper" if budget is BUDGET_PRESETS["paper"]
                           else "custom"),
            substrate=self.substrate, note=note)
        run_dir = os.path.join(self.out_dir, run_id)
        trials_dir = os.path.join(run_dir, "trials")
        os.makedirs(trials_dir, exist_ok=True)

        records: list[TrialRecord] = []
        idx = 0
        for spec in tasks:
            for cond in conditions:
                for seed in range(seeds):
                    idx += 1
                    rec = run_trial(spec, cond, seed=seed, run_id=run_id,
                                    budget=budget, progress=progress)
                    records.append(rec)
                    body = rec.to_json()
                    if not keep_traces:
                        body["trace"] = (["<stripped: --no-traces>"]
                                         if rec.trace else [])
                    fname = (f"{idx:03d}-{spec.task_id}-{cond.value}"
                             f"-s{seed}.json")
                    with open(os.path.join(trials_dir, fname), "w",
                              encoding="utf-8") as f:
                        json.dump(body, f, indent=1, sort_keys=True)

        # the report is computed FROM the persisted files (raw verdicts
        # stay authoritative — the summary is additive, never a rewrite)
        loaded = self.load_trials(run_dir)
        report = report_from_trials(cfg.to_json(), loaded)
        with open(os.path.join(run_dir, "report.json"), "w",
                  encoding="utf-8") as f:
            json.dump(report, f, indent=1, sort_keys=True)
        with open(os.path.join(run_dir, "report.md"), "w",
                  encoding="utf-8") as f:
            f.write(render_paper_tables(report))
        return report

    # ── re-aggregation from disk ───────────────────────────────────────
    @staticmethod
    def load_trials(run_dir: str) -> list[dict[str, Any]]:
        trials_dir = os.path.join(run_dir, "trials")
        out: list[dict[str, Any]] = []
        for name in sorted(os.listdir(trials_dir)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(trials_dir, name), encoding="utf-8") as f:
                out.append(json.load(f))
        return out

    @staticmethod
    def load_report(run_dir: str) -> dict[str, Any]:
        with open(os.path.join(run_dir, "report.json"),
                  encoding="utf-8") as f:
            return json.load(f)
