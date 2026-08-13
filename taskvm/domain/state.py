"""Task state — the first-class object of TaskVM (mental-model doc §4).

A task state is a set of *task variables* (semantic quantities the user
can see and govern) plus the *surface evidence* that grounds each variable
in what was actually visible on some substrate.

Hard boundary (master handoff §3.2 / §5): nothing in this module may carry
a database primary key, an app-internal operation name, or a
substrate-specific selector. ``SurfaceHandle`` is a TaskVM-owned,
short-lived handle; its ``opaque_token`` may wrap whatever a substrate
session produced, but the domain never interprets it.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from taskvm.domain.errors import ValidationError
from taskvm.domain.intent import TaskIntent

# Mutability values (kept as plain str constants — an Enum adds no value
# for a 3-way display/behaviour hint and complicates serialisation).
MUTABILITY_EDITABLE = "editable"
MUTABILITY_READONLY = "readonly"
MUTABILITY_LOCKED = "locked"  # e.g. behind an irreversible action


@dataclass(frozen=True)
class SurfaceHandle:
    """A TaskVM-owned short-lived handle to a visible surface element.

    ``opaque_token`` may carry a substrate-session token (DOM path, a11y
    node ref, coordinates — the domain does not know and does not care).
    It is never a stable cross-session identity and never a storage key.
    """

    handle_id: str
    opaque_token: str | None = None

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
class TaskVariable:
    """One governable task quantity.

    ``semantic_key`` is the cross-layer identity (e.g. "release_date").
    It is a semantic name, not a binding into any app. ``value`` is the
    value TaskVM currently believes (sourced from observations or from a
    user edit awaiting execution).
    """

    semantic_key: str
    label: str
    value: Any = None
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

    def with_value(self, value: Any, *, confidence: float | None = None) -> "TaskVariable":
        return replace(
            self, value=value,
            confidence=self.confidence if confidence is None else confidence)


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

    def values(self) -> dict[str, Any]:
        return {v.semantic_key: v.value for v in self.variables}
