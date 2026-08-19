"""A5 transport contract locks — the data plane without a browser.

Everything runs on a hand-built kernel (same discipline as
tests/e2e_ui/test_playwright_e2e.py): zero model calls, zero substrate,
zero network. The locks:

  - ``attach_session`` mints an ordered 3-message stream (createSurface →
    updateComponents → updateDataModel) that passes the two-layer gate;
    the bootstrap route replays exactly that order;
  - ``refresh_data_model`` is the ZERO-Genui-call value path:
    ``data_revision`` bumps while ``generation`` stays frozen (the
    GenUI-call marker);
  - ``apply_action`` is the ONLY write path and lands exactly one
    governance local_patch (the kernel's desired actually moves, then
    the poller-style refresh mirrors it into the data model);
  - honest rejections carry the right statuses: governance-owned 403,
    unknown action 400, readonly 403, missing value 400, bad type 400,
    unknown semantic key 400;
  - the SSE first frame replays every a2ui message + progress event in
    order (the reconnect contract);
  - the poller retires itself when its session is replaced;
  - the /a2ui host page is honest about the build state (200 with the
    built island, 404 with the build instruction otherwise).
"""
from __future__ import annotations

import json
import time
import types

import pytest

from taskvm.domain import (
    ActionContract, NodeKind, TaskIntent, TaskVariable,
    WorkflowGraph, WorkflowNode,
)
from taskvm.kernel import TaskVMKernel
from taskvm.projection.store import ProjectionSessionStore
from taskvm.projection.view_models import snapshot_view
from taskvm.workspace_ui import serve
from taskvm.workspace_ui.a2ui_transport import (
    A2uiTransport, A2uiTransportError, register_a2ui_routes,
)

try:
    from flask import Flask
    _FLASK_OK = True
except ImportError:  # pragma: no cover
    _FLASK_OK = False


def _make_kernel(sid: str = "s1") -> TaskVMKernel:
    intent = TaskIntent(goal="发布产品")
    kernel = TaskVMKernel(sid, intent)
    kernel.init_task_state([
        TaskVariable(semantic_key="release_note", label="发布备注",
                     observed="v1", desired="v1", value_type="string"),
        TaskVariable(semantic_key="budget", label="预算",
                     observed=2000, desired=2000, value_type="number",
                     mutability="readonly"),
    ])
    kernel.set_plan(WorkflowGraph(nodes=(
        WorkflowNode(node_id="seq1", kind=NodeKind.SEQUENCE, label="发布流程"),
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="写发布备注",
                     parent_id="seq1",
                     contract=ActionContract(
                         contract_id="c1",
                         semantic_goal="set release_note",
                         desired_state={"release_note": "v1"},
                         completion_condition="release_note shows v1")),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a1",)),
    )))
    return kernel


@pytest.fixture()
def stack():
    store = ProjectionSessionStore()
    kernel = _make_kernel("s1")
    store.register("s1", kernel)
    transport = A2uiTransport(session_lookup=store.get)
    state = types.SimpleNamespace(sid="s1")
    app = serve(store)
    register_a2ui_routes(app, transport, store, state)
    return types.SimpleNamespace(
        app=app, store=store, transport=transport, kernel=kernel,
        client=app.test_client())


def _desired_of(sess, key):
    for v in snapshot_view(sess)["variables"]:
        if v["key"] == key:
            return v["desired"]
    raise AssertionError(f"variable {key!r} not in snapshot")


# ── attach + bootstrap: the ordered stream ──────────────────────────────────


def test_attach_mints_ordered_stream_and_bootstrap_replays_it(stack):
    assert stack.client.get("/api/app/a2ui/bootstrap").status_code == 404
    sess = stack.store.get("s1")
    info = stack.transport.attach_session("s1", sess)
    assert info["generation"] == 1
    assert info["dataRevision"] == 1
    assert info["componentCount"] > 0
    assert info["surfaceId"] == "taskvm-task-s1"

    body = stack.client.get("/api/app/a2ui/bootstrap").get_json()
    assert body["ok"] is True
    msgs = body["messages"]
    assert len(msgs) == 3
    assert "createSurface" in msgs[0]
    assert "updateComponents" in msgs[1]
    assert "updateDataModel" in msgs[2]
    assert body["seq"] == 3
    # the component tree is the real A4 baseline (generic variable list)
    comps = msgs[1]["updateComponents"]["components"]
    assert any(c["id"] == "root" for c in comps)
    # the data model is the deterministic projection (facts, not literals)
    assert msgs[2]["updateDataModel"]["value"]["variables"][
        "release_note"]["desired"] == "v1"


