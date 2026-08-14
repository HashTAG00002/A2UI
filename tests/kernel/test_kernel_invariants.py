"""Kernel invariant + regression tests (v5 contract).

Invariants 1-7 from handoff 02; 8 (atomicity) and 9 (observed/desired
planes); 13/14 (composition coherence, one-shot/two-phase closure).
Adversarial temporal coverage: test_adversarial_contracts.py and
test_v4_audit_fixes.py; v5 timeline governance (pending-compensation
gate / COMPLETE truncation / PARTIAL): test_timeline_governance.py.
The composition CONTENT rules (unknown binding/key, split-brain) are
owned by TaskArchitecture — tests/domain/test_architecture.py; the
kernel-side tests here pin ATOMICITY of the rejection (invariant 8).
"""
import dataclasses

import pytest

from taskvm.domain import (
    ActionContract,
    CommittedNodeViolationError,
    CompensationEntryResult,
    CompensationPatch,
    CompensationResult,
    EventKind,
    GoalPatch,
    LocalPatch,
    NodeKind,
    NodeStatus,
    ObservedValue,
    PatchSemanticsError,
    ProjectionComponent,
    ProjectionSchema,
    SurfaceEvidence,
    SurfaceHandle,
    TaskIntent,
    TaskVariable,
    UnknownCheckpointError,
    ValidationError,
    VariableUpdate,
    VerificationResult,
    WorkflowGraph,
    WorkflowNode,
)
from taskvm.kernel import TaskVMKernel


def _intent(goal="把发布会议改到 8/18 并同步依赖任务"):
    return TaskIntent(goal=goal,
                      constraints=("不要改动已确认的安排",),
                      scope=("日历", "任务板"),
                      success_criteria=("会议日期为 2026-08-18",))


def _vars(desired="2026-08-18", release_observed="2026-08-14"):
    """Composition time: reality is 08-14, the goal is 08-18 → every
    variable starts in pending divergence (observed != desired)."""
    return (
        TaskVariable(semantic_key="release_date", label="发布日期",
                     observed=release_observed, desired=desired,
                     value_type="date"),
        TaskVariable(semantic_key="copy_deadline", label="文案截止",
                     observed="2026-08-14", desired=desired,
                     value_type="date"),
        TaskVariable(semantic_key="qa_deadline", label="测试截止",
                     observed="2026-08-14", desired=desired,
                     value_type="date"),
    )


def _contract(cid, key, value):
    return ActionContract(contract_id=cid,
                          semantic_goal=f"set {key} to {value}",
                          desired_state={key: value},
                          completion_condition=f"{key} shows {value}")


def _graph(date="2026-08-18"):
    """a1 → v1 → fanout(l1, l2) → b1 → cp1 → t1."""
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


def _verify(k, node_id, passed, h=None, detail=""):
    k.land_verification(VerificationResult(
        node_id=node_id, epoch=k.epoch, passed=passed,
        action_id=None if h is None else h["action_id"], detail=detail))


def _run_action(k, node_id, observations=()):
    h = k.request_action(node_id)
    k.start_action(h["action_id"])
    assert k.finish_action(h["action_id"], observations=observations)
    _verify(k, node_id, True, h, detail="observed match")


def _comp_success(plan, epoch, values=None):
    return CompensationResult.for_plan(plan, epoch=epoch, outcomes=[
        CompensationEntryResult(
            node_id=e.node_id, semantic_key=e.semantic_key,
            final_observed=((values or {}).get(e.semantic_key, e.to_observed)),
            compensated=True)
        for e in plan.entries])


def _obs(key, value, *evidence):
    return ObservedValue(semantic_key=key, value=value,
                         evidence=tuple(evidence))


# ── invariant 1: monotonic revisions ──────────────────────────────────────
def test_state_revisions_monotonic():
    k = _kernel()
    r0 = k.task_state().revision
    k.apply_observation([_obs("release_date", "2026-08-15")])
    r1 = k.task_state().revision
    k.apply_observation([_obs("release_date", "2026-08-16")])
    r2 = k.task_state().revision
    assert r0 < r1 < r2


