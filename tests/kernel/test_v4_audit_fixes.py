"""Wave-A.2 (v4) audit groups, migrated to the v5 typed contract.

Each group was originally written to FAIL against the v3 kernel. What
changed in v5 (layered ownership): verification/compensation land as
TYPED results; checkpoint ids are namespaced; the content-rule groups
moved to their single owner —
  G4's NodeContractOverride surface check → tests/domain/test_static_shapes.py
  G11 workflow primitive shapes         → tests/domain/test_static_shapes.py
  G13b duplicate observation keys       → tests/domain/test_static_shapes.py
                                          (the kernel-side wiring stays here)
  G9's id-collision guard               → replaced by the ckpt: namespace
                                          (tests/kernel/test_timeline_governance.py)
Groups:
  G1  VERIFY failure must enter FAILED; only VERIFY may READY→FAILED.
  G2  ACTION verification requires a FINISHED handle in the current epoch.
  G3  GoalPatch is a closed two-phase transition.
  G4  LocalPatch single source of truth + deterministic retarget.
  G5  Compensation derives from the COMMITTED action history.
  G6  A successful compensation deterministically rewinds the frontier.
  G7  A workflow CHECKPOINT node is part of its own boundary record.
  G8  set_plan is one-shot.
  G9  Checkpoints require a stable action boundary.
  G10 Write-path defensive copies.
  G12 Structure comparison includes label/type/mutability.
  G13 initialized flag / requeue kind gate / ephemeral loop-body commits.
"""
import pytest

from taskvm.domain import (
    ActionContract,
    CompensationPatch,
    EventKind,
    GoalPatch,
    LocalPatch,
    MUTABILITY_READONLY,
    NodeKind,
    NodeStatus,
    ObservedValue,
    Reversibility,
    TaskIntent,
    TaskVariable,
    ValidationError,
    VariableUpdate,
    VerificationResult,
    WorkflowGraph,
    WorkflowNode,
)
from taskvm.domain.results import CompensationEntryResult, CompensationResult
from taskvm.kernel import TaskVMKernel


# ── helpers ────────────────────────────────────────────────────────────────
def _obs(key, value):
    return ObservedValue(semantic_key=key, value=value)


def _var(key, observed, desired, **kw):
    return TaskVariable(semantic_key=key, label=kw.pop("label", key),
                        observed=observed, desired=desired, **kw)


def _contract(cid, key, value, **kw):
    return ActionContract(contract_id=cid,
                          semantic_goal=f"set {key} to {value}",
                          desired_state={key: value},
                          completion_condition=f"{key} visibly shows {value}",
                          **kw)


def _action_node(nid, key, value, **kw):
    return WorkflowNode(node_id=nid, kind=NodeKind.ACTION, label=nid,
                        contract=_contract(f"c_{nid}", key, value), **kw)


def _terminal(nid, *deps):
    return WorkflowNode(node_id=nid, kind=NodeKind.TERMINAL, label="完成",
                        depends_on=tuple(deps))


def _kernel(variables, graph=None, goal="g"):
    k = TaskVMKernel(session_id="v4", intent=TaskIntent(goal=goal))
    k.init_task_state(variables)
    if graph is not None:
        k.set_plan(graph)
    return k


def _verify(k, node_id, passed, h=None, detail=""):
    k.land_verification(VerificationResult(
        node_id=node_id, epoch=k.epoch, passed=passed,
        action_id=None if h is None else h["action_id"], detail=detail))


def _run_action(k, node_id, key, value):
    h = k.request_action(node_id)
    assert k.start_action(h["action_id"])
    assert k.finish_action(h["action_id"], observations=[_obs(key, value)])
    _verify(k, node_id, True, h)


def _comp_success(plan, epoch, values=None):
    return CompensationResult.for_plan(plan, epoch=epoch, outcomes=[
        CompensationEntryResult(
            node_id=e.node_id, semantic_key=e.semantic_key,
            final_observed=((values or {}).get(e.semantic_key, e.to_observed)),
            compensated=True)
        for e in plan.entries])


