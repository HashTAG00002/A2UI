"""Typed landing results — how the runtime/verifier (E) reports; how the
kernel lands reports on the timeline.

Layered ownership (docs/contracts/layered_ownership_protocol.md §1/§2):
CONTENT correctness (fresh observations, observed == desired, evidence
sufficiency, honest compensated verdicts) is E's responsibility. These
types make bad input inexpressible so the kernel never has to re-prove
content:

- ``VerificationResult`` binds ONE node, ONE epoch, and — for ACTION
  work — exactly ONE finished attempt. There is no bare-bool channel.
- ``CompensationResult`` is built via ``for_plan`` and can reference ONLY
  entries of its plan. There is no free-form ``dict[semantic_key, Any]``
  synchronisation channel: an extra key is not rejected at the kernel
  boundary, it is UNREPRESENTABLE.

The kernel lands these with TIME checks only: identity, epoch, lifecycle,
single-use, coverage.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from taskvm.domain.errors import ValidationError
from taskvm.domain.patch import CompensationPlan


@dataclass(frozen=True)
class VerificationResult:
    """The verifier's typed verdict for one node in one epoch.

    ``action_id`` is REQUIRED when landing on an ACTION node (it names the
    FINISHED attempt this verdict certifies) and must be None for VERIFY
    nodes (they have no action attempt). ``evidence_ref`` is an opaque
    reference to the verifier's evidence bundle — the kernel stores it in
    the event payload but never interprets it.
    """

    node_id: str
    epoch: int
    passed: bool
    action_id: str | None = None
    evidence_ref: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValidationError("VerificationResult.node_id must be non-empty")
        if self.epoch < 0:
            raise ValidationError("VerificationResult.epoch must be >= 0")


@dataclass(frozen=True)
class CompensationEntryResult:
    """E's report for ONE plan entry: the freshly observed value after the
    compensation attempt, and E's verdict for THIS entry. ``compensated``
    is E's content judgment — the kernel archives it, never re-derives it.
    """

    node_id: str
    semantic_key: str
    final_observed: Any = None
    compensated: bool = False


@dataclass(frozen=True)
class CompensationResult:
    """The runtime's typed report against one CompensationPlan.

    Entry identity is plan-bound (see ``for_plan``): outcomes can only
    name real plan entries, each at most once. Coverage may be partial —
    an entry E never attempted is simply absent; the KERNEL decides the
    timeline disposition (complete / partial / failed) from the per-entry
    verdicts and the coverage.
    """

    plan_id: str
    epoch: int
    entry_results: tuple[CompensationEntryResult, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.plan_id:
            raise ValidationError("CompensationResult.plan_id must be non-empty")
        if self.epoch < 0:
            raise ValidationError("CompensationResult.epoch must be >= 0")
        entries = tuple(self.entry_results)
        object.__setattr__(self, "entry_results", entries)
        ids = [(e.node_id, e.semantic_key) for e in entries]
        if len(set(ids)) != len(ids):
            raise ValidationError(
                f"CompensationResult duplicate entry results: {ids}")

    @classmethod
    def for_plan(cls, plan: CompensationPlan, *, epoch: int,
                 outcomes: Iterable[CompensationEntryResult],
                 detail: str = "") -> "CompensationResult":
        """The construction path the runtime uses: every outcome must name
        a REAL entry of ``plan`` — reporting on a foreign node or key is
        inexpressible (it raises here, at the producer boundary)."""
        known = {(e.node_id, e.semantic_key) for e in plan.entries}
        out = tuple(outcomes)
        foreign = [(e.node_id, e.semantic_key) for e in out
                   if (e.node_id, e.semantic_key) not in known]
        if foreign:
            raise ValidationError(
                f"compensation outcome(s) {foreign} do not correspond to "
                f"entries of plan {plan.plan_id}")
        return cls(plan_id=plan.plan_id, epoch=epoch,
                   entry_results=out, detail=detail)
