"""taskvm_bench.evaluation.funnel — the Stage Survival Funnel (B-07
trial-integrity round, Task C).

One requested trial can die anywhere between "bridge spawn refused"
and "post-trial integrity unavailable". A verdict table alone answers
"pass/fail" but never "WHERE did the requested trials die?" — and a
batch whose early stages kill every trial before the CUA ever runs
would look identical to a batch that reached CUA and failed there.
The funnel makes that distinction mechanical:

    requested → materialized → entered bootstrap → survived compiler
    → survived architect → entered CUA → completed → strict pass

Every count is derived ONLY from materialized ``TrialRecord`` fields
(``stage_reached`` / ``cua_entered`` / ``trial_verdict`` /
``failure_class`` — the schema-``taskvm-userop-2`` fields); nothing is
inferred, nothing guessed. ``trials_missing`` (requested minus
materialized) is the one number the records alone cannot carry — the
caller, who knows the batch plan, supplies ``trials_requested``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from taskvm_bench.evaluation.results import STAGES, TrialRecord

#: the stage index each survival bar is measured against (STAGES order
#: is causal: setup → compiler → architect → execution → evaluation →
#: complete — imported from the schema, never duplicated here).
_STAGE_INDEX = {s: i for i, s in enumerate(STAGES)}


def _reached(record: Any, stage: str) -> bool:
    """Did this trial reach (enter) ``stage`` or further? Unknown /
    empty ``stage_reached`` (pre-integrity records) reaches nothing —
    honest, never guessed."""
    idx = _STAGE_INDEX.get(_stage_of(record))
    return idx is not None and idx >= _STAGE_INDEX[stage]


def _stage_of(record: Any) -> str:
    if isinstance(record, TrialRecord):
        return record.stage_reached or ""
    return str(record.get("stage_reached") or "")


def _cua_entered(record: Any) -> bool:
    if isinstance(record, TrialRecord):
        return bool(record.cua_entered)
    return bool(record.get("cua_entered", False))


def _verdict(record: Any) -> str:
    if isinstance(record, TrialRecord):
        return record.trial_verdict or ""
    return str(record.get("trial_verdict") or "")


def _failure_class(record: Any) -> str:
    if isinstance(record, TrialRecord):
        return record.failure_class or ""
    return str(record.get("failure_class") or "")


def _eval_error(record: Any) -> Any:
    if isinstance(record, TrialRecord):
        return record.evaluation_error
    return record.get("evaluation_error")


@dataclass
class TrialsFunnel:
    """The survival funnel over ONE batch's materialized records.

    All ``*_rate`` fields are plain floats in [0, 1]; a zero
    denominator yields 0.0 (never a crash — an empty batch is a
    legitimate observation, not an arithmetic error)."""
    trials_requested: int = 0
    trials_materialized: int = 0
    trials_missing: int = 0
    counts_by_stage: dict = field(default_factory=dict)
    counts_by_failure_class: dict = field(default_factory=dict)
    #: survival bars (count + rate, denominator in parentheses)
    entered_bootstrap_count: int = 0
    entered_bootstrap_rate: float = 0.0      # / trials_requested
    survived_compiler_count: int = 0
    survived_compiler_rate: float = 0.0      # / entered_bootstrap
    survived_architect_count: int = 0
    survived_architect_rate: float = 0.0     # / entered_bootstrap
    cua_entry_count: int = 0
    cua_entry_rate: float = 0.0              # / survived_architect
    complete_count: int = 0
    complete_rate: float = 0.0               # / trials_materialized
    strict_pass_count: int = 0
    strict_pass_rate: float = 0.0            # / trials_materialized

    def to_dict(self) -> dict:
        return asdict(self)


def build_funnel(records: Iterable[Any], *, trials_requested: int) -> TrialsFunnel:
    """Fold materialized records (``TrialRecord`` or their dict form)
    into ONE :class:`TrialsFunnel`.

    Definitions (also the test oracle):

    * ``entered bootstrap`` — ``stage_reached`` at or past ``compiler``
      (the SUT composition was entered at all);
    * ``survived compiler`` — at or past ``architect`` (the compiler
      model call landed; sub-attributed via the shared ledger by
      :func:`classify_trial_failure`, recorded as ``stage_reached``);
    * ``survived architect`` — at or past ``execution`` (the kernel /
      runtime assembly completed — the driver plane was entered);
    * ``entered CUA`` — ``cua_entered`` is True (REAL telemetry: ledger
      ``cua`` rows or an observed GUI action), denominator
      ``survived_architect``;
    * ``completed`` — ``stage_reached == complete``, denominator
      ``trials_materialized``;
    * ``strict pass`` — verdict ``pass`` with NO ``evaluation_error``
      (a graded pass never launders a broken world), denominator
      ``trials_materialized``.
    """
    records = list(records)
    f = TrialsFunnel(trials_requested=trials_requested)
    f.trials_materialized = len(records)
    f.trials_missing = max(0, trials_requested - len(records))

    for r in records:
        stage = _stage_of(r) or "(unknown)"
        f.counts_by_stage[stage] = f.counts_by_stage.get(stage, 0) + 1
        fc = _failure_class(r)
        if fc:
            f.counts_by_failure_class[fc] = \
                f.counts_by_failure_class.get(fc, 0) + 1
        if _reached(r, "compiler"):
            f.entered_bootstrap_count += 1
        if _reached(r, "architect"):
            f.survived_compiler_count += 1
        if _reached(r, "execution"):
            f.survived_architect_count += 1
        if _cua_entered(r):
            f.cua_entry_count += 1
        if _stage_of(r) == "complete":
            f.complete_count += 1
        if _verdict(r) == "pass" and _eval_error(r) is None:
            f.strict_pass_count += 1

    n_req = f.trials_requested
    n_boot = f.entered_bootstrap_count
    n_arch = f.survived_architect_count
    n_mat = f.trials_materialized
    f.entered_bootstrap_rate = (n_boot / n_req) if n_req else 0.0
    f.survived_compiler_rate = (f.survived_compiler_count / n_boot) \
        if n_boot else 0.0
    f.survived_architect_rate = (n_arch / n_boot) if n_boot else 0.0
    f.cua_entry_rate = (f.cua_entry_count / n_arch) if n_arch else 0.0
    f.complete_rate = (f.complete_count / n_mat) if n_mat else 0.0
    f.strict_pass_rate = (f.strict_pass_count / n_mat) if n_mat else 0.0
    return f


def render_funnel(f: TrialsFunnel) -> str:
    """The terminal funnel block — one line per survival bar so a
    human sees at a glance WHERE the batch lost its trials."""
    def bar(count: int, rate: float) -> str:
        return f"{count:4d}  ({rate:.3f})"

    stages = "  ".join(
        f"{s}={f.counts_by_stage.get(s, 0)}" for s in STAGES)
    unknown = f.counts_by_stage.get("(unknown)", 0)
    if unknown:
        stages += f"  (unknown)={unknown}"
    classes = "  ".join(
        f"{k}={v}" for k, v in sorted(f.counts_by_failure_class.items())
    ) or "(none)"
    return (
        "── stage survival funnel ───────────────────────────────────\n"
        f"  trials requested:     {f.trials_requested}\n"
        f"  trials materialized:  {f.trials_materialized}"
        f"   (missing: {f.trials_missing})\n"
        f"  stage histogram:      {stages}\n"
        f"  failure classes:      {classes}\n"
        f"  entered bootstrap:   {bar(f.entered_bootstrap_count, f.entered_bootstrap_rate)}"
        f"   / requested\n"
        f"  survived compiler:   {bar(f.survived_compiler_count, f.survived_compiler_rate)}"
        f"   / entered bootstrap\n"
        f"  survived architect:  {bar(f.survived_architect_count, f.survived_architect_rate)}"
        f"   / entered bootstrap\n"
        f"  entered CUA:         {bar(f.cua_entry_count, f.cua_entry_rate)}"
        f"   / survived architect\n"
        f"  completed:           {bar(f.complete_count, f.complete_rate)}"
        f"   / materialized\n"
        f"  strict pass:         {bar(f.strict_pass_count, f.strict_pass_rate)}"
        f"   / materialized"
    )


__all__ = ["TrialsFunnel", "build_funnel", "render_funnel"]
