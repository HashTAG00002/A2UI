"""Route matrix tests (contract §6/§12): every route × method; no 405 on
the served page's own actions; structured 4xx on invalid input; 404 on
unknown sid; SSE content-type; artifact 404 path.

Uses Flask test client — no real browser, no model, no substrate.
"""
from __future__ import annotations

import json

import pytest

from taskvm.domain import (
    ActionContract,
    NodeKind,
    Reversibility,
    TaskIntent,
    TaskVariable,
    WorkflowGraph,
    WorkflowNode,
)
from taskvm.kernel import TaskVMKernel

from taskvm.projection.app import create_app
from taskvm.projection.store import (
    ArtifactStore,
    ProjectionSessionStore,
    SurfaceDecl,
)


# ── fixtures ───────────────────────────────────────────────────────────────

def _contract(cid, key, value):
    return ActionContract(
        contract_id=cid, semantic_goal=f"set {key} to {value}",
        desired_state={key: value},
        completion_condition=f"{key} shows {value}")


def _make_kernel(sid="s1"):
    intent = TaskIntent(goal="发布产品", scope=["发布"])
    kernel = TaskVMKernel(sid, intent)
    kernel.init_task_state([
        TaskVariable(semantic_key="release_date", label="发布日期",
                     observed="2026-08-14", desired="2026-08-18",
                     value_type="date"),
    ])
    graph = WorkflowGraph(nodes=(
        WorkflowNode(node_id="seq1", kind=NodeKind.SEQUENCE, label="发布流程"),
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="设置发布日期",
                     parent_id="seq1", contract=_contract("c1", "release_date", "2026-08-18")),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a1",)),
    ))
    kernel.set_plan(graph)
    return kernel


@pytest.fixture
def app_and_store():
    store = ProjectionSessionStore()
    art = ArtifactStore()
    art.put("ref1", b"fake-png-bytes")
    store.register("s1", _make_kernel("s1"),
                   surfaces=[SurfaceDecl(surface_id="surf1", display_name="X平台")],
                   artifacts=art)
    app = create_app(store)
    return app, store


@pytest.fixture
def client(app_and_store):
    app, _ = app_and_store
    app.config["TESTING"] = True
    return app.test_client()


# ── read routes ───────────────────────────────────────────────────────────

class TestReadRoutes:
    def test_list_sessions(self, client):
        r = client.get("/api/sessions")
        assert r.status_code == 200
        data = r.get_json()
        assert "s1" in data["sessions"]

    def test_get_snapshot(self, client):
        r = client.get("/api/sessions/s1/snapshot")
        assert r.status_code == 200
        data = r.get_json()
        assert data["sid"] == "s1"
        assert "governance" in data
        assert "workflow" in data

    def test_get_governance(self, client):
        r = client.get("/api/sessions/s1/governance")
        assert r.status_code == 200
        data = r.get_json()
        assert data["goal"] == "发布产品"

    def test_get_variables(self, client):
        r = client.get("/api/sessions/s1/variables")
        assert r.status_code == 200
        data = r.get_json()
        assert len(data) == 1
        assert data[0]["key"] == "release_date"

    def test_get_workflow(self, client):
        r = client.get("/api/sessions/s1/workflow")
        assert r.status_code == 200
        data = r.get_json()
        assert data["has_plan"] is True

    def test_get_checkpoints(self, client):
        r = client.get("/api/sessions/s1/checkpoints")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)

    def test_get_surfaces(self, client):
        r = client.get("/api/sessions/s1/surfaces")
        assert r.status_code == 200
        data = r.get_json()
        assert len(data) == 1
        assert data[0]["display_name"] == "X平台"

    def test_get_conflicts(self, client):
        r = client.get("/api/sessions/s1/conflicts")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)

    def test_get_events(self, client):
        r = client.get("/api/sessions/s1/events")
        assert r.status_code == 200
        data = r.get_json()
        assert "events" in data
        assert "total" in data

    def test_get_events_pagination(self, client):
        r = client.get("/api/sessions/s1/events?offset=0&limit=5")
        assert r.status_code == 200
        data = r.get_json()
        assert data["offset"] == 0
        assert data["limit"] == 5

    def test_get_artifact(self, client):
        r = client.get("/api/sessions/s1/artifacts/ref1")
        assert r.status_code == 200
        assert r.data == b"fake-png-bytes"

    def test_get_artifact_404(self, client):
        r = client.get("/api/sessions/s1/artifacts/nonexistent")
        assert r.status_code == 404


# ── 404 on unknown sid ────────────────────────────────────────────────────

class TestUnknownSid:
    def test_snapshot_404(self, client):
        r = client.get("/api/sessions/nonexistent/snapshot")
        assert r.status_code == 404

    def test_governance_404(self, client):
        r = client.get("/api/sessions/nonexistent/governance")
        assert r.status_code == 404

    def test_variables_404(self, client):
        r = client.get("/api/sessions/nonexistent/variables")
        assert r.status_code == 404

    def test_workflow_404(self, client):
        r = client.get("/api/sessions/nonexistent/workflow")
        assert r.status_code == 404

    def test_artifact_404(self, client):
        r = client.get("/api/sessions/nonexistent/artifacts/ref1")
        assert r.status_code == 404

    def test_governance_pause_404(self, client):
        r = client.post("/api/sessions/nonexistent/governance/pause",
                        json={})
        assert r.status_code == 404


