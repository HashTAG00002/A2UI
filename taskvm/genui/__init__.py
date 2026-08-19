"""taskvm.genui — the GenUI production layer (A2UI dynamic task surface).

Public surface (composition root = workspace_ui; everything above takes
plain JSON / value objects, no kernel references):

- protocol: version/catalog/surface-id single source of truth + message
  constructors;
- TaskSurfaceContextBuilder: public snapshot → model-facing context;
- TaskDataModelProjector: context → deterministic A2UI data model;
- validate_components: the two-layer gate (SDK schema + TaskVM policy);
- baseline_components: the generic deterministic fallback surface;
- GenUIDecoder (A4): the real model-backed decode loop — model call →
  two-layer validation → one bounded repair → honest baseline fallback,
  every provider request landing exactly one shared-ledger row
  (role=genui_decoder);
- SurfaceStore / SurfaceStoreRegistry: per-session ordered message
  stream (bootstrap + SSE tail);
- ActionRouter (A6): renderer action → structured LocalPatchIntent
  (C2S write-path validation half; execution belongs to the
  composition root, which hands the intent to the session's
  GovernanceService-backed governance port);
- schema: vendored-mirror → official a2ui-agent-sdk catalog/validator.

The layer is a plain-JSON port layer (tests/genui/test_imports.py): the
model port and ledger are INJECTED by the composition root — no taskvm
layer is imported here, keeping substrate independence by construction.
"""
from taskvm.genui.action_router import (
    ActionRouteError, ActionRouter, LocalPatchIntent,
)
from taskvm.genui.baseline import baseline_components
from taskvm.genui.context import (
    TaskSurfaceContext, TaskSurfaceContextBuilder, SurfaceVariable,
)
from taskvm.genui.data_model import TaskDataModelProjector
from taskvm.genui.decoder import (
    DecodeAttempt, DecodeResult, GenUIDecoder, SOURCE_FALLBACK,
    SOURCE_MODEL,
)
from taskvm.genui.policy import SurfacePolicy
from taskvm.genui.protocol import (
    ACTION_LOCAL_PATCH, ALLOWED_SURFACE_ACTIONS, CATALOG_ID,
    GENUI_DECODER_MODEL_ROLE, PROTOCOL_VERSION, surface_id_for_session,
)
from taskvm.genui.store import SurfaceStore, SurfaceStoreRegistry
from taskvm.genui.validator import (
    ComponentValidationError, validate_components,
)

__all__ = [
    "ACTION_LOCAL_PATCH",
    "ALLOWED_SURFACE_ACTIONS",
    "ActionRouteError",
    "ActionRouter",
    "CATALOG_ID",
    "ComponentValidationError",
    "DecodeAttempt",
    "DecodeResult",
    "GENUI_DECODER_MODEL_ROLE",
    "GenUIDecoder",
    "LocalPatchIntent",
    "PROTOCOL_VERSION",
    "SOURCE_FALLBACK",
    "SOURCE_MODEL",
    "SurfacePolicy",
    "SurfaceStore",
    "SurfaceStoreRegistry",
    "SurfaceVariable",
    "TaskDataModelProjector",
    "TaskSurfaceContext",
    "TaskSurfaceContextBuilder",
    "baseline_components",
    "surface_id_for_session",
    "validate_components",
]
