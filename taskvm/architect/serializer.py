"""ActionContractSerializer — deterministic CUA-goal serialisation.

Replaces the deleted LLM ``SubgoalGenerator`` (contract §6: "Action
contract 到 CUA instruction 使用确定性序列化；不为每个 patch 额外生成两个
自然语言候选"). ZERO model calls, ZERO candidates, ZERO mock flag — the same
contract always serialises to the same instruction (reproducibility is a
property of the harness, not of a language model's mood).

The serializer consumes ONLY substrate-neutral semantic content
(``ActionContract`` / ``CompensationEntry`` + visible labels). It scans its
own output with the no-leak gate before returning: a leak is an honest
sentinel failure, never silently stripped text.
"""
from __future__ import annotations

from typing import Any, Mapping

from taskvm.architect.noleak import PromptLeakError, scan
from taskvm.domain.contract import ActionContract, Reversibility
from taskvm.domain.patch import CompensationEntry

_REVERSIBILITY_TEXT = {
    Reversibility.REVERSIBLE: "reversible",
    Reversibility.PARTIALLY_REVERSIBLE: "partially reversible",
    Reversibility.IRREVERSIBLE: "irreversible",
}

_LEAK_SENTINEL = (
    'INTERNAL ERROR: instruction generation produced a leak — do not '
    'execute. Report {"action": "fail", "reason": "harness leak in '
    'instruction generation"}.')

_CANNOT_LOCATE = (
    "Unable to identify the target on screen by a visible title. Report "
    '{"action": "fail", "reason": "target not visible on the current '
    'page"}.')


class ActionContractSerializer:
    """Deterministic contract → CUA goal text (no model, no state)."""

    def cua_goal(self, contract: ActionContract,
                 labels: Mapping[str, str] | None = None,
                 *, attempt: int = 1) -> str:
        """Serialise one action contract into the CUA's goal instruction.

        ``labels`` maps semantic_key → business label (from the task
        variables); used so the CUA reads business names, never raw keys
        when a label exists.
        """
        labels = labels or {}
        parts: list[str] = []
        if attempt > 1:
            parts.append(f"Retry (attempt {attempt}): the previous attempt "
                         f"did not complete the change below — make sure to "
                         f"CONFIRM the change, not cancel.")
        target_bits = []
        for ev in contract.target_evidence:
            ctx = f" ({ev.visible_context})" if ev.visible_context else ""
            target_bits.append(f"'{ev.visible_label}'{ctx}")
        if target_bits:
            parts.append("Target (find it by what is written on screen): "
                         + "; ".join(target_bits) + ".")
        parts.append(f"Goal: {contract.semantic_goal}.")
        if contract.desired_state:
            kv = ", ".join(
                f"{labels.get(k, k)} = {v!r}"
                for k, v in contract.desired_state.items())
            parts.append(f"Set: {kv}.")
        if contract.completion_condition:
            parts.append(f"Done when: {contract.completion_condition}")
        if contract.reversibility is not Reversibility.REVERSIBLE:
            note = contract.risk_note or "this change may not be undoable"
            parts.append(f"CAUTION: this action is "
                         f"{_REVERSIBILITY_TEXT[contract.reversibility]} "
                         f"— {note}.")
        parts.append('Operate the visible UI like a user would (open the '
                     'target, edit the field, confirm). When the screen '
                     'reflects the target value, report success; if the UI '
                     'offers no way to achieve this, report failure with a '
                     'reason — never force an unrelated control.')
        text = "\n".join(parts)
        if scan(text):
            return _LEAK_SENTINEL
        return text

    def compensation_goal(self, entry: CompensationEntry,
                          labels: Mapping[str, str] | None = None) -> str:
        """Serialise one rollback entry — history-driven, deterministic.

        The kernel derived ``from_observed``/``to_observed`` from its own
        committed action history; the CUA goal simply names the quantity
        and the value reality must return to. No model "invents" reversal
        copy (contract §5: CompensationPatch 重新发明目标).
        """
        labels = labels or {}
        name = labels.get(entry.semantic_key, entry.semantic_key)
        text = (
            f"Restore '{name}': the visible value should return to "
            f"{entry.to_observed!r} (it currently reads "
            f"{entry.from_observed!r}). Operate the visible UI like a user "
            f"would to undo this specific change. When the screen shows the "
            f"restored value, report success; if the UI offers no way to "
            f"undo it, report failure with a reason — do not fake it.")
        if scan(text):
            return _LEAK_SENTINEL
        return text


# ── patch-op adapter (deterministic replacement for the deleted
#    governance.subgoal_generator.instruction_for_op — same visible-locator
#    inputs, but ZERO model calls and NO mock flag) ─────────────────────────
def patchop_cua_goal(*, surface_label: str, visible_locator: str | None,
                     field_display: str, target_value: Any,
                     restore: bool = False, attempt: int = 1) -> str:
    """One patch-op → goal instruction, deterministically.

    ``visible_locator`` None → the honest cannot-locate sentinel (the CUA
    fails rather than the harness fabricating an addressable id).
    """
    if visible_locator is None:
        return _CANNOT_LOCATE
    verb = "restore the previous value of" if restore else "change"
    target = ("its previous value" if restore
              else repr(target_value) if target_value is not None else "the intended value")
    lines = [f"On the {surface_label} view, find: {visible_locator}."]
    if attempt > 1:
        lines.append(f"This is retry attempt {attempt} — a previous attempt "
                     f"did not complete; make sure to CONFIRM, not cancel.")
    lines.append(f"Task: {verb} its {field_display} to {target}, using the "
                 f"page's own UI (open the item, edit, confirm).")
    lines.append(f"When the {field_display} shows the target value, report "
                 f"success; if the UI offers no way to do this, report "
                 f"failure with a reason.")
    text = "\n".join(lines)
    try:
        if scan(text):
            return _LEAK_SENTINEL
    except PromptLeakError:  # pragma: no cover — scan does not raise
        return _LEAK_SENTINEL
    return text
