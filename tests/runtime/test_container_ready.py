"""RFC-container-autocommit — runtime side of the GATE-G0 r13 fix.

The autonomy loop's ready pull must use the SAME kernel readiness rule
the store applies when marking READY (``taskvm.kernel.schedulable_nodes``:
the domain rule + the own-parent-container relaxation). Before the RFC,
run() re-derived readiness from the domain's strict rule and returned
NO_READY with the lanes forever unscheduled — the r13 failure shape
(world never changed, WORLD_WITNESS_MISSING x2).
"""
from __future__ import annotations

from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode

from tests.runtime.conftest import (
    DONE, FakeSubstrate, ScriptedCUA, action_node, make_kernel,
    make_runtime, status_of, type_kv, var,
)


def _r13_graph(barrier_on_container=False):
    """checkpoint -> fan-out (two lanes, each 'after' the checkpoint AND
    the container itself) -> barrier -> terminal — the distilled r13 call
    #003 topology."""
    barrier_deps = ("fan",) if barrier_on_container else ("lane_a", "lane_b")
    return WorkflowGraph(nodes=(
        WorkflowNode("cp", NodeKind.CHECKPOINT, "互动前检查点"),
        WorkflowNode("fan", NodeKind.FAN_OUT, "并行互动操作"),
        action_node("lane_a", desired={"liked": "true"}, parent_id="fan",
                    depends_on=("cp", "fan")),
        action_node("lane_b", desired={"marked": "true"}, parent_id="fan",
                    depends_on=("cp", "fan")),
        WorkflowNode("bar", NodeKind.BARRIER, "等待点赞与收藏完成",
                     depends_on=barrier_deps),
        WorkflowNode("term", NodeKind.TERMINAL, "任务完成",
                     depends_on=("bar",)),
    ))


def _drive(graph):
    k = make_kernel([var("liked", "false", "true"),
                     var("marked", "false", "true")], graph)
    sub = FakeSubstrate({"app": {"liked": "false", "marked": "false"}})
    cua = ScriptedCUA([type_kv("liked", "true"), DONE,
                       type_kv("marked", "true"), DONE])
    rt = make_runtime(k, sub, cua)
    return rt.run(), k, sub


def test_runtime_runs_the_r13_lane_after_own_container_shape():
    """The exact r13 topology (barrier fans in the lanes): the autonomy
    loop drives BOTH lanes, the container auto-commits, the barrier and
    terminal fire, and the substrate world actually changed — the witness
    r13 was missing."""
    reason, k, sub = _drive(_r13_graph(barrier_on_container=False))

    assert reason == "done"
    assert status_of(k, "lane_a").value == "committed"
    assert status_of(k, "lane_b").value == "committed"
    assert status_of(k, "fan").value == "committed"    # auto-commit
    assert status_of(k, "bar").value == "committed"
    assert status_of(k, "term").value == "committed"
    assert sub.world["app"]["liked"] == "true"
    assert sub.world["app"]["marked"] == "true"


def test_runtime_barrier_after_container():
    """The barrier may fan in the CONTAINER itself (prompt-blessed form):
    it fires exactly when the container auto-commits after both lanes."""
    reason, k, sub = _drive(_r13_graph(barrier_on_container=True))

    assert reason == "done"
    assert status_of(k, "bar").value == "committed"
    assert status_of(k, "term").value == "committed"
    assert sub.world["app"]["liked"] == "true"
    assert sub.world["app"]["marked"] == "true"
