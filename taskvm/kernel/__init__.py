"""taskvm.kernel — the L3 state machine (master handoff §2 'L3 TaskVM Kernel').

Depends ONLY on taskvm.domain (+ stdlib). Knows nothing about Flask,
Playwright, model providers, substrates, benchmarks, or evaluation —
enforced by tests/architecture.

PUBLIC SURFACE (frozen contract — docs/contracts/kernel.md):
    TaskVMKernel            the facade — the ONLY cross-layer entry point
    ProjectionSnapshot / WorkflowSnapshot / CheckpointRecord
                            immutable read models

The mutable Store classes (EventLog, TaskSessionStore, ProjectionStore,
WorkflowStore, CheckpointStore) are kernel-internal modules. Upper layers
MUST NOT import them (the architecture gate rejects
``taskvm.kernel.*_store`` / ``taskvm.kernel.event_log`` imports from
outside this package): all interaction goes through the facade and its
snapshots.
"""
from taskvm.kernel.checkpoint_store import CheckpointRecord
from taskvm.kernel.kernel import TaskVMKernel
from taskvm.kernel.projection_store import ProjectionSnapshot
from taskvm.kernel.workflow_store import WorkflowSnapshot

__all__ = [
    "TaskVMKernel",
    "ProjectionSnapshot",
    "WorkflowSnapshot",
    "CheckpointRecord",
]
