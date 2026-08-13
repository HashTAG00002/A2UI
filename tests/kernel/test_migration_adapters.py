"""Migration-layer tests: legacy → domain converters stay one-directional
and strip platform-internal concepts."""
from dataclasses import dataclass, field
from typing import Any

from taskvm._migration.legacy_state import (
    legacy_edit_to_variable_update,
    legacy_graph_to_task_state,
    legacy_op_to_action_contract,
)
from taskvm.domain import MUTABILITY_READONLY, TaskIntent


@dataclass
class _FakeLegacyBinding:
    """Shape of the legacy binding edge (duck-typed on purpose: the
    converter must not import the legacy module)."""
    locator: str | None = None
    platform_addressing: str = "INTERNAL-ONLY"   # must NOT survive conversion


@dataclass
class _FakeLegacyVar:
    var_id: str
    label: str
    value: Any
    editable: bool = True
    kind: str = "string"
    bindings: list = field(default_factory=list)


@dataclass
class _FakeLegacyGraph:
    task_id: str
    goal: str
    variables: list = field(default_factory=list)


@dataclass
class _FakeLegacyOp:
    value: Any
    platform_addressing: str = "INTERNAL-ONLY"
    app_verb: str = "INTERNAL-ONLY"


def test_legacy_graph_conversion_strips_platform_concepts():
    g = _FakeLegacyGraph(task_id="t", goal="g", variables=[
        _FakeLegacyVar(var_id="release_date", label="发布日期",
                       value="2026-08-14", kind="date",
                       bindings=[_FakeLegacyBinding(locator="项目发布会议")]),
        _FakeLegacyVar(var_id="note", label="备注", value="x", editable=False),
    ])
    state = legacy_graph_to_task_state(g, intent=TaskIntent(goal="g"))
    v = state.variable("release_date")
    assert v.semantic_key == "release_date" and v.value == "2026-08-14"
    assert v.value_type == "date"
    # the visible locator survived as evidence; the platform addressing did not
    assert v.evidence[0].visible_label == "项目发布会议"
    assert "INTERNAL-ONLY" not in repr(state)
    assert state.variable("note").mutability == MUTABILITY_READONLY


def test_legacy_edit_conversion():
    assert legacy_edit_to_variable_update(
        {"var_id": "release_date", "old": "a", "new": "b"}) == ("release_date", "b")


def test_legacy_op_to_contract_keeps_only_semantics():
    op = _FakeLegacyOp(value="2026-08-18")
    c = legacy_op_to_action_contract(op, semantic_key="release_date",
                                     visible_label="项目发布会议",
                                     contract_id="c1")
    assert c.desired_state == {"release_date": "2026-08-18"}
    assert c.target_evidence[0].visible_label == "项目发布会议"
    assert "INTERNAL-ONLY" not in repr(c)
