"""A-02 — stop during model inference: the returned ACT must never act.

The race (reproduced live before the fix, see the probe in the task
report): ``predict_action`` blocks on the provider → a public stop lands
→ the model then returns a LEGAL ACT → the pre-fix code walked straight
into ``start_action`` (the kernel's ``request_governance("stop")`` bumps
NO epoch — only pause does — so the kernel gate passed) and then
``substrate.act()`` wrote the GUI after the stop.

The ONLY acceptance invariant (re-prompt A-02): if a public stop arrives
while ``predict_action`` is in flight, then even when that inference
returns a legal ACT, ``substrate.act()`` must fire ZERO times.

Every test here is BARRIER-BASED (threading.Event handshakes): the stop
provably lands INSIDE the inference window, and the model's reply provably
arrives AFTER it. No sleep-based race tests.

Additional pinned properties:
  * the attempt carries lifecycle evidence (the kernel's
    GOVERNANCE_REQUESTED stop event; run() returns STOPPED; the handle
    honestly stays REQUESTED — never started, never partially executed);
  * the CUA provider request still lands EXACTLY ONE ledger row (1
    provider request = 1 row; execution disposition is NOT a ledger
    field — C-2);
  * pause/resume semantics are untouched (covered by
    tests/runtime/test_hot_governance.py, re-run alongside).
"""
from __future__ import annotations

import threading

from taskvm.domain.events import EventKind
from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode

from tests.runtime.conftest import (
    FakeLedger, FakeSubstrate, action_node, make_kernel, make_runtime,
    status_of, type_kv, var,
)


def _single_graph():
    return WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        action_node("a1", desired={"x": "A"}, parent_id="root"),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("a1", "root")),
    ))


