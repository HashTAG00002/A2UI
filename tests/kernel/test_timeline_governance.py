"""v5 timeline governance tests — Kernel-owned TIME/HISTORY/TRANSITION
semantics required by the layered ownership protocol (§4.8 pending
compensation gate, §4.11 COMPLETE/PARTIAL timeline disposition) that
f154b4c documented but did not implement, plus the typed landing rules
for VerificationResult / CompensationResult.

These tests were written to FAIL on the f154b4c kernel:
  - a pending compensation plan did NOT block forward autonomy;
  - a COMPLETE rollback did NOT truncate active future checkpoints;
  - no PARTIAL disposition existed (bare bool + free-form dict);
  - verification was a bare (node_id, bool) with no attempt identity;
  - checkpoint ids had no namespace and collided with workflow node ids.
"""
import pytest

from taskvm.domain import (
    ActionContract,
    CompensationEntryResult,
    CompensationPatch,
    CompensationResult,
    EventKind,
    GoalPatch,
    NodeKind,
    NodeStatus,
    ObservedValue,
    TaskIntent,
    TaskVariable,
    ValidationError,
    VerificationResult,
    WorkflowGraph,
    WorkflowNode,
)
from taskvm.kernel import TaskVMKernel


# ── helpers ────────────────────────────────────────────────────────────────
def _obs(key, value):
    return ObservedValue(semantic_key=key, value=value)


def _var(key, observed, desired, **kw):
    return TaskVariable(semantic_key=key, label=kw.pop("label", key),
                        observed=observed, desired=desired, **kw)


def _contract(cid, key, value):
    return ActionContract(contract_id=cid,
                          semantic_goal=f"set {key} to {value}",
                          desired_state={key: value},
                          completion_condition=f"{key} visibly shows {value}")


def _action_node(nid, key, value, **kw):
    return WorkflowNode(node_id=nid, kind=NodeKind.ACTION, label=nid,
                        contract=_contract(f"c_{nid}", key, value), **kw)


def _terminal(nid, *deps):
    return WorkflowNode(node_id=nid, kind=NodeKind.TERMINAL, label="完成",
                        depends_on=tuple(deps))


def _kernel(variables, graph=None, goal="g"):
    k = TaskVMKernel(session_id="v5", intent=TaskIntent(goal=goal))
    k.init_task_state(variables)
    if graph is not None:
        k.set_plan(graph)
    return k


def _run(k, node_id, key, value):
    h = k.request_action(node_id)
    assert k.start_action(h["action_id"])
    assert k.finish_action(h["action_id"], observations=[_obs(key, value)])
    k.land_verification(VerificationResult(
        node_id=node_id, epoch=h["epoch"], passed=True,
        action_id=h["action_id"]))


def _result(plan, epoch, *outcomes, detail=""):
    return CompensationResult.for_plan(plan, epoch=epoch,
                                       outcomes=outcomes, detail=detail)


def _full_success(plan, epoch):
    return _result(plan, epoch, *(
        CompensationEntryResult(node_id=e.node_id, semantic_key=e.semantic_key,
                                final_observed=e.to_observed, compensated=True)
        for e in plan.entries))


# ══ 1. pending compensation blocks forward autonomy (protocol §4.8) ════════
def _kernel_with_pending_plan():
    """a1 committed before ckpt:C0; a2 READY; world drifted after C0 so the
    (empty-entry) plan is pending."""
    g = WorkflowGraph(nodes=(
        _action_node("a1", "x", 2),
        _action_node("a2", "x", 3, depends_on=("a1",)),
        _terminal("t", "a2"),
    ))
    k = _kernel((_var("x", 1, 3),), g)
    _run(k, "a1", "x", 2)
    k.commit_checkpoint("C0", "boundary")
    k.apply_observation([_obs("x", 2)])          # world moved (drift)
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:C0"))
    return k, plan


def test_pending_compensation_blocks_forward_autonomy():
    k, plan = _kernel_with_pending_plan()
    assert k.workflow().statuses["a2"] is NodeStatus.READY
    with pytest.raises(ValidationError, match="compensation"):
        k.request_action("a2")                   # forward action
    with pytest.raises(ValidationError, match="compensation"):
        k.advance_control("t")                   # (would-be) terminal path
    with pytest.raises(ValidationError, match="compensation"):
        k.commit_checkpoint("C9", "mid-rollback")  # pinning a doomed head
    # landing resolves the gate: empty plan + empty result ⇒ COMPLETE
    assert k.record_compensation_result(
        plan.plan_id, _result(plan, k.epoch)) == "complete"
    h = k.request_action("a2")                   # gate released
    assert h["node_id"] == "a2"


