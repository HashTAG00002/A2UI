"""action_router — renderer action → structured governance intent (A6).

The A2UI C2S write path's validation + translation half (workplan §4
``action_router.py``). The renderer dispatches ``action.event`` with
data bindings ALREADY RESOLVED (protocol: client_to_server.json's
context is "after resolving all data bindings"), so the router
re-validates the literal payload against the same ground truth the
policy layer uses (allowlist / mutability / value type) and produces
a STRUCTURED :class:`LocalPatchIntent` — plain JSON-able data, no
natural-language re-translation, no model call (workplan §20.2: the
"middle model" ban — governance semantics travel structured all the
way into the governance entry).

Layering (tests/genui/test_imports.py locks genui to a plain-JSON
port layer): this module imports NOTHING from other taskvm layers.
The intent's EXECUTION belongs to the composition root
(workspace_ui), which hands it to the session's governance port —
the GovernanceService-backed adapter composition already registers.
The dynamic surface may only ever mint ``taskvm.local_patch``;
governance events (pause/rollback/…) are fixed-shell territory and
are rejected with an explicit governance-owned error, mirroring the
policy layer's S2C rule one-for-one (same vocabulary, both
directions).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from taskvm.genui.context import TaskSurfaceContext
from taskvm.genui.protocol import (
    ACTION_LOCAL_PATCH, GOVERNANCE_ACTION_NAMES,
)

__all__ = [
    "ActionRouteError",
    "ActionRouter",
    "LocalPatchIntent",
]


class ActionRouteError(ValueError):
    """Honest routing rejection. ``http_status`` mirrors the transport's
    write-path contract: 400 malformed/unknown, 403 ownership. The
    message is user-presentable (it rides the POST response verbatim
    — no guessing, no best-effort repair)."""

    def __init__(self, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True)
class LocalPatchIntent:
    """ONE structured local-patch intent — the C2S twin of
    ``LocalPatchRequested``. ``updates`` maps semantic keys to NEW
    desired values (literals — bindings were resolved client-side),
    ``rationale``/``correlation_id`` ride the governance event."""

    updates: dict[str, Any]
    rationale: str = ""
    correlation_id: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {"kind": "local_patch",
                "updates": dict(self.updates),
                "rationale": self.rationale,
                "correlation_id": self.correlation_id}


def is_data_binding(value: Any) -> bool:
    """Protocol-native DynamicValue shape ``{"path": …}`` — legitimate
    on the S2C tree (policy judges its path against the whitelist) but
    NEVER acceptable on the C2S write path: the client resolves
    bindings before dispatch, so an unresolved one reaching the router
    means a non-conforming client. Fail closed with an explicit error
    (never resolve it server-side — the server would be inventing the
    user's edit value)."""
    return (isinstance(value, dict) and set(value.keys()) == {"path"}
            and isinstance(value["path"], str))


class ActionRouter:
    """Validate one renderer action against the surface ground truth
    and mint the structured intent. Stateless per call: ``context`` is
    the CURRENT TaskSurfaceContext (mutability ground truth), supplied
    by the composition root per request."""

    def __init__(self, context: TaskSurfaceContext) -> None:
        self._context = context

    # ── the entry ─────────────────────────────────────────────────────
    def route(self, name: Any,
              context: Mapping[str, Any] | None) -> LocalPatchIntent:
        """Renderer action (name + resolved context) → LocalPatchIntent.

        Raises :class:`ActionRouteError` for every honest rejection —
        unknown action 400, governance-owned 403, malformed payload
        400, readonly variable 403, bad value type 400."""
        ctx = dict(context or {})
        if name != ACTION_LOCAL_PATCH:
            if name in GOVERNANCE_ACTION_NAMES:
                raise ActionRouteError(
                    f"action {name!r} is governance-owned — the dynamic "
                    "surface may never emit it (fixed shell territory)",
                    http_status=403)
            raise ActionRouteError(
                f"unknown action {name!r} (allowlist: "
                f"[{ACTION_LOCAL_PATCH}])")
        key = ctx.get("semanticKey")
        if not isinstance(key, str) or not key:
            raise ActionRouteError(
                f"{ACTION_LOCAL_PATCH} requires a non-empty "
                "context.semanticKey")
        if "value" not in ctx or ctx.get("value") is None:
            raise ActionRouteError(
                f"{ACTION_LOCAL_PATCH} needs context.value (the edited "
                "desired value)")
        value = ctx["value"]
        if is_data_binding(value):
            raise ActionRouteError(
                "context.value arrived as an unresolved data binding "
                f"({value['path']!r}) — the client must resolve "
                "bindings before dispatch (client_to_server.json)")
        var = self._context.variable(key)
        if var is None:
            raise ActionRouteError(f"unknown semantic key {key!r}")
        if not var.editable:
            raise ActionRouteError(
                f"variable {key!r} is {var.mutability} — local_patch "
                "only targets editable variables", http_status=403)
        self._check_value_type(var, value)
        return LocalPatchIntent(
            updates={key: value},
            rationale=str(ctx.get("rationale") or "a2ui surface action"))

    # ── literal type gate ─────────────────────────────────────────────
    @staticmethod
    def _check_value_type(var, value: Any) -> None:
        """Identical literal semantics to SurfacePolicy._check_value_type
        (post A5-IFACE-01) — one rule set, two enforcement points (S2C
        tree validation + C2S write path). bool never poses as a
        number."""
        vt = var.value_type
        bad = False
        if vt == "boolean":
            bad = not isinstance(value, bool)
        elif vt in ("number",):
            bad = not isinstance(value, (int, float)) \
                or isinstance(value, bool)
        elif vt in ("integer",):
            bad = not isinstance(value, int) or isinstance(value, bool)
        elif vt in ("string", "date", "text", "status"):
            bad = not isinstance(value, str)
        if bad:
            raise ActionRouteError(
                f"variable {var.semantic_key!r} ({vt}) rejects value "
                f"{value!r}")
