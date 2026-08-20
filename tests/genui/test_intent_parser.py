"""A6 IntentParser contract locks — free text → structured governance
intent.

The parser is model-backed (scripted ports here — zero network) but its
VERDICTS are deterministic given the reply: a valid reply maps onto one
structured kind; every malformed / ungroundable reply degrades to the
honest clarify (a question back, never a guess). The locks:

  - happy path for every executable kind (local_patch single+multi,
    goal_patch, checkpoint, rollback by user-visible LABEL);
  - the model's own clarify rides through as a model-source clarify;
  - malformed replies (bad kind / unknown key / readonly / bad literal
    type / binding-shaped value / invented rollback label / unparseable
    text) → ONE bounded repair → honest clarify fallback (source=
    clarify, the last attempt carries the verbatim rejection reasons);
  - ledger discipline: one provider request = one row (role=
    intent_parser, purpose intent_parse/intent_repair, ok=False +
    error on transport failures, tokens/latency/request_id always);
  - GUI-only (repo contract §3): the prompt carries the public context
    payload but NEVER a checkpoint id (labels only);
  - the routing slot pin: INTENT_PARSER_MODEL_ROLE is a key of the
    workspace_ui role→model table (workplan §20.2 single resolution
    point).
"""
from __future__ import annotations

import pytest

from taskvm.genui import (
    INTENT_PARSER_MODEL_ROLE, INTENT_KINDS, IntentParser, ParsedIntent,
)
from taskvm.genui.intent_parser import IntentCallRecord, validate_reply


class _Reply:
    def __init__(self, parsed, model="qwen-small", raw=""):
        self.parsed = parsed
        self.model = model
        self.raw = raw
        self.prompt_tokens = 111
        self.completion_tokens = 7


class ScriptedPort:
    """Returns the scripted replies in order; records every call."""

    def __init__(self, *replies, fail_with=None):
        self._replies = list(replies)
        self.fail_with = fail_with
        self.calls: list[dict] = []

    def complete_json(self, *, system, user, model=None, max_tokens=1024,
                      temperature=None, image_data_url=None):
        self.calls.append({"system": system, "user": user, "model": model,
                           "max_tokens": max_tokens,
                           "temperature": temperature})
        if self.fail_with is not None:
            raise self.fail_with
        return self._replies.pop(0)


class ListLedger:
    def __init__(self):
        self.records: list = []

    def record(self, rec):
        self.records.append(rec)


def _parse(port, text, context, **kw):
    return IntentParser(port, **kw).parse(text, context)


# ── happy paths ─────────────────────────────────────────────────────────────


def test_local_patch_single_and_multi_key(context):
    port = ScriptedPort(
        _Reply({"kind": "local_patch",
                "updates": {"release_date": "2026-09-01"},
                "rationale": "改到月底"}),
        _Reply({"kind": "local_patch",
                "updates": {"release_date": "2026-09-15",
                            "budget": 3000},
                "rationale": "日期和预算一起改"}),
    )
    r1 = _parse(port, "把发布日期改到 9 月 1 号", context)
    r2 = _parse(port, "日期改 9 月 15，预算改 3000", context)
    assert r1.kind == "local_patch" and not r1.is_clarify
    assert r1.updates == {"release_date": "2026-09-01"}
    assert r1.rationale == "改到月底"
    assert r1.source == "model" and len(r1.attempts) == 1
    assert r2.updates == {"release_date": "2026-09-15", "budget": 3000}


def test_goal_patch_with_constraints(context):
    r = _parse(ScriptedPort(_Reply({
        "kind": "goal_patch", "goal": "改到 9 月初并通知所有人",
        "constraints": ["不改预算"]})), "任务目标改成九月初", context)
    assert r.kind == "goal_patch"
    assert r.goal == "改到 9 月初并通知所有人"
    assert r.constraints == ("不改预算",)
    assert r.scope == () and r.success_criteria == ()


def test_checkpoint_and_rollback_by_label(context):
    r1 = _parse(ScriptedPort(_Reply({
        "kind": "checkpoint", "checkpoint_label": "改预算前"})),
        "先存个检查点", context)
    assert r1.kind == "checkpoint"
    assert r1.checkpoint_label == "改预算前"
    # rollback addresses the USER-VISIBLE label (never an id)
    r2 = _parse(ScriptedPort(_Reply({
        "kind": "rollback", "checkpoint_label": "日期确认点"})),
        "回到日期确认点", context)
    assert r2.kind == "rollback"
    assert r2.checkpoint_label == "日期确认点"


