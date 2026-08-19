"""Task state — the first-class object of TaskVM (mental-model doc §4).

A task state is a set of *task variables*. Each variable carries TWO
values, because TaskVM is a control system over reality, not a scratchpad:

- ``observed``: what reality currently shows. Written ONLY by
  observation paths (bottom-up sync, action results, compensation
  re-observation). Never by a patch.
- ``desired``: what the task layer wants reality to become. Written ONLY
  by governance paths (initial composition, LocalPatch, GoalPatch,
  recomposition). Never by an observation.

When ``desired != observed`` the variable is in *pending divergence* —
work is in flight or not yet started. The kernel never lets a patch
pretend the world already moved, and never lets an observation rewrite
the user's intent (contract §3.2 双向可执行性 + §3.5 诚实性).

Hard boundary (contract §3.2 / §5): nothing in this module may
carry a database primary key, an app-internal operation name, or a
substrate-specific selector. ``SurfaceHandle`` is a TaskVM-owned,
short-lived handle id ONLY; the mapping from handle id to any concrete
substrate locator is held privately by the substrate session, never by
domain objects.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from taskvm.domain.errors import ValidationError
from taskvm.domain.intent import TaskIntent

# Mutability values (plain str constants — a 3-way display/behaviour hint).
MUTABILITY_EDITABLE = "editable"
MUTABILITY_READONLY = "readonly"
MUTABILITY_LOCKED = "locked"  # e.g. behind an irreversible action


@dataclass(frozen=True)
class SurfaceHandle:
    """A TaskVM-owned short-lived handle id — the ONLY cross-layer field.

    Concrete locators (whatever a substrate session uses to find the
    element again) are substrate-private and must never appear on this
    object; upper layers reference surfaces exclusively by handle id.
    """

    handle_id: str

    def __post_init__(self) -> None:
        if not self.handle_id:
            raise ValidationError("SurfaceHandle.handle_id must be non-empty")


@dataclass(frozen=True)
class SurfaceEvidence:
    """What was visibly observed, grounding one task variable.

    Only user-visible content is allowed here: the label a person could
    read on screen, the surrounding visible context, and the observed
    value. This is the no-leak contract in data form (GG red line §0:
    "can a real user see this string on the rendered screen?").
    """

    surface: SurfaceHandle
    visible_label: str
    visible_context: str = ""
    observed_value: Any = None
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationError("SurfaceEvidence.confidence must be in [0, 1]")


@dataclass(frozen=True)
class ObservedValue:
    """The observation contract: one freshly-observed value for one task
    variable, with the visible evidence that grounds it.

    This is the only way new reality enters the kernel — value and its
    evidence travel together and land on the SAME variable.
    """

    semantic_key: str
    value: Any
    evidence: tuple[SurfaceEvidence, ...] = ()
    confidence: float | None = None  # None → keep the variable's current confidence

    def __post_init__(self) -> None:
        if not self.semantic_key:
            raise ValidationError("ObservedValue.semantic_key must be non-empty")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValidationError("ObservedValue.confidence must be in [0, 1]")


@dataclass(frozen=True)
class ObservationBatch:
    """One atomic observation delivery.

    A duplicate semantic key inside ONE batch is a CONTENT error (a silent
    last-write-wins would eat a real conflict). It is rejected here, at
    construction — the single owner of the rule (layered ownership
    protocol §5); the kernel folds only validated batches and never
    rescans for duplicates.
    """

    observations: tuple[ObservedValue, ...] = ()

    def __post_init__(self) -> None:
        obs = tuple(self.observations)
        object.__setattr__(self, "observations", obs)
        keys = [o.semantic_key for o in obs]
        if len(set(keys)) != len(keys):
            dups = sorted({k for k in keys if keys.count(k) > 1})
            raise ValidationError(
                f"duplicate semantic keys in one observation batch: {dups}; "
                "aggregate or resolve the conflict upstream first")


@dataclass(frozen=True)
class TaskVariable:
    """One governable task quantity: identity + observed + desired.

    ``semantic_key`` is the cross-layer identity (e.g. "release_date") —
    a semantic name, not a binding into any app.
    """

    semantic_key: str
    label: str
    observed: Any = None
    desired: Any = None
    value_type: str = "string"  # display/serialisation hint: "date" | "status" | ...
    mutability: str = MUTABILITY_EDITABLE
    confidence: float = 1.0
    evidence: tuple[SurfaceEvidence, ...] = ()

    def __post_init__(self) -> None:
        if not self.semantic_key:
            raise ValidationError("TaskVariable.semantic_key must be non-empty")
        if self.mutability not in (MUTABILITY_EDITABLE, MUTABILITY_READONLY,
                                   MUTABILITY_LOCKED):
            raise ValidationError(f"unknown mutability: {self.mutability!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationError("TaskVariable.confidence must be in [0, 1]")
        object.__setattr__(self, "evidence", tuple(self.evidence))

    @property
    def diverged(self) -> bool:
        """Pending divergence: the task layer wants something reality has
        not (yet) shown. The honest 'in flight' signal."""
        return self.desired != self.observed

    def with_observed(self, value: Any, *,
                      evidence: tuple[SurfaceEvidence, ...] | None = None,
                      confidence: float | None = None) -> "TaskVariable":
        """Observation path only. An empty evidence tuple keeps the prior
        evidence (a value sync does not invalidate where it was last seen)."""
        return replace(
            self, observed=value,
            evidence=self.evidence if not evidence else tuple(evidence),
            confidence=self.confidence if confidence is None else confidence)

    def with_desired(self, value: Any) -> "TaskVariable":
        """Governance path only."""
        return replace(self, desired=value)


@dataclass(frozen=True)
class TaskState:
    """The task world TaskVM currently believes in (kernel-owned revisions).

    Immutable: every mutation goes through the kernel, which returns a new
    ``TaskState`` with a strictly greater ``revision`` (invariant 1).
    """

    intent: TaskIntent
    variables: tuple[TaskVariable, ...] = ()
    revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "variables", tuple(self.variables))
        keys = [v.semantic_key for v in self.variables]
        if len(set(keys)) != len(keys):
            raise ValidationError(f"duplicate semantic_key in TaskState: {keys}")
        if self.revision < 0:
            raise ValidationError("TaskState.revision must be >= 0")

    def variable(self, semantic_key: str) -> TaskVariable | None:
        for v in self.variables:
            if v.semantic_key == semantic_key:
                return v
        return None

    def observed_values(self) -> dict[str, Any]:
        return {v.semantic_key: v.observed for v in self.variables}

    def desired_values(self) -> dict[str, Any]:
        return {v.semantic_key: v.desired for v in self.variables}

    def diverged_keys(self) -> tuple[str, ...]:
        return tuple(v.semantic_key for v in self.variables if v.diverged)
