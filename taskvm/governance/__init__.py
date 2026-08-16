"""taskvm.governance — the L4 governance entry (thin router onto the kernel).

Agent-C role collapse (2026-08-14, docs/contracts/architect.md): the
production governance path is :class:`GovernanceService` — six events,
one entry, kernel-facade commands, and (for GoalPatch ONLY) one Task
Architect recomposition. The legacy scripted/sim/human event-source stack
and the LLM SubgoalGenerator / rule-based workflow classifier / rollback
NL generator are GONE from production; their test fixtures live in
``tests/fakes/`` (nothing under taskvm/ may import them).

Legacy survivors below (vm_state / subgoal types / translate /
checkpoint_graph) exist ONLY because the not-yet-migrated legacy layers
(taskvm.workspace_ui, taskvm.execution, taskvm.harness, evaluation
phase scripts) still import them; they are staged for deletion by Agents
B/D/E/F (architect contract §10). New code must not build on them.
"""
from taskvm.governance.events import (
    ConflictResolutionRequested, GoalPatchRequested, GovernanceEvent,
    LocalPatchRequested, PauseRequested, ResumeRequested, RollbackRequested,
)
from taskvm.governance.service import (
    BootstrapResult, GoalRecomposeFailed, GovernanceOutcome,
    GovernanceService,
)

# ── legacy survivors (staged for B/D/E/F waves — see module docstring) ──
from taskvm.governance.vm_state import VMStateSnapshot
from taskvm.governance.subgoal import (SubgoalInstruction, WorkflowNode,
                                       WorkflowNodeType, WorkflowPlan)
from taskvm.governance.checkpoint_graph import (
    CheckpointGraph, CheckpointDirection,
)
from taskvm.governance.translate import (
    TITLE_FIELD, INTERNAL_ID_RE, OPERATOR_JARGON_RE,
    build_locator_index, build_locator_index_strict, resolve_locator,
    entity_id_to_locator, visible_entity_titles, assert_no_internal_id,
    assert_no_operator_jargon, eid_to_title_in_seed,
)

__all__ = [
    # the unified governance entry
    "GovernanceService", "GovernanceOutcome", "GovernanceEvent",
    "GoalRecomposeFailed", "BootstrapResult",
    "PauseRequested", "ResumeRequested", "LocalPatchRequested",
    "GoalPatchRequested", "RollbackRequested", "ConflictResolutionRequested",
    # legacy survivors
    "VMStateSnapshot",
    "SubgoalInstruction", "WorkflowNode", "WorkflowNodeType", "WorkflowPlan",
    "CheckpointGraph", "CheckpointDirection",
    "TITLE_FIELD", "INTERNAL_ID_RE", "OPERATOR_JARGON_RE",
    "build_locator_index", "build_locator_index_strict", "resolve_locator",
    "entity_id_to_locator", "visible_entity_titles", "assert_no_internal_id",
    "assert_no_operator_jargon", "eid_to_title_in_seed",
]
