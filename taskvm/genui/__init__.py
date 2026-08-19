"""taskvm.genui — the GenUI production layer (A2UI dynamic task surface).

Public surface (composition root = workspace_ui; everything above takes
plain JSON / value objects, no kernel references):

- protocol: version/catalog/surface-id single source of truth + message
  constructors;
- TaskSurfaceContextBuilder: public snapshot → model-facing context;
- TaskDataModelProjector: context → deterministic A2UI data model;
- validate_components: the two-layer gate (SDK schema + TaskVM policy);
- SurfaceStore / SurfaceStoreRegistry: per-session ordered message
  stream (bootstrap + SSE tail);
- schema: vendored-mirror → official a2ui-agent-sdk catalog/validator.

The real model-backed decoder (A4) lives one wave ahead; this package
deliberately ships without any model-call code so the deterministic half
(§5 boundary: model generates structure, server owns facts) is fully
testable first.
"""
from taskvm.genui.context import (
    TaskSurfaceContext, TaskSurfaceContextBuilder, SurfaceVariable,
)
from taskvm.genui.data_model import TaskDataModelProjector
from taskvm.genui.policy import SurfacePolicy
from taskvm.genui.protocol import (
    ACTION_LOCAL_PATCH, ALLOWED_SURFACE_ACTIONS, CATALOG_ID,
    PROTOCOL_VERSION, surface_id_for_session,
)
from taskvm.genui.store import SurfaceStore, SurfaceStoreRegistry
from taskvm.genui.validator import (
    ComponentValidationError, validate_components,
)

__all__ = [
    "ACTION_LOCAL_PATCH",
    "ALLOWED_SURFACE_ACTIONS",
    "CATALOG_ID",
    "ComponentValidationError",
    "PROTOCOL_VERSION",
    "SurfacePolicy",
    "SurfaceStore",
    "SurfaceStoreRegistry",
    "SurfaceVariable",
    "TaskDataModelProjector",
    "TaskSurfaceContext",
    "TaskSurfaceContextBuilder",
    "surface_id_for_session",
    "validate_components",
]
