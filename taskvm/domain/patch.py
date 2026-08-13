"""The three governance patch classes (master handoff §5, oracle export
§3.1: '论文就讲 LocalPatch / GoalPatch / CompensationPatch 三种').

The class boundary IS the semantics — the kernel routes on the type:

- ``LocalPatch``: touches neither the terminal intent nor the workflow
  topology. Only variable target values and (rarely) the contract of a
  not-yet-committed node. Never requires the Task Architect.
- ``GoalPatch``: changes the terminal goal / scope / constraints / success
  criteria and/or the future topology. ALWAYS signals replan: the upper
  layer must re-organise the uncommitted future; committed history is
  preserved (kernel invariant 3).
- ``CompensationPatch``: asks reality to return to a previously confirmed
  task state. It references a committed checkpoint and the values that
  were OBSERVED there — it never invents a new goal and never restores a
  storage snapshot (kernel invariant 6).

None of these carry platform selectors, storage keys, or app-internal
operation names — the same no-leak contract as ``ActionContract``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from taskvm.domain.contract import ActionContract
from taskvm.domain.errors import ValidationError
from taskvm.domain.intent import TaskIntent


@dataclass(frozen=True)
class VariableUpdate:
    """A new target value for one existing task variable."""

    semantic_key: str
    new_value: Any

    def __post_init__(self) -> None:
        if not self.semantic_key:
            raise ValidationError("VariableUpdate.semantic_key must be non-empty")


@dataclass(frozen=True)
class NodeContractOverride:
    """A LocalPatch-level adjustment of one not-yet-committed action
    node's contract (e.g. 'same node, but write 16:00 instead of 15:00').
    Cannot add, remove, or re-wire nodes — that would be a GoalPatch."""

    node_id: str
    contract: ActionContract

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValidationError("NodeContractOverride.node_id must be non-empty")


@dataclass(frozen=True)
class Patch:
    """Common envelope. ``correlation_id`` links the patch to the
    governance event / user gesture that produced it."""

    patch_id: str
    rationale: str = ""
    correlation_id: str = ""
    created_at: float = 0.0  # kernel fills this in

    def __post_init__(self) -> None:
        if not self.patch_id:
            raise ValidationError("Patch.patch_id must be non-empty")


@dataclass(frozen=True)
class LocalPatch(Patch):
    """Local adjustment: terminal goal and workflow topology unchanged."""

    variable_updates: tuple[VariableUpdate, ...] = ()
    node_overrides: tuple[NodeContractOverride, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "variable_updates", tuple(self.variable_updates))
        object.__setattr__(self, "node_overrides", tuple(self.node_overrides))
        if not self.variable_updates and not self.node_overrides:
            raise ValidationError("LocalPatch must change at least one thing")


@dataclass(frozen=True)
class GoalPatch(Patch):
    """Terminal-goal / scope / constraint / topology change.

    ``new_intent`` is None only when the terminal condition itself is
    unchanged but the future topology must be re-organised (still a
    GoalPatch). The kernel ALWAYS treats this as replan-signalling.
    """

    new_intent: TaskIntent | None = None


@dataclass(frozen=True)
class CompensationPatch(Patch):
    """Return to a previously confirmed task state.

    ``observed_before`` maps semantic keys to the values the requester
    believes were in effect at the target checkpoint. The kernel accepts
    the patch only if those values match what IT recorded at that
    checkpoint boundary — compensation is grounded in TaskVM's own
    observation history, never in an external oracle.
    """

    target_checkpoint_id: str = ""
    observed_before: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.target_checkpoint_id:
            raise ValidationError(
                "CompensationPatch needs a target_checkpoint_id")


@dataclass(frozen=True)
class CompensationEntry:
    """One variable reversion inside a compensation plan: undo the current
    believed value back to the value observed at the target checkpoint."""

    semantic_key: str
    from_value: Any
    to_value: Any


@dataclass(frozen=True)
class CompensationPlan:
    """The kernel's validated answer to a CompensationPatch — the list of
    reversions the runtime must carry out through the SAME real execution
    path as forward work (compensation ≠ snapshot restore)."""

    plan_id: str
    target_checkpoint_id: str
    entries: tuple[CompensationEntry, ...] = ()
    epoch: int = 0
    created_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))


def requires_replan(patch: Patch) -> bool:
    """The one-line classifier (handoff 02: '判定规则：是否改变 terminal
    success predicate / scope / workflow topology')."""
    return isinstance(patch, GoalPatch)
