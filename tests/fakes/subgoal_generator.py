"""GG.3 SubgoalGenerator — LLM-generated goal-level subgoal instructions.

GG red-line §0 + §1.3: the instruction fed to the GUI executor (the grounding
model) must contain ZERO internal ids (entity_id like E1/T1/wxid_*) + ZERO
operator jargon (move_event/set_deadline/toggle_like/...). It must describe the
goal in terms a user sees on screen: the app, the visible locator (the entity's
title), the field's display name, and the target value. This replaces the two
hardcoded if/elif template builders (``_build_edit_nl`` in governance_interpreter
+ ``_build_instruction`` in gui_executor) — both leaked entity_id + used
per-operator templates with zero generalization (GG §1.3's exact condemnation).

**Two paths:**
  - **LLM path** (``mock=False``, default for live runs): calls ``gpt-5.6-sol``
    ``complete_json`` (non-vision, cheap) with a small prompt → 2 candidates →
    a light TTS pick (the longer one that still names the visible locator + the
    target value verbatim). Never rolls >2-3 times (user constraint).
  - **Mock path** (``mock=True``, default for dry-run/L0 tests): a deterministic
    template that composes the visible locator + field display + target value
    into a goal-level NL. ZERO internal ids by construction (it never touches
    entity_id). This keeps ``test_imports.py``'s dry-run tests + offline runs
    model-free, and it is itself GG-compliant (a honest, if less fluent, NL).

**No-leak guarantee**: every generated NL is scanned by
``translate.assert_no_internal_id`` + ``assert_no_operator_jargon`` before
return; a leak (should never happen given the inputs are clean) is an honest
FAIL (returns a sentinel + records the leak).

The visible locator is produced by ``translate.entity_id_to_locator`` upstream
(harness control-plane, reading the screen-visible title from canonical state).
If the locator is None (entity has no visible title — should not happen for
seeded entities), the generator returns an honest "cannot locate" NL rather than
fabricating an entity_id.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from taskvm.benchmark import model_client
from taskvm.execution.patch_compiler import PatchOp
from taskvm.governance.translate import (TITLE_FIELD, FIELD_DISPLAY, KIND_DISPLAY,
                                         entity_id_to_locator, assert_no_internal_id,
                                         assert_no_operator_jargon)

logger = logging.getLogger(__name__)

# the honest "cannot locate" sentinel — returned when the visible locator is
# None. The GUI executor will treat this as a fail (the model can't ground a
# target it can't name). NEVER falls back to拼接 entity_id.
CANNOT_LOCATE_NL = (
    'Unable to identify the target on screen by a visible title. Output '
    '{"action":"fail","reason":"target not visible on the current page"}.')

_N_LEAK_SENTINEL = (
    'INTERNAL ERROR: subgoal generation produced a leak — do not execute. '
    'Output {"action":"fail","reason":"harness leak in instruction generation"}.')


def _field_display(field: str) -> str:
    return FIELD_DISPLAY.get(field, field)


def _mock_subgoal(*, app: str, visible_locator: str, field_display: str,
                  target_value: Any, undo: bool, attempt: int) -> str:
    """Deterministic, model-free NL (the mock path). Zero internal ids by
    construction — it only composes visible-locator + field-display + value."""
    verb = "restore" if undo else "change"
    target = "its previous value" if undo else f"'{target_value}'"
    nl = (
        f"On this {app} page, {verb} {visible_locator}: set its {field_display} "
        f"to {target}. Use the page's UI — open the target's detail/edit view, "
        f"change the {field_display}, then confirm the change (click the "
        f"confirm/submit button in the review dialog, NOT cancel). "
        f'When the {field_display} reflects {target}, output {{"action":"done"}}. '
        f'If the UI offers no way to {verb} this, output '
        f'{{"action":"fail","reason":"..."}}.'
    )
    if attempt > 1:
        nl = (
            f"Previous attempt did not complete the {verb}. Try again — make "
            f"sure to click the CONFIRM/SUBMIT button (not cancel). " + nl)
    return nl


_SYS_PROMPT = (
    "You write a SHORT, goal-level natural-language instruction for a GUI agent "
    "that will execute it on a rendered app page by looking at the screen. The "
    "instruction MUST name the target entity ONLY by its visible on-screen title "
    "(given to you as a 'visible locator' — a human phrase like 'the event "
    "titled \"项目发布会议\"'). It MUST NOT contain any internal database id "
    "(like E1, T1, wxid_*) and MUST NOT use operator/API jargon (like "
    "move_event, set_deadline, toggle_like). Describe what a user would do: "
    "open the target's detail/edit view, change the named field to the target "
    "value, confirm. End with: output {\"action\":\"done\"} when the field "
    "reflects the new value, or {\"action\":\"fail\",\"reason\":\"...\"} if the "
    "UI offers no way. Output JSON: {\"instruction\":\"...\"}. Keep it under 4 "
    "sentences.")


def _llm_subgoal(*, app: str, visible_locator: str, field_display: str,
                 target_value: Any, undo: bool, attempt: int,
                 model: str | None) -> tuple[str, str]:
    """Call the LLM for 2 candidates + pick the more specific one (TTS).
    Returns (chosen_nl, reason). Never rolls >2 generations."""
    verb = "restore" if undo else "set"
    target = "its previous value" if undo else f"'{target_value}'"
    user = (
        f"App: {app}\nVisible target: {visible_locator}\n"
        f"Field to {verb}: {field_display}\nTarget value: {target}\n"
        f"Retry hint: {'previous attempt failed — emphasize clicking CONFIRM not cancel.' if attempt > 1 else 'none'}\n"
        f"Write the instruction.")
    candidates: list[str] = []
    for _ in range(2):
        try:
            parsed, _raw, _resp = model_client.complete_json(
                _SYS_PROMPT, user, max_tokens=512, temperature=None, model=model)
        except Exception as e:  # 429/timeout/parse — honest degrade to mock
            logger.warning(f"[subgoal] LLM call failed ({e}); degrading to mock")
            return _mock_subgoal(app=app, visible_locator=visible_locator,
                                 field_display=field_display,
                                 target_value=target_value, undo=undo,
                                 attempt=attempt), "llm_failed_degraded_to_mock"
        if isinstance(parsed, dict) and isinstance(parsed.get("instruction"), str):
            candidates.append(parsed["instruction"])
    if not candidates:
        return _mock_subgoal(app=app, visible_locator=visible_locator,
                             field_display=field_display,
                             target_value=target_value, undo=undo,
                             attempt=attempt), "llm_no_parse_degraded_to_mock"
    # TTS pick: prefer the candidate that names BOTH the visible locator's
    # distinctive text AND the target value verbatim (more specific = better
    # grounding). Tie-break by length (longer = more complete).
    loc_text = visible_locator
    val_text = str(target_value)
    def _score(c: str) -> tuple[int, int]:
        s = 0
        if loc_text and loc_text in c:
            s += 2
        if val_text and val_text in c:
            s += 2
        return (s, len(c))
    chosen = max(candidates, key=_score)
    reason = (f"tts_pick: loc_in={loc_text in chosen}, val_in={val_text in chosen}, "
              f"len={len(chosen)} (of {len(candidates)} candidates)")
    return chosen, reason


def generate_subgoal(*, app: str, visible_locator: str | None,
                     field: str, target_value: Any,
                     undo: bool = False, attempt: int = 1,
                     mock: bool = False, model: str | None = None,
                     ) -> tuple[str, dict]:
    """Generate a zero-internal-id goal-level NL instruction for one PatchOp.

    ``visible_locator``: the screen-visible locator phrase (from
    ``translate.entity_id_to_locator``), e.g. '标题为"项目发布会议"的会议'.
    None → returns the CANNOT_LOCATE sentinel (honest fail, no entity_id fabric).

    ``mock=True`` → deterministic path (no model call); ``mock=False`` → LLM.
    Returns (nl, meta) where meta records the path + TTS reason + no-leak scan.
    """
    meta: dict = {"app": app, "field": field, "undo": undo, "attempt": attempt,
                  "path": "mock" if mock else "llm", "model": model}
    if visible_locator is None:
        meta["reason"] = "no_visible_locator_cannot_locate"
        return CANNOT_LOCATE_NL, meta
    field_display = _field_display(field)
    if mock:
        nl = _mock_subgoal(app=app, visible_locator=visible_locator,
                           field_display=field_display,
                           target_value=target_value, undo=undo, attempt=attempt)
        meta["reason"] = "mock_deterministic"
    else:
        nl, reason = _llm_subgoal(app=app, visible_locator=visible_locator,
                                  field_display=field_display,
                                  target_value=target_value, undo=undo,
                                  attempt=attempt, model=model)
        meta["reason"] = reason
    # no-leak gate (defense in depth — inputs are clean, but verify the output)
    id_leaks = assert_no_internal_id(nl)
    jargon_leaks = assert_no_operator_jargon(nl)
    meta["no_leak_id_hits"] = id_leaks
    meta["no_leak_jargon_hits"] = jargon_leaks
    if id_leaks or jargon_leaks:
        logger.error(f"[subgoal] LEAK in generated NL: id={id_leaks} "
                     f"jargon={jargon_leaks}\nNL: {nl}")
        meta["reason"] = "LEAK_detected_sentinel_returned"
        return _N_LEAK_SENTINEL, meta
    return nl, meta


def instruction_for_op(op: PatchOp, canonical_entities: dict[str, dict[str, Any]],
                       *, undo: bool = False, attempt: int = 1,
                       mock: bool = False, model: str | None = None,
                       ) -> tuple[str, dict]:
    """Convenience: generate the instruction for one PatchOp by first translating
    its entity_id → visible locator (control-plane, from canonical state), then
    generating the NL. This is the drop-in replacement for ``_build_instruction``
    / ``_build_edit_nl``: the caller passes the op + the canonical entities, and
    gets back a zero-id NL + meta."""
    visible_locator = entity_id_to_locator(op.app, op.entity_id, canonical_entities,
                                           field=op.field)
    return generate_subgoal(app=op.app, visible_locator=visible_locator,
                            field=op.field, target_value=op.value,
                            undo=undo, attempt=attempt, mock=mock, model=model)
