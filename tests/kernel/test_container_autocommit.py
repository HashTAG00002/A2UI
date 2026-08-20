"""RFC-container-autocommit (GATE-G0 r13 postmortem / owner ruling
2026-08-20): the prompt-sanctioned plan shapes that name a CONTAINER in
'after' must be executable.

The r13 failure (eval_results/audit_gate_g0_r13_postmortem_20260820.json,
plan evidence call_archive/call_003_task_architect.txt): the architect
repair rewired BOTH fan-out lanes to after=["互动前检查点","并行互动操作"]
— their OWN container. The domain's ready_nodes requires every depends_on
entry COMMITTED, and a container auto-commits (commit 0ce1449) only once
every child has committed: a SEMANTIC cycle the graph's acyclicity check
cannot see (membership is not a depends_on edge of the container). The
lanes were permanently not-ready, the barrier/terminal never fired, run()
returned NO_READY with the world unchanged — WORLD_WITNESS_MISSING x2.

The kernel fix: ``taskvm.kernel.schedulable_nodes`` — the domain rule plus
ONE relaxation (a depends_on entry naming the node's OWN parent container,
SEQUENCE/FAN_OUT, is satisfied once the container is SCHEDULED). The
runtime's ready pull uses the same rule (tests/runtime/test_container_ready.py).

Regression lock (owner ruling): flat chains / single chains schedule
BYTE-IDENTICALLY — schedulable_nodes(g, s) == g.ready_nodes(s) for EVERY
status map of any graph with no own-parent 'after' entry; the r11/r12 plan
shapes must pass exactly as before (the full tests/ + taskvm_bench/ suites
are the behavioural half of the lock).
"""
from itertools import product

import pytest

from taskvm.domain import (
    ActionContract, NodeKind, NodeStatus, ObservedValue, TaskIntent,
    TaskVariable, VerificationResult, WorkflowGraph, WorkflowNode,
)
from taskvm.kernel import TaskVMKernel, schedulable_nodes

_ALL_STATUSES = tuple(NodeStatus)


def _contract(cid, key, value):
    return ActionContract(contract_id=cid,
                          semantic_goal=f"set {key} to {value}",
                          desired_state={key: value},
                          completion_condition=f"{key} visibly shows {value}")


def _kernel(graph):
    k = TaskVMKernel(session_id="rfc-ca", intent=TaskIntent(
        goal="容器依赖语义回归（RFC-container-autocommit）"))
    k.init_task_state((
        TaskVariable(semantic_key="liked", label="点赞",
                     observed="false", desired="true", value_type="string"),
        TaskVariable(semantic_key="marked", label="收藏",
                     observed="false", desired="true", value_type="string"),
    ))
    k.set_plan(graph)
    return k


def _verify(k, node_id, passed, h=None, detail=""):
    k.land_verification(VerificationResult(
        node_id=node_id, epoch=k.epoch, passed=passed,
        action_id=None if h is None else h["action_id"], detail=detail))


def _run_action(k, node_id, key, value):
    h = k.request_action(node_id)
    k.start_action(h["action_id"])
    assert k.finish_action(h["action_id"], observations=[
        ObservedValue(semantic_key=key, value=value)])
    _verify(k, node_id, True, h, detail="observed match")


# ── the r13 minimal repro ──────────────────────────────────────────────────
def _r13_graph(barrier_on_container=True):
    """checkpoint -> fan-out with two lanes each naming their OWN container
    in 'after' -> barrier -> terminal (the distilled r13 call #003 shape;
    ``barrier_on_container`` toggles the barrier's fan-in form — the r13
    plan used the lanes, the prompt blesses both)."""
    barrier_deps = ("fan",) if barrier_on_container else ("lane_a", "lane_b")
    return WorkflowGraph(nodes=(
        WorkflowNode(node_id="cp", kind=NodeKind.CHECKPOINT,
                     label="互动前检查点"),
        WorkflowNode(node_id="fan", kind=NodeKind.FAN_OUT,
                     label="并行互动操作"),
        WorkflowNode(node_id="lane_a", kind=NodeKind.ACTION,
                     label="点赞匹配帖子", parent_id="fan",
                     depends_on=("cp", "fan"),
                     contract=_contract("c_a", "liked", "true")),
        WorkflowNode(node_id="lane_b", kind=NodeKind.ACTION,
                     label="收藏匹配帖子", parent_id="fan",
                     depends_on=("cp", "fan"),
                     contract=_contract("c_b", "marked", "true")),
        WorkflowNode(node_id="bar", kind=NodeKind.BARRIER,
                     label="等待点赞与收藏完成", depends_on=barrier_deps),
        WorkflowNode(node_id="term", kind=NodeKind.TERMINAL,
                     label="任务完成", depends_on=("bar",)),
    ))