def test_model_own_clarify_rides_through(context):
    r = _parse(ScriptedPort(_Reply({
        "kind": "clarify", "question": "你想改哪个变量？"})),
        "帮我改一下", context)
    assert r.is_clarify
    assert r.source == "model"          # the model's own honest verdict
    assert r.question == "你想改哪个变量？"
    assert len(r.attempts) == 1


# ── honest rejections → bounded repair → clarify fallback ──────────────────


@pytest.mark.parametrize("reply,fragment", [
    ({"kind": "magic", "x": 1}, "unknown intent kind"),
    ({"kind": "local_patch", "updates": {"ghost": "x"}},
     "unknown semantic key"),
    ({"kind": "local_patch", "updates": {"notify_list": "6 人"}},
     "notify_list"),
    ({"kind": "local_patch", "updates": {"budget": "三千"}},
     "rejects value"),
    ({"kind": "local_patch",
      "updates": {"release_date":
                  {"path": "/variables/release_date/desired"}}},
     "must be a literal"),
    ({"kind": "goal_patch"}, "non-empty goal"),
    ({"kind": "rollback", "checkpoint_label": "不存在的点"},
     "checkpoint list"),
    ({"kind": "clarify"}, "non-empty question"),
])
def test_bad_replies_repair_then_clarify(context, reply, fragment):
    """Every ungroundable reply gets ONE bounded repair round (the
    full prompt + verbatim reasons), then the honest clarify — never
    a guess, never a partial intent."""
    port = ScriptedPort(_Reply(reply), _Reply(reply))
    r = _parse(port, "随便来点什么", context)
    assert r.is_clarify
    assert r.source == "clarify"
    assert len(port.calls) == 2                 # parse + one repair
    assert port.calls[1]["user"] != port.calls[0]["user"]
    assert fragment in port.calls[1]["user"]    # reasons fed back
    assert fragment in " ".join(r.attempts[0].errors)
    assert r.attempts[-1].purpose == "intent_clarify"


def test_unparseable_reply_then_clarify(context):
    port = ScriptedPort(_Reply(None, raw="not json at all"),
                        _Reply(None, raw="still not json"))
    r = _parse(port, "改一下", context)
    assert r.is_clarify and r.source == "clarify"
    assert len(port.calls) == 2


def test_transport_failure_is_honest_clarify(context):
    port = ScriptedPort(fail_with=RuntimeError("gateway down"))
    ledger = ListLedger()
    r = IntentParser(port, ledger).parse("改日期", context)
    assert r.is_clarify and r.source == "clarify"
    # TWO rows (parse + repair) — both honest failures
    assert len(ledger.records) == 2
    assert all(not rec.ok for rec in ledger.records)
    assert all("gateway down" in rec.error for rec in ledger.records)


def test_empty_text_clarifies_without_any_model_call(context):
    port = ScriptedPort()
    r = _parse(port, "   ", context)
    assert r.is_clarify and r.source == "clarify"
    assert port.calls == []          # zero provider requests
    assert r.question                # an honest question, not silence


# ── ledger discipline (one provider request = one row) ─────────────────────


def test_ledger_rows_for_success_and_repair(context):
    ledger = ListLedger()
    port = ScriptedPort(
        _Reply({"kind": "magic"}),                       # round 1 bad
        _Reply({"kind": "local_patch",                   # repair ok
                "updates": {"budget": 3000}, "rationale": "r"}),
    )
    r = IntentParser(port, ledger).parse("预算改成 3000", context)
    assert r.kind == "local_patch"
    assert len(ledger.records) == 2
    first, second = ledger.records
    assert isinstance(first, IntentCallRecord)
    assert first.role == INTENT_PARSER_MODEL_ROLE
    assert first.purpose == "intent_parse" and not first.is_repair
    assert second.purpose == "intent_repair" and second.is_repair
    assert first.ok and second.ok       # both REQUESTS completed
    assert first.model == "qwen-small"
    assert first.prompt_tokens == 111 and first.completion_tokens == 7
    assert first.request_id and second.request_id
    assert first.request_id != second.request_id


# ── GUI-only (repo contract §3): no internal ids in the prompt ─────────────


