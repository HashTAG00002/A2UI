"""Kernel invariant tests (handoff 02 §Kernel 服务, invariants 1-7)."""
import pytest

from taskvm.domain import (
    ActionContract,
    CommittedNodeViolationError,
    CompensationMismatchError,
    CompensationPatch,
    EventKind,
    GoalPatch,
    LocalPatch,
    NodeContractOverride,
    NodeKind,
    NodeStatus,
    PatchSemanticsError,
    ProjectionComponent,
    ProjectionSchema,
    TaskIntent,
    TaskVariable,
    UnknownCheckpointError,
    ValidationError,
    VariableUpdate,
    WorkflowGraph,
    WorkflowNode,
)
from taskvm.kernel import TaskVMKernel


def _intent(goal="把发布会议改到 8/18 并同步依赖任务"):
    return TaskIntent(goal=goal,
                      constraints=("不要改动已确认的安排",),
                      scope=("日历", "任务板"),
                      success_criteria=("会议日期为 2026-08-18",))


def _vars():
    return (
        TaskVariable(semantic_key="release_date", label="发布日期",
                     value="2026-08-14", value_type="date"),
        TaskVariable(semantic_key="copy_deadline", label="文案截止",
                     value="2026-08-14", value_type="date"),
        TaskVariable(semantic_key="qa_deadline", label="测试截止",
                     value="2026-08-14", value_type="date"),
    )


def _contract(cid, key, value):
    return ActionContract(contract_id=cid,
                          semantic_goal=f"set {key} to {value}",
                          desired_state={key: value},
                          completion_condition=f"{key} shows {value}")


def _graph(date="2026-08-18"):
    """seq(a1) → fanout(l1, l2) → barrier → checkpoint → terminal."""
    return WorkflowGraph(nodes=(
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="改会议日期",
                     contract=_contract("c_a1", "release_date", date)),
        WorkflowNode(node_id="v1", kind=NodeKind.VERIFY, label="验证改期",
                     depends_on=("a1",), verification="release_date updated"),
        WorkflowNode(node_id="fo", kind=NodeKind.FAN_OUT, label="同步依赖",
                     depends_on=("v1",)),
        WorkflowNode(node_id="l1", kind=NodeKind.ACTION, label="同步文案",
                     parent_id="fo",
                     contract=_contract("c_l1", "copy_deadline", date)),
        WorkflowNode(node_id="l2", kind=NodeKind.ACTION, label="同步测试",
                     parent_id="fo",
                     contract=_contract("c_l2", "qa_deadline", date)),
        WorkflowNode(node_id="b1", kind=NodeKind.BARRIER, label="汇合校验",
                     depends_on=("l1", "l2")),
        WorkflowNode(node_id="cp1", kind=NodeKind.CHECKPOINT, label="检查点",
                     depends_on=("b1",)),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("cp1",)),
    ))


def _kernel():
    k = TaskVMKernel(session_id="s1", intent=_intent())
    k.init_task_state(_vars())
    k.set_plan(_graph(), ProjectionSchema(root_id="root", components=(
        ProjectionComponent(component_id="root", component_type="column"),
    )))
    return k


def _run_node(k, node_id, observed):
    h = k.request_action(node_id)
    k.start_action(h["action_id"])
    ok = k.finish_action(h["action_id"], observed_values=observed)
    assert ok
    k.record_verification(node_id, True, detail="observed match")


# ── invariant 1: monotonic revisions ──────────────────────────────────────
def test_state_revisions_monotonic():
    k = _kernel()
    r0 = k.task_state().revision
    k.apply_observation({"release_date": "2026-08-15"})
    r1 = k.task_state().revision
    k.apply_observation({"release_date": "2026-08-16"})
    r2 = k.task_state().revision
    assert r0 < r1 < r2


# ── invariant 2: schema/data revisions independent ────────────────────────
def test_schema_and_data_revisions_are_independent():
    k = _kernel()
    rev0 = k.projection().revision
    k.apply_observation({"release_date": "2026-08-15"})
    rev1 = k.projection().revision
    assert rev1.data_revision > rev0.data_revision
    assert rev1.schema_revision == rev0.schema_revision  # untouched by values


# ── invariant 3: committed nodes survive GoalPatch verbatim ───────────────
def test_goal_patch_preserves_committed_nodes():
    k = _kernel()
    _run_node(k, "a1", {"release_date": "2026-08-18"})
    _run_node(k, "v1", None)  # the verify node rides the same lifecycle
    old_graph = k.workflow().graph
    carried = tuple(old_graph.node(nid) for nid in ("a1", "v1"))
    new_graph = WorkflowGraph(nodes=carried + (
        WorkflowNode(node_id="a2", kind=NodeKind.ACTION, label="改到 8/20",
                     depends_on=("v1",),
                     contract=_contract("c_a2", "release_date", "2026-08-20")),
        WorkflowNode(node_id="t2", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a2",)),
    ))
    out = k.apply_goal_patch(
        GoalPatch(patch_id="gp1", new_intent=_intent("改到 8/20")),
        new_graph=new_graph)
    assert out["requires_replan"] is True
    statuses = k.workflow().statuses
    assert statuses["a1"] is NodeStatus.COMMITTED
    assert statuses["v1"] is NodeStatus.COMMITTED
    assert statuses["a2"] is NodeStatus.READY


def test_goal_patch_cannot_rewrite_committed_node():
    k = _kernel()
    _run_node(k, "a1", {"release_date": "2026-08-18"})
    tampered = WorkflowGraph(nodes=(
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="篡改",
                     contract=_contract("c_x", "release_date", "2026-01-01")),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a1",)),
    ))
    with pytest.raises(CommittedNodeViolationError):
        k.apply_goal_patch(GoalPatch(patch_id="gp_bad"), new_graph=tampered)