# ══ G1: VERIFY failure path ═══════════════════════════════════════════════
def test_verify_failure_enters_failed_then_requeue_recovers():
    g = WorkflowGraph(nodes=(
        _action_node("a1", "x", 2),
        WorkflowNode(node_id="v1", kind=NodeKind.VERIFY, label="验证",
                     depends_on=("a1",), verification="x updated"),
        _terminal("t", "v1"),
    ))
    k = _kernel((_var("x", 1, 2),), g)
    _run_action(k, "a1", "x", 2)
    assert k.workflow().statuses["v1"] is NodeStatus.READY
    _verify(k, "v1", False, detail="visible value wrong")
    assert k.workflow().statuses["v1"] is NodeStatus.FAILED   # not an exception
    k.requeue("v1")
    assert k.workflow().statuses["v1"] is NodeStatus.READY
    _verify(k, "v1", True)
    assert k.workflow().statuses["v1"] is NodeStatus.COMMITTED


def test_action_cannot_fail_directly_from_ready():
    k = _kernel((_var("x", 1, 2),),
                WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                     _terminal("t", "a1"))))
    with pytest.raises(ValidationError):
        _verify(k, "a1", False,
                h={"action_id": "action:00001"})   # no FINISHED handle at all


# ══ G2: ACTION verify requires a FINISHED handle (current epoch) ══════════
def test_action_verification_requires_finished_handle():
    k = _kernel((_var("x", 1, 2),),
                WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                     _terminal("t", "a1"))))
    h = k.request_action("a1")
    k.start_action(h["action_id"])
    # the frozen protocol is request → start → finish → verify: skipping
    # finish must NOT be committable
    with pytest.raises(ValidationError):
        _verify(k, "a1", True, h)
    assert k.workflow().statuses["a1"] is NodeStatus.RUNNING
    k.finish_action(h["action_id"], observations=[_obs("x", 2)])
    _verify(k, "a1", True, h)
    assert k.workflow().statuses["a1"] is NodeStatus.COMMITTED


def test_finished_handle_from_dead_epoch_does_not_count():
    k = _kernel((_var("x", 1, 2),),
                WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                     _terminal("t", "a1"))))
    h = k.request_action("a1")
    k.start_action(h["action_id"])
    k.finish_action(h["action_id"], observations=[_obs("x", 2)])
    # hot interrupt: epoch bump resets the RUNNING node to READY
    k.request_governance("pause", "user paused")
    k.request_governance("resume")
    assert k.workflow().statuses["a1"] is NodeStatus.READY
    h2 = k.request_action("a1")
    assert h2["epoch"] > h["epoch"]
    k.start_action(h2["action_id"])
    # the NEW generation has not finished — old finish must not authorise
    with pytest.raises(ValidationError):
        _verify(k, "a1", True, h2)


# ══ G3: GoalPatch two-phase closure ═══════════════════════════════════════
def _two_step_graph(date="2026-08-18"):
    return WorkflowGraph(nodes=(
        _action_node("a1", "release_date", date),
        WorkflowNode(node_id="v1", kind=NodeKind.VERIFY, label="验证",
                     depends_on=("a1",), verification="date updated"),
        _terminal("t1", "v1"),
    ))


def test_goal_patch_no_longer_installs_graph_or_schema():
    k = _kernel((_var("release_date", "2026-08-14", "2026-08-18"),),
                _two_step_graph())
    with pytest.raises(TypeError):
        k.apply_goal_patch(GoalPatch(patch_id="gp"),
                           new_graph=_two_step_graph("2026-08-20"))
    with pytest.raises(TypeError):
        k.apply_goal_patch(GoalPatch(patch_id="gp"), new_schema=None)


