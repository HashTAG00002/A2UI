"""tests/governance/ — GovernanceService scenarios (handoff 04 §Governance).

The unified entry contract, pinned against a real kernel + a scripted
architect port:

  3. LocalPatchRequested — 0 compiler / 0 architect calls; deterministic
     retarget; topology + milestones untouched; epoch bumped.
  4. GoalPatchRequested — exactly 1 architect call; committed history
     carried verbatim; only the future replaced; failure = honest
     GoalRecomposeFailed with execution left BLOCKED (no fallback).
  5. RollbackRequested — 0 / 0; the CompensationPlan derives from the
     kernel's own committed action history.

Plus the event vocabulary's own validity rules (the six frozen events).
"""
import pytest

from taskvm.architect import ModelCallLedger, TaskArchitect
from taskvm.domain import (
    NodeKind, NodeStatus, ObservedValue, TaskIntent, ValidationError,
    VerificationResult,
)
from taskvm.domain.contract import ActionContract
from taskvm.governance import (
    ConflictResolutionRequested, GoalPatchRequested, GoalRecomposeFailed,
    GovernanceService, LocalPatchRequested, PauseRequested, ResumeRequested,
    RollbackRequested,
)
from taskvm.kernel import TaskVMKernel


# ── scripted model port (architect side only; the compiler is unused here) ──
class FakeArchitectPort:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def complete_json(self, *, system, user, model=None, **kw):
        self.calls += 1
        import json as _json
        r = self.replies.pop(0)
        from taskvm.architect import ModelReply
        return ModelReply(parsed=r, raw=_json.dumps(r, ensure_ascii=False),
                          model=model or "fake")


def _contract(cid, key, value):
    return ActionContract(contract_id=cid,
                          semantic_goal=f"set {key} to {value}",
                          desired_state={key: value},
                          completion_condition=f"{key} visibly shows {value}")


def _vars(desired="2026-08-18", release_observed="2026-08-14",
          copy_observed="2026-08-14", qa_observed="2026-08-14"):
    from taskvm.domain import TaskVariable
    return (
        TaskVariable(semantic_key="release_date", label="发布日期",
                     observed=release_observed, desired=desired,
                     value_type="date"),
        TaskVariable(semantic_key="copy_deadline", label="文案截止",
                     observed=copy_observed, desired=desired,
                     value_type="date"),
        TaskVariable(semantic_key="qa_deadline", label="测试截止",
                     observed=qa_observed, desired=desired,
                     value_type="date"),
    )


def _graph(date="2026-08-18"):
    from taskvm.domain import WorkflowGraph, WorkflowNode
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


INTENT = TaskIntent(goal="把项目发布会议推迟到 2026-08-18 并同步所有依赖任务",
                    success_criteria=("会议日期为 2026-08-18",))


def _kernel():
    k = TaskVMKernel(session_id="gov-test", intent=INTENT)
    k.init_task_state(_vars())
    k.set_plan(_graph())
    return k


def _run_action(k, node_id, key, value):
    h = k.request_action(node_id)
    k.start_action(h["action_id"])
    assert k.finish_action(h["action_id"], observations=[
        ObservedValue(semantic_key=key, value=value)])
    k.land_verification(VerificationResult(
        node_id=node_id, epoch=k.epoch, passed=True,
        action_id=h["action_id"], detail="observed match"))


