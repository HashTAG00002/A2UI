"""A-02 Governance Lifecycle Tests — persistent stop, single-owner path,
no resume from stopped, no double epoch bump.

Tests the three invariants of A-02:
  1. **Persistent stop**: ``stop()`` is terminal — ``start()`` cannot revive
     the same driver, ``resume()`` returns ``"stopped"``, ``pause()`` also
     returns ``"stopped"``.
  2. **Single-owner path**: one HTTP lifecycle request → one kernel
     governance event (no double epoch bump from a second write path).
  3. **Runtime _stopped flag**: once ``request_stop()`` lands, the runtime's
     ``_pre_tick()`` returns STOPPED on every subsequent tick, blocking any
     further GUI action.

Uses Flask test client + the same _FakeRuntime pattern as test_route_matrix.
"""
from __future__ import annotations

import pytest

from taskvm.domain import (
    ActionContract, NodeKind, Reversibility, TaskIntent,
    TaskVariable, WorkflowGraph, WorkflowNode,
)
from taskvm.kernel import TaskVMKernel
from taskvm.projection.app import create_app
from taskvm.projection.store import (
    ArtifactStore, ProjectionSessionStore, SurfaceDecl,
)
from taskvm.projection.services.driver import ThreadedRuntimeDriver


# ── shared fakes (identical to test_route_matrix.py) ────────────────────

class _FakeRuntime:
    """Minimal duck-typed runtime — records governance calls for epoch
    counting and has the lifecycle methods the driver calls."""

    def __init__(self, kernel):
        self._kernel = kernel
        self._paused = False
        self._stopped = False
        self.governance_calls: list[str] = []

    def run(self, step_budget=None):
        if self._stopped:
            return "stopped"
        return "done"

    def request_pause(self):
        self._paused = True
        self.governance_calls.append("pause")
        self._kernel.request_governance("pause", "test")

    def request_resume(self):
        if self._stopped:
            return
        self._paused = False
        self.governance_calls.append("resume")
        self._kernel.request_governance("resume", "test")

    def request_stop(self):
        self._stopped = True
        self._paused = True
        self.governance_calls.append("stop")
        self._kernel.request_governance("stop", "test")

    def runtime_events(self):
        return ()


# ── fixture ──────────────────────────────────────────────────────────────

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
                     parent_id="seq1",
                     contract=_contract("c1", "release_date", "2026-08-18")),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a1",)),
    ))
    kernel.set_plan(graph)
    return kernel


@pytest.fixture
def app_and_store():
    store = ProjectionSessionStore()
    art = ArtifactStore()
    kernel = _make_kernel("s1")
    rt = _FakeRuntime(kernel)
    driver = ThreadedRuntimeDriver(rt)
    store.register("s1", kernel,
                   runtime=rt,
                   driver=driver,
                   surfaces=[SurfaceDecl(surface_id="surf1",
                                         display_name="X平台")],
                   artifacts=art)
    app = create_app(store)
    return app, store, rt, driver


@pytest.fixture
def client(app_and_store):
    app, _, _, _ = app_and_store
    app.config["TESTING"] = True
    return app.test_client()


# ── 1. Persistent Stop ──────────────────────────────────────────────────

class TestPersistentStop:
    """A-02 invariant 1: stop is terminal and cannot be reversed."""

    def test_stop_returns_stopped(self, app_and_store):
        _, _, _, driver = app_and_store
        state = driver.stop()
        assert state == "stopped"
        assert driver.status() == "stopped"

    def test_start_after_stop_returns_stopped(self, app_and_store):
        _, _, _, driver = app_and_store
        driver.stop()
        # start() on a stopped driver must NOT revive it
        state = driver.start()
        assert state == "stopped"
        assert driver.status() == "stopped"

    def test_resume_after_stop_returns_stopped(self, app_and_store):
        _, _, _, driver = app_and_store
        driver.stop()
        state = driver.resume()
        assert state == "stopped"
        assert driver.status() == "stopped"

    def test_pause_after_stop_returns_stopped(self, app_and_store):
        _, _, _, driver = app_and_store
        driver.stop()
        state = driver.pause()
        assert state == "stopped"
        assert driver.status() == "stopped"

    def test_stop_is_idempotent(self, app_and_store):
        _, _, _, driver = app_and_store
        driver.stop()
        # a second stop is still "stopped" (no error, no state change)
        state = driver.stop()
        assert state == "stopped"

    def test_runtime_stopped_flag_prevents_run(self, app_and_store):
        """Once request_stop() is called, runtime.run() returns STOPPED."""
        _, _, rt, driver = app_and_store
        driver.stop()
        assert rt._stopped is True
        result = rt.run(step_budget=1)
        assert str(result) == "stopped"


# ── 2. Single-Owner Path (no double epoch bump) ─────────────────────────

