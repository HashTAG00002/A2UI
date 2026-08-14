"""Wave-A.1 adversarial contract tests — the kernel under hostile usage
(v4 contract).

These tests attack the kernel's public contract exactly the way a buggy
or confused downstream agent would: double-landing action results,
stale compensation reports, cross-GoalPatch rollbacks, loop termination
abuse, illegal architect output at composition boundaries, and
non-editable variable tampering. Happy-path coverage lives in
test_kernel_invariants.py / test_scenario.py; the Wave-A.2 audit's 13
counterexample groups live in test_v4_audit_fixes.py.
"""
import pytest

from taskvm.domain import (
    ActionContract,
    CompensationPatch,
    EventKind,
    GoalPatch,
    LocalPatch,
    MUTABILITY_LOCKED,
    MUTABILITY_READONLY,
    NodeKind,
    NodeStatus,
    ObservedValue,
    PatchSemanticsError,
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
    k = TaskVMKernel(session_id="adv", intent=TaskIntent(goal=goal))
    k.init_task_state(variables)
    if graph is not None:
        k.set_plan(graph)
    return k


def _run_body(k, node_id, key, value):
    h = k.request_action(node_id)
    assert k.start_action(h["action_id"])
    assert k.finish_action(h["action_id"], observations=[_obs(key, value)])
    k.record_verification(node_id, True)


def _loop_graph(max_iterations=3):
    return WorkflowGraph(nodes=(
        WorkflowNode(node_id="L", kind=NodeKind.BOUNDED_LOOP, label="循环同步",
                     termination_predicate="所有依赖任务都已同步",
                     max_iterations=max_iterations),
        _action_node("body", "x", 2, parent_id="L"),
        _terminal("t", "L"),
    ))


# ══ 1. action exactly-once: one active handle per (node, epoch) ═══════════
def test_one_active_action_per_node():
    k = _kernel((_var("x", 1, 2),),
                WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                     _terminal("t", "a1"))))
    h1 = k.request_action("a1")
    with pytest.raises(ValidationError, match="active action"):
        k.request_action("a1")   # second ACTIVE handle, same node+epoch
    k.start_action(h1["action_id"])
    with pytest.raises(ValidationError):
        k.request_action("a1")   # still active (STARTED → node RUNNING)
    k.finish_action(h1["action_id"], observations=[_obs("x", 2)])
    k.record_verification("a1", True)
    with pytest.raises(ValidationError):
        k.request_action("a1")   # committed nodes take no new actions


# ══ 2. a FINISHED handle can never land again ══════════════════════════════
def test_finished_action_cannot_land_twice():
    k = _kernel((_var("x", 1, 2), _var("y", 1, 2),),
                WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                     _action_node("a2", "y", 2),
                                     _terminal("t", "a1", "a2"))))
    h = k.request_action("a1")
    k.start_action(h["action_id"])
    assert k.finish_action(h["action_id"],
                           observations=[_obs("x", 2)]) is True
    with pytest.raises(ValidationError, match="terminal"):
        k.finish_action(h["action_id"],
                        observations=[_obs("x", 999)])  # second landing
    assert k.task_state().variable("x").observed == 2  # untouched
    with pytest.raises(ValidationError, match="terminal"):
        k.start_action(h["action_id"])
    # protocol ordering is enforced too: finish-before-start / double-start
    h2 = k.request_action("a2")
    with pytest.raises(ValidationError, match="STARTED"):
        k.finish_action(h2["action_id"])
    k.start_action(h2["action_id"])
    with pytest.raises(ValidationError, match="already started"):
        k.start_action(h2["action_id"])


# ══ 3. an old-epoch result cannot rewrite a committed node ════════════════
def test_old_action_cannot_mutate_committed_state():
    k = _kernel((_var("x", 1, 2),),
                WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                     _terminal("t1", "a1"))),
                goal="goal-v1")
    h_old = k.request_action("a1")
    k.start_action(h_old["action_id"])
    assert h_old["epoch"] == 0
    # a GoalPatch lands mid-flight: the old future (incl. a1) is voided…
    k.apply_goal_patch(GoalPatch(patch_id="gp",
                                 new_intent=TaskIntent(goal="goal-v2")))
    # …and the architect re-closes with a fresh plan for a1
    k.recompose((_var("x", 1, 2),), reason="goal-v2 replan",
                new_graph=WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                               _terminal("t2", "a1"))))
    # the new generation re-executes a1 and commits it
    h_new = k.request_action("a1")
    assert h_new["epoch"] > h_old["epoch"]
    k.start_action(h_new["action_id"])
    k.finish_action(h_new["action_id"], observations=[_obs("x", 2)])
    k.record_verification("a1", True)
    assert k.workflow().statuses["a1"] is NodeStatus.COMMITTED
    # NOW the stale generation's result arrives — it must be discarded
    accepted = k.finish_action(h_old["action_id"],
                               observations=[_obs("x", 999)])
    assert accepted is False
    assert k.task_state().variable("x").observed == 2  # NOT 999
    assert k.workflow().statuses["a1"] is NodeStatus.COMMITTED
    discarded = [e for e in k.events()
                 if e.kind is EventKind.ACTION_DISCARDED]
    assert discarded and discarded[-1].payload["action_epoch"] == 0


