"""StateCompiler — visible world → task variables + binding evidence.

The FIRST of the two high-level model roles (architect contract §1). One
call turns what a user could see on screen into the semantic task state the
kernel will govern. Fast/slow path discipline (handoff 04):

- **fast path, 0 model calls** — :meth:`StateCompiler.extract_observed`
  re-reads a known quantity by applying the deterministic ``value_pattern``
  minted at compile time; :meth:`StateCompiler.rebind` restores a handle by
  visible-label match after a local structural change.
- **slow path, 1 model call (+ ≤1 repair)** — a region's structure
  fingerprint changed, a handle's visible label vanished, or new task-
  relevant structure appeared. :meth:`StateCompiler.needs_slow_path` makes
  the routing decision deterministically; the CALLER decides to recompile.

Honesty rules: ``observed`` values only ever come from observation (the
model reads them off the visible text; the deterministic path re-reads
them mechanically). The compiler never invents ``desired`` — that is the
Task Architect's plane. Output keys are scanned for internal-id echoes
(:mod:`.noleak`); a hit is a repairable failure, never silently accepted.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from taskvm.architect.noleak import assert_prompt_clean, scan_json_values
from taskvm.architect.observation import (
    CompilerObservationView, HandleEvidence, VisibleRegion,
)
from taskvm.architect.port import (
    MODEL_ROLE_STATE_COMPILER, ModelCallLedger, ModelPort,
)
from taskvm.domain.errors import ValidationError
from taskvm.domain.intent import TaskIntent
from taskvm.domain.state import (
    MUTABILITY_EDITABLE, MUTABILITY_LOCKED, MUTABILITY_READONLY, ObservedValue,
    SurfaceEvidence, SurfaceHandle, TaskState, TaskVariable,
)

_VALID_MUTABILITY = (MUTABILITY_EDITABLE, MUTABILITY_READONLY, MUTABILITY_LOCKED)
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_SYSTEM_PROMPT = """\
You are the State Compiler of a Task Virtual Machine. You read ONLY what a \
real user could see on the rendered screen (window names, visible labels, \
visible values). From the observations, extract the task-relevant state as \
JSON with EXACTLY this shape:
{"variables": [{"semantic_key": "snake_case_business_key",
  "label": "human-visible name of the quantity",
  "value_type": "string|date|number|status|boolean",
  "mutability": "editable|readonly",
  "observed": <value read from the visible text, or null>,
  "confidence": <0.0-1.0>,
  "evidence": [{"surface_label": "<the Surface it was read from>",
    "visible_label": "<the on-screen string that names it, verbatim>",
    "visible_context": "<nearby visible text, verbatim>",
    "value_pattern": "<one regex with ONE capture group that re-reads this \
value from the surface's visible text>"}]}],
 "ambiguities": ["<one sentence each>"],
 "needs_clarification": false}
