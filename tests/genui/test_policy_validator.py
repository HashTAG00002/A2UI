"""policy + validator — every violation path must be rejected, and the
two-layer gate must accept a clean tree. Workplan §15 GenUI unit list:
valid accepted / malformed rejected / unknown component rejected /
unknown action rejected / readonly input rejected / unknown semantic key
rejected / excessive tree rejected."""
from __future__ import annotations

import copy

import pytest

from taskvm.genui import protocol
from taskvm.genui.policy import (
    MAX_COMPONENTS, MAX_TREE_DEPTH, SurfacePolicy,
)
from taskvm.genui.validator import (
    ComponentValidationError, validate_components,
)


def _replace(components, target_id, **overrides):
    out = []
    for c in components:
        c = copy.deepcopy(c)
        if c.get("id") == target_id:
            c.update(overrides)
        out.append(c)
    return out


# ── acceptance ─────────────────────────────────────────────────────────────

def test_valid_tree_passes_both_layers(context, data_model, valid_components):
    errors = validate_components(valid_components, context, data_model)
    assert errors == [], errors


def test_validate_components_defaults_data_model(context, valid_components):
    assert validate_components(valid_components, context) == []


def test_strict_mode_raises_with_both_error_lists(context, valid_components):
    bad = _replace(valid_components, "submit",
                   action={"event": {"name": "pause", "context": {}}})
    with pytest.raises(ComponentValidationError) as ei:
        validate_components(bad, context, strict=True)
    assert ei.value.policy_errors
    assert "governance" in " ".join(ei.value.policy_errors)


# ── layer 1: protocol/catalog schema ───────────────────────────────────────

def test_unknown_component_rejected_by_protocol_layer(context, valid_components):
    bad = _replace(valid_components, "title",
                   component="NotAComponent", text="x")
    errors = validate_components(bad, context)
    assert any("NotAComponent" in e for e in errors)


def test_legacy_v08_data_binding_rejected(context, valid_components):
    """`dataBinding` is v0.8 syntax; v0.9 binds via value/{"path": ...}."""
    bad = _replace(valid_components, "date_field",
                   value=None, dataBinding="/variables/release_date/desired")
    errors = validate_components(bad, context)
    assert errors


def test_missing_required_prop_rejected(context, valid_components):
    bad = _replace(valid_components, "title", text=None)
    errors = validate_components(bad, context)
    assert errors


# ── layer 2: bindings ──────────────────────────────────────────────────────

def test_input_bound_to_readonly_variable_rejected(context, valid_components):
    bad = _replace(valid_components, "date_field", label="通知名单",
                   value={"path": "/variables/notify_list/desired"})
    errors = validate_components(bad, context)
    assert any("not editable" in e for e in errors)


def test_input_bound_to_observed_plane_rejected(context, valid_components):
    bad = _replace(valid_components, "date_field",
                   value={"path": "/variables/release_date/observed"})
    errors = validate_components(bad, context)
    assert any("may only bind /variables/<key>/desired" in e for e in errors)


def test_unknown_binding_path_rejected(context, valid_components):
    bad = _replace(valid_components, "status_text",
                   text={"path": "/variables/ghost_key/desired"})
    errors = validate_components(bad, context)
    assert any("not a whitelisted path" in e for e in errors)


def test_positional_workflow_binding_rejected(context, valid_components):
    bad = _replace(valid_components, "status_text",
                   text={"path": "/workflow/nodes/0/label"})
    errors = validate_components(bad, context)
    assert any("not a whitelisted path" in e for e in errors)


def test_input_label_may_bind_label_plane(context, valid_components):
    """A4 refinement: only the WRITE channel (``value``) of an input is
    restricted to editable ``/desired`` planes — the label/display
    channel may bind any whitelisted path (an input's label legitimately
    shows the variable's label plane, keeping label changes at 0 GenUI
    calls)."""
    good = _replace(valid_components, "date_field",
                    label={"path": "/variables/release_date/label"})
    assert validate_components(good, context) == []