# ══ 4. a stale compensation result is DISCARDED, never lands ══════════════
def test_stale_compensation_result_is_discarded():
    k = _kernel((_var("x", 1, 1),), goal="goal-v1")
    k.commit_checkpoint("C1", "boundary")
    k.apply_observation([_obs("x", 5)])
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="C1"))
    assert plan.epoch == k.epoch
    # an epoch boundary passes BEFORE the runtime reports back
    k.apply_goal_patch(GoalPatch(patch_id="gp",
                                 new_intent=TaskIntent(goal="goal-v2")))
    assert k.epoch > plan.epoch
    ok = k.record_compensation_result(plan.plan_id, applied=True,
                                      observed_values={"x": 1})
    assert ok is False
    kinds = [e.kind for e in k.events()]
    assert EventKind.COMPENSATION_DISCARDED in kinds       # explicit signal
    assert EventKind.COMPENSATION_APPLIED not in kinds
    assert EventKind.COMPENSATION_FAILED not in kinds      # NOT a failure
    assert k.task_state().variable("x").observed == 5      # state untouched
    discarded = [e for e in k.events()
                 if e.kind is EventKind.COMPENSATION_DISCARDED][-1]
    assert discarded.payload["plan_epoch"] == plan.epoch
    assert discarded.payload["current_epoch"] == k.epoch


# ══ 5. a compensation plan lands exactly once ═════════════════════════════
def test_compensation_plan_is_single_use():
    k = _kernel((_var("x", 1, 1),), goal="g")
    k.commit_checkpoint("C1", "boundary")
    k.apply_observation([_obs("x", 5)])
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="C1"))
    assert k.record_compensation_result(plan.plan_id, applied=True,
                                        observed_values={"x": 1}) is True
    with pytest.raises(ValidationError, match="exactly once"):
        k.record_compensation_result(plan.plan_id, applied=True,
                                     observed_values={"x": 1})
    # a FAILED plan is equally terminal — no silent retry through the
    # same plan; an honest retry requires a NEW CompensationPatch
    k2 = _kernel((_var("x", 1, 1),), goal="g")
    k2.commit_checkpoint("C1", "boundary")
    k2.apply_observation([_obs("x", 5)])
    plan_f = k2.request_compensation(CompensationPatch(
        patch_id="rb2", target_checkpoint_id="C1"))
    assert k2.record_compensation_result(plan_f.plan_id, applied=False,
                                         detail="world refused") is False
    with pytest.raises(ValidationError, match="exactly once"):
        k2.record_compensation_result(plan_f.plan_id, applied=False)


# ══ 6. rollback restores the checkpoint's INTENT ══════════════════════════
def test_rollback_restores_checkpoint_intent():
    k = _kernel((_var("x", 1, 2),),
                WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                     _terminal("t", "a1"))),
                goal="目标一")
    k.commit_checkpoint("C1", "目标一 boundary")
    k.apply_observation([_obs("x", 5)])
    k.apply_goal_patch(GoalPatch(patch_id="gp",
                                 new_intent=TaskIntent(goal="目标二")))
    assert k.task_state().intent.goal == "目标二"
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="C1"))
    assert plan.requires_recompose is True   # cross-GoalPatch signal
    assert k.record_compensation_result(plan.plan_id, applied=True,
                                        observed_values={"x": 1}) is True
    assert k.task_state().intent.goal == "目标一"   # intent REALLY restored
    # the wrong future is not silently kept: uncommitted nodes planned
    # for the abandoned goal are invalidated
    assert k.workflow().statuses["a1"] is NodeStatus.INVALIDATED
    ev = [e for e in k.events()
          if e.kind is EventKind.COMPENSATION_APPLIED][-1]
    assert ev.payload["intent_restored"] is True
    assert ev.payload["requires_recompose"] is True
    assert "a1" in ev.payload["invalidated_node_ids"]