# ── governance command routes ─────────────────────────────────────────────

class TestGovernanceCommands:
    def test_pause(self, client):
        r = client.post("/api/sessions/s1/governance/pause", json={})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["action"] == "paused"

    def test_pause_with_rationale(self, client):
        r = client.post("/api/sessions/s1/governance/pause",
                        json={"rationale": "need to review"})
        assert r.status_code == 200
        assert r.get_json()["reason"] == "need to review"

    def test_resume(self, client):
        r = client.post("/api/sessions/s1/governance/resume", json={})
        assert r.status_code == 200
        assert r.get_json()["action"] == "resumed"

    def test_stop(self, client):
        r = client.post("/api/sessions/s1/governance/stop", json={})
        assert r.status_code == 200
        assert r.get_json()["action"] == "stopped"

    def test_local_patch(self, client):
        r = client.post("/api/sessions/s1/governance/local_patch",
                        json={"updates": {"release_date": "2026-08-20"},
                              "rationale": "edit date"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["action"] == "local_patch"

    def test_goal_patch(self, client):
        r = client.post("/api/sessions/s1/governance/goal_patch",
                        json={"goal": "新目标",
                              "constraints": ["不可逆操作需确认"],
                              "rationale": "scope change"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["action"] == "goal_patch"

    def test_checkpoint(self, client):
        r = client.post("/api/sessions/s1/governance/checkpoint",
                        json={"label": "发布前检查点"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert "checkpoint_id" in data
        assert data["label"] == "发布前检查点"

    def test_rollback(self, client):
        # first commit a checkpoint
        r1 = client.post("/api/sessions/s1/governance/checkpoint",
                         json={"label": "cp1"})
        cp_id = r1.get_json()["checkpoint_id"]
        # then rollback to it
        r2 = client.post("/api/sessions/s1/governance/rollback",
                         json={"target_checkpoint_id": cp_id})
        assert r2.status_code == 200
        data = r2.get_json()
        assert data["ok"] is True
        assert data["action"] == "rollback"

    def test_resolve_conflict(self, client):
        # record a conflict first
        store = ProjectionSessionStore()
        kernel = _make_kernel("s_cf")
        store.register("s_cf", kernel)
        kernel.record_conflict("test conflict",
                              semantic_keys=["release_date"],
                              correlation_id="cf1")
        app = create_app(store)
        app.config["TESTING"] = True
        c = app.test_client()
        r = c.post("/api/sessions/s_cf/governance/resolve_conflict",
                   json={"conflict_id": "cf1", "resolution": "keep_world"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True


# ── no 405 on correct verbs ────────────────────────────────────────────────

class TestNo405OnCorrectVerbs:
    """Every route returns 2xx/4xx but never 405 when the correct verb is
    used (contract §6: 0 unexpected 405)."""

    def test_get_snapshot_not_405(self, client):
        r = client.get("/api/sessions/s1/snapshot")
        assert r.status_code != 405

    def test_post_pause_not_405(self, client):
        r = client.post("/api/sessions/s1/governance/pause", json={})
        assert r.status_code != 405

    def test_post_checkpoint_not_405(self, client):
        r = client.post("/api/sessions/s1/governance/checkpoint",
                        json={"label": "test"})
        assert r.status_code != 405

    def test_post_local_patch_not_405(self, client):
        r = client.post("/api/sessions/s1/governance/local_patch",
                        json={"updates": {"release_date": "x"}})
        assert r.status_code != 405

    def test_post_goal_patch_not_405(self, client):
        r = client.post("/api/sessions/s1/governance/goal_patch",
                        json={"goal": "test"})
        assert r.status_code != 405

    def test_post_rollback_not_405(self, client):
        r = client.post("/api/sessions/s1/governance/rollback",
                        json={"target_checkpoint_id": "x"})
        # may be 400 (bad checkpoint) but not 405
        assert r.status_code != 405


# ── SSE endpoint ──────────────────────────────────────────────────────────

class TestSSEEndpoint:
    def test_sse_content_type(self, client):
        """SSE stream returns text/event-stream content type."""
        r = client.get("/api/sessions/s1/sse")
        assert r.status_code == 200
        assert "text/event-stream" in r.content_type

    def test_sse_404_unknown_sid(self, client):
        r = client.get("/api/sessions/nonexistent/sse")
        assert r.status_code == 404

    def test_sse_yields_data(self, client):
        """SSE stream yields at least one data frame (initial snapshot)."""
        r = client.get("/api/sessions/s1/sse", buffered=False)
        assert r.status_code == 200
        # read some bytes
        data = next(r.response)
        assert b"data:" in data or b"data: " in data


# ── index route ───────────────────────────────────────────────────────────

class TestIndexRoute:
    def test_index_without_static(self):
        store = ProjectionSessionStore()
        store.register("s1", _make_kernel("s1"))
        app = create_app(store)
        app.config["TESTING"] = True
        c = app.test_client()
        r = c.get("/")
        assert r.status_code == 200
        data = r.get_json()
        assert data["service"] == "taskvm-projection"
        assert "s1" in data["sessions"]