def test_attach_failure_is_honest_no_half_created_surface(stack):
    sess = stack.store.get("s1")

    def _bad_factory(context):
        return [{"id": "root", "component": "NotARealComponent"}]

    stack.transport._factory = _bad_factory
    with pytest.raises(A2uiTransportError):
        stack.transport.attach_session("s1", sess)
    assert stack.transport.store("s1") is None          # nothing minted
    assert stack.client.get("/api/app/a2ui/bootstrap").status_code == 404
    # the reason rode an a2ui_failed progress event
    events = stack.transport.progress_after("s1", 0)
    assert any(ev["stage"] == "a2ui_failed" for _, ev in events)


# ── the zero-Genui-call value path ──────────────────────────────────────────


def test_value_update_bumps_data_revision_only(stack):
    sess = stack.store.get("s1")
    stack.transport.attach_session("s1", sess)
    s = stack.transport.store("s1")

    changed = stack.transport.refresh_data_model("s1", sess)
    assert changed is False                    # nothing moved — no frame

    sess.governance_port().local_patch({"release_note": "v2"},
                                       rationale="test")
    changed = stack.transport.refresh_data_model("s1", sess)
    assert changed is True
    assert s.data_revision == 2
    assert s.generation == 1                   # the GenUI-call marker: FROZEN
    assert s.latest_data_model()["variables"]["release_note"][
        "desired"] == "v2"


# ── the action write path ───────────────────────────────────────────────────


def test_action_lands_exactly_one_local_patch(stack):
    sess = stack.store.get("s1")
    stack.transport.attach_session("s1", sess)
    resp = stack.client.post("/api/app/a2ui/action", json={
        "name": "taskvm.local_patch",
        "context": {"semanticKey": "release_note", "value": "v3"},
    })
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    # the kernel's desired ACTUALLY moved (the only real write)
    assert _desired_of(sess, "release_note") == "v3"
    # the poller-style refresh mirrors it into the data model
    stack.transport.refresh_data_model("s1", sess)
    s = stack.transport.store("s1")
    assert s.latest_data_model()["variables"]["release_note"][
        "desired"] == "v3"
    assert s.generation == 1                   # still zero GenUI calls


@pytest.mark.parametrize("payload,status,fragment", [
    # governance-owned: the dynamic surface may never emit these
    ({"name": "pause", "context": {}}, 403, "governance-owned"),
    ({"name": "rollback", "context": {}}, 403, "governance-owned"),
    # unknown action
    ({"name": "taskvm.magic", "context": {}}, 400, "unknown action"),
    # missing semanticKey
    ({"name": "taskvm.local_patch", "context": {}}, 400, "semanticKey"),
    # unknown semantic key
    ({"name": "taskvm.local_patch",
      "context": {"semanticKey": "nope", "value": "x"}}, 400, "unknown"),
    # readonly variable
    ({"name": "taskvm.local_patch",
      "context": {"semanticKey": "budget", "value": 1}}, 403, "readonly"),
    # missing value
    ({"name": "taskvm.local_patch",
      "context": {"semanticKey": "release_note"}}, 400, "context.value"),
    # bad value type (string variable rejects a number)
    ({"name": "taskvm.local_patch",
      "context": {"semanticKey": "release_note", "value": 123}}, 400,
     "rejects value"),
])
def test_action_honest_rejections(stack, payload, status, fragment):
    sess = stack.store.get("s1")
    stack.transport.attach_session("s1", sess)
    resp = stack.client.post("/api/app/a2ui/action", json=payload)
    assert resp.status_code == status
    body = resp.get_json()
    assert body["ok"] is False
    assert fragment in body["error"]