def test_pending_gate_releases_on_failure():
    """A FAILED landing is also a resolution: the gate opens again."""
    g = WorkflowGraph(nodes=(
        _action_node("a1", "x", 2),
        _action_node("a2", "x", 3, depends_on=("a1",)),
        _terminal("t", "a2"),
    ))
    k2 = _kernel((_var("x", 1, 3),), g)
    _run(k2, "a1", "x", 2)
    k2.commit_checkpoint("C0", "boundary")
    _run(k2, "a2", "x", 3)
    plan2 = k2.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:C0"))
    failed = _result(plan2, k2.epoch, *(
        CompensationEntryResult(node_id=e.node_id, semantic_key=e.semantic_key,
                                final_observed=e.from_observed,
                                compensated=False)
        for e in plan2.entries))
    assert k2.record_compensation_result(plan2.plan_id, failed) == "failed"
    assert k2.task_state().variable("x").observed == 3   # world NOT rewound
    with pytest.raises(ValidationError):     # a2 is committed — no rerun…
        k2.request_action("a2")
    k2.commit_checkpoint("C1", "post-failure boundary")   # …but gate is open


def test_pending_plan_is_superseded_by_governance():
    k, plan = _kernel_with_pending_plan()
    k.request_governance("pause", "user interrupted")     # epoch bump
    assert k.epoch > plan.epoch
    h = k.request_action("a2")          # stale plan no longer blocks
    assert h["epoch"] == k.epoch
    # the stale plan's late landing is DISCARDED, never applied
    assert k.record_compensation_result(
        plan.plan_id, _result(plan, plan.epoch)) == "discarded"
    assert EventKind.COMPENSATION_DISCARDED in [e.kind for e in k.events()]


def test_goal_patch_during_pending_compensation_blocks_until_recompose():
    k, plan = _kernel_with_pending_plan()
    k.apply_goal_patch(GoalPatch(patch_id="gp",
                                 new_intent=TaskIntent(goal="别的目标")))
    with pytest.raises(ValidationError, match="recompose"):
        k.request_action("a2")
    assert k.record_compensation_result(
        plan.plan_id, _result(plan, plan.epoch)) == "discarded"


# ══ 2. COMPLETE rollback truncates active future checkpoints (§4.11) ═══════
def test_complete_rollback_truncates_future_checkpoints():
    g = WorkflowGraph(nodes=(
        _action_node("a1", "x", 2),
        _action_node("a2", "x", 3, depends_on=("a1",)),
        _terminal("t", "a2"),
    ))
    k = _kernel((_var("x", 1, 3),), g)
    _run(k, "a1", "x", 2)
    k.commit_checkpoint("C0", "boundary")
    _run(k, "a2", "x", 3)
    k.commit_checkpoint("C1", "future boundary")
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:C0"))
    assert k.record_compensation_result(
        plan.plan_id, _full_success(plan, k.epoch)) == "complete"
    assert [r.checkpoint_id for r in k.checkpoints()] == ["ckpt:C0"]
    ev = [e for e in k.events()
          if e.kind is EventKind.COMPENSATION_APPLIED][-1]
    assert ev.payload["disposition"] == "complete"
    assert ev.payload["truncated_checkpoint_ids"] == ["ckpt:C1"]


# ══ 3. PARTIAL rollback (§4.11): honest partial, nothing faked ═════════════
def _partial_setup():
    """C0 after a1; then a2 (x 2→3) and a3 (y 1→9) commit."""
    g = WorkflowGraph(nodes=(
        _action_node("a1", "x", 2),
        _action_node("a2", "x", 3, depends_on=("a1",)),
        _action_node("a3", "y", 9, depends_on=("a2",)),
        _terminal("t", "a3"),
    ))
    k = _kernel((_var("x", 1, 3), _var("y", 1, 9)), g)
    _run(k, "a1", "x", 2)
    k.commit_checkpoint("C0", "boundary")
    _run(k, "a2", "x", 3)
    _run(k, "a3", "y", 9)
    k.commit_checkpoint("C1", "future boundary")   # before the plan (gate)
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:C0"))
    assert [(e.node_id, e.semantic_key) for e in plan.entries] == [
        ("a3", "y"), ("a2", "x")]                    # LIFO
    return k, plan


