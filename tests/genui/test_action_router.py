"""A6 ActionRouter contract locks — the C2S write-path validation half.

The router re-validates the renderer's POSTed action against the SAME
ground truth the S2C policy layer uses (conftest snapshot: release_date
editable string / notify_list readonly string / budget editable
integer) and mints ONE structured LocalPatchIntent — plain JSON-able
data, zero model calls. The locks:

  - happy path: a conformant local_patch on an editable variable mints
    the intent with the exact updates; the rationale defaults honestly
    and a client-provided one rides through verbatim;
  - honest rejections with the right status: governance-owned 403,
    unknown action 400, missing semanticKey 400, unknown key 400,
    readonly 403, missing value 400, bad literal type 400;
  - unresolved data bindings fail CLOSED — the server never resolves
    them itself (it would be inventing the user's edit);
  - the one-rule-set lock: for the same (variable, literal) pair the
    router's verdict EQUALS the S2C policy layer's verdict on a bound
    tree — two enforcement points, one rule set, no drift;
  - ``LocalPatchIntent.to_payload`` is the structured governance-event
    shape (kind / updates / rationale / correlation_id).
"""
from __future__ import annotations

import pytest

from taskvm.genui import (
    ACTION_LOCAL_PATCH, ActionRouteError, ActionRouter, LocalPatchIntent,
    validate_components,
)


# ── the happy path ──────────────────────────────────────────────────────────


def test_happy_path_mints_structured_intent(context):
    intent = ActionRouter(context).route(ACTION_LOCAL_PATCH, {
        "semanticKey": "release_date", "value": "2026-09-01"})
    assert isinstance(intent, LocalPatchIntent)
    assert intent.updates == {"release_date": "2026-09-01"}
    assert intent.rationale == "a2ui surface action"   # honest default


def test_client_rationale_rides_the_intent_verbatim(context):
    intent = ActionRouter(context).route(ACTION_LOCAL_PATCH, {
        "semanticKey": "release_date", "value": "2026-09-01",
        "rationale": "把日期改到月底"})
    assert intent.rationale == "把日期改到月底"


def test_integer_literal_accepted_for_integer_variable(context):
    intent = ActionRouter(context).route(ACTION_LOCAL_PATCH, {
        "semanticKey": "budget", "value": 3000})
    assert intent.updates == {"budget": 3000}


def test_to_payload_is_the_governance_event_shape(context):
    intent = ActionRouter(context).route(ACTION_LOCAL_PATCH, {
        "semanticKey": "budget", "value": 3000, "rationale": "加预算"})
    assert intent.to_payload() == {
        "kind": "local_patch", "updates": {"budget": 3000},
        "rationale": "加预算", "correlation_id": "",
    }


def test_router_is_stateless_per_call(context):
    """Same router, two routes, two independent intents — pins that no
    per-request state is cached on the router between calls."""
    r = ActionRouter(context)
    i1 = r.route(ACTION_LOCAL_PATCH,
                 {"semanticKey": "budget", "value": 3000})
    i2 = r.route(ACTION_LOCAL_PATCH,
                 {"semanticKey": "budget", "value": 4000})
    assert i1.updates == {"budget": 3000}
    assert i2.updates == {"budget": 4000}


# ── honest rejections ───────────────────────────────────────────────────────


