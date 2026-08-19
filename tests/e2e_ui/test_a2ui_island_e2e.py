"""A5 island e2e — the React island's REAL A2UI stream in a real browser.

Hand-built kernel (zero model calls — same discipline as
test_playwright_e2e.py), the REAL transport routes, the REAL built island
(taskvm/workspace_ui/static/a2ui/, produced by `npm run build`), Playwright
chromium headless.

Acceptance locks (the A5 card):

  1. ``/a2ui`` serves the built island; the §20.1 progress events (goal /
     t1 / t2 / ready) drive the morph chain through the REAL SSE
     connection; the A2UI surface renders through the official renderer
     with BOUND values (data model, not literals);
  2. server→client value updates land with ZERO model calls and WITHOUT
     regenerating the component tree: the live poller appends ONE
     small updateDataModel frame; ``generation`` stays frozen while
     ``dataRevision`` bumps (asserted server-side, visible browser-side);
  3. client→server: a REAL GUI gesture (type into the rendered TextField
     + click the rendered Button) POSTs ONE taskvm.local_patch; the
     kernel's desired actually moves; the value round-trips back onto
     the screen through the zero-model-call path;
  4. readonly variables are rejected honestly (HTTP 403).

Screenshots land in eval_results/a5_transport_20260820/.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import types

import pytest

try:
    from playwright.sync_api import sync_playwright
    _PW_AVAILABLE = True
except ImportError:
    _PW_AVAILABLE = False

from taskvm.domain import (
    ActionContract, NodeKind, TaskIntent, TaskVariable,
    WorkflowGraph, WorkflowNode,
)
from taskvm.kernel import TaskVMKernel
from taskvm.projection.store import ProjectionSessionStore
from taskvm.workspace_ui import serve
from taskvm.workspace_ui.a2ui_transport import (
    A2uiTransport, kernel_stage_payload, register_a2ui_routes,
)

pytestmark = pytest.mark.skipif(not _PW_AVAILABLE,
                                reason="playwright not installed")

_SHOT_DIR = os.path.join("eval_results", "a5_transport_20260820")


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


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def island():
    """The REAL island stack: stock projection app + static island +
    A2UI transport, plus the §20.1 signals the APP shell would push."""
    store = ProjectionSessionStore()
    kernel = _make_kernel("s1")
    store.register("s1", kernel)
    transport = A2uiTransport(session_lookup=store.get)
    state = types.SimpleNamespace(sid="s1")
    app = serve(store)
    register_a2ui_routes(app, transport, store, state)

    # the honest stage signals (app_open wiring): goal → t1 labels → t2 DAG,
    # then attach_session mints the surface and pushes "ready"
    transport.push_stage("s1", "goal", {"goal": "发布产品"})
    transport.push_stage("s1", "t1", {"variables": [
        {"label": "发布备注"}, {"label": "预算"},
    ]})
    transport.push_stage("s1", "t2", kernel_stage_payload(kernel))
    transport.attach_session("s1", store.get("s1"))   # starts the poller

    port = _free_port()

    def _run():
        app.run(host="127.0.0.1", port=port, threaded=True,
                debug=False, use_reloader=False)

    threading.Thread(target=_run, daemon=True).start()
    import requests
    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            if requests.get(f"{base}/api/app/a2ui/bootstrap",
                            timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        pytest.fail("island server did not start")

    yield types.SimpleNamespace(base=base, store=store,
                                transport=transport, kernel=kernel)


def _can_launch_browser():
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


def _bootstrap(island):
    import requests
    return requests.get(f"{island.base}/api/app/a2ui/bootstrap",
                        timeout=5).json()


def _desired(island):
    return _bootstrap(island)["messages"][-1]["updateDataModel"][
        "value"]["variables"]["release_note"]["desired"]


def _dump_evidence(name: str, payload: dict) -> None:
    """Contract §6: verification claims ship their RAW fields — the
    bootstrap endpoint's own JSON, not a hand-summary."""
    path = os.path.join(_SHOT_DIR, name)
    existing = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
    existing.update(payload)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


