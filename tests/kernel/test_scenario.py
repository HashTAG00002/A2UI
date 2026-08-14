"""Pure in-memory end-to-end scenarios (handoff 02 §迁移策略 5; v5 contract).

Two arcs:
  1. test_full_workflow_reaches_terminal — the node advancement protocol:
     ACTION nodes ride the action lifecycle; VERIFY commits from READY;
     BARRIER / CHECKPOINT / TERMINAL advance via advance_control; the
     fan-out container auto-commits when its lanes are done.
  2. test_full_governance_scenario — LocalPatch (deterministic retarget) →
     GoalPatch (phase one: invalidate + block) → stale epoch rejection →
     recompose (phase two: atomic closure) → CompensationPatch derived
     from committed action history, restoring the observed checkpoint
     state and marking undone work COMPENSATED.

No Flask, no browser, no model, no substrate.
"""
import pytest

from taskvm.domain import (
    ActionContract,
    CompensationEntryResult,
    CompensationPatch,
    CompensationResult,
    EventKind,
    GoalPatch,
    LocalPatch,
    NodeKind,
    NodeStatus,
    ObservedValue,
    ProjectionComponent,
    ProjectionSchema,
    TaskIntent,
    TaskVariable,
    ValidationError,
    VariableUpdate,
    VerificationResult,
    WorkflowGraph,
    WorkflowNode,
)
from taskvm.kernel import TaskVMKernel


def _contract(cid, key, value):
    return ActionContract(contract_id=cid,
                          semantic_goal=f"set {key} to {value}",
                          desired_state={key: value},
                          completion_condition=f"{key} visibly shows {value}")


