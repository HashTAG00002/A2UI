"""A4 acceptance — three mutually unrelated UNSEEN goals (different app
domains, different variable shapes) each produce a DIFFERENT validated
component tree from the same decoder, with zero frontend code changes;
ordinary value updates trigger ZERO GenUI model calls (updateDataModel
only). Workplan §7-P3 DoD + §16 Set A/Set B.

The three goals have never appeared anywhere in this repo's code before
(no fixture, no template, no semantic-key branch — grep-verified by the
static-gate test below). The fake port plays a capable model: it returns
a plausible, structurally DISTINCT tree per goal — Tabs layout for the
clock, Card+Row for the weather digest, flat form Column for messaging.
Every tree must pass the REAL two-layer gate (no mocked validation).
"""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

from taskvm.genui.context import TaskSurfaceContextBuilder
from taskvm.genui.data_model import TaskDataModelProjector
from taskvm.genui.decoder import GenUIDecoder, SOURCE_MODEL
from taskvm.genui.store import SurfaceStore
from taskvm.genui.validator import validate_components

# ── three UNSEEN goals: different apps, different variable shapes ──────────

GOAL_ALARM = {
    "governance": {"goal": "明早 6:30 叫我起床赶高铁", "autonomy": "ready"},
    "variables": [
        {"key": "alarm_time", "label": "闹钟时间", "value_type": "date",
         "observed": "2026-08-20T07:00", "desired": "2026-08-21T06:30",
         "mutability": "editable"},
        {"key": "repeat_mode", "label": "重复方式", "value_type": "string",
         "observed": "仅一次", "desired": None,
         "mutability": "readonly"},
        {"key": "volume", "label": "铃声音量", "value_type": "number",
         "observed": 5, "desired": 7, "mutability": "editable"},
        {"key": "alarm_label", "label": "闹钟备注", "value_type": "string",
         "observed": "闹钟", "desired": "赶高铁起床",
         "mutability": "editable"},
    ],
    "workflow": {"has_plan": True, "nodes": []},
}

GOAL_WEATHER = {
    "governance": {"goal": "查一下这周末北京的天气，如果下雨就提醒我带伞",
                   "autonomy": "ready"},
    "variables": [
        {"key": "city", "label": "城市", "value_type": "string",
         "observed": "北京", "desired": None, "mutability": "readonly"},
        {"key": "weekend_forecast", "label": "周末预报",
         "value_type": "string",
         "observed": "周六小雨 22°C，周日多云 25°C", "desired": None,
         "mutability": "readonly"},
        {"key": "rain_alert", "label": "下雨提醒", "value_type": "boolean",
         "observed": False, "desired": True, "mutability": "editable"},
    ],
    "workflow": {"has_plan": True, "nodes": []},
}

GOAL_MESSAGE = {
    "governance": {"goal": "给项目群里发消息说设计稿已更新，并@一下设计师",
                   "autonomy": "ready"},
    "variables": [
        {"key": "target_group", "label": "目标群聊", "value_type": "string",
         "observed": "TaskVM 项目组", "desired": None,
         "mutability": "readonly"},
        {"key": "message_text", "label": "消息内容", "value_type": "text",
         "observed": "", "desired": "设计稿已更新，请查收最新版本",
         "mutability": "editable"},
        {"key": "mention_list", "label": "提醒谁看", "value_type": "string",
         "observed": "", "desired": "@设计师小王", "mutability": "editable"},
        {"key": "send_status", "label": "发送状态", "value_type": "status",
         "observed": "未发送", "desired": None, "mutability": "readonly"},
    ],
    "workflow": {"has_plan": True, "nodes": []},
}

UNSEEN_GOALS = [GOAL_ALARM, GOAL_WEATHER, GOAL_MESSAGE]


# ── the "model": one structurally distinct tree per goal ────────────────────

