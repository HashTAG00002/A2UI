"""View-model tests (contract §9/§12): workflow primitives correctness,
business labels, checkpoint timeline, surface cards from events.

Pure functions: 0 side effects, 0 model calls, 0 substrate knowledge.
"""
from __future__ import annotations

import pytest

from taskvm.domain import (
    ActionContract,
    EventKind,
    NodeKind,
    NodeStatus,
    ProjectionComponent,
    ProjectionSchema,
    Reversibility,
    SurfaceEvidence,
    TaskIntent,
    TaskVariable,
    VerificationResult,
    WorkflowGraph,
    WorkflowNode,
)
from taskvm.kernel import TaskVMKernel

from taskvm.projection.store import (
    ArtifactStore,
    ProjectionSession,
    SurfaceDecl,
)
from taskvm.projection.view_models import (
    checkpoint_view,
    conflicts_view,
    governance_view,
    projection_data_view,
    projection_schema_view,
    snapshot_view,
    surface_cards,
    variables_view,
    workflow_view,
)


# ── helpers ──────────────────────────────────────────────────────────────

def _contract(cid, key, value, reversible=True):
    return ActionContract(
        contract_id=cid,
        semantic_goal=f"set {key} to {value}",
        desired_state={key: value},
        completion_condition=f"{key} visibly shows {value}",
        reversibility=(Reversibility.REVERSIBLE if reversible
                       else Reversibility.IRREVERSIBLE))


def _make_kernel_with_sequence():
    """A kernel with a simple sequence plan: A → VERIFY → TERMINAL."""
    intent = TaskIntent(goal="发布产品", scope=["发布"])
    kernel = TaskVMKernel("s1", intent)
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
        WorkflowNode(node_id="v1", kind=NodeKind.VERIFY, label="验证发布日期",
                     parent_id="seq1", depends_on=("a1",),
                     verification="release_date shows 2026-08-18"),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("v1",)),
    ))
    kernel.set_plan(graph)
    return kernel


def _make_kernel_with_fanout():
    """A kernel with fan-out + barrier: FAN_OUT → (A, A) → BARRIER → TERMINAL."""
    intent = TaskIntent(goal="多平台发布")
    kernel = TaskVMKernel("s2", intent)
    kernel.init_task_state([
        TaskVariable(semantic_key="x_date", label="X发布日期",
                     observed="old", desired="new"),
        TaskVariable(semantic_key="w_date", label="微信发布日期",
                     observed="old", desired="new"),
    ])
    graph = WorkflowGraph(nodes=(
        WorkflowNode(node_id="fo1", kind=NodeKind.FAN_OUT, label="多平台分发"),
        WorkflowNode(node_id="a_x", kind=NodeKind.ACTION, label="X平台发布",
                     parent_id="fo1",
                     contract=_contract("cx", "x_date", "new")),
        WorkflowNode(node_id="a_w", kind=NodeKind.ACTION, label="微信发布",
                     parent_id="fo1",
                     contract=_contract("cw", "w_date", "new")),
        WorkflowNode(node_id="b1", kind=NodeKind.BARRIER, label="发布完成验证",
                     depends_on=("fo1",)),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("b1",)),
    ))
    kernel.set_plan(graph)
    return kernel


def _make_kernel_with_loop():
    """A kernel with a bounded loop containing one ACTION child."""
    intent = TaskIntent(goal="批量处理")
    kernel = TaskVMKernel("s3", intent)
    kernel.init_task_state([
        TaskVariable(semantic_key="batch_done", label="批量完成数",
                     observed=0, desired=10),
    ])
    graph = WorkflowGraph(nodes=(
        WorkflowNode(node_id="lp1", kind=NodeKind.BOUNDED_LOOP,
                     label="批量循环",
                     termination_predicate="batch_done >= 10",
                     max_iterations=5),
        WorkflowNode(node_id="a_lp", kind=NodeKind.ACTION, label="处理一条",
                     parent_id="lp1",
                     contract=_contract("clp", "batch_done", 10)),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("lp1",)),
    ))
    kernel.set_plan(graph)
    return kernel


def _make_session(kernel=None, surfaces=()):
    if kernel is None:
        kernel = _make_kernel_with_sequence()
    return ProjectionSession(
        sid=kernel.session_id,
        kernel=kernel,
        surfaces=tuple(surfaces),
    )


# ── workflow_view: sequence primitive ────────────────────────────────────