def test_goal_patch_invalidates_future_and_blocks_execution():
    k = _kernel((_var("release_date", "2026-08-14", "2026-08-18"),),
                _two_step_graph())
    _run_action(k, "a1", "release_date", "2026-08-18")
    out = k.apply_goal_patch(GoalPatch(
        patch_id="gp", new_intent=TaskIntent(goal="改到 8/20")))
    assert out["requires_replan"] is True
    statuses = k.workflow().statuses
    assert statuses["a1"] is NodeStatus.COMMITTED        # history kept
    assert statuses["v1"] is NodeStatus.INVALIDATED      # old future void
    assert statuses["t1"] is NodeStatus.INVALIDATED
    # execution is BLOCKED until recompose closes the loop
    with pytest.raises(ValidationError, match="recompose"):
        k.request_action("a1")
    with pytest.raises(ValidationError, match="recompose"):
        k.advance_control("t1")
    with pytest.raises(ValidationError, match="recompose"):
        k.commit_checkpoint("C9", "mid-replan")
    # recompose closes it: committed history carried, new future installed
    carried = k.workflow().graph.node("a1")
    k.recompose((_var("release_date", "2026-08-18", "2026-08-20"),),
                reason="GoalPatch gp replan",
                new_graph=WorkflowGraph(nodes=(
                    carried,
                    _action_node("a2", "release_date", "2026-08-20",
                                 depends_on=("a1",)),
                    _terminal("t2", "a2"),
                )))
    assert k.workflow().statuses["a2"] is NodeStatus.READY
    h = k.request_action("a2")                            # unblocked
    assert h["contract"].desired_state["release_date"] == "2026-08-20"


def test_goal_patch_recompose_kills_split_brain():
    """After GoalPatch + recompose, TaskVariable.desired and the future
    ActionContracts MUST agree — no intent=20 / workflow=20 / desired=18."""
    k = _kernel((_var("release_date", "2026-08-14", "2026-08-18"),),
                _two_step_graph())
    k.apply_goal_patch(GoalPatch(patch_id="gp",
                                 new_intent=TaskIntent(goal="改到 8/20")))
    # split-brain attempt: variables say 8/20 but the contract says 8/18
    with pytest.raises(ValidationError):
        k.recompose((_var("release_date", "2026-08-18", "2026-08-20"),),
                    reason="replan",
                    new_graph=WorkflowGraph(nodes=(
                        k.workflow().graph.node("a1"),
                        _action_node("a2", "release_date", "2026-08-18",
                                     depends_on=("a1",)),
                        _terminal("t2", "a2"),
                    )))
    # and the atomically rejected recompose changed nothing
    assert k.task_state().variable("release_date").desired == "2026-08-18"


def test_split_brain_guard_compares_literals_not_python_types():
    """GATE-G0 r10 postmortem (2026-08-20): the architect model emits
    ``desired: true`` (JSON bool) in the variable list and
    ``sets: {"post_liked": "true"}`` (JSON string) in the action contract —
    the SAME literal in two Python types. The guard compared raw and
    rejected the coherent plan 4 attempts in a row (5 of 7 attempts
    spelled the target as a string). Literal normalization makes bool ≡
    its string spelling, exactly like the verifier's value matching."""
    k = _kernel((_var("post_liked", None, True),),
                WorkflowGraph(nodes=(
                    _action_node("a1", "post_liked", "true"),   # str target
                    _terminal("t", "a1"),
                )))
    # the composition VALIDATED (kernel construction proves it); the two
    # planes carry the same literal
    assert k.task_state().variable("post_liked").desired is True


def test_split_brain_guard_still_rejects_real_divergence():
    """The normalization is not a free pass: a writer targeting the
    OPPOSITE boolean is still a genuine split brain."""
    with pytest.raises(ValidationError, match="split-brain"):
        _kernel((_var("post_liked", None, True),),
                WorkflowGraph(nodes=(
                    _action_node("a1", "post_liked", "false"),
                    _terminal("t", "a1"),
                )))


