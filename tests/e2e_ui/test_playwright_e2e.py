"""Playwright E2E tests — real browser against the projection server.

These tests are skipped when Playwright browsers are not installed.
They verify the full browser ↔ server round trip: page load, API fetch,
SSE EventSource, and governance button interactions.

Requires:
  PLAYWRIGHT_BROWSERS_PATH pointing to the browser installation.
  LD_LIBRARY_PATH for shared libraries.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

# Try to import playwright; skip entire module if unavailable
try:
    from playwright.sync_api import sync_playwright
    _PW_AVAILABLE = True
except ImportError:
    _PW_AVAILABLE = False

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

pytestmark = pytest.mark.skipif(not _PW_AVAILABLE,
                                reason="playwright not installed")


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


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    store = ProjectionSessionStore()
    art = ArtifactStore()
    art.put("ref1", b"pw-png-bytes")
    store.register("s1", _make_kernel("s1"),
                   surfaces=(SurfaceDecl(surface_id="surf1",
                                        display_name="X平台"),),
                   artifacts=art)
    app = create_app(store)
    port = _free_port()

    def _run():
        app.run(host="127.0.0.1", port=port, threaded=True,
                debug=False, use_reloader=False)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    import requests
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


def _can_launch_browser():
    """Check if a Playwright browser can actually launch."""
    if not _PW_AVAILABLE:
        return False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def browser():
    if not _can_launch_browser():
        pytest.skip("playwright browser not installed")
    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=True)
    yield b
    b.close()
    pw.stop()


class TestPlaywrightE2E:
    def test_api_fetch_from_browser(self, server_url, browser):
        """A real browser can fetch the projection API and parse JSON."""
        page = browser.new_page()
        page.goto(server_url)
        # execute fetch inside the browser context
        result = page.evaluate("""async (url) => {
            const r = await fetch(url + '/api/sessions/s1/snapshot');
            const d = await r.json();
            return {status: r.status, sid: d.sid, has_workflow: !!d.workflow};
        }""", server_url)
        assert result["status"] == 200
        assert result["sid"] == "s1"
        assert result["has_workflow"] is True
        page.close()

    def test_sse_eventsource_in_browser(self, server_url, browser):
        """SSE EventSource connects and receives the initial snapshot."""
        page = browser.new_page()
        page.goto(server_url)
        # set up EventSource and capture the first message
        page.evaluate("""async (url) => {
            window._sse_events = [];
            const es = new EventSource(url + '/api/sessions/s1/sse');
            window._sse = es;
            return new Promise((resolve) => {
                es.onmessage = (e) => {
                    window._sse_events.push(e.data);
                    es.close();
                    resolve(true);
                };
                es.onerror = () => { es.close(); resolve(false); };
                setTimeout(() => { es.close(); resolve(false); }, 5000);
            });
        }""", server_url)
        # verify at least one event was received
        events = page.evaluate("() => window._sse_events || []")
        assert len(events) > 0, "no SSE events received"
        data = json.loads(events[0])
        assert data["sse_type"] == "snapshot"
        page.close()

    def test_governance_command_via_fetch(self, server_url, browser):
        """POST a governance command from the browser and verify response."""
        page = browser.new_page()
        page.goto(server_url)
        result = page.evaluate("""async (url) => {
            const r = await fetch(url + '/api/sessions/s1/governance/checkpoint', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({label: '浏览器检查点'})
            });
            const d = await r.json();
            return {status: r.status, ok: d.ok, label: d.label};
        }""", server_url)
        assert result["status"] == 200
        assert result["ok"] is True
        assert result["label"] == "浏览器检查点"
        page.close()

    def test_no_internal_id_in_page(self, server_url, browser):
        """No internal entity_id or data-* attributes leak to the browser."""
        page = browser.new_page()
        page.goto(f"{server_url}/api/sessions/s1/snapshot")
        body_text = page.inner_text("body")
        assert "entity_id" not in body_text
        page.close()