# ── scenario 3: LocalPatch — 0 model calls, topology untouched ─────────────
def test_scenario_3_local_patch_zero_calls_deterministic_retarget():
    k = _kernel()
    port = FakeArchitectPort()
    ledger = ModelCallLedger()
    svc = GovernanceService(k, architect=TaskArchitect(port, ledger),
                            ledger=ledger)

    nodes_before = {n.node_id: n.kind for n in k.workflow().graph.nodes}
    epoch_before = k.epoch

    out = svc.handle(LocalPatchRequested(
        updates={"qa_deadline": "2026-08-18T16:00"},
        rationale="测试截止改成下午 4 点", correlation_id="corr-1"))

    assert out.handled == "LocalPatchRequested"
    assert out.detail["architect_calls"] == 0
    assert out.detail["compiler_calls"] == 0
    assert port.calls == 0 and ledger.total() == 0
    assert out.epoch > epoch_before, "the epoch bumps — old requests die"
    assert out.detail["retargeted_nodes"] == ["l2"], (
        "the kernel retargets the one uncommitted contract writing the key")
    nodes_after = {n.node_id: n.kind for n in k.workflow().graph.nodes}
    assert nodes_after == nodes_before, "topology + milestones untouched"
    qa = k.task_state().variable("qa_deadline")
    assert qa.desired == "2026-08-18T16:00"   # desired moved…
    assert qa.observed == "2026-08-14"        # …reality NOT faked
    assert k.workflow().graph.node("l2").contract.desired_state[
        "qa_deadline"] == "2026-08-18T16:00"  # runtime sees the NEW target


# ── scenario 4: GoalPatch — 1 architect call, history carried verbatim ─────
RECOMPOSE_JSON = {
    "variables": [
        {"semantic_key": "release_date", "desired": "2026-08-20"},
        {"semantic_key": "copy_deadline"},
        {"semantic_key": "qa_deadline", "desired": "2026-08-20"},
    ],
    "workflow": {"nodes": [
        {"kind": "action", "label": "改会议到 20 日", "after": ["验证改期"],
         "semantic_goal": "把会议改到 8/20",
         "sets": {"release_date": "2026-08-20"},
         "completion": "日历显示 2026-08-20", "reversibility": "reversible",
         "risk": "", "target_evidence": ["发布会议"]},
        {"kind": "action", "label": "同步测试到 20 日",
         "after": ["改会议到 20 日"],
         "semantic_goal": "测试截止同步到 8/20",
         "sets": {"qa_deadline": "2026-08-20"},
         "completion": "任务板显示 8/20", "reversibility": "reversible",
         "risk": "", "target_evidence": ["测试任务"]},
        {"kind": "terminal", "label": "新完成", "after": ["同步测试到 20 日"]},
    ]},
}


def test_scenario_4_goal_patch_one_call_history_carried_verbatim():
    k = _kernel()
    # committed history: a1 + v1 verified, checkpoint C1, lane l1 committed
    _run_action(k, "a1", "release_date", "2026-08-18")
    k.land_verification(VerificationResult(
        node_id="v1", epoch=k.epoch, passed=True, detail="date updated"))
    k.commit_checkpoint("C1", "会议已改期并验证")
    _run_action(k, "l1", "copy_deadline", "2026-08-18")

    port = FakeArchitectPort(RECOMPOSE_JSON)
    ledger = ModelCallLedger()
    svc = GovernanceService(k, architect=TaskArchitect(port, ledger),
                            ledger=ledger)

    out = svc.handle(GoalPatchRequested(
        new_intent=TaskIntent(goal="把发布会议推迟到 2026-08-20",
                              success_criteria=("会议日期为 2026-08-20",)),
        rationale="不要 18 日了，改 20 日"))

    assert out.handled == "GoalPatchRequested"
    assert out.detail["architect_calls"] == 1 and port.calls == 1
    assert ledger.counts_by_role().get("task_architect") == 1
    assert out.detail["carried_history_nodes"] >= 3, (
        "committed history (a1/v1/l1 + structural closure) survives verbatim")

    statuses = k.workflow().statuses
    assert statuses["a1"] is NodeStatus.COMMITTED
    assert statuses["l1"] is NodeStatus.COMMITTED
    labels = {n.label for n in k.workflow().graph.nodes}
    assert "改会议日期" in labels and "同步文案" in labels, (
        "carried nodes keep their frozen labels/definitions")
    assert k.pending_recompose is None, "the transition closed atomically"
    # no split-brain: the new desired plane matches the new future writers
    assert k.task_state().variable("release_date").desired == "2026-08-20"
    assert k.task_state().variable("qa_deadline").desired == "2026-08-20"