class TestWorkflowSequence:
    def test_sequence_has_plan(self):
        kernel = _make_kernel_with_sequence()
        wf = kernel.workflow()
        view = workflow_view(wf, kernel.events())
        assert view["has_plan"] is True
        assert len(view["nodes"]) == 4

    def test_sequence_labels_are_business(self):
        kernel = _make_kernel_with_sequence()
        view = workflow_view(kernel.workflow(), kernel.events())
        labels = [n["label"] for n in view["nodes"]]
        assert "发布流程" in labels
        assert "设置发布日期" in labels
        assert "node_id" not in [l for l in labels]  # no raw id as label

    def test_sequence_kind_labels(self):
        kernel = _make_kernel_with_sequence()
        view = workflow_view(kernel.workflow(), kernel.events())
        kind_labels = {n["kind_label"] for n in view["nodes"]}
        assert "sequence" in kind_labels
        assert "step" in kind_labels
        assert "verification" in kind_labels
        assert "goal" in kind_labels

    def test_sequence_status_labels_are_business(self):
        kernel = _make_kernel_with_sequence()
        view = workflow_view(kernel.workflow(), kernel.events())
        for n in view["nodes"]:
            assert n["status_label"] in (
                "waiting", "ready", "executing", "verified",
                "failed", "invalidated", "rolled_back")

    def test_sequence_depth(self):
        kernel = _make_kernel_with_sequence()
        view = workflow_view(kernel.workflow(), kernel.events())
        by_id = {n["node_id"]: n for n in view["nodes"]}
        assert by_id["seq1"]["depth"] == 0
        assert by_id["a1"]["depth"] == 1
        assert by_id["v1"]["depth"] == 1
        assert by_id["t1"]["depth"] == 0  # t1 has no parent_id

    def test_sequence_progress(self):
        kernel = _make_kernel_with_sequence()
        view = workflow_view(kernel.workflow(), kernel.events())
        assert view["progress"]["total"] == 4
        assert view["progress"]["committed"] == 0

    def test_action_node_has_reversibility(self):
        kernel = _make_kernel_with_sequence()
        view = workflow_view(kernel.workflow(), kernel.events())
        a1 = [n for n in view["nodes"] if n["node_id"] == "a1"][0]
        assert "action" in a1
        assert a1["action"]["goal"] == "set release_date to 2026-08-18"
        assert a1["action"]["irreversible"] is False

    def test_verify_node_has_verification(self):
        kernel = _make_kernel_with_sequence()
        view = workflow_view(kernel.workflow(), kernel.events())
        v1 = [n for n in view["nodes"] if n["node_id"] == "v1"][0]
        assert "verification" in v1
        assert "release_date" in v1["verification"]

    def test_checkpoint_marker(self):
        """CHECKPOINT nodes have is_checkpoint=True."""
        from taskvm.domain import NodeKind, WorkflowNode
        intent = TaskIntent(goal="测试")
        kernel = TaskVMKernel("s_cp", intent)
        kernel.init_task_state([
            TaskVariable(semantic_key="x", label="X", observed="a", desired="b"),
        ])
        graph = WorkflowGraph(nodes=(
            WorkflowNode(node_id="cp1", kind=NodeKind.CHECKPOINT, label="检查点1"),
            WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="动作",
                         depends_on=("cp1",),
                         contract=_contract("c1", "x", "b")),
            WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                         depends_on=("a1",)),
        ))
        kernel.set_plan(graph)
        view = workflow_view(kernel.workflow(), kernel.events())
        cp = [n for n in view["nodes"] if n["node_id"] == "cp1"][0]
        assert cp["is_checkpoint"] is True
        assert cp["rollback_boundary"] is True


# ── workflow_view: fan-out / barrier primitive ──────────────────────────

class TestWorkflowFanOut:
    def test_fanout_has_lanes(self):
        kernel = _make_kernel_with_fanout()
        view = workflow_view(kernel.workflow(), kernel.events())
        assert view["has_plan"] is True
        assert len(view["nodes"]) == 5

    def test_fanout_kind_label(self):
        kernel = _make_kernel_with_fanout()
        view = workflow_view(kernel.workflow(), kernel.events())
        fo = [n for n in view["nodes"] if n["kind"] == "fan_out"][0]
        assert fo["kind_label"] == "fan-out"

    def test_barrier_kind_label(self):
        kernel = _make_kernel_with_fanout()
        view = workflow_view(kernel.workflow(), kernel.events())
        b = [n for n in view["nodes"] if n["kind"] == "barrier"][0]
        assert b["kind_label"] == "verify barrier"

    def test_fanout_progress_shows_committed_children(self):
        kernel = _make_kernel_with_fanout()
        wf = kernel.workflow()
        # manually mark one lane as committed to test progress
        statuses = dict(wf.statuses)
        statuses["a_x"] = NodeStatus.COMMITTED
        from taskvm.kernel import WorkflowSnapshot
        wf2 = WorkflowSnapshot(graph=wf.graph, statuses=statuses)
        view = workflow_view(wf2, kernel.events())
        fo = [n for n in view["nodes"] if n["node_id"] == "fo1"][0]
        assert fo["progress"] == {"committed": 1, "total": 2}


