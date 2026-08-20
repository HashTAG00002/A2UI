"""workspace_ui.verifier_escalation — deterministic-first runtime verifier
with model-based escalation (the R4 route; owner order 2026-08-20).

WHY THIS MODULE EXISTS (owner order, 2026-08-20): the per-app CJK label
vocabulary that briefly lived (uncommitted) in ``taskvm/verifier/
visible.py`` — a map of X-app toggle labels ("已点赞"/"已收藏"/...) to
canonical booleans — is DELETED. Per-app vocabulary is open-scenario
hardcoding, the exact enemy the repository contract forbids. The
replacement is the route R4 already landed (commit 6c9855d,
PURETY-GEN §4.2):

    Layer 1 (cheap pre-filter, ZERO model calls): the deterministic
      ``VisibleVerifier`` exact/contains match under GENERIC literal
      normalization (bool → "true"/"false", str.strip). A PASS
      short-circuits — the frozen runtime.md §6 contract keeps holding
      for every rule-decidable comparison.
    Layer 2 (final judge on mismatch): ``ModelVerifier`` (three-state
      changed / not_yet / cannot_verify) reads the FRESH observation
      (screenshot + scrubbed visible text) with a business-language
      intent built from the node contract. The model verdict OVERRULES
      the rule mismatch — rules may never veto the model
      (PURETY-GEN §4.2). ``changed`` ⇒ verification PASSES with the
      model's visible evidence in ``detail``; ``not_yet`` /
      ``cannot_verify`` ⇒ honest failure (the capability bound lives on
      the model side, never in harness-side vocabulary tables).

Env-gating (B-06 discipline): ``build_escalating_verifier`` returns the
plain ``VisibleVerifier`` unless a real model is configured
(``OPENAI_API_KEY``) — keyless unit tests and scripted runs never
impersonate model verdicts. Every escalation is ONE real provider
request = ONE ledger row with role ``model_verifier`` (owned by
``ModelVerifier`` itself); the injected no-leak gate runs before any
request.

Layer placement: this module is composition-layer assembly (workspace_ui
is ACTIVE) — it wires the two frozen verifier pieces together and
satisfies the runtime's ``Verifier`` Protocol structurally, exactly like
``VisibleVerifier`` does (no import of ``taskvm.runtime``).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
from typing import Any, Mapping

from taskvm.domain.results import VerificationResult
from taskvm.domain.workflow import WorkflowNode
from taskvm.verifier.model_verifier import VERDICT_CHANGED, ModelVerifier
from taskvm.verifier.visible import VisibleVerifier


class EscalatingVerifier:
    """Deterministic-first verifier; the model is the final judge of every
    rule-unresolvable comparison (R4 / PURETY-GEN §4.2 route).

    Satisfies the runtime ``Verifier`` Protocol structurally (same keyword
    signature as ``VisibleVerifier.verify``); composition injects it in
    place of the plain deterministic verifier. ``ModelVerifier`` (if
    wired) owns its own ledger rows and no-leak gating; this class adds
    none of its own.
    """

    def __init__(self, *, visible: VisibleVerifier | None = None,
                 model_verifier: ModelVerifier | None = None) -> None:
        self._visible = visible or VisibleVerifier()
        self._model = model_verifier

    def verify(self, *, node: WorkflowNode,
               before_observed: Mapping[str, Any],
               after_observed: Mapping[str, Any],
               desired: Mapping[str, Any],
               observation: Any,
               action_id: str | None,
               epoch: int) -> VerificationResult:
        base = self._visible.verify(
            node=node, before_observed=before_observed,
            after_observed=after_observed, desired=desired,
            observation=observation, action_id=action_id, epoch=epoch)
        if base.passed or self._model is None:
            return base
        # A deterministic mismatch is NOT the final word: rules cannot
        # resolve rendering-vocabulary gaps (e.g. a boolean contract
        # target vs the screen's CJK toggle label) — the model reads the
        # FRESH screen and judges. (R4: rules never veto the model.)
        intent = self._intent_text(node, desired)
        try:
            verdict = _run_async(self._model.verify_intent(
                observation=observation, intent=intent))
        except Exception as e:                     # infra error — honest
            return VerificationResult(
                node_id=node.node_id, epoch=epoch, passed=False,
                action_id=action_id, evidence_ref=base.evidence_ref,
                detail=(f"{base.detail}; model escalation unavailable "
                        f"({type(e).__name__}: {e}) — deterministic "
                        f"result stands"))
        v = str(verdict.get("verdict", ""))
        evidence = str(verdict.get("evidence", ""))[:300]
        if v == VERDICT_CHANGED:
            return VerificationResult(
                node_id=node.node_id, epoch=epoch, passed=True,
                action_id=action_id, evidence_ref=base.evidence_ref,
                detail=(f"model overruled rule mismatch "
                        f"(visible evidence: {evidence})"))
        return VerificationResult(
            node_id=node.node_id, epoch=epoch, passed=False,
            action_id=action_id, evidence_ref=base.evidence_ref,
            detail=(f"{base.detail}; model verdict {v}: {evidence}"))

    @staticmethod
    def _intent_text(node: WorkflowNode,
                     desired: Mapping[str, Any]) -> str:
        """Business-language verification intent built from the node
        contract.

        GUI-only: the node LABEL and the goal's variable names/values are
        public semantics (the same things the projection renders); NO
        node ids, surface ids or any internal vocabulary. The
        ModelVerifier runs the injected no-leak gate over this text
        regardless.
        """
        lines: list[str] = []
        label = str(getattr(node, "label", "") or "").strip()
        if label:
            lines.append(f"任务步骤「{label}」")
        contract = getattr(node, "contract", None)
        targets = dict(getattr(contract, "desired_state", None) or {})
        if targets:
            kv = "；".join(f"{k} 应达到 {v!r}" for k, v in targets.items())
            lines.append(f"期望状态：{kv}")
        cc = str(getattr(contract, "completion_condition", "") or "").strip()
        if cc:
            lines.append(f"完成条件：{cc}")
        pred = str(getattr(node, "verification", "") or "").strip()
        if pred:
            lines.append(f"验证谓词：{pred}")
        lines.append("请根据屏幕当前可见证据判断上述目标是否已经达成。")
        return "\n".join(lines)


def _run_async(coro: Any) -> Any:
    """Run a coroutine to completion from a synchronous caller.

    The runtime's verification loop is synchronous; ``ModelVerifier.
    verify_intent`` is async (built for the bridge's aiohttp context).
    When no event loop runs in this thread (the composition background
    thread — the normal case) ``asyncio.run`` is safe; if a loop IS
    running, the coroutine runs on a private thread's own loop (the
    port's HTTP call is synchronous and thread-agnostic, so this never
    deadlocks on the caller's loop).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def build_escalating_verifier(*, port: Any, ledger: Any,
                              enabled: bool | None = None) -> Any:
    """The composition default (owner order 2026-08-20): escalation ON
    when a real model is configured (``OPENAI_API_KEY`` — the same B-06
    env gate every real-model path uses), plain deterministic
    ``VisibleVerifier`` otherwise. An explicit ``enabled`` overrides the
    environment (tests inject fake ports and force the route on).
    """
    if enabled is None:
        enabled = bool(os.environ.get("OPENAI_API_KEY"))
    if not enabled:
        return VisibleVerifier()
    from taskvm.architect.noleak import assert_prompt_clean
    return EscalatingVerifier(model_verifier=ModelVerifier(
        port=port, ledger=ledger, prompt_gate=assert_prompt_clean))
