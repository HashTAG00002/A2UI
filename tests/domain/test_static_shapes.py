"""Static shape rules owned by domain constructors (layered protocol §5).

MOVED from tests/kernel/ (the assertions are unchanged; the OWNER moved):
- workflow primitive shapes (former Wave-A.2 G11 kernel-side duplicates),
- projection tree cycles (former adversarial #10),
- LocalPatch duplicate keys / no node-override channel (former #14 / G4).

NEW in v5:
- ObservationBatch rejects duplicate semantic keys at construction (F13b).
  (no-orphan lives in test_architecture.py — it needs the history
  exemption, so its owner is TaskArchitecture, not WorkflowGraph.)
"""
import pytest

from taskvm.domain import (
    ActionContract,
    LocalPatch,
    NodeKind,
    ObservationBatch,
    ObservedValue,
    ProjectionComponent,
    ProjectionSchema,
    ValidationError,
    VariableUpdate,
    WorkflowGraph,
    WorkflowNode,
)


def _action(nid, key="x", value=1, **kw):
    return WorkflowNode(
        node_id=nid, kind=NodeKind.ACTION, label=nid,
        contract=ActionContract(contract_id=f"c_{nid}",
                                semantic_goal=f"set {key} to {value}",
                                desired_state={key: value}),
        **kw)


def _terminal(nid, *deps):
    return WorkflowNode(node_id=nid, kind=NodeKind.TERMINAL, label="完成",
                        depends_on=tuple(deps))


# ══ workflow primitive shapes (moved from kernel G11 — assertions same) ═══
def test_sequence_children_must_form_a_chain():
    with pytest.raises(ValidationError):
        WorkflowGraph(nodes=(
            WorkflowNode(node_id="seq", kind=NodeKind.SEQUENCE, label="s"),
            _action("c1", "x", 1, parent_id="seq"),
            _action("c2", "x", 2, parent_id="seq"),   # not chained!
            _terminal("t", "seq"),
        ))
    ok = WorkflowGraph(nodes=(
        WorkflowNode(node_id="seq", kind=NodeKind.SEQUENCE, label="s"),
        _action("c1", "x", 1, parent_id="seq"),
        _action("c2", "x", 2, parent_id="seq", depends_on=("c1",)),
        _terminal("t", "seq"),
    ))
    assert ok is not None


def test_fanout_lanes_must_be_independent():
    with pytest.raises(ValidationError):
        WorkflowGraph(nodes=(
            WorkflowNode(node_id="fo", kind=NodeKind.FAN_OUT, label="f"),
            _action("l1", "x", 1, parent_id="fo"),
            _action("l2", "x", 2, parent_id="fo", depends_on=("l1",)),
            WorkflowNode(node_id="b", kind=NodeKind.BARRIER, label="b",
                         depends_on=("l1", "l2")),
            _terminal("t", "b"),
        ))


def test_barrier_must_fan_in_a_fanout():
    with pytest.raises(ValidationError):
        WorkflowGraph(nodes=(
            _action("a1", "x", 1),
            _action("a2", "x", 2),
            WorkflowNode(node_id="b", kind=NodeKind.BARRIER, label="b",
                         depends_on=("a1", "a2")),   # not fan-out lanes!
            _terminal("t", "b"),
        ))


def test_exactly_one_terminal_and_terminal_is_a_sink():
    with pytest.raises(ValidationError):
        WorkflowGraph(nodes=(_action("a1", "x", 1),))   # no terminal
    with pytest.raises(ValidationError):
        WorkflowGraph(nodes=(
            _action("a1", "x", 1),
            _terminal("t1", "a1"),
            _terminal("t2", "a1"),                            # two terminals
        ))
    with pytest.raises(ValidationError):
        WorkflowGraph(nodes=(
            _action("a1", "x", 1),
            _terminal("t1", "a1"),
            _action("a2", "x", 2, depends_on=("t1",)),   # terminal not sink
        ))


# ══ projection schema is a real tree (moved from kernel adversarial #10) ═══
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


# ══ patch constructor rules (moved from kernel #14 / G4) ═══════════════════
def test_local_patch_rejects_duplicate_variable_updates():
    with pytest.raises(ValidationError, match="duplicate"):
        LocalPatch(patch_id="lp_dup",
                   variable_updates=(VariableUpdate("x", 1),
                                     VariableUpdate("x", 2)))


def test_node_contract_override_is_gone():
    import taskvm.domain as d
    assert not hasattr(d, "NodeContractOverride")
    with pytest.raises(TypeError):
        LocalPatch(patch_id="lp_dup2",
                   variable_updates=(VariableUpdate("x", 1),),
                   node_overrides=())


# ══ ObservationBatch: duplicate keys rejected at construction (F13b) ═══════
def test_observation_batch_rejects_duplicate_keys():
    with pytest.raises(ValidationError, match="duplicate"):
        ObservationBatch((
            ObservedValue(semantic_key="x", value=3),
            ObservedValue(semantic_key="x", value=4),
        ))
    ok = ObservationBatch((
        ObservedValue(semantic_key="x", value=3),
        ObservedValue(semantic_key="y", value=4),
    ))
    assert len(ok.observations) == 2