# ── workflow_view: bounded loop primitive ───────────────────────────────

class TestWorkflowBoundedLoop:
    def test_loop_has_max_iterations(self):
        kernel = _make_kernel_with_loop()
        view = workflow_view(kernel.workflow(), kernel.events())
        lp = [n for n in view["nodes"] if n["kind"] == "bounded_loop"][0]
        assert "loop" in lp
        assert lp["loop"]["max_iterations"] == 5
        assert lp["loop"]["termination_predicate"] == "batch_done >= 10"

    def test_loop_iteration_from_events(self):
        kernel = _make_kernel_with_loop()
        # begin_loop_iteration is 1-based; emits LOOP_ITERATION_STARTED
        kernel.begin_loop_iteration("lp1")
        view = workflow_view(kernel.workflow(), kernel.events())
        lp = [n for n in view["nodes"] if n["node_id"] == "lp1"][0]
        assert lp["loop"]["iteration"] == 1


# ── workflow_view: empty plan ───────────────────────────────────────────

class TestWorkflowEmpty:
    def test_no_plan(self):
        intent = TaskIntent(goal="空")
        kernel = TaskVMKernel("s_empty", intent)
        kernel.init_task_state([])
        view = workflow_view(kernel.workflow(), kernel.events())
        assert view["has_plan"] is False
        assert view["nodes"] == []


# ── variables_view ───────────────────────────────────────────────────────

class TestVariablesView:
    def test_variables_sorted_by_key(self):
        kernel = _make_kernel_with_sequence()
        vs = variables_view(kernel)
        assert len(vs) == 1
        assert vs[0]["key"] == "release_date"
        assert vs[0]["label"] == "发布日期"
        assert vs[0]["observed"] == "2026-08-14"
        assert vs[0]["desired"] == "2026-08-18"
        assert vs[0]["diverged"] is True
        assert vs[0]["editable"] is True

    def test_readonly_variable_not_editable(self):
        intent = TaskIntent(goal="测试")
        kernel = TaskVMKernel("s_ro", intent)
        kernel.init_task_state([
            TaskVariable(semantic_key="ro", label="只读",
                         observed="x", desired="x", mutability="readonly"),
        ])
        vs = variables_view(kernel)
        assert vs[0]["editable"] is False
        assert vs[0]["mutability"] == "readonly"


# ── governance_view ──────────────────────────────────────────────────────

class TestGovernanceView:
    def test_governance_goal_shown(self):
        kernel = _make_kernel_with_sequence()
        sess = _make_session(kernel)
        view = governance_view(sess)
        assert view["goal"] == "发布产品"
        assert view["autonomy"] == "idle"
        assert view["epoch"] == 0

    def test_governance_pending_recompose(self):
        kernel = _make_kernel_with_sequence()
        sess = _make_session(kernel)
        # trigger a goal patch to set pending_recompose
        from taskvm.domain import GoalPatch, LocalPatch, VariableUpdate
        lp = LocalPatch(patch_id="lp1",
                        variable_updates=[
                            VariableUpdate(semantic_key="release_date",
                                           new_value="2026-08-20")],
                        rationale="edit")
        kernel.apply_local_patch(lp)
        view = governance_view(sess)
        assert view["autonomy"] == "idle"

    def test_governance_model_calls_from_probe(self):
        kernel = _make_kernel_with_sequence()
        calls = [0]
        def probe():
            calls[0] += 1
            return calls[0]
        sess = ProjectionSession(
            sid="s1", kernel=kernel, model_call_probe=probe)
        view = governance_view(sess)
        assert view["model_calls"] == 1
        view2 = governance_view(sess)
        assert view2["model_calls"] == 2


# ── checkpoint_view ───────────────────────────────────────────────────────

class TestCheckpointView:
    def test_empty_checkpoints(self):
        kernel = _make_kernel_with_sequence()
        view = checkpoint_view(kernel.checkpoints())
        assert view == []

    def test_checkpoint_after_commit(self):
        kernel = _make_kernel_with_sequence()
        kernel.commit_checkpoint("cp1", "发布前检查点")
        view = checkpoint_view(kernel.checkpoints())
        assert len(view) == 1
        assert view[0]["checkpoint_id"] == "ckpt:cp1"
        assert view[0]["label"] == "发布前检查点"
        assert view[0]["rollback_available"] is True


# ── surface_cards ─────────────────────────────────────────────────────────

