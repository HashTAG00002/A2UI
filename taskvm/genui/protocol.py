"""protocol — the single source of truth for A2UI protocol identity.

Every "v0.9" literal, catalog id, surface-id naming rule, message-envelope
constructor and data-model path convention lives HERE and nowhere else
(workplan §4 `protocol.py`; the repo contract forbids scattering the
version string across the codebase).

This module is dependency-free on purpose: no taskvm imports, no SDK
imports — the GenUI layer's public port is plain JSON shapes, so the
composition root (workspace_ui) can wire it without layering violations.
"""
from __future__ import annotations

import re
from typing import Any

#: The protocol version stamped into every server→client message envelope.
#: v0.9 is FROZEN for this wave (workplan §1.3: no v0.9/v0.9.1 mixing).
PROTOCOL_VERSION: str = "v0.9"

#: The A2UI catalog this runtime renders with. Must equal the vendored
#: mirror's ``$id`` (invariant tested in tests/protocol + tests/genui).
#: First wave is Basic Catalog ONLY — no custom TaskVM components
#: (workplan §6).
CATALOG_ID: str = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"

#: Prefix for every TaskVM-owned A2UI surface id.
_SURFACE_PREFIX: str = "taskvm-task-"

_SURFACE_ID_RE = re.compile(r"[^a-z0-9-]+")


def surface_id_for_session(session_id: str) -> str:
    """Deterministic, collision-free surface id for one task session.

    Rule: ``taskvm-task-<sanitized-session-id>`` where the sanitiser keeps
    ``[a-z0-9-]`` and collapses everything else to ``-``. A real user sees
    this string in no UI chrome (it is protocol plumbing, not content),
    but it is stable and traceable in every A2UI message.
    """
    cleaned = _SURFACE_ID_RE.sub(
        "-", (session_id or "").strip().lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)  # canonical: no dash runs
    if not cleaned:
        raise ValueError("surface_id_for_session: session_id must be non-empty")
    return _SURFACE_PREFIX + cleaned


# ── data-model path conventions (binding whitelist vocabulary) ─────────────

def variable_path(key: str, plane: str) -> str:
    """JSON-Pointer path to one variable's plane inside the surface data
    model. ``plane`` ∈ {"label", "observed", "desired", "mutability",
    "status", "confidence", "value_type"}."""
    _PLANES = ("label", "observed", "desired", "mutability", "status",
               "confidence", "value_type")
    if plane not in _PLANES:
        raise ValueError(f"unknown variable plane {plane!r}; expected one of {_PLANES}")
    if not key:
        raise ValueError("variable key must be non-empty")
    return f"/variables/{key}/{plane}"


def task_path(field: str) -> str:
    """Path to a scalar field under ``/task`` (goal / status)."""
    if field not in ("goal", "status"):
        raise ValueError(f"unknown task field {field!r}")
    return f"/task/{field}"


#: The ONLY action a model-generated dynamic task surface may emit in this
#: wave (workplan §10). Everything else is a 4xx — no best-effort guessing.
ACTION_LOCAL_PATCH: str = "taskvm.local_patch"

#: The allowlist for dynamic-surface actions (small by design).
ALLOWED_SURFACE_ACTIONS: frozenset[str] = frozenset({ACTION_LOCAL_PATCH})

#: Governance actions belong to the FIXED shell (trusted chrome) and can
#: never be minted by the model-generated surface. Listed explicitly so a
#: violation produces an honest "this is governance-owned" error instead
#: of a generic unknown-action error.
GOVERNANCE_ACTION_NAMES: frozenset[str] = frozenset({
    "start", "pause", "resume", "stop", "checkpoint", "rollback",
    "goal_patch", "recompose", "resolve_conflict",
})

#: Component-id namespace reserved for the fixed Governance Shell. The
#: dynamic surface must never create, hide or replace governance controls
#: (workplan §4 `policy.py`).
RESERVED_ID_PREFIXES: tuple[str, ...] = ("governance-", "gov-")

ROOT_COMPONENT_ID: str = "root"


# ── message envelope constructors ──────────────────────────────────────────
#
# The server deterministically produces createSurface / updateDataModel /
# surface ids; the GenUI decoder only ever produces the *components* list
# (workplan §5 — "the model generates structure, the server owns facts").

def create_surface_message(surface_id: str) -> dict[str, Any]:
    return {"version": PROTOCOL_VERSION,
            "createSurface": {"surfaceId": surface_id,
                              "catalogId": CATALOG_ID}}


def update_components_message(surface_id: str,
                              components: list[dict[str, Any]]
                              ) -> dict[str, Any]:
    return {"version": PROTOCOL_VERSION,
            "updateComponents": {"surfaceId": surface_id,
                                 "components": components}}


def update_data_model_message(surface_id: str, value: Any,
                              path: str = "/") -> dict[str, Any]:
    return {"version": PROTOCOL_VERSION,
            "updateDataModel": {"surfaceId": surface_id,
                                "path": path,
                                "value": value}}


def delete_surface_message(surface_id: str) -> dict[str, Any]:
    return {"version": PROTOCOL_VERSION,
            "deleteSurface": {"surfaceId": surface_id}}
