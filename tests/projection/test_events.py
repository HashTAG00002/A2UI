"""Event adapter tests (contract §6/§12): kernel EventKind + RuntimeEventKind →
SSE vocabulary mapping is total and typed.
"""
from __future__ import annotations

import json
import time

import pytest

from taskvm.domain import EventKind
from taskvm.projection.events import (
    KERNEL_EVENT_SSE,
    RUNTIME_EVENT_SSE,
    format_sse,
    sse_envelope,
)


# ── KERNEL_EVENT_SSE mapping is total ─────────────────────────────────────

class TestKernelEventSSEMapping:
    def test_all_event_kinds_mapped(self):
        """Every EventKind enum member must have an SSE type."""
        from taskvm.domain import EventKind
        for kind in EventKind:
            assert kind in KERNEL_EVENT_SSE, (
                f"EventKind.{kind.name} missing from KERNEL_EVENT_SSE")

    def test_sse_type_is_string(self):
        for kind, sse_type in KERNEL_EVENT_SSE.items():
            assert isinstance(sse_type, str)
            assert sse_type, f"empty sse_type for {kind}"

    def test_sse_types_are_unique(self):
        """No two EventKinds map to the same SSE type (frontend routing
        must be unambiguous)."""
        values = list(KERNEL_EVENT_SSE.values())
        assert len(values) == len(set(values)), (
            f"duplicate SSE types: {values}")

    def test_sse_types_use_dot_notation(self):
        """SSE types use domain.action format (contract §6)."""
        for sse_type in KERNEL_EVENT_SSE.values():
            assert "." in sse_type, (
                f"SSE type '{sse_type}' lacks dot notation")


# ── RUNTIME_EVENT_SSE mapping ─────────────────────────────────────────────

class TestRuntimeEventSSEMapping:
    def test_runtime_sse_types_are_unique(self):
        values = list(RUNTIME_EVENT_SSE.values())
        assert len(values) == len(set(values))

    def test_runtime_sse_types_use_dot_notation(self):
        for sse_type in RUNTIME_EVENT_SSE.values():
            assert "." in sse_type


# ── sse_envelope for kernel events ────────────────────────────────────────

class TestSSEEnvelopeKernel:
    def _make_event(self, kind=EventKind.STATE_UPDATED, epoch=1,
                    revision=2, payload=None, event_id="evt1",
                    correlation_id="", session_id="s1"):
        from taskvm.domain import Event
        return Event(
            event_id=event_id,
            session_id=session_id,
            kind=kind,
            epoch=epoch,
            revision=revision,
            timestamp=time.time(),
            payload=payload or {"key": "value"},
            correlation_id=correlation_id,
        )

    def test_envelope_has_sse_type(self):
        ev = self._make_event()
        env = sse_envelope(ev)
        assert env["sse_type"] == KERNEL_EVENT_SSE[EventKind.STATE_UPDATED]

    def test_envelope_has_event_id(self):
        ev = self._make_event()
        env = sse_envelope(ev)
        assert env["event_id"] == "evt1"

    def test_envelope_has_epoch(self):
        ev = self._make_event(epoch=5)
        env = sse_envelope(ev)
        assert env["epoch"] == 5

    def test_envelope_has_revision(self):
        ev = self._make_event(revision=3)
        env = sse_envelope(ev)
        assert env["revision"] == 3

    def test_envelope_has_correlation_id(self):
        ev = self._make_event(correlation_id="c1")
        env = sse_envelope(ev)
        assert env["correlation_id"] == "c1"

    def test_envelope_payload_in_detail(self):
        ev = self._make_event(payload={"node_id": "n1"})
        env = sse_envelope(ev)
        assert env["detail"]["node_id"] == "n1"

    def test_envelope_is_json_serializable(self):
        ev = self._make_event()
        env = sse_envelope(ev)
        s = json.dumps(env)
        assert isinstance(s, str)
        parsed = json.loads(s)
        assert parsed["sse_type"] == "state.updated"


# ── sse_envelope for runtime events ───────────────────────────────────────

class TestSSEEnvelopeRuntime:
    def _make_runtime_event(self, kind="surface_observed", epoch=1,
                            payload=None):
        class FakeEvent:
            def __init__(self, k, e, p):
                self.kind = type("K", (), {"value": k})()
                self.epoch = e
                self.payload = p or {}
                self.event_id = "rt1"
                self.correlation_id = ""
                self.session_id = "s1"
        return FakeEvent(kind, epoch, payload)

    def test_runtime_envelope_sse_type(self):
        ev = self._make_runtime_event("surface_observed")
        env = sse_envelope(ev)
        assert env["sse_type"] == "surface.observed"

    def test_runtime_unknown_kind(self):
        ev = self._make_runtime_event("nonexistent_kind")
        env = sse_envelope(ev)
        assert env["sse_type"] == "runtime.unknown"

    def test_runtime_envelope_is_json_serializable(self):
        ev = self._make_runtime_event("action_landed",
                                      payload={"node_id": "n1"})
        env = sse_envelope(ev)
        s = json.dumps(env)
        assert isinstance(s, str)


# ── format_sse ────────────────────────────────────────────────────────────

class TestFormatSSE:
    def test_format_produces_valid_sse_frame(self):
        env = {"sse_type": "state.updated", "detail": {"k": "v"}}
        frame = format_sse(env)
        assert frame.startswith("data: ")
        assert frame.endswith("\n\n")
        payload = frame[len("data: "):].rstrip("\n\n")
        parsed = json.loads(payload)
        assert parsed["sse_type"] == "state.updated"

    def test_format_handles_unicode(self):
        env = {"sse_type": "state.updated", "detail": {"label": "发布日期"}}
        frame = format_sse(env)
        assert "发布日期" in frame  # ensure_ascii=False