def test_recompose_after_goal_patch_requires_new_graph():
    k = _kernel((_var("release_date", "2026-08-14", "2026-08-18"),),
                _two_step_graph())
    k.apply_goal_patch(GoalPatch(patch_id="gp",
                                 new_intent=TaskIntent(goal="改到 8/20")))
    with pytest.raises(ValidationError, match="new_graph"):
        k.recompose((_var("release_date", "2026-08-18", "2026-08-20"),),
                    reason="replan without a plan")


def test_partial_recompose_validates_the_RETAINED_graph():
    """Structure drift recompose without a new graph is only legal when the
    retained graph/schema still closes over the new variable set."""
    k = _kernel((_var("x", 1, 2), _var("y", 1, 2)),
                WorkflowGraph(nodes=(
                    _action_node("a1", "x", 2),
                    _action_node("a2", "y", 2, depends_on=("a1",)),
                    _terminal("t", "a2"),
                )))
    # dropping y while a2's contract still targets it → dangling → reject
    with pytest.raises(ValidationError, match="unknown task variables"):
        k.recompose((_var("x", 1, 2),), reason="y surface gone")
    # nothing changed (atomic)
    assert k.task_state().variable("y") is not None
    # supplying the matching new graph closes it legitimately
    k.recompose((_var("x", 1, 2),), reason="y surface gone",
                new_graph=WorkflowGraph(nodes=(
                    _action_node("a1", "x", 2),
                    _terminal("t", "a1"),
                )))
    assert k.task_state().variable("y") is None
    assert k.workflow().graph.node("a2") is None


# ══ G4: LocalPatch single source of truth ═════════════════════════════════
def test_local_patch_retargets_uncommitted_contracts_deterministically():
    k = _kernel((_var("release_date", "2026-08-14", "2026-08-18"),
                 _var("qa_deadline", "2026-08-14", "2026-08-18")),
                WorkflowGraph(nodes=(
                    _action_node("a1", "release_date", "2026-08-18"),
                    _action_node("l2", "qa_deadline", "2026-08-18",
                                 depends_on=("a1",)),
                    _terminal("t", "l2"),
                )))
    k.apply_local_patch(LocalPatch(
        patch_id="lp",
        variable_updates=(VariableUpdate("qa_deadline", "2026-08-18T16:00"),)))
    # the future contract now carries the NEW target — runtime can never
    # receive the stale 15:00-equivalent target
    c = k.workflow().graph.node("l2").contract
    assert c.desired_state["qa_deadline"] == "2026-08-18T16:00"
    # committed history is NOT retargeted
    k2 = _kernel((_var("release_date", "2026-08-14", "2026-08-19"),),
                 WorkflowGraph(nodes=(
                     _action_node("a1", "release_date", "2026-08-18"),
                     _action_node("a2", "release_date", "2026-08-19",
                                  depends_on=("a1",)),
                     _terminal("t", "a2"),
                 )))
    _run_action(k2, "a1", "release_date", "2026-08-18")
    k2.apply_local_patch(LocalPatch(
        patch_id="lp2",
        variable_updates=(VariableUpdate("release_date", "2026-08-21"),)))
    assert k2.workflow().graph.node("a1").contract.desired_state[
        "release_date"] == "2026-08-18"          # history untouched
    assert k2.workflow().graph.node("a2").contract.desired_state[
        "release_date"] == "2026-08-21"          # future retargeted
    assert k2.task_state().variable("release_date").desired == "2026-08-21"


