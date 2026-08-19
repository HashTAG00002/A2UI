"""TaskIntent — what the human governs (mental-model doc §3.4).

The intent is the terminal condition of a task session: the goal, the
boundaries, and the success criteria. It is pure task semantics: it must
never name a concrete app implementation, a database key, or a substrate
selector. ``scope`` entries are user-visible names (what a person would
call the task areas), not platform identifiers.
"""
from __future__ import annotations

from dataclasses import dataclass

from taskvm.domain.errors import ValidationError


@dataclass(frozen=True)
class TaskIntent:
    """The governable terminal condition of one task session.

    Changed only by a GoalPatch (never by a LocalPatch — that is the
    defining boundary between the two patch classes, contract §5).
    """

    goal: str
    constraints: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValidationError("TaskIntent.goal must be a non-empty string")
        # coerce list-likes to tuples so the frozen object stays hashable-safe
        for f in ("constraints", "scope", "success_criteria"):
            object.__setattr__(self, f, tuple(getattr(self, f)))

    def describes_same_terminal(self, other: "TaskIntent") -> bool:
        """Whether two intents share goal/scope/constraints/success
        criteria — ALL four define the terminal condition.

        Used by the kernel to detect that a GoalPatch actually changes the
        terminal condition (vs. a pure topology re-organisation, which is
        still a GoalPatch but does not alter the intent itself).
        """
        return (
            self.goal == other.goal
            and self.scope == other.scope
            and self.constraints == other.constraints
            and self.success_criteria == other.success_criteria
        )
