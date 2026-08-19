"""decoder — the REAL model-backed A2UI component-tree generator (A4).

The decode loop (workplan §7-P3):

    TaskSurfaceContext
      → model call (structure only — every dynamic value a {"path"} binding)
      → validate_components (two-layer gate: protocol/catalog + policy)
      → ok   → install (composition root puts it in the SurfaceStore)
      → fail → ONE bounded repair retry (rejection reasons fed back verbatim)
      → fail → honest deterministic fallback (baseline_components), never a
               task-specific template, never silent: the result carries
               source="fallback" and a full attempt trail.

Dependency-injection contract (tests/genui/test_imports.py locks genui to
a plain-JSON port layer — it imports NOTHING from other taskvm layers):
the model port and the ledger are injected by the composition root. The
protocols below are structurally compatible with
``taskvm.architect.HttpModelPort`` / ``ModelCallLedger`` (the same
instance the architect layer uses — one unified call report; the
verifier layer's MODEL_ROLE_MODEL_VERIFIER precedent), so wiring is a
plain pass-through.

Ledger single-owner contract: every REAL provider request lands exactly
one row (role=genui_decoder, purpose=surface_compose/surface_repair,
is_repair on retries, honest ok=False + error on failures). The
fallback itself makes zero model calls → zero ledger rows, and is
recorded honestly in the DecodeResult attempt trail instead.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from taskvm.genui.baseline import baseline_components
from taskvm.genui.context import TaskSurfaceContext
from taskvm.genui.protocol import GENUI_DECODER_MODEL_ENV
from taskvm.genui.schema import catalog_prompt_summary
from taskvm.genui.validator import validate_components

logger = logging.getLogger("taskvm.genui.decoder")

#: Result provenance vocabulary.
SOURCE_MODEL = "model"          # a model attempt passed both layers
SOURCE_FALLBACK = "fallback"    # deterministic baseline (honest, visible)

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "prompts")
_DEFAULT_SYSTEM_PROMPT_FILE = os.path.join(_PROMPTS_DIR, "decoder_system.md")

_SYSTEM_PROMPT_CACHE: dict[str, str] = {}


# ── injected ports (duck-typed; architect-compatible) ──────────────────────

@runtime_checkable
class DecoderModelPort(Protocol):
    """One ``complete_json`` = ONE provider request (no internal retry).

    Structurally compatible with ``taskvm.architect.ModelPort``; the
    composition root injects the shared ``HttpModelPort`` instance.
    """

    def complete_json(self, *, system: str, user: str,
                      model: str | None = None, max_tokens: int = 3072,
                      temperature: float | None = None,
                      image_data_url: str | None = None) -> Any: ...


@runtime_checkable
class DecoderLedger(Protocol):
    """Append-only call accounting — structurally compatible with
    ``taskvm.architect.ModelCallLedger`` (record() only string-checks the
    role, so the SAME instance serves architect + runtime + genui)."""

    def record(self, rec: Any) -> Any: ...


@dataclass(frozen=True)
class DecoderCallRecord:
    """One landed (or failed) decoder model call — field-for-field
    compatible with ``taskvm.architect.ModelCallRecord`` so the shared
    ledger accepts it unchanged (audit + benchmark read one report)."""

    role: str                    # GENUI_DECODER_MODEL_ROLE
    purpose: str                 # surface_compose / surface_repair
    model: str
    ok: bool
    is_repair: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    revision: int = 0
    error: str = ""
    request_id: str = ""         # minted here (row single-owner)


# ── the decode result (honest attempt trail) ───────────────────────────────

@dataclass(frozen=True)
class DecodeAttempt:
    """One round of the loop. Model attempts carry the validation errors
    verbatim (what the repair round was fed); the fallback attempt marks
    the honest fallback event (model="" — zero model calls)."""

    index: int                   # 1-based round number
    ok: bool
    model: str = ""
    purpose: str = ""            # surface_compose / surface_repair /
                                 # surface_fallback
    errors: tuple[str, ...] = ()
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    request_id: str = ""


@dataclass(frozen=True)
class DecodeResult:
    components: list[dict[str, Any]]
    source: str                  # SOURCE_MODEL | SOURCE_FALLBACK
    attempts: tuple[DecodeAttempt, ...] = field(default_factory=tuple)

    @property
    def used_fallback(self) -> bool:
        return self.source == SOURCE_FALLBACK

    @property
    def model_calls(self) -> int:
        """Real provider requests issued (fallback adds zero)."""
        return sum(1 for a in self.attempts
                   if a.purpose != "surface_fallback")

    def summary(self) -> dict[str, Any]:
        """Machine-readable trail for ledgers / eval archives."""
        return {
            "source": self.source,
            "model_calls": self.model_calls,
            "component_count": len(self.components),
            "attempts": [
                {"index": a.index, "ok": a.ok, "model": a.model,
                 "purpose": a.purpose, "errors": list(a.errors),
                 "prompt_tokens": a.prompt_tokens,
                 "completion_tokens": a.completion_tokens,
                 "latency_ms": a.latency_ms, "request_id": a.request_id}
                for a in self.attempts
            ],
        }


# ── the decoder ─────────────────────────────────────────────────────────────

class GenUIDecoder:
    """Model-backed component-tree generator with bounded repair.

    Model routing (workplan §20.2): the decoder may run on a cheaper
    model — priority: constructor ``model`` arg > ``TASKVM_GENUI_DECODER_MODEL``
    env var > the port's own default. Whichever wins lands in every
    ledger row's ``model`` field (honest accounting).
    """

    def __init__(self, port: DecoderModelPort,
                 ledger: DecoderLedger | None = None, *,
                 model: str | None = None,
                 max_repairs: int = 1,
                 system_prompt: str | None = None,
                 max_tokens: int = 8192,
                 temperature: float | None = 0.2) -> None:
        if max_repairs < 0:
            raise ValueError("max_repairs must be >= 0")
        self._port = port
        self._ledger = ledger
        self._model = model
        self._max_repairs = max_repairs
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._system_prompt = (system_prompt if system_prompt is not None
                               else load_system_prompt())

    # ── the loop ───────────────────────────────────────────────────────
    def decode(self, context: TaskSurfaceContext, *,
               surface_id: str = "taskvm-task-decode",
               data_model: dict[str, Any] | None = None) -> DecodeResult:
        """Context → validated components (model or honest fallback)."""
        from taskvm.genui.protocol import GENUI_DECODER_MODEL_ROLE

        attempts: list[DecodeAttempt] = []
        user_prompt = self._user_prompt(context)

        for round_index in range(1, 2 + self._max_repairs):
            is_repair = round_index > 1
            purpose = "surface_repair" if is_repair else "surface_compose"
            model_id = self._resolve_model()
            request_id = uuid.uuid4().hex
            t0 = time.monotonic()
            reply: Any = None
            ok, error = True, ""
            try:
                reply = self._port.complete_json(
                    system=self._system_prompt, user=user_prompt,
                    model=model_id, max_tokens=self._max_tokens,
                    temperature=self._temperature)
            except Exception as exc:  # transport — honest failure row
                ok, error = False, f"{type(exc).__name__}: {exc}"

            if self._ledger is not None:
                self._ledger.record(DecoderCallRecord(
                    role=GENUI_DECODER_MODEL_ROLE, purpose=purpose,
                    model=(getattr(reply, "model", "") or ""
                           or (model_id or "")),
                    ok=ok, is_repair=is_repair,
                    prompt_tokens=getattr(reply, "prompt_tokens", None),
                    completion_tokens=getattr(reply, "completion_tokens",
                                              None),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    error=error, request_id=request_id))

            if not ok:
                errors = [f"model call failed: {error}"]
                attempts.append(DecodeAttempt(
                    index=round_index, ok=False, model=model_id or "?",
                    purpose=purpose, errors=tuple(errors),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    request_id=request_id))
            else:
                components = _coerce_components(
                    getattr(reply, "parsed", None))
                if components is None:
                    errors = ["reply did not parse as a JSON array of "
                              "component objects (or {\"components\": [...]})"]
                else:
                    errors = validate_components(
                        components, context, data_model,
                        surface_id=surface_id)
                attempts.append(DecodeAttempt(
                    index=round_index, ok=not errors, model=model_id or "?",
                    purpose=purpose, errors=tuple(errors),
                    prompt_tokens=getattr(reply, "prompt_tokens", None),
                    completion_tokens=getattr(reply, "completion_tokens",
                                              None),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    request_id=request_id))
                if not errors:
                    return DecodeResult(components=components,
                                        source=SOURCE_MODEL,
                                        attempts=tuple(attempts))
            user_prompt = self._repair_prompt(errors)

        # bounded repair exhausted → honest deterministic fallback
        components = baseline_components(context)
        attempts.append(DecodeAttempt(
            index=len(attempts) + 1, ok=True, model="",
            purpose="surface_fallback"))
        logger.warning(
            "genui decoder fell back to the baseline surface after %d "
            "model attempt(s); last errors: %s",
            len(attempts) - 1, "; ".join(attempts[-2].errors[:5]))
        return DecodeResult(components=components,
                            source=SOURCE_FALLBACK,
                            attempts=tuple(attempts))

    # ── prompts ───────────────────────────────────────────────────────
    def _user_prompt(self, context: TaskSurfaceContext) -> str:
        payload = json.dumps(context.to_payload(), ensure_ascii=False,
                             indent=2)
        return (
            "## TaskSurfaceContext — the task's public semantic snapshot\n"
            f"{payload}\n\n"
            "Generate the `updateComponents.components` list for this task "
            "surface now. Output ONLY the JSON array — no prose, no "
            "markdown fences."
        )

    def _repair_prompt(self, errors: list[str]) -> str:
        lines = "\n".join(f"- {e}" for e in errors)
        return (
            "Your previous component list was REJECTED by validation:\n"
            f"{lines}\n\n"
            "Regenerate the FULL corrected component list. Fix every "
            "rejection reason. Output ONLY the JSON array — no prose, no "
            "markdown fences."
        )

    # ── model routing (§20.2) ─────────────────────────────────────────
    def _resolve_model(self) -> str | None:
        if self._model:
            return self._model
        return os.environ.get(GENUI_DECODER_MODEL_ENV) or None


# ── helpers ─────────────────────────────────────────────────────────────────

def load_system_prompt(*, refresh: bool = False) -> str:
    """decoder_system.md + the catalog digest (cached per process).

    The directive file is the TaskVM-side contract; the component
    vocabulary + exact v0.9 binding/action syntax is injected
    programmatically from the vendored mirror (schema.catalog_prompt_summary)
    so the two can never drift apart.
    """
    cached = _SYSTEM_PROMPT_CACHE.get("system")
    if cached is not None and not refresh:
        return cached
    try:
        with open(_DEFAULT_SYSTEM_PROMPT_FILE, encoding="utf-8") as fh:
            directive = fh.read()
    except OSError as e:
        raise RuntimeError(
            "decoder system prompt missing: "
            f"{_DEFAULT_SYSTEM_PROMPT_FILE}") from e
    prompt = (f"{directive.rstrip()}\n\n---\n\n"
              f"{catalog_prompt_summary()}")
    _SYSTEM_PROMPT_CACHE["system"] = prompt
    return prompt


def _coerce_components(parsed: Any) -> list[dict[str, Any]] | None:
    """Accept a bare JSON array or a {\"components\": [...]} wrapper."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("components"),
                                                list):
        return parsed["components"]
    return None
