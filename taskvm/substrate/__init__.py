"""taskvm.substrate — L0 substrate adapters (E17-C, protocol stack).

The StateAdapter abstraction + per-substrate implementations:
  - ``base``: the StateAdapter ABC + make_adapter/make_adapters factory + the
    builtin app adapters (calendar/taskboard/drive/mail/outlook_cal) + the
    MobileGym adapters (wechat/alipay). Co-located in base.py because they
    share the StateAdapter contract (handoff §3.2 lists substrate/builtin/
    adapters.py as a future split — not required by any caller today).
  - ``mobilegym.bridge``: the MobileGymBridge async HTTP shim (moved from
    harness/mobilegym_bridge.py).
  - ``osworld``: OSWorld adapter (future — placeholder only).

Old import paths (``taskvm.harness.state_adapter``,
``taskvm.harness.mobilegym_bridge``) remain valid via file-level re-export
shims — zero regression.
"""
from taskvm.substrate.base import (
    StateAdapter, make_adapter, make_adapters,
)

__all__ = ["StateAdapter", "make_adapter", "make_adapters"]
