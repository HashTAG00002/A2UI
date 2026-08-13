"""Pure in-memory end-to-end scenarios (handoff 02 §迁移策略 5 + Wave-A
regression: a full workflow must REACH TERMINAL through public APIs).

Two arcs:
  1. test_full_workflow_reaches_terminal — the node advancement protocol:
     ACTION nodes ride the action lifecycle; VERIFY commits from READY;
     BARRIER / CHECKPOINT / TERMINAL advance via advance_control; the
     fan-out container auto-commits when its lanes are done.
  2. test_full_governance_scenario — LocalPatch → GoalPatch (future-only
     replacement) → stale epoch rejection → CompensationPatch restoring
     the observed checkpoint state.

No Flask, no browser, no model, no substrate.
"""
import pytest

from taskvm.domain import (
    ActionContract,
    CompensationPatch,
    EventKind,
    GoalPatch,
    LocalPatch,
    NodeContractOverride,
    NodeKind,
    NodeStatus,
    ObservedValue,
    ProjectionComponent,
    ProjectionSchema,
    TaskIntent,
    TaskVariable,
    ValidationError,
    VariableUpdate,
    WorkflowGraph,
    WorkflowNode,
)
from taskvm.kernel import TaskVMKernel


def _contract(cid, key, value):
    return ActionContract(contract_id=cid,
                          semantic_goal=f"set {key} to {value}",
                          desired_state={key: value},
                          completion_condition=f"{key} visibly shows {value}")


def _vars():
    return (
        TaskVariable(semantic_key="release_date", label="发布日期",
                     observed="2026-08-14", desired="2026-08-18",
                     value_type="date"),
        TaskVariable(semantic_key="copy_deadline", label="文案截止",
                     observed="2026-08-14", desired="2026-08-18",
                     value_type="date"),
        TaskVariable(semantic_key="qa_deadline", label="测试截止",
                     observed="2026-08-14", desired="2026-08-18",
                     value_type="date"),
    )


def _full_graph(date="2026-08-18"):
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
    k = TaskVMKernel(session_id="arc", intent=TaskIntent(
        goal="把项目发布会议推迟到 2026-08-18 并同步所有依赖任务",
        constraints=("不改动已确认的安排",),
        scope=("日历", "任务板"),
        success_criteria=("会议日期为 2026-08-18", "所有依赖任务截止同步")))
    k.init_task_state(_vars())
    k.set_plan(_full_graph(), ProjectionSchema(root_id="root", components=(
        ProjectionComponent(component_id="root", component_type="column",
                            children=("f_date",)),
        ProjectionComponent(component_id="f_date", component_type="field",
                            label="发布日期", binding_key="release_date",
                            editable=True),
    )))
    return k


def _run_action(k, node_id, key, value):
    h = k.request_action(node_id)
    k.start_action(h["action_id"])
    assert k.finish_action(h["action_id"], observations=[
        ObservedValue(semantic_key=key, value=value)])
    k.record_verification(node_id, True, detail="observed match")


