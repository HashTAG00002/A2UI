"""Governance events — the unified L4 entry vocabulary.

Six events, one entry point (``GovernanceService.handle``). They carry
pure governance SEMANTICS — user-visible intent, never platform selectors
or internal ids. The service maps them onto kernel commands (and, for
GoalPatch only, one Task Architect recomposition).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from taskvm.domain.errors import ValidationError
from taskvm.domain.intent import TaskIntent


@dataclass(frozen=True)
class GovernanceEvent:
    """Base: a correlation id links the event to the causing user gesture."""

    correlation_id: str = ""
    rationale: str = ""


@dataclass(frozen=True)
class PauseRequested(GovernanceEvent):
    """Stop scheduling new work at the next action boundary."""


@dataclass(frozen=True)
class ResumeRequested(GovernanceEvent):
    """Resume forward autonomy inside the governance boundary."""


@dataclass(frozen=True)
class LocalPatchRequested(GovernanceEvent):
    """Local adjustment: terminal goal + topology unchanged.

    ``updates`` maps task-variable semantic keys to new target values.
    Locally-scoped by construction — adding/renaming variables is a scope
    change and belongs to :class:`GoalPatchRequested`.
    """

    updates: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.updates:
            raise ValidationError(
                "LocalPatchRequested needs at least one variable update; "
                "use GoalPatchRequested for scope changes")


@dataclass(frozen=True)
class GoalPatchRequested(GovernanceEvent):
    """Terminal / scope / constraint / topology change.

    ``new_intent`` None = terminal condition unchanged but the future
    topology must be re-organised (still a GoalPatch).
    """

    new_intent: TaskIntent | None = None


@dataclass(frozen=True)
class RollbackRequested(GovernanceEvent):
    """Return reality to a previously confirmed checkpoint."""

    target_checkpoint_id: str = ""

    def __post_init__(self) -> None:
        if not self.target_checkpoint_id:
            raise ValidationError(
                "RollbackRequested needs a target_checkpoint_id")


@dataclass(frozen=True)
class ConflictResolutionRequested(GovernanceEvent):
    """A conflict was surfaced and the user chose a resolution."""

    description: str = ""
    semantic_keys: tuple[str, ...] = ()
    resolution: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic_keys",
                           tuple(self.semantic_keys))
        if not self.resolution:
            raise ValidationError(
                "ConflictResolutionRequested needs the chosen resolution")