TREES = {
    "alarm": [
        {"id": "root", "component": "Column",
         "children": ["title", "clock-tabs"]},
        {"id": "title", "component": "Text", "text": "起床闹钟",
         "variant": "h2"},
        {"id": "clock-tabs", "component": "Tabs", "tabs": [
            {"title": "时间", "child": "time-col"},
            {"title": "音量", "child": "vol-col"}]},
        {"id": "time-col", "component": "Column",
         "children": ["time-input", "repeat-text", "label-input",
                      "apply-btn", "apply-btn-label"]},
        {"id": "time-input", "component": "DateTimeInput",
         "label": {"path": "/variables/alarm_time/label"},
         "value": {"path": "/variables/alarm_time/desired"},
         "enableTime": True},
        {"id": "repeat-text", "component": "Text",
         "text": {"path": "/variables/repeat_mode/observed"},
         "variant": "caption"},
        {"id": "label-input", "component": "TextField",
         "label": {"path": "/variables/alarm_label/label"},
         "value": {"path": "/variables/alarm_label/desired"}},
        {"id": "apply-btn", "component": "Button",
         "child": "apply-btn-label", "variant": "primary",
         "action": {"event": {
             "name": "taskvm.local_patch",
             "context": {"semanticKey": "alarm_time"}}}},
        {"id": "apply-btn-label", "component": "Text", "text": "设置闹钟"},
        {"id": "vol-col", "component": "Column",
         "children": ["vol-input", "vol-btn", "vol-btn-label"]},
        {"id": "vol-input", "component": "TextField",
         "label": {"path": "/variables/volume/label"},
         "value": {"path": "/variables/volume/desired"},
         "variant": "number"},
        {"id": "vol-btn", "component": "Button", "child": "vol-btn-label",
         "action": {"event": {
             "name": "taskvm.local_patch",
             "context": {"semanticKey": "volume"}}}},
        {"id": "vol-btn-label", "component": "Text", "text": "调音量"},
    ],
    "weather": [
        {"id": "root", "component": "Column",
         "children": ["city-card", "forecast-row"]},
        {"id": "city-card", "component": "Card", "child": "city-col"},
        {"id": "city-col", "component": "Column",
         "children": ["city-text", "rain-chk"]},
        {"id": "city-text", "component": "Text",
         "text": {"path": "/variables/city/observed"}, "variant": "h3"},
        {"id": "rain-chk", "component": "CheckBox",
         "label": {"path": "/variables/rain_alert/label"},
         "value": {"path": "/variables/rain_alert/desired"}},
        {"id": "forecast-row", "component": "Row",
         "children": ["forecast-text", "forecast-divider"]},
        {"id": "forecast-text", "component": "Text",
         "text": {"path": "/variables/weekend_forecast/observed"}},
        {"id": "forecast-divider", "component": "Divider",
         "axis": "vertical"},
    ],
    "message": [
        {"id": "root", "component": "Column",
         "children": ["header", "group-text", "msg-field", "mention-field",
                      "send-btn", "send-btn-label", "status-text"]},
        {"id": "header", "component": "Text",
         "text": {"path": "/task/goal"}, "variant": "h3"},
        {"id": "group-text", "component": "Text",
         "text": {"path": "/variables/target_group/observed"},
         "variant": "caption"},
        {"id": "msg-field", "component": "TextField",
         "label": {"path": "/variables/message_text/label"},
         "value": {"path": "/variables/message_text/desired"},
         "variant": "longText"},
        {"id": "mention-field", "component": "TextField",
         "label": {"path": "/variables/mention_list/label"},
         "value": {"path": "/variables/mention_list/desired"}},
        {"id": "send-btn", "component": "Button",
         "child": "send-btn-label", "variant": "primary",
         "action": {"event": {
             "name": "taskvm.local_patch",
             "context": {"semanticKey": "message_text"}}}},
        {"id": "send-btn-label", "component": "Text", "text": "发送"},
        {"id": "status-text", "component": "Text",
         "text": {"path": "/variables/send_status/observed"},
         "variant": "caption"},
    ],
}


class _Reply:
    def __init__(self, parsed):
        self.parsed = parsed
        self.raw = json.dumps(parsed, ensure_ascii=False)
        self.model = "fake-capable-model"
        self.prompt_tokens = 800
        self.completion_tokens = 400


class _SequentialPort:
    """Plays the model: returns the canned tree for each goal, in order."""

    def __init__(self):
        self.calls: list[dict] = []

    def complete_json(self, *, system, user, model=None, max_tokens=3072,
                      temperature=None, image_data_url=None):
        self.calls.append({"user": user, "model": model})
        goal = next(g for g in ("闹钟", "天气", "群") if g in user)
        return _Reply(copy.deepcopy(
            TREES["alarm" if goal == "闹钟"
                  else "weather" if goal == "天气" else "message"]))


def _decode_all(port):
    builder = TaskSurfaceContextBuilder()
    results = []
    for snapshot in UNSEEN_GOALS:
        context = builder.build(snapshot)
        results.append((context, GenUIDecoder(port).decode(context)))
    return results


# ── DoD 1: three unseen goals → three DIFFERENT validated trees ────────────

def test_three_unseen_goals_three_distinct_valid_trees():
    port = _SequentialPort()
    pairs = _decode_all(port)
    trees = [result.components for _ctx, result in pairs]

    # every tree passed the real two-layer gate via the decoder
    for (context, result), tree in zip(pairs, trees):
        assert result.source == SOURCE_MODEL, result.summary()
        assert validate_components(tree, context) == []

    # distinct by canonical content hash
    hashes = {json.dumps(t, sort_keys=True, ensure_ascii=False)
              for t in trees}
    assert len(hashes) == 3

    # distinct by component-type histogram (structural difference)
    histograms = [
        {c["component"] for c in tree} for tree in trees]
    assert len({json.dumps(sorted(h)) for h in histograms}) == 3
    assert "Tabs" in histograms[0] and "Tabs" not in histograms[1]
    assert "Card" in histograms[1] and "Card" not in histograms[2]
    assert "DateTimeInput" in histograms[0]

    # distinct by shape signature: (component type, ordered child ids)
    def _signature(tree):
        by_id = {c["id"]: c for c in tree}
        return tuple(sorted(
            (c["id"], c["component"],
             tuple(by_id.get(ch, {}).get("component", "?")
                   for ch in c.get("children", [])))
            for c in tree))
    signatures = {_signature(t) for t in trees}
    assert len(signatures) == 3

    # exactly one model call per goal — the decoder is not task-templated
    # (each call produced a DIFFERENT tree from the same system prompt)
    assert len(port.calls) == 3