def test_action_requires_live_session_and_surface(stack):
    # session registered but no A2UI surface minted yet → honest 404
    resp = stack.client.post("/api/app/a2ui/action", json={
        "name": "taskvm.local_patch",
        "context": {"semanticKey": "release_note", "value": "x"}})
    assert resp.status_code == 404
    assert "no A2UI surface" in resp.get_json()["error"]

    # no session at all (empty store) → honest 404, distinct message
    import flask
    empty_store = ProjectionSessionStore()
    empty_transport = A2uiTransport()
    app = flask.Flask(__name__)
    register_a2ui_routes(app, empty_transport, empty_store,
                         types.SimpleNamespace(sid="s1"))
    resp = app.test_client().post("/api/app/a2ui/action", json={
        "name": "taskvm.local_patch",
        "context": {"semanticKey": "release_note", "value": "x"}})
    assert resp.status_code == 404
    assert "no active session" in resp.get_json()["error"]


# ── progress ring + the SSE first frame ─────────────────────────────────────


def test_sse_first_frame_replays_a2ui_and_progress_in_order(stack):
    stack.transport.push_stage("s1", "goal", {"goal": "发布产品"})
    sess = stack.store.get("s1")
    stack.transport.attach_session("s1", sess)   # pushes "ready"

    with stack.app.test_request_context("/api/app/a2ui/sse?after=0"):
        resp = stack.app.view_functions["a2ui_sse"]()
    assert resp.mimetype == "text/event-stream"
    assert resp.headers["X-Accel-Buffering"] == "no"

    frame = next(resp.response)      # first batch: full replay, no wait
    chunks = [c for c in frame.split("\n\n") if c]
    data_chunks = [c for c in chunks if c.startswith("data: ")]
    progress_chunks = [c for c in chunks
                       if c.startswith("event: progress")]
    assert len(data_chunks) == 3     # createSurface → components → data
    seqs = [json.loads(c[6:])["seq"] for c in data_chunks]
    assert seqs == [1, 2, 3]
    msgs = [json.loads(c[6:])["message"] for c in data_chunks]
    assert "createSurface" in msgs[0]
    assert "updateComponents" in msgs[1]
    assert "updateDataModel" in msgs[2]
    stages = [json.loads(c[len("event: progress\ndata: "):])["stage"]
              for c in progress_chunks]
    assert stages == ["goal", "ready"]


# ── the poller lifecycle ────────────────────────────────────────────────────


def test_poller_retires_when_session_replaced(stack, monkeypatch):
    import taskvm.workspace_ui.a2ui_transport as mod
    monkeypatch.setattr(mod, "POLL_INTERVAL_S", 0.05)
    sess = stack.store.get("s1")
    stack.transport.attach_session("s1", sess)
    poller = stack.transport._pollers["s1"]
    assert poller.is_alive()

    # a new goal replaces the session under the same sid
    new_kernel = _make_kernel("s1")
    stack.store.drop("s1")
    stack.store.register("s1", new_kernel)
    deadline = time.time() + 3.0
    while poller.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert not poller.is_alive()

    # and drop_session stops an attached poller immediately
    stack.transport.attach_session("s1", stack.store.get("s1"))
    poller2 = stack.transport._pollers["s1"]
    stack.transport.drop_session("s1")
    deadline = time.time() + 3.0
    while poller2.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert not poller2.is_alive()


# ── the /a2ui host page honesty ─────────────────────────────────────────────


@pytest.mark.skipif(not _FLASK_OK, reason="flask not installed")
def test_a2ui_page_is_honest_about_build_state(tmp_path):
    from flask import Flask
    store = ProjectionSessionStore()
    transport = A2uiTransport()
    state = types.SimpleNamespace(sid="s1")
    app = Flask(__name__, static_folder=str(tmp_path),
                static_url_path="/static")
    register_a2ui_routes(app, transport, store, state)
    client = app.test_client()

    resp = client.get("/a2ui")
    assert resp.status_code == 404
    assert "npm run build" in resp.get_json()["error"]

    island = tmp_path / "a2ui"
    island.mkdir()
    (island / "index.html").write_text(
        "<html><body>island</body></html>", encoding="utf-8")
    resp = client.get("/a2ui")
    assert resp.status_code == 200
    assert b"island" in resp.data