# ── invariant 2: schema/data revisions independent ────────────────────────
def test_schema_and_data_revisions_are_independent():
    k = _kernel()
    rev0 = k.projection().revision
    k.apply_observation([_obs("release_date", "2026-08-15")])
    rev1 = k.projection().revision
    assert rev1.data_revision > rev0.data_revision
    assert rev1.schema_revision == rev0.schema_revision  # untouched by values


# ── invariant 3 + 14: GoalPatch two-phase closure ─────────────────────────
def test_goal_patch_preserves_committed_nodes():
    k = _kernel()
    _run_action(k, "a1", [_obs("release_date", "2026-08-18")])
    _verify(k, "v1", True)  # VERIFY: READY → COMMITTED directly
    out = k.apply_goal_patch(GoalPatch(patch_id="gp1",
                                       new_intent=_intent("改到 8/20")))
    assert out["requires_replan"] is True
    statuses = k.workflow().statuses
    assert statuses["a1"] is NodeStatus.COMMITTED     # history preserved
    assert statuses["v1"] is NodeStatus.COMMITTED
    assert statuses["l1"] is NodeStatus.INVALIDATED   # old future void
    # phase two: recompose carries history verbatim + installs the future
    carried = tuple(k.workflow().graph.node(nid) for nid in ("a1", "v1"))
    k.recompose(_vars(desired="2026-08-20", release_observed="2026-08-18"),
                reason="gp1 replan",
                new_graph=WorkflowGraph(nodes=carried + (
                    WorkflowNode(node_id="a2", kind=NodeKind.ACTION,
                                 label="改到 8/20", depends_on=("v1",),
                                 contract=_contract("c_a2", "release_date",
                                                    "2026-08-20")),
                    WorkflowNode(node_id="t2", kind=NodeKind.TERMINAL,
                                 label="完成", depends_on=("a2",)),
                )))
    statuses = k.workflow().statuses
    assert statuses["a1"] is NodeStatus.COMMITTED
    assert statuses["v1"] is NodeStatus.COMMITTED
    assert statuses["a2"] is NodeStatus.READY
    # no split-brain: desired plane and the future contract agree
    assert k.task_state().variable("release_date").desired == "2026-08-20"
    assert k.workflow().graph.node("a2").contract.desired_state[
        "release_date"] == "2026-08-20"


def test_committed_history_cannot_be_rewritten_via_recompose():
    k = _kernel()
    _run_action(k, "a1", [_obs("release_date", "2026-08-18")])
    k.apply_goal_patch(GoalPatch(patch_id="gp", new_intent=_intent("改到 8/20")))
    tampered = WorkflowGraph(nodes=(
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="篡改",
                     contract=_contract("c_x", "release_date", "2026-01-01")),
        WorkflowNode(node_id="a2", kind=NodeKind.ACTION, label="改到 8/20",
                     depends_on=("a1",),
                     contract=_contract("c_a2", "release_date", "2026-08-20")),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a2",)),
    ))
    with pytest.raises(CommittedNodeViolationError):
        k.recompose(_vars(desired="2026-08-20",
                          release_observed="2026-08-18"),
                    reason="replan", new_graph=tampered)


def test_committed_history_cannot_be_dropped_via_recompose():
    k = _kernel()
    _run_action(k, "a1", [_obs("release_date", "2026-08-18")])
    k.apply_goal_patch(GoalPatch(patch_id="gp", new_intent=_intent("改到 8/20")))
    dropped = WorkflowGraph(nodes=(
        WorkflowNode(node_id="a2", kind=NodeKind.ACTION, label="改到 8/20",
                     contract=_contract("c_a2", "release_date", "2026-08-20")),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a2",)),
    ))
    with pytest.raises(CommittedNodeViolationError):
        k.recompose(_vars(desired="2026-08-20",
                          release_observed="2026-08-18"),
                    reason="replan", new_graph=dropped)


