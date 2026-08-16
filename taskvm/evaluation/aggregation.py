"""taskvm.evaluation.aggregation — failure taxonomy + the report schema.

Handoff 07 §统计与报告 requirements implemented here:

* ONE unified report schema (``taskvm.evaluation.report/1``) — runners do
  not invent their own fields;
* the aggregate is computed FROM the persisted trial JSONs, so the raw
  verdicts stay authoritative (a summary can never overwrite or weaken
  them — it is additive);
* failed / errored trials are counted, never dropped;
* failure taxonomy distinguishes at least observation / compiler /
  architect / CUA / verifier / recovery / budget / environment;
* diagnostic-only conditions (oracle upper bound) are labelled loudly.

Interaction compression is measured from the actual trace (real model
calls + GUI actions); the theoretical lower bound (the task's required
visible writes) is reported alongside, separately labelled — never mixed.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from taskvm.benchmark.registry import DIAGNOSTIC_ONLY_CONDITIONS
from taskvm.evaluation.statistics import mean, percentile, safe_div, wilson_ci

__all__ = [
    "REPORT_SCHEMA", "FAILURE_TAXONOMY", "classify_failure",
    "actions_after_last_pause", "aggregate_trials", "report_from_trials",
    "render_paper_tables",
]

#: The frozen report schema identifier.
REPORT_SCHEMA = "taskvm.evaluation.report/1"

#: The failure taxonomy (handoff 07 minimum set + honest extras).
FAILURE_TAXONOMY: tuple[str, ...] = (
    "success",            # oracle verdict PASS
    "budget",             # trial budget exhausted before the goal
    "cua",                # execution capability boundary (parse/no-target)
    "architect",          # planning failure (no plan / unparseable program)
    "compiler",           # state compilation / rebinding failure
    "observation",        # could not establish what is on screen
    "verifier",           # claimed done, oracle disagrees (false done)
    "recovery",           # governance/recovery failed (rollback fidelity,
    "environment",        #   conflict handling, blocked kernel, ...)
    "evaluation_error",   # the EVALUATION plane failed (never a system
    "harness_crash",      #   result; counted separately)
)

_STOP_CLASS: dict[str, str] = {
    "budget": "budget",
    "budget_exhausted": "budget",
    "cua_fail": "cua",
    "planner_parse_fail": "architect",
    "no_plan": "architect",
    "pending_recompose": "compiler",
    "no_surface": "observation",
    "blocked": "recovery",
    "paused": "recovery",
    "escalated": "recovery",
    "no_ready_work": "recovery",
    "planner_cua_disagree": "verifier",
    "done": "verifier",   # stopped done but the oracle disagreed
}


def classify_failure(rec: Mapping[str, Any]) -> str:
    """Map one trial record onto the failure taxonomy (deterministic,
    precedence-ordered; the first matching rule wins)."""
    if rec.get("evaluation_error"):
        return "evaluation_error"
    if rec.get("harness_crash"):
        return "harness_crash"
    verdict = rec.get("verdict") or {}
    success = bool(verdict.get("success"))
    if success:
        return "success"
    stop = str(rec.get("stop_reason", ""))
    if stop in ("done", "planner_cua_disagree"):
        # the system believed it finished; the frozen predicate says not
        return "verifier"
    if verdict.get("interference_violations") or verdict.get("missing_witness"):
        # overshoot / rollback fidelity / never actually performed the work
        return "recovery"
    if stop in _STOP_CLASS:
        return _STOP_CLASS[stop]
    trace = rec.get("trace") or []
    if any(e.get("event") == "governance_error" for e in trace):
        return "recovery"
    return "environment"


def actions_after_last_pause(trace: Sequence[Mapping[str, Any]]) -> int | None:
    """GUI gestures issued after the most recent pause notification —
    the pause-latency metric (handoff 07 §治理与恢复: 应为 0). None when
    no pause event occurred in this trial."""
    idx = None
    for i, e in enumerate(trace):
        if (e.get("event") == "external"
                and str(e.get("kind")) == "pause_resume"):
            idx = i
    if idx is None:
        return None
    return sum(1 for e in trace[idx + 1:] if "gesture" in e)


def _trial_flags(rec: Mapping[str, Any]) -> dict[str, Any]:
    trace = rec.get("trace") or []
    kinds = {str(e.get("kind")) for e in trace
             if e.get("event") == "external"}
    events = {str(e.get("event")) for e in trace}
    verdict = rec.get("verdict") or {}
    return {
        "goal_patch_injected": "goal_patch" in kinds,
        "rollback_requested": "rollback" in events,
        "conflict_resolved": "conflict_resolution" in events,
        "compensation_executed": "compensation" in events,
        "structure_recovered": "structure_recovery" in events,
        "actions_after_pause": actions_after_last_pause(trace),
        "false_done": (str(rec.get("stop_reason")) == "done"
                       and not verdict.get("success", False)),
    }


def aggregate_trials(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate persisted trial records into the report body (grouped by
    condition, by condition×split, by condition×family)."""
    def _bucket(recs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        n = len(recs)
        graded = [r for r in recs if not r.get("evaluation_error")]
        succ = sum(1 for r in graded
                   if (r.get("verdict") or {}).get("success"))
        eval_err = n - len(graded)
        crash = sum(1 for r in recs if r.get("harness_crash"))
        classes = Counter(r.get("failure_class", "?") for r in recs)
        roles: dict[str, list[int]] = {}
        toks: dict[str, list[int]] = {}
        for r in recs:
            for role, c in (r.get("model_calls_by_role") or {}).items():
                roles.setdefault(role, [0, 0])
                roles[role][0] += int(c)
                roles[role][1] += 1
            for role, pt in (r.get("model_tokens_by_role") or {}).items():
                p, c = (pt if isinstance(pt, (list, tuple)) else (0, 0))
                toks.setdefault(role, [0, 0])
                toks[role][0] += int(p)
                toks[role][1] += int(c)
        elapsed = [float(r.get("elapsed_ms", 0.0)) for r in recs]
        gui = [int(r.get("gui_actions", 0)) for r in recs]
        writes = [int(r.get("system_writes", 0)) for r in recs]
        flags = [_trial_flags(r) for r in recs]
        aap = [f["actions_after_pause"] for f in flags
               if f["actions_after_pause"] is not None]
        total_calls = sum(v[0] for v in roles.values())
        out = {
            "n_trials": n,
            "graded": len(graded),
            "successes": succ,
            "evaluation_errors": eval_err,
            "harness_crashes": crash,
            "success_rate": safe_div(succ, len(graded)),
            "success_rate_ci95": list(wilson_ci(succ, len(graded))),
            "failure_taxonomy": {c: classes.get(c, 0)
                                 for c in FAILURE_TAXONOMY
                                 if classes.get(c, 0)},
            "total_model_calls": total_calls,
            "mean_model_calls_by_role": {
                r: safe_div(v[0], v[1]) for r, v in sorted(roles.items())},
            "mean_tokens_by_role": {
                r: [v[0], v[1]] for r, v in sorted(toks.items())},
            "mean_gui_actions": mean(gui),
            "mean_system_writes": mean(writes),
            "interaction_compression": {
                "note": ("measured from the trace: total model calls + GUI "
                         "actions per required write; the theoretical "
                         "lower bound (1 gesture per required write) is "
                         "reported separately and never mixed in"),
                "mean_interactions_per_required_write": safe_div(
                    mean([int(r.get("total_interactions", 0))
                          for r in recs]),
                    mean([max(1, int(r.get("required_ops", 1)))
                          for r in recs])),
            },
            "elapsed_ms": {
                "p50": percentile(elapsed, 50),
                "p90": percentile(elapsed, 90),
                "p95": percentile(elapsed, 95),
            },
            "governance": {
                "goal_patch_trials": sum(
                    1 for f in flags if f["goal_patch_injected"]),
                "rollback_trials": sum(
                    1 for f in flags if f["rollback_requested"]),
                "conflict_resolved_trials": sum(
                    1 for f in flags if f["conflict_resolved"]),
                "compensation_trials": sum(
                    1 for f in flags if f["compensation_executed"]),
                "structure_recovery_trials": sum(
                    1 for f in flags if f["structure_recovered"]),
                "false_done_trials": sum(
                    1 for f in flags if f["false_done"]),
                "mean_actions_after_pause": (mean(aap) if aap else None),
                "pause_trials": len(aap),
            },
        }
        return out

    by_cond: dict[str, list[Mapping[str, Any]]] = {}
    for r in records:
        by_cond.setdefault(str(r["condition"]), []).append(r)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "by_condition": {c: _bucket(rs)
                         for c, rs in sorted(by_cond.items())},
        "by_condition_split": {},
        "by_condition_family": {},
    }
    for c, rs in sorted(by_cond.items()):
        splits: dict[str, list[Mapping[str, Any]]] = {}
        fams: dict[str, list[Mapping[str, Any]]] = {}
        for r in rs:
            splits.setdefault(str(r["split"]), []).append(r)
            fams.setdefault(str(r["family"]), []).append(r)
        report["by_condition_split"][c] = {
            s: _bucket(sub) for s, sub in sorted(splits.items())}
        report["by_condition_family"][c] = {
            f: _bucket(sub) for f, sub in sorted(fams.items())}
    return report


