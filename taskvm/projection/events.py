"""taskvm.projection.events — typed kernel/runtime event → SSE adapter
(contract §7: SSE vocabulary is the frozen event-kind set; no free-form
strings — D-F3 repair, RFC-D1 revision).

A single ``sse_envelope(event)`` converts any ``taskvm.domain.events.Event``
or ``RuntimeEvent`` into a JSON-safe dict suitable for ``json.dumps`` +
``text/event-stream`` wire format. The mapping is total and frozen: every
``EventKind`` and every ``RuntimeEventKind`` value has an entry, the two
transport-level frame types (initial ``snapshot`` / ``governance.applied``
command ack) are registered here as first-class vocabulary, and the union
is exposed as ``SSE_TYPE_VOCABULARY`` for the totality assertion in
``tests/projection/test_events.py``.
"""
from __future__ import annotations

import json
import time
from typing import Any, Mapping

from taskvm.domain.events import Event, EventKind

#: Frozen SSE type vocabulary — kernel events (contract §7, RFC-D1).
#: Frontend routes on ``sse_type``; the raw ``kind`` is kept in ``detail``
#: for inspection.
KERNEL_EVENT_SSE: dict[EventKind, str] = {
    EventKind.OBSERVATION_RECEIVED:    "observation.received",
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

#: Runtime events (``taskvm.runtime.ports.RuntimeEventKind`` VALUE strings →
#: SSE types). TOTAL over the runtime's own typed kinds — the old table
#: carried three keys that were never RuntimeEventKind values (dead
#: mappings) and missed five real ones (D-F3 totality repair).
RUNTIME_EVENT_SSE: dict[str, str] = {
    "action_observed":       "action.observed",
    "action_landed":         "action.landed",
    "structure_invalidated": "structure.invalidated",
    "surface_conflict":      "surface.conflict",
    "compensation_entry":    "compensation.entry",
    "budget_exhausted":      "budget.exhausted",
    "loop_tick":             "loop.tick",
    "node_failed":           "node.failed",
}

#: Transport-level frame types the SSE endpoint itself emits (not derived
#: from any event object): the initial snapshot a subscriber receives on
#: connect, and the acknowledgment pushed when a governance command lands.
#: Registered here (D-F3) so no free-form ``sse_type`` ever leaves the app.
TRANSPORT_EVENT_SSE: dict[str, str] = {
    "snapshot":          "snapshot",
    "governance.applied": "governance.applied",
}

#: The frozen vocabulary — every ``sse_type`` the projection may emit.
SSE_TYPE_VOCABULARY: frozenset[str] = frozenset(
    set(KERNEL_EVENT_SSE.values())
    | set(RUNTIME_EVENT_SSE.values())
    | set(TRANSPORT_EVENT_SSE.values()))


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
    For runtime events ``event`` is a ``taskvm.runtime.ports.RuntimeEvent``
    (``kind`` is a ``RuntimeEventKind``; its VALUE string is the lookup key).
    An unmapped kind raises ``ValueError`` — the mapping is total by
    construction, so a raise means a new kind was added without registering
    its SSE type (the free-form-string regression D-F3 closed).
    """
    raw_kind = getattr(event, "kind", None)
    kind_val = getattr(raw_kind, "value", raw_kind) if raw_kind else None

    # kernel events
    if isinstance(raw_kind, EventKind):
        sse_type = KERNEL_EVENT_SSE.get(raw_kind)
        if sse_type is None:
            raise ValueError(
                f"EventKind.{raw_kind.name} has no registered SSE type — "
                "add it to KERNEL_EVENT_SSE (contract §7 freeze)")
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

    # runtime events (structurally typed; RuntimeEventKind is a str enum)
    sse_type = RUNTIME_EVENT_SSE.get(str(kind_val or ""))
    if sse_type is None:
        raise ValueError(
            f"runtime event kind {kind_val!r} has no registered SSE type — "
            "add it to RUNTIME_EVENT_SSE (contract §7 freeze)")
    return {
        "sse_type": sse_type,
        "event_id": getattr(event, "event_id", ""),
        "session_id": getattr(event, "session_id", ""),
        "epoch": getattr(event, "epoch", 0),
        "revision": getattr(event, "revision", 0),
        "correlation_id": getattr(event, "correlation_id", ""),
        "detail": _jsonable(_runtime_detail(event)),
        "ts": getattr(event, "timestamp", time.time()),
    }


def _runtime_detail(event: Any) -> Any:
    """RuntimeEvents carry ``payload`` + display fields; surface the
    human-relevant ones (never internal ids — the scrubber discipline is
    upstream, in the runtime)."""
    payload = getattr(event, "payload", None)
    if payload:
        return payload
    detail = getattr(event, "detail", "")
    fields: dict[str, Any] = {"detail": detail}
    for attr in ("node_id", "surface_id", "artifact_ref"):
        v = getattr(event, attr, "")
        if v:
            fields[attr] = v
    return fields


def format_sse(envelope: dict[str, Any]) -> str:
    """Format one envelope as a ``text/event-stream`` frame.

    The envelope's ``sse_type`` MUST be inside ``SSE_TYPE_VOCABULARY``
    (D-F3 assertion lives at the single emission chokepoint)."""
    sse_type = envelope.get("sse_type")
    if sse_type not in SSE_TYPE_VOCABULARY:
        raise ValueError(
            f"sse_type {sse_type!r} is not in the frozen SSE vocabulary "
            "(contract §7) — register it in taskvm/projection/events.py")
    return f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n"
