"""taskvm.runtime — the L2 Autonomy Runtime (frozen contract:
docs/contracts/runtime.md).

The execution clock: drives the kernel's action lifecycle forward while no
governance event blocks it; discards stale CUA responses through the kernel's
own start_action gate; runs real-GUI compensation; synchronizes surfaces by
observation (not hidden canonical polling). stdlib-only Python — the real
HTTP model / Playwright / mobile bridge live behind injected Protocol ports
(see runtime_rfc_backlog.md RFC-001).

Public surface (frozen contract — docs/contracts/runtime.md):
    AutonomyRuntime           the facade — the runtime entry point
    SurfaceSync, CompensationExecutor   internal collaborators (injectable)
    CUAGoalSerializer, CUAModel, ObservationExtractor, Verifier, CallLedger
                             the DI Protocol ports
    CUADecision, CUADecisionKind, ModelCallRecord, MODEL_ROLE_CUA
    RuntimeEvent, RuntimeEventKind
    RuntimeBudgets, DEFAULT_BUDGETS
    StructureInvalidation    the structural-drift signal the extractor raises
"""
from taskvm.runtime.autonomy import AutonomyRuntime
from taskvm.runtime.bootstrap import RuntimePorts, compose_runtime
from taskvm.runtime.compensation import CompensationExecutor
from taskvm.runtime.config import DEFAULT_BUDGETS, RuntimeBudgets
from taskvm.runtime.ports import (
    CallLedger, CUADecision, CUADecisionKind, CUAGoalSerializer, CUAModel,
    ModelCallRecord, MODEL_ROLE_CUA, ObservationExtractor, RuntimeEvent,
    RuntimeEventKind, Verifier,
)
from taskvm.runtime.sync import StructureInvalidation, SurfaceSync

__all__ = [
    "AutonomyRuntime", "SurfaceSync", "CompensationExecutor",
    # bootstrap seam (composition entry point — substrate.md §8 T1)
    "RuntimePorts", "compose_runtime",
    # ports
    "CUAGoalSerializer", "CUAModel", "ObservationExtractor", "Verifier",
    "CallLedger",
    # cua + accounting
    "CUADecision", "CUADecisionKind", "ModelCallRecord", "MODEL_ROLE_CUA",
    # runtime events
    "RuntimeEvent", "RuntimeEventKind",
    # budgets
    "RuntimeBudgets", "DEFAULT_BUDGETS",
    # sync signal
    "StructureInvalidation",
]
