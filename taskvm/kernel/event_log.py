"""EventLog — the append-only audit/history stream of one session.

This is deliberately NOT an event-sourcing framework: the log is the
audit trail (what happened, in what order, at which revision/epoch), not
the state-recovery mechanism — stores hold the live state; nobody
rebuilds state by replaying events. Only the kernel appends, exactly one
event per accepted mutation. Readers get defensive copies. The index of
an event in the log is a durable boundary marker: CheckpointStore
references it to pin down exactly which facts a checkpoint covers
(invariant 5).
"""
from __future__ import annotations

import copy
import threading

from taskvm.domain.events import Event, EventKind


class EventLog:
    """In-memory append-only event log for one session."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._lock = threading.RLock()

    def append(self, event: Event) -> int:
        """Append one event; returns its durable index (0-based)."""
        with self._lock:
            self._events.append(copy.deepcopy(event))
            return len(self._events) - 1

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def all(self) -> list[Event]:
        with self._lock:
            return copy.deepcopy(self._events)

    def since(self, index: int) -> list[Event]:
        """Events with log index >= ``index`` (exclusive boundary replay)."""
        with self._lock:
            return copy.deepcopy(self._events[index:])

    def by_kind(self, kind: EventKind) -> list[Event]:
        with self._lock:
            return copy.deepcopy([e for e in self._events if e.kind is kind])

    def by_correlation(self, correlation_id: str) -> list[Event]:
        with self._lock:
            return copy.deepcopy(
                [e for e in self._events if e.correlation_id == correlation_id])
