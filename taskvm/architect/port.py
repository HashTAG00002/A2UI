"""ModelPort + ModelCallLedger — the ONLY way L4 touches a model.

The architect layer is deliberately free of any model SDK: it talks to a
:class:`ModelPort` protocol and records every call in a
:class:`ModelCallLedger`. The concrete HTTP adapter (:mod:`.http_port`, stdlib
``urllib`` — the gate whitelists stdlib only) honours the repo's existing
environment conventions (``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` /
``TASKVM_MODEL``).

Why the ledger exists (architect contract §5): the
benchmark must be able to distinguish **compiler calls / architect calls /
CUA calls** so the paper can honestly report the harness's model-call
overhead. Nothing in this layer may call a model without landing a record,
and no port may retry internally: **one ``complete_json`` = one provider
request = one ledger record** (single-owner contract).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

MODEL_ROLE_STATE_COMPILER = "state_compiler"
MODEL_ROLE_TASK_ARCHITECT = "task_architect"
MODEL_ROLE_CUA = "cua"
MODEL_ROLE_MODEL_VERIFIER = "model_verifier"
MODEL_ROLE_GENUI_DECODER = "genui_decoder"

MODEL_ROLES = (MODEL_ROLE_STATE_COMPILER, MODEL_ROLE_TASK_ARCHITECT,
               MODEL_ROLE_CUA, MODEL_ROLE_MODEL_VERIFIER,
               MODEL_ROLE_GENUI_DECODER)


@dataclass(frozen=True)
class ModelReply:
    """One completion: the parsed JSON (or None), raw text, and usage."""

    parsed: Any = None
    raw: str = ""
    model: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@runtime_checkable
class ModelPort(Protocol):
    """The port L4 consumes. ``complete_json`` returns a JSON-decoded reply.

    ``image_data_url`` (optional): a base64 data-URL screenshot — the vision
    path. There is NO port-level retry (single-owner contract): one call = one
    provider request = one ledger record, so the benchmark's reported
    model-call overhead is the true one. An unparseable reply returns
    ``parsed=None``; the L4 semantic repair loop is the single repair
    owner (each of ITS attempts lands its own ledger record with
    ``is_repair``).
    """

    def complete_json(self, *, system: str, user: str,
                      model: str | None = None, max_tokens: int = 3072,
                      temperature: float | None = None,
                      image_data_url: str | None = None) -> ModelReply: ...


@dataclass(frozen=True)
class ModelCallRecord:
    """One landed (or failed) high-level model call — audit raw material.

    ``request_id`` (single-owner): minted by the row's single owner — the
    transport/model adapter that actually issued the provider request. Every
    real provider request gets exactly one row carrying a fresh unique
    ``request_id``; downstream layers (runtime, evaluation) reference that
    row ONLY via ``Ledger.annotate`` (node_id / attempt / execution
    context) — never by appending a second row for the same request
    (single-owner: ``1 provider request = 1 ledger row``)."""

    role: str                    # one of MODEL_ROLES
    purpose: str                 # e.g. "initial_compose", "goal_recompose"
    model: str
    ok: bool
    is_repair: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    revision: int = 0            # task-state revision at call time (0 = n/a)
    error: str = ""
    request_id: str = ""         # unique id minted by the row's owner
    node_id: str = ""            # execution context, attached via annotate
    attempt: int = 0             # execution context, attached via annotate


class ModelCallLedger:
    """Thread-safe, append-only call accounting.

    ``counts_by_role()`` is what the benchmark reads to separate compiler /
    architect / CUA overhead. Records are immutable; the ledger never edits
    history. ``annotate`` (single-owner) is the single sanctioned mutation: it
    REPLACES a row in place (same request_id, same provider request) with
    execution context attached — it can never create or drop rows, so the
    ``provider request == ledger row`` invariant survives annotation.
    """

    #: fields ``annotate`` may set — everything else is owner-written once
    _ANNOTATABLE = frozenset({"purpose", "node_id", "attempt", "is_repair",
                              "revision"})

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[ModelCallRecord] = []

    def record(self, rec: ModelCallRecord) -> ModelCallRecord:
        if rec.role not in MODEL_ROLES:
            raise ValueError(
                f"unknown model role {rec.role!r}; use a MODEL_ROLE_* constant")
        with self._lock:
            if rec.request_id and any(
                    r.request_id == rec.request_id for r in self._records):
                raise ValueError(
                    f"duplicate request_id {rec.request_id!r}: one provider "
                    "request must map to exactly one ledger row (C-2)")
            self._records.append(rec)
        return rec

    def annotate(self, request_id: str, **fields: Any) -> ModelCallRecord | None:
        """Attach execution context to an existing row (single-owner contract).

        Only the annotation fields (``purpose`` / ``node_id`` / ``attempt`` /
        ``is_repair`` / ``revision``) may be set; unknown field names raise
        ``ValueError`` (a typo must never corrupt accounting). Returns the
        annotated record, or ``None`` when ``request_id`` is unknown — an
        honest no-op, never a new row."""
        unknown = set(fields) - self._ANNOTATABLE
        if unknown:
            raise ValueError(
                f"annotate() cannot set non-annotatable fields: {sorted(unknown)}")
        if not request_id:
            return None
        with self._lock:
            for i, r in enumerate(self._records):
                if r.request_id == request_id:
                    updated = replace(r, **fields) if fields else r
                    self._records[i] = updated
                    return updated
        return None

    @property
    def records(self) -> tuple[ModelCallRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def counts_by_role(self) -> dict[str, int]:
        out: dict[str, int] = {}
        with self._lock:
            for r in self._records:
                out[r.role] = out.get(r.role, 0) + 1
        return out

    def total(self) -> int:
        return len(self)

    def tokens_by_role(self) -> dict[str, tuple[int, int]]:
        """role -> (prompt_tokens_sum, completion_tokens_sum); None counts 0."""
        out: dict[str, tuple[int, int]] = {}
        with self._lock:
            for r in self._records:
                p, c = out.get(r.role, (0, 0))
                out[r.role] = (p + (r.prompt_tokens or 0),
                               c + (r.completion_tokens or 0))
        return out

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(vars(r)) for r in self._records]


class _LedgeredPort:
    """Mixin: wrap any ModelPort so every call lands in the ledger.

    ``role``/``purpose`` are per-callsite concerns, so callers pass them
    explicitly; the mixin only guarantees no unaccounted call can escape.
    """

    def __init__(self, inner: ModelPort, ledger: ModelCallLedger,
                 default_model: str) -> None:
        self._inner = inner
        self._ledger = ledger
        self._default_model = default_model

    def _call(self, *, role: str, purpose: str, revision: int = 0,
              is_repair: bool = False, **kw) -> ModelReply:
        t0 = time.monotonic()
        reply: ModelReply | None = None
        ok, error = True, ""
        try:
            reply = self._inner.complete_json(**kw)
            if reply.parsed is None:
                ok, error = False, "json_parse_failure"
            return reply
        except Exception as e:  # transport / HTTP — honest failure record
            ok, error = False, f"{type(e).__name__}: {e}"
            raise
        finally:
            self._ledger.record(ModelCallRecord(
                role=role, purpose=purpose,
                model=(reply.model if reply and reply.model
                       else (kw.get("model") or self._default_model)),
                ok=ok, is_repair=is_repair,
                prompt_tokens=(reply.prompt_tokens if reply else None),
                completion_tokens=(reply.completion_tokens if reply else None),
                latency_ms=int((time.monotonic() - t0) * 1000),
                revision=revision, error=error))
