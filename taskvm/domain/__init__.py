"""taskvm.domain — pure data + invariants (master handoff §3.1).

This package is the bottom of the dependency stack. It imports ONLY the
Python standard library; it must never import Flask / Playwright / a model
client / requests / benchmark / evaluation / any concrete substrate
(enforced by tests/architecture).

Public surface (stable contract — see docs/contracts/kernel.md):
    Intent & state:   TaskIntent, TaskVariable, TaskState,
                      SurfaceHandle, SurfaceEvidence
    Projection:       ProjectionSchema, ProjectionData, ProjectionRevision,
                      ProjectionComponent
    Workflow:         WorkflowGraph, WorkflowNode, NodeKind, NodeStatus
    Action contract:  ActionContract, Reversibility
    Patches:          Patch, LocalPatch, GoalPatch, CompensationPatch,
                      VariableUpdate, NodeContractOverride, requires_replan
    Events:           Event, EventKind
    Errors:           TaskVMError and subclasses
"""
from taskvm.domain.contract import ActionContract, Reversibility
from taskvm.domain.errors import (
    CompensationMismatchError,
    CommittedNodeViolationError,
    PatchSemanticsError,
    RevisionConflictError,
    StaleEpochError,
    TaskVMError,
    UnknownCheckpointError,
    ValidationError,
)
from taskvm.domain.events import Event, EventKind
from taskvm.domain.intent import TaskIntent
from taskvm.domain.patch import (
    CompensationEntry,
    CompensationPatch,
    CompensationPlan,
    GoalPatch,
    LocalPatch,
    NodeContractOverride,
    Patch,
    VariableUpdate,
    requires_replan,
)
from taskvm.domain.projection import (
    ProjectionComponent,
    ProjectionData,
    ProjectionRevision,
    ProjectionSchema,
)
from taskvm.domain.state import (
    MUTABILITY_EDITABLE,
    MUTABILITY_LOCKED,
    MUTABILITY_READONLY,
    ObservedValue,
    SurfaceEvidence,
    SurfaceHandle,
    TaskState,
    TaskVariable,
)
from taskvm.domain.workflow import (
    HISTORICAL_STATUSES,
    NodeKind,
    NodeStatus,
    WorkflowGraph,
    WorkflowNode,
)

__all__ = [
    # intent & state
    "TaskIntent", "TaskVariable", "TaskState", "ObservedValue",
    "SurfaceHandle", "SurfaceEvidence",
    "MUTABILITY_EDITABLE", "MUTABILITY_READONLY", "MUTABILITY_LOCKED",
    # projection
    "ProjectionSchema", "ProjectionData", "ProjectionRevision",
    "ProjectionComponent",
    # workflow
    "WorkflowGraph", "WorkflowNode", "NodeKind", "NodeStatus",
    "HISTORICAL_STATUSES",
    # action contract
    "ActionContract", "Reversibility",
    # patches
    "Patch", "LocalPatch", "GoalPatch", "CompensationPatch",
    "CompensationEntry", "CompensationPlan",
    "VariableUpdate", "NodeContractOverride", "requires_replan",
    # events
    "Event", "EventKind",
    # errors
    "TaskVMError", "ValidationError", "RevisionConflictError",
    "StaleEpochError", "CommittedNodeViolationError",
    "UnknownCheckpointError", "CompensationMismatchError",
    "PatchSemanticsError",
]
