"""taskvm.governance — the L4 governance entry (thin router onto the kernel).

The production governance path is :class:`GovernanceService` (docs/
contracts/architect.md): six events,
one entry, kernel-facade commands, and (for GoalPatch ONLY) one Task
Architect recomposition. The runtime plane is
``GovernanceService`` + ``taskvm.runtime.AutonomyRuntime`` over the
substrate port; nothing under taskvm/ imports state shims.
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
