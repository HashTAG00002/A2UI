"""mobilegym.provider — composition entry for the MobileGym substrate."""
from __future__ import annotations

from typing import Any

from taskvm.substrate.mobilegym.evaluation import (
    MobileGymEvaluationEnvironment,
)
from taskvm.substrate.mobilegym.session import MobileGymSubstrateSession


class MobileGymProvider:
    name = "mobilegym"

    def create_session(self, config: dict[str, Any] | None = None
                       ) -> MobileGymSubstrateSession:
        cfg = dict(config or {})
        return MobileGymSubstrateSession(
            sid=cfg.get("sid", ""),
            bridge_url=cfg.get("bridge_url", "http://localhost:3019"),
            surface_app=cfg.get("app", "wechat"),
            timeout=cfg.get("timeout", 30.0),
        )


class MobileGymEvaluationProvider:
    name = "mobilegym"

    def create(self, config: dict[str, Any] | None = None
               ) -> MobileGymEvaluationEnvironment:
        cfg = dict(config or {})
        app = cfg.get("app", "wechat")
        return MobileGymEvaluationEnvironment(
            app=app,
            sid=cfg.get("sid", ""),
            bridge_url=cfg.get("bridge_url", "http://localhost:3019"),
            timeout=cfg.get("timeout", 10.0),
        )