class BarrierCUA:
    """A CUA whose inference is a two-event barrier: it signals
    ``entered`` when the provider call begins, then WAITS on ``release``
    before returning the scripted decision — the stop provably lands
    inside the inference window, deterministically.

    ``own_ledger`` makes it an A-13-style adapter that declares
    ``records_own_ledger`` and lands its own row (so the test can pin
    BOTH ledger ownership styles against the same invariant)."""

    def __init__(self, decision, ledger=None, *, own_ledger: bool = False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._decision = decision
        self._ledger = ledger
        self.records_own_ledger = own_ledger
        self.calls = 0

    def predict_action(self, *, goal, observation, labels=None,
                       attempt=1, model=None):
        self.calls += 1
        self.entered.set()           # the provider request is now in flight
        self.release.wait(timeout=10)  # the stop lands in THIS window
        if self.records_own_ledger and self._ledger is not None:
            # the adapter owns its row — landed on EVERY path (A-13)
            from taskvm.runtime.ports import ModelCallRecord, MODEL_ROLE_CUA
            self._ledger.record(ModelCallRecord(
                role=MODEL_ROLE_CUA, purpose="cua.predict_action",
                model=model or "", ok=True, attempt=attempt))
        return self._decision


def _stop_race(cua, stop_gesture):
    """Run the barrier race: inference enters → stop lands → release →
    the legal ACT returns. Returns (reason, substrate, kernel, runtime)."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    rt = make_runtime(k, sub, cua)
    result = {}

    def drive():
        result["reason"] = rt.run()

    t = threading.Thread(target=drive)
    t.start()
    assert cua.entered.wait(timeout=5), "inference never started"
    stop_gesture(rt, k)              # the public stop, INSIDE the window
    cua.release.set()                # the model now returns a LEGAL ACT
    t.join(timeout=10)
    return result.get("reason"), sub, k, rt


# ── the load-bearing invariant ─────────────────────────────────────────────
def test_stop_during_inference_never_acts():
    """predict blocked → request_stop() → release → legal ACT ⇒
    substrate.act == 0, run() == STOPPED, lifecycle evidence on the
    kernel, exactly ONE cua ledger row."""
    cua = BarrierCUA(type_kv("x", "A"))
    reason, sub, k, rt = _stop_race(
        cua, lambda rt, k: rt.request_stop())

    assert reason == "stopped"
    assert sub.act_log == []                 # ZERO GUI writes — the invariant
    assert sub.world["app"]["x"] == "x0"     # the world was not touched
    # lifecycle evidence: the stop landed on the kernel's governance log
    stops = [e for e in k.events()
             if e.kind is EventKind.GOVERNANCE_REQUESTED
             and e.payload.get("action") == "stop"]
    assert stops, "no GOVERNANCE_REQUESTED(stop) lifecycle evidence"
    # EXACTLY one governance event (single-owner path, A-02 invariant 2)
    gov = [e for e in k.events()
           if e.kind is EventKind.GOVERNANCE_REQUESTED]
    assert len(gov) == 1
    # the handle honestly never started (no partial execution)
    assert status_of(k, "a1").value == "ready"
    # 1 provider request = 1 ledger row (fake CUA: the runtime records it)
    assert cua.calls == 1
    assert len(_rows(rt)) == 1


def _rows(rt):
    """The ledger the RUNTIME actually recorded into (make_runtime's own
    FakeLedger instance)."""
    return rt._ledger.records


def test_stop_during_inference_adapter_owned_ledger_still_one_row():
    """Same race with an adapter that declares ``records_own_ledger`` (the
    production HttpCUAModel style): the adapter lands its row, the
    runtime adds NONE — still exactly one row for the request."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    ledger = FakeLedger()
    cua = BarrierCUA(type_kv("x", "A"), ledger, own_ledger=True)
    rt = make_runtime(k, sub, cua)
    result = {}
    t = threading.Thread(target=lambda: result.update(reason=rt.run()))
    t.start()
    assert cua.entered.wait(timeout=5)
    rt.request_stop()
    cua.release.set()
    t.join(timeout=10)

    assert result["reason"] == "stopped"
    assert sub.act_log == []
    assert ledger.total() == 1               # the adapter's row — and ONLY it
    assert len(_rows(rt)) == 0               # the runtime added none (A-13)


def test_external_kernel_stop_during_inference_never_acts():
    """The EXTERNAL stop path (composition wrote
    ``kernel.request_governance("stop")`` directly — no runtime flag was
    set and NO epoch was bumped): the ACT branch's governance-log check
    must still veto the gesture before substrate.act."""
    cua = BarrierCUA(type_kv("x", "A"))
    reason, sub, k, rt = _stop_race(
        cua, lambda rt, kk: kk.request_governance("stop", "external stop"))

    assert reason == "stopped"
    assert sub.act_log == []                 # ZERO GUI writes
    assert sub.world["app"]["x"] == "x0"
    assert any(e.kind is EventKind.GOVERNANCE_REQUESTED
               and e.payload.get("action") == "stop"
               for e in k.events())


def test_external_kernel_pause_during_inference_discards_via_epoch():
    """The EXTERNAL pause path bumps the epoch, so the kernel's own
    start_action gate discards the stale handle — ACTION_DISCARDED lands
    and nothing acts (the pre-existing §4 path, re-pinned here as the
    contrast to stop: pause is epoch-bumping, stop is not)."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = BarrierCUA(type_kv("x", "A"))
    rt = make_runtime(k, sub, cua)
    result = {}
    t = threading.Thread(target=lambda: result.update(reason=rt.run()))
    t.start()
    assert cua.entered.wait(timeout=5)
    k.request_governance("pause", "external pause")
    cua.release.set()
    t.join(timeout=10)

    assert result["reason"] == "paused"
    assert sub.act_log == []
    assert EventKind.ACTION_DISCARDED in [e.kind for e in k.events()]


# ── pause/resume semantics untouched ───────────────────────────────────────
def test_pause_during_inference_blocks_the_gesture_but_resume_works():
    """The same barrier race with request_pause: the in-flight prediction
    is discarded (0 acts), and a subsequent resume lets a FRESH runtime
    pass complete the node — pause/resume semantics are not broken by
    the A-02 re-gate."""
    from tests.runtime.conftest import ScriptedCUA

    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = BarrierCUA(type_kv("x", "A"))
    rt = make_runtime(k, sub, cua)
    result = {}
    t = threading.Thread(target=lambda: result.update(reason=rt.run()))
    t.start()
    assert cua.entered.wait(timeout=5)
    rt.request_pause()               # pause inside the inference window
    cua.release.set()
    t.join(timeout=10)

    assert result["reason"] == "paused"
    assert sub.act_log == []         # the paused-inference gesture never ran

    # resume: swap in a working CUA and finish the node normally
    from tests.runtime.conftest import DONE as _DONE
    rt._cua = ScriptedCUA([type_kv("x", "A"), _DONE])
    rt.request_resume()
    reason = rt.run()
    assert reason == "done"
    assert sub.world["app"]["x"] == "A"
    assert status_of(k, "a1").value == "committed"
