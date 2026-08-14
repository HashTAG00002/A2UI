"""taskvm.substrate.mobilegym — the MobileGym substrate (Agent B).

Everything MobileGym-specific lives here:
  - ``bridge``: the resident aiohttp bridge holding one ``MobileGymEnv``
    (start with ``python -m taskvm.substrate.mobilegym.bridge``; inject the
    L2 CUA loop at assembly time via ``--cua-loop <module>`` — the bridge
    itself never imports upper layers);
  - ``session``: the unified-port SubstrateSession (observe/act/capture
    over the bridge's L1 primitive routes);
  - ``evaluation``: the EvaluationEnvironment (reset/seed/oracle_state —
    the only place ``set_state`` semantics are reachable);
  - ``provider``: composition entries for both registries.

The old ``taskvm.harness.mobilegym_bridge`` shim is deleted.
"""
from taskvm.substrate.mobilegym.bridge import (
    MobileGymBridge, build_app, main,
    SITE, DEFAULT_PORT, APPS, DEFAULT_SIM_URL,
)
from taskvm.substrate.mobilegym.evaluation import (
    MobileGymEvaluationEnvironment, make_mobilegym_environments,
)
from taskvm.substrate.mobilegym.provider import (
    MobileGymProvider, MobileGymEvaluationProvider,
)
from taskvm.substrate.mobilegym.session import MobileGymSubstrateSession

__all__ = [
    "MobileGymBridge", "build_app", "main",
    "SITE", "DEFAULT_PORT", "APPS", "DEFAULT_SIM_URL",
    "MobileGymEvaluationEnvironment", "make_mobilegym_environments",
    "MobileGymProvider", "MobileGymEvaluationProvider",
    "MobileGymSubstrateSession",
]
