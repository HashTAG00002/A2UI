"""Control-flow contract tests — runtime.md §10 (fan-out/barrier) + §11
(bounded loop), the two §14 rows the E-F1 audit found untested.

These pin the RUNTIME's behaviour when it drives the kernel's frozen
control primitives (the kernel-side landing semantics are already pinned
in tests/kernel/; what was missing is the runtime driving them):

* BOUNDED_LOOP — begin → body ACTION commits → evaluate(terminated=False)
  → next iteration → evaluate(terminated=True) → loop COMMITTED; and the
  max_iterations guard: the loop FAILS honestly (no self-retry, no third
  begin) and the runtime stops with no ready work.
* BARRIER — the fan-in gate: with only one required lane verified the
  barrier is not even READY (kernel snapshot) and is never advanced (no
  NODE_COMMITTED event); once every required lane is verified the runtime
  advances it and the plan finishes.
* Lane independence — one lane failing never rewrites a sibling lane's
  already-COMMITTED status, and the verified lane's write survives in the
  visible world.

All assertions live on BOTH planes: the kernel snapshot (statuses +
kernel events) and the runtime's own event stream.
"""
from __future__ import annotations

from taskvm.domain.events import EventKind
from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode
from taskvm.runtime import CUADecision, CUADecisionKind, RuntimeEventKind
# naming: autonomy.DONE is the run() stop-reason string "done", while the
# conftest DONE is the scripted CUA decision — alias both to avoid the clash
from taskvm.runtime.autonomy import DONE as RUN_DONE, NO_READY

from tests.runtime.conftest import (
    DONE as CUA_DONE, FakeSubstrate, ScriptedCUA, action_node, make_kernel,
    make_runtime, status_of, type_kv, var,
)


def _loop_graph(max_iterations: int) -> WorkflowGraph:
    """root(sequence) → lp(bounded loop, body=poll) → term.

    The body contract is idempotent (``tick=1`` passes every iteration);
    the LOOP predicate reads the visible ``synced`` flag — iteration 2's
    gesture flips it, so termination is visible-state-driven (§11), not
    a script trick the runtime could "know".
    """
    return WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        WorkflowNode("lp", NodeKind.BOUNDED_LOOP, "sync loop",
                     parent_id="root",
                     termination_predicate="synced == yes",
                     max_iterations=max_iterations),
        action_node("poll", desired={"tick": "1"}, parent_id="lp"),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("lp",)),
    ))


def _fanout_graph() -> WorkflowGraph:
    """fo(fan-out: lanes l1, l2) → b1(barrier) → term.

    Lanes are independent siblings (no lane→lane edge — the domain
    validator enforces it); the barrier fans in both lanes.
    """
    return WorkflowGraph(nodes=(
        WorkflowNode("fo", NodeKind.FAN_OUT, "fan-out"),
        action_node("l1", desired={"p": "1"}, parent_id="fo"),
        action_node("l2", desired={"q": "1"}, parent_id="fo"),
        WorkflowNode("b1", NodeKind.BARRIER, "join",
                     depends_on=("l1", "l2")),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("b1",)),
    ))


def _loop_vars():
    return [var("tick", "0", "1"), var("synced", "no", "yes")]


def _loop_world():
    return {"app": {"tick": "0", "synced": "no"}}


def _kernel_events(kernel, kind: EventKind):
    return [e for e in kernel.events() if e.kind is kind]


# ── §11 / §14: Bounded Loop ────────────────────────────────────────────────
def test_bounded_loop_two_iterations_then_committed():
    """begin → body commits → evaluate(False) → iteration 2 → body commits
    → evaluate(True) → loop COMMITTED; terminal then closes the plan."""
    k = make_kernel(_loop_vars(), _loop_graph(max_iterations=3))
    sub = FakeSubstrate(_loop_world())
    # iteration 1: type tick=1, DONE; iteration 2: type synced=yes, DONE
    cua = ScriptedCUA([type_kv("tick", "1"), CUA_DONE,
                       type_kv("synced", "yes"), CUA_DONE])
    rt = make_runtime(k, sub, cua)

    assert rt.run() == RUN_DONE

    # kernel snapshot plane
    assert status_of(k, "lp").value == "committed"
    assert status_of(k, "poll").value == "committed"
    assert status_of(k, "term").value == "committed"
    # the loop committed via the visible predicate, not a shortcut
    assert sub.world["app"] == {"tick": "1", "synced": "yes"}

    # kernel event plane: exactly two iterations, continue → committed
    started = _kernel_events(k, EventKind.LOOP_ITERATION_STARTED)
    assert [e.payload["iteration"] for e in started] == [1, 2]
    evaluated = _kernel_events(k, EventKind.LOOP_ITERATION_EVALUATED)
    assert [e.payload["outcome"] for e in evaluated] == [
        "continue", "committed"]

    # runtime event plane: LOOP_TICK carries each termination decision
    ticks = [e for e in rt.runtime_events()
             if e.kind is RuntimeEventKind.LOOP_TICK]
    assert [t.detail for t in ticks] == ["terminated=False", "terminated=True"]

    # determinism pin: one gesture + one DONE per iteration, nothing else
    assert len(cua.calls) == 4


