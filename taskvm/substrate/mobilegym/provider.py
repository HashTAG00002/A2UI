"""mobilegym.provider — composition entry for the MobileGym substrate.

App selection is catalog-driven (MG-FULL-APPS / PURETY-GEN): the config's
``app`` key is validated against ``app_catalog`` — an unknown app raises
an honest ``ValueError`` at composition time, never a silent fallback to
some hardcoded default. When the caller expresses no preference the
catalog's first app is the environment's choice (a stable, catalog-derived
default — not an app-specific hardcode).
"""
from __future__ import annotations

from typing import Any

from taskvm.substrate.mobilegym.app_catalog import (
    ALL_APP_IDS,
    is_valid_app_or_raise,
)
from taskvm.substrate.mobilegym.evaluation import (
    MobileGymEvaluationEnvironment,
)
from taskvm.substrate.mobilegym.session import MobileGymSubstrateSession

#: the surface handed to a session when the caller has no preference —
#: catalog-derived (first entry of the catalog order), never a per-app
#: hardcode. The surface is only the session's home label: the runtime
#: reaches EVERY catalog app through GuiAction(kind="open").
DEFAULT_SURFACE_APP: str = ALL_APP_IDS[0]


class MobileGymProvider:
    name = "mobilegym"

    def create_session(self, config: dict[str, Any] | None = None
                       ) -> MobileGymSubstrateSession:
        cfg = dict(config or {})
        app = cfg.get("app") or DEFAULT_SURFACE_APP
        is_valid_app_or_raise(app)      # honest ValueError, no silent default
        return MobileGymSubstrateSession(
            sid=cfg.get("sid", ""),
            bridge_url=cfg.get("bridge_url", "http://localhost:3019"),
            surface_app=app,
            timeout=cfg.get("timeout", 30.0),
        )


class MobileGymEvaluationProvider:
    name = "mobilegym"

    def create(self, config: dict[str, Any] | None = None
               ) -> MobileGymEvaluationEnvironment:
        cfg = dict(config or {})
        app = cfg.get("app") or DEFAULT_SURFACE_APP
        is_valid_app_or_raise(app)      # honest ValueError, no silent default
        return MobileGymEvaluationEnvironment(
            app=app,
            sid=cfg.get("sid", ""),
            bridge_url=cfg.get("bridge_url", "http://localhost:3019"),
            timeout=cfg.get("timeout", 10.0),
        )