def test_partial_rollback_marks_undone_compensated_and_waits_for_governance():
    k, plan = _partial_setup()
    res = _result(
        plan, k.epoch,
        CompensationEntryResult(node_id="a3", semantic_key="y",
                                final_observed=1, compensated=True),
        CompensationEntryResult(node_id="a2", semantic_key="x",
                                final_observed=3, compensated=False))
    assert k.record_compensation_result(plan.plan_id, res) == "partial"
    state = k.task_state()
    assert state.variable("y").observed == 1     # undone write folded back
    assert state.variable("x").observed == 3     # standing write kept — honest
    # PARTIAL never truncates the future checkpoint history
    assert [r.checkpoint_id for r in k.checkpoints()] == ["ckpt:C0", "ckpt:C1"]
    # workflow: undone work is honestly COMPENSATED (was committed, later
    # undone) — never disguised as still-done, never silently re-armed;
    # the standing write stays COMMITTED; the abandoned future is void
    statuses = k.workflow().statuses
    assert statuses["a1"] is NodeStatus.COMMITTED    # pre-boundary
    assert statuses["a2"] is NodeStatus.COMMITTED    # write still stands
    assert statuses["a3"] is NodeStatus.COMPENSATED  # undone — the truth
    assert statuses["t"] is NodeStatus.INVALIDATED
    # forward autonomy waits for governance (recompose), per protocol §8
    assert k.pending_recompose is not None
    with pytest.raises(ValidationError, match="recompose"):
        k.request_action("a3")
    # history: only the undone action was consumed — a later rollback to
    # the same boundary still owes the a2 reversion
    plan2 = k.request_compensation(CompensationPatch(
        patch_id="rb2", target_checkpoint_id="ckpt:C0"))
    assert [(e.node_id, e.semantic_key) for e in plan2.entries] == [("a2", "x")]
    ev = [e for e in k.events()
          if e.kind is EventKind.COMPENSATION_PARTIAL][-1]
    assert ev.payload["disposition"] == "partial"
    assert ev.payload["uncompensated"] == [{"node_id": "a2", "semantic_key": "x"}]
    # resolution path: the Architect re-closes (carrying ALL history,
    # including the COMPENSATED record), and execution resumes
    carried = tuple(k.workflow().graph.node(nid) for nid in ("a1", "a2", "a3"))
    k.recompose((_var("x", 3, 3), _var("y", 1, 9)), reason="post-partial replan",
                new_graph=WorkflowGraph(nodes=carried + (
                    # new work attaches to the STANDING commit — a
                    # COMPENSATED (undone) node can never satisfy a
                    # dependency: its effect is gone from the world
                    _action_node("a4", "y", 9, depends_on=("a2",)),
                    _terminal("t2", "a4"),
                )))
    assert k.pending_recompose is None
    assert k.request_action("a4")["node_id"] == "a4"


def test_missing_coverage_is_not_complete():
    """COMPLETE requires every plan entry to land compensated; an entry the
    runtime never reported on counts as NOT undone (PARTIAL)."""
    k, plan = _partial_setup()
    res = _result(
        plan, k.epoch,
        CompensationEntryResult(node_id="a3", semantic_key="y",
                                final_observed=1, compensated=True))
    assert k.record_compensation_result(plan.plan_id, res) == "partial"
    assert k.task_state().variable("x").observed == 3   # a2 still stands
    assert k.workflow().statuses["a2"] is NodeStatus.COMMITTED


def test_failed_rollback_changes_nothing():
    k, plan = _partial_setup()
    res = _result(plan, k.epoch, *(
        CompensationEntryResult(node_id=e.node_id, semantic_key=e.semantic_key,
                                final_observed=e.from_observed,
                                compensated=False)
        for e in plan.entries))
    before = k.task_state()
    assert k.record_compensation_result(plan.plan_id, res) == "failed"
    assert k.task_state() == before
    assert k.workflow().statuses["a3"] is NodeStatus.COMMITTED
    assert EventKind.COMPENSATION_FAILED in [e.kind for e in k.events()]


