"""taskvm.projection.events — typed kernel/runtime event → SSE adapter
(contract §6: SSE vocabulary is the frozen event-kind set; no free-form
strings).

A single ``sse_envelope(event)`` converts any ``taskvm.domain.events.Event``
or runtime-emit into a JSON-safe dict suitable for ``json.dumps`` +
``text/event-stream`` wire format. The mapping is total and frozen: every
``EventKind`` has an entry, and the ``sse_type`` field is stable for
frontend routing.
"""
from __future__ import annotations

import json
import time
from typing import Any, Mapping

from taskvm.domain.events import Event, EventKind

#: Frozen SSE type vocabulary (contract §6). Frontend routes on ``sse_type``;
#: the raw ``kind`` is kept in ``detail`` for inspection.
KERNEL_EVENT_SSE: dict[EventKind, str] = {
    EventKind.OBSERVATION_RECEIVED:    "observation",
    EventKind.STATE_UPDATED:           "state.updated",
    EventKind.PLAN_CREATED:            "plan.created",
    EventKind.PLAN_PATCHED:            "plan.patched",
    EventKind.ACTION_REQUESTED:        "action.requested",
    EventKind.ACTION_STARTED:          "action.started",
    EventKind.ACTION_FINISHED:         "action.finished",
    EventKind.ACTION_DISCARDED:        "action.discarded",
    EventKind.ACTION_REQUEUED:         "action.requeued",
    EventKind.VERIFICATION_PASSED:     "verification.passed",
    EventKind.VERIFICATION_FAILED:     "verification.failed",
    EventKind.NODE_COMMITTED:          "node.committed",
    EventKind.CHECKPOINT_COMMITTED:    "checkpoint.committed",
    EventKind.GOVERNANCE_REQUESTED:    "governance.requested",
    EventKind.CONFLICT_DETECTED:       "conflict.detected",
    EventKind.CONFLICT_RESOLVED:       "conflict.resolved",
    EventKind.COMPENSATION_REQUESTED:  "compensation.requested",
    EventKind.COMPENSATION_APPLIED:    "compensation.complete",
    EventKind.COMPENSATION_PARTIAL:    "compensation.partial",
    EventKind.COMPENSATION_FAILED:     "compensation.failed",
    EventKind.COMPENSATION_DISCARDED:  "compensation.discarded",
    EventKind.LOOP_ITERATION_STARTED:  "loop.iteration_started",
    EventKind.LOOP_ITERATION_EVALUATED: "loop.iteration_evaluated",
}

#: Runtime events (structurally typed) map to the same vocabulary space.
RUNTIME_EVENT_SSE: dict[str, str] = {
    "surface_observed":  "surface.observed",
    "surface_conflict":  "surface.conflict",
    "budget_exhausted":  "budget.exhausted",
    "budget_recovered":  "budget.recovered",
    "action_landed":     "action.landed",
    "artifact_captured":  "artifact.captured",
}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def sse_envelope(event: Any) -> dict[str, Any]:
    """Convert any kernel/runtime event into a JSON-safe SSE envelope.

    Envelope shape (contract §6.3)::

        {
          "sse_type": "action.started",     # frontend routes on this
          "event_id": "...",
          "epoch": 3,
          "revision": 7,
          "detail": { ... payload ... }
        }

    For kernel events ``event`` is ``taskvm.domain.events.Event``.
    For runtime events ``event`` is any object with ``kind`` (str or
    EventKind), ``epoch``, ``payload``, and optional ``event_id`` /
    ``correlation_id``.
    """
    raw_kind = getattr(event, "kind", None)
    kind_val = getattr(raw_kind, "value", raw_kind) if raw_kind else None

    # kernel events
    if isinstance(raw_kind, EventKind):
        sse_type = KERNEL_EVENT_SSE.get(raw_kind, "kernel.unknown")
        return {
            "sse_type": sse_type,
            "event_id": getattr(event, "event_id", ""),
            "session_id": getattr(event, "session_id", ""),
            "epoch": getattr(event, "epoch", 0),
            "revision": getattr(event, "revision", 0),
            "correlation_id": getattr(event, "correlation_id", ""),
            "detail": _jsonable(getattr(event, "payload", {})),
            "ts": getattr(event, "timestamp", time.time()),
        }

    # runtime events (structurally typed)
    sse_type = RUNTIME_EVENT_SSE.get(str(kind_val or ""), "runtime.unknown")
    return {
        "sse_type": sse_type,
        "event_id": getattr(event, "event_id", ""),
        "session_id": getattr(event, "session_id", ""),
        "epoch": getattr(event, "epoch", 0),
        "revision": getattr(event, "revision", 0),
        "correlation_id": getattr(event, "correlation_id", ""),
        "detail": _jsonable(getattr(event, "payload", {})),
        "ts": getattr(event, "timestamp", time.time()),
    }


def format_sse(envelope: dict[str, Any]) -> str:
    """Format one envelope as a ``text/event-stream`` frame."""
    return f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n"