# ── invariant 4: stale epoch results are discarded ─────────────────────────
def test_stale_epoch_action_result_is_discarded():
    k = _kernel()
    h = k.request_action("a1")
    k.start_action(h["action_id"])
    k.apply_goal_patch(GoalPatch(patch_id="gp1",
                                 new_intent=_intent("改到 8/20")))
    accepted = k.finish_action(h["action_id"],
                               observations=[_obs("release_date", "2026-08-18")])
    assert accepted is False
    assert k.task_state().variable("release_date").observed == "2026-08-14"
    kinds = [e.kind for e in k.events()]
    assert EventKind.ACTION_DISCARDED in kinds
    assert EventKind.ACTION_FINISHED not in kinds
    # the node belongs to the voided future now — never silently re-armed
    assert k.workflow().statuses["a1"] is NodeStatus.INVALIDATED


def test_current_epoch_action_result_lands():
    k = _kernel()
    h = k.request_action("a1")
    k.start_action(h["action_id"])
    assert k.finish_action(h["action_id"],
                           observations=[_obs("release_date", "2026-08-18")])
    assert k.task_state().variable("release_date").observed == "2026-08-18"


# ── invariant 5: checkpoints pin a boundary ────────────────────────────────
def test_checkpoint_pins_event_revision_boundary():
    k = _kernel()
    _run_action(k, "a1", [_obs("release_date", "2026-08-18")])
    n_events = len(k.events())
    rec = k.commit_checkpoint("C1", "会议已改期")
    assert rec.checkpoint_id == "ckpt:C1"
    assert rec.event_index == n_events
    assert rec.state_revision == k.task_state().revision
    assert rec.observed["release_date"] == "2026-08-18"
    assert rec.desired["release_date"] == "2026-08-18"
    assert "a1" in rec.committed_nodes
    with pytest.raises(UnknownCheckpointError):
        k.request_compensation(CompensationPatch(
            patch_id="cp_x", target_checkpoint_id="ckpt:C99"))


# ── invariant 6: compensation = committed action history, verified ────────
def test_compensation_patch_carries_no_caller_supplied_history():
    """The spoof surface is ELIMINATED: CompensationPatch has exactly one
    payload field (the target checkpoint). Passing any history map is a
    TypeError, and {} / partial maps cannot vacuously pass."""
    fields = {f.name for f in dataclasses.fields(CompensationPatch)}
    assert fields == {"patch_id", "rationale", "correlation_id",
                      "created_at", "target_checkpoint_id"}
    with pytest.raises(TypeError):
        CompensationPatch(patch_id="x", target_checkpoint_id="C1",
                          observed_before={})


def _kernel_with_post_checkpoint_commit():
    """C1 taken after a1+v1; lane l1 commits AFTER the checkpoint."""
    k = _kernel()
    _run_action(k, "a1", [_obs("release_date", "2026-08-18")])
    _verify(k, "v1", True)
    k.commit_checkpoint("C1", "会议已改期")
    _run_action(k, "l1", [_obs("copy_deadline", "2026-08-18")])
    return k


def test_compensation_failed_verdict_is_honestly_archived():
    """The runtime's verifier judged the reversion did NOT hold (its
    content call — the kernel never re-checks values): the landing is
    FAILED, the state is untouched, exactly one event is emitted."""
    k = _kernel_with_post_checkpoint_commit()
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb1", target_checkpoint_id="ckpt:C1"))
    assert {e.semantic_key: e.to_observed
            for e in plan.entries} == {"copy_deadline": "2026-08-14"}
    before = k.task_state()
    n_events = len(k.events())
    failed = CompensationResult.for_plan(plan, epoch=k.epoch, outcomes=[
        CompensationEntryResult(
            node_id=e.node_id, semantic_key=e.semantic_key,
            final_observed="2026-08-15",   # the verifier saw the WRONG value
            compensated=False)
        for e in plan.entries], detail="visible value does not match target")
    assert k.record_compensation_result(plan.plan_id, failed) == "failed"
    kinds = [e.kind for e in k.events()]
    assert EventKind.COMPENSATION_FAILED in kinds
    assert EventKind.COMPENSATION_APPLIED not in kinds
    assert k.task_state() == before        # state untouched
    assert len(k.events()) == n_events + 1  # exactly one event (the failure)


