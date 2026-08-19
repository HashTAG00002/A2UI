"""HttpModelPort — the concrete ModelPort adapter, stdlib ``urllib`` only.

The architect layer is stdlib-whitelisted by the architecture gate, so the
OpenAI-compatible chat call is implemented with ``urllib.request`` — no SDK.
Environment conventions are IDENTICAL to the
``taskvm/benchmark/model_client.py`` so ops do not change:

- ``OPENAI_BASE_URL`` (default ``https://aigc.sankuai.com/v1/openai/native``)
- ``OPENAI_API_KEY``
- ``TASKVM_MODEL`` (default ``gpt-5.6-sol``)

JSON extraction mirrors the client: strip ``<think>`` blocks and code
fences, then parse the first balanced ``{...}`` / ``[...]``. There is NO
port-level retry (single-owner contract): one ``complete_json`` call issues
exactly ONE provider request, so ledger records always equal real provider
calls — an unparseable reply returns ``parsed=None`` and the L4 semantic
repair loop (the single repair owner) decides whether to re-ask.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from taskvm.architect.port import ModelReply

DEFAULT_BASE_URL = "https://aigc.sankuai.com/v1/openai/native"
DEFAULT_MODEL = "gpt-5.6-sol"
_TIMEOUT_S = 120.0

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class HttpModelPortError(Exception):
    """Transport / HTTP failure — honest, carries the status or reason."""


class HttpModelPort:
    """Minimal OpenAI-compatible /chat/completions client (stdlib only)."""

    def __init__(self, *, base_url: str | None = None,
                 api_key: str | None = None,
                 default_model: str | None = None,
                 timeout_s: float = _TIMEOUT_S) -> None:
        self.base_url = (base_url or os.environ.get(
            "OPENAI_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.default_model = default_model or os.environ.get(
            "TASKVM_MODEL", DEFAULT_MODEL)
        self.timeout_s = timeout_s

    # ── ModelPort protocol ─────────────────────────────────────────────
    def complete_json(self, *, system: str, user: str,
                      model: str | None = None, max_tokens: int = 3072,
                      temperature: float | None = None,
                      image_data_url: str | None = None) -> ModelReply:
        """ONE provider request per call — no hidden retry.

        The ledger records one call per ``complete_json``; an internal
        parse-retry would make real provider calls exceed ledger records
        and under-report the benchmark's true model overhead. A parse
        failure returns ``parsed=None`` — the L4 semantic repair loop is
        the single repair owner.
        """
        mdl = model or self.default_model
        raw = self._chat(system, user, mdl, max_tokens, temperature,
                         image_data_url)
        # the provider's usage block IS the token
        # accounting the ledger contract requires. _chat already parsed it
        # off the response — carry it into the ModelReply so every ledger
        # record reports the true cost of its one real provider request.
        # Never fabricated: absent usage stays None (honest absence).
        pt, ct = self.last_usage
        return ModelReply(parsed=_extract_json(raw), raw=raw, model=mdl,
                          prompt_tokens=pt, completion_tokens=ct)

    # ── internals ──────────────────────────────────────────────────────
    def _chat(self, system: str, user: str, model: str, max_tokens: int,
              temperature: float | None, image_data_url: str | None) -> str:
        content: Any = user
        if image_data_url:
            content = [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}],
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            raise HttpModelPortError(
                f"model endpoint HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise HttpModelPortError(f"model endpoint unreachable: {e}") from e
        try:
            choice = payload["choices"][0]["message"]
            text = choice.get("content") or ""
            if isinstance(text, list):  # some gateways return segments
                text = "".join(seg.get("text", "") for seg in text
                               if isinstance(seg, dict))
            usage = payload.get("usage") or {}
            self._last_usage = (usage.get("prompt_tokens"),
                                usage.get("completion_tokens"))
            return _strip_think(text)
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise HttpModelPortError(
                f"malformed completion payload: {e}") from e

    @property
    def last_usage(self) -> tuple[int | None, int | None]:
        return getattr(self, "_last_usage", (None, None))


def _strip_think(s: str) -> str:
    return _THINK_RE.sub("", s or "").strip()


def _extract_json(text: str) -> Any:
    """First balanced {…} or […] in ``text``; None on failure."""
    if not text:
        return None
    t = re.sub(r"```(?:json)?\s*", "", text).strip()
    for start_ch, end_ch in (("{", "}"), ("[", "]")):
        start = t.find(start_ch)
        if start < 0:
            continue
        depth, in_str, esc = 0, False, False
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
                    except json.JSONDecodeError:
                        break
        # fall through to the other bracket kind
    return None
