"""data_model — TaskDataModelProjector purity + whitelist derivation."""
from __future__ import annotations

import copy

from taskvm.genui.context import TaskSurfaceContextBuilder
from taskvm.genui.data_model import (
    STATUS_DIVERGED, STATUS_PENDING, STATUS_SYNCED, TaskDataModelProjector,
    binding_path_whitelist, variable_status,
)


def test_projector_is_pure(context):
    p = TaskDataModelProjector()
    a = p.project(context)
    b = p.project(context)
    assert a == b and a is not b


def test_same_snapshot_same_data_model(snapshot):
    """The DoD: 同一 snapshot 输入必得同一 data model (deep equality,
    including key order stability for the JSON wire form)."""
    import json
    ctx1 = TaskSurfaceContextBuilder().build(snapshot)
    ctx2 = TaskSurfaceContextBuilder().build(copy.deepcopy(snapshot))
    dm1 = TaskDataModelProjector().project(ctx1)
    dm2 = TaskDataModelProjector().project(ctx2)
    assert json.dumps(dm1, ensure_ascii=False, sort_keys=False) == \
        json.dumps(dm2, ensure_ascii=False, sort_keys=False)


def test_shape_contains_task_variables_workflow_checkpoints_conflicts(data_model):
    assert set(data_model) == {"task", "variables", "workflow",
                               "checkpoints", "conflicts"}
    assert data_model["task"] == {"goal": "把发布会日期改到 8 月底并通知所有参会人",
                                  "status": "ready"}
    assert set(data_model["variables"]) == {"release_date", "notify_list",
                                            "budget"}


def test_variable_entry_fields(data_model):
    entry = data_model["variables"]["release_date"]
    assert entry["label"] == "发布日期"
    assert entry["observed"] == "2026-08-01"
    assert entry["desired"] == "2026-08-30"
    assert entry["mutability"] == "editable"
    assert entry["status"] == STATUS_DIVERGED
    assert entry["confidence"] == 0.95


def test_variable_status_vocabulary():
    assert variable_status("a", "a") == STATUS_SYNCED
    assert variable_status("a", "b") == STATUS_DIVERGED
    assert variable_status(None, None) == STATUS_PENDING
    assert variable_status(None, "b") == STATUS_DIVERGED


def test_observed_change_only_touches_data_model(snapshot):
    """An ordinary observation lands → only the data model moves; the
    variable SET (structure) is untouched (0 structural changes)."""
    projector = TaskDataModelProjector()
    ctx = TaskSurfaceContextBuilder().build(snapshot)
    before = projector.project(ctx)
    assert before["variables"]["release_date"]["status"] == STATUS_DIVERGED

    # reality confirms the desired date (ordinary CUA-landed update)
    for v in snapshot["variables"]:
        if v["key"] == "release_date":
            v["observed"] = "2026-08-30"
            v["diverged"] = False
    after = projector.project(TaskSurfaceContextBuilder().build(snapshot))

    assert after["variables"]["release_date"]["status"] == STATUS_SYNCED
    assert set(after["variables"]) == set(before["variables"])  # structure stable
    assert after["task"] == before["task"]                      # goal untouched


# ── binding-path whitelist ─────────────────────────────────────────────────

def test_whitelist_contains_variable_planes_and_task_fields(data_model):
    wl = binding_path_whitelist(data_model)
    for plane in ("label", "observed", "desired", "mutability", "status",
                  "confidence", "value_type"):
        assert f"/variables/release_date/{plane}" in wl
    assert "/task/goal" in wl
    assert "/task/status" in wl


def test_whitelist_excludes_list_positions(data_model):
    wl = binding_path_whitelist(data_model)
    assert "/workflow/nodes/0/label" not in wl
    assert "/checkpoints/0/label" not in wl
    assert "/conflicts/0/description" not in wl


def test_whitelist_includes_none_leaves(data_model):
    """A variable whose desired is still None must remain bindable (the
    user can fill an empty desired plane)."""
    wl = binding_path_whitelist(data_model)
    assert "/variables/release_date/desired" in wl


def test_whitelist_for_empty_model():
    assert binding_path_whitelist({}) == set()