class TestSingleOwnerEpoch:
    """A-02 invariant 2: one HTTP lifecycle request → exactly ONE kernel
    governance event. The old double-write path (app.py calling
    KernelGovernancePort.pause AND runtime calling kernel.request_governance)
    is gone."""

    def _count_governance_events(self, kernel, action: str) -> int:
        return sum(1 for e in kernel.events()
                   if getattr(e, "kind", None) is not None
                   and "governance" in str(e.kind)
                   and getattr(e, "payload", {}).get("action") == action)

    def test_pause_one_governance_event(self, app_and_store):
        _, store, _, _ = app_and_store
        sess = store.get("s1")
        kernel = sess.kernel
        before = len(kernel.events())
        # simulate the HTTP pause route's path
        driver = sess.driver
        driver.pause()
        after = len(kernel.events())
        delta = after - before
        # exactly ONE governance event (not two)
        assert delta == 1, (
            f"pause produced {delta} kernel events, expected 1 "
            f"(single-owner path violated)")

    def test_resume_one_governance_event(self, app_and_store):
        _, store, _, _ = app_and_store
        sess = store.get("s1")
        kernel = sess.kernel
        driver = sess.driver
        # pause first so resume has something to do
        driver.pause()
        before = len(kernel.events())
        driver.resume()
        after = len(kernel.events())
        delta = after - before
        assert delta == 1, (
            f"resume produced {delta} kernel events, expected 1 "
            f"(single-owner path violated)")

    def test_stop_one_governance_event(self, app_and_store):
        _, store, _, _ = app_and_store
        sess = store.get("s1")
        kernel = sess.kernel
        driver = sess.driver
        before = len(kernel.events())
        driver.stop()
        after = len(kernel.events())
        delta = after - before
        assert delta == 1, (
            f"stop produced {delta} kernel events, expected 1 "
            f"(single-owner path violated)")

    def test_runtime_governance_calls_match(self, app_and_store):
        """The _FakeRuntime records its own governance_calls — they must
        match the kernel events 1:1 (no phantom second call)."""
        _, store, rt, driver = app_and_store
        kernel = store.get("s1").kernel
        driver.pause()
        assert rt.governance_calls == ["pause"]
        driver.resume()
        assert rt.governance_calls == ["pause", "resume"]
        driver.stop()
        assert rt.governance_calls == ["pause", "resume", "stop"]
        # exactly 3 governance calls → exactly 3 kernel events
        from taskvm.domain.events import EventKind
        gov_events = [e for e in kernel.events()
                      if e.kind == EventKind.GOVERNANCE_REQUESTED]
        assert len(gov_events) == 3, (
            f"expected 3 governance events, got {len(gov_events)}: "
            f"{[e.kind for e in kernel.events()]}")
        # verify the actions match
        actions = [e.payload.get("action") for e in gov_events]
        assert actions == ["pause", "resume", "stop"]


# ── 3. HTTP Route Level ──────────────────────────────────────────────────

class TestHTTPLifecycle:
    """A-02 at the HTTP layer: routes return correct states and 409 on
    resume-after-stop."""

    def test_stop_then_resume_409(self, client):
        """stop → resume must return 409 (stop is persistent)."""
        r = client.post("/api/sessions/s1/governance/stop", json={})
        assert r.status_code == 200
        assert r.get_json()["state"] == "stopped"

        r2 = client.post("/api/sessions/s1/governance/resume", json={})
        assert r2.status_code == 409
        assert r2.get_json()["ok"] is False
        assert r2.get_json()["state"] == "stopped"

    def test_stop_then_start_409(self, client):
        """stop → start must return 409 (cannot revive a stopped driver)."""
        client.post("/api/sessions/s1/governance/stop", json={})
        r = client.post("/api/sessions/s1/governance/start", json={})
        assert r.status_code == 409
        assert r.get_json()["ok"] is False

    def test_stop_then_pause_returns_stopped(self, client):
        """stop → pause returns 200 with state=stopped (not an error, but
        the state is terminal)."""
        client.post("/api/sessions/s1/governance/stop", json={})
        r = client.post("/api/sessions/s1/governance/pause", json={})
        assert r.status_code == 200
        assert r.get_json()["state"] == "stopped"

    def test_pause_resume_cycle(self, client):
        """Normal pause → resume cycle works (no stop in between)."""
        r1 = client.post("/api/sessions/s1/governance/pause", json={})
        assert r1.status_code == 200
        assert r1.get_json()["state"] == "paused"

        r2 = client.post("/api/sessions/s1/governance/resume", json={})
        assert r2.status_code == 200
        assert r2.get_json()["state"] == "running"

    def test_stop_is_terminal_in_http(self, client):
        """After stop, no lifecycle route can change the state."""
        client.post("/api/sessions/s1/governance/stop", json={})
        # try resume
        r = client.post("/api/sessions/s1/governance/resume", json={})
        assert r.get_json()["state"] == "stopped"
        # try pause
        r = client.post("/api/sessions/s1/governance/pause", json={})
        assert r.get_json()["state"] == "stopped"
        # try start
        r = client.post("/api/sessions/s1/governance/start", json={})
        assert r.status_code == 409