def test_prompt_carries_public_context_but_never_checkpoint_ids(context):
    port = ScriptedPort(_Reply({"kind": "clarify", "question": "q"}))
    IntentParser(port).parse("回到日期确认点", context)
    assert port.calls, "one call expected"
    prompt = port.calls[0]["user"]
    assert "日期确认点" in prompt          # the user-visible label rides
    assert "release_date" in prompt       # semantic keys are public
    # the conftest snapshot's INTERNAL ids must NEVER reach the model
    assert "cp:00001" not in prompt
    assert "s-internal-001" not in prompt
    assert "checkpoint_id" not in prompt


# ── model routing (§20.2) + the routing-slot pin ────────────────────────────


def test_model_routing_priority(context, monkeypatch):
    # constructor arg wins over env
    port = ScriptedPort(_Reply({"kind": "clarify", "question": "q"}))
    IntentParser(port, model="ctor-model").parse("x", context)
    assert port.calls[0]["model"] == "ctor-model"
    # env var beats the port default
    monkeypatch.setenv("TASKVM_INTENT_PARSER_MODEL", "env-model")
    port2 = ScriptedPort(_Reply({"kind": "clarify", "question": "q"}))
    IntentParser(port2).parse("x", context)
    assert port2.calls[0]["model"] == "env-model"
    # no arg, no env → None (the port's own default decides)
    monkeypatch.delenv("TASKVM_INTENT_PARSER_MODEL")
    port3 = ScriptedPort(_Reply({"kind": "clarify", "question": "q"}))
    IntentParser(port3).parse("x", context)
    assert port3.calls[0]["model"] is None
    # temperature is NOT sent by default (the FRIDAY-gateway rule)
    assert port3.calls[0]["temperature"] is None


def test_routing_slot_pin():
    """INTENT_PARSER_MODEL_ROLE is a key of the workspace_ui
    role→model table — the §20.2 single resolution point (the
    GENUI_DECODER_MODEL_ROLE precedent)."""
    from taskvm.workspace_ui.composition import DEFAULT_ROLE_MODELS
    assert INTENT_PARSER_MODEL_ROLE in DEFAULT_ROLE_MODELS


# ── validate_reply direct contract ─────────────────────────────────────────


def test_validate_reply_accepts_known_kinds_and_rejects_others(context):
    assert validate_reply({"kind": "local_patch",
                           "updates": {"budget": 1}}, context) == \
        ("local_patch", [])
    kind, errors = validate_reply({"kind": "nope"}, context)
    assert kind == "clarify" and errors
    # INTENT_KINDS is the closed vocabulary
    assert set(INTENT_KINDS) == {"local_patch", "goal_patch",
                                 "checkpoint", "rollback", "clarify"}


# ── the shared-ledger registration lock (the e71520e precedent) ────────────


def test_intent_parser_role_registered_in_shared_ledger():
    """INTENT_PARSER_MODEL_ROLE is the SAME string registered in
    ``taskvm.architect.port.MODEL_ROLES`` — the shared ModelCallLedger
    (which rejects unknown roles) buckets intent-parser calls under the
    key the benchmark reads. Identical-string pin, the
    ``MODEL_ROLE_MODEL_VERIFIER`` / ``GENUI_DECODER`` precedent."""
    from taskvm.architect.port import (
        MODEL_ROLE_INTENT_PARSER, MODEL_ROLES,
    )
    assert MODEL_ROLE_INTENT_PARSER == "intent_parser"
    assert MODEL_ROLE_INTENT_PARSER in MODEL_ROLES
    assert INTENT_PARSER_MODEL_ROLE == MODEL_ROLE_INTENT_PARSER


def test_intent_rows_accepted_by_the_real_shared_ledger(context):
    """The REAL ``ModelCallLedger`` (not the ListLedger double above)
    accepts :class:`IntentCallRecord` rows — field-for-field
    compatibility is a contract, not an accident (one provider request
    = one row on the ledger the goal's other roles already use)."""
    from taskvm.architect.port import ModelCallLedger
    ledger = ModelCallLedger()
    port = ScriptedPort(_Reply({"kind": "clarify", "question": "q"}))
    IntentParser(port, ledger).parse("改一下", context)
    assert len(ledger.records) == 1
    rec = ledger.records[0]
    assert rec.role == INTENT_PARSER_MODEL_ROLE
    assert rec.ok and rec.request_id