# ══ G5: action-history-based compensation ═════════════════════════════════
def test_compensation_entries_come_from_committed_action_history():
    """A variable ADDED by a post-checkpoint recompose and CHANGED BY
    TASKVM gets an inverse entry (its before was recorded when the action
    started) — a snapshot-diff plan misses it."""
    k = _kernel((_var("x", 1, 2),),
                WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                     _terminal("t", "a1"))))
    k.commit_checkpoint("C0", "boundary")          # C0 knows only x=1
    # GoalPatch + recompose introduces z (with a new plan touching it)
    k.apply_goal_patch(GoalPatch(patch_id="gp",
                                 new_intent=TaskIntent(goal="新目标含 z")))
    k.recompose((_var("x", 1, 1), _var("z", None, 9)),
                reason="replan with z",
                new_graph=WorkflowGraph(nodes=(
                    _action_node("a2", "z", 9),
                    _terminal("t2", "a2"),
                )))
    _run_action(k, "a2", "z", 9)                    # TaskVM really wrote z
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:C0"))
    by_key = {e.semantic_key: e for e in plan.entries}
    assert "z" in by_key                            # the inverse EXISTS now
    assert by_key["z"].to_observed is None          # z before TaskVM: absent
    # runtime executes the inverse and re-observes
    assert k.record_compensation_result(
        plan.plan_id, _comp_success(plan, k.epoch)) == "complete"
    # z is NOT logically deleted — its observed plane honestly shows the
    # restored absence; the variable stays visible until recompose decides
    assert k.task_state().variable("z") is not None
    assert k.task_state().variable("z").observed is None


def test_irreversible_action_is_never_fake_reverted():
    g = WorkflowGraph(nodes=(
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="发送公告",
                     contract=_contract("c_a1", "announcement", "已发送",
                                        reversibility=Reversibility.IRREVERSIBLE)),
        _terminal("t", "a1"),
    ))
    k = _kernel((_var("announcement", "草稿", "已发送"),), g)
    k.commit_checkpoint("C0", "发送前")
    _run_action(k, "a1", "announcement", "已发送")   # IRREVERSIBLE commit
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:C0"))
    assert plan.entries == ()                        # NO fake "set it back"
    assert [b.node_id for b in plan.uncompensatable] == ["a1"]
    # v5 rollback closure: standing uncompensatable work means the
    # timeline is NOT fully back at the boundary — honest PARTIAL,
    # never a fake COMPLETE (§4.11)
    assert k.record_compensation_result(
        plan.plan_id, _comp_success(plan, k.epoch)) == "partial"
    # the node is honestly NOT compensated — the send still stands
    assert k.workflow().statuses["a1"] is NodeStatus.COMMITTED
    assert k.task_state().variable("announcement").observed == "已发送"
    ev = [e for e in k.events()
          if e.kind is EventKind.COMPENSATION_PARTIAL][-1]
    assert "a1" in ev.payload["uncompensatable_nodes"]
    # PARTIAL waits for governance (recompose) before forward autonomy
    assert k.pending_recompose is not None


def test_external_drift_is_not_rolled_back():
    """Reality moving WITHOUT a TaskVM action is not TaskVM's to undo."""
    k = _kernel((_var("x", 1, 2),),
                WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                     _terminal("t", "a1"))))
    k.commit_checkpoint("C0", "boundary")
    k.apply_observation([_obs("x", 5)])              # the WORLD moved x
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:C0"))
    assert plan.entries == ()                        # nothing to compensate
    assert k.record_compensation_result(
        plan.plan_id, _comp_success(plan, k.epoch)) == "complete"
    assert k.task_state().variable("x").observed == 5   # drift kept, honest


def test_multiple_writes_restore_through_history_lifo():
    k = _kernel((_var("x", 1, 3),),
                WorkflowGraph(nodes=(
                    _action_node("a1", "x", 2),
                    _action_node("a2", "x", 3, depends_on=("a1",)),
                    _terminal("t", "a2"),
                )))
    k.commit_checkpoint("C0", "x=1")
    _run_action(k, "a1", "x", 2)
    _run_action(k, "a2", "x", 3)
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:C0"))
    # LIFO: undo a2 (3→2) first, then a1 (2→1); final resting value 1
    assert [(e.node_id, e.from_observed, e.to_observed)
            for e in plan.entries] == [("a2", 3, 2), ("a1", 2, 1)]
    assert k.record_compensation_result(
        plan.plan_id, _comp_success(plan, k.epoch)) == "complete"
    assert k.task_state().variable("x").observed == 1


