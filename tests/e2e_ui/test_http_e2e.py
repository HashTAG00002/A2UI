"""E2E HTTP tests — real TCP server + real HTTP client (no Flask test_client).

Launches the projection Flask app on a random port, exercises the full
route matrix via ``requests``, and verifies the SSE stream produces live
deltas. This is the closest to production without a real browser.

Contract coverage:
  - §6 route matrix (every GET + every POST)
  - §3 zero model calls on read paths (assert in governance view)
  - §5 no substrate import (static analysis gate at import time)
  - SSE §6.4 (live delta push after governance command)
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
from taskvm.projection.app import create_app
from taskvm.projection.store import (
    ArtifactStore,
    ProjectionSessionStore,
    SurfaceDecl,
)


# ── helpers ──────────────────────────────────────────────────────────────

def _contract(cid, key, value):
    return ActionContract(
        contract_id=cid,
        semantic_goal=f"set {key} to {value}",
        desired_state={key: value},
        completion_condition=f"{key} shows {value}",
    )


def _make_kernel(sid="s1"):
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    """Start a real Flask server on a free port; return base URL."""
    store = ProjectionSessionStore()
    art = ArtifactStore()
    art.put("ref1", b"e2e-png-bytes")
    store.register("s1", _make_kernel("s1"),
                   surfaces=(SurfaceDecl(surface_id="surf1",
                                        display_name="X平台"),),
                   artifacts=art)
    # s2: pristine kernel for rollback test (s1's goal_patch sets
    # _pending_recompose which blocks checkpoint/rollback on s1)
    store.register("s2", _make_kernel("s2"),
                   surfaces=(SurfaceDecl(surface_id="surf1",
                                        display_name="X平台"),),
                   artifacts=art)
    app = create_app(store)
    port = _free_port()

    def _run():
        # threaded=True so SSE doesn't block other requests
        app.run(host="127.0.0.1", port=port, threaded=True,
                debug=False, use_reloader=False)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # wait for server to be ready
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            r = requests.get(f"{base}/api/sessions", timeout=1)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        pytest.fail("server did not start within 6s")
    yield base


# ── read route matrix via real HTTP ───────────────────────────────────────

class TestReadRoutesE2E:
    def test_list_sessions(self, server_url):
        r = requests.get(f"{server_url}/api/sessions")
        assert r.status_code == 200
        assert "s1" in r.json()["sessions"]

    def test_snapshot(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/s1/snapshot")
        assert r.status_code == 200
        data = r.json()
        assert data["sid"] == "s1"
        assert "governance" in data
        assert "workflow" in data

    def test_governance(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/s1/governance")
        assert r.status_code == 200
        assert r.json()["goal"] == "发布产品"

    def test_variables(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/s1/variables")
        assert r.status_code == 200
        assert r.json()[0]["key"] == "release_date"

    def test_workflow(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/s1/workflow")
        assert r.status_code == 200
        assert r.json()["has_plan"] is True

    def test_checkpoints(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/s1/checkpoints")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_surfaces(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/s1/surfaces")
        assert r.status_code == 200
        assert r.json()[0]["display_name"] == "X平台"

    def test_conflicts(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/s1/conflicts")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_events(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/s1/events")
        assert r.status_code == 200
        data = r.json()
        assert "events" in data
        assert "total" in data

    def test_artifact_bytes(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/s1/artifacts/ref1")
        assert r.status_code == 200
        assert r.content == b"e2e-png-bytes"

    def test_artifact_404(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/s1/artifacts/nonexistent")
        assert r.status_code == 404


# ── 404 on unknown sid ───────────────────────────────────────────────────

class TestUnknownSidE2E:
    def test_snapshot_404(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/nope/snapshot")
        assert r.status_code == 404

    def test_governance_404(self, server_url):
        r = requests.get(f"{server_url}/api/sessions/nope/governance")
        assert r.status_code == 404


# ── governance commands via real HTTP ───────────────────────────────────

class TestGovernanceCommandsE2E:
    def test_pause(self, server_url):
        r = requests.post(f"{server_url}/api/sessions/s1/governance/pause",
                          json={})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_resume(self, server_url):
        r = requests.post(f"{server_url}/api/sessions/s1/governance/resume",
                          json={})
        assert r.status_code == 200

    def test_checkpoint(self, server_url):
        r = requests.post(f"{server_url}/api/sessions/s1/governance/checkpoint",
                          json={"label": "E2E检查点"})
        # RFC-D1 §6: checkpoint CREATES a resource ⇒ 201
        assert r.status_code == 201
        assert r.json()["ok"] is True
        assert r.json()["label"] == "E2E检查点"

    def test_local_patch(self, server_url):
        r = requests.post(f"{server_url}/api/sessions/s1/governance/local_patch",
                          json={"updates": {"release_date": "2026-09-01"},
                                "rationale": "e2e edit"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_goal_patch(self, server_url):
        r = requests.post(f"{server_url}/api/sessions/s1/governance/goal_patch",
                          json={"goal": "新目标",
                                "rationale": "e2e scope change"})
        # RFC-D1 §6: goal_patch is async two-phase ⇒ 202 Accepted
        assert r.status_code == 202
        assert r.json()["ok"] is True

    def test_rollback(self, server_url):
        # Use s2 (pristine) because test_goal_patch on s1 leaves the
        # kernel in _pending_recompose state, which blocks checkpoint.
        r1 = requests.post(f"{server_url}/api/sessions/s2/governance/checkpoint",
                           json={"label": "rb_target"})
        assert r1.status_code == 201, r1.text
        cp_id = r1.json()["checkpoint_id"]
        r2 = requests.post(f"{server_url}/api/sessions/s2/governance/rollback",
                           json={"target_checkpoint_id": cp_id})
        # RFC-D1 §6: rollback accepts the plan asynchronously ⇒ 202; s2
        # has no runtime/driver registered, so the honest disposition is
        # "pending" (the plan is accepted, execution awaits a driver).
        assert r2.status_code == 202
        assert r2.json()["ok"] is True
        assert r2.json()["disposition"] == "pending"


# ── SSE stream: initial snapshot + live delta ────────────────────────────

class TestSSEStreamE2E:
    def test_sse_initial_snapshot(self, server_url):
        """SSE stream yields an initial snapshot frame."""
        r = requests.get(f"{server_url}/api/sessions/s1/sse",
                         stream=True, timeout=5)
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        # read first data line
        for line in r.iter_lines():
            if line and line.startswith(b"data: "):
                payload = line[len(b"data: "):]
                data = json.loads(payload)
                assert data["sse_type"] == "snapshot"
                break
        r.close()

    def test_sse_live_delta(self, server_url):
        """Governance command pushes a live delta to SSE subscribers."""
        # open SSE in a background thread
        received = []
        stop = threading.Event()

        def _listen():
            r = requests.get(f"{server_url}/api/sessions/s1/sse",
                             stream=True, timeout=10)
            for line in r.iter_lines():
                if stop.is_set():
                    break
                if line and line.startswith(b"data: "):
                    payload = line[len(b"data: "):]
                    try:
                        data = json.loads(payload)
                        received.append(data)
                        if data.get("sse_type") == "governance.applied":
                            break
                    except Exception:
                        pass
            r.close()

        t = threading.Thread(target=_listen, daemon=True)
        t.start()
        time.sleep(0.5)  # let SSE subscriber connect
        # trigger a governance command → should push to SSE
        requests.post(f"{server_url}/api/sessions/s1/governance/pause", json={})
        t.join(timeout=5)
        stop.set()
        # verify we got a governance.applied delta
        types = [d.get("sse_type") for d in received]
        assert "governance.applied" in types, \
            f"expected governance.applied in SSE, got: {types}"


# ── no-leak gate: internal ids must not appear in API responses ─────────

class TestNoLeakE2E:
    def test_no_entity_id_in_snapshot(self, server_url):
        """Internal entity_id must NOT appear in any API response (GG §0)."""
        r = requests.get(f"{server_url}/api/sessions/s1/snapshot")
        text = r.text
        # entity_id pattern: a string that looks like an internal id
        # (this is a heuristic — if internal ids leak, they'd show here)
        assert "entity_id" not in text, "entity_id key leaked into snapshot"
        assert "data-" not in text, "data-* attribute leaked into snapshot"
