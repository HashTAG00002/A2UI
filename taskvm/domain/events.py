"""Events — the single source of truth for 'what happened'.

Every kernel mutation appends exactly one event to the session's EventLog.
Each event carries the session id, the domain revision and execution epoch
at emission time, a wall-clock timestamp, and a correlation id linking it
to the causing request/patch. Upper layers (projection SSE, runtime sync,
evaluation replay) must derive their views from this log — never from
side channels.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    # observation / state
    OBSERVATION_RECEIVED = "observation_received"
    STATE_UPDATED = "state_updated"
    # plan lifecycle
    PLAN_CREATED = "plan_created"
    PLAN_PATCHED = "plan_patched"
    # action lifecycle (epoch-stamped)
    ACTION_REQUESTED = "action_requested"
    ACTION_STARTED = "action_started"
    ACTION_FINISHED = "action_finished"
    ACTION_DISCARDED = "action_discarded"  # stale epoch / superseded result
    ACTION_REQUEUED = "action_requeued"    # failed node returned to READY
    # verification
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_FAILED = "verification_failed"
    # control-node advancement (barrier / terminal fan-in confirmations)
    NODE_COMMITTED = "node_committed"
    # checkpoints
    CHECKPOINT_COMMITTED = "checkpoint_committed"
    # governance
    GOVERNANCE_REQUESTED = "governance_requested"
    CONFLICT_DETECTED = "conflict_detected"
    CONFLICT_RESOLVED = "conflict_resolved"
    # compensation
    COMPENSATION_REQUESTED = "compensation_requested"
    COMPENSATION_APPLIED = "compensation_applied"    # COMPLETE rollback
    COMPENSATION_PARTIAL = "compensation_partial"    # PARTIAL: some entries stand
    COMPENSATION_FAILED = "compensation_failed"      # execution honestly failed
    COMPENSATION_DISCARDED = "compensation_discarded"  # stale epoch — NOT a failure
    # bounded loop protocol
    LOOP_ITERATION_STARTED = "loop_iteration_started"
    LOOP_ITERATION_EVALUATED = "loop_iteration_evaluated"


@dataclass(frozen=True)
class Event:
    """One immutable fact in the session's history."""

    event_id: str
    session_id: str
    kind: EventKind
    revision: int          # task-state revision at emission
    epoch: int             # execution epoch at emission
    timestamp: float
    correlation_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EventKind):
            object.__setattr__(self, "kind", EventKind(self.kind))
