"""The required pure in-memory end-to-end scenario (handoff 02 §迁移策略 5):

    初始化 task state → fan-out workflow → action success → verification →
    checkpoint → LocalPatch → GoalPatch 只替换未来 → stale epoch result 被拒绝
    → CompensationPatch 恢复 observed before

No Flask, no browser, no model, no substrate — this is the kernel state
machine driving the whole four-step arc alone.
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
    ProjectionComponent,
    ProjectionSchema,
    TaskIntent,
    TaskVariable,
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


def test_full_governance_scenario():
    # ── 1. 初始化 task state ─────────────────────────────────────────────
    k = TaskVMKernel(session_id="arc", intent=TaskIntent(
        goal="把项目发布会议推迟到 2026-08-18 并同步所有依赖任务",
        constraints=("不改动已确认的安排",),
        scope=("日历", "任务板"),
        success_criteria=("会议日期为 2026-08-18", "所有依赖任务截止同步")))
    k.init_task_state((
        TaskVariable(semantic_key="release_date", label="发布日期",
                     value="2026-08-14", value_type="date"),
        TaskVariable(semantic_key="copy_deadline", label="文案截止",
                     value="2026-08-14", value_type="date"),
        TaskVariable(semantic_key="qa_deadline", label="测试截止",
                     value="2026-08-14", value_type="date"),
    ))
    k.set_plan(_full_graph(), ProjectionSchema(root_id="root", components=(
        ProjectionComponent(component_id="root", component_type="column",
                            children=("f_date",)),
        ProjectionComponent(component_id="f_date", component_type="field",
                            label="发布日期", binding_key="release_date",
                            editable=True),
    )))
    assert k.epoch == 0
    assert k.task_state().revision == 1

    # ── 2-4. fan-out workflow: action success + verification ─────────────
    assert k.workflow().statuses["a1"] is NodeStatus.READY
    h = k.request_action("a1")
    k.start_action(h["action_id"])
    assert k.finish_action(h["action_id"],
                           observed_values={"release_date": "2026-08-18"})
    k.record_verification("a1", True, detail="visible date matches")
    hv = k.request_action("v1")
    k.start_action(hv["action_id"])
    assert k.finish_action(hv["action_id"])
    k.record_verification("v1", True)
    # fan-out lanes unblocked by the verify commit
    statuses = k.workflow().statuses
    assert statuses["l1"] is NodeStatus.READY
    assert statuses["l2"] is NodeStatus.READY

    # ── 5. checkpoint pins the verified boundary ─────────────────────────
    rec = k.commit_checkpoint("C1", "会议已改期并验证")
    assert rec.variables["release_date"] == "2026-08-18"
    assert set(rec.committed_nodes) == {"a1", "v1"}

    # lane l1 completes and commits AFTER the checkpoint
    h1 = k.request_action("l1")
    k.start_action(h1["action_id"])
    assert k.finish_action(h1["action_id"],
                           observed_values={"copy_deadline": "2026-08-18"})
    k.record_verification("l1", True)

    # ── 6. LocalPatch: 局部调整（同一节点，下午 4 点），不改拓扑 ──────────
    epoch_before = k.epoch
    out = k.apply_local_patch(LocalPatch(
        patch_id="lp1", rationale="Calendar 先改成下午 4 点",
        variable_updates=(VariableUpdate("qa_deadline", "2026-08-18T16:00"),),
        node_overrides=(NodeContractOverride(
            "l2", _contract("c_l2b", "qa_deadline", "2026-08-18T16:00")),)))
    assert out["requires_replan"] is False
    assert k.epoch > epoch_before  # in-flight work predates the adjustment
    assert k.task_state().variable("qa_deadline").value == "2026-08-18T16:00"
    # topology untouched: same node ids
    assert {n.node_id for n in k.workflow().graph.nodes} == \
           {n.node_id for n in _full_graph().nodes}
    assert k.workflow().graph.node("l2").contract.desired_state[
        "qa_deadline"] == "2026-08-18T16:00"

    # ── 7. GoalPatch: 终点改为 8/20，已 commit 的历史保留，只换未来 ──────
    # start an in-flight action on l2 FIRST (it will become stale)
    h2 = k.request_action("l2")
    k.start_action(h2["action_id"])
    stale_epoch = h2["epoch"]
    # carried history: committed nodes verbatim; the fan-out container "fo"
    # comes along because the committed lane l1 still lives inside it.
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
    assert statuses["v1"] is NodeStatus.COMMITTED
    assert statuses["l1"] is NodeStatus.COMMITTED
    assert "l2" not in statuses                     # uncommitted future replaced
    assert statuses["a3"] is NodeStatus.READY
    assert k.task_state().intent.goal.endswith("并同步所有依赖任务")

    # ── 8. stale epoch result 被拒绝，不污染 TaskState ───────────────────
    assert stale_epoch < k.epoch
    accepted = k.finish_action(h2["action_id"],
                               observed_values={"qa_deadline": "2026-08-18"})
    assert accepted is False
    # the discarded write must NOT have landed
    assert k.task_state().variable("qa_deadline").value == "2026-08-18T16:00"
    discarded = [e for e in k.events()
                 if e.kind is EventKind.ACTION_DISCARDED]
    assert discarded and discarded[-1].payload["action_epoch"] == stale_epoch

    # ── 9. CompensationPatch: 回到 C1（observed before），非快照恢复 ─────
    k.apply_observation({"release_date": "2026-08-20"})  # a3 executed upstream
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb1", target_checkpoint_id="C1",
        observed_before={"release_date": "2026-08-18",
                         "copy_deadline": "2026-08-14",
                         "qa_deadline": "2026-08-14"}))
    targets = {e.semantic_key: e.to_value for e in plan.entries}
    assert targets["release_date"] == "2026-08-18"     # 8/20 → back to 8/18
    assert targets["copy_deadline"] == "2026-08-14"    # l1's commit reverted
    # runtime executed the reversions through the real path, then re-observed
    k.record_compensation_result(
        plan.plan_id, applied=True,
        observed_values={"release_date": "2026-08-18",
                         "copy_deadline": "2026-08-14",
                         "qa_deadline": "2026-08-14"},
        detail="inverse actions executed + re-observed")
    state = k.task_state()
    assert state.variable("release_date").value == "2026-08-18"
    assert state.variable("copy_deadline").value == "2026-08-14"
    # l1 was committed AFTER C1 → now honestly marked COMPENSATED
    assert k.workflow().statuses["l1"] is NodeStatus.COMPENSATED
    assert k.workflow().statuses["a1"] is NodeStatus.COMMITTED  # kept (at C1)
    kinds = [e.kind for e in k.events()]
    assert EventKind.COMPENSATION_REQUESTED in kinds
    assert EventKind.COMPENSATION_APPLIED in kinds


def test_compensation_failure_is_honestly_recorded():
    k = TaskVMKernel(session_id="arc2", intent=TaskIntent(goal="g"))
    k.init_task_state((TaskVariable(semantic_key="x", label="x", value=1),))
    k.commit_checkpoint("C0", "init")
    k.apply_observation({"x": 2})
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="C0", observed_before={"x": 1}))
    k.record_compensation_result(plan.plan_id, applied=False,
                                 detail="the world refused (irreversible)")
    assert k.task_state().variable("x").value == 2  # state NOT rewound
    kinds = [e.kind for e in k.events()]
    assert EventKind.COMPENSATION_FAILED in kinds
    assert EventKind.COMPENSATION_APPLIED not in kinds
