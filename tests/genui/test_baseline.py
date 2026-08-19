"""baseline — the generic fallback surface: purity, two-layer validation,
zero literal facts, per-type inputs, and honest degradation tiers."""
from __future__ import annotations

import json
import re

from taskvm.genui.baseline import baseline_components
from taskvm.genui.context import (
    SurfaceVariable, TaskSurfaceContext, WorkflowView,
)
from taskvm.genui.policy import MAX_COMPONENTS
from taskvm.genui.validator import validate_components

_ID_CLEAN_RE = re.compile(r"[^a-z0-9-]+")


def _var(key: str, *, label=None, vt="string", observed=None, desired=None,
         mutability="editable") -> SurfaceVariable:
    return SurfaceVariable(
        semantic_key=key, display_label=label or key, value_type=vt,
        observed=observed, desired=desired, mutability=mutability,
        confidence=1.0, visible_source_label=None)


def _context(variables, goal="示例目标") -> TaskSurfaceContext:
    return TaskSurfaceContext(
        goal=goal, task_status="ready", variables=variables,
        workflow=WorkflowView(has_plan=False, nodes=[]),
        checkpoints=[], conflicts=[],
        allowed_surface_actions=["taskvm.local_patch"])


# ── the hard gate: the fallback itself must pass both layers ──────────────

def test_baseline_passes_two_layer_validation(context):
    components = baseline_components(context)
    errors = validate_components(components, context)
    assert errors == [], errors


def test_baseline_is_sdk_protocol_conformant(context):
    from taskvm.genui import schema
    from taskvm.genui.protocol import update_components_message
    components = baseline_components(context)
    msgs = [update_components_message("taskvm-task-t", components)]
    assert schema.validate_protocol_messages(msgs) == []


def test_empty_variable_context_still_valid():
    ctx = _context([])
    components = baseline_components(ctx)
    assert validate_components(components, ctx) == []


def test_single_component_each_type_input_valid():
    """boolean / date / number / string each map to the right input and
    the whole tree still passes both layers."""
    ctx = _context([
        _var("mute_alarm", vt="boolean", observed=False, desired=True),
        _var("meeting_time", vt="date", observed="2026-08-20T09:00",
             desired="2026-08-21T10:00"),
        _var("volume", vt="number", observed=5, desired=8),
        _var("note_text", vt="string", observed="旧", desired="新"),
        _var("readonly_label", vt="string", observed="只读值",
             mutability="readonly"),
    ])
    components = baseline_components(ctx)
    assert validate_components(components, ctx) == []
    by_type = {c["component"] for c in components}
    assert {"CheckBox", "DateTimeInput", "TextField", "Button"} <= by_type
    # the number variable gets variant=number, not shortText
    num_field = next(c for c in components
                     if c.get("id") == "var-volume-input")
    assert num_field["variant"] == "number"


# ── purity / determinism ───────────────────────────────────────────────────

def test_baseline_is_pure_and_fresh(context):
    a = baseline_components(context)
    b = baseline_components(context)
    assert a == b
    assert a is not b
    a[0]["children"].append("mutated")
    assert "mutated" not in baseline_components(context)[0]["children"]


# ── structure-only: zero literal facts ─────────────────────────────────────

def test_zero_literal_task_values(context):
    """Goal/labels/observed/desired must be bindings, not copies of the
    context's facts (same invariant the decoder model must obey)."""
    blob = json.dumps(baseline_components(context), ensure_ascii=False)
    for fact in ("把发布会日期改到 8 月底并通知所有参会人",
                 "发布日期", "通知名单", "预算",
                 "2026-08-01", "2026-08-30", "3 人", "5 人"):
        assert fact not in blob, f"literal fact {fact!r} copied into tree"
    # budget's numeric value must not appear as a literal either
    assert '"text": 2000' not in blob and ': 2000' not in blob


def test_root_id_present_and_unique(context):
    components = baseline_components(context)
    roots = [c for c in components if c["id"] == "root"]
    assert len(roots) == 1
    assert roots[0]["component"] == "Column"


def test_every_variable_reachable(context):
    components = baseline_components(context)
    root_children = set(components[0]["children"])
    for v in context.variables:
        # every variable contributes a row (or label) among root children
        # (ids are sanitised: [a-z0-9-] only)
        safe = _ID_CLEAN_RE.sub("-", v.semantic_key.strip().lower()).strip("-")
        assert any(cid.startswith(f"var-{safe}") for cid in root_children), \
            v.semantic_key


def test_editable_gets_input_and_apply_readonly_does_not(context):
    components = baseline_components(context)
    by_id = {c["id"]: c for c in components}
    # editable release_date: TextField + Button(taskvm.local_patch)
    assert by_id["var-release-date-input"]["component"] == "TextField"
    apply_btn = by_id["var-release-date-apply"]
    assert apply_btn["component"] == "Button"
    assert apply_btn["action"]["event"]["name"] == "taskvm.local_patch"
    assert apply_btn["action"]["event"]["context"] == {
        "semanticKey": "release_date"}
    # readonly notify_list: text pair only, no input, no button
    assert by_id["var-notify-list-label"]["component"] == "Text"
    assert "var-notify-list-input" not in by_id
    assert "var-notify-list-apply" not in by_id


def test_binding_paths_address_whitelisted_planes(context):
    components = baseline_components(context)
    paths = set()
    for c in components:
        for value in c.values():
            if isinstance(value, dict) and set(value) == {"path"}:
                paths.add(value["path"])
    assert "/task/goal" in paths and "/task/status" in paths
    assert "/variables/release_date/desired" in paths
    assert "/variables/notify_list/observed" in paths
    # inputs only ever bind the desired plane of editable variables
    for c in components:
        if c.get("id", "").endswith("-input"):
            assert c["value"]["path"].endswith("/desired")


def test_id_sanitisation_is_collision_free():
    ctx = _context([
        _var("a b"), _var("a-b"), _var("A_B"),
    ])
    components = baseline_components(ctx)
    ids = [c["id"] for c in components]
    assert len(ids) == len(set(ids))
    assert validate_components(components, ctx) == []


# ── honest degradation tiers under the 80-component budget ────────────────

def test_full_tier_under_normal_load(context):
    assert len(baseline_components(context)) <= MAX_COMPONENTS


def test_input_tier_when_buttons_would_overflow():
    """20 editable vars: full = 4+100 > 80 → degrade to input-only rows
    (4 + 40 = 44), every variable still present with a live input."""
    ctx = _context([_var(f"v{i:02d}") for i in range(20)])
    components = baseline_components(ctx)
    assert len(components) <= MAX_COMPONENTS
    assert validate_components(components, ctx) == []
    inputs = [c for c in components if c.get("id", "").endswith("-input")]
    assert len(inputs) == 20
    # degraded tier drops the apply buttons, not the variables
    assert not any(c.get("component") == "Button" for c in components)


def test_label_tier_when_inputs_would_overflow():
    """40 readonly vars: 4+120 > 80 even input-tier → one label Text per
    variable directly under root (44 total)."""
    ctx = _context([_var(f"v{i:02d}", mutability="readonly")
                    for i in range(40)])
    components = baseline_components(ctx)
    assert len(components) == 1 + 3 + 40          # root + fixed + labels
    assert validate_components(components, ctx) == []
    labels = [c for c in components if c.get("id", "").endswith("-label")]
    assert len(labels) == 40