def test_goal_patch_cannot_drop_committed_node():
    k = _kernel()
    _run_node(k, "a1", {"release_date": "2026-08-18"})
    dropped = WorkflowGraph(nodes=(
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成"),
    ))
    with pytest.raises(CommittedNodeViolationError):
        k.apply_goal_patch(GoalPatch(patch_id="gp_bad2"), new_graph=dropped)


# ── invariant 4: stale epoch results are discarded ─────────────────────────
def test_stale_epoch_action_result_is_discarded():
    k = _kernel()
    h = k.request_action("a1")
    k.start_action(h["action_id"])
    # a GoalPatch lands while the action is in flight → epoch bump
    k.apply_goal_patch(GoalPatch(patch_id="gp1",
                                 new_intent=_intent("改到 8/20")))
    accepted = k.finish_action(h["action_id"],
                               observed_values={"release_date": "2026-08-18"})
    assert accepted is False
    assert k.task_state().variable("release_date").value == "2026-08-14"
    kinds = [e.kind for e in k.events()]
    assert EventKind.ACTION_DISCARDED in kinds
    assert EventKind.ACTION_FINISHED not in kinds
    # the node is back to READY for the new generation to retry
    assert k.workflow().statuses["a1"] is NodeStatus.READY


def test_current_epoch_action_result_lands():
    k = _kernel()
    h = k.request_action("a1")
    k.start_action(h["action_id"])
    assert k.finish_action(h["action_id"],
                           observed_values={"release_date": "2026-08-18"})
    assert k.task_state().variable("release_date").value == "2026-08-18"


# ── invariant 5: checkpoints pin a boundary ────────────────────────────────
def test_checkpoint_pins_event_revision_boundary():
    k = _kernel()
    _run_node(k, "a1", {"release_date": "2026-08-18"})
    n_events = len(k.events())
    rec = k.commit_checkpoint("C1", "会议已改期")
    assert rec.event_index == n_events
    assert rec.state_revision == k.task_state().revision
    assert rec.variables["release_date"] == "2026-08-18"
    assert "a1" in rec.committed_nodes
    with pytest.raises(UnknownCheckpointError):
        k.request_compensation(CompensationPatch(
            patch_id="cp_x", target_checkpoint_id="C99"))


# ── invariant 6: compensation grounded in recorded observation ────────────
def test_compensation_rejects_fabricated_before_values():
    k = _kernel()
    _run_node(k, "a1", {"release_date": "2026-08-18"})
    k.commit_checkpoint("C1", "会议已改期")
    with pytest.raises(CompensationMismatchError):
        k.request_compensation(CompensationPatch(
            patch_id="cp_bad", target_checkpoint_id="C1",
            observed_before={"release_date": "1999-01-01"}))  # never observed


# ── invariant 7: defensive copies ─────────────────────────────────────────
def test_snapshots_are_defensive_copies():
    k = _kernel()
    snap = k.projection()
    snap.data.values["release_date"] = "HACKED"
    assert k.task_state().variable("release_date").value == "2026-08-14"
    assert k.projection().data.values["release_date"] == "2026-08-14"
    st = k.task_state()
    st.variables[0].evidence  # tuple — immutable
    wf = k.workflow()
    wf.statuses["a1"] = NodeStatus.COMMITTED
    assert k.workflow().statuses["a1"] is NodeStatus.READY


# ── patch routing semantics ────────────────────────────────────────────────
def test_local_patch_rejects_unknown_variable():
    k = _kernel()
    with pytest.raises(PatchSemanticsError):
        k.apply_local_patch(LocalPatch(
            patch_id="lp_bad",
            variable_updates=(VariableUpdate("brand_new_var", "x"),)))


def test_local_patch_cannot_touch_committed_node():
    k = _kernel()
    _run_node(k, "a1", {"release_date": "2026-08-18"})
    with pytest.raises(CommittedNodeViolationError):
        k.apply_local_patch(LocalPatch(
            patch_id="lp_bad2",
            node_overrides=(NodeContractOverride(
                "a1", _contract("c_new", "release_date", "2026-08-19")),)))


def test_wrong_patch_type_rejected():
    k = _kernel()
    with pytest.raises(PatchSemanticsError):
        k.apply_local_patch(GoalPatch(patch_id="gp_wrong"))
    with pytest.raises(PatchSemanticsError):
        k.apply_goal_patch(LocalPatch(
            patch_id="lp_wrong",
            variable_updates=(VariableUpdate("release_date", "x"),)))


def test_observation_cannot_invent_variables():
    k = _kernel()
    with pytest.raises(ValidationError):
        k.apply_observation({"never_declared": "x"})


def test_projection_progress_tracks_commits():
    k = _kernel()
    _run_node(k, "a1", {"release_date": "2026-08-18"})
    data = k.projection().data
    assert data.node_status["a1"] == "committed"
    assert 0.0 < data.progress < 1.0