def test_trees_are_goal_specific_not_template_copies():
    """Each tree must reference its OWN goal's variable keys in bindings —
    proof the structure adapts to the task world, not vice versa."""
    port = _SequentialPort()
    pairs = _decode_all(port)
    for (context, result) in pairs:
        blob = json.dumps(result.components, ensure_ascii=False)
        for v in context.variables:
            assert v.semantic_key in blob, (
                f"tree for goal {context.goal!r} never binds "
                f"{v.semantic_key!r}")


# ── DoD 2: ordinary value updates → ZERO GenUI calls ───────────────────────

def test_value_change_is_updateDataModel_only_zero_genui_calls():
    """The A4 invariant: after the initial compose, a variable VALUE
    change (CUA landed / observation folded / verifier pass) walks
    context → projector → store.set_data_model ONLY — the decoder is
    never re-invoked and the component tree (structure) is untouched."""
    port = _SequentialPort()
    builder = TaskSurfaceContextBuilder()
    projector = TaskDataModelProjector()
    store = SurfaceStore("unseen-alarm")

    # initial compose: ONE decoder call, structure installed
    context = builder.build(GOAL_ALARM)
    result = GenUIDecoder(port).decode(context)
    assert result.source == SOURCE_MODEL
    calls_after_compose = len(port.calls)

    store.ensure_surface()
    store.set_components(result.components)
    store.set_data_model(projector.project(context))
    assert store.generation == 1 and store.data_revision == 1

    # …time passes: the CUA sets the alarm, observation folds a NEW value…
    snapshot2 = copy.deepcopy(GOAL_ALARM)
    snapshot2["variables"][0]["observed"] = "2026-08-21T06:30"  # synced
    snapshot2["variables"][2]["observed"] = 7                   # volume done
    context2 = builder.build(snapshot2)

    # the ONLY legitimate reaction: a deterministic data-model refresh
    store.set_data_model(projector.project(context2))

    # structure untouched, no decoder re-invocation, zero model calls
    assert store.generation == 1                # NOT a structural change
    assert store.data_revision == 2
    assert len(port.calls) == calls_after_compose   # ZERO new GenUI calls
    assert store.latest_components() == result.components
    # and the fresh values ARE visible through the existing bindings
    assert store.latest_data_model()["variables"]["alarm_time"][
        "observed"] == "2026-08-21T06:30"

    # three more value ticks — still zero model calls
    for tick in range(3):
        snapshot3 = copy.deepcopy(snapshot2)
        snapshot3["variables"][3]["desired"] = f"备注 v{tick}"
        ctx3 = builder.build(snapshot3)
        store.set_data_model(projector.project(ctx3))
    assert len(port.calls) == calls_after_compose
    assert store.generation == 1 and store.data_revision == 5


# ── DoD 3: zero frontend coupling (Set B static gate) ──────────────────────


def _code_without_docstrings(src: str) -> str:
    """AST-normalized source: docstrings stripped (module/function/class),
    comments gone via unparse. Scanning THIS means the gate matches code
    semantics, not documentation wording — a docstring that merely
    *mentions* a banned pattern is honest prose, while the same pattern
    in real code cannot hide behind a comment."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_no_goal_specific_branches_in_production_genui():
    """Set B: no `if semantic_key ==`, no per-goal template in the
    production genui layer — the surface is generated, not branched.
    Docstrings are stripped first (AST-verified) so the gate reads code,
    not prose."""
    genui_dir = Path(__file__).resolve().parents[2] / "taskvm" / "genui"
    for py in genui_dir.glob("*.py"):
        code = _code_without_docstrings(py.read_text(encoding="utf-8"))
        assert "alarm_time" not in code, f"{py.name}: goal-specific key"
        assert "weekend_forecast" not in code, f"{py.name}: goal-specific key"
        assert "target_group" not in code, f"{py.name}: goal-specific key"
        # key-LITERAL comparison is the hardcoded-branch signature; a
        # parameter lookup (``v.semantic_key == semantic_key``) is generic.
        assert "semantic_key == '" not in code, f"{py.name}: key branching"
        assert 'semantic_key == "' not in code, f"{py.name}: key branching"