# ══ 7. rollback restores structure — but never fakes the world ════════════
def test_rollback_restores_structure_without_logical_deletion():
    """The audit's reversal of the old z-test: a variable that appeared
    after the checkpoint must NOT be logically deleted to fake a rollback;
    a variable that vanished IS restored from the checkpoint's structure;
    reality that moved without a TaskVM action is kept as-is."""
    k = _kernel((
        _var("x", 1, 1, label="X 变量"),
        _var("y", 2, 2, label="Y 标签", value_type="date"),
    ), goal="g")
    k.commit_checkpoint("C0", "结构边界")
    k.apply_observation([_obs("x", 10)])       # the WORLD moved x (no action)
    # structure drift: y disappears, z appears (same intent)
    k.recompose((_var("x", 10, 1, label="X 变量"),
                 _var("z", 9, 9, label="Z 新变量")),
                reason="structure drift: y surface gone, z appeared")
    assert k.task_state().variable("y") is None
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="C0"))
    assert plan.requires_recompose is True   # structure differs
    # no committed TaskVM action after C0 ⇒ NOTHING to compensate;
    # the old snapshot-diff would have fabricated reversion entries here
    assert plan.entries == ()
    assert k.record_compensation_result(
        plan.plan_id, applied=True,
        observed_values={"x": 10}) is True
    state = k.task_state()
    assert state.variable("x").observed == 10   # external drift kept — honest
    y = state.variable("y")
    assert y is not None and y.label == "Y 标签"   # structure restored
    assert y.value_type == "date" and y.observed == 2 and y.desired == 2
    z = state.variable("z")
    assert z is not None and z.observed == 9   # NOT logically deleted
    ev = [e for e in k.events()
          if e.kind is EventKind.COMPENSATION_APPLIED][-1]
    assert ev.payload["restored_structure_keys"] == ["y"]
    assert "dropped_semantic_keys" not in ev.payload


# ══ 8. a bounded loop repeats until termination ═══════════════════════════
def test_bounded_loop_repeats_until_termination():
    k = _kernel((_var("x", 1, 2),), _loop_graph(max_iterations=3))
    statuses = k.workflow().statuses
    assert statuses["L"] is NodeStatus.READY
    assert statuses["body"] is NodeStatus.PENDING   # not schedulable yet
    with pytest.raises(ValidationError):
        k.request_action("body")                    # loop not started
    # iteration 1
    assert k.begin_loop_iteration("L") == 1
    assert k.workflow().statuses["body"] is NodeStatus.READY
    _run_body(k, "body", "x", 2)
    # a completed body pass does NOT commit the loop (no auto-commit)
    assert k.workflow().statuses["L"] is NodeStatus.RUNNING
    out = k.evaluate_loop_termination("L", False, detail="还没全同步")
    assert out == {"outcome": "continue", "iteration": 1, "next_iteration": 2}
    assert k.workflow().statuses["L"] is NodeStatus.READY
    # iteration 2 — the body is re-armed (its iteration-1 commit was ephemeral)
    assert k.begin_loop_iteration("L") == 2
    assert k.workflow().statuses["body"] is NodeStatus.READY
    _run_body(k, "body", "x", 2)
    out = k.evaluate_loop_termination("L", True, detail="全部同步")
    assert out["outcome"] == "committed"
    assert k.workflow().statuses["L"] is NodeStatus.COMMITTED
    starts = [e for e in k.events()
              if e.kind is EventKind.LOOP_ITERATION_STARTED]
    assert [e.payload["iteration"] for e in starts] == [1, 2]
    evals = [e for e in k.events()
             if e.kind is EventKind.LOOP_ITERATION_EVALUATED]
    assert [e.payload["terminated"] for e in evals] == [False, True]


# ══ 9. a bounded loop stops at max_iterations with escalation ═════════════
def test_bounded_loop_stops_at_max_iterations():
    k = _kernel((_var("x", 1, 2),), _loop_graph(max_iterations=2))
    k.begin_loop_iteration("L")
    _run_body(k, "body", "x", 2)
    out = k.evaluate_loop_termination("L", False)
    assert out["outcome"] == "continue"
    k.begin_loop_iteration("L")
    _run_body(k, "body", "x", 2)
    out = k.evaluate_loop_termination("L", False)
    assert out["outcome"] == "failed"
    assert out["reason"] == "max_iterations_exceeded"
    assert k.workflow().statuses["L"] is NodeStatus.FAILED
    with pytest.raises(ValidationError):
        k.begin_loop_iteration("L")   # FAILED loops don't silently restart
    ev = [e for e in k.events()
          if e.kind is EventKind.LOOP_ITERATION_EVALUATED][-1]
    assert ev.payload["reason"] == "max_iterations_exceeded"


