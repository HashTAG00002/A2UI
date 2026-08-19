"""context — TaskSurfaceContextBuilder: mapping fidelity + the no-leak
contract (zero internal ids ever reach the model-facing payload)."""
from __future__ import annotations

import json

from taskvm.genui.context import TaskSurfaceContextBuilder

#: Internal-only keys that must NEVER appear in the context payload
#: (GUI-only rule: if a real user cannot see it on a rendered screen,
#: it does not go into model input).
_FORBIDDEN_KEYS = {
    "sid", "node_id", "checkpoint_id", "conflict_id", "epoch",
    "event_index", "state_revision", "parent_id", "depends_on",
    "surface_id", "rollback_available", "pending_recompose",
    "model_calls", "revision", "revisions", "created_at",
    "last_observed_at", "latest_artifact_ref", "artifact_refs",
    "recent_actions", "projection_schema", "projection_data",
}


def _all_keys(node, acc: set) -> set:
    if isinstance(node, dict):
        for k, v in node.items():
            acc.add(k)
            _all_keys(v, acc)
    elif isinstance(node, list):
        for item in node:
            _all_keys(item, acc)
    return acc


# ── field mapping ──────────────────────────────────────────────────────────

def test_goal_and_status_come_from_governance_view(context):
    assert context.goal == "把发布会日期改到 8 月底并通知所有参会人"
    assert context.task_status == "ready"


def test_variables_mapped_with_all_public_fields(context):
    by_key = {v.semantic_key: v for v in context.variables}
    assert set(by_key) == {"release_date", "notify_list", "budget"}

    date = by_key["release_date"]
    assert date.display_label == "发布日期"
    assert date.observed == "2026-08-01"
    assert date.desired == "2026-08-30"
    assert date.mutability == "editable"
    assert date.editable is True
    assert date.confidence == 0.95

    notify = by_key["notify_list"]
    assert notify.mutability == "readonly"
    assert notify.editable is False


def test_variables_sorted_like_public_view(context):
    keys = [v.semantic_key for v in context.variables]
    assert keys == sorted(keys)


def test_workflow_nodes_carry_labels_and_statuses_only(context):
    payload = context.workflow.to_payload()
    assert payload["has_plan"] is True
    labels = [n["label"] for n in payload["nodes"]]
    assert "修改发布日期" in labels
    kinds = {n["kind"] for n in payload["nodes"]}
    assert "step" in kinds and "checkpoint" in kinds
    statuses = {n["status"] for n in payload["nodes"]}
    assert "verified" in statuses and "waiting" in statuses


def test_checkpoints_and_conflicts_reduced_to_visible_fields(context):
    assert context.checkpoints[0].label == "日期确认点"
    assert context.checkpoints[0].committed_nodes == 2
    assert context.conflicts[0].description == "通知名单人数与预算档位不符"
    assert context.conflicts[0].semantic_keys == ["notify_list"]


def test_allowed_actions_injected_from_protocol(context):
    assert context.allowed_surface_actions == ["taskvm.local_patch"]


# ── the no-leak contract ───────────────────────────────────────────────────

def test_payload_contains_zero_internal_keys(context):
    payload = context.to_payload()
    keys = _all_keys(payload, set())
    leaked = keys & _FORBIDDEN_KEYS
    assert not leaked, f"internal keys leaked into model context: {leaked}"


def test_payload_json_contains_no_internal_id_values(snapshot, context):
    """Even as STRING VALUES the internal ids must not survive (an id
    smuggled into a label would still be a leak)."""
    blob = json.dumps(context.to_payload(), ensure_ascii=False)
    for needle in ("s-internal-001", "n-01", "n-02", "n-03", "cp:00001",
                   "conflict:00002", "app-calendar"):
        assert needle not in blob, f"internal id {needle!r} leaked as a value"


def test_missing_node_labels_never_fall_back_to_internal_ids(snapshot):
    """P2 (GUI-only): a workflow node without a user-visible label/kind/
    status must degrade to \"\" — never to the compiler's internal
    node_id / kind / status vocabulary."""
    for n in snapshot["workflow"]["nodes"]:
        n.pop("label", None)
        n.pop("kind_label", None)
        n.pop("status_label", None)
    ctx = TaskSurfaceContextBuilder().build(snapshot)
    assert all(node.label == "" for node in ctx.workflow.nodes)
    blob = json.dumps(ctx.to_payload(), ensure_ascii=False)
    for needle in ("n-01", "n-02", "n-03"):
        assert needle not in blob, f"internal node id {needle!r} leaked"
    # the raw kernel enums (kind/status) must not survive either
    kinds = {node.kind for node in ctx.workflow.nodes}
    statuses = {node.status for node in ctx.workflow.nodes}
    assert kinds == {""} and statuses == {""}


# ── purity ─────────────────────────────────────────────────────────────────

def test_builder_is_pure(snapshot):
    a = TaskSurfaceContextBuilder().build(snapshot).to_payload()
    b = TaskSurfaceContextBuilder().build(snapshot).to_payload()
    assert a == b
    # and building does not mutate the input snapshot
    assert snapshot["variables"][0]["key"] == "release_date"


def test_source_labels_optional_injection(snapshot):
    builder = TaskSurfaceContextBuilder()
    ctx = builder.build(snapshot, source_labels={"release_date": "日历"})
    var = ctx.variable("release_date")
    assert var.visible_source_label == "日历"
    assert var.to_payload()["visible_source_label"] == "日历"
    # absent labels stay absent
    assert "visible_source_label" not in ctx.variable("budget").to_payload()


def test_empty_snapshot_degrades_honestly():
    ctx = TaskSurfaceContextBuilder().build({})
    payload = ctx.to_payload()
    assert payload["goal"] == ""
    assert payload["task_status"] == "unknown"
    assert payload["variables"] == []
    assert payload["workflow"] == {"has_plan": False, "nodes": []}