def report_from_trials(run_config: Mapping[str, Any],
                       records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The full report document (config echo + aggregates + loud labels)."""
    diag = sorted(c.value if hasattr(c, "value") else str(c)
                  for c in DIAGNOSTIC_ONLY_CONDITIONS)
    return {
        "schema": REPORT_SCHEMA,
        "config": dict(run_config),
        "diagnostic_only_conditions": diag,
        "warning": ("taskvm-oracle-upper-bound consumes evaluation-plane "
                    "ground truth: diagnostic upper bound only, NEVER a "
                    "main paper result"),
        "n_trials": len(records),
        "totals": {
            "successes": sum(
                1 for r in records
                if not r.get("evaluation_error")
                and (r.get("verdict") or {}).get("success")),
            "evaluation_errors": sum(
                1 for r in records if r.get("evaluation_error")),
            "harness_crashes": sum(
                1 for r in records if r.get("harness_crash")),
        },
        **aggregate_trials(records),
    }


def render_paper_tables(report: Mapping[str, Any]) -> str:
    """A compact markdown rendering of the report (the ``paper`` format).

    Primary conditions first, ablations after, diagnostic last — the same
    ordering discipline the paper tables must keep."""
    lines: list[str] = []
    lines.append(f"# Benchmark report `{report.get('config', {}).get('run_id', '')}`")
    lines.append("")
    lines.append(f"schema: `{report['schema']}` — "
                 f"{report['n_trials']} trials "
                 f"({report['totals']['successes']} pass, "
                 f"{report['totals']['evaluation_errors']} eval-error, "
                 f"{report['totals']['harness_crashes']} crash)")
    diag = set(report.get("diagnostic_only_conditions", ()))
    lines.append("")
    lines.append("## by condition")
    lines.append("| condition | n | success rate [95% CI] | false done | "
                 "model calls | GUI actions | top failure |")
    lines.append("|---|---|---|---|---|---|---|")
    order = ([c for c in report["by_condition"] if c not in diag]
             + sorted(diag & set(report["by_condition"])))
    for cond in order:
        b = report["by_condition"][cond]
        tax = b["failure_taxonomy"]
        top = max(((v, k) for k, v in tax.items() if k != "success"),
                  default=(0, "—"))
        flag = " ⚠DIAGNOSTIC" if cond in diag else ""
        lines.append(
            f"| {cond}{flag} | {b['n_trials']} | "
            f"{b['success_rate']:.3f} "
            f"[{b['success_rate_ci95'][0]:.2f}, "
            f"{b['success_rate_ci95'][1]:.2f}] | "
            f"{b['governance']['false_done_trials']} | "
            f"{b['total_model_calls']} | "
            f"{b['mean_gui_actions']:.1f} | "
            f"{top[1]}×{top[0]} |")
    lines.append("")
    lines.append("## by condition × split (success rate)")
    splits = sorted({s for row in report["by_condition_split"].values()
                     for s in row})
    lines.append("| condition | " + " | ".join(splits) + " |")
    lines.append("|---|" + "---|" * len(splits))
    conds = [c for c in order if c in report["by_condition_split"]]
    for cond in conds:
        row = report["by_condition_split"][cond]
        cells = [f"{row[s]['success_rate']:.2f} ({row[s]['n_trials']})"
                 if s in row else "—" for s in splits]
        lines.append(f"| {cond} | " + " | ".join(cells) + " |")
    lines.append("")
    gov = {c: report["by_condition"][c]["governance"]
           for c in order if c in report["by_condition"]}
    if any(g["pause_trials"] or g["rollback_trials"]
           or g["conflict_resolved_trials"] for g in gov.values()):
        lines.append("## governance signals")
        lines.append("| condition | rollbacks | compensations | conflicts | "
                     "pause trials | mean actions after pause |")
        lines.append("|---|---|---|---|---|---|")
        for cond, g in gov.items():
            aap = (f"{g['mean_actions_after_pause']:.2f}"
                   if g["mean_actions_after_pause"] is not None else "—")
            lines.append(f"| {cond} | {g['rollback_trials']} | "
                         f"{g['compensation_trials']} | "
                         f"{g['conflict_resolved_trials']} | "
                         f"{g['pause_trials']} | {aap} |")
        lines.append("")
    return "\n".join(lines)


def iter_trial_files(run_dir: str) -> Iterable[str]:
    import os
    trials = os.path.join(run_dir, "trials")
    if os.path.isdir(trials):
        for name in sorted(os.listdir(trials)):
            if name.endswith(".json"):
                yield os.path.join(trials, name)
