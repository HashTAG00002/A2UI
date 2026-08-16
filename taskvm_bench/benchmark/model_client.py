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
             retries: int = 4) -> tuple[str, Any]:
    """Call the frontier model. ``messages`` = [{role, content}, ...].

    ``temperature`` defaults to None — many reasoning models on the proxy
    (gpt-5.5, etc.) reject any non-default temperature (400 unsupported_value),
    so we OMIT the param entirely when None. Pass an explicit float only for
    models known to accept it.

    Returns (content_text, raw_response) — the raw response is kept so the
    caller can extract ``.usage`` via ``record_usage``.
    """
    client = _get_client()
    mdl = model or TASKVM_DEFAULT_MODEL
    extra_body = None
    if thinking:
        # google thinking_config (Gemini-family on the proxy); harmless for others.
        extra_body = {"google": {"thinking_config": {"include_thoughts": True,
                             "thinking_budget": 128}, "thought_tag_marker": "think"}}
    last = None
    for attempt in range(retries):
        try:
            kwargs = dict(model=mdl, messages=messages, max_tokens=max_tokens)
            if temperature is not None:
                kwargs["temperature"] = temperature
            if extra_body:
                kwargs["extra_body"] = extra_body
            resp = client.chat.completions.create(**kwargs)
            return _strip_think(resp.choices[0].message.content or ""), resp
        except Exception as e:
            last = e
            # if the proxy rejects a non-default temperature, retry without it
            if temperature is not None and "temperature" in str(e).lower():
                logger.warning(f"[taskvm] temperature={temperature} rejected; retrying without it")
                temperature = None
                continue
            wait = min(2 ** attempt, 16)
            logger.warning(f"[taskvm] attempt {attempt + 1} failed: {e}; retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"taskvm complete failed after {retries} retries: {last}")


def complete_json(system: str, user: str, max_tokens: int = 2048,
                  temperature: Optional[float] = None, model: Optional[str] = None,
                  thinking: bool = False, repair_retries: int = 2):
    """Call + parse the first JSON object/array from the response.
    Returns (parsed, raw_text, raw_response). parsed is None on parse failure.
    On parse failure, re-prompts with a stricter "JSON only, no fences" repair.
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
        parsed = _parse_json(raw)
    return parsed, raw, resp


def complete_vision(messages, max_tokens: int = 2048,
                    temperature: Optional[float] = None,
                    model: Optional[str] = None, thinking: bool = False,
                    retries: int = 4) -> tuple[str, Any]:
    """Call the frontier model with image content blocks already in ``messages``
    (caller builds the [{type:"text",...},{type:"image_url",...}] content list).
    Returns (content_text, raw_response).
    """
    client = _get_client()
    mdl = model or TASKVM_DEFAULT_MODEL
    extra_body = None
    if thinking:
        extra_body = {"google": {"thinking_config": {"include_thoughts": True,
                             "thinking_budget": 128, "thought_tag_marker": "think"}}}
    last = None
    for attempt in range(retries):
        try:
            kwargs = dict(model=mdl, max_tokens=max_tokens, messages=messages)
            if temperature is not None:
                kwargs["temperature"] = temperature
            if extra_body:
                kwargs["extra_body"] = extra_body
            resp = client.chat.completions.create(**kwargs)
            return _strip_think(resp.choices[0].message.content or ""), resp
        except Exception as e:
            last = e
            if temperature is not None and "temperature" in str(e).lower():
                temperature = None
                continue
            wait = min(2 ** attempt, 16)
            logger.warning(f"[taskvm vision] attempt {attempt + 1} failed: {e}; retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"taskvm complete_vision failed after {retries} retries: {last}")


def complete_vision_json(system: str, user: str, img_data_url: str,
                         max_tokens: int = 2048,
                         temperature: Optional[float] = None,
                         model: Optional[str] = None, thinking: bool = False,
                         repair_retries: int = 2, detail: str = "high"):
    """Call frontier vision + parse the first JSON object/array. Returns
    (parsed, raw_text, raw_response); parsed None on parse failure (re-prompts
    ``repair_retries`` times with a stricter "JSON only" repair).
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