# ══ 4. typed verification landing (F2 strengthened: attempt identity) ══════
def test_land_verification_binds_the_current_finished_attempt():
    k = _kernel((_var("x", 1, 2),),
                WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                     _terminal("t", "a1"))))
    h = k.request_action("a1")
    k.start_action(h["action_id"])
    k.finish_action(h["action_id"], observations=[_obs("x", 2)])
    # a verdict naming the WRONG attempt can never land
    with pytest.raises(ValidationError, match="attempt"):
        k.land_verification(VerificationResult(
            node_id="a1", epoch=k.epoch, passed=True,
            action_id="action:99999"))
    # a verdict from a dead epoch can never land
    with pytest.raises(ValidationError, match="stale epoch"):
        k.land_verification(VerificationResult(
            node_id="a1", epoch=k.epoch + 1, passed=True,
            action_id=h["action_id"]))
    k.land_verification(VerificationResult(
        node_id="a1", epoch=k.epoch, passed=True,
        action_id=h["action_id"], evidence_ref="screenshot#17"))
    assert k.workflow().statuses["a1"] is NodeStatus.COMMITTED


def test_land_verification_verify_kind_rules():
    g = WorkflowGraph(nodes=(
        _action_node("a1", "x", 2),
        WorkflowNode(node_id="v1", kind=NodeKind.VERIFY, label="验证",
                     depends_on=("a1",), verification="x updated"),
        _terminal("t", "v1"),
    ))
    k = _kernel((_var("x", 1, 2),), g)
    _run(k, "a1", "x", 2)
    # a VERIFY node has no action attempt — naming one is a protocol error
    with pytest.raises(ValidationError, match="no action attempt"):
        k.land_verification(VerificationResult(
            node_id="v1", epoch=k.epoch, passed=True,
            action_id="action:00001"))
    # VERIFY is the only kind allowed READY → FAILED (F1)
    k.land_verification(VerificationResult(
        node_id="v1", epoch=k.epoch, passed=False, detail="value wrong"))
    assert k.workflow().statuses["v1"] is NodeStatus.FAILED
    k.requeue("v1")
    k.land_verification(VerificationResult(
        node_id="v1", epoch=k.epoch, passed=True))
    assert k.workflow().statuses["v1"] is NodeStatus.COMMITTED


# ══ 5. typed compensation landing: identity / single-use ═══════════════════
def test_compensation_result_identity_and_single_use():
    k, plan = _partial_setup()
    with pytest.raises(ValidationError, match="unknown compensation plan"):
        k.record_compensation_result(
            "comp:99999", _result(plan, k.epoch))
    other = CompensationResult(plan_id="comp:99999", epoch=k.epoch)
    with pytest.raises(ValidationError, match="names plan"):
        k.record_compensation_result(plan.plan_id, other)
    with pytest.raises(ValidationError, match="epoch"):
        k.record_compensation_result(
            plan.plan_id, _result(plan, k.epoch + 1))
    assert k.record_compensation_result(
        plan.plan_id, _full_success(plan, k.epoch)) == "complete"
    with pytest.raises(ValidationError, match="exactly once"):
        k.record_compensation_result(
            plan.plan_id, _full_success(plan, k.epoch))


# ══ 6. checkpoint id namespace (no collision with node ids by construction) ═
def test_checkpoint_ids_are_namespaced():
    g = WorkflowGraph(nodes=(
        _action_node("a1", "x", 2),
        WorkflowNode(node_id="cp1", kind=NodeKind.CHECKPOINT, label="C1",
                     depends_on=("a1",)),
        _terminal("t", "cp1"),
    ))
    k = _kernel((_var("x", 1, 2),), g)
    # a manual checkpoint NAMED like a workflow node can never BE confused
    # with it: the record lives in the ckpt: namespace
    rec0 = k.commit_checkpoint("a1", "looks like a node, is not")
    assert rec0.checkpoint_id == "ckpt:a1"
    assert k.workflow().statuses["a1"] is NodeStatus.READY   # untouched
    _run(k, "a1", "x", 2)
    # the workflow CHECKPOINT node advances via advance_control and pins a
    # namespaced record that contains itself (F7)
    rec = k.advance_control("cp1")
    assert rec.checkpoint_id == "ckpt:cp1"
    assert "cp1" in rec.committed_nodes
    with pytest.raises(ValidationError, match="duplicate"):
        k.commit_checkpoint("cp1", "same record id again")