class TestSurfaceCards:
    def test_declared_surfaces_appear(self):
        kernel = _make_kernel_with_sequence()
        sess = _make_session(kernel, surfaces=[
            SurfaceDecl(surface_id="surf1", display_name="X平台"),
            SurfaceDecl(surface_id="surf2", display_name="微信"),
        ])
        cards = surface_cards(sess)
        assert len(cards) == 2
        assert cards[0]["display_name"] == "X平台"
        assert cards[1]["display_name"] == "微信"

    def test_surface_card_with_artifact(self):
        kernel = _make_kernel_with_sequence()
        art = ArtifactStore()
        art.put("ref1", b"fake-png-data")
        sess = ProjectionSession(
            sid="s1", kernel=kernel,
            surfaces=[SurfaceDecl(surface_id="surf1", display_name="X")],
            artifacts=art)
        # pass a runtime event to associate artifact with surface
        class FakeEvent:
            surface_id = "surf1"
            epoch = 3
            artifact_ref = "ref1"
            node_id = "n1"
            kind = type("K", (), {"value": "action_landed"})()
            detail = ""
            payload = {}
        cards = surface_cards(sess, [FakeEvent()])
        assert cards[0]["latest_artifact_ref"] == "ref1"
        assert "ref1" in cards[0]["artifact_refs"]

    def test_surface_card_missing_artifact(self):
        kernel = _make_kernel_with_sequence()
        sess = _make_session(kernel, surfaces=[
            SurfaceDecl(surface_id="surf1", display_name="X"),
        ])
        cards = surface_cards(sess)
        assert cards[0]["latest_artifact_ref"] is None
        assert cards[0]["artifact_refs"] == []

    def test_surface_card_from_runtime_event(self):
        """A runtime event with artifact_ref populates the card."""
        kernel = _make_kernel_with_sequence()
        art = ArtifactStore()
        art.put("ref_rt", b"runtime-screenshot")
        sess = ProjectionSession(
            sid="s1", kernel=kernel,
            surfaces=[SurfaceDecl(surface_id="surf1", display_name="X")],
            artifacts=art)

        class FakeEvent:
            surface_id = "surf1"
            epoch = 3
            artifact_ref = "ref_rt"
            node_id = "n1"
            kind = type("K", (), {"value": "action_landed"})()
            detail = ""
            payload = {}

        cards = surface_cards(sess, [FakeEvent()])
        assert cards[0]["status"] == "executing"
        assert cards[0]["current_goal"] == "n1"
        assert "ref_rt" in cards[0]["artifact_refs"]


# ── conflicts_view ────────────────────────────────────────────────────────

class TestConflictsView:
    def test_no_conflicts(self):
        kernel = _make_kernel_with_sequence()
        assert conflicts_view(kernel) == []

    def test_conflict_detected_and_resolved(self):
        kernel = _make_kernel_with_sequence()
        kernel.record_conflict("desc", semantic_keys=["release_date"],
                              correlation_id="c1")
        kernel.resolve_conflict("keep_world", correlation_id="c1")
        view = conflicts_view(kernel)
        assert len(view) == 0  # resolved conflict is not open

    def test_conflict_detected_unresolved(self):
        kernel = _make_kernel_with_sequence()
        kernel.record_conflict("desc", semantic_keys=["release_date"],
                              correlation_id="c1")
        view = conflicts_view(kernel)
        assert len(view) == 1
        assert view[0]["conflict_id"] == "c1"
        assert view[0]["resolved"] is False


# ── snapshot_view (full bundle) ──────────────────────────────────────────

class TestSnapshotView:
    def test_snapshot_has_all_sections(self):
        kernel = _make_kernel_with_sequence()
        sess = _make_session(kernel)
        snap = snapshot_view(sess)
        for key in ("sid", "governance", "variables", "projection_schema",
                    "projection_data", "workflow", "checkpoints",
                    "surfaces", "conflicts", "revisions"):
            assert key in snap, f"missing key: {key}"
        assert snap["sid"] == "s1"

    def test_snapshot_revisions(self):
        kernel = _make_kernel_with_sequence()
        sess = _make_session(kernel)
        snap = snapshot_view(sess)
        assert snap["revisions"]["events"] == len(kernel.events())
        assert snap["revisions"]["state"] == kernel.task_state().revision


# ── projection_schema_view / projection_data_view ─────────────────────────

class TestProjectionSchemaData:
    def test_schema_none_if_no_projection(self):
        kernel = _make_kernel_with_sequence()
        view = projection_schema_view(kernel)
        # kernel without projection schema → None
        assert view is None or "components" in view

    def test_data_view_has_revision(self):
        kernel = _make_kernel_with_sequence()
        view = projection_data_view(kernel)
        assert "revision" in view
        assert "progress" in view
        assert "values" in view
        assert "node_status" in view