def test_compensation_without_full_coverage_cannot_complete():
    """COMPLETE requires every plan entry landed compensated; an entry the
    runtime never reported on counts as NOT undone."""
    k = _kernel_with_post_checkpoint_commit()
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb1", target_checkpoint_id="ckpt:C1"))
    # the runtime reports NOTHING for the only entry ⇒ nothing undone ⇒ FAILED
    empty_report = CompensationResult.for_plan(plan, epoch=k.epoch,
                                               outcomes=[])
    assert k.record_compensation_result(plan.plan_id, empty_report) == "failed"
    assert EventKind.COMPENSATION_APPLIED not in [e.kind for e in k.events()]


def test_applied_compensation_restores_and_rearms():
    k = _kernel_with_post_checkpoint_commit()
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb1", target_checkpoint_id="ckpt:C1"))
    assert k.record_compensation_result(
        plan.plan_id, _comp_success(plan, k.epoch)) == "complete"
    v = k.task_state().variable("copy_deadline")
    assert v.observed == "2026-08-14" and v.desired == "2026-08-18"
    assert v.diverged is True               # honestly back to pending work
    # same intent/structure ⇒ deterministic frontier rewind: l1 re-armed
    statuses = k.workflow().statuses
    assert statuses["a1"] is NodeStatus.COMMITTED
    assert statuses["v1"] is NodeStatus.COMMITTED
    assert statuses["l1"] is NodeStatus.READY


# ── invariant 7: defensive copies ─────────────────────────────────────────
def test_snapshots_are_defensive_copies():
    k = _kernel()
    snap = k.projection()
    snap.data.values["release_date"]["observed"] = "HACKED"
    assert k.task_state().variable("release_date").observed == "2026-08-14"
    wf = k.workflow()
    wf.statuses["a1"] = NodeStatus.COMMITTED
    assert k.workflow().statuses["a1"] is NodeStatus.READY


# ── invariant 8: patch atomicity ───────────────────────────────────────────
def test_local_patch_failure_is_atomic():
    """A LocalPatch mixing a VALID update with an UNKNOWN variable must
    leave state/epoch/graph/events completely untouched."""
    k = _kernel()
    before_state, before_epoch = k.task_state(), k.epoch
    before_graph = k.workflow().graph
    n_events = len(k.events())
    with pytest.raises(PatchSemanticsError):
        k.apply_local_patch(LocalPatch(
            patch_id="lp_bad",
            variable_updates=(VariableUpdate("qa_deadline", "2026-08-19"),
                              VariableUpdate("ghost", "x"))))
    assert k.task_state() == before_state       # desired NOT half-updated
    assert k.epoch == before_epoch              # epoch NOT bumped
    assert k.workflow().graph == before_graph   # graph NOT touched
    assert len(k.events()) == n_events          # no half-event