# ── regression: the full workflow REACHES TERMINAL via public API ─────────
def test_full_workflow_reaches_terminal():
    k = _kernel()
    # control nodes cannot be requested as CUA actions (protocol clarity)
    with pytest.raises(ValidationError):
        k.request_action("v1")
    with pytest.raises(ValidationError):
        k.request_action("b1")
    # ACTION: full lifecycle
    _run_action(k, "a1", "release_date", "2026-08-18")
    # VERIFY: commits directly from READY (independent observation)
    k.record_verification("v1", True, detail="visible date matches")
    # fan-out lanes unblocked
    statuses = k.workflow().statuses
    assert statuses["l1"] is NodeStatus.READY
    assert statuses["l2"] is NodeStatus.READY
    _run_action(k, "l1", "copy_deadline", "2026-08-18")
    _run_action(k, "l2", "qa_deadline", "2026-08-18")
    # the fan-out container auto-commits once every lane committed
    assert k.workflow().statuses["fo"] is NodeStatus.COMMITTED
    # BARRIER / CHECKPOINT / TERMINAL: control advancement
    assert k.advance_control("b1") is None
    rec = k.advance_control("cp1")
    assert rec is not None and rec.checkpoint_id == "cp1"
    assert rec.observed["qa_deadline"] == "2026-08-18"
    assert k.advance_control("t1") is None
    # every node committed; the plan is complete
    statuses = k.workflow().statuses
    assert all(st is NodeStatus.COMMITTED for st in statuses.values())
    assert k.projection().data.progress == 1.0
    # all task variables converged (observed == desired everywhere)
    assert k.task_state().diverged_keys() == ()
    kinds = [e.kind for e in k.events()]
    assert EventKind.NODE_COMMITTED in kinds           # barrier + terminal
    assert EventKind.CHECKPOINT_COMMITTED in kinds     # the cp1 record