# ══ GATE-G0 r9 postmortem (2026-08-20) ═══════════════════════════════════
def test_readonly_observation_deepening_never_compensates():
    """GATE-G0 r9: ``target_post_text`` went null → full text after the
    'find the post' action. That is the world becoming VISIBLE (observation
    deepening), never a system write — spawning a compensation entry for
    it burned a whole rollback round telling the CUA to "restore the post
    text to None"."""
    k = _kernel((_var("target_post_text", None, "核心CPI意外下降",
                      mutability=MUTABILITY_READONLY),),
                WorkflowGraph(nodes=(
                    _action_node("a1", "target_post_text", "核心CPI意外下降"),
                    _terminal("t", "a1"),
                )))
    k.commit_checkpoint("C0", "搜索前")
    _run_action(k, "a1", "target_post_text",
                "扣除食物和能源的核心CPI意外下降")
    assert k.task_state().variable("target_post_text").observed == (
        "扣除食物和能源的核心CPI意外下降")
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:C0"))
    assert plan.entries == ()          # observation deepening ≠ a write


def test_verified_write_blind_to_observed_plane_still_compensates():
    """GATE-G0 r9: a like toggle is icon COLOR, not visible text — the
    observed plane reads null BEFORE and AFTER the action. The ACTION's
    own verification (deterministic or the R4 screenshot-grounded model
    verifier) confirmed the desired value landed; that verified value is
    the write the compensation must undo (r9's rollback skipped the like
    undo because before==after==None)."""
    k = _kernel((_var("target_post_liked", None, True),),
                WorkflowGraph(nodes=(
                    _action_node("a1", "target_post_liked", True),
                    _terminal("t", "a1"),
                )))
    k.commit_checkpoint("C0", "点赞前")
    h = k.request_action("a1")
    assert k.start_action(h["action_id"])
    # the text plane is blind: NO observation folds — observed stays None
    assert k.finish_action(h["action_id"], observations=())
    _verify(k, "a1", True, h)          # the visual verifier confirmed it
    assert k.task_state().variable("target_post_liked").observed is None
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:C0"))
    by_key = {e.semantic_key: e for e in plan.entries}
    assert "target_post_liked" in by_key          # the like DOES undo now
    assert by_key["target_post_liked"].from_observed is True
    assert by_key["target_post_liked"].to_observed is None


# ══ G6: deterministic workflow rewind after compensation ═════════════════
def test_applied_compensation_rewinds_frontier_to_boundary():
    g = WorkflowGraph(nodes=(
        _action_node("a1", "x", 2),
        WorkflowNode(node_id="cp1", kind=NodeKind.CHECKPOINT, label="C1",
                     depends_on=("a1",)),
        _action_node("a2", "x", 3, depends_on=("cp1",)),
        _terminal("t", "a2"),
    ))
    k = _kernel((_var("x", 1, 3),), g)
    _run_action(k, "a1", "x", 2)
    rec = k.advance_control("cp1")
    assert rec is not None
    _run_action(k, "a2", "x", 3)
    assert k.workflow().statuses["t"] is NodeStatus.READY   # dangerous frontier
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:cp1"))
    assert k.record_compensation_result(
        plan.plan_id, _comp_success(plan, k.epoch)) == "complete"
    statuses = k.workflow().statuses
    assert statuses["a1"] is NodeStatus.COMMITTED     # pre-boundary history
    assert statuses["cp1"] is NodeStatus.COMMITTED    # the boundary itself
    assert statuses["t"] is NodeStatus.PENDING        # NOT wrongly ready
    with pytest.raises(ValidationError):
        k.advance_control("t")                        # cannot falsely finish
    # the same path is deterministically re-armed (no architect needed)
    assert statuses["a2"] is NodeStatus.READY
    ev = [e for e in k.events()
          if e.kind is EventKind.COMPENSATION_APPLIED][-1]
    assert "a2" in ev.payload["compensated_nodes"]    # audit trail in the log


