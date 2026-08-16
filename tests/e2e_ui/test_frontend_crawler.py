"""Frontend Crawler — page-by-page render verification.

Crawls every user-facing page and every API endpoint, asserting:
  - HTML pages return 200 + text/html
  - API endpoints return 200 (or semantic 404 for unknown sid)
  - No 405 (Method Not Allowed) anywhere — every route accepts its
    declared method
  - No 500 (Internal Server Error) anywhere — the server is honest
  - No internal entity_id / data-* / operator vocabulary leaks (GG §0)

This is the journey point 15 suite: "no 405/500 anywhere".
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import requests

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
from taskvm.projection.store import (
    ArtifactStore,
    ProjectionSessionStore,
    SurfaceDecl,
)


def _contract(cid, key, value):
    return ActionContract(
        contract_id=cid,
        semantic_goal=f"set {key} to {value}",
        desired_state={key: value},
        completion_condition=f"{key} shows {value}",
    )


def _make_kernel(sid="crawl"):
    intent = TaskIntent(goal="发布产品", scope=("发布",))
    kernel = TaskVMKernel(sid, intent)
    kernel.init_task_state([
        TaskVariable(semantic_key="release_date", label="发布日期",
                     observed="2026-08-14", desired="2026-08-18",
                     value_type="date"),
    ])
    graph = WorkflowGraph(nodes=(
        WorkflowNode(node_id="seq1", kind=NodeKind.SEQUENCE, label="发布流程"),
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="设置发布日期",
                     parent_id="seq1",
                     contract=_contract("c1", "release_date", "2026-08-18")),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a1",)),
    ))
    kernel.set_plan(graph)
    return kernel


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    store = ProjectionSessionStore()
    art = ArtifactStore()
    art.put("crawl-ref", b"crawl-png-bytes")
    store.register("crawl", _make_kernel("crawl"),
                   surfaces=(SurfaceDecl(surface_id="surf1",
                                        display_name="X平台"),),
                   artifacts=art)
    from taskvm.workspace_ui import serve
    app = serve(store)
    port = _free_port()

    def _run():
        app.run(host="127.0.0.1", port=port, threaded=True,
                debug=False, use_reloader=False)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            r = requests.get(f"{base}/api/sessions", timeout=1)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        pytest.fail("server did not start")
    yield base


# ── HTML page crawl ─────────────────────────────────────────────────────

class TestHTMLPages:
    """Every user-facing HTML page returns 200 + text/html."""

    def test_index_page(self, server_url):
        r = requests.get(f"{server_url}/")
        assert r.status_code == 200
        assert "text/html" in r.headers["Content-Type"]
        assert "TaskVM" in r.text or "taskvm" in r.text.lower()

    def test_session_page(self, server_url):
        r = requests.get(f"{server_url}/sessions/crawl")
        assert r.status_code == 200
        assert "text/html" in r.headers["Content-Type"]

    def test_session_page_unknown_sid(self, server_url):
        """Unknown sid returns 404 (honest) — the page route checks
        the session exists before serving the SPA shell."""
        r = requests.get(f"{server_url}/sessions/nope")
        assert r.status_code == 404

    def test_static_css(self, server_url):
        r = requests.get(f"{server_url}/static/css/taskvm.css")
        assert r.status_code == 200
        assert "text/css" in r.headers["Content-Type"]

    def test_static_js(self, server_url):
        r = requests.get(f"{server_url}/static/js/taskvm.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["Content-Type"].lower()


# ── API route matrix crawl ──────────────────────────────────────────────

class TestAPIMatrix:
    """Every API endpoint returns a valid status (never 405/500)."""

    def test_get_sessions(self, server_url):
        r = requests.get(f"{server_url}/api/sessions")
        assert r.status_code == 200
        assert "crawl" in r.json()["sessions"]

    def test_get_snapshot(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/crawl/snapshot")
        assert r.status_code == 200
        data = r.json()
        assert data["sid"] == "crawl"

    def test_get_governance(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/crawl/governance")
        assert r.status_code == 200

    def test_get_variables(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/crawl/variables")
        assert r.status_code == 200

    def test_get_workflow(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/crawl/workflow")
        assert r.status_code == 200

    def test_get_checkpoints(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/crawl/checkpoints")
        assert r.status_code == 200

    def test_get_surfaces(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/crawl/surfaces")
        assert r.status_code == 200

    def test_get_conflicts(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/crawl/conflicts")
        assert r.status_code == 200

    def test_get_events(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/crawl/events")
        assert r.status_code == 200

    def test_get_artifact(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/crawl/artifacts/crawl-ref")
        assert r.status_code == 200
        assert r.content == b"crawl-png-bytes"

    def test_get_artifact_404(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/crawl/artifacts/nope")
        assert r.status_code == 404

    def test_unknown_sid_snapshot_404(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/nope/snapshot")
        assert r.status_code == 404

    def test_post_pause(self, server_url):
        r = requests.post(f"{server_url}/api/sessions/crawl/governance/pause",
                          json={})
        assert r.status_code == 200

    def test_post_resume(self, server_url):
        r = requests.post(f"{server_url}/api/sessions/crawl/governance/resume",
                          json={})
        assert r.status_code == 200

    def test_post_checkpoint(self, server_url):
        r = requests.post(f"{server_url}/api/sessions/crawl/governance/checkpoint",
                          json={"label": "crawl-ckpt"})
        assert r.status_code == 201

    def test_post_local_patch(self, server_url):
        r = requests.post(f"{server_url}/api/sessions/crawl/governance/local_patch",
                          json={"updates": {"release_date": "2026-09-01"},
                                "rationale": "crawl edit"})
        assert r.status_code == 200


# ── no 405 / no 500 sweep ───────────────────────────────────────────────

class TestNo405No500:
    """Sweep every known route — none may return 405 or 500."""

    def test_no_405_on_get_routes(self, server_url):
        get_routes = [
            "/api/sessions",
            "/api/sessions/crawl/snapshot",
            "/api/sessions/crawl/governance",
            "/api/sessions/crawl/variables",
            "/api/sessions/crawl/workflow",
            "/api/sessions/crawl/checkpoints",
            "/api/sessions/crawl/surfaces",
            "/api/sessions/crawl/conflicts",
            "/api/sessions/crawl/events",
            "/api/sessions/crawl/artifacts/crawl-ref",
            "/sessions/crawl",
            "/",
        ]
        for path in get_routes:
            r = requests.get(f"{server_url}{path}")
            assert r.status_code != 405, f"405 on GET {path}"
            assert r.status_code != 500, f"500 on GET {path}"

    def test_no_405_on_post_routes(self, server_url):
        post_routes = [
            "/api/sessions/crawl/governance/pause",
            "/api/sessions/crawl/governance/resume",
            "/api/sessions/crawl/governance/stop",
            "/api/sessions/crawl/governance/local_patch",
            "/api/sessions/crawl/governance/checkpoint",
        ]
        for path in post_routes:
            r = requests.post(f"{server_url}{path}", json={})
            assert r.status_code != 405, f"405 on POST {path}"
            assert r.status_code != 500, f"500 on POST {path}"


# ── no-leak crawl ───────────────────────────────────────────────────────

class TestNoLeakCrawl:
    """No internal entity_id / data-* / operator vocabulary in any
    user-facing response (GG §0)."""

    LEAK_PATTERNS = ("entity_id", "data-node-id", "data-action-id",
                     "get_state", "n001", "n002", "n003", "n004",
                     "c001", "c002", "c003")

    def test_no_leak_in_snapshot(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/crawl/snapshot")
        text = r.text
        for pat in self.LEAK_PATTERNS:
            assert pat not in text, f"leak: '{pat}' in snapshot"

    def test_no_leak_in_html_page(self, server_url):
        r = requests.get(f"{server_url}/sessions/crawl")
        text = r.text
        for pat in self.LEAK_PATTERNS:
            assert pat not in text, f"leak: '{pat}' in HTML page"

    def test_no_leak_in_events(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/crawl/events")
        text = r.text
        for pat in self.LEAK_PATTERNS:
            assert pat not in text, f"leak: '{pat}' in events"