# ── the governance arc ─────────────────────────────────────────────────────
def test_full_governance_scenario():
    k = _kernel()
    assert k.epoch == 0
    assert k.task_state().revision == 1

    # forward work: a1 + v1 verified, checkpoint, then lane l1 commits
    _run_action(k, "a1", "release_date", "2026-08-18")
    k.record_verification("v1", True)
    rec = k.commit_checkpoint("C1", "会议已改期并验证")
    assert rec.observed["release_date"] == "2026-08-18"
    assert set(rec.committed_nodes) == {"a1", "v1"}
    _run_action(k, "l1", "copy_deadline", "2026-08-18")

    # ── LocalPatch: 局部调整（同一变量/节点，下午 4 点），不改拓扑 ──────────
    epoch_before = k.epoch
    out = k.apply_local_patch(LocalPatch(
        patch_id="lp1", rationale="测试截止改成下午 4 点",
        variable_updates=(VariableUpdate("qa_deadline", "2026-08-18T16:00"),),
        node_overrides=(NodeContractOverride(
            "l2", _contract("c_l2b", "qa_deadline", "2026-08-18T16:00")),)))
    assert out["requires_replan"] is False
    assert k.epoch > epoch_before
    qa = k.task_state().variable("qa_deadline")
    assert qa.desired == "2026-08-18T16:00"   # desired moved …
    assert qa.observed == "2026-08-14"        # … reality NOT faked
    assert qa.diverged is True
    assert {n.node_id for n in k.workflow().graph.nodes} == \
           {n.node_id for n in _full_graph().nodes}   # topology untouched
    assert k.workflow().graph.node("l2").contract.desired_state[
        "qa_deadline"] == "2026-08-18T16:00"

    # ── GoalPatch: 终点改为 8/20，已 commit 的历史保留，只换未来 ──────
    h2 = k.request_action("l2")          # in-flight when the patch lands
    k.start_action(h2["action_id"])
    stale_epoch = h2["epoch"]
    # carried history: committed nodes verbatim; "fo" comes along because
    # the committed lane l1 still lives inside it.
    carried = tuple(k.workflow().graph.node(nid)
                    for nid in ("a1", "v1", "fo", "l1"))
    new_graph = WorkflowGraph(nodes=carried + (
        WorkflowNode(node_id="a3", kind=NodeKind.ACTION, label="改会议到 8/20",
                     depends_on=("v1",),
                     contract=_contract("c_a3", "release_date", "2026-08-20")),
        WorkflowNode(node_id="l3", kind=NodeKind.ACTION, label="同步全部依赖",
                     depends_on=("a3",),
                     contract=_contract("c_l3", "qa_deadline", "2026-08-20")),
        WorkflowNode(node_id="t2", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("l3",)),
    ))
    out = k.apply_goal_patch(
        GoalPatch(patch_id="gp1", rationale="不要 18 日了，改 20 日",
                  new_intent=TaskIntent(
                      goal="把项目发布会议推迟到 2026-08-20 并同步所有依赖任务",
                      success_criteria=("会议日期为 2026-08-20",))),
        new_graph=new_graph)
    assert out["requires_replan"] is True and out["intent_changed"] is True
    statuses = k.workflow().statuses
    assert statuses["a1"] is NodeStatus.COMMITTED   # history preserved
    assert statuses["l1"] is NodeStatus.COMMITTED
    assert "l2" not in statuses                     # future replaced
    assert statuses["a3"] is NodeStatus.READY
    # the architect recomposes: desired moves to 8/20 (observed untouched)
    k.recompose(tuple(
        TaskVariable(semantic_key=v.semantic_key, label=v.label,
                     observed=v.observed,
                     desired={"release_date": "2026-08-20",
                              "copy_deadline": "2026-08-20",
                              "qa_deadline": "2026-08-20"}[v.semantic_key],
                     value_type=v.value_type)
        for v in k.task_state().variables), reason="GoalPatch gp1 replan")

    # ── stale epoch result 被拒绝，不污染 TaskState ───────────────────
    assert stale_epoch < k.epoch
    accepted = k.finish_action(h2["action_id"], observations=[
        ObservedValue(semantic_key="qa_deadline", value="2026-08-18")])
    assert accepted is False
    assert k.task_state().variable("qa_deadline").observed == "2026-08-14"
    discarded = [e for e in k.events()
                 if e.kind is EventKind.ACTION_DISCARDED]
    assert discarded and discarded[-1].payload["action_epoch"] == stale_epoch

    # ── CompensationPatch: 回到 C1（kernel 自录的 observed state）────────
    _run_action(k, "a3", "release_date", "2026-08-20")   # new epoch work
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb1", target_checkpoint_id="C1"))
    targets = {e.semantic_key: e.to_observed for e in plan.entries}
    assert targets["release_date"] == "2026-08-18"     # 8/20 → back to 8/18
    assert targets["copy_deadline"] == "2026-08-14"    # l1's commit reverted
    # runtime executed the reversions through the real path, then re-observed
    k.record_compensation_result(
        plan.plan_id, applied=True,
        observed_values={"release_date": "2026-08-18",
                         "copy_deadline": "2026-08-14"},
        detail="inverse actions executed + re-observed")
    state = k.task_state()
    assert state.variable("release_date").observed == "2026-08-18"
    assert state.variable("copy_deadline").observed == "2026-08-14"
    # desired plane restored to the checkpoint wholesale (rollback returns
    # the task world, not just reality)
    assert state.variable("release_date").desired == "2026-08-18"
    # nodes committed AFTER C1 are honestly marked COMPENSATED
    statuses = k.workflow().statuses
    assert statuses["l1"] is NodeStatus.COMPENSATED
    assert statuses["a3"] is NodeStatus.COMPENSATED
    assert statuses["a1"] is NodeStatus.COMMITTED      # kept (at C1)
    kinds = [e.kind for e in k.events()]
    assert EventKind.COMPENSATION_REQUESTED in kinds
    assert EventKind.COMPENSATION_APPLIED in kinds


def test_compensation_failure_is_honestly_recorded():
    k = TaskVMKernel(session_id="arc2", intent=TaskIntent(goal="g"))
    k.init_task_state((TaskVariable(semantic_key="x", label="x",
                                    observed=1, desired=2),))
    k.commit_checkpoint("C0", "init")
    k.apply_observation([ObservedValue(semantic_key="x", value=2)])
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="C0"))
    k.record_compensation_result(plan.plan_id, applied=False,
                                 detail="the world refused (irreversible)")
    assert k.task_state().variable("x").observed == 2  # state NOT rewound
    kinds = [e.kind for e in k.events()]
    assert EventKind.COMPENSATION_FAILED in kinds
    assert EventKind.COMPENSATION_APPLIED not in kinds