def test_recompose_failure_is_atomic():
    """A recompose whose new_graph tampers with committed history must
    leave the (GoalPatch-updated) intent, epoch, graph, and events
    untouched."""
    k = _kernel()
    _run_action(k, "a1", [_obs("release_date", "2026-08-18")])
    k.apply_goal_patch(GoalPatch(patch_id="gp",
                                 new_intent=_intent("改到 8/20")))
    before_state, before_epoch = k.task_state(), k.epoch
    before_graph = k.workflow().graph
    n_events = len(k.events())
    tampered = WorkflowGraph(nodes=(
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="篡改",
                     contract=_contract("c_x", "release_date", "2026-01-01")),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a1",)),
    ))
    with pytest.raises(CommittedNodeViolationError):
        k.recompose(_vars(desired="2026-08-20",
                          release_observed="2026-08-18"),
                    reason="replan", new_graph=tampered)
    assert k.task_state() == before_state
    assert k.epoch == before_epoch
    assert k.workflow().graph == before_graph
    assert len(k.events()) == n_events
    assert k.pending_recompose is not None   # still awaiting a valid closure


# ── invariant 9: observed vs desired planes ────────────────────────────────
def test_observed_desired_divergence_during_inflight_execution():
    k = _kernel()
    v = k.task_state().variable("release_date")
    assert v.observed == "2026-08-14" and v.desired == "2026-08-18"
    assert v.diverged is True
    # projection exposes the pending divergence honestly
    proj = k.projection().data.values["release_date"]
    assert proj == {"observed": "2026-08-14", "desired": "2026-08-18",
                    "diverged": True}
    # a LocalPatch moves ONLY the desired plane
    k.apply_local_patch(LocalPatch(
        patch_id="lp1",
        variable_updates=(VariableUpdate("release_date", "2026-08-19"),)))
    v = k.task_state().variable("release_date")
    assert v.observed == "2026-08-14" and v.desired == "2026-08-19"
    # an observation moves ONLY the observed plane
    k.apply_observation([_obs("release_date", "2026-08-19")])
    v = k.task_state().variable("release_date")
    assert v.observed == "2026-08-19" and v.desired == "2026-08-19"
    assert v.diverged is False
    assert k.projection().data.values["release_date"]["diverged"] is False


def test_observation_never_writes_desired_and_patch_never_writes_observed():
    k = _kernel()
    k.apply_observation([_obs("release_date", "2026-08-15")])
    assert k.task_state().variable("release_date").desired == "2026-08-18"
    k.apply_local_patch(LocalPatch(
        patch_id="lp1",
        variable_updates=(VariableUpdate("release_date", "2026-08-20"),)))
    assert k.task_state().variable("release_date").observed == "2026-08-15"


# ── observation contract: evidence lands on the variable ──────────────────
def test_evidence_survives_observation():
    k = _kernel()
    ev = SurfaceEvidence(surface=SurfaceHandle(handle_id="h_cal_1"),
                         visible_label="项目发布会议",
                         visible_context="日历 · 8 月视图",
                         observed_value="2026-08-16", confidence=0.9)
    k.apply_observation([_obs("release_date", "2026-08-16", ev)])
    v = k.task_state().variable("release_date")
    assert v.observed == "2026-08-16"
    assert len(v.evidence) == 1
    assert v.evidence[0].visible_label == "项目发布会议"
    assert v.evidence[0].surface.handle_id == "h_cal_1"
    # a later observation WITHOUT evidence keeps the prior evidence
    k.apply_observation([_obs("release_date", "2026-08-17")])
    v = k.task_state().variable("release_date")
    assert v.observed == "2026-08-17"
    assert v.evidence[0].visible_label == "项目发布会议"
    # evidence from one variable never bleeds into another
    assert k.task_state().variable("copy_deadline").evidence == ()


def test_observation_cannot_invent_variables():
    k = _kernel()
    with pytest.raises(ValidationError):
        k.apply_observation([_obs("never_declared", "x")])


# ── recomposition: the ONLY structural entry ───────────────────────────────
def test_init_task_state_is_one_shot():
    k = _kernel()
    with pytest.raises(ValidationError, match="one-shot"):
        k.init_task_state(_vars())


