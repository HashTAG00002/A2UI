"""Patch compiler — turn a user edit into a semantic patch against the binding.

NOT gate-critical (the gate-critical step is binding discovery in the compiler).
This is deterministic engineering: given the now-fixed binding + a user edit
(var_id → new_value), produce a list of {entity, operator, value} operations
to apply via the action_dispatcher. W1 is rule-based (no model variant needed);
the rule is "apply the new value to every binding of the edited variable."

A frontier-model variant (optional) is wired via ``compile_patch_model`` but not
the W1 default — patch generation is NOT what the kill-test measures (W1 plan:
"What W1 tests vs. does NOT").
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from taskvm.task_state.entity_binding import TaskBinding, EntityBinding
from taskvm.task_state.dependency_graph import propagate

logger = logging.getLogger(__name__)


@dataclass
class PatchOp:
    """One executable operation derived from a user edit."""
    app: str
    entity_id: str
    field: str
    operator: str
    value: Any

    def to_dict(self) -> dict:
        return {"app": self.app, "entity_id": self.entity_id, "field": self.field,
                "operator": self.operator, "value": self.value}


def compile_patch(edit: dict, binding: TaskBinding) -> list[PatchOp]:
    """Rule-based: apply the edit's new_value to every binding of the edited
    variable (via dependency_graph.propagate). Returns the ordered PatchOps."""
    var_id = edit.get("var_id")
    new_value = edit.get("new")
    if var_id is None or new_value is None:
        raise ValueError(f"edit needs var_id + new: {edit}")
    affected: list[EntityBinding] = propagate(edit, binding)
    ops = [PatchOp(app=b.app, entity_id=b.entity_id, field=b.field,
                   operator=b.operator, value=new_value) for b in affected]
    logger.info(f"[patch] edit {var_id}={new_value!r} → {len(ops)} ops: "
                f"{[(o.app, o.entity_id, o.operator) for o in ops]}")
    return ops


def compile_patch_model(edit: dict, binding: TaskBinding, *, model: str | None = None):
    """Optional frontier-model patch variant (NOT the W1 default). Provided so
    W1 can collect the sub-kill-2 data point (rule vs model patch_compiler on
    the 2 tasks → train-free evidence for W4 Go/No-Go). Returns the same shape
    as ``compile_patch`` for comparison. Not exercised by the default kill-test.

    When implemented (W4), it will call ``benchmark.model_client`` with a
    prompt built from the binding + edit; the rule-based ``compile_patch`` is
    the W1 default since patch generation is deterministic engineering, NOT the
    gate-critical model step (binding discovery is — see ``task_state/compiler``).
    """
    raise NotImplementedError(
        "model-variant patch_compiler is a W4 data-point; W1 uses rule-based "
        "compile_patch. Implement when collecting sub-kill-2 evidence.")
