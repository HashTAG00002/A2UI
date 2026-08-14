"""TaskArchitecture — the composition's STATIC coherence, proven exactly
once at construction (layered ownership protocol §1/§5).

These tests own the CONTENT rules that used to be re-checked inside the
kernel (`_validate_composition_locked`): projection bindings ⊆ variables,
ActionContract keys ⊆ variables, final-writer / split-brain coherence, and
the frozen-history exemption. The kernel installs only validated
compositions; the atomicity of that installation is tested in
tests/kernel/.
"""
import pytest

from taskvm.domain import (
    ActionContract,
    NodeKind,
    ProjectionComponent,
    ProjectionSchema,
    TaskArchitecture,
    TaskVariable,
    ValidationError,
    WorkflowGraph,
    WorkflowNode,
)


def _var(key, desired, observed=None):
    return TaskVariable(semantic_key=key, label=key,
                        observed=observed, desired=desired)


def _contract(cid, key, value):
    return ActionContract(contract_id=cid, semantic_goal=f"set {key}",
                          desired_state={key: value})


def _action(nid, key, value, **kw):
    return WorkflowNode(node_id=nid, kind=NodeKind.ACTION, label=nid,
                        contract=_contract(f"c_{nid}", key, value), **kw)


def _terminal(nid, *deps):
    return WorkflowNode(node_id=nid, kind=NodeKind.TERMINAL, label="完成",
                        depends_on=tuple(deps))


# ── projection bindings must reference declared variables ─────────────────
def test_projection_binding_must_reference_a_variable():
    schema = ProjectionSchema(root_id="root", components=(
        ProjectionComponent(component_id="root", component_type="column",
                            children=("f1",)),
        ProjectionComponent(component_id="f1", component_type="field",
                            label="幽灵", binding_key="ghost", editable=True),
    ))
    with pytest.raises(ValidationError, match="unknown task variables"):
        TaskArchitecture(variables=(_var("x", 2),), schema=schema)


# ── contract keys must reference declared variables ────────────────────────
def test_contract_keys_must_reference_variables():
    graph = WorkflowGraph(nodes=(_action("a1", "ghost", 2),
                                 _terminal("t", "a1")))
    with pytest.raises(ValidationError, match="unknown task variables"):
        TaskArchitecture(variables=(_var("x", 2),), graph=graph)


# ── split-brain guard: final writer target == variable desired ─────────────
def test_split_brain_rejected():
    graph = WorkflowGraph(nodes=(
        _action("a1", "release_date", "2026-08-18"), _terminal("t", "a1")))
    with pytest.raises(ValidationError, match="split-brain"):
        TaskArchitecture(variables=(_var("release_date", "2026-08-20"),),
                         graph=graph)


def test_final_writer_is_the_downstream_one():
    graph = WorkflowGraph(nodes=(
        _action("a1", "x", 2),
        _action("a2", "x", 3, depends_on=("a1",)),
        _terminal("t", "a2"),
    ))
    # a2 runs last and targets 3; a desired of 2 contradicts the plan
    with pytest.raises(ValidationError, match="split-brain"):
        TaskArchitecture(variables=(_var("x", 2),), graph=graph)
    ok = TaskArchitecture(variables=(_var("x", 3),), graph=graph)
    assert ok is not None


def test_multiple_final_writers_must_agree():
    graph = WorkflowGraph(nodes=(
        _action("a1", "x", 2),
        _action("a2", "x", 3),
        _terminal("t", "a1", "a2"),
    ))
    with pytest.raises(ValidationError, match="multiple final writers"):
        TaskArchitecture(variables=(_var("x", 2),), graph=graph)


# ── frozen history is exempt (the kernel supplies the exemption set) ───────
def test_exempt_nodes_are_frozen_history():
    graph = WorkflowGraph(nodes=(_action("a1", "x", 9), _terminal("t", "a1")))
    arch = TaskArchitecture(variables=(_var("x", 3),), graph=graph,
                            exempt_node_ids=frozenset({"a1"}))
    assert arch is not None
    with pytest.raises(ValidationError, match="split-brain"):
        TaskArchitecture(variables=(_var("x", 3),), graph=graph)


