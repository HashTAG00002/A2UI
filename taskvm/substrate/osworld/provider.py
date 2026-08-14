"""osworld.provider — composition entry for the OSWorld substrate."""
from __future__ import annotations

import os
from typing import Any

from taskvm.substrate.osworld.session import (
    HttpOSWorldRuntime, OSWorldSubstrateSession,
)
from taskvm.substrate.port import SubstrateUnavailable


class OSWorldProvider:
    name = "osworld"

    def create_session(self, config: dict[str, Any] | None = None
                       ) -> OSWorldSubstrateSession:
        cfg = dict(config or {})
        endpoint = (cfg.get("endpoint")
                    or os.environ.get("TASKVM_OSWORLD_ENDPOINT"))
        if not endpoint:
            raise SubstrateUnavailable(
                "OSWorld endpoint not configured: pass config['endpoint'] "
                "or set TASKVM_OSWORLD_ENDPOINT to the remote-agent service "
                "URL. Honest unavailability — no fake desktop is "
                "substituted.")
        runtime = HttpOSWorldRuntime(endpoint,
                                     token=cfg.get("token"),
                                     timeout=cfg.get("timeout", 30.0))
        return OSWorldSubstrateSession(runtime)


class OSWorldEvaluationProvider:
    """OSWorld evaluation env: reset/seed via the benchmark runner's VM
    snapshot controls. Skeleton — see session docstring for the honesty
    note; oracle_state reads the judge service, not the runtime port."""
    name = "osworld"

    def create(self, config: dict[str, Any] | None = None):
        raise SubstrateUnavailable(
            "OSWorld evaluation environment is a skeleton in this wave "
            "(no VM attached); runtime sessions ARE available. See "
            "03_SUBSTRATE_ISOLATION_AGENT remaining blockers.")
