"""CheckpointStore — single source of truth for committed checkpoints.

A checkpoint is a **TaskVM logical checkpoint**, NOT an app/storage
snapshot: it pins the exact event-log index, task-state revision,
execution epoch, the full TaskIntent, the task state's SEMANTIC
STRUCTURE (every variable's key/label/type/mutability — not merely the
values of variables that happen to still exist), both value planes, and
the set of workflow nodes committed at the boundary (invariant 5).

Restoring REALITY is never the kernel's job: only the runtime can do
that, through real compensation actions on the visible surface. The
record merely defines the verified logical target a compensation flow
aims at. No hidden-state restore exists anywhere in this design.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any

from taskvm.domain.errors import UnknownCheckpointError, ValidationError
from taskvm.domain.intent import TaskIntent


@dataclass(frozen=True)
class CheckpointRecord:
    """One committed logical-checkpoint boundary.

    ``observed`` / ``desired`` snapshot BOTH value planes of every task
    variable: compensation targets the observed plane (reality) and
    restores the desired plane (the task-layer intent over variables).
    ``intent`` is the full govern intent at the boundary; ``structure``
    maps semantic_key → {"label", "value_type", "mutability"} so a later
    rollback can restore the task state's semantic structure even if the
    current state no longer carries some of the variables (e.g. a
    GoalPatch/recompose replaced them).
    """

    checkpoint_id: str
    label: str
    state_revision: int        # TaskState.revision at the boundary
    event_index: int           # EventLog length at the boundary (exclusive end)
    epoch: int                 # execution epoch at the boundary
    intent: TaskIntent | None = None
    structure: dict[str, dict[str, str]] = field(default_factory=dict)
    observed: dict[str, Any] = field(default_factory=dict)
    desired: dict[str, Any] = field(default_factory=dict)
    committed_nodes: tuple[str, ...] = ()
    created_at: float = 0.0


class CheckpointStore:
    """Committed checkpoints for one session, in commit order."""

    def __init__(self) -> None:
        self._records: list[CheckpointRecord] = []
        self._lock = threading.RLock()

    def add(self, record: CheckpointRecord) -> CheckpointRecord:
        with self._lock:
            if any(r.checkpoint_id == record.checkpoint_id for r in self._records):
                raise ValidationError(
                    f"duplicate checkpoint_id {record.checkpoint_id!r}")
            self._records.append(copy.deepcopy(record))
            return copy.deepcopy(record)

    def get(self, checkpoint_id: str) -> CheckpointRecord:
        with self._lock:
            for r in self._records:
                if r.checkpoint_id == checkpoint_id:
                    return copy.deepcopy(r)
        raise UnknownCheckpointError(f"unknown checkpoint {checkpoint_id!r}")

    def all(self) -> list[CheckpointRecord]:
        with self._lock:
            return copy.deepcopy(self._records)

    def latest(self) -> CheckpointRecord | None:
        with self._lock:
            return copy.deepcopy(self._records[-1]) if self._records else None
