"""Domain-layer contract tests: pure data + invariants, no kernel needed."""
import dataclasses

import pytest

from taskvm.domain import (
    ActionContract,
    CompensationPatch,
    EventKind,
    GoalPatch,
    LocalPatch,
    NodeKind,
    NodeStatus,
    ProjectionComponent,
    ProjectionSchema,
    Reversibility,
    SurfaceEvidence,
    SurfaceHandle,
    TaskIntent,
    TaskState,
    TaskVariable,
    ValidationError,
    VariableUpdate,
    WorkflowGraph,
    WorkflowNode,
    requires_replan,
)


# ── TaskIntent / TaskState ────────────────────────────────────────────────
def test_intent_requires_goal():
    with pytest.raises(ValidationError):
        TaskIntent(goal="")


def test_task_state_rejects_duplicate_keys():
    v = TaskVariable(semantic_key="release_date", label="发布日期")
    with pytest.raises(ValidationError):
        TaskState(intent=TaskIntent(goal="g"), variables=(v, v))


def test_domain_objects_are_frozen():
    v = TaskVariable(semantic_key="k", label="l", value=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.value = 2


def test_surface_handle_is_taskvm_owned_not_a_storage_key():
    h = SurfaceHandle(handle_id="h1", opaque_token="opaque:whatever-substrate")
    assert h.handle_id == "h1"
    # the token is opaque: the domain exposes no accessor that interprets it
    assert not hasattr(h, "app") and not hasattr(h, "field")


def test_evidence_confidence_bounds():
    with pytest.raises(ValidationError):
        SurfaceEvidence(surface=SurfaceHandle(handle_id="h"), visible_label="x",
                        confidence=1.5)


# ── Projection schema/data separation ──────────────────────────────────────
def test_projection_schema_validates_tree_references():
    with pytest.raises(ValidationError):
        ProjectionSchema(root_id="root", components=(
            ProjectionComponent(component_id="root", component_type="column",
                                children=("ghost",)),
        ))


def test_projection_schema_ok():
    s = ProjectionSchema(root_id="root", components=(
        ProjectionComponent(component_id="root", component_type="column",
                            children=("f1",)),
        ProjectionComponent(component_id="f1", component_type="field",
                            label="发布日期", binding_key="release_date",
                            editable=True),
    ))
    assert s.root_id == "root"


def test_projection_schema_rejects_duplicate_ids():
    with pytest.raises(ValidationError):
        ProjectionSchema(root_id="a", components=(
            ProjectionComponent(component_id="a", component_type="column"),
            ProjectionComponent(component_id="a", component_type="field"),
        ))


# ── Workflow validation ────────────────────────────────────────────────────
def _action(nid, **kw):
    return WorkflowNode(node_id=nid, kind=NodeKind.ACTION, label=nid,
                        contract=ActionContract(contract_id=f"c_{nid}",
                                                semantic_goal="do " + nid),
                        **kw)


def test_workflow_rejects_cycle():
    a = _action("a", depends_on=("b",))
    b = _action("b", depends_on=("a",))
    with pytest.raises(ValidationError, match="cycle"):
        WorkflowGraph(nodes=(a, b))


def test_workflow_rejects_unknown_dependency():
    with pytest.raises(ValidationError):
        WorkflowGraph(nodes=(_action("a", depends_on=("ghost",)),))


def test_bounded_loop_requires_guards():
    with pytest.raises(ValidationError):
        WorkflowGraph(nodes=(WorkflowNode(node_id="L", kind=NodeKind.BOUNDED_LOOP,
                                          label="loop"),))
    with pytest.raises(ValidationError):
        WorkflowGraph(nodes=(WorkflowNode(node_id="L", kind=NodeKind.BOUNDED_LOOP,
                                          label="loop",
                                          termination_predicate="all synced"),))
    ok = WorkflowGraph(nodes=(WorkflowNode(node_id="L", kind=NodeKind.BOUNDED_LOOP,
                                           label="loop",
                                           termination_predicate="all synced",
                                           max_iterations=5),))
    assert ok.node("L").max_iterations == 5


def test_action_node_requires_contract():
    with pytest.raises(ValidationError):
        WorkflowGraph(nodes=(WorkflowNode(node_id="a", kind=NodeKind.ACTION,
                                          label="a"),))


def test_barrier_requires_fan_in_edges():
    with pytest.raises(ValidationError):
        WorkflowGraph(nodes=(WorkflowNode(node_id="b", kind=NodeKind.BARRIER,
                                          label="b"),))


def test_fan_out_requires_lanes():
    with pytest.raises(ValidationError):
        WorkflowGraph(nodes=(WorkflowNode(node_id="fo", kind=NodeKind.FAN_OUT,
                                          label="fo"),))


def test_ready_nodes_dependency_semantics():
    a = _action("a")
    b = _action("b", depends_on=("a",))
    g = WorkflowGraph(nodes=(a, b))
    ready = g.ready_nodes({"a": NodeStatus.COMMITTED, "b": NodeStatus.PENDING})
    assert [n.node_id for n in ready] == ["b"]
    assert g.ready_nodes({"a": NodeStatus.PENDING, "b": NodeStatus.PENDING}) == (a,)


# ── Patch class semantics ──────────────────────────────────────────────────
def test_patch_classes_encode_the_replan_boundary():
    lp = LocalPatch(patch_id="p1",
                    variable_updates=(VariableUpdate("release_date", "08-18"),))
    gp = GoalPatch(patch_id="p2", new_intent=TaskIntent(goal="new goal"))
    cp = CompensationPatch(patch_id="p3", target_checkpoint_id="C1",
                           observed_before={"release_date": "08-14"})
    assert requires_replan(lp) is False
    assert requires_replan(gp) is True
    assert requires_replan(cp) is False


def test_local_patch_cannot_carry_intent_or_topology():
    # LocalPatch has no intent field and no node-list field — the type system
    # itself is the boundary (handoff 02 §Patch).
    fields = {f.name for f in dataclasses.fields(LocalPatch)}
    assert "new_intent" not in fields
    assert "nodes" not in fields and "new_graph" not in fields


def test_local_patch_must_change_something():
    with pytest.raises(ValidationError):
        LocalPatch(patch_id="p1")


def test_compensation_patch_needs_a_checkpoint():
    with pytest.raises(ValidationError):
        CompensationPatch(patch_id="p3")


# ── ActionContract no-leak shape ───────────────────────────────────────────
def test_action_contract_has_no_substrate_specific_fields():
    """The cross-layer contract must not expose storage keys, app-internal
    operation names, or platform selectors (master handoff §5)."""
    fields = {f.name for f in dataclasses.fields(ActionContract)}
    forbidden_fragments = ("entity", "operator", "selector", "dom", "xpath",
                           "coord", "app")
    for f in fields:
        assert not any(frag in f for frag in forbidden_fragments), f


def test_irreversible_contract_requires_confirmation():
    c = ActionContract(contract_id="c1", semantic_goal="send the message",
                       reversibility=Reversibility.IRREVERSIBLE)
    assert c.requires_confirmation is True


# ── Event envelope ─────────────────────────────────────────────────────────
def test_event_kinds_cover_the_handoff_minimum():
    names = {k.value for k in EventKind}
    required = {
        "observation_received", "state_updated",
        "plan_created", "plan_patched",
        "action_requested", "action_started", "action_finished",
        "action_discarded",
        "verification_passed", "verification_failed",
        "checkpoint_committed", "governance_requested",
        "conflict_detected", "conflict_resolved",
        "compensation_requested", "compensation_applied",
        "compensation_failed",
    }
    assert required <= names
