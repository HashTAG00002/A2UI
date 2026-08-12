"""Backward-compat shim — the real home is now ``taskvm/substrate/mobilegym/bridge.py``.

E17-C (2026-08-12): the MobileGymBridge + build_app + main moved to
``taskvm.substrate.mobilegym.bridge`` (the L0 MobileGym substrate, handoff
§3.2). This file re-exports the public surface AND preserves the
``python -m taskvm.harness.mobilegym_bridge`` module-path invocation (used by
``run_x_toggle_ablation.py:106``) — a ``__init__.py`` re-export alone cannot
do that, so this file-level shim is mandatory (recon area 8).

New code should import from ``taskvm.substrate.mobilegym.bridge``.
"""
from taskvm.substrate.mobilegym.bridge import (  # noqa: F401
    MobileGymBridge, build_app, main,
    SITE, DEFAULT_PORT, APPS, DEFAULT_SIM_URL,
)

if __name__ == "__main__":
    main()