def test_bounded_loop_max_iterations_fails_honestly():
    """The predicate never becomes true → the max guard FAILS the loop
    with reason=max_iterations_exceeded; the runtime never re-begins it
    (no third begin, no self-retry) and stops with no ready work."""
    k = make_kernel(_loop_vars(), _loop_graph(max_iterations=2))
    sub = FakeSubstrate(_loop_world())
    # the CUA refreshes tick but never flips `synced` → never terminated
    cua = ScriptedCUA([type_kv("tick", "1"), CUA_DONE,
                       type_kv("tick", "1"), CUA_DONE])
    rt = make_runtime(k, sub, cua)

    assert rt.run() == NO_READY

    # kernel snapshot plane: FAILED loop, honest downstream freeze
    assert status_of(k, "lp").value == "failed"
    assert status_of(k, "poll").value == "committed"   # last pass did land
    assert status_of(k, "term").value == "pending"

    # kernel event plane: exactly max_iterations begins — never a 3rd
    started = _kernel_events(k, EventKind.LOOP_ITERATION_STARTED)
    assert [e.payload["iteration"] for e in started] == [1, 2]
    evaluated = _kernel_events(k, EventKind.LOOP_ITERATION_EVALUATED)
    assert [e.payload["outcome"] for e in evaluated] == [
        "continue", "failed"]
    last = evaluated[-1].payload
    assert last["reason"] == "max_iterations_exceeded"

    # runtime event plane: both ticks honestly report not-terminated
    ticks = [e for e in rt.runtime_events()
             if e.kind is RuntimeEventKind.LOOP_TICK]
    assert [t.detail for t in ticks] == ["terminated=False"] * 2

    # re-pulsing the runtime must NOT resurrect the maxed loop
    assert rt.run() == NO_READY
    assert len(_kernel_events(k, EventKind.LOOP_ITERATION_STARTED)) == 2


# ── §10 / §14: barrier gate ────────────────────────────────────────────────
def test_barrier_waits_for_all_required_lanes():
    """With only lane-A verified the barrier is not advanced (not even
    READY); once both lanes are verified the runtime advances it and the
    plan completes (serial fan-out: one lane at a time, runtime.md §10)."""
    k = make_kernel([var("p", "p0", "1"), var("q", "q0", "1")],
                    _fanout_graph())
    sub = FakeSubstrate({"app": {"p": "p0", "q": "q0"}})
    cua = ScriptedCUA()
    rt = make_runtime(k, sub, cua)

    # lane-A commits; lane-B has not run yet
    cua.script = [type_kv("p", "1"), CUA_DONE]
    rt.run(step_budget=1)
    assert status_of(k, "l1").value == "committed"
    assert status_of(k, "l2").value == "ready"        # not started yet

    # the barrier is gated: not even READY, and never advanced
    assert status_of(k, "b1").value == "pending"
    committed = [e.payload["node_id"] for e in _kernel_events(
        k, EventKind.NODE_COMMITTED)]
    assert "b1" not in committed

    # lane-B commits → the barrier becomes READY and the runtime passes it
    cua.script = [type_kv("q", "1"), CUA_DONE]
    assert rt.run() == RUN_DONE
    assert status_of(k, "l2").value == "committed"
    assert status_of(k, "b1").value == "committed"
    assert status_of(k, "term").value == "committed"
    committed = [e.payload["node_id"] for e in _kernel_events(
        k, EventKind.NODE_COMMITTED)]
    assert committed.count("b1") == 1                 # advanced exactly once


# ── §10 / §14: lane independence ───────────────────────────────────────────
def test_lane_failure_does_not_destroy_committed_sibling_lane():
    """Lane-B's ACTION fails (CUA reports fail) — lane-A's COMMITTED
    status and its verified write in the visible world survive; the
    barrier stays gated and the runtime stops honestly instead of
    hot-retrying the failed lane (runtime.md §10)."""
    k = make_kernel([var("p", "p0", "1"), var("q", "q0", "1")],
                    _fanout_graph())
    sub = FakeSubstrate({"app": {"p": "p0", "q": "q0"}})
    cua = ScriptedCUA([
        type_kv("p", "1"), CUA_DONE,                   # lane-A: commits
        CUADecision(kind=CUADecisionKind.FAIL,
                    reason="lane B cannot proceed"),
    ])
    rt = make_runtime(k, sub, cua)

    assert rt.run() == NO_READY

    # kernel snapshot plane: sibling lane untouched, failed lane honest
    assert status_of(k, "l1").value == "committed"
    assert status_of(k, "l2").value == "failed"
    assert status_of(k, "b1").value == "pending"
    assert status_of(k, "term").value == "pending"

    # the verified lane's write survived in the visible world
    assert sub.world["app"]["p"] == "1"
    assert sub.world["app"]["q"] == "q0"

    # kernel event plane: lane-B's failure verdict landed honestly
    failed = [e.payload["node_id"] for e in _kernel_events(
        k, EventKind.VERIFICATION_FAILED)]
    assert "l2" in failed
    # no hot retry: the failed lane was never requeued
    assert not _kernel_events(k, EventKind.ACTION_REQUEUED)

    # runtime event plane: the failure was published for projection
    node_failed = [e for e in rt.runtime_events()
                   if e.kind is RuntimeEventKind.NODE_FAILED]
    assert any(e.node_id == "l2" for e in node_failed)
    assert not any(e.node_id == "l1" for e in node_failed)