# ══ G7: CHECKPOINT node is part of its own boundary ═══════════════════════
def test_workflow_checkpoint_includes_itself_in_boundary():
    g = WorkflowGraph(nodes=(
        _action_node("a1", "x", 2),
        WorkflowNode(node_id="cp1", kind=NodeKind.CHECKPOINT, label="C1",
                     depends_on=("a1",)),
        _action_node("a2", "x", 3, depends_on=("cp1",)),
        _terminal("t", "a2"),
    ))
    k = _kernel((_var("x", 1, 3),), g)
    _run_action(k, "a1", "x", 2)
    rec = k.advance_control("cp1")
    assert "cp1" in rec.committed_nodes     # itself, not just its predecessors
    _run_action(k, "a2", "x", 3)
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:cp1"))
    k.record_compensation_result(plan.plan_id, _comp_success(plan, k.epoch))
    # the checkpoint node must NOT be judged 'after the checkpoint'
    assert k.workflow().statuses["cp1"] is NodeStatus.COMMITTED


# ══ G8: set_plan is one-shot ═══════════════════════════════════════════════
def test_set_plan_is_one_shot():
    k = _kernel((_var("x", 1, 2),),
                WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                     _terminal("t", "a1"))))
    _run_action(k, "a1", "x", 2)
    with pytest.raises(ValidationError, match="one-shot"):
        k.set_plan(WorkflowGraph(nodes=(_action_node("a9", "x", 9),
                                        _terminal("t9", "a9"))))
    # execution history is untouched
    assert k.workflow().statuses["a1"] is NodeStatus.COMMITTED


# ══ G9: checkpoint stability (the id-collision guard is replaced by the ════
# ckpt: namespace — see test_timeline_governance.py) ════════════════════════
def test_checkpoint_rejected_while_action_in_flight():
    k = _kernel((_var("x", 1, 2),),
                WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                     _terminal("t", "a1"))))
    h = k.request_action("a1")
    k.start_action(h["action_id"])
    with pytest.raises(ValidationError, match="in-flight"):
        k.commit_checkpoint("C1", "unstable")
    k.finish_action(h["action_id"], observations=[_obs("x", 2)])
    with pytest.raises(ValidationError, match="in-flight"):
        k.commit_checkpoint("C1", "still unstable")   # finished, unverified
    _verify(k, "a1", True, h)
    k.commit_checkpoint("C1", "stable boundary")      # now it lands


# ══ G10: write-path defensive copies ═══════════════════════════════════════
def test_write_paths_and_handles_are_aliasing_safe():
    # state write path
    nested = {"tags": ["a"]}
    v = _var("x", nested, {"tags": ["b"]})
    k = TaskVMKernel(session_id="alias", intent=TaskIntent(goal="g"))
    k.init_task_state((v,))
    nested["tags"].append("HACK")
    assert k.task_state().variable("x").observed == {"tags": ["a"]}
    # graph + contract write path
    c = _contract("c_a1", "x", {"tags": ["b"]})
    g = WorkflowGraph(nodes=(
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="a1",
                     contract=c),
        _terminal("t", "a1"),
    ))
    k.set_plan(g)
    c.desired_state["x"]["tags"].append("HACK")
    assert k.workflow().graph.node("a1").contract.desired_state[
        "x"] == {"tags": ["b"]}
    # returned handle path: mutating it must not reach the kernel's copy
    h = k.request_action("a1")
    h["contract"].desired_state["x"]["tags"].append("HACK")
    k.start_action(h["action_id"])
    k.finish_action(h["action_id"])     # folds nothing; the point is the store
    inner = k.workflow().graph.node("a1").contract.desired_state["x"]
    assert inner == {"tags": ["b"]}