def test_r13_shape_lanes_after_own_container_run_full_chain():
    """The exact r13 topology (barrier fans in the LANES) now runs end to
    end through the public kernel API — before the RFC the lanes were
    permanently not-ready after the checkpoint committed."""
    k = _kernel(_r13_graph(barrier_on_container=False))
    k.advance_control("cp")          # the r13 break point: checkpoint in,
    st = k.workflow().statuses       # then silence
    assert st["lane_a"] is NodeStatus.READY
    assert st["lane_b"] is NodeStatus.READY
    _run_action(k, "lane_a", "liked", "true")
    _run_action(k, "lane_b", "marked", "true")
    st = k.workflow().statuses
    assert st["fan"] is NodeStatus.COMMITTED   # auto-commit once lanes done
    assert st["bar"] is NodeStatus.READY
    k.advance_control("bar")
    k.advance_control("term")
    assert all(s is NodeStatus.COMMITTED
               for s in k.workflow().statuses.values())


def test_barrier_depending_on_container_is_usable():
    """barrier after=[the fan-out CONTAINER] — the prompt-blessed form made
    real by the container auto-commit: the barrier becomes READY exactly
    when the container commits (all lanes done), never before."""
    k = _kernel(_r13_graph(barrier_on_container=True))
    k.advance_control("cp")
    _run_action(k, "lane_a", "liked", "true")
    # one lane done: container not yet committed, barrier still waiting
    assert k.workflow().statuses["fan"] is NodeStatus.READY
    assert k.workflow().statuses["bar"] is NodeStatus.PENDING
    _run_action(k, "lane_b", "marked", "true")
    assert k.workflow().statuses["fan"] is NodeStatus.COMMITTED
    assert k.workflow().statuses["bar"] is NodeStatus.READY
    k.advance_control("bar")
    k.advance_control("term")
    assert all(s is NodeStatus.COMMITTED
               for s in k.workflow().statuses.values())


def test_sequence_step_after_own_sequence_container_is_schedulable():
    """SEQUENCE / FAN_OUT unified (owner ruling): a step naming its own
    SEQUENCE in 'after' orders-within-the-sequence, same as a lane."""
    graph = WorkflowGraph(nodes=(
        WorkflowNode(node_id="sq", kind=NodeKind.SEQUENCE, label="序列"),
        WorkflowNode(node_id="s1", kind=NodeKind.ACTION, label="步骤一",
                     parent_id="sq",
                     contract=_contract("c_s1", "liked", "true")),
        WorkflowNode(node_id="s2", kind=NodeKind.ACTION, label="步骤二",
                     parent_id="sq", depends_on=("s1", "sq"),
                     contract=_contract("c_s2", "marked", "true")),
        WorkflowNode(node_id="term", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("sq",)),
    ))
    k = _kernel(graph)
    assert k.workflow().statuses["s1"] is NodeStatus.READY
    _run_action(k, "s1", "liked", "true")
    # s2 names its own sequence in 'after' — schedulable once the sequence
    # is scheduled and s1 committed
    assert k.workflow().statuses["s2"] is NodeStatus.READY
    _run_action(k, "s2", "marked", "true")
    assert k.workflow().statuses["sq"] is NodeStatus.COMMITTED
    assert k.workflow().statuses["term"] is NodeStatus.READY
    k.advance_control("term")
    assert all(s is NodeStatus.COMMITTED
               for s in k.workflow().statuses.values())


