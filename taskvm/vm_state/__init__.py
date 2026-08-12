"""taskvm.vm_state — L1 VM-state layer (E17-C, protocol stack).

The binding compiler + entity binding + dependency graph + projection policy +
representation + the verifier package. Per handoff §3.2 this is the new home
for the L1 layer (formerly split across ``task_state/`` and ``verifier/``).

E17-C implementation choice (zero-regression): rather than physically moving
``task_state/`` and ``verifier/`` (which have many import sites across
baselines/evaluation/workspace_ui), this package RE-EXPORTS them. Old paths
(``taskvm.task_state.*``, ``taskvm.verifier.*``) remain the real homes; this
package provides the new ``taskvm.vm_state.*`` path as aliases. A future
refactor can physically move the files once the import surface is migrated.

The verifier sub-package is exposed as ``taskvm.vm_state.verifier`` (re-export
of ``taskvm.verifier``).
"""
# L1 state-model re-exports (alias the existing task_state path)
from taskvm.task_state.compiler import compile_binding
from taskvm.task_state.entity_binding import (
    TaskBinding, EntityBinding, Dependency, OPERATOR_REGISTRY,
)
from taskvm.task_state.representation import TaskVariable, TaskStateGraph
from taskvm.task_state.dependency_graph import propagate
from taskvm.task_state.projection_policy import decide_visible, compression_ratio

__all__ = [
    # compiler
    "compile_binding",
    # entity_binding
    "TaskBinding", "EntityBinding", "Dependency", "OPERATOR_REGISTRY",
    # representation
    "TaskVariable", "TaskStateGraph",
    # dependency_graph
    "propagate",
    # projection_policy
    "decide_visible", "compression_ratio",
]
