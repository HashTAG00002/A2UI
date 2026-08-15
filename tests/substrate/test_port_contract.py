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