def test_lane_still_waits_while_own_container_unscheduled():
    """The relaxation is ordering-WITHIN-container, not a bypass: before
    the container is scheduled (its own deps unmet) the lane stays not
    ready."""
    graph = WorkflowGraph(nodes=(
        WorkflowNode(node_id="pre", kind=NodeKind.ACTION, label="前置",
                     contract=_contract("c_pre", "liked", "true")),
        WorkflowNode(node_id="fan", kind=NodeKind.FAN_OUT, label="并行",
                     depends_on=("pre",)),
        WorkflowNode(node_id="lane_a", kind=NodeKind.ACTION, label="点赞",
                     parent_id="fan", depends_on=("fan",),
                     contract=_contract("c_a", "liked", "true")),
        WorkflowNode(node_id="term", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("fan",)),
    ))
    k = _kernel(graph)
    # 'pre' not committed -> fan PENDING -> lane_a's own-container dep is
    # NOT satisfied at scheduling level
    assert k.workflow().statuses["fan"] is NodeStatus.PENDING
    assert k.workflow().statuses["lane_a"] is NodeStatus.PENDING
    _run_action(k, "pre", "liked", "true")
    assert k.workflow().statuses["fan"] is NodeStatus.READY
    assert k.workflow().statuses["lane_a"] is NodeStatus.READY
    _run_action(k, "lane_a", "liked", "true")
    assert k.workflow().statuses["fan"] is NodeStatus.COMMITTED
    k.advance_control("term")
    assert all(s is NodeStatus.COMMITTED
               for s in k.workflow().statuses.values())


def test_loop_body_naming_own_loop_stays_strict():
    """BOUNDED_LOOP is excluded from the relaxation (owner ruling): a body
    node naming its own loop keeps the strict COMMITTED requirement —
    loop bodies are gated by the kernel's loop protocol, never by an
    own-parent dependency."""
    graph = WorkflowGraph(nodes=(
        WorkflowNode(node_id="loop", kind=NodeKind.BOUNDED_LOOP, label="循环",
                     termination_predicate="liked == true", max_iterations=3),
        WorkflowNode(node_id="body", kind=NodeKind.ACTION, label="体",
                     parent_id="loop", depends_on=("loop",),
                     contract=_contract("c_body", "liked", "true")),
        WorkflowNode(node_id="term", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("loop",)),
    ))
    running = {"loop": NodeStatus.RUNNING, "body": NodeStatus.PENDING,
               "term": NodeStatus.PENDING}
    assert [n.node_id for n in schedulable_nodes(graph, running)] == []
    # the domain's strict view agrees — no behavioural change for loops
    assert [n.node_id for n in graph.ready_nodes(running)] == []


# ── the regression lock: byte-level identity for flat shapes ───────────────
def _flat_chain_graph():
    """A flat single chain with NO own-parent 'after' entry (the r11/r12
    family shape): a1 -> v1 -> cp -> term."""
    return WorkflowGraph(nodes=(
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="行动",
                     contract=_contract("c_a1", "liked", "true")),
        WorkflowNode(node_id="v1", kind=NodeKind.VERIFY, label="核验",
                     depends_on=("a1",), verification="liked == true"),
        WorkflowNode(node_id="cp", kind=NodeKind.CHECKPOINT, label="检查点",
                     depends_on=("v1",)),
        WorkflowNode(node_id="term", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("cp",)),
    ))


