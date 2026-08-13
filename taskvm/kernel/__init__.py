"""taskvm.kernel — the L3 state machine: stores + event log + patch/checkpoint
semantics (master handoff §2 'L3 TaskVM Kernel').

Depends ONLY on taskvm.domain (+ stdlib). Knows nothing about Flask,
Playwright, model providers, substrates, benchmarks, or evaluation —
enforced by tests/architecture.

Public surface (stable contract — docs/contracts/kernel.md):
    TaskVMKernel            the facade (all mutation flows)
    EventLog                append-only history
    TaskSessionStore        task state head + execution epoch
    ProjectionStore         schema/data with independent revisions
    WorkflowStore           plan + node statuses (invariant 3)
    CheckpointStore         verified boundaries (invariant 5)
    ProjectionSnapshot / WorkflowSnapshot / CheckpointRecord
"""
from taskvm.kernel.checkpoint_store import CheckpointRecord, CheckpointStore
from taskvm.kernel.event_log import EventLog
from taskvm.kernel.kernel import TaskVMKernel
from taskvm.kernel.projection_store import ProjectionSnapshot, ProjectionStore
from taskvm.kernel.session_store import TaskSessionStore
from taskvm.kernel.workflow_store import WorkflowSnapshot, WorkflowStore

__all__ = [
    "TaskVMKernel",
    "EventLog",
    "TaskSessionStore",
    "ProjectionStore",
    "WorkflowStore",
    "CheckpointStore",
    "ProjectionSnapshot",
    "WorkflowSnapshot",
    "CheckpointRecord",
]
