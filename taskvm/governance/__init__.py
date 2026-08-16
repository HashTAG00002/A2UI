"""taskvm.governance — the L4 governance entry (thin router onto the kernel).

Agent-C role collapse (2026-08-14, docs/contracts/architect.md): the
production governance path is :class:`GovernanceService` — six events,
one entry, kernel-facade commands, and (for GoalPatch ONLY) one Task
Architect recomposition. The legacy scripted/sim/human event-source stack,
the vm_state / subgoal / translate / checkpoint_graph survivors and the
LLM SubgoalGenerator were deleted by Agent G's Wave-3 cluster deletion
(2026-08-16, handoff 08 ⭐): the runtime plane is
``GovernanceService`` + ``taskvm.runtime.AutonomyRuntime`` over the
substrate port, and nothing under taskvm/ imports legacy state shims.
"""
from taskvm.governance.events import (
    ConflictResolutionRequested, GoalPatchRequested, GovernanceEvent,
    LocalPatchRequested, PauseRequested, ResumeRequested, RollbackRequested,
)
from taskvm.governance.service import (
    BootstrapResult, GoalRecomposeFailed, GovernanceOutcome,
    GovernanceService,
)

__all__ = [
    # the unified governance entry
    "GovernanceService", "GovernanceOutcome", "GovernanceEvent",
    "GoalRecomposeFailed", "BootstrapResult",
    "PauseRequested", "ResumeRequested", "LocalPatchRequested",
    "GoalPatchRequested", "RollbackRequested", "ConflictResolutionRequested",
]