def _fan_graph_no_own_parent():
    """The textbook fan-out graph whose lanes do NOT name the container —
    the shape every pre-r13 test used; scheduling must stay identical."""
    return WorkflowGraph(nodes=(
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="行动",
                     contract=_contract("c_a1", "liked", "true")),
        WorkflowNode(node_id="fan", kind=NodeKind.FAN_OUT, label="并行",
                     depends_on=("a1",)),
        WorkflowNode(node_id="l1", kind=NodeKind.ACTION, label="道一",
                     parent_id="fan",
                     contract=_contract("c_l1", "liked", "true")),
        WorkflowNode(node_id="l2", kind=NodeKind.ACTION, label="道二",
                     parent_id="fan",
                     contract=_contract("c_l2", "marked", "true")),
        WorkflowNode(node_id="bar", kind=NodeKind.BARRIER, label="汇合",
                     depends_on=("l1", "l2")),
        WorkflowNode(node_id="term", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("bar",)),
    ))


def _sequence_graph_no_own_parent():
    """A sequence-container chain without own-parent entries."""
    return WorkflowGraph(nodes=(
        WorkflowNode(node_id="sq", kind=NodeKind.SEQUENCE, label="序列"),
        WorkflowNode(node_id="s1", kind=NodeKind.ACTION, label="步骤一",
                     parent_id="sq",
                     contract=_contract("c_s1", "liked", "true")),
        WorkflowNode(node_id="s2", kind=NodeKind.ACTION, label="步骤二",
                     parent_id="sq", depends_on=("s1",),
                     contract=_contract("c_s2", "marked", "true")),
        WorkflowNode(node_id="term", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("sq",)),
    ))


@pytest.mark.parametrize("graph_builder", [
    _flat_chain_graph,
    _fan_graph_no_own_parent,
    _sequence_graph_no_own_parent,
])
def test_schedulable_equals_domain_ready_for_every_status_map(graph_builder):
    """Regression lock: with NO own-parent depends_on entries the kernel
    rule is EXACTLY the domain rule — same nodes, same ORDER — for every
    status map (exhaustive cross-product over all NodeStatus values, not
    only reachable ones). Flat chains / single chains schedule
    byte-identically."""
    g = graph_builder()
    ids = [n.node_id for n in g.nodes]
    for combo in product(_ALL_STATUSES, repeat=len(ids)):
        statuses = dict(zip(ids, combo))
        kernel_view = schedulable_nodes(g, statuses)
        domain_view = g.ready_nodes(statuses)
        assert kernel_view == domain_view, (
            f"divergence at statuses={statuses}: "
            f"schedulable={[n.node_id for n in kernel_view]} "
            f"domain={[n.node_id for n in domain_view]}")


def test_relaxation_only_adds_nodes_on_the_r13_graph():
    """The relaxation can only ADD nodes (it weakens one dependency rule,
    strengthens nothing): domain-ready ⊆ kernel-schedulable for every
    status map of the r13 graph — the fix cannot regress any state that
    the strict rule already scheduled."""
    g = _r13_graph()
    ids = [n.node_id for n in g.nodes]
    for combo in product(_ALL_STATUSES, repeat=len(ids)):
        statuses = dict(zip(ids, combo))
        dom = {n.node_id for n in g.ready_nodes(statuses)}
        ker = {n.node_id for n in schedulable_nodes(g, statuses)}
        assert dom <= ker, (
            f"strictly-scheduled node lost at statuses={statuses}: "
            f"domain-only={sorted(dom - ker)}")


def test_relaxation_actively_unblocks_the_r13_deadlock_state():
    """The exact status map r13 died on: checkpoint committed, container
    merely READY (its lanes never ran). The domain rule returns NOTHING
    (the frozen deadlock view); the kernel rule returns both lanes."""
    g = _r13_graph(barrier_on_container=False)
    statuses = {
        "cp": NodeStatus.COMMITTED,
        "fan": NodeStatus.READY,
        "lane_a": NodeStatus.PENDING,
        "lane_b": NodeStatus.PENDING,
        "bar": NodeStatus.PENDING,
        "term": NodeStatus.PENDING,
    }
    # the domain's strict view sees ONLY the container (no deps) — the
    # runtime's kind filter then discards it, leaving nothing actionable:
    # the frozen deadlock view
    assert [n.node_id for n in g.ready_nodes(statuses)] == ["fan"]
    assert [n.node_id for n in schedulable_nodes(g, statuses)] == \
        ["fan", "lane_a", "lane_b"]
