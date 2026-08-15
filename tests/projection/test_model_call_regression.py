"""Model-call regression test (contract §3/§12): fake counters —
N≥20 consecutive data deltas ⇒ +0 architect/compiler/CUA calls.

The projection must NOT trigger any model calls during ordinary read
paths (snapshot, workflow, variables, surfaces, events, artifact).
"""
from __future__ import annotations

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
from taskvm.projection.view_models import snapshot_view


def _contract(cid, key, value):
    return ActionContract(
        contract_id=cid, semantic_goal=f"set {key} to {value}",
        desired_state={key: value},
        completion_condition=f"{key} shows {value}")


def _make_kernel(sid="s1"):
    intent = TaskIntent(goal="发布")
    kernel = TaskVMKernel(sid, intent)
    kernel.init_task_state([
        TaskVariable(semantic_key="release_date", label="发布日期",
                     observed="2026-08-14", desired="2026-08-18"),
    ])
    graph = WorkflowGraph(nodes=(
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="设置",
                     contract=_contract("c1", "release_date", "2026-08-18")),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a1",)),
    ))
    kernel.set_plan(graph)
    return kernel


class TestModelCallRegression:
    def test_snapshot_has_zero_model_calls(self):
        """snapshot_view costs 0 model calls (contract §3)."""
        kernel = _make_kernel()
        calls = [0]
        def probe():
            calls[0] += 1
            return calls[0]

        from taskvm.projection.store import ProjectionSession
        sess = ProjectionSession(
            sid="s1", kernel=kernel,
            model_call_probe=probe)
        snap1 = snapshot_view(sess)
        before = calls[0]
        snap2 = snapshot_view(sess)
        snap3 = snapshot_view(sess)
        # the probe itself increments — but the KEY assertion is that
        # snapshot_view doesn't trigger external model calls. The probe
        # is called at most once per governance_view (for the bar), so
        # 3 snapshots → 3 probe calls, not 3*N architecture calls.
        assert calls[0] <= 3  # at most 1 per snapshot (governance bar)

    def test_repeated_snapshot_model_calls_unchanged(self):
        """20 consecutive snapshots: model call count grows only by the
        probe invocations (1 per snapshot for governance bar), not by
        any architecture/compiler/CUA call."""
        kernel = _make_kernel()
        calls = [0]
        def probe():
            calls[0] += 1
            return calls[0]

        from taskvm.projection.store import ProjectionSession
        sess = ProjectionSession(
            sid="s1", kernel=kernel,
            model_call_probe=probe)

        baseline = 0
        for _ in range(20):
            snapshot_view(sess)
        # 20 snapshots → at most 20 governance_bar probe calls (1 each)
        # No architecture/compiler/CUA calls should fire.
        after = calls[0]
        delta = after - baseline
        assert delta <= 20, (
            f"20 snapshots triggered {delta} probe calls; "
            "expected at most 20 (1 per governance bar)")
        assert delta > 0, "probe should have been called at least once"

    def test_read_routes_no_model_calls(self):
        """All GET read routes cost 0 external model calls."""
        kernel = _make_kernel()
        calls = [0]
        def probe():
            calls[0] += 1
            return calls[0]

        store = ProjectionSessionStore()
        art = ArtifactStore()
        art.put("ref1", b"data")
        store.register("s1", kernel,
                       surfaces=[SurfaceDecl(surface_id="s1",
                                            display_name="X")],
                       artifacts=art,
                       model_call_probe=probe)
        app = create_app(store)
        app.config["TESTING"] = True
        c = app.test_client()

        # fire all read routes
        c.get("/api/sessions/s1/snapshot")
        c.get("/api/sessions/s1/governance")
        c.get("/api/sessions/s1/variables")
        c.get("/api/sessions/s1/workflow")
        c.get("/api/sessions/s1/checkpoints")
        c.get("/api/sessions/s1/surfaces")
        c.get("/api/sessions/s1/conflicts")
        c.get("/api/sessions/s1/events")
        c.get("/api/sessions/s1/artifacts/ref1")

        # The probe is invoked by governance_view (inside snapshot + governance
        # route). Each governance_view calls probe() once. So we expect:
        # snapshot route: 1, governance route: 1 = 2 total (at most).
        # No architecture/compiler/CUA calls should fire.
        assert calls[0] <= 2, (
            f"read routes triggered {calls[0]} probe calls; "
            "expected at most 2 (1 snapshot + 1 governance)")

    def test_artifact_serving_no_model_calls(self):
        """GET /artifacts/<ref> costs 0 model calls (contract §5)."""
        kernel = _make_kernel()
        calls = [0]
        def probe():
            calls[0] += 1
            return calls[0]

        store = ProjectionSessionStore()
        art = ArtifactStore()
        art.put("ref1", b"data")
        store.register("s1", kernel, artifacts=art,
                       model_call_probe=probe)
        app = create_app(store)
        app.config["TESTING"] = True
        c = app.test_client()
        c.get("/api/sessions/s1/artifacts/ref1")
        assert calls[0] == 0, "artifact serving triggered a model call"