class TestA2uiIslandE2E:

    def test_island_loads_and_surface_renders_with_bound_values(
            self, island, browser):
        """The REAL built island loads; progress events drive the morph;
        the A2UI surface renders with BOUND values, not literals."""
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{island.base}/a2ui")

        # the morph chain completed: the plane went live
        page.wait_for_selector('[data-testid="plane-live"]', timeout=15000)
        # the goal text came from the SERVER's goal progress signal
        goal = page.inner_text('[data-testid="goal-text"]')
        assert "发布产品" in goal
        # bound /variables/<key>/desired renders the data model's value
        page.wait_for_function(
            "() => document.querySelector('input')?.value === 'v1'",
            timeout=10000)
        # the editable variable's label is rendered (from the binding)
        assert "发布备注" in page.inner_text('[data-testid="plane-live"]')
        assert errors == []      # no island runtime errors

        os.makedirs(_SHOT_DIR, exist_ok=True)
        page.screenshot(path=os.path.join(
            _SHOT_DIR, "e2e_01_island_live_bound_values.png"),
            full_page=True)
        page.close()

    def test_server_to_client_value_update_zero_genui_calls(
            self, island, browser):
        """A server-side desired change reaches the browser through ONE
        small updateDataModel frame — the component tree (generation) is
        NEVER regenerated on this path."""
        import requests
        before = _bootstrap(island)
        assert before["generation"] == 1

        # a governance local_patch moves the kernel's desired (the same
        # write the fixed shell's route would perform) — zero model calls
        island.store.get("s1").governance_port().local_patch(
            {"release_note": "v2-from-server"}, rationale="e2e")

        page = browser.new_page()
        page.goto(f"{island.base}/a2ui")
        page.wait_for_selector('[data-testid="plane-live"]', timeout=15000)
        # the poller (1s) + SSE + renderer land the new value on screen
        page.wait_for_function(
            "() => document.querySelector('input')?.value === "
            "'v2-from-server'", timeout=10000)

        after = _bootstrap(island)
        assert after["generation"] == 1            # NO component regen
        assert after["dataRevision"] > before["dataRevision"]
        assert _desired(island) == "v2-from-server"

        os.makedirs(_SHOT_DIR, exist_ok=True)
        _dump_evidence("evidence_server_to_client_zero_genui_calls.json", {
            "claim": "server-side desired change lands in the browser via "
                     "ONE small updateDataModel frame; generation FROZEN "
                     "(zero GenUI decoder calls); dataRevision bumps",
            "bootstrap_before": before,
            "bootstrap_after": after,
            "kernel_write": "governance local_patch release_note -> "
                            "v2-from-server (zero model calls)",
            "browser_input_value_observed": "v2-from-server",
        })
        page.screenshot(path=os.path.join(
            _SHOT_DIR, "e2e_02_server_value_update_landed.png"),
            full_page=True)
        page.close()

    def test_gui_gesture_local_patch_round_trip(self, island, browser):
        """A REAL GUI gesture (type + click the rendered Button) posts
        ONE local_patch; the kernel's desired moves; the value lands
        back through the zero-model-call path."""
        import requests
        page = browser.new_page()
        page.goto(f"{island.base}/a2ui")
        page.wait_for_selector('[data-testid="plane-live"]', timeout=15000)
        page.wait_for_function(
            "() => document.querySelector('input')?.value === "
            "'v2-from-server'", timeout=10000)

        # the user edits the TextField and presses the rendered Button —
        # the REAL GUI gesture, nothing else
        page.fill("input", "v3-from-gesture")
        page.get_by_role("button", name="更新").click()

        # the bridge posted ONE local_patch (the honest ack is on screen)
        page.wait_for_function(
            "() => document.querySelector('[data-testid=\"last-action\"]')"
            "?.textContent?.includes('local_patch(release_note) 已提交')",
            timeout=10000)

        # the kernel's desired ACTUALLY moved. The bootstrap endpoint
        # reads the SurfaceStore's data model, which the poller mirrors
        # within one POLL_INTERVAL_S — wait for the frame, then assert.
        deadline = time.time() + 5.0
        while time.time() < deadline and _desired(island) != "v3-from-gesture":
            time.sleep(0.2)
        assert _desired(island) == "v3-from-gesture"
        after = _bootstrap(island)
        assert after["generation"] == 1        # still zero GenUI calls

        os.makedirs(_SHOT_DIR, exist_ok=True)
        _dump_evidence("evidence_gui_gesture_round_trip.json", {
            "claim": "a REAL GUI gesture (type + click the rendered Button) "
                     "posts ONE taskvm.local_patch; the kernel desired "
                     "moves and mirrors back with generation FROZEN",
            "gesture": "fill TextField 'v3-from-gesture' + click Button '更新'",
            "bootstrap_after": after,
            "kernel_desired_observed": _desired(island),
            "generation_frozen": after["generation"] == 1,
        })
        page.screenshot(path=os.path.join(
            _SHOT_DIR, "e2e_03_gui_gesture_round_trip.png"),
            full_page=True)
        page.close()

    def test_readonly_variable_rejected_honestly(self, island, browser):
        """A readonly variable never gets an input affordance in the
        baseline tree, and a direct local_patch against it is a 403."""
        import requests
        page = browser.new_page()
        page.goto(f"{island.base}/a2ui")
        page.wait_for_selector('[data-testid="plane-live"]', timeout=15000)
        # only the editable variable has an input; budget renders as text
        assert page.locator("input").count() == 1
        assert "预算" in page.inner_text('[data-testid="plane-live"]')

        resp = requests.post(f"{island.base}/api/app/a2ui/action",
                             json={"name": "taskvm.local_patch",
                                   "context": {"semanticKey": "budget",
                                               "value": 1}}, timeout=5)
        assert resp.status_code == 403
        assert "readonly" in resp.json()["error"]
        page.close()
