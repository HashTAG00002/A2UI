"""taskvm.projection — the L5 Projection & User Frontend (frozen contract:
docs/contracts/projection.md).

The read-side mirror + governance command entry: projects Kernel snapshots
and Runtime events/artifacts into a continuously living task UI. Never
plans, never executes a GUI action inside a route, never calls a model for
ordinary updates, never learns the substrate (the architecture gate bans
``taskvm.substrate`` from this package — sessions are REGISTERED by the
composition root).

Public surface (frozen contract §0/§5/§6):
    ProjectionSessionStore / ProjectionSession / ArtifactStore
        the composition seam (kernel + optional runtime + artifacts)
    create_app(store, ...)                       the Flask factory
    KernelGovernancePort / GoalRecomposer        governance command ports
    ThreadedRuntimeDriver                        autonomy driver (threaded)
    snapshot_view / workflow_view / ...          pure view-model builders
    sse_* helpers / KERNEL_EVENT_SSE             the typed event adapter
"""
from taskvm.projection.app import create_app
from taskvm.projection.events import (
    KERNEL_EVENT_SSE, RUNTIME_EVENT_SSE, sse_envelope,
)
from taskvm.projection.services.driver import (
    RuntimeDriverPort, ThreadedRuntimeDriver,
)
from taskvm.projection.services.governance import (
    GoalRecomposer, KernelGovernancePort,
)
from taskvm.projection.store import (
    ArtifactStore, ProjectionSession, ProjectionSessionStore, SurfaceDecl,
)
from taskvm.projection.view_models import (
    checkpoint_view, conflicts_view, governance_view, snapshot_view,
    surface_cards, variables_view, workflow_view,
)

__all__ = [
    # composition seam
    "ProjectionSessionStore", "ProjectionSession", "ArtifactStore",
    "SurfaceDecl",
    # app
    "create_app",
    # governance + driver ports
    "KernelGovernancePort", "GoalRecomposer",
    "RuntimeDriverPort", "ThreadedRuntimeDriver",
    # view models
    "snapshot_view", "governance_view", "variables_view", "workflow_view",
    "surface_cards", "checkpoint_view", "conflicts_view",
    # event adapter
    "KERNEL_EVENT_SSE", "RUNTIME_EVENT_SSE", "sse_envelope",
]
