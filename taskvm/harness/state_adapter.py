"""Backward-compat shim — the real home is now ``taskvm/substrate/base.py``.

E17-C (2026-08-12): the StateAdapter base + all 7 subclasses + make_adapter /
make_adapters + the registry constants moved to ``taskvm.substrate.base`` (the
L0 substrate layer of the protocol stack, handoff §3.2). This file re-exports
the full public surface so all 13 existing import sites
(``from taskvm.harness.state_adapter import StateAdapter / make_adapters``)
keep working unchanged — zero regression.

This is a FILE-LEVEL shim (not ``harness/__init__.py``) because every caller
imports from the ``.state_adapter`` submodule directly (recon area 8). New
code should import from ``taskvm.substrate.base``.
"""
from taskvm.substrate.base import *  # noqa: F401,F403
from taskvm.substrate.base import (  # noqa: F401  (explicit for IDE)
    StateAdapter, CalendarAdapter, TaskBoardAdapter, DriveAdapter,
    MailAdapter, OutlookCalAdapter, WechatAdapter, AlipayAdapter,
    make_adapter, make_adapters, _ADAPTER_CLASSES, DEFAULT_PORTS,
    _CORE_APPS, _HELDOUT_APPS, _MOBILEGYM_APPS,
)