def test_scenario_4_goal_patch_failure_is_honest_and_blocked():
    """Architect failure ⇒ GoalRecomposeFailed, execution stays BLOCKED,
    no fallback plan is installed."""
    k = _kernel()
    _run_action(k, "a1", "release_date", "2026-08-18")

    port = FakeArchitectPort({"nonsense": True}, {"also_nonsense": 1})
    ledger = ModelCallLedger()
    svc = GovernanceService(k, architect=TaskArchitect(port, ledger,
                                                       max_repairs=1),
                            ledger=ledger)
    with pytest.raises(GoalRecomposeFailed, match="BLOCKED"):
        svc.handle(GoalPatchRequested(
            new_intent=TaskIntent(goal="改到 2026-08-20")))
    assert k.pending_recompose is not None, "failure is inspectable, not hidden"
    with pytest.raises(ValidationError, match="recompose"):
        k.request_action("l1"), "execution stays blocked until closure"
    # nothing was silently installed
    assert k.workflow().graph.node("l2").label == "同步测试"


# ── scenario 5: Rollback — 0 model calls, history-driven plan ──────────────
def test_scenario_5_rollback_zero_calls_history_driven():
    k = _kernel()
    _run_action(k, "a1", "release_date", "2026-08-18")
    k.land_verification(VerificationResult(
        node_id="v1", epoch=k.epoch, passed=True, detail="date updated"))
    rec = k.commit_checkpoint("C1", "会议已改期并验证")
    _run_action(k, "l1", "copy_deadline", "2026-08-18")

    port = FakeArchitectPort()          # would explode if ever consulted
    ledger = ModelCallLedger()
    svc = GovernanceService(k, architect=TaskArchitect(port, ledger),
                            ledger=ledger)

    out = svc.handle(RollbackRequested(
        target_checkpoint_id=rec.checkpoint_id, rationale="回到 C1"))

    assert out.handled == "RollbackRequested"
    assert out.detail["architect_calls"] == 0
    assert out.detail["compiler_calls"] == 0
    assert port.calls == 0 and ledger.total() == 0
    assert out.compensation_plan is not None
    # LIFO over what TaskVM ACTUALLY committed after C1: l1 (a1 predates it)
    entries = [(e.node_id, e.semantic_key, e.to_observed)
               for e in out.compensation_plan.entries]
    assert entries == [("l1", "copy_deadline", "2026-08-14")], (
        "the plan derives from committed action history, not a world diff")


# ── the remaining events route with zero model involvement ─────────────────
def test_pause_resume_conflict_route_without_model():
    k = _kernel()
    port = FakeArchitectPort()
    svc = GovernanceService(k, architect=TaskArchitect(port),
                            ledger=ModelCallLedger())

    out = svc.handle(PauseRequested(rationale="喝口水"))
    assert out.handled == "PauseRequested" and out.detail["paused"] is True
    out = svc.handle(ResumeRequested(rationale="继续"))
    assert out.handled == "ResumeRequested" and out.detail["resumed"] is True
    out = svc.handle(ConflictResolutionRequested(
        description="文案截止与测试截止冲突",
        semantic_keys=("copy_deadline", "qa_deadline"),
        resolution="keep_projected"))
    assert out.handled == "ConflictResolutionRequested"
    assert out.detail["conflict_id"]
    assert port.calls == 0


# ── the event vocabulary's own validity rules ───────────────────────────────
def test_event_validity_rules():
    with pytest.raises(ValidationError):
        LocalPatchRequested(updates={})            # empty local patch = scope
    with pytest.raises(ValidationError):
        RollbackRequested(target_checkpoint_id="")
    with pytest.raises(ValidationError):
        ConflictResolutionRequested(description="x", resolution="")
    # correlation ids thread through (the governance audit trail)
    ev = LocalPatchRequested(updates={"a": 1}, correlation_id="c-9")
    assert ev.correlation_id == "c-9"
