"""taskvm.substrate.mobilegym — the MobileGym substrate (E17-C).

Holds the MobileGymBridge (async HTTP shim between TaskVM and the MobileGym
Playwright phone-sim) + fixtures. Moved from ``taskvm/harness/`` per handoff
§3.2; the old path ``taskvm.harness.mobilegym_bridge`` re-exports from here.
"""
from taskvm.substrate.mobilegym.bridge import MobileGymBridge, build_app

__all__ = ["MobileGymBridge", "build_app"]
