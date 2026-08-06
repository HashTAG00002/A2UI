"""Dependency graph — effect propagation from a user edit.

Given a user edit (var_id → new_value), return the EntityBindings that must be
applied. W1: returns the edited variable's own bindings (the fixtures' bindings
already capture all direct effects of each variable). Cross-variable derived
effects (a dependency pointing to an entity NOT in the edited var's bindings)
are logged for W2+ propagation; W1 keeps it simple.

This is NOT the gate-critical step — binding discovery (compiler) is. Propagation
is deterministic engineering over the now-fixed binding.
"""
from __future__ import annotations

import logging

from taskvm.task_state.entity_binding import EntityBinding, TaskBinding

logger = logging.getLogger(__name__)


def propagate(edit: dict, binding: TaskBinding) -> list[EntityBinding]:
    """Return the EntityBindings to apply for a user edit ``{var_id, old, new}``.

    W1: the edited variable's own bindings. If a dependency points to an entity
    not in those bindings, it's logged (W2+ would follow it).
    """
    var_id = edit.get("var_id")
    if not var_id:
        raise ValueError(f"edit missing var_id: {edit}")
    own = binding.bindings_for(var_id)
    own_keys = {(b.app, b.entity_id) for b in own}
    # W2+: follow dependencies to entities not in own bindings
    extra: list[EntityBinding] = []
    for dep in binding.dependencies:
        if dep.from_var == var_id and (dep.to_app, dep.to_entity_id) not in own_keys:
            logger.info(f"[propagate] W2+ dependency {dep.from_var}→"
                        f"({dep.to_app},{dep.to_entity_id}) not in own bindings; "
                        f"W1 skips, W2+ would propagate")
    return own + extra