def _vars(desired="2026-08-18", release_observed="2026-08-14",
          copy_observed="2026-08-14", qa_observed="2026-08-14",
          qa_desired=None):
    return (
        TaskVariable(semantic_key="release_date", label="发布日期",
                     observed=release_observed, desired=desired,
                     value_type="date"),
        TaskVariable(semantic_key="copy_deadline", label="文案截止",
                     observed=copy_observed, desired=desired,
                     value_type="date"),
        TaskVariable(semantic_key="qa_deadline", label="测试截止",
                     observed=qa_observed,
                     desired=qa_desired if qa_desired is not None else desired,
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
    _verify(k, "v1", True, detail="visible date matches")
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
    assert rec is not None and rec.checkpoint_id == "ckpt:cp1"
    assert rec.observed["qa_deadline"] == "2026-08-18"
    assert "cp1" in rec.committed_nodes   # the node belongs to its boundary
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
    _verify(k, "v1", True)
    rec = k.commit_checkpoint("C1", "会议已改期并验证")
    assert rec.observed["release_date"] == "2026-08-18"
    assert set(rec.committed_nodes) == {"a1", "v1"}
    _run_action(k, "l1", "copy_deadline", "2026-08-18")

    # ── LocalPatch: 局部调整（下午 4 点），单一真源 + 确定性 retarget ──────
    epoch_before = k.epoch
    out = k.apply_local_patch(LocalPatch(
        patch_id="lp1", rationale="测试截止改成下午 4 点",
        variable_updates=(VariableUpdate("qa_deadline", "2026-08-18T16:00"),)))
    assert out["requires_replan"] is False
    assert out["retargeted_nodes"] == ["l2"]   # the kernel retargets — no
    assert k.epoch > epoch_before              # manual contract override
    qa = k.task_state().variable("qa_deadline")
    assert qa.desired == "2026-08-18T16:00"   # desired moved …
    assert qa.observed == "2026-08-14"        # … reality NOT faked
    assert qa.diverged is True
    assert {n.node_id for n in k.workflow().graph.nodes} == \
           {n.node_id for n in _full_graph().nodes}   # topology untouched
    assert k.workflow().graph.node("l2").contract.desired_state[
        "qa_deadline"] == "2026-08-18T16:00"   # runtime gets the NEW target

    # ── GoalPatch phase one: 终点改为 8/20 → 旧未来作废 + 执行阻断 ──────
    h2 = k.request_action("l2")          # in-flight when the patch lands
    k.start_action(h2["action_id"])
    stale_epoch = h2["epoch"]
    out = k.apply_goal_patch(
        GoalPatch(patch_id="gp1", rationale="不要 18 日了，改 20 日",
                  new_intent=TaskIntent(
                      goal="把项目发布会议推迟到 2026-08-20 并同步所有依赖任务",
                      success_criteria=("会议日期为 2026-08-20",))))
    assert out["requires_replan"] is True and out["intent_changed"] is True
    statuses = k.workflow().statuses
    assert statuses["a1"] is NodeStatus.COMMITTED   # history preserved
    assert statuses["v1"] is NodeStatus.COMMITTED
    assert statuses["l1"] is NodeStatus.COMMITTED
    assert statuses["l2"] is NodeStatus.INVALIDATED  # old future void
    with pytest.raises(ValidationError, match="recompose"):
        k.request_action("l2")           # execution blocked until closure
    assert k.pending_recompose is not None

    # ── stale epoch result 被拒绝，不污染 TaskState ───────────────────
    assert stale_epoch < k.epoch
    accepted = k.finish_action(h2["action_id"], observations=[
        ObservedValue(semantic_key="qa_deadline", value="2026-08-18")])
    assert accepted is False
    assert k.task_state().variable("qa_deadline").observed == "2026-08-14"
    discarded = [e for e in k.events()
                 if e.kind is EventKind.ACTION_DISCARDED]
    assert discarded and discarded[-1].payload["action_epoch"] == stale_epoch

    # ── GoalPatch phase two: recompose 原子闭环（携带已验证历史）─────────
    # carried history: committed nodes verbatim; "fo" comes along because
    # the committed lane l1 still lives inside it.
    carried = tuple(k.workflow().graph.node(nid)
                    for nid in ("a1", "v1", "fo", "l1"))
    k.recompose(_vars(desired="2026-08-20", release_observed="2026-08-18",
                      copy_observed="2026-08-18"),
                reason="gp1 replan",
                new_graph=WorkflowGraph(nodes=carried + (
                    WorkflowNode(node_id="a3", kind=NodeKind.ACTION,
                                 label="改会议到 8/20", depends_on=("v1",),
                                 contract=_contract("c_a3", "release_date",
                                                    "2026-08-20")),
                    WorkflowNode(node_id="l3", kind=NodeKind.ACTION,
                                 label="同步测试到 8/20", depends_on=("a3",),
                                 contract=_contract("c_l3", "qa_deadline",
                                                    "2026-08-20")),
                    WorkflowNode(node_id="t2", kind=NodeKind.TERMINAL,
                                 label="完成", depends_on=("l3",)),
                )))
    assert k.pending_recompose is None    # closed
    statuses = k.workflow().statuses
    assert statuses["a1"] is NodeStatus.COMMITTED
    assert statuses["l1"] is NodeStatus.COMMITTED
    assert statuses["a3"] is NodeStatus.READY
    # no split-brain: desired plane and future contracts agree
    assert k.task_state().variable("release_date").desired == "2026-08-20"
    assert k.task_state().variable("qa_deadline").desired == "2026-08-20"

    # ── CompensationPatch: 回到 C1（由已提交动作历史推导，非快照 diff）───
    _run_action(k, "a3", "release_date", "2026-08-20")   # new epoch work
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb1", target_checkpoint_id="ckpt:C1"))
    # LIFO over what TaskVM ACTUALLY committed after C1: a3 then l1
    assert [(e.node_id, e.semantic_key, e.from_observed, e.to_observed)
            for e in plan.entries] == [
        ("a3", "release_date", "2026-08-20", "2026-08-18"),
        ("l1", "copy_deadline", "2026-08-18", "2026-08-14")]
    assert plan.requires_recompose is True   # crosses the GoalPatch boundary
    # runtime executed the reversions through the real path, then re-observed
    report = CompensationResult.for_plan(plan, epoch=k.epoch, outcomes=[
        CompensationEntryResult(node_id=e.node_id, semantic_key=e.semantic_key,
                                final_observed=e.to_observed, compensated=True)
        for e in plan.entries],
        detail="inverse actions executed + re-observed")
    assert k.record_compensation_result(plan.plan_id, report) == "complete"
    state = k.task_state()
    assert state.variable("release_date").observed == "2026-08-18"
    assert state.variable("copy_deadline").observed == "2026-08-14"
    # the checkpoint intent is really restored; desired follows C1's plane
    assert state.intent.goal.startswith("把项目发布会议推迟到 2026-08-18")
    assert state.variable("release_date").desired == "2026-08-18"
    # undone post-C1 commits are honestly marked COMPENSATED (audit trail),
    # the abandoned future is invalidated, and recompose is required
    statuses = k.workflow().statuses
    assert statuses["a3"] is NodeStatus.COMPENSATED
    assert statuses["l1"] is NodeStatus.COMPENSATED
    assert statuses["a1"] is NodeStatus.COMMITTED      # kept (at C1)
    assert statuses["l3"] is NodeStatus.INVALIDATED
    assert k.pending_recompose is not None
    kinds = [e.kind for e in k.events()]
    assert EventKind.COMPENSATION_REQUESTED in kinds
    assert EventKind.COMPENSATION_APPLIED in kinds


def test_compensation_failure_is_honestly_recorded():
    k = TaskVMKernel(session_id="arc2", intent=TaskIntent(goal="g"))
    k.init_task_state((TaskVariable(semantic_key="x", label="x",
                                    observed=1, desired=2),))
    g = WorkflowGraph(nodes=(
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="改 x",
                     contract=_contract("c_a1", "x", 2)),
        WorkflowNode(node_id="t", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a1",)),
    ))
    k.set_plan(g)
    k.commit_checkpoint("C0", "init")
    _run_action(k, "a1", "x", 2)
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:C0"))
    failed = CompensationResult.for_plan(plan, epoch=k.epoch, outcomes=[
        CompensationEntryResult(node_id=e.node_id, semantic_key=e.semantic_key,
                                final_observed=e.from_observed,
                                compensated=False)
        for e in plan.entries], detail="the world refused (irreversible)")
    assert k.record_compensation_result(plan.plan_id, failed) == "failed"
    assert k.task_state().variable("x").observed == 2  # state NOT rewound
    kinds = [e.kind for e in k.events()]
    assert EventKind.COMPENSATION_FAILED in kinds
    assert EventKind.COMPENSATION_APPLIED not in kinds
