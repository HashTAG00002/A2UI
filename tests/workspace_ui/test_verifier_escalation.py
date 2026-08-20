"""tests/workspace_ui — the verifier ESCALATION route (owner order
2026-08-20): deterministic first, the model as the FINAL judge.

The route under test (taskvm/workspace_ui/verifier_escalation.py):

  * a deterministic PASS short-circuits with ZERO model calls (the
    frozen runtime.md §6 contract keeps holding for rule-decidable
    comparisons);
  * a deterministic MISMATCH escalates to ModelVerifier (R4 three-state)
    with a business-language intent: ``changed`` OVERRULES the rule
    (rules never veto the model — PURETY-GEN §4.2); ``not_yet`` /
    ``cannot_verify`` fail honestly;
  * no model verifier wired ⇒ plain deterministic behavior;
  * a model infrastructure error ⇒ the deterministic result stands
    (honest degradation — never a fabricated verdict);
  * GUI-only: the escalation intent carries the node LABEL and public
    variable semantics, NEVER the node id;
  * env gate: build_escalating_verifier returns the plain
    VisibleVerifier without OPENAI_API_KEY, the EscalatingVerifier with
    it (or forced) — keyless runs never impersonate model verdicts;
  * accounting: one escalation = one ledger row (role model_verifier);
  * the sync caller may sit inside a running event loop (thread shim).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from taskvm.architect import ModelCallLedger, ModelReply
from taskvm.domain.contract import ActionContract, Reversibility
from taskvm.domain.workflow import NodeKind, WorkflowNode
from taskvm.substrate import Observation, SurfaceInfo
from taskvm.verifier.model_verifier import ModelVerifier
from taskvm.verifier.visible import VisibleVerifier
from taskvm.workspace_ui.verifier_escalation import (
    EscalatingVerifier,
    build_escalating_verifier,
)


# ── fakes / helpers (same shapes as tests/verifier/test_model_verifier) ────

class FakePort:
    """Scripted ModelPort double: records every call, replies in order."""

    default_model = "fake-escalation-model"

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


def _reply(verdict: str, evidence: str = "屏幕显示已点赞") -> ModelReply:
    return ModelReply(parsed={"verdict": verdict, "evidence": evidence},
                      raw=json.dumps({"verdict": verdict}),
                      model="fake-escalation-model",
                      prompt_tokens=11, completion_tokens=7)


def _action_node(node_id="n1", label="", desired=None, completion=""):
    return WorkflowNode(
        node_id=node_id, kind=NodeKind.ACTION,
        label=label or node_id,
        contract=ActionContract(
            contract_id=f"c-{node_id}",
            semantic_goal=f"realise {node_id}",
            desired_state=dict(desired or {}),
            completion_condition=completion,
            reversibility=Reversibility("reversible")))


def _obs(visible="liked=已点赞", ref="shot://app/1"):
    return Observation(
        surface=SurfaceInfo(surface_id="app", display_name="app"),
        revision=1, timestamp=0.0, screenshot_ref=ref,
        visible_text=visible, fingerprint="fp:1")


def _escalation(replies, ledger=None) -> tuple[EscalatingVerifier, FakePort]:
    port = FakePort(replies)
    mv = ModelVerifier(port=port, ledger=ledger)
    return EscalatingVerifier(model_verifier=mv), port


def _verify(verifier, *, node, after, desired=None, observation=None):
    return verifier.verify(
        node=node, before_observed={}, after_observed=after,
        desired=desired if desired is not None else dict(after),
        observation=observation if observation is not None else _obs(),
        action_id="a1", epoch=1)


# ── §E.1 deterministic PASS short-circuits — zero model calls ─────────────

def test_deterministic_pass_shortcircuits_zero_model_calls():
    verifier, port = _escalation([_reply("changed")])
    node = _action_node(desired={"x": "A"})
    vr = _verify(verifier, node=node, after={"x": "A"})
    assert vr.passed is True
    assert port.calls == []                       # ZERO model calls


# ── §E.2 the vocabulary-gap mismatch escalates; the model OVERRULES ───────

def test_mismatch_model_changed_overrules_the_rule():
    verifier, port = _escalation([_reply("changed", "屏幕显示「已点赞」")])
    node = _action_node(node_id="n42", label="给帖子点赞",
                        desired={"post_liked": "true"})
    vr = _verify(verifier, node=node,
                 after={"post_liked": "已点赞"})   # CJK label: rule mismatch
    assert vr.passed is True                      # model overrules the rule
    assert "已点赞" in vr.detail                   # model's visible evidence
    assert len(port.calls) == 1                   # exactly one model call


def test_mismatch_model_not_yet_fails_honestly():
    verifier, _ = _escalation([_reply("not_yet", "屏幕仍显示「点赞」")])
    node = _action_node(desired={"post_liked": "true"})
    vr = _verify(verifier, node=node, after={"post_liked": "点赞"})
    assert vr.passed is False
    assert "not_yet" in vr.detail


def test_mismatch_model_cannot_verify_fails_honestly():
    verifier, _ = _escalation([_reply("cannot_verify", "证据不足")])
    node = _action_node(desired={"post_liked": "true"})
    vr = _verify(verifier, node=node, after={"post_liked": "点赞"})
    assert vr.passed is False
    assert "cannot_verify" in vr.detail


# ── §E.3 no model wired / infra error — deterministic honesty ──────────────

def test_no_model_verifier_is_pure_deterministic():
    verifier = EscalatingVerifier()               # no model_verifier
    node = _action_node(desired={"post_liked": "true"})
    vr = _verify(verifier, node=node, after={"post_liked": "已点赞"})
    assert vr.passed is False                     # honest mismatch, no crash
    assert "unmet desired_state" in vr.detail


def test_model_infra_error_deterministic_result_stands():
    verifier, _ = _escalation([RuntimeError("connection refused")])
    node = _action_node(desired={"post_liked": "true"})
    vr = _verify(verifier, node=node, after={"post_liked": "已点赞"})
    assert vr.passed is False
    assert "unavailable" in vr.detail
    assert "deterministic result stands" in vr.detail


# ── §E.4 GUI-only: the intent carries public semantics, never ids ─────────

def test_intent_text_public_semantics_only_never_node_id():
    verifier, port = _escalation([_reply("changed")])
    node = _action_node(node_id="n42", label="给帖子点赞",
                        desired={"post_liked": "true"},
                        completion="post_liked == true")
    _verify(verifier, node=node, after={"post_liked": "已点赞"})
    user_prompt = port.calls[0]["user"]
    assert "n42" not in user_prompt               # NO internal node id
    assert "给帖子点赞" in user_prompt             # the public label
    assert "post_liked" in user_prompt            # public variable name
    assert "已点赞" in user_prompt                 # the scrubbed visible text


# ── §E.5 env gate (B-06): keyless runs never impersonate model verdicts ───

def test_build_env_gate_off_returns_plain_visible_verifier(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    port = FakePort([])
    verifier = build_escalating_verifier(port=port, ledger=ModelCallLedger())
    assert isinstance(verifier, VisibleVerifier)
    assert not isinstance(verifier, EscalatingVerifier)


def test_build_env_gate_on_escalates(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-escalation-gate")
    verifier = build_escalating_verifier(
        port=FakePort([]), ledger=ModelCallLedger())
    assert isinstance(verifier, EscalatingVerifier)


def test_build_forced_on_overrides_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    verifier = build_escalating_verifier(
        port=FakePort([]), ledger=ModelCallLedger(), enabled=True)
    assert isinstance(verifier, EscalatingVerifier)


# ── §E.6 accounting: one escalation = one ledger row ───────────────────────

def test_one_escalation_lands_exactly_one_ledger_row():
    ledger = ModelCallLedger()
    verifier, _ = _escalation([_reply("changed")], ledger=ledger)
    node = _action_node(desired={"post_liked": "true"})
    vr = _verify(verifier, node=node, after={"post_liked": "已点赞"})
    assert vr.passed is True
    counts = ledger.counts_by_role()
    assert counts.get("model_verifier") == 1


# ── §E.7 the sync caller may sit inside a running event loop ──────────────

def test_verify_inside_running_event_loop_uses_thread_shim():
    verifier, port = _escalation([_reply("changed", "loop-context ok")])
    node = _action_node(desired={"post_liked": "true"})

    async def _inside_loop():
        # a running event loop in THIS thread must not break the sync
        # verify() — the coroutine runs on a private thread's own loop
        return _verify(verifier, node=node, after={"post_liked": "已点赞"})

    vr = asyncio.run(_inside_loop())
    assert vr.passed is True
    assert len(port.calls) == 1


# ── §E.8 VERIFY nodes escalate symmetrically ──────────────────────────────

def test_verify_node_mismatch_escalates_too():
    verifier, _ = _escalation([_reply("changed", "可见证据：已收藏")])
    node = WorkflowNode(
        node_id="v9", kind=NodeKind.VERIFY, label="核对收藏状态",
        verification="post_bookmarked == true")
    vr = _verify(verifier, node=node,
                 after={"post_bookmarked": "已收藏"},
                 desired={"post_bookmarked": "true"})
    assert vr.passed is True
