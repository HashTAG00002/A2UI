"""protocol — single-source-of-truth invariants for the A2UI identity."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskvm.genui import protocol
from taskvm.genui import schema


# ── the version literal lives in protocol.py ONLY ──────────────────────────

def test_protocol_version_is_v09():
    assert protocol.PROTOCOL_VERSION == "v0.9"


def test_no_scattered_version_literals():
    """No other genui source file may hard-code the version string
    (workplan §4: 禁止多处散落 "v0.9")."""
    genui_dir = Path(protocol.__file__).parent
    for py in genui_dir.glob("*.py"):
        if py.name == "protocol.py":
            continue
        content = py.read_text(encoding="utf-8")
        assert '"v0.9"' not in content and "'v0.9'" not in content, (
            f"{py.name} hard-codes the protocol version — use "
            "taskvm.genui.protocol.PROTOCOL_VERSION")


def test_catalog_id_matches_vendored_mirror():
    cat = json.loads((schema.SPEC_ROOT / "catalogs_basic" / "catalog.json")
                     .read_text(encoding="utf-8"))
    assert cat["$id"] == protocol.CATALOG_ID
    # and the SDK catalog agrees (invariant: $id == catalogId)
    assert schema.get_catalog().catalog_id == protocol.CATALOG_ID


# ── model-call role registration (A4 · workplan §20.2) ─────────────────────

def test_decoder_role_registered_in_shared_ledger():
    """Contract lock: genui's local role constant and the architect
    ledger's MODEL_ROLES must name the same string, so the shared
    ModelCallLedger buckets GenUI decoder calls under one key (the
    verifier layer's MODEL_ROLE_MODEL_VERIFIER precedent)."""
    from taskvm.architect.port import (
        MODEL_ROLE_GENUI_DECODER, MODEL_ROLES,
    )
    assert protocol.GENUI_DECODER_MODEL_ROLE == MODEL_ROLE_GENUI_DECODER
    assert MODEL_ROLE_GENUI_DECODER in MODEL_ROLES


def test_shared_ledger_accepts_decoder_role_record():
    from taskvm.architect.port import ModelCallLedger, ModelCallRecord
    ledger = ModelCallLedger()
    ledger.record(ModelCallRecord(
        role=protocol.GENUI_DECODER_MODEL_ROLE, purpose="surface_compose",
        model="gpt-5.6-sol", ok=True))
    assert ledger.counts_by_role() == {"genui_decoder": 1}


# ── surface id naming ──────────────────────────────────────────────────────

def test_surface_id_naming_rules():
    assert protocol.surface_id_for_session("demo-session-42") == \
        "taskvm-task-demo-session-42"
    assert protocol.surface_id_for_session("Room 5 / 私密") == \
        "taskvm-task-room-5"
    assert protocol.surface_id_for_session("ABC") == "taskvm-task-abc"
    assert protocol.surface_id_for_session("x--y") == "taskvm-task-x-y"


def test_surface_id_rejects_empty():
    with pytest.raises(ValueError):
        protocol.surface_id_for_session("   ")


def test_surface_ids_are_deterministic_and_unique():
    a = protocol.surface_id_for_session("s1")
    b = protocol.surface_id_for_session("s1")
    c = protocol.surface_id_for_session("s2")
    assert a == b
    assert a != c


# ── message envelope constructors ──────────────────────────────────────────

def test_message_constructors_produce_valid_protocol_stream():
    sid = protocol.surface_id_for_session("msg-shapes")
    stream = [
        protocol.create_surface_message(sid),
        protocol.update_components_message(sid, [
            {"id": "root", "component": "Column", "children": ["t"]},
            {"id": "t", "component": "Text", "text": "hello"},
        ]),
        protocol.update_data_model_message(sid, {"task": {"goal": "g"}}),
        protocol.delete_surface_message(sid),
    ]
    # the official SDK validator must accept the whole stream
    assert schema.validate_protocol_messages(stream) == []
    for msg in stream:
        assert msg["version"] == protocol.PROTOCOL_VERSION


def test_update_data_model_path_defaults_to_root():
    sid = "taskvm-task-x"
    msg = protocol.update_data_model_message(sid, {"a": 1})
    assert msg["updateDataModel"]["path"] == "/"


# ── action vocabulary ──────────────────────────────────────────────────────

def test_action_allowlist_is_minimal_and_disjoint_from_governance():
    assert protocol.ALLOWED_SURFACE_ACTIONS == {"taskvm.local_patch"}
    assert protocol.GOVERNANCE_ACTION_NAMES & protocol.ALLOWED_SURFACE_ACTIONS == set()
    assert "pause" in protocol.GOVERNANCE_ACTION_NAMES
    assert "rollback" in protocol.GOVERNANCE_ACTION_NAMES


def test_variable_path_and_task_path_validation():
    assert protocol.variable_path("release_date", "desired") == \
        "/variables/release_date/desired"
    with pytest.raises(ValueError):
        protocol.variable_path("k", "nope")
    with pytest.raises(ValueError):
        protocol.variable_path("", "desired")
    with pytest.raises(ValueError):
        protocol.task_path("epoch")
    assert protocol.task_path("goal") == "/task/goal"


def test_reserved_id_prefixes_cover_governance_namespace():
    assert "governance-" in protocol.RESERVED_ID_PREFIXES
    assert "gov-" in protocol.RESERVED_ID_PREFIXES