def test_recompose_adds_removes_variables_and_syncs_projection():
    k = _kernel()
    old_epoch = k.epoch
    owner = TaskVariable(semantic_key="owner", label="负责人",
                         observed="Alice", desired="Bob")
    # add-only drift: the retained graph stays coherent (owner has no
    # writer; every retained contract target still matches desired)
    k.recompose(_vars() + (owner,), reason="owner surface appeared")
    state = k.task_state()
    assert state.variable("owner").desired == "Bob"
    assert k.epoch > old_epoch
    proj = k.projection().data.values
    assert proj["owner"]["diverged"] is True
    # dropping a variable the retained graph still references → rejected
    with pytest.raises(ValidationError, match="unknown task variables"):
        k.recompose(_vars()[:2] + (owner,), reason="qa surface gone")
    # with a matching new graph the same drop closes legitimately
    k.recompose(_vars()[:2] + (owner,), reason="qa surface gone",
                new_graph=WorkflowGraph(nodes=(
                    WorkflowNode(node_id="a1", kind=NodeKind.ACTION,
                                 label="改会议日期",
                                 contract=_contract("c_a1", "release_date",
                                                    "2026-08-18")),
                    WorkflowNode(node_id="l1", kind=NodeKind.ACTION,
                                 label="同步文案", depends_on=("a1",),
                                 contract=_contract("c_l1", "copy_deadline",
                                                    "2026-08-18")),
                    WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL,
                                 label="完成", depends_on=("l1",)),
                )))
    assert k.task_state().variable("qa_deadline") is None
    assert "qa_deadline" not in k.projection().data.values
    with pytest.raises(ValidationError):
        k.recompose(_vars(), reason="")      # reason required


def test_recompose_requires_init_first():
    k = TaskVMKernel(session_id="bare", intent=_intent())
    with pytest.raises(ValidationError):
        k.recompose(_vars(), reason="too early")


# ── projection: authoritative replace (no stale keys) ─────────────────────
def test_removed_workflow_nodes_disappear_from_projection():
    k = _kernel()
    assert "l2" in k.projection().data.node_status
    _run_action(k, "a1", [_obs("release_date", "2026-08-18")])
    _verify(k, "v1", True)
    k.apply_goal_patch(GoalPatch(patch_id="gp1",
                                 new_intent=_intent("改到 8/20")))
    carried = tuple(k.workflow().graph.node(nid) for nid in ("a1", "v1"))
    k.recompose(_vars(desired="2026-08-20", release_observed="2026-08-18"),
                reason="gp1 replan",
                new_graph=WorkflowGraph(nodes=carried + (
                    WorkflowNode(node_id="a2", kind=NodeKind.ACTION,
                                 label="改到 8/20", depends_on=("v1",),
                                 contract=_contract("c_a2", "release_date",
                                                    "2026-08-20")),
                    WorkflowNode(node_id="t2", kind=NodeKind.TERMINAL,
                                 label="完成", depends_on=("a2",)),
                )))
    ns = k.projection().data.node_status
    assert "l2" not in ns and "b1" not in ns and "t1" not in ns
    assert ns["a1"] == "committed" and ns["a2"] == "ready"


# ── patch routing semantics ────────────────────────────────────────────────
def test_local_patch_rejects_unknown_variable():
    k = _kernel()
    with pytest.raises(PatchSemanticsError):
        k.apply_local_patch(LocalPatch(
            patch_id="lp_bad",
            variable_updates=(VariableUpdate("brand_new_var", "x"),)))


def test_wrong_patch_type_rejected():
    k = _kernel()
    with pytest.raises(PatchSemanticsError):
        k.apply_local_patch(GoalPatch(patch_id="gp_wrong"))
    with pytest.raises(PatchSemanticsError):
        k.apply_goal_patch(LocalPatch(
            patch_id="lp_wrong",
            variable_updates=(VariableUpdate("release_date", "x"),)))


def test_projection_progress_tracks_commits():
    k = _kernel()
    _run_action(k, "a1", [_obs("release_date", "2026-08-18")])
    data = k.projection().data
    assert data.node_status["a1"] == "committed"
    assert 0.0 < data.progress < 1.0
