"""store — SurfaceStore generation/data-revision independence, bootstrap
replay and SSE tail ordering; zero kernel coupling."""
from __future__ import annotations

import pytest

from taskvm.genui import protocol
from taskvm.genui.store import SurfaceStore, SurfaceStoreRegistry


@pytest.fixture
def store() -> SurfaceStore:
    return SurfaceStore("session-A")


SIMPLE_COMPONENTS = [
    {"id": "root", "component": "Column", "children": ["t"]},
    {"id": "t", "component": "Text", "text": "任务变量"},
]


def test_surface_id_derived_from_session(store):
    assert store.surface_id == "taskvm-task-session-a"
    assert store.session_id == "session-A"


def test_ensure_surface_is_idempotent(store):
    first = store.ensure_surface()
    second = store.ensure_surface()
    assert first == second
    assert store.seq == 1  # appended exactly once


def test_writes_before_surface_creation_rejected():
    s = SurfaceStore("session-B")
    with pytest.raises(Exception):
        s.set_components(SIMPLE_COMPONENTS)
    with pytest.raises(Exception):
        s.set_data_model({"task": {}})


def test_generation_bumps_only_on_components(store):
    store.ensure_surface()
    assert store.generation == 0
    store.set_components(SIMPLE_COMPONENTS)
    assert store.generation == 1
    store.set_data_model({"task": {"goal": "g"}})
    store.set_data_model({"task": {"goal": "g2"}})
    assert store.generation == 1          # data updates are NOT structural
    store.set_components(SIMPLE_COMPONENTS)
    assert store.generation == 2


def test_data_revision_bumps_only_on_data(store):
    store.ensure_surface()
    store.set_components(SIMPLE_COMPONENTS)
    assert store.data_revision == 0
    store.set_data_model({"task": {"goal": "g"}})
    assert store.data_revision == 1
    store.set_components(SIMPLE_COMPONENTS)
    assert store.data_revision == 1       # structural change is not a data rev


def test_bootstrap_messages_order_and_content(store):
    store.ensure_surface()
    store.set_components(SIMPLE_COMPONENTS)
    store.set_data_model({"task": {"goal": "g"}})
    boot = store.bootstrap_messages()
    assert [list(m)[1] for m in boot] == \
        ["createSurface", "updateComponents", "updateDataModel"]
    assert boot[0]["createSurface"]["surfaceId"] == store.surface_id
    assert boot[0]["createSurface"]["catalogId"] == protocol.CATALOG_ID
    assert boot[1]["updateComponents"]["components"] == SIMPLE_COMPONENTS
    assert boot[2]["updateDataModel"]["value"] == {"task": {"goal": "g"}}


def test_bootstrap_before_components_is_create_only(store):
    store.ensure_surface()
    boot = store.bootstrap_messages()
    assert len(boot) == 1 and "createSurface" in boot[0]


def test_events_after_returns_ordered_tail(store):
    store.ensure_surface()
    store.set_components(SIMPLE_COMPONENTS)
    mid = store.seq
    store.set_data_model({"task": {"goal": "g"}})
    store.set_data_model({"task": {"goal": "g2"}})
    tail = store.events_after(mid)
    assert len(tail) == 2
    assert tail[0]["updateDataModel"]["value"]["task"]["goal"] == "g"
    assert tail[1]["updateDataModel"]["value"]["task"]["goal"] == "g2"
    assert store.events_after(store.seq) == []


def test_latest_accessors(store):
    store.ensure_surface()
    assert store.latest_components() is None
    assert store.latest_data_model() is None
    store.set_components(SIMPLE_COMPONENTS)
    store.set_data_model({"task": {"goal": "g"}})
    assert store.latest_components() == SIMPLE_COMPONENTS
    assert store.latest_data_model() == {"task": {"goal": "g"}}


def test_message_stream_is_sdk_valid(store):
    """The store's whole stream must stay protocol-conformant (what the
    renderer consumes is exactly what the SDK validator blesses)."""
    from taskvm.genui import schema
    store.ensure_surface()
    store.set_components(SIMPLE_COMPONENTS)
    store.set_data_model({"task": {"goal": "g"}})
    assert schema.validate_protocol_messages(store.events_after(0)) == []


def test_registry_get_or_create_idempotent():
    reg = SurfaceStoreRegistry()
    a = reg.get_or_create("s1")
    b = reg.get_or_create("s1")
    assert a is b
    assert reg.get("s1") is a
    assert reg.get("missing") is None
    assert reg.get_or_create("s2").surface_id == "taskvm-task-s2"


def test_stores_do_not_reference_kernel_types(store):
    """Design invariant: the store tree holds only plain protocol data
    (no kernel/domain objects — the kernel semantic state stays clean)."""
    import json
    store.ensure_surface()
    store.set_components(SIMPLE_COMPONENTS)
    store.set_data_model({"task": {"goal": "g"}})
    json.dumps(store.bootstrap_messages())  # fully JSON-serialisable
    assert not hasattr(store, "kernel")
