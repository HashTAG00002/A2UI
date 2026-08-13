"""CheckpointStore — single source of truth for committed checkpoints.

A checkpoint is a *verified boundary*: it pins the exact event-log index,
task-state revision, execution epoch, variable snapshot, and the set of
workflow nodes that were committed at the moment of commitment
(invariant 5). Compensation flows resolve against these records — never
against storage snapshots.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any

from taskvm.domain.errors import UnknownCheckpointError, ValidationError


@dataclass(frozen=True)
class CheckpointRecord:
    """One committed checkpoint boundary."""

    checkpoint_id: str
    label: str
    state_revision: int        # TaskState.revision at the boundary
    event_index: int           # EventLog length at the boundary (exclusive end)
    epoch: int                 # execution epoch at the boundary
    variables: dict[str, Any] = field(default_factory=dict)   # observed values
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
