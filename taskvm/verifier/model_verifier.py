"""taskvm.verifier.model_verifier — the model-based verifier (three-state).

PURETY-GEN §4.2: write-back verification is MODEL-based — a VLM reads the
FRESH observation (screenshot + scrubbed visible text) plus the business
intent and answers in an HONEST three-state verdict:

    changed        the screen's visible evidence shows the intent happened
    not_yet        the evidence shows it has NOT happened (e.g. the old
                   value is still on screen)
    cannot_verify  the visible evidence is insufficient to decide — the
                   explicit presentation of the MODEL's capability bound
                   (never a harness failure; the constraint lives on the
                   model side, not in harness-side rule enumeration)

Deterministic rule checks are DEMOTED to a cheap pre-filter (an injected
``prefilter`` callable, plus the built-in ``baseline_fingerprint``
short-circuit): a rule may only save a model call by proving the screen
did NOT change (``not_yet``); it may NEVER produce the final verdict for
``changed`` / ``cannot_verify`` — the model is the sole final judge.

Accounting: every REAL provider request lands exactly one ledger row with
``role="model_verifier"`` (registered in ``taskvm.architect.port.MODEL_ROLES``
— a protocol-constant append, not a scenario enumeration). This package is
pinned by the architecture gate to stdlib + ``taskvm.domain`` + the R2.5
skill loader (``taskvm.skills`` — a stdlib-only leaf; bench_design §17.2,
the R2.5 card as RFC), so it
talks to the port and the ledger STRUCTURALLY (duck-typed Protocols, same
call shapes as ``taskvm.architect.ModelPort`` / ``ModelCallLedger``) and
never imports the architect layer — mirroring how ``visible.VisibleVerifier``
satisfies the runtime's ``Verifier`` Protocol without importing
``taskvm.runtime``. ``ModelVerifierCallRecord`` is field-compatible with
``ModelCallRecord`` (so the same ledger instance, ``counts_by_role`` and
``annotate`` all work across layers).

The no-leak gate is INJECTED (``prompt_gate`` — composition passes
``taskvm.architect.noleak.assert_prompt_clean``): a leaking prompt issues
NO provider request and lands NO ledger row, and the verifier answers the
honest ``cannot_verify`` (same policy as the CUA adapter: rows count real
provider requests, not harness bugs).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from taskvm.skills.loader import inject_skill

# ── the three-state verdict protocol (frozen: PURETY-GEN §4.2) ──────────────

VERDICT_CHANGED = "changed"
VERDICT_NOT_YET = "not_yet"
VERDICT_CANNOT_VERIFY = "cannot_verify"
VERDICTS = (VERDICT_CHANGED, VERDICT_NOT_YET, VERDICT_CANNOT_VERIFY)

#: identical string to ``taskvm.architect.port.MODEL_ROLE_MODEL_VERIFIER`` so
#: the SAME ModelCallLedger instance accepts rows from both layers (same
#: pattern as ``taskvm.runtime.ports.MODEL_ROLE_CUA``).
MODEL_ROLE_MODEL_VERIFIER = "model_verifier"

_SYSTEM_PROMPT = (
    "你是一个界面验证员。你会看到一次写入操作后手机屏幕的最新截图（若"
    "提供）和清洗后的可见文本，以及一个用业务语言描述的验证意图。你只"
    "做一件事：判断屏幕上的可见证据是否表明该意图描述的变化已经发生。"
    "只依据屏幕上真实可见的内容判断，不要猜测不可见的内部状态。输出"
    "严格 JSON："
    '{"verdict":"changed|not_yet|cannot_verify","evidence":"引用屏幕上'
    '可见的证据"}'
    "\n三态含义：changed=可见证据表明变化已发生；not_yet=可见证据表明"
    "变化尚未发生（例如屏幕仍显示旧状态）；cannot_verify=屏幕证据不足"
    "以判定（诚实报告能力边界，不要编造证据）。evidence 必须引用屏幕"
    "上实际可见的文字或元素。"
)


@runtime_checkable
class _ModelPort(Protocol):
    """Structurally ``taskvm.architect.ModelPort`` (no import — gate)."""

    def complete_json(self, *, system: str, user: str,
                      model: str | None = None, max_tokens: int = 3072,
                      temperature: float | None = None,
                      image_data_url: str | None = None) -> Any: ...


@runtime_checkable
class _Ledger(Protocol):
    """Structurally ``taskvm.architect.ModelCallLedger`` (no import)."""

    def record(self, rec: Any) -> Any: ...


#: a deterministic pre-filter: ``(observation, intent) -> "not_yet" | None``.
#: ``None`` = cannot short-circuit, hand the decision to the model. A rule
#: pre-filter may NEVER answer ``changed`` / ``cannot_verify`` — any other
#: return value is ignored (the model stays the sole final judge).
Prefilter = Callable[[Any, str], "str | None"]

#: a no-leak gate: ``(text, what=...) -> None | raise`` (composition injects
#: ``taskvm.architect.noleak.assert_prompt_clean``).
PromptGate = Callable[..., None]


@dataclass(frozen=True)
class ModelVerifierCallRecord:
    """One landed (or failed) verifier model call — audit raw material.

    Field-compatible with ``taskvm.architect.port.ModelCallRecord`` (same
    names, same semantics) so the SAME ledger instance accepts and
    annotates verifier rows without the verifier package importing the
    architect layer (architecture gate: verifier = stdlib + taskvm.domain).
    """

    role: str = MODEL_ROLE_MODEL_VERIFIER
    purpose: str = "verifier.verify_intent"
    model: str = ""
    ok: bool = False
    is_repair: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    revision: int = 0
    error: str = ""
    request_id: str = ""
    node_id: str = ""
    attempt: int = 0


class ModelVerifier:
    """The model-based verifier — the sole FINAL judge of write-back
    verification (three-state), with deterministic rules demoted to an
    optional cheap pre-filter.

    Satisfies the bridge's injected-verifier contract structurally:
    ``async verify_intent(observation, intent) -> {"verdict", "evidence"}``
    (the shape ``bridge.mutate`` consumes). The model call itself runs in
    a worker thread (``asyncio.to_thread``) so a synchronous HTTP port
    never blocks the host event loop (the bridge is one aiohttp process).
    """

    #: Promise: this adapter owns its ledger rows (one provider request =
    #: one row on every path: success / unparseable / transport error).
    records_own_ledger = True

    def __init__(self, port: _ModelPort, ledger: _Ledger | None = None, *,
                 model: str | None = None,
                 prefilter: Prefilter | None = None,
                 prompt_gate: PromptGate | None = None) -> None:
        self._port = port
        self._ledger = ledger
        self._model = model
        self._prefilter = prefilter
        self._prompt_gate = prompt_gate
        self._requests = 0

    @property
    def request_count(self) -> int:
        """REAL provider requests issued through this verifier (must equal
        the number of ledger rows it owns)."""
        return self._requests

    # ── the three-state contract ──────────────────────────────────────────
    async def verify_intent(self, *, observation: Any, intent: str,
                            baseline_fingerprint: str | None = None
                            ) -> dict:
        """Judge ``intent`` against the FRESH ``observation``.

        Returns ``{"verdict": <one of VERDICTS>, "evidence": str}`` — the
        frozen three-state shape (``cannot_verify`` is an HONEST output,
        not a failure). Order of operations:

          1. cheap pre-filter (deterministic, ZERO model calls): the
             built-in fingerprint short-circuit and the injected
             ``prefilter`` may prove ``not_yet`` only — never a final
             ``changed``/``cannot_verify`` (rules cannot veto the model);
          2. compose the prompt from business language + scrubbed visible
             text; the screenshot travels as the multimodal image part
             (a real ``data:image/...`` URL only — never inlined as text);
          3. the injected no-leak gate runs BEFORE any provider request —
             a leak issues no request and lands no row;
          4. ONE provider request (worker thread) = ONE ledger row on
             every path; a malformed reply is the honest
             ``cannot_verify`` (model-side bound), still accounted.
        """
        intent = str(intent or "").strip()
        visible = str(getattr(observation, "visible_text", "") or "")
        # 1. cheap deterministic pre-filter — not_yet short-circuit only
        if baseline_fingerprint:
            fp = getattr(observation, "fingerprint", None)
            if fp and str(fp) == str(baseline_fingerprint):
                return {"verdict": VERDICT_NOT_YET,
                        "evidence": "屏幕可见结构与写入前完全一致"
                                    "（fingerprint 未变），变化未发生"}
        if self._prefilter is not None:
            try:
                rule = self._prefilter(observation, intent)
            except Exception:                     # a rule bug must not veto
                rule = None
            if rule == VERDICT_NOT_YET:           # the ONLY sanctioned answer
                return {"verdict": VERDICT_NOT_YET,
                        "evidence": "确定性预检判定变化未发生（未调用模型）"}
        # 2. compose the prompt (business language + visible text; the
        #    screenshot goes as the image part, never as prompt text)
        user = (f"## 验证意图\n{intent}\n\n## 屏幕可见文本\n{visible}")
        image = None
        ref = getattr(observation, "screenshot_ref", None)
        if isinstance(ref, str) and ref.startswith("data:image/"):
            image = ref
        # 3. no-leak gate (injected; runs BEFORE any provider request) —
        #    the skill injection (R2.5) happened above, so a distilled
        #    skill is scanned like any other prompt text
        if self._prompt_gate is not None:
            try:
                self._prompt_gate(
                    inject_skill("verifier", _SYSTEM_PROMPT) + "\n" + user,
                    what="verifier prompt")
            except Exception:
                return {"verdict": VERDICT_CANNOT_VERIFY,
                        "evidence": "指令生成内部错误，验证已安全终止"
                                    "（未发出模型请求）"}
        # 4. ONE provider request = ONE ledger row, on every path
        request_id = self._mint_request_id()
        t0 = time.monotonic()
        try:
            reply = await asyncio.to_thread(
                self._port.complete_json,
                system=inject_skill("verifier", _SYSTEM_PROMPT), user=user,
                model=self._model, image_data_url=image)
        except Exception as e:
            # infrastructure error — NOT a model capability bound; the row
            # still lands (rows count real provider requests) and the error
            # propagates honestly for the caller to surface.
            self._record(request_id, ok=False, model=self._model,
                         error=f"{type(e).__name__}: {e}",
                         latency_ms=int((time.monotonic() - t0) * 1000))
            raise
        parsed = reply.parsed if isinstance(reply.parsed, dict) else None
        if parsed is None:
            self._record(request_id, ok=False, model=self._model,
                         error="unparseable verifier reply", reply=reply,
                         latency_ms=int((time.monotonic() - t0) * 1000))
            return {"verdict": VERDICT_CANNOT_VERIFY,
                    "evidence": "模型返回无法解析，无法判定"}
        verdict = str(parsed.get("verdict", "")).strip()
        evidence = str(parsed.get("evidence", ""))[:500]
        if verdict not in VERDICTS:
            self._record(request_id, ok=False, model=self._model,
                         error=f"malformed verdict {verdict!r}", reply=reply,
                         latency_ms=int((time.monotonic() - t0) * 1000))
            return {"verdict": VERDICT_CANNOT_VERIFY,
                    "evidence": f"模型返回了无法识别的判定 {verdict!r}，"
                                f"按无法判定处理"}
        self._record(request_id, ok=True, model=self._model, reply=reply,
                     latency_ms=int((time.monotonic() - t0) * 1000))
        return {"verdict": verdict, "evidence": evidence}

    # ── ledger (structural ModelCallLedger compatibility) ─────────────────
    def _mint_request_id(self) -> str:
        self._requests += 1
        return f"verifier-{uuid.uuid4().hex[:16]}"

    def _record(self, request_id: str, *, ok: bool, model: str | None,
                error: str = "", reply: Any = None,
                latency_ms: int = 0) -> None:
        if self._ledger is None:
            return
        self._ledger.record(ModelVerifierCallRecord(
            model=str(model or getattr(self._port, "default_model", "")
                      or ""),
            ok=ok,
            prompt_tokens=getattr(reply, "prompt_tokens", None),
            completion_tokens=getattr(reply, "completion_tokens", None),
            latency_ms=latency_ms,
            error=error, request_id=request_id))