Rules:
- semantic_key expresses the BUSINESS meaning (e.g. release_date, reviewer), \
never an internal id, a database key, or an operator name.
- One variable per independently governable quantity; include every quantity \
relevant to the goal, not the whole screen.
- Evidence must QUOTE visible strings verbatim; never paraphrase ids you \
cannot see. If the goal cannot be grounded in what is visible, say so in \
ambiguities and set needs_clarification=true.
- Output ONLY the JSON object."""


class CompilerOutputError(ValidationError):
    """The model output failed structural assembly after bounded repair."""


@dataclass(frozen=True)
class SlowPathReport:
    """Deterministic fast/slow routing verdict (no model involved)."""

    needed: bool
    reason: str = ""
    changed_surfaces: tuple[str, ...] = ()
    lost_handles: tuple[str, ...] = ()

    def __bool__(self) -> bool:  # `if report:` reads naturally
        return self.needed


@dataclass(frozen=True)
class CompilerResult:
    """The compiled world: variables (observed plane) + handle knowledge."""

    variables: tuple[TaskVariable, ...] = ()
    handle_evidence: tuple[HandleEvidence, ...] = ()
    ambiguities: tuple[str, ...] = ()
    needs_clarification: bool = False
    revision: int = 0


class StateCompiler:
    """The visible-world → task-state compiler (one model call per slow path)."""

    def __init__(self, port: ModelPort, ledger: ModelCallLedger | None = None,
                 *, model: str | None = None, max_repairs: int = 1) -> None:
        self._port = port
        self._ledger = ledger
        self._model = model
        if max_repairs < 0:
            raise ValidationError("max_repairs must be >= 0")
        self._max_repairs = max_repairs

    # ── slow path: the one model call ───────────────────────────────────
    def compile(self, view: CompilerObservationView, intent: TaskIntent, *,
                revision: int = 0,
                prior_state: TaskState | None = None,
                purpose: str = "initial_compile") -> CompilerResult:
        """Visible observations + goal → variables/evidence/ambiguities.

        ``prior_state`` (optional): for an INCREMENTAL recompile after
        structural drift — the model sees the prior semantic keys/labels so
        it can keep, rename or drop them; observed values still come only
        from the new visible text.
        """
        user = self._build_user_prompt(view, intent, prior_state)
        assert_prompt_clean(_SYSTEM_PROMPT + "\n" + user,
                            what="state-compiler prompt")
        repair_note = ""
        last_err: Exception | None = None
        for attempt in range(1 + self._max_repairs):
            is_repair = attempt > 0
            reply = self._call_model(user + repair_note,
                                     purpose=purpose,
                                     is_repair=is_repair,
                                     revision=revision,
                                     image_data_url=self._first_screenshot(view))
            parsed = reply.parsed
            if not isinstance(parsed, dict) or "variables" not in parsed:
                last_err = CompilerOutputError(
                    "state-compiler output is not a JSON object with "
                    "'variables'")
                repair_note = self._repair_note(last_err)
                continue
            leaks = scan_json_values(parsed.get("variables"))
            if leaks:
                last_err = CompilerOutputError(
                    f"state-compiler output echoes internal vocabulary: "
                    f"{sorted(set(leaks))}")
                repair_note = self._repair_note(last_err)
                continue
            try:
                return self._assemble(parsed, view, revision)
            except ValidationError as e:
                last_err = e
                repair_note = self._repair_note(e)
        raise CompilerOutputError(
            f"state compiler failed after {1 + self._max_repairs} attempt(s); "
            f"last error: {last_err}")

    # ── fast path: deterministic, 0 model calls ─────────────────────────
    @staticmethod
    def extract_observed(view: CompilerObservationView,
                         handle: HandleEvidence) -> ObservedValue | None:
        """Re-read one known quantity by applying its ``value_pattern``.

        Returns None when the pattern is absent or no longer matches — the
        caller then routes to :meth:`needs_slow_path` (honest: a fast path
        that guesses would fabricate reality).
        """
        region = view.region(handle.surface_label)
        if region is None or not handle.value_pattern:
            return None
        try:
            m = re.search(handle.value_pattern, region.visible_text)
        except re.error:
            return None
        if m is None:
            return None
        value = m.group(1) if m.groups() else m.group(0)
        return ObservedValue(
            semantic_key=handle.semantic_key, value=value,
            evidence=(SurfaceEvidence(
                surface=handle.handle, visible_label=handle.visible_label,
                visible_context=handle.visible_context,
                observed_value=value),),
            confidence=1.0)

    @staticmethod
    def extract_batch(view: CompilerObservationView,
                      handles: tuple[HandleEvidence, ...],
                      ) -> tuple[ObservedValue, ...]:
        """Deterministic value sync for a handle set (0 model calls)."""
        out: list[ObservedValue] = []
        for h in handles:
            ov = StateCompiler.extract_observed(view, h)
            if ov is not None:
                out.append(ov)
        return tuple(out)

    @staticmethod
    def rebind(view: CompilerObservationView,
               handles: tuple[HandleEvidence, ...],
               ) -> tuple[tuple[HandleEvidence, ...], tuple[str, ...]]:
        """Deterministic rebind after a local structural change: keep every
        handle whose ``visible_label`` still occurs on its surface; report
        the lost ones (they need the slow path)."""
        kept: list[HandleEvidence] = []
        lost: list[str] = []
        for h in handles:
            region = view.region(h.surface_label)
            if region is not None and h.visible_label in region.visible_text:
                kept.append(h)
            else:
                lost.append(h.semantic_key)
        return tuple(kept), tuple(lost)

    @staticmethod
    def needs_slow_path(
            view: CompilerObservationView,
            previous_fingerprints: dict[str, str],
            handles: tuple[HandleEvidence, ...],
    ) -> SlowPathReport:
        """Fast/slow routing — deterministic, no model, no guessing.

        Slow path is required when: a known surface's fingerprint changed,
        a surface disappeared, a NEW surface appeared, or any handle's
        visible label can no longer be found on its surface.
        """
        current = view.fingerprints()
        changed = tuple(sorted(
            s for s, fp in previous_fingerprints.items()
            if s not in current or (fp and current[s] and current[s] != fp)))
        gone = tuple(sorted(
            s for s in previous_fingerprints if s not in current))
        new = tuple(sorted(
            s for s in current if s not in previous_fingerprints))
        lost: list[str] = []
        for h in handles:
            region = view.region(h.surface_label)
            if region is None or h.visible_label not in region.visible_text:
                lost.append(h.semantic_key)
        if changed or gone or new or lost:
            reasons = []
            if changed:
                reasons.append(f"structure changed on {list(changed)}")
            if gone:
                reasons.append(f"surface(s) gone: {list(gone)}")
            if new:
                reasons.append(f"new surface(s): {list(new)}")
            if lost:
                reasons.append(f"handle(s) lost: {sorted(set(lost))}")
            return SlowPathReport(True, "; ".join(reasons), changed, tuple(lost))
        return SlowPathReport(False)

    # ── internals ───────────────────────────────────────────────────────
    @staticmethod
    def _first_screenshot(view: CompilerObservationView) -> str | None:
        for r in view.regions:
            if r.screenshot_data_url:
                return r.screenshot_data_url
        return None

    def _call_model(self, user: str, *, purpose: str, is_repair: bool,
                    revision: int, image_data_url: str | None):
        if self._ledger is None:
            return self._port.complete_json(
                system=_SYSTEM_PROMPT, user=user, model=self._model,
                image_data_url=image_data_url)
        from taskvm.architect.port import ModelCallRecord
        t0 = time.monotonic()
        reply = None
        try:
            reply = self._port.complete_json(
                system=_SYSTEM_PROMPT, user=user, model=self._model,
                image_data_url=image_data_url)
            return reply
        finally:
            self._ledger.record(ModelCallRecord(
                role=MODEL_ROLE_STATE_COMPILER, purpose=purpose,
                model=(reply.model if reply else (self._model or "")),
                ok=reply is not None and reply.parsed is not None,
                is_repair=is_repair,
                prompt_tokens=(reply.prompt_tokens if reply else None),
                completion_tokens=(reply.completion_tokens if reply else None),
                latency_ms=int((time.monotonic() - t0) * 1000),
                revision=revision))

    @staticmethod
    def _build_user_prompt(view: CompilerObservationView, intent: TaskIntent,
                           prior_state: TaskState | None) -> str:
        parts = ["# Task goal", intent.goal]
        if intent.constraints:
            parts.append("Constraints: " + "; ".join(intent.constraints))
        if intent.scope:
            parts.append("Scope: " + "; ".join(intent.scope))
        if prior_state is not None and prior_state.variables:
            lines = ["", "# Previously compiled task variables (keep, rename "
                          "or drop as the visible world dictates; never "
                          "invent ids)"]
            for v in prior_state.variables:
                lines.append(f"- {v.semantic_key} ({v.label})")
            parts.append("\n".join(lines))
        parts += ["", "# Observed visible surfaces (read-only — compile from "
                      "these)", view.visible_digest()]
        parts.append("Compile the task state now. Output ONLY the JSON object.")
        return "\n".join(parts)

    @staticmethod
    def _repair_note(err: Exception) -> str:
        return (f"\n\nYour previous output was rejected: {err}. Fix it and "
                f"output the corrected JSON object only.")

    def _assemble(self, parsed: dict, view: CompilerObservationView,
                  revision: int) -> CompilerResult:
        raw_vars = parsed.get("variables")
        if not isinstance(raw_vars, list):
            raise CompilerOutputError("'variables' must be a JSON array")
        variables: list[TaskVariable] = []
        handles: list[HandleEvidence] = []
        seen: set[str] = set()
        regions = {r.surface_label for r in view.regions}
        for i, rv in enumerate(raw_vars):
            if not isinstance(rv, dict):
                raise CompilerOutputError(f"variable #{i} is not an object")
            key = str(rv.get("semantic_key") or "").strip()
            if not _KEY_RE.match(key):
                raise CompilerOutputError(
                    f"variable #{i} semantic_key {key!r} is not a lower "
                    f"snake_case business key")
            if key in seen:
                raise CompilerOutputError(f"duplicate semantic_key {key!r}")
            seen.add(key)
            label = str(rv.get("label") or key)
            value_type = str(rv.get("value_type") or "string")
            mutability = str(rv.get("mutability") or MUTABILITY_EDITABLE)
            if mutability not in _VALID_MUTABILITY:
                raise CompilerOutputError(
                    f"variable {key!r} mutability {mutability!r} unknown")
            confidence = rv.get("confidence", 1.0)
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 1.0
            confidence = min(max(confidence, 0.0), 1.0)
            observed = rv.get("observed")
            evidence_t: list[SurfaceEvidence] = []
            handle_ev: HandleEvidence | None = None
            evs = rv.get("evidence") or []
            if not isinstance(evs, list):
                raise CompilerOutputError(
                    f"variable {key!r} evidence must be an array")
            for j, ev in enumerate(evs):
                if not isinstance(ev, dict):
                    continue
                surface_label = str(ev.get("surface_label") or "")
                visible_label = str(ev.get("visible_label") or "")
                if not visible_label:
                    continue
                if surface_label and surface_label not in regions:
                    # the model cited a surface that does not exist — a
                    # hallucinated grounding; reject for repair, do not keep
                    raise CompilerOutputError(
                        f"variable {key!r} evidence cites unknown surface "
                        f"{surface_label!r} (visible surfaces: "
                        f"{sorted(regions)})")
                handle = SurfaceHandle(handle_id=f"h{len(handles) + 1:03d}")
                pattern = str(ev.get("value_pattern") or "")
                if pattern:  # validate the regex compiles + has 1 group
                    try:
                        compiled = re.compile(pattern)
                    except re.error as e:
                        raise CompilerOutputError(
                            f"variable {key!r} value_pattern does not "
                            f"compile: {e}") from e
                    if compiled.groups < 1:
                        raise CompilerOutputError(
                            f"variable {key!r} value_pattern needs exactly "
                            f"one capture group")
                evidence_t.append(SurfaceEvidence(
                    surface=handle, visible_label=visible_label,
                    visible_context=str(ev.get("visible_context") or ""),
                    observed_value=observed, confidence=confidence))
                if handle_ev is None:
                    handle_ev = HandleEvidence(
                        handle=handle, semantic_key=key,
                        surface_label=(surface_label
                                       or view.regions[0].surface_label),
                        visible_label=visible_label,
                        visible_context=str(ev.get("visible_context") or ""),
                        value_pattern=pattern, last_value=observed)
                    handles.append(handle_ev)
            variables.append(TaskVariable(
                semantic_key=key, label=label, observed=observed,
                desired=None, value_type=value_type,
                mutability=mutability, confidence=confidence,
                evidence=tuple(evidence_t)))
        ambiguities = tuple(
            str(a) for a in (parsed.get("ambiguities") or [])
            if isinstance(a, str))
        return CompilerResult(
            variables=tuple(variables),
            handle_evidence=tuple(handles),
            ambiguities=ambiguities,
            needs_clarification=bool(parsed.get("needs_clarification")),
            revision=revision)
