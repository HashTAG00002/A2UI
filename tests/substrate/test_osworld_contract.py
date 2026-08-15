"""tests/substrate/test_osworld_contract.py — the OSWorld minimum adapter's
contract test (B-3; the file ``osworld/session.py``'s own docstring has
promised since Agent B's wave — it exists now).

Uses a fake runtime transport (the documented substitution point: three
methods — screenshot / perform / alive). Locks the handoff's minimum
shape: desktop surface, unified Observation/ActionReceipt, click/type/
key/scroll through the real pipeline, and the HONEST SubstrateUnavailable
when the VM endpoint is missing (never fabricated observations).
"""
from __future__ import annotations

import pytest

from taskvm.substrate import (ActionReceipt, GuiAction, Observation,
                              SubstrateUnavailable)
from taskvm.substrate.osworld.session import (HttpOSWorldRuntime,
                                              OSWorldSubstrateSession)


class FakeRuntime:
    alive = True

    def __init__(self):
        self.performed: list[dict] = []

    def screenshot(self) -> bytes:
        return b"\x89PNG-fake-desktop"

    def perform(self, action: dict) -> dict:
        self.performed.append(action)
        return {"status": "ok"}


class DeadRuntime:
    alive = False

    def screenshot(self) -> bytes:
        raise ConnectionError("no VM")

    def perform(self, action: dict) -> dict:
        raise ConnectionError("no VM")


def test_lists_one_desktop_surface():
    s = OSWorldSubstrateSession(runtime=FakeRuntime())
    surfaces = s.list_surfaces()
    assert len(surfaces) == 1
    assert surfaces[0].surface_kind == "screen"
    assert surfaces[0].display_name == "Desktop"


def test_observe_returns_unified_observation():
    s = OSWorldSubstrateSession(runtime=FakeRuntime())
    obs = s.observe()
    assert isinstance(obs, Observation)
    assert obs.screenshot_ref.startswith("data:image/png;base64,")
    assert obs.fingerprint                    # pixels → structural digest
    assert obs.previous_fingerprint_matched is None   # first observation
    obs2 = s.observe(previous_fingerprint=obs.fingerprint)
    assert obs2.previous_fingerprint_matched is True  # same pixels


def test_act_translates_gestures_to_the_real_pipeline():
    rt = FakeRuntime()
    s = OSWorldSubstrateSession(runtime=rt)
    r = s.act(None, GuiAction(kind="click", coordinate=(123.0, 456.0)),
              epoch="e1")
    assert isinstance(r, ActionReceipt) and r.status == "ok"
    assert rt.performed == [{"type": "click", "coordinate": [123.0, 456.0]}]

    r = s.act(None, GuiAction(kind="type", text="hello"), epoch="e1")
    assert rt.performed[-1] == {"type": "type", "text": "hello"}

    r = s.act(None, GuiAction(kind="key", key="Enter"), epoch="e1")
    assert rt.performed[-1] == {"type": "key", "key": "Enter"}

    r = s.act(None, GuiAction(kind="scroll", direction="down"), epoch="e1")
    assert rt.performed[-1]["type"] == "scroll"
    assert rt.performed[-1]["direction"] == "down"


def test_dead_runtime_is_honestly_unavailable():
    s = OSWorldSubstrateSession(runtime=DeadRuntime())
    with pytest.raises(SubstrateUnavailable):
        s.list_surfaces()
    with pytest.raises(SubstrateUnavailable):
        s.observe()
    r = s.act(None, GuiAction(kind="click", coordinate=(1.0, 1.0)),
              epoch="e1")
    assert r.status == "unavailable"    # honest receipt, no fabricated ok


def test_http_runtime_with_bogus_endpoint_reports_dead():
    """127.0.0.1:1 refuses immediately — the honest alive=False path over
    the real HTTP transport (no OSWorld VM is required for this test)."""
    rt = HttpOSWorldRuntime(endpoint="http://127.0.0.1:1")
    assert rt.alive is False
