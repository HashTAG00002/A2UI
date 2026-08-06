"""trace_capture — LIVE observation capture (W2+ stub).

W1 hand-authors the trace (replay-mode = frozen compiler input, see
``benchmark/fixtures.py::TraceFixture``). This module is the W2 live-capture
path: drive the apps with the compute-use model, capture screenshot + DOM +
a11y per step, and emit a ``TraceFixture`` for the compiler. Not built in W1.
"""
from __future__ import annotations


def capture_trace(*args, **kwargs):
    raise NotImplementedError("trace_capture is W2+; W1 uses hand-authored TraceFixture.")