def test_input_value_channel_still_restricted(context, valid_components):
    """The write-channel restriction itself is unchanged: value bound to
    the observed plane (or a readonly variable's desired) is rejected."""
    bad = _replace(valid_components, "date_field",
                   value={"path": "/variables/release_date/observed"})
    assert any("may only bind /variables/<key>/desired" in e
               for e in validate_components(bad, context))


def test_display_text_may_bind_observed_plane(context, valid_components):
    """Read-only display is allowed to bind observed (only INPUTS are
    restricted to editable desired planes)."""
    good = _replace(valid_components, "status_text",
                    text={"path": "/variables/notify_list/observed"})
    assert validate_components(good, context) == []


# ── layer 2: actions ───────────────────────────────────────────────────────

def test_unknown_action_rejected(context, valid_components):
    bad = _replace(valid_components, "submit",
                   action={"event": {"name": "taskvm.magic", "context": {}}})
    errors = validate_components(bad, context)
    assert any("allowlist" in e for e in errors)


def test_governance_action_rejected_explicitly(context, valid_components):
    for gov in ("pause", "rollback", "goal_patch"):
        bad = _replace(valid_components, "submit",
                       action={"event": {"name": gov, "context": {}}})
        errors = validate_components(bad, context)
        assert any("governance" in e for e in errors), gov


def test_action_unknown_semantic_key_rejected(context, valid_components):
    bad = _replace(valid_components, "submit",
                   action={"event": {"name": "taskvm.local_patch",
                                     "context": {"semanticKey": "ghost"}}})
    errors = validate_components(bad, context)
    assert any("unknown semantic key" in e for e in errors)


def test_action_readonly_semantic_key_rejected(context, valid_components):
    bad = _replace(valid_components, "submit",
                   action={"event": {"name": "taskvm.local_patch",
                                     "context": {"semanticKey": "notify_list"}}})
    errors = validate_components(bad, context)
    assert any("not editable" in e for e in errors)


def test_action_missing_semantic_key_rejected(context, valid_components):
    bad = _replace(valid_components, "submit",
                   action={"event": {"name": "taskvm.local_patch",
                                     "context": {}}})
    errors = validate_components(bad, context)
    assert any("semanticKey" in e for e in errors)


# ── layer 2: structure + limits ────────────────────────────────────────────

def test_missing_root_rejected(context, valid_components):
    bad = _replace(valid_components, "root", id="not-root")
    errors = validate_components(bad, context)
    assert any("root" in e for e in errors)


def test_root_referenced_as_child_rejected(context, valid_components):
    bad = _replace(valid_components, "root",
                   children=["title", "date_field", "budget_field",
                             "status_text", "submit", "submit_label",
                             "root"])
    errors = validate_components(bad, context)
    assert any("root must not be referenced" in e for e in errors)


def test_orphan_component_rejected(context, valid_components):
    bad = valid_components + [
        {"id": "lonely", "component": "Text", "text": "无处安放"}]
    errors = validate_components(bad, context)
    assert any("unreachable" in e for e in errors)


def test_unknown_child_rejected(context, valid_components):
    bad = _replace(valid_components, "root", children=["title", "ghost"])
    errors = validate_components(bad, context)
    assert any("unknown child" in e for e in errors)


def test_duplicate_ids_rejected(context, valid_components):
    bad = valid_components + [
        {"id": "title", "component": "Text", "text": "重复"}]
    errors = validate_components(bad, context)
    assert any("duplicate" in e for e in errors)


def test_cycle_is_rejected(context, valid_components):
    """a→b, b→a forms a cycle that can never hang off the root tree; the
    structural checks must flag it (multiple parents / orphans)."""
    bad = [
        {"id": "root", "component": "Column", "children": ["a"]},
        {"id": "a", "component": "Column", "children": ["b"]},
        {"id": "b", "component": "Column", "children": ["a"]},
        {"id": "t", "component": "Text", "text": "x"},
    ]
    errors = SurfacePolicy(context, {}).check_components(bad)
    assert any("multiple parents" in e or "unreachable" in e for e in errors)


