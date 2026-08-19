"""The three governance patch classes (oracle export
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

from taskvm.domain.contract import ActionContract, Reversibility
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
    """Local adjustment: terminal goal and workflow topology unchanged.

    ``variable_updates`` is the SINGLE source of truth: the kernel
    deterministically retargets every not-yet-committed contract that
    references an updated key (topology, evidence, reversibility and risk
    class are structurally out of reach). There is deliberately NO
    node-override channel — a free-form contract swap could smuggle a
    GoalPatch into a LocalPatch (Wave-A.2 audit G4).
    """

    variable_updates: tuple[VariableUpdate, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "variable_updates", tuple(self.variable_updates))
        if not self.variable_updates:
            raise ValidationError("LocalPatch must update at least one variable")
        # determinism: the same target may not be updated twice in one patch
        keys = [u.semantic_key for u in self.variable_updates]
        if len(set(keys)) != len(keys):
            raise ValidationError(
                f"LocalPatch duplicate variable update keys: {keys}")


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

    Carries ONLY the target checkpoint. The 'before' values come from the
    kernel's OWN checkpoint record — the requester cannot supply, amend,
    or spoof them (an empty/partial caller-supplied history would be a
    vacuous-pass hole; eliminating the parameter eliminates the hole).
    """

    target_checkpoint_id: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.target_checkpoint_id:
            raise ValidationError(
                "CompensationPatch needs a target_checkpoint_id")


@dataclass(frozen=True)
class CompensationEntry:
    """One committed TaskVM action to undo (plan order = LIFO).

    Compensation undoes WHAT TASKVM ACTUALLY DID — it is derived from the
    kernel's own committed action history (before/after recorded at action
    time), never from a snapshot-diff of the world (Wave-A.2 audit G5):
    external drift is not TaskVM's to undo, and a variable that only
    appeared later still has its true pre-action 'before' on record.

    ``to_observed`` is the reality the runtime must restore (verified by
    fresh observation before the kernel accepts the result);
    ``to_desired`` is the task-layer value restored alongside.
    """

    node_id: str
    semantic_key: str
    from_observed: Any
    to_observed: Any
    to_desired: Any = None
    reversibility: Reversibility = Reversibility.REVERSIBLE


@dataclass(frozen=True)
class UncompensatableAction:
    """A committed post-checkpoint action that CANNOT be honestly undone
    (e.g. IRREVERSIBLE work like a sent message). It is reported, never
    disguised as a revertible value change (mental-model §3.5)."""

    node_id: str
    semantic_keys: tuple[str, ...] = ()
    reversibility: Reversibility = Reversibility.IRREVERSIBLE
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic_keys", tuple(self.semantic_keys))


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
    # committed post-checkpoint actions that cannot be honestly undone
    uncompensatable: tuple[UncompensatableAction, ...] = ()
    # True when the rollback crosses a GoalPatch/structure boundary: the
    # remaining future topology was planned for the abandoned goal, so the
    # Task Architect MUST recompose before execution continues.
    requires_recompose: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        object.__setattr__(self, "uncompensatable", tuple(self.uncompensatable))


def requires_replan(patch: Patch) -> bool:
    """The one-line classifier: whether the patch changes the terminal
    success predicate / scope / workflow topology')."""
    return isinstance(patch, GoalPatch)