# ══ 10. projection schema is a real tree: cycles rejected ════════════════
def test_projection_schema_rejects_cycle():
    with pytest.raises(ValidationError):
        ProjectionSchema(root_id="r", components=(
            ProjectionComponent(component_id="r", component_type="column",
                                children=("a",)),
            ProjectionComponent(component_id="a", component_type="column",
                                children=("b",)),
            ProjectionComponent(component_id="b", component_type="column",
                                children=("a",)),     # b→a closes a cycle
        ))
    with pytest.raises(ValidationError):
        ProjectionSchema(root_id="a", components=(
            ProjectionComponent(component_id="a", component_type="column",
                                children=("a",)),     # self-loop
        ))
    with pytest.raises(ValidationError):
        ProjectionSchema(root_id="r", components=(
            ProjectionComponent(component_id="r", component_type="column",
                                children=("x",)),
            ProjectionComponent(component_id="x", component_type="field"),
            ProjectionComponent(component_id="orphan",  # unreachable
                                component_type="field"),
        ))


# ══ 11. projection bindings must reference declared variables ════════════
def test_projection_binding_must_exist():
    k = _kernel((_var("x", 1, 2),))
    bad_schema = ProjectionSchema(root_id="root", components=(
        ProjectionComponent(component_id="root", component_type="column",
                            children=("f1",)),
        ProjectionComponent(component_id="f1", component_type="field",
                            label="幽灵", binding_key="ghost", editable=True),
    ))
    graph = WorkflowGraph(nodes=(_action_node("a1", "x", 2),
                                 _terminal("t", "a1")))
    with pytest.raises(ValidationError, match="unknown task variables"):
        k.set_plan(graph, bad_schema)
    # rejected atomically: nothing installed, no plan event
    assert k.projection().schema is None
    assert k.workflow().graph is None
    assert EventKind.PLAN_CREATED not in [e.kind for e in k.events()]


# ══ 12. action contract keys must reference declared variables ═══════════
def test_action_contract_keys_must_exist():
    k = _kernel((_var("x", 1, 2),))
    bad_graph = WorkflowGraph(nodes=(
        _action_node("a1", "ghost", 2),
        _terminal("t", "a1")))
    with pytest.raises(ValidationError, match="unknown task variables"):
        k.set_plan(bad_graph)
    assert k.workflow().graph is None
    assert EventKind.PLAN_CREATED not in [e.kind for e in k.events()]


# ══ 13. LocalPatch cannot retarget readonly/locked variables ═════════════
def test_local_patch_rejects_readonly():
    k = _kernel((
        _var("ro", 1, 1, mutability=MUTABILITY_READONLY),
        _var("lk", 1, 1, mutability=MUTABILITY_LOCKED),
        _var("ed", 1, 1),
    ))
    with pytest.raises(PatchSemanticsError, match="readonly/locked"):
        k.apply_local_patch(LocalPatch(
            patch_id="lp_ro",
            variable_updates=(VariableUpdate("ro", 5),)))
    with pytest.raises(PatchSemanticsError, match="readonly/locked"):
        k.apply_local_patch(LocalPatch(
            patch_id="lp_lk",
            variable_updates=(VariableUpdate("lk", 5),)))
    # the editable one is unaffected by the rejections (atomic) and works
    assert k.task_state().variable("ro").desired == 1
    k.apply_local_patch(LocalPatch(
        patch_id="lp_ok", variable_updates=(VariableUpdate("ed", 5),)))
    assert k.task_state().variable("ed").desired == 5


# ══ 14. LocalPatch is deterministic: duplicate targets rejected ══════════
def test_local_patch_rejects_duplicate_variable_updates():
    with pytest.raises(ValidationError, match="duplicate"):
        LocalPatch(patch_id="lp_dup",
                   variable_updates=(VariableUpdate("x", 1),
                                     VariableUpdate("x", 2)))
    # the v3 node-override channel is gone entirely (audit G4): there is
    # exactly ONE way to retarget, and it is the variable update
    with pytest.raises(TypeError):
        LocalPatch(patch_id="lp_dup2",
                   variable_updates=(VariableUpdate("x", 1),),
                   node_overrides=())