def test_template_children_rejected(context, valid_components):
    bad = _replace(valid_components, "root",
                   children={"template": "row-for-each"})
    errors = SurfacePolicy(context, {}).check_components(bad)
    assert any("plain array" in e for e in errors)


def test_component_count_limit(context, valid_components):
    """81 components (root + 80 leaves) exceeds the 80 cap."""
    comps = [{"id": "root", "component": "Column",
              "children": [f"t{i}" for i in range(MAX_COMPONENTS)]}]
    comps += [{"id": f"t{i}", "component": "Text", "text": "x"}
              for i in range(MAX_COMPONENTS)]
    errors = SurfacePolicy(context, {}).check_components(comps)
    assert any("exceeds the limit" in e for e in errors)


def test_tree_depth_limit(context):
    """root(d1)→c1(d2)→…→c8(d9): depth 9 breaks the ≤8 cap, with no
    dangling refs muddying the assertion."""
    comps = [{"id": "root", "component": "Column", "children": ["c1"]}]
    for i in range(1, MAX_TREE_DEPTH + 1):
        comps.append({"id": f"c{i}", "component": "Column",
                      "children": [f"c{i+1}"]})
    comps.append({"id": f"c{MAX_TREE_DEPTH + 1}", "component": "Column",
                  "children": []})
    errors = SurfacePolicy(context, {}).check_components(comps)
    assert any("depth exceeds" in e for e in errors)
    assert not any("unknown child" in e for e in errors)


def test_within_limits_accepted(context):
    comps = [{"id": "root", "component": "Column", "children": ["c1"]}]
    comps.append({"id": "c1", "component": "Column", "children": []})
    assert SurfacePolicy(context, {}).check_components(comps) == []


# ── layer 2: content safety ────────────────────────────────────────────────

def test_absolute_url_rejected(context, valid_components):
    bad = _replace(valid_components, "title",
                   component="Image", text=None,
                   url="https://evil.example.com/pixel.png")
    errors = validate_components(bad, context)
    assert any("URL" in e for e in errors)


def test_protocol_relative_url_rejected(context, valid_components):
    bad = _replace(valid_components, "title",
                   component="Image", text=None, url="//cdn.example.com/a.png")
    errors = validate_components(bad, context)
    assert any("URL" in e for e in errors)


def test_script_like_text_rejected(context, valid_components):
    bad = _replace(valid_components, "title", text="<script>alert(1)</script>")
    errors = validate_components(bad, context)
    assert any("script-like" in e for e in errors)


def test_javascript_url_rejected(context, valid_components):
    bad = _replace(valid_components, "title",
                   component="Image", text=None, url="javascript:alert(1)")
    errors = validate_components(bad, context)
    assert any("URL" in e for e in errors)


def test_oversized_text_rejected(context, valid_components):
    bad = _replace(valid_components, "title", text="长" * 3000)
    errors = SurfacePolicy(context, {}).check_components(bad)
    assert any("exceeds" in e for e in errors)


# ── layer 2: governance namespace ──────────────────────────────────────────

def test_governance_namespace_squatting_rejected(context, valid_components):
    bad = _replace(valid_components, "title", id="governance-stop")
    bad = [c for c in bad if c["id"] != "root"] + [
        {"id": "root", "component": "Column",
         "children": ["governance-stop", "date_field", "budget_field",
                      "status_text", "submit", "submit_label"]}]
    errors = SurfacePolicy(context, {}).check_components(bad)
    assert any("reserved governance namespace" in e for e in errors)


def test_empty_components_rejected(context):
    assert SurfacePolicy(context, {}).check_components([]) != []


# ── cross-layer invariants ─────────────────────────────────────────────────

def test_validator_reports_protocol_and_policy_separately(context, valid_components):
    bad = _replace(valid_components, "title", component="NotAComponent")
    bad = _replace(bad, "date_field",
                   value={"path": "/variables/notify_list/desired"})
    with pytest.raises(ComponentValidationError) as ei:
        validate_components(bad, context, strict=True)
    assert ei.value.protocol_errors        # unknown component (layer 1)
    assert ei.value.policy_errors          # readonly binding (layer 2)