# ══ G12: structure comparison includes metadata ═══════════════════════════
def test_structure_drift_detects_metadata_change_not_just_keys():
    k = _kernel((_var("release_date", "2026-08-14", "2026-08-14",
                      value_type="date"),),
                goal="g")
    k.commit_checkpoint("C0", "boundary")
    # same key set, different metadata (label/value_type/mutability)
    k.recompose((_var("release_date", "2026-08-14", "2026-08-14",
                      label="发布时刻", value_type="datetime",
                      mutability=MUTABILITY_READONLY),),
                reason="surface drift changed the widget")
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:C0"))
    assert plan.requires_recompose is True    # metadata drift IS structure drift
    assert k.record_compensation_result(
        plan.plan_id, _comp_success(plan, k.epoch)) == "complete"
    v = k.task_state().variable("release_date")
    assert v.label != "发布时刻"              # checkpoint metadata restored
    assert v.value_type == "date"
    assert v.mutability == "editable"


# ══ G13: the small holes (dup observation keys now proven at the domain ════
# constructor — tests/domain/test_static_shapes.py; the kernel wiring ═══════
# stays pinned here) ════════════════════════════════════════════════════════
def test_initialized_flag_not_empty_variables():
    k = TaskVMKernel(session_id="init", intent=TaskIntent(goal="g"))
    with pytest.raises(ValidationError):
        k.recompose((_var("x", 1, 2),), reason="too early")   # not initialised
    k.init_task_state(())                                     # legal: EMPTY init
    with pytest.raises(ValidationError, match="one-shot"):
        k.init_task_state((_var("x", 1, 2),))                 # still one-shot
    k.recompose((_var("x", 1, 2),), reason="first structure") # now legal
    assert k.task_state().variable("x").desired == 2


def test_observation_batch_rejects_duplicate_keys():
    k = _kernel((_var("x", 1, 2),))
    with pytest.raises(ValidationError, match="duplicate"):
        k.apply_observation([_obs("x", 3), _obs("x", 4)])
    assert k.task_state().variable("x").observed == 1         # no silent LWW


def test_requeue_is_gated_to_action_and_verify():
    g = WorkflowGraph(nodes=(
        WorkflowNode(node_id="L", kind=NodeKind.BOUNDED_LOOP, label="loop",
                     termination_predicate="done", max_iterations=1),
        _action_node("body", "x", 2, parent_id="L"),
        _terminal("t", "L"),
    ))
    k = _kernel((_var("x", 1, 2),), g)
    k.begin_loop_iteration("L")
    _run_action(k, "body", "x", 2)
    out = k.evaluate_loop_termination("L", False)   # hits max → FAILED
    assert out["outcome"] == "failed"
    with pytest.raises(ValidationError, match="ACTION/VERIFY"):
        k.requeue("L")   # a maxed-out loop needs governance, not a retry


def test_loop_body_ephemeral_commits_are_not_historical():
    g = WorkflowGraph(nodes=(
        WorkflowNode(node_id="L", kind=NodeKind.BOUNDED_LOOP, label="loop",
                     termination_predicate="done", max_iterations=3),
        _action_node("body", "x", 2, parent_id="L"),
        _terminal("t", "L"),
    ))
    k = _kernel((_var("x", 1, 2),), g)
    k.begin_loop_iteration("L")
    _run_action(k, "body", "x", 2)      # ephemeral commit, loop still RUNNING
    # hot interrupt mid-iteration: the ephemeral commit is reset with it
    k.request_governance("pause")
    k.request_governance("resume")
    statuses = k.workflow().statuses
    assert statuses["L"] is NodeStatus.READY
    assert statuses["body"] is not NodeStatus.COMMITTED
    # a GoalPatch in this window must NOT treat the ephemeral body commit
    # as permanent history (no CommittedNodeViolationError, no carry-over)
    k.apply_goal_patch(GoalPatch(patch_id="gp",
                                 new_intent=TaskIntent(goal="别的目标")))
    k.recompose((_var("x", 2, 5),), reason="replan",
                new_graph=WorkflowGraph(nodes=(
                    _action_node("a_new", "x", 5),
                    _terminal("t2", "a_new"),
                )))
    assert k.workflow().graph.node("body") is None
    assert k.workflow().graph.node("L") is None
