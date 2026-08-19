"""Shared fixtures for the GenUI production-layer tests.

The snapshot below mirrors the projection layer's public view shapes
(``taskvm/projection/view_models.py`` builders) — including the internal
fields (sid / epoch / node_id / checkpoint_id) that the context builder
MUST strip before anything reaches a model.
"""
from __future__ import annotations

import pytest

from taskvm.genui.context import TaskSurfaceContextBuilder
from taskvm.genui.data_model import TaskDataModelProjector

SNAPSHOT = {
    "sid": "s-internal-001",
    "governance": {
        "goal": "把发布会日期改到 8 月底并通知所有参会人",
        "constraints": ["不改预算"],
        "scope": [],
        "success_criteria": [],
        "autonomy": "ready",
        "epoch": 7,
        "pending_recompose": None,
        "model_calls": None,
    },
    "variables": [
        {"key": "release_date", "label": "发布日期",
         "observed": "2026-08-01", "desired": "2026-08-30",
         "diverged": True, "mutability": "editable", "editable": True,
         "confidence": 0.95},
        {"key": "notify_list", "label": "通知名单",
         "observed": "3 人", "desired": "5 人",
         "diverged": True, "mutability": "readonly", "editable": False,
         "confidence": 1.0},
        {"key": "budget", "label": "预算",
         "observed": 2000, "desired": 2000,
         "diverged": False, "mutability": "editable", "editable": True,
         "confidence": 1.0},
    ],
    "projection_schema": None,
    "projection_data": {"revision": 4, "progress": 0.5,
                        "values": {}, "node_status": {}},
    "workflow": {
        "has_plan": True,
        "nodes": [
            {"node_id": "n-01", "kind": "action", "kind_label": "step",
             "label": "修改发布日期", "status": "committed",
             "status_label": "verified", "depth": 1, "parent_id": "plan",
             "depends_on": [], "is_checkpoint": False,
             "rollback_boundary": False},
            {"node_id": "n-02", "kind": "checkpoint",
             "kind_label": "checkpoint", "label": "日期确认点",
             "status": "committed", "status_label": "verified", "depth": 1,
             "parent_id": "plan", "depends_on": ["n-01"],
             "is_checkpoint": True, "rollback_boundary": True},
            {"node_id": "n-03", "kind": "verify", "kind_label": "verification",
             "label": "校验通知名单", "status": "pending",
             "status_label": "waiting", "depth": 1, "parent_id": "plan",
             "depends_on": ["n-02"], "is_checkpoint": False,
             "rollback_boundary": False},
        ],
        "progress": {"committed": 2, "total": 3},
    },
    "checkpoints": [
        {"checkpoint_id": "cp:00001", "label": "日期确认点",
         "state_revision": 3, "event_index": 12, "epoch": 5,
         "committed_nodes": 2, "created_at": "2026-08-19T10:00:00Z",
         "rollback_available": True},
    ],
    "surfaces": [
        {"surface_id": "app-calendar", "display_name": "日历",
         "current_goal": "", "last_observed_at": None,
         "latest_artifact_ref": None, "artifact_refs": [],
         "status": "unknown", "recent_actions": []},
    ],
    "conflicts": [
        {"conflict_id": "conflict:00002",
         "description": "通知名单人数与预算档位不符",
         "semantic_keys": ["notify_list"], "epoch": 6, "resolved": False},
    ],
    "revisions": {"state": 3, "schema": 1, "data": 4, "events": 20},
}


@pytest.fixture
def snapshot() -> dict:
    import copy
    return copy.deepcopy(SNAPSHOT)


@pytest.fixture
def context(snapshot):
    return TaskSurfaceContextBuilder().build(snapshot)


@pytest.fixture
def data_model(context):
    return TaskDataModelProjector().project(context)


@pytest.fixture
def valid_components() -> list[dict]:
    """A schema-conformant, policy-clean tree over the fixture variables
    (uses the CORRECT v0.9 binding syntax: value + {"path": ...})."""
    return [
        {"id": "root", "component": "Column",
         "children": ["title", "date_field", "budget_field",
                      "status_text", "submit", "submit_label"]},
        {"id": "title", "component": "Text", "text": "任务变量",
         "variant": "h2"},
        {"id": "date_field", "component": "TextField", "label": "发布日期",
         "value": {"path": "/variables/release_date/desired"}},
        {"id": "budget_field", "component": "TextField", "label": "预算",
         "value": {"path": "/variables/budget/desired"},
         "variant": "number"},
        {"id": "status_text", "component": "Text",
         "text": {"path": "/task/status"}},
        {"id": "submit", "component": "Button", "child": "submit_label",
         "variant": "primary",
         "action": {"event": {"name": "taskvm.local_patch",
                              "context": {"semanticKey": "release_date"}}}},
        {"id": "submit_label", "component": "Text", "text": "更新日期"},
    ]
