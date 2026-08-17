"""Frontier-model client for TaskVM (ported from SenseAct
``build/synthesize/_client.py`` + ``scripts/run_eval.py::_record_usage``).

OpenAI SDK against the Meituan aigc proxy (OpenAI-compatible). One shared
thread-safe client; retries with exponential backoff; strips ``<think>…</think>``
tags some proxies inject. ``complete_json`` / ``complete_vision_json`` parse the
first balanced JSON object/array and re-prompt on parse failure.

Generalized vs the SenseAct original: the model is a parameter (default
``TASKVM_DEFAULT_MODEL``), so the compiler can call a strong frontier model
(gpt-5.5 / gpt-5.6-sol / claude-sonnet-5) while bulk synthesis can call a
cheaper one. Two model roles (compiler vs compute-use) use the SAME client but
independent ``CostModel`` accumulators + independent calls (no shared context).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Meituan aigc endpoint (OpenAI-compatible) ─────────────────────────────────
BASE_URL = os.environ.get(
    "OPENAI_BASE_URL", "https://aigc.sankuai.com/v1/openai/native")
API_KEY = os.environ.get("OPENAI_API_KEY", "1925796454518841403")

# W1 default frontier model. Swap via env or the per-call ``model=`` arg.
# 2026-08-05 connectivity probe (this API key) — see docs §Appendix B:
#   gpt-5.5 / claude-sonnet-5 / claude-opus-5 / gpt-5.6-terra / gpt-5.6-luna /
#   kimi-k2.7-code all return 429 (per-model QPM quota ~exhausted for this app,
#   NOT an auth failure) even with 8s-spaced retries — unreliable for N-sample
#   concurrent kill-test runs. gpt-5.6-sol / gemini-3.6-flash / glm-5.2 /
#   glm-5v-turbo / aws.claude-sonnet-4.6 / aws.claude-opus-4.8 / deepseek-v4-* /
#   kimi-k3 / MiniMax-M3 / LongCat-2.0 all returned 200 on first call.
# Default picked accordingly: gpt-5.6-sol (already on the paper's frontier-model
# shortlist, no quota risk). Final "frontier model" wording decision for the
# paper is still a W1-close call (see W1 plan §deliverable 5) — swap freely.
TASKVM_DEFAULT_MODEL = os.environ.get("TASKVM_MODEL", "gpt-5.6-sol")

_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        logger.info(f"[taskvm] model client ready @ {BASE_URL} default={TASKVM_DEFAULT_MODEL}")
    return _client


# ── B-02: provider request journal + counter (bench-side accounting) ───────
#
# The prototype-side single-owner ledger (A-13, taskvm.architect.
# ModelCallLedger) does not reach into this bench client — its equivalent
# here is an append-only, thread-safe journal: ONE entry per REAL provider
# request (primary, every bounded-backoff retry, every explicit temperature
# downgrade, every upper-layer-initiated repair re-prompt), plus a plain
# request counter. Upper layers reconcile ``request_count()`` against their
# own CostModel/ledger so a hidden retry can never silently double-count.
_journal_lock = threading.Lock()
_journal: list = []               # capped, oldest dropped
_JOURNAL_CAP = 4000


def _note_request(**fields):
    """Append one REAL-request entry; returns its index (for ok/error
    back-patching once the response/exception is known)."""
    with _journal_lock:
        _journal.append(fields)
        if len(_journal) > _JOURNAL_CAP:
            del _journal[:len(_journal) - _JOURNAL_CAP]
        return len(_journal) - 1


def _patch_entry(idx, **fields):
    with _journal_lock:
        if 0 <= idx < len(_journal):
            _journal[idx].update(fields)


def request_count() -> int:
    """REAL provider requests issued through this module (B-02 invariant:
    equals the journal length; repair re-prompts and the downgrade retry
    each count as their own request)."""
    with _journal_lock:
        return len(_journal)


def journal_snapshot() -> list:
    """Copy of the request journal (oldest first) for run manifests."""
    with _journal_lock:
        return [dict(e) for e in _journal]


def reset_request_bookkeeping() -> None:
    """Test seam: clear counter + journal (never used by production)."""
    with _journal_lock:
        _journal.clear()


# ── B-02: provider error taxonomy ─────────────────────────────────────────

class ProviderFatalError(RuntimeError):
    """Non-retryable provider failure (401/402/403, any other explicit
    non-429 4xx, or a temperature rejection AFTER the one allowed
    downgrade). Subclasses RuntimeError so legacy ``except RuntimeError``
    callers keep working."""


_FATAL_STATUS = frozenset({401, 402, 403})


def _status_of(e) -> "int | None":
    # openai SDK: APIStatusError subclasses carry .status_code;
    # urllib HTTPError carries .code. Anything else → None (transport-ish).
    st = getattr(e, "status_code", None)
    if isinstance(st, int):
        return st
    code = getattr(e, "code", None)
    return code if isinstance(code, int) else None


def _classify(e, *, temperature_active: bool) -> str:
    """Map one provider exception to ``'downgrade' | 'fatal' | 'backoff'``.

    B-02 rules:
      * 401/402/403 → immediate fatal;
      * any other explicit non-429 4xx → fatal (400/404/422 won't heal);
      * 429 and 5xx → bounded exponential backoff;
      * no HTTP status (timeout / connection refused / DNS) → bounded
        transient retry;
      * an unsupported-temperature error while a temperature is still
        being sent → 'downgrade' (at most ONE explicit drop — the retry
        loop enforces never-twice).
    """
    status = _status_of(e)
    if temperature_active and status in (None, 400) and \
            "temperature" in str(e).lower():
        return "downgrade"
    if status is None:
        return "backoff"                    # transport / timeout transient
    if status in _FATAL_STATUS:
        return "fatal"
    if status == 429 or status >= 500:
        return "backoff"
    return "fatal"                          # explicit non-retryable 4xx


def _bounded_retry(send_once, *, temperature, retries: int, label: str):
    """The ONE retry skeleton shared by ``complete`` / ``complete_vision``.

    ``send_once(temperature)`` performs exactly ONE real provider request
    (journaled here). Taxonomy-driven: fatal raises immediately; 429/5xx/
    transport back off exponentially (``min(2**attempt, 16)``s) bounded by
    ``retries`` TOTAL attempts; an unsupported temperature drops the
    parameter at most ONCE and retries immediately (the slot still comes
    from the same bounded budget — never an extra hidden request).
    """
    temp = temperature
    downgraded_at = None                # attempt index that runs downgraded
    last = None
    attempts = max(1, retries)
    for attempt in range(attempts):
        idx = _note_request(
            label=label, attempt=attempt,
            phase="downgrade" if downgraded_at == attempt else "primary",
            temperature=temp)
        try:
            out = send_once(temp)
            _patch_entry(idx, ok=True)
            return out
        except Exception as e:
            last = e
            _patch_entry(idx, ok=False, error=str(e)[:200],
                         status=_status_of(e))
            verdict = _classify(e, temperature_active=temp is not None)
            if verdict == "downgrade":
                if downgraded_at is not None:
                    raise ProviderFatalError(
                        f"taskvm {label}: temperature rejected again after "
                        f"the one allowed downgrade: {e}") from e
                downgraded_at = attempt + 1
                logger.warning(
                    f"[taskvm] {label}: temperature={temp} unsupported — "
                    "ONE explicit downgrade, retrying without it")
                temp = None
                continue                    # immediate retry, no sleep
            if verdict == "fatal":
                raise ProviderFatalError(
                    f"taskvm {label}: non-retryable provider error "
                    f"(status={_status_of(e)}): {e}") from e
            if attempt < attempts - 1:
                wait = min(2 ** attempt, 16)
                logger.warning(
                    f"[taskvm] {label} attempt {attempt + 1} failed: {e}; "
                    f"retry in {wait}s")
                time.sleep(wait)
    raise RuntimeError(
        f"taskvm {label} failed after {attempts} retries: {last}")


# some proxies wrap reasoning in <think>…</think>; strip before JSON parse.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_think(s: str) -> str:
    return _THINK_RE.sub("", s or "").strip()


def _parse_json(text: str):
    """Parse the first balanced {…} or […] JSON object from text. None on fail."""
    if not text:
        return None
    t = re.sub(r"```(?:json)?\s*", "", text).strip()
    for start_ch, end_ch in (("{", "}"), ("[", "]")):
        start = t.find(start_ch)
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == start_ch:
                depth += 1
            elif ch == end_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except Exception:
                        break
    try:
        return json.loads(t)
    except Exception:
        return None


def complete(messages, max_tokens: int = 2048, temperature: Optional[float] = None,
             model: Optional[str] = None, thinking: bool = False,
             retries: int = 4) -> tuple:
    """Call the frontier model. ``messages`` = [{role, content}, ...].

    ``temperature`` defaults to None — many reasoning models on the proxy
    (gpt-5.5, etc.) reject any non-default temperature (400 unsupported_value),
    so we OMIT the param entirely when None. Pass an explicit float only for
    models known to accept it. An unsupported temperature is dropped at most
    ONCE (B-02 explicit downgrade, journaled — never a silent loop).

    B-02 error taxonomy (see ``_bounded_retry``): 401/402/403 and other
    explicit non-429 4xx raise ``ProviderFatalError`` immediately; 429 /
    5xx / transport-transient back off exponentially, bounded by
    ``retries`` total attempts. Every REAL provider request lands one
    journal entry (``request_count()``).

    Returns (content_text, raw_response) — the raw response is kept so the
    caller can extract ``.usage`` via ``record_usage``.
    """
    client = _get_client()
    mdl = model or TASKVM_DEFAULT_MODEL

    def send_once(temp):
        kwargs: dict = dict(model=mdl, messages=messages, max_tokens=max_tokens)
        if temp is not None:
            kwargs["temperature"] = temp
        if thinking:
            # google thinking_config (Gemini-family on the proxy); harmless for others.
            kwargs["extra_body"] = {"google": {"thinking_config": {
                "include_thoughts": True, "thinking_budget": 128},
                "thought_tag_marker": "think"}}
        resp = client.chat.completions.create(**kwargs)
        return _strip_think(resp.choices[0].message.content or ""), resp

    return _bounded_retry(send_once, temperature=temperature,
                          retries=retries, label="complete")


def complete_json(system: str, user: str, max_tokens: int = 2048,
                  temperature: Optional[float] = None, model: Optional[str] = None,
                  thinking: bool = False, repair_retries: int = 0):
    """Call + parse the first JSON object/array from the response.
    Returns (parsed, raw_text, raw_response). parsed is None on parse failure.

    B-02: ``repair_retries`` defaults to **0** — a parse failure NEVER
    triggers a hidden low-level re-request. Semantic JSON repair is owned
    by the explicit upper-layer orchestration (e.g.
    ``task_state/compiler.py`` passes ``repair_retries=1``); only that
    explicit instruction initiates the stricter re-prompt, and every
    repair re-prompt is its own REAL provider request, independently
    journaled (``request_count()`` / ``journal_snapshot()``).
    """
    sys_prompt = system + "\n\nRespond with ONLY valid JSON — no markdown fences, no prose."
    raw, resp = complete(
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": user}],
        max_tokens=max_tokens, temperature=temperature, model=model, thinking=thinking)
    parsed = _parse_json(raw)
    attempt = 0
    while parsed is None and attempt < repair_retries:
        attempt += 1
        repair = (f"Your previous response was not valid JSON: {raw[:200]!r}. "
                  "Output ONLY the JSON object now, starting with {, no fences, no prose.")
        raw, resp = complete(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": user},
             {"role": "assistant", "content": raw},
             {"role": "user", "content": repair}],
            max_tokens=max_tokens, temperature=None, model=model, thinking=thinking)
        # B-02: mark this as the upper-layer-initiated repair request in
        # the journal (its own REAL provider request, independently counted)
        _patch_entry(request_count() - 1, phase=f"repair{attempt}")
        parsed = _parse_json(raw)
    return parsed, raw, resp


def complete_vision(messages, max_tokens: int = 2048,
                    temperature: Optional[float] = None,
                    model: Optional[str] = None, thinking: bool = False,
                    retries: int = 4) -> tuple:
    """Call the frontier model with image content blocks already in ``messages``
    (caller builds the [{type:"text",...},{type:"image_url",...}] content list).
    B-02 retry/error taxonomy identical to ``complete`` (see there):
    401/402/403 + explicit non-429 4xx → immediate fatal; 429/5xx/transport
    → bounded exponential backoff; unsupported temperature → at most one
    explicit downgrade; every REAL request journaled.
    Returns (content_text, raw_response).
    """
    client = _get_client()
    mdl = model or TASKVM_DEFAULT_MODEL

    def send_once(temp):
        kwargs: dict = dict(model=mdl, max_tokens=max_tokens, messages=messages)
        if temp is not None:
            kwargs["temperature"] = temp
        if thinking:
            kwargs["extra_body"] = {"google": {"thinking_config": {
                "include_thoughts": True, "thinking_budget": 128,
                "thought_tag_marker": "think"}}}
        resp = client.chat.completions.create(**kwargs)
        return _strip_think(resp.choices[0].message.content or ""), resp

    return _bounded_retry(send_once, temperature=temperature,
                          retries=retries, label="vision")


def complete_vision_json(system: str, user: str, img_data_url: str,
                         max_tokens: int = 2048,
                         temperature: Optional[float] = None,
                         model: Optional[str] = None, thinking: bool = False,
                         repair_retries: int = 0, detail: str = "high"):
    """Call frontier vision + parse the first JSON object/array. Returns
    (parsed, raw_text, raw_response); parsed None on parse failure.

    B-02: ``repair_retries`` defaults to **0** — no hidden low-level
    re-request on a parse failure (same rule as ``complete_json``); an
    explicit upper-layer repair orchestration passes its own budget and
    every repair re-prompt is journaled as its own provider request.
    """
    sys_prompt = system + "\n\nRespond with ONLY valid JSON - no markdown fences, no prose."
    content = [{"type": "text", "text": sys_prompt + "\n\n" + user},
               {"type": "image_url",
                "image_url": {"url": img_data_url, "detail": detail}}]
    raw, resp = complete_vision(
        [{"role": "user", "content": content}],
        max_tokens=max_tokens, temperature=temperature, model=model, thinking=thinking)
    parsed = _parse_json(raw)
    attempt = 0
    while parsed is None and attempt < repair_retries:
        attempt += 1
        repair = (f"Your previous response was not valid JSON: {raw[:200]!r}. "
                  "Output ONLY the JSON object now, starting with {{, no fences, no prose.")
        content = [{"type": "text", "text": repair},
                   {"type": "image_url",
                    "image_url": {"url": img_data_url, "detail": detail}}]
        raw, resp = complete_vision(
            [{"role": "user", "content": content}],
            max_tokens=max_tokens, temperature=None, model=model, thinking=thinking)
        # B-02: journaled as the upper-layer-initiated repair request
        _patch_entry(request_count() - 1, phase=f"repair{attempt}")
        parsed = _parse_json(raw)
    return parsed, raw, resp


# ── usage extraction ───────────────────────────────────────────────────────
def record_usage(resp, cost_model, tool: Optional[str] = None,
                 role: Optional[str] = None,
                 model: Optional[str] = None) -> None:
    """Extract ``resp.usage`` (provider-agnostic getattr) into a ``CallUsage``
    on ``cost_model``. This is plain cost bookkeeping for the W1 report (total
    tokens spent) — not a research metric, so it only keeps what a report
    line needs: prompt/completion/reasoning/cached token counts.
    Handles OpenAI-style ``.usage`` + Anthropic/Gemini dict-fallback shapes.
    """
    from .cost_model import CallUsage

    if resp is None:
        return
    u = getattr(resp, "usage", None)
    if u is None:
        # some proxies return usage on the raw response dict
        u = resp.get("usage") if isinstance(resp, dict) else None
    if u is None:
        return
    pt = getattr(u, "prompt_tokens", 0) or (u.get("prompt_tokens", 0) if isinstance(u, dict) else 0) or 0
    ct = getattr(u, "completion_tokens", 0) or (u.get("completion_tokens", 0) if isinstance(u, dict) else 0) or 0
    cd = getattr(u, "completion_tokens_details", None)
    if cd is None and isinstance(u, dict):
        cd = u.get("completion_tokens_details")
    rt = getattr(cd, "reasoning_tokens", 0) if cd and not isinstance(cd, dict) else (cd.get("reasoning_tokens", 0) if cd else 0)
    pd = getattr(u, "prompt_tokens_details", None)
    if pd is None and isinstance(u, dict):
        pd = u.get("prompt_tokens_details")
    cached = getattr(u, "cached_tokens", 0) or 0
    if not cached and pd:
        cached = getattr(pd, "cached_tokens", 0) if not isinstance(pd, dict) else pd.get("cached_tokens", 0)
    cost_model.record_call(CallUsage(
        prompt_tokens=pt, completion_tokens=ct, reasoning_tokens=rt or 0,
        cached_tokens=cached or 0, tool=tool, model=model, role=role))


if __name__ == "__main__":
    # connectivity + parse sanity check
    txt, _ = complete([{"role": "user", "content":
                        "Reply with exactly one word: OK"}], max_tokens=20)
    print("text:", repr(txt))
    obj, raw, _ = complete_json(
        "You output JSON only.",
        'Return {"a": 1, "b": "two"} as JSON.', max_tokens=80)
    print("json:", obj, "| raw:", repr(raw)[:120])