def test_exempt_nodes_may_reference_vanished_variables():
    """A committed node's contract is a frozen record: it may reference a
    variable the new composition no longer declares."""
    graph = WorkflowGraph(nodes=(_action("a1", "ghost", 9),
                                 _terminal("t", "a1")))
    arch = TaskArchitecture(variables=(_var("x", 3),), graph=graph,
                            exempt_node_ids=frozenset({"a1"}))
    assert arch is not None
    with pytest.raises(ValidationError, match="unknown task variables"):
        TaskArchitecture(variables=(_var("x", 3),), graph=graph)


# ── variables are unique within the composition ────────────────────────────
def test_duplicate_variable_keys_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        TaskArchitecture(variables=(_var("x", 1), _var("x", 2)))


# ── empty / partial compositions are legal ─────────────────────────────────
def test_minimal_compositions_are_legal():
    assert TaskArchitecture() is not None
    assert TaskArchitecture(variables=(_var("x", 1),)) is not None


def test_graph_without_variables_still_checks_contract_keys():
    graph = WorkflowGraph(nodes=(_action("a1", "x", 1), _terminal("t", "a1")))
    with pytest.raises(ValidationError, match="unknown task variables"):
        TaskArchitecture(graph=graph)


# ── no-orphan: every non-exempt node must lead to the TERMINAL ─────────────
def test_orphan_nodes_are_rejected():
    graph = WorkflowGraph(nodes=(
        _action("a1", "x", 1),
        _action("orphan", "x", 1),          # nothing depends on it
        _terminal("t", "a1"),
    ))
    with pytest.raises(ValidationError, match="reach the TERMINAL"):
        TaskArchitecture(variables=(_var("x", 1),), graph=graph)


def test_fanout_whose_result_nobody_consumes_is_orphaned():
    graph = WorkflowGraph(nodes=(
        _action("a1", "x", 1),
        WorkflowNode(node_id="fo", kind=NodeKind.FAN_OUT, label="f",
                     depends_on=("a1",)),
        _action("l1", "x", 1, parent_id="fo"),
        WorkflowNode(node_id="b", kind=NodeKind.BARRIER, label="b",
                     depends_on=("l1",)),
        _terminal("t", "a1"),               # t ignores the fan-out result
    ))
    with pytest.raises(ValidationError, match="reach the TERMINAL"):
        TaskArchitecture(variables=(_var("x", 1),), graph=graph)


def test_containers_reach_terminal_through_their_children():
    """The fan-out container itself has no dependents — it reaches the
    terminal through the container/child ordering edges."""
    graph = WorkflowGraph(nodes=(
        WorkflowNode(node_id="fo", kind=NodeKind.FAN_OUT, label="f"),
        _action("l1", "x", 2, parent_id="fo"),
        _action("l2", "y", 9, parent_id="fo"),
        WorkflowNode(node_id="b", kind=NodeKind.BARRIER, label="b",
                     depends_on=("l1", "l2")),
        _terminal("t", "b"),
    ))
    arch = TaskArchitecture(variables=(_var("x", 2), _var("y", 9)), graph=graph)
    assert arch is not None


def test_exempt_history_may_be_a_dead_end():
    """A carried committed/compensated node is a frozen record — it does
    not need a path to the new terminal."""
    graph = WorkflowGraph(nodes=(
        _action("a1", "x", 1),              # carried history, dead-end
        _action("a2", "x", 2),
        _terminal("t", "a2"),
    ))
    arch = TaskArchitecture(variables=(_var("x", 2),), graph=graph,
                            exempt_node_ids=frozenset({"a1"}))
    assert arch is not None
    with pytest.raises(ValidationError, match="reach the TERMINAL"):
        TaskArchitecture(variables=(_var("x", 2),), graph=graph)
