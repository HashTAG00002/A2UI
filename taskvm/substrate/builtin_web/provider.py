"""builtin_web.provider — composition entry for the builtin web substrate.

``SubstrateRegistry.create_session("builtin_web", config)`` lands here.
The config dict decides WHICH app surface the session drives (URL/port
knowledge stays here + in ``launcher``); after creation the caller holds
a plain ``SubstrateSession`` and cannot tell web from any other substrate.
"""
from __future__ import annotations

from typing import Any

from taskvm.substrate.builtin_web.evaluation import (
    WebEvaluationEnvironment,
    make_evaluation_environment,
)
from taskvm.substrate.builtin_web.launcher import app_url
from taskvm.substrate.builtin_web.session import WebSubstrateSession


class BuiltinWebProvider:
    name = "builtin_web"

    def create_session(self, config: dict[str, Any] | None = None
                       ) -> WebSubstrateSession:
        cfg = dict(config or {})
        app = cfg.get("app") or "calendar"
        url = app_url(app,
                      host=cfg.get("host", "localhost"),
                      port=cfg.get("port"),
                      base_url=cfg.get("base_url"))
        return WebSubstrateSession(
            app=app, url=url, sid=cfg.get("sid", ""),
            viewport=tuple(cfg.get("viewport", (1100, 760))),
            screenshot_dir=cfg.get("screenshot_dir"),
        )


class BuiltinWebEvaluationProvider:
    """Evaluation-plane provider for the same substrate — a SEPARATE class
    with SEPARATE powers (reset/seed/oracle), reachable only via
    ``EvaluationRegistry.create("builtin_web", config)``."""
    name = "builtin_web"

    def create(self, config: dict[str, Any] | None = None
               ) -> WebEvaluationEnvironment:
        cfg = dict(config or {})
        app = cfg.get("app") or "calendar"
        return make_evaluation_environment(
            app,
            host=cfg.get("host", "localhost"),
            port=cfg.get("port"),
            base_url=cfg.get("base_url"),
            timeout=cfg.get("timeout", 10.0),
        )
