"""intent_parser — free-text intent → structured governance intent (A6).

Handover A6: "用户自由文本意图 → 轻量模型解析为 GoalPatch / Patch /
RollbackIntent（治理核心灵活可变，不假定人只有枚举的几种意图）".
The user may phrase ANY governance wish in natural language; this
module maps it onto ONE structured intent — a small fast model call
(workplan §20.2, role=intent_parser) followed by DETERMINISTIC
structural + semantic validation. The parser never invents semantics:
a malformed, ambiguous or unvalidatable reply degrades to an honest
``clarify`` intent (a question back to the user), never a guess, never
a half-executed write.

The validation rule set is the SAME one the S2C policy layer and the
C2S ActionRouter run (``action_router.literal_type_error`` — one rule
set, three enforcement points): intent-driven local_patch updates must
address EDITABLE variables with type-correct literals, exactly like a
renderer action would.

Dependency-injection contract (tests/genui/test_imports.py locks genui
to a plain-JSON port layer): the model port and the ledger are INJECTED
by the composition root, structurally compatible with
``taskvm.architect.HttpModelPort`` / ``ModelCallLedger`` (the decoder
precedent). The intent's EXECUTION belongs to the composition root
(workspace_ui), which hands it to the session's governance port — the
parser only produces structured, validated data.

Ledger single-owner contract: every REAL provider request lands exactly
one row (role=intent_parser, purpose=intent_parse / intent_repair,
is_repair on retries, honest ok=False + error on failures). The clarify
fallback itself makes zero model calls → zero ledger rows, and is
recorded honestly in the ParsedIntent attempt trail instead.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from taskvm.genui.action_router import literal_type_error
from taskvm.genui.context import TaskSurfaceContext
from taskvm.genui.protocol import INTENT_PARSER_MODEL_ENV

logger = logging.getLogger("taskvm.genui.intent_parser")

#: Result provenance vocabulary (mirrors the decoder's).
SOURCE_MODEL = "model"          # a model reply parsed + validated
SOURCE_CLARIFY = "clarify"      # honest fallback: a question, never a guess

#: The structured intent kinds. ``clarify`` is the honest "could not
#: map this to a governance command" verdict; everything else maps
#: 1:1 onto a GovernancePortLike method.
INTENT_KINDS = ("local_patch", "goal_patch", "checkpoint", "rollback",
                "clarify")

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "prompts")
_DEFAULT_SYSTEM_PROMPT_FILE = os.path.join(_PROMPTS_DIR,
                                           "intent_parser_system.md")

_SYSTEM_PROMPT_CACHE: dict[str, str] = {}


# ── injected ports (duck-typed; architect-compatible) ──────────────────────


@runtime_checkable
class IntentModelPort(Protocol):
    """One ``complete_json`` = ONE provider request (no internal retry).
    Structurally compatible with ``taskvm.architect.ModelPort``; the
    composition root injects the shared ``HttpModelPort`` instance."""

    def complete_json(self, *, system: str, user: str,
                      model: str | None = None, max_tokens: int = 1024,
                      temperature: float | None = None,
                      image_data_url: str | None = None) -> Any: ...


@runtime_checkable
class IntentLedger(Protocol):
    """Append-only call accounting — structurally compatible with
    ``taskvm.architect.ModelCallLedger`` (record() only string-checks
    the role, so the SAME instance serves every role)."""

    def record(self, rec: Any) -> Any: ...


@dataclass(frozen=True)
class IntentCallRecord:
    """One landed (or failed) intent-parser model call — field-for-field
    compatible with ``taskvm.architect.ModelCallRecord`` so the shared
    ledger accepts it unchanged."""

    role: str                    # INTENT_PARSER_MODEL_ROLE
    purpose: str                 # intent_parse / intent_repair
    model: str
    ok: bool
    is_repair: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    revision: int = 0
    error: str = ""
    request_id: str = ""         # minted here (row single-owner)


# ── the parse result (honest attempt trail) ────────────────────────────────


@dataclass(frozen=True)
class IntentAttempt:
    """One round of the loop (mirrors the decoder's DecodeAttempt). The
    clarify fallback attempt marks the honest no-guess event (model=""
    — zero model calls)."""

    index: int                   # 1-based round number
    ok: bool
    model: str = ""
    purpose: str = ""            # intent_parse / intent_repair /
                                 # intent_clarify
    errors: tuple[str, ...] = ()
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    request_id: str = ""


@dataclass(frozen=True)
class ParsedIntent:
    """ONE structured governance intent (or the honest clarify). The
    execution half lives in the composition root: local_patch → the
    ActionRouter-validated governance write; goal_patch / checkpoint /
    rollback → the session's governance port. ``clarify`` executes
    NOTHING — it is a question back to the user."""

    kind: str                    # INTENT_KINDS
    updates: dict[str, Any] = field(default_factory=dict)
    goal: str = ""
    constraints: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    checkpoint_label: str = ""
    rationale: str = ""
    question: str = ""           # clarify only
    source: str = SOURCE_MODEL   # SOURCE_MODEL | SOURCE_CLARIFY
    attempts: tuple[IntentAttempt, ...] = field(default_factory=tuple)

    @property
    def is_clarify(self) -> bool:
        return self.kind == "clarify"

    def to_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "source": self.source}
        if self.kind == "local_patch":
            out["updates"] = dict(self.updates)
        if self.kind == "goal_patch":
            out.update({"goal": self.goal,
                        "constraints": list(self.constraints),
                        "scope": list(self.scope),
                        "success_criteria": list(self.success_criteria)})
        if self.kind in ("checkpoint", "rollback"):
            out["checkpoint_label"] = self.checkpoint_label
        if self.kind == "clarify":
            out["question"] = self.question
        if self.rationale:
            out["rationale"] = self.rationale
        return out

    def summary(self) -> dict[str, Any]:
        """Machine-readable trail for logs / eval archives."""
        return {**self.to_payload(),
                "attempts": [{"index": a.index, "ok": a.ok,
                              "model": a.model, "purpose": a.purpose,
                              "errors": list(a.errors),
                              "latency_ms": a.latency_ms,
                              "request_id": a.request_id}
                             for a in self.attempts]}


# ── the deterministic validation (structure + semantics) ───────────────────


def _str_list(value: Any, field_name: str, errors: list[str]) -> list[str]:
    out: list[str] = []
    if value is None:
        return out
    if not isinstance(value, list) or not all(
            isinstance(s, str) for s in value):
        errors.append(f"{field_name} must be a list of strings")
        return out
    return [s for s in value if s]


def validate_reply(payload: Any,
                   context: TaskSurfaceContext) -> tuple[str, list[str]]:
    """Model reply (parsed JSON object) → (kind, errors). Deterministic
    STRUCTURAL + SEMANTIC validation — the same ground truth the policy
    layer and the ActionRouter run. Returns the validated kind, or
    ``clarify`` with the rejection reasons when the reply does not
    honestly map onto a governance command."""
    if not isinstance(payload, dict):
        return "clarify", ["reply is not a JSON object"]
    kind = payload.get("kind")
    if kind not in INTENT_KINDS:
        return "clarify", [f"unknown intent kind {kind!r} "
                           f"(allowed: {list(INTENT_KINDS)})"]
    errors: list[str] = []
    rationale = payload.get("rationale") or ""
    if rationale and not isinstance(rationale, str):
        errors.append("rationale must be a string")

    if kind == "local_patch":
        updates = payload.get("updates")
        if not isinstance(updates, dict) or not updates:
            errors.append("local_patch requires a non-empty updates "
                          "object ({semantic_key: literal})")
        else:
            for key, value in updates.items():
                if not isinstance(key, str) or not key:
                    errors.append(f"updates key {key!r} is not a "
                                  "semantic_key string")
                    continue
                var = context.variable(key)
                if var is None:
                    errors.append(f"unknown semantic key {key!r}")
                    continue
                if not var.editable:
                    errors.append(f"variable {key!r} is {var.mutability} "
                                  "— only editable variables may be "
                                  "patched")
                    continue
                if isinstance(value, dict):
                    errors.append(f"updates[{key!r}] must be a literal, "
                                  f"not an object ({value!r})")
                    continue
                err = literal_type_error(var, value)
                if err is not None:
                    errors.append(err)

    elif kind == "goal_patch":
        goal = payload.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            errors.append("goal_patch requires a non-empty goal string")
        _str_list(payload.get("constraints"), "constraints", errors)
        _str_list(payload.get("scope"), "scope", errors)
        _str_list(payload.get("success_criteria"), "success_criteria",
                  errors)

    elif kind in ("checkpoint", "rollback"):
        label = payload.get("checkpoint_label")
        if not isinstance(label, str) or not label.strip():
            errors.append(f"{kind} requires a non-empty "
                          "checkpoint_label string")
        elif kind == "rollback":
            # the label must be a REAL checkpoint the user can see —
            # never an invented one (fail closed, no guess)
            known = {c.label for c in context.checkpoints}
            if label not in known:
                errors.append(
                    f"checkpoint {label!r} is not in the task's "
                    f"checkpoint list {sorted(known)}")

    elif kind == "clarify":
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            errors.append("clarify requires a non-empty question string")

    if errors:
        return "clarify", errors
    return kind, []


# ── the parser ──────────────────────────────────────────────────────────────


class IntentParser:
    """Free text → structured governance intent, with bounded repair
    and an honest clarify fallback.

    Model routing (workplan §20.2): the parser runs on the SMALL fast
    presentation model — priority: constructor ``model`` arg >
    ``TASKVM_INTENT_PARSER_MODEL`` env var > the port's own default.
    Whichever wins lands in every ledger row's ``model`` field.

    ``temperature`` defaults to ``None`` = NOT SENT (the FRIDAY-gateway
    constraint the decoder documents; callers who know their model
    supports it may still pass one).
    """

    def __init__(self, port: IntentModelPort,
                 ledger: IntentLedger | None = None, *,
                 model: str | None = None,
                 max_repairs: int = 1,
                 system_prompt: str | None = None,
                 max_tokens: int = 1024,
                 temperature: float | None = None) -> None:
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
    def parse(self, text: str, context: TaskSurfaceContext) -> ParsedIntent:
        """User free text + the task's public context → ONE structured
        intent (model-backed) or the honest clarify fallback (clarify
        executes nothing — never a guess)."""
        from taskvm.genui.protocol import INTENT_PARSER_MODEL_ROLE

        if not isinstance(text, str) or not text.strip():
            return self._clarify(attempts=[], reason="empty request text")
        attempts: list[IntentAttempt] = []
        user_prompt = self._user_prompt(text, context)

        for round_index in range(1, 2 + self._max_repairs):
            is_repair = round_index > 1
            purpose = "intent_repair" if is_repair else "intent_parse"
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
                self._ledger.record(IntentCallRecord(
                    role=INTENT_PARSER_MODEL_ROLE, purpose=purpose,
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
                attempts.append(IntentAttempt(
                    index=round_index, ok=False, model=model_id or "?",
                    purpose=purpose, errors=tuple(errors),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    request_id=request_id))
            else:
                payload = getattr(reply, "parsed", None)
                if not isinstance(payload, dict):
                    payload = _extract_object(
                        getattr(reply, "raw", "") or "")
                if payload is None:
                    kind, errors = "clarify", [
                        "reply did not parse as a JSON object"]
                else:
                    kind, errors = validate_reply(payload, context)
                attempts.append(IntentAttempt(
                    index=round_index, ok=not errors,
                    model=model_id or "?", purpose=purpose,
                    errors=tuple(errors),
                    prompt_tokens=getattr(reply, "prompt_tokens", None),
                    completion_tokens=getattr(
                        reply, "completion_tokens", None),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    request_id=request_id))
                if not errors:
                    return _intent_from_reply(payload, kind, attempts)
            user_prompt = self._repair_prompt(text, context, errors)

        # bounded repair exhausted → honest clarify (never a guess)
        logger.warning(
            "intent parser fell back to clarify after %d model "
            "attempt(s); last errors: %s",
            len(attempts), "; ".join(attempts[-1].errors[:5]) if attempts
            else "(none)")
        return self._clarify(attempts=attempts)

    # ── prompts ───────────────────────────────────────────────────────
    def _user_prompt(self, text: str,
                     context: TaskSurfaceContext) -> str:
        payload = json.dumps(context.to_payload(), ensure_ascii=False,
                             indent=2)
        return (
            "## TaskSurfaceContext — the task's public semantic snapshot\n"
            f"{payload}\n\n"
            "## The user's free-text request\n"
            f"{text.strip()}\n\n"
            "Parse it into exactly ONE structured intent now. Output "
            "ONLY the JSON object — no prose, no markdown fences."
        )

    def _repair_prompt(self, text: str, context: TaskSurfaceContext,
                       errors: list[str]) -> str:
        """The FULL original prompt + the verbatim rejection reasons
        (the context payload MUST ride along on the repair round too —
        the decoder's real-run lesson: a repair prompt without the
        payload makes the model regenerate from the error text alone)."""
        lines = "\n".join(f"- {e}" for e in errors)
        return (
            f"{self._user_prompt(text, context)}\n\n"
            "## Your previous attempt was REJECTED by validation\n"
            f"{lines}\n\n"
            "Regenerate the FULL corrected intent object. Fix every "
            "rejection reason. Output ONLY the JSON object — no prose, "
            "no markdown fences."
        )

    def _clarify(self, *, attempts: list[IntentAttempt],
                 reason: str = "") -> ParsedIntent:
        """The honest no-guess fallback: a question back to the user,
        with the attempt trail attached."""
        question = (reason or
                    "无法把这句话解析成确定的治理意图（改变量 / 改目标 / "
                    "存检查点 / 回滚）。请换一种说法，或直接使用面板控件。")
        attempts = list(attempts)
        attempts.append(IntentAttempt(
            index=len(attempts) + 1, ok=True, model="",
            purpose="intent_clarify"))
        return ParsedIntent(kind="clarify", question=question,
                            source=SOURCE_CLARIFY,
                            attempts=tuple(attempts))

    # ── model routing (§20.2) ─────────────────────────────────────────
    def _resolve_model(self) -> str | None:
        if self._model:
            return self._model
        return os.environ.get(INTENT_PARSER_MODEL_ENV) or None


# ── helpers ─────────────────────────────────────────────────────────────────


def load_system_prompt(*, refresh: bool = False) -> str:
    """intent_parser_system.md (cached per process) — the output
    contract + the no-guess rules the directive file carries."""
    cached = _SYSTEM_PROMPT_CACHE.get("system")
    if cached is not None and not refresh:
        return cached
    try:
        with open(_DEFAULT_SYSTEM_PROMPT_FILE, encoding="utf-8") as fh:
            prompt = fh.read()
    except OSError as e:
        raise RuntimeError(
            "intent parser system prompt missing: "
            f"{_DEFAULT_SYSTEM_PROMPT_FILE}") from e
    _SYSTEM_PROMPT_CACHE["system"] = prompt
    return prompt


def _intent_from_reply(payload: dict[str, Any], kind: str,
                       attempts: list[IntentAttempt]) -> ParsedIntent:
    """A VALIDATED reply → the ParsedIntent (validation already ran —
    this only lifts the fields)."""
    return ParsedIntent(
        kind=kind,
        updates=dict(payload.get("updates") or {})
        if kind == "local_patch" else {},
        goal=str(payload.get("goal") or "")
        if kind == "goal_patch" else "",
        constraints=tuple(payload.get("constraints") or ())
        if kind == "goal_patch" else (),
        scope=tuple(payload.get("scope") or ())
        if kind == "goal_patch" else (),
        success_criteria=tuple(payload.get("success_criteria") or ())
        if kind == "goal_patch" else (),
        checkpoint_label=str(payload.get("checkpoint_label") or "")
        if kind in ("checkpoint", "rollback") else "",
        rationale=str(payload.get("rationale") or ""),
        question=str(payload.get("question") or "")
        if kind == "clarify" else "",
        source=SOURCE_MODEL,
        attempts=tuple(attempts))


_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```")


def _extract_object(raw: str) -> dict[str, Any] | None:
    """First balanced {…} object from raw reply text (the shared port's
    dict-first generic extractor shape — fences tolerated). Returns
    None when nothing object-shaped parses; the repair / clarify loop
    owns that outcome."""
    if not raw:
        return None
    text = _FENCE_RE.sub("", raw).strip()
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
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
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None
