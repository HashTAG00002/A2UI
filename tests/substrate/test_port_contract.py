"""tests/substrate/test_port_contract.py — the frozen L1 port surface
(B-3, Oracle audit 2026-08-15; contract docs/contracts/substrate.md §2).

Locks:
  * SubstrateSession = observe/act/capture ONLY (the promised gate name
    ``test_gui_action_vocabulary_is_gestures`` referenced by port.py's
    own comments finally exists here);
  * GuiAction vocabulary is REAL gestures — set/mutate/restore are
    contract violations at construction time;
  * the two registries are physically separate capability sets;
  * unknown substrates fail HONESTLY (SubstrateUnavailable), never a
    fallback.
"""
from __future__ import annotations

import pytest


def test_substrate_session_protocol_surface():
    """Runtime capabilities ONLY: list_surfaces / observe / act / capture /
    close. There is deliberately no reset/seed/oracle on this protocol."""
    from taskvm.substrate import SubstrateSession
    expected = {"list_surfaces", "observe", "act", "capture", "close"}
    methods = {name for name in dir(SubstrateSession)
               if not name.startswith("_")}
    assert methods == expected, (
        f"SubstrateSession protocol drifted: +{methods - expected} "
        f"-{expected - methods}. Amend the frozen contract first.")


def test_gui_action_vocabulary_is_gestures():
    """The complete action vocabulary is real-world input events. Adding a
    non-gesture verb (set / mutate / restore / assign / delete / update) is
    a contract violation — port.py's comment promises THIS test by name."""
    from taskvm.substrate import GUI_ACTION_KINDS
    kinds = set(GUI_ACTION_KINDS)
    assert kinds == {"click", "tap", "type", "key", "scroll", "wait", "open"}
    banned = {"set", "mutate", "restore", "assign", "delete", "update",
              "patch", "write"}
    assert not kinds & banned, (
        f"non-gesture verbs in the port action vocabulary: {kinds & banned}")


def test_gui_action_rejects_non_gesture_kind():
    from taskvm.substrate import GuiAction
    with pytest.raises(ValueError, match="real-world gestures only"):
        GuiAction(kind="mutate", text="status=done")
    with pytest.raises(ValueError):
        GuiAction(kind="set_state", text="{}")
    # legal construction stays legal
    a = GuiAction(kind="click", coordinate=(500.0, 120.0))
    assert a.coordinate == (500.0, 120.0)


def test_two_registries_are_separate_capability_sets():
    """Runtime sessions and evaluation environments come from DIFFERENT
    registries — possession of one must not grant the other."""
    from taskvm.substrate import (evaluation_registry, substrate_registry)
    assert substrate_registry is not evaluation_registry
    assert set(substrate_registry._entrypoints) == \
        {"builtin_web", "mobilegym", "osworld"}
    assert set(evaluation_registry._entrypoints) == \
        {"builtin_web", "mobilegym", "osworld"}


def test_unknown_substrate_fails_honestly():
    """No fallback, no default — an unknown name raises the honest
    SubstrateUnavailable."""
    from taskvm.substrate import (SubstrateUnavailable, evaluation_registry,
                                  substrate_registry)
    with pytest.raises(SubstrateUnavailable):
        substrate_registry.create_session("does-not-exist", {})
    with pytest.raises(SubstrateUnavailable):
        evaluation_registry.create("does-not-exist", {})


def test_surface_handle_is_ephemeral_token_shape():
    """SurfaceHandle: frozen value object; ``handle_id`` is an opaque
    TaskVM-owned token (h1/h2/…), never an app DB primary key."""
    from taskvm.substrate import SurfaceHandle
    h = SurfaceHandle(handle_id="h1", surface_id="web:calendar",
                      anchor_role="button", anchor_text="Save")
    with pytest.raises(Exception):
        h.handle_id = "event-42"          # frozen — handles never mutate
    assert h.bbox_norm is None            # position optional
    assert h.fingerprint == ""            # filled by the producing session


def test_open_gesture_translates_gui_visible_name_to_app_id(monkeypatch):
    """GUI-only contract (GATE-G0 2026-08-20 r3 postmortem): ``GuiAction.
    target`` is "surface_id or visible app name" (port contract), and a
    GUI-only CUA can ONLY produce the visible spelling — the manifest
    displayName the home screen renders ("X", "支付宝"). The mobilegym
    session must translate that visible spelling to the canonical app_id
    before the bridge sees it (r3: the model named "X" correctly on
    gesture #1 and then burned 11 of its 12 gestures re-finding the app,
    because the bridge only accepted the internal id "x")."""
    import taskvm.substrate.mobilegym.session as session_mod
    from taskvm.substrate.mobilegym.session import MobileGymSubstrateSession
    from taskvm.substrate.port import GuiAction

    sent: list[dict] = []

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "ok", "detail": "open"}

    def _fake_post(url, json=None, timeout=None):
        sent.append(json)
        return _FakeResp()

    monkeypatch.setattr(session_mod.requests, "post", _fake_post)
    session = MobileGymSubstrateSession(
        sid="s1", bridge_url="http://localhost:3019", surface_app="wechat")

    def _open(target):
        session.act(None, GuiAction(kind="open", target=target), epoch="e1")
        return sent[-1]

    # the rendered home screen shows "X" — the only legal GUI-only spelling
    assert _open("X")["target"] == "x"
    # Chinese display names translate too (the only spelling on screen)
    assert _open("支付宝")["target"] == "alipay"
    assert _open("微信读书")["target"] == "wechat_reading"
    # the canonical internal id passes through untouched (both directions)
    assert _open("x")["target"] == "x"
    # whitespace is the model's, not the catalog's — strip before resolving
    assert _open(" X ")["target"] == "x"
    # a name on NO screen passes through unchanged so the bridge answers
    # its honest "unknown app" receipt — never guessed, never dropped
    assert _open("phone")["target"] == "phone"

    # non-open gestures never touch the target translation (open is the
    # only kind whose target names an app)
    session.act(None, GuiAction(kind="type", text="核心CPI下降"), epoch="e1")
    assert sent[-1] == {"kind": "type", "text": "核心CPI下降"}
