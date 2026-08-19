"""tests/verifier — the model-based verifier (three-state, PURETY-GEN §4.2).

Fake-port tests over ``taskvm.verifier.model_verifier.ModelVerifier``:

  * the honest three-state contract: changed / not_yet / cannot_verify all
    pass through; a malformed or unparseable model reply is the honest
    ``cannot_verify`` (the model-side capability bound — never a crash,
    never a fabricated changed);
  * deterministic rules are DEMOTED to a cheap pre-filter: the built-in
    fingerprint short-circuit and an injected prefilter may prove
    ``not_yet`` with ZERO model calls, but may NEVER confirm/veto — a rule
    answering anything else is ignored, a crashing rule is swallowed;
  * accounting: one REAL provider request = one ledger row with
    role="model_verifier" on every path (success / unparseable / transport
    error) — verified against the REAL ``taskvm.architect.ModelCallLedger``
    (structural compatibility: record + counts_by_role + annotate all work
    across layers without the verifier importing the architect);
  * the no-leak gate (the REAL ``taskvm.architect.noleak.assert_prompt_clean``
    injected as ``prompt_gate``): a clean prompt passes; a leaking prompt
    (internal wxid_* in the visible text) issues NO request, lands NO row,
    and answers the honest cannot_verify;
  * the screenshot travels ONLY as the multimodal image part (a real
    data:image/ URL); a non-data-URL ref degrades honestly to text-only.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from taskvm.architect import MODEL_ROLES, ModelCallLedger, ModelReply
from taskvm.architect.noleak import assert_prompt_clean
from taskvm.verifier import (
    MODEL_ROLE_MODEL_VERIFIER,
    VERDICT_CANNOT_VERIFY,
    VERDICT_CHANGED,
    VERDICT_NOT_YET,
    ModelVerifier,
)


# ── fakes ───────────────────────────────────────────────────────────────────

class FakePort:
    """Scripted ModelPort double: records every call, replies in order."""

    default_model = "fake-verifier-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete_json(self, *, system, user, model=None, max_tokens=3072,
                      temperature=None, image_data_url=None):
        self.calls.append({"system": system, "user": user, "model": model,
                           "image_data_url": image_data_url})
        item = self.replies.pop(0) if self.replies else None
        if isinstance(item, Exception):
            raise item
        return item


def _reply(verdict: str, evidence: str = "屏幕可见证据") -> ModelReply:
    return ModelReply(parsed={"verdict": verdict, "evidence": evidence},
                      raw=json.dumps({"verdict": verdict}),
                      model="fake-verifier-model",
                      prompt_tokens=11, completion_tokens=7)


def _obs(*, visible_text: str = "微信 黄勇 消息已发送",
         screenshot_ref="data:image/png;base64,aGVsbG8=",
         fingerprint: str = "abc123") -> SimpleNamespace:
    return SimpleNamespace(visible_text=visible_text,
                           screenshot_ref=screenshot_ref,
                           fingerprint=fingerprint)


def _run(coro):
    return asyncio.run(coro)


# ── the honest three-state contract ─────────────────────────────────────────

@pytest.mark.parametrize("verdict", [VERDICT_CHANGED, VERDICT_NOT_YET,
                                     VERDICT_CANNOT_VERIFY])
def test_three_verdicts_pass_through(verdict):
    port = FakePort([_reply(verdict, evidence="引用证据")])
    out = _run(ModelVerifier(port).verify_intent(
        observation=_obs(), intent="在微信中对黄勇发送了消息"))
    assert out == {"verdict": verdict, "evidence": "引用证据"}


def test_malformed_verdict_is_honest_cannot_verify():
    port = FakePort([_reply("probably_done")])
    out = _run(ModelVerifier(port).verify_intent(
        observation=_obs(), intent="点赞"))
    assert out["verdict"] == VERDICT_CANNOT_VERIFY
    assert "probably_done" in out["evidence"]


def test_unparseable_reply_is_honest_cannot_verify():
    port = FakePort([ModelReply(parsed=None, raw="not json",
                                model="m")])
    out = _run(ModelVerifier(port).verify_intent(
        observation=_obs(), intent="点赞"))
    assert out["verdict"] == VERDICT_CANNOT_VERIFY


def test_non_dict_parsed_is_honest_cannot_verify():
    port = FakePort([ModelReply(parsed=["changed"], raw="[]",
                                model="m")])
    out = _run(ModelVerifier(port).verify_intent(
        observation=_obs(), intent="点赞"))
    assert out["verdict"] == VERDICT_CANNOT_VERIFY


# ── rules demoted to a cheap pre-filter ─────────────────────────────────────

def test_fingerprint_shortcircuit_saves_the_model_call():
    """The screen did not change at all (fingerprint identical to the
    pre-write baseline) → not_yet with ZERO model calls and ZERO ledger
    rows (rows count real provider requests)."""
    port = FakePort([_reply(VERDICT_CHANGED)])
    ledger = ModelCallLedger()
    verifier = ModelVerifier(port, ledger)
    out = _run(verifier.verify_intent(
        observation=_obs(fingerprint="fp1"), intent="发送消息",
        baseline_fingerprint="fp1"))
    assert out["verdict"] == VERDICT_NOT_YET
    assert "fingerprint" in out["evidence"]
    assert port.calls == [], "the pre-filter must save the model call"
    assert len(ledger) == 0


def test_fingerprint_differs_proceeds_to_the_model():
    port = FakePort([_reply(VERDICT_CHANGED)])
    verifier = ModelVerifier(port)
    out = _run(verifier.verify_intent(
        observation=_obs(fingerprint="fp2"), intent="发送消息",
        baseline_fingerprint="fp1"))
    assert out["verdict"] == VERDICT_CHANGED
    assert len(port.calls) == 1


def test_injected_prefilter_notyet_shortcircuits():
    port = FakePort([_reply(VERDICT_CHANGED)])
    verifier = ModelVerifier(port, prefilter=lambda obs, i: VERDICT_NOT_YET)
    out = _run(verifier.verify_intent(observation=_obs(), intent="发送消息"))
    assert out["verdict"] == VERDICT_NOT_YET
    assert port.calls == []


def test_prefilter_can_never_confirm_or_veto():
    """A rule answering 'changed' (or anything besides not_yet) is IGNORED
    — the model stays the sole final judge. Here the rule says changed and
    the model says not_yet: the final verdict is not_yet (the rule cannot
    veto the model)."""
    port = FakePort([_reply(VERDICT_NOT_YET)])
    verifier = ModelVerifier(port, prefilter=lambda obs, i: VERDICT_CHANGED)
    out = _run(verifier.verify_intent(observation=_obs(), intent="发送消息"))
    assert out["verdict"] == VERDICT_NOT_YET
    assert len(port.calls) == 1, "an affirming rule must not short-circuit"


def test_crashing_prefilter_is_swallowed():
    """A buggy rule must not veto anything — its exception is swallowed
    and the decision goes to the model."""
    port = FakePort([_reply(VERDICT_CHANGED)])

    def broken(obs, intent):
        raise RuntimeError("rule bug")

    verifier = ModelVerifier(port, prefilter=broken)
    out = _run(verifier.verify_intent(observation=_obs(), intent="发送消息"))
    assert out["verdict"] == VERDICT_CHANGED
    assert len(port.calls) == 1


# ── accounting: one provider request = one ledger row, every path ───────────

def test_one_request_one_row_with_real_architect_ledger():
    """Structural compatibility proof: the verifier's rows land in the
    REAL ModelCallLedger (record / counts_by_role / records all work)
    without the verifier package importing the architect layer."""
    port = FakePort([_reply(VERDICT_CHANGED), _reply("bogus"),
                     ModelReply(parsed=None, raw="x", model="m")])
    ledger = ModelCallLedger()
    verifier = ModelVerifier(port, ledger)
    _run(verifier.verify_intent(observation=_obs(), intent="i1"))
    _run(verifier.verify_intent(observation=_obs(), intent="i2"))
    _run(verifier.verify_intent(observation=_obs(), intent="i3"))
    assert verifier.request_count == 3
    assert len(ledger) == 3
    assert ledger.counts_by_role()[MODEL_ROLE_MODEL_VERIFIER] == 3
    oks = [r.ok for r in ledger.records]
    assert oks == [True, False, False]
    assert len({r.request_id for r in ledger.records}) == 3, (
        "request_ids must be unique — 1 provider request = 1 row")
    assert ledger.tokens_by_role()[MODEL_ROLE_MODEL_VERIFIER] == (22, 14), (
        "reply#1 + reply#2 carry 11/7 each; the unparseable reply#3 has "
        "no token counts (None counts 0)")


def test_real_ledger_annotates_verifier_rows():
    """The real ledger's annotate() (dataclasses.replace on the row) works
    on verifier rows — field-compatible ModelCallRecord shape."""
    port = FakePort([_reply(VERDICT_CHANGED)])
    ledger = ModelCallLedger()
    verifier = ModelVerifier(port, ledger)
    _run(verifier.verify_intent(observation=_obs(), intent="i"))
    row = ledger.records[0]
    updated = ledger.annotate(row.request_id, node_id="n001", attempt=2)
    assert updated is not None
    assert updated.node_id == "n001" and updated.attempt == 2
    assert updated.role == MODEL_ROLE_MODEL_VERIFIER
    assert updated.purpose == "verifier.verify_intent"


def test_transport_error_lands_a_row_and_raises():
    """An infrastructure failure is NOT a model capability bound: the row
    still lands (a real provider request WAS attempted) and the error
    propagates honestly for the caller to surface."""
    port = FakePort([RuntimeError("connection reset")])
    ledger = ModelCallLedger()
    verifier = ModelVerifier(port, ledger)
    with pytest.raises(RuntimeError, match="connection reset"):
        _run(verifier.verify_intent(observation=_obs(), intent="i"))
    assert verifier.request_count == 1
    assert len(ledger) == 1
    row = ledger.records[0]
    assert row.ok is False and "connection reset" in row.error


def test_model_role_registered_in_architect_roles():
    """Cross-package consistency: the verifier's role constant is the
    identical string registered in architect.MODEL_ROLES (the ledger's
    record() validates against that tuple)."""
    assert MODEL_ROLE_MODEL_VERIFIER in MODEL_ROLES
    assert MODEL_ROLE_MODEL_VERIFIER == "model_verifier"


# ── the injected no-leak gate ───────────────────────────────────────────────

def test_clean_prompt_passes_the_real_noleak_gate():
    port = FakePort([_reply(VERDICT_CHANGED)])
    verifier = ModelVerifier(port, prompt_gate=assert_prompt_clean)
    out = _run(verifier.verify_intent(observation=_obs(), intent="发送消息"))
    assert out["verdict"] == VERDICT_CHANGED
    assert len(port.calls) == 1


def test_leaking_prompt_is_blocked_before_any_request():
    """GUI-only red line: visible text carrying an internal id (wxid_*)
    would leak into the model input. The injected gate fires BEFORE any
    provider request: no request, no ledger row, honest cannot_verify."""
    port = FakePort([_reply(VERDICT_CHANGED)])
    ledger = ModelCallLedger()
    verifier = ModelVerifier(port, ledger, prompt_gate=assert_prompt_clean)
    out = _run(verifier.verify_intent(
        observation=_obs(visible_text="聊天 wxid_hy_123 微信"),
        intent="发送消息"))
    assert out["verdict"] == VERDICT_CANNOT_VERIFY
    assert "安全终止" in out["evidence"]
    assert port.calls == [], "a leaking prompt must issue NO request"
    assert len(ledger) == 0, "and land NO ledger row"


# ── the screenshot travels as the image part only ───────────────────────────

def test_screenshot_goes_as_image_part_never_as_text():
    port = FakePort([_reply(VERDICT_CHANGED)])
    _run(ModelVerifier(port).verify_intent(
        observation=_obs(screenshot_ref="data:image/png;base64,aGVsbG8="),
        intent="i"))
    call = port.calls[0]
    assert call["image_data_url"] == "data:image/png;base64,aGVsbG8="
    assert "data:image" not in call["user"], (
        "the image must never be inlined into the prompt text")


def test_non_data_url_ref_degrades_to_text_only():
    """An artifact ref / file path is NOT a valid image payload — honest
    text-only degradation, no guessing."""
    port = FakePort([_reply(VERDICT_CHANGED)])
    _run(ModelVerifier(port).verify_intent(
        observation=_obs(screenshot_ref="/tmp/shot.png"), intent="i"))
    assert port.calls[0]["image_data_url"] is None
    assert "/tmp/shot.png" not in port.calls[0]["user"]


def test_intent_and_visible_text_reach_the_prompt():
    port = FakePort([_reply(VERDICT_CHANGED)])
    _run(ModelVerifier(port).verify_intent(
        observation=_obs(visible_text="微信 黄勇"), intent="发送消息：hi"))
    user = port.calls[0]["user"]
    assert "发送消息：hi" in user
    assert "微信 黄勇" in user