@pytest.mark.parametrize("name,ctx,status,fragment", [
    # governance-owned: the dynamic surface may never emit these
    ("pause", {}, 403, "governance-owned"),
    ("rollback", {}, 403, "governance-owned"),
    # unknown action
    ("taskvm.magic", {}, 400, "unknown action"),
    # missing / empty semanticKey
    (ACTION_LOCAL_PATCH, {}, 400, "semanticKey"),
    (ACTION_LOCAL_PATCH, {"semanticKey": ""}, 400, "semanticKey"),
    # unknown semantic key
    (ACTION_LOCAL_PATCH, {"semanticKey": "nope", "value": "x"},
     400, "unknown semantic key"),
    # readonly variable
    (ACTION_LOCAL_PATCH, {"semanticKey": "notify_list", "value": "6 人"},
     403, "readonly"),
    # missing / null value
    (ACTION_LOCAL_PATCH, {"semanticKey": "release_date"}, 400,
     "context.value"),
    (ACTION_LOCAL_PATCH, {"semanticKey": "release_date", "value": None},
     400, "context.value"),
    # bad literal types (budget is integer; bool never poses as a number)
    (ACTION_LOCAL_PATCH, {"semanticKey": "budget", "value": "三千"},
     400, "rejects value"),
    (ACTION_LOCAL_PATCH, {"semanticKey": "budget", "value": True},
     400, "rejects value"),
    (ACTION_LOCAL_PATCH, {"semanticKey": "budget", "value": 3.5},
     400, "rejects value"),
    # bad literal type (release_date is string)
    (ACTION_LOCAL_PATCH, {"semanticKey": "release_date", "value": 123},
     400, "rejects value"),
])
def test_honest_rejections(context, name, ctx, status, fragment):
    with pytest.raises(ActionRouteError) as ei:
        ActionRouter(context).route(name, ctx)
    assert ei.value.http_status == status
    assert fragment in str(ei.value)


def test_unresolved_data_binding_fails_closed(context):
    """A non-conforming client posting an UNRESOLVED ``{"path": …}``
    value gets an explicit 400 — the server must never resolve the
    binding itself (it would be inventing the user's edit value). The
    S2C twin of this rule is policy._check_value_type's A5-IFACE-01
    branch: legal on the tree, illegal on the write path."""
    with pytest.raises(ActionRouteError) as ei:
        ActionRouter(context).route(ACTION_LOCAL_PATCH, {
            "semanticKey": "release_date",
            "value": {"path": "/variables/release_date/desired"}})
    assert ei.value.http_status == 400
    assert "unresolved data binding" in str(ei.value)


# ── the one-rule-set lock (two enforcement points, no drift) ────────────────


def _policy_verdict(context, data_model, key, value) -> list[str]:
    """Run the S2C two-layer gate on a minimal tree whose Button action
    context carries the (key, value) pair under test."""
    tree = [
        {"id": "root", "component": "Column",
         "children": ["btn", "btn_label"]},
        {"id": "btn", "component": "Button", "child": "btn_label",
         "action": {"event": {"name": ACTION_LOCAL_PATCH,
                              "context": {"semanticKey": key,
                                          "value": value}}}},
        {"id": "btn_label", "component": "Text", "text": "更新"},
    ]
    return validate_components(tree, context, data_model,
                                surface_id="taskvm-task-mirror")


@pytest.mark.parametrize("key,value", [
    ("release_date", "2026-09-01"),   # string literal: both accept
    ("release_date", 42),             # wrong type: both reject
    ("budget", 3000),                 # integer literal: both accept
    ("budget", 3000.5),               # float into integer: both reject
    ("budget", True),                 # bool posing as int: both reject
    ("budget", "三千"),               # string into integer: both reject
    ("notify_list", "6 人"),           # readonly: both reject
])
def test_router_verdict_equals_policy_verdict(context, data_model,
                                              key, value):
    """One rule set, two enforcement points: the router's C2S verdict on
    a (variable, literal) pair EQUALS the S2C policy gate's verdict on
    a tree carrying the same pair in an action context. Any drift here
    means the write path and the tree path disagree about the truth."""
    policy_errors = _policy_verdict(context, data_model, key, value)
    try:
        ActionRouter(context).route(ACTION_LOCAL_PATCH,
                                    {"semanticKey": key, "value": value})
        router_rejected = False
    except ActionRouteError:
        router_rejected = True
    assert router_rejected == bool(policy_errors), (
        f"verdict drift for ({key!r}, {value!r}): "
        f"policy errors={policy_errors!r}, "
        f"router rejected={router_rejected}")
