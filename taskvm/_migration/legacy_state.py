"""Legacy → domain converters (one-directional; see package __init__).

These functions let an old call site produce NEW domain objects without
the new packages ever touching legacy modules. What they deliberately DO
NOT carry over:

- storage-layer primary keys and app-internal operation names from the
  legacy binding objects: those are control-plane/app-internal concepts
  that must never enter the new domain (master handoff §5; GG red line);
- verifier-only ground truth of any kind.

What they DO carry: the user-visible semantics — labels, values,
visible-title locators (as SurfaceEvidence), goals.
"""
from __future__ import annotations

from typing import Any

from taskvm.domain.contract import ActionContract
from taskvm.domain.intent import TaskIntent
from taskvm.domain.state import (
    MUTABILITY_EDITABLE,
    MUTABILITY_READONLY,
    SurfaceEvidence,
    SurfaceHandle,
    TaskState,
    TaskVariable,
)


def legacy_graph_to_task_state(graph: Any, *, intent: TaskIntent) -> TaskState:
    """Convert a legacy ``TaskStateGraph`` (task_state.representation) into
    the new ``TaskState``. Legacy per-variable binding edges are dropped
    from the domain object; each edge's visible-title locator (when the
    model emitted one) is preserved as SurfaceEvidence so the variable
    stays grounded in visible reality."""
    variables: list[TaskVariable] = []
    for v in graph.variables:
        evidence: list[SurfaceEvidence] = []
        for i, b in enumerate(getattr(v, "bindings", []) or []):
            locator = getattr(b, "locator", None)
            if locator:
                evidence.append(SurfaceEvidence(
                    surface=SurfaceHandle(handle_id=f"{v.var_id}__ev{i}"),
                    visible_label=str(locator),
                    observed_value=v.value))
        variables.append(TaskVariable(
            semantic_key=v.var_id,
            label=v.label,
            value=v.value,
            value_type=getattr(v, "kind", "string"),
            mutability=(MUTABILITY_EDITABLE if getattr(v, "editable", True)
                        else MUTABILITY_READONLY),
            evidence=tuple(evidence)))
    return TaskState(intent=intent, variables=tuple(variables))


def legacy_edit_to_variable_update(edit: dict) -> tuple[str, Any]:
    """A legacy user edit {var_id, old, new} → (semantic_key, new_value)
    ready for ``VariableUpdate``. Pure field renaming, no semantics."""
    if "var_id" not in edit or "new" not in edit:
        raise ValueError(f"legacy edit needs var_id + new: {edit}")
    return edit["var_id"], edit["new"]


def legacy_op_to_action_contract(op: Any, *, semantic_key: str,
                                 visible_label: str,
                                 contract_id: str) -> ActionContract:
    """Convert a legacy executable op into a semantic ActionContract.

    The legacy op's platform addressing and app-internal verb are
    STRIPPED — resolving the contract back to concrete gestures is the
    substrate session's private job (Agents B/E). What survives is: the
    semantic goal, the visible locating label, and the desired value.
    """
    return ActionContract(
        contract_id=contract_id,
        semantic_goal=f"set {semantic_key} to {op.value}",
        desired_state={semantic_key: op.value},
        completion_condition=f"{visible_label} visibly shows {op.value}",
        target_evidence=(SurfaceEvidence(
            surface=SurfaceHandle(handle_id=f"{contract_id}__target"),
            visible_label=visible_label,
            observed_value=None),))
