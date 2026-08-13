"""TaskSessionStore — the single source of truth for:

    current TaskState (variables + intent)
    current execution epoch / generation

The kernel is the only writer. Reads are defensive deep copies (domain
objects are frozen, but ``value: Any`` payloads may hold mutable
structures — invariant 7).
"""
from __future__ import annotations

import copy
import threading
from dataclasses import replace

from taskvm.domain.errors import RevisionConflictError
from taskvm.domain.intent import TaskIntent
from taskvm.domain.state import TaskState


class TaskSessionStore:
    """One task session's state head + epoch counter."""

    def __init__(self, session_id: str, intent: TaskIntent) -> None:
        if not session_id:
            raise ValueError("session_id must be non-empty")
        self._session_id = session_id
        self._state = TaskState(intent=intent, variables=(), revision=0)
        self._epoch = 0
        self._lock = threading.RLock()

    @property
    def session_id(self) -> str:
        return self._session_id

    # ── task state (revision-monotonic; invariant 1) ────────────────────
    def task_state(self) -> TaskState:
        with self._lock:
            return copy.deepcopy(self._state)

    def set_task_state(self, state: TaskState) -> TaskState:
        """Install a new state head. The given state's revision is ignored;
        the store assigns ``current + 1`` so revisions are monotonic by
        construction. Returns the installed (re-stamped) state."""
        with self._lock:
            stamped = replace(state, revision=self._state.revision + 1)
            self._state = stamped
            return copy.deepcopy(stamped)

    def check_revision(self, state: TaskState) -> None:
        """Guard for callers that pass explicit revisions (migration path)."""
        with self._lock:
            if state.revision <= self._state.revision:
                raise RevisionConflictError(
                    f"state revision {state.revision} not > current "
                    f"{self._state.revision}")

    # ── intent (changed only via GoalPatch) ──────────────────────────────
    def set_intent(self, intent: TaskIntent) -> TaskState:
        with self._lock:
            self._state = replace(
                self._state, intent=intent, revision=self._state.revision + 1)
            return copy.deepcopy(self._state)

    # ── execution epoch / generation ─────────────────────────────────────
    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def bump_epoch(self) -> int:
        """Start a new execution generation. Every in-flight action from the
        old generation becomes stale (its late result must be discarded —
        invariant 4)."""
        with self._lock:
            self._epoch += 1
            return self._epoch
