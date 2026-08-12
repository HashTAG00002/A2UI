"""``ScriptedUserDriver`` — the programmatic L4 implementation (E17-B).

Generates ``UserBehaviorEvent``s from a ``CanonicalTaskGraph`` (for
killtests/evaluation). The human driver (``HumanWebSocketDriver``) and this
driver are interchangeable — L3 cannot tell them apart (handoff §1.1 "无缝切换").

Two construction modes:
  1. AUTO (default): a single ``edit_field`` event from the fixture's
     ``user_edit`` (``{var_id, new}``). This is the minimal "advance to final
     state" sequence — what a plain round-trip killtest needs.
  2. EXPLICIT ``event_sequence``: a list of ``(event_type, payload)`` tuples,
     for tasks that exercise governance (rollback_to, multi-checkpoint flows).
     E.g. MG-2 ``expense_and_notify``: edit_field(V1) → checkpoint →
     rollback_to(C0) → edit_field(V2).

The driver also has a ``--dry-run`` CLI (handoff §5.3 step 3) that prints the
event sequence + the subgoals the interpreter would produce — for mock-mode
pipeline verification without MobileGym or a model.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from taskvm.benchmark.fixtures import CanonicalTaskGraph
from taskvm.benchmark.mobilegym_fixtures import get_mobilegym_task, all_mobilegym_tasks
from taskvm.benchmark.fixtures import get_task, all_tasks
from taskvm.governance.user_behavior_driver import UserBehaviorDriver, UserBehaviorEvent
from taskvm.governance.vm_state import VMStateSnapshot


class ScriptedUserDriver(UserBehaviorDriver):
    """Programmatic user-behavior driver — emits a fixed event sequence."""

    def __init__(self, task: CanonicalTaskGraph,
                 event_sequence: list[tuple[str, dict[str, Any]]] | None = None,
                 ) -> None:
        self.task = task
        if event_sequence is None:
            # AUTO: single edit_field from the fixture's user_edit
            ue = task.user_edit
            event_sequence = [("edit_field", {"var_id": ue.get("var_id", ""),
                                              "new_value": ue.get("new", "")})]
        # validate event types up front
        for et, _ in event_sequence:
            UserBehaviorEvent(et, {})  # raises on unknown type
        self._sequence: list[UserBehaviorEvent] = [
            UserBehaviorEvent(et, dict(pl)) for et, pl in event_sequence]
        self._idx = 0

    # ── UserBehaviorDriver interface ──────────────────────────────────────
    def next_event(self) -> UserBehaviorEvent | None:
        if self._idx >= len(self._sequence):
            return None
        ev = self._sequence[self._idx]
        self._idx += 1
        return ev

    def on_state_update(self, vm_state: VMStateSnapshot) -> None:
        # scripted driver does not react to state — no-op (the sequence is fixed)
        pass

    # ── helpers ───────────────────────────────────────────────────────────
    @property
    def event_sequence(self) -> list[UserBehaviorEvent]:
        return list(self._sequence)

    def reset(self) -> None:
        self._idx = 0


# ── canonical event sequences for the E17 tasks ────────────────────────────
def mg1_event_sequence() -> list[tuple[str, dict[str, Any]]]:
    """MG-1 social_morning_brief: like the CPI post, then send the wechat note.
    Two checkpoints: C1 (liked), C2 (messaged). Exercises rollback_to C1
    (un-like, reversible) vs the send_message honest-409. Uses TWO var_ids
    (morning_brief_liked + morning_brief_message) — see fixture docstring."""
    from taskvm.benchmark.mobilegym_fixtures import (
        MORNING_BRIEF_POST_TOKEN, MORNING_BRIEF_TEXT)
    return [
        # advance to C1: like the CPI post (identified by VISIBLE CONTENT token)
        ("edit_field", {"var_id": "morning_brief_liked",
                        "new_value": True,
                        "visible_token": MORNING_BRIEF_POST_TOKEN,
                        "target_checkpoint_id": "C1"}),
        # record C1
        ("checkpoint", {}),
        # advance to C2: send the wechat note
        ("edit_field", {"var_id": "morning_brief_message",
                        "new_value": MORNING_BRIEF_TEXT,
                        "contact_name": "黄勇",
                        "target_checkpoint_id": "C2"}),
    ]


def mg2_event_sequence() -> list[tuple[str, dict[str, Any]]]:
    """MG-2 expense_and_notify: send V1 → checkpoint C1 → rollback_to C0
    (honest 409) → resend V2 (C2). Exercises the reversibility spectrum."""
    from taskvm.benchmark.mobilegym_fixtures import EXPENSE_NOTIFY_V1, EXPENSE_NOTIFY_V2
    return [
        ("edit_field", {"var_id": "expense_summary",
                        "new_value": EXPENSE_NOTIFY_V1,
                        "target_checkpoint_id": "C1"}),
        ("checkpoint", {}),
        # rollback_to C0: attempt to undo the send → honest 409 (irreversible)
        ("rollback_to", {"target_checkpoint_id": "C0"}),
        # advance to C2: resend V2 (the resend succeeds — forward write path)
        ("edit_field", {"var_id": "expense_summary",
                        "new_value": EXPENSE_NOTIFY_V2,
                        "target_checkpoint_id": "C2"}),
    ]


def get_task_event_sequence(task_id: str) -> list[tuple[str, dict[str, Any]]] | None:
    """Return the canonical governance event sequence for a task, or None if
    the task has no special sequence (use AUTO single-edit)."""
    if task_id == "social_morning_brief":
        return mg1_event_sequence()
    if task_id == "expense_and_notify":
        return mg2_event_sequence()
    return None


def make_scripted_driver(task_id: str) -> ScriptedUserDriver:
    """Build a ScriptedUserDriver for a task, using its canonical governance
    event sequence if defined, else AUTO (single edit_field)."""
    # try mobilegym tasks first, then builtin tasks
    mg = all_mobilegym_tasks()
    if task_id in mg:
        task = mg[task_id]
    else:
        task = get_task(task_id)
    seq = get_task_event_sequence(task_id)
    return ScriptedUserDriver(task, event_sequence=seq)


# ── CLI: --dry-run for mock-mode pipeline verification (handoff §5.3 step 3) ─
def _dry_run(task_id: str) -> int:
    from taskvm.governance.governance_interpreter import GovernanceInterpreter
    driver = make_scripted_driver(task_id)
    interp = GovernanceInterpreter()
    print(f"=== ScriptedUserDriver dry-run: {task_id} ===")
    print(f"task goal: {driver.task.goal}")
    print(f"event sequence ({len(driver.event_sequence)} events):")
    out: list[dict] = []
    # a minimal empty VMStateSnapshot for dry-run (no adapters — interpret's
    # edit_field path only needs binding + task; rollback_to needs rollback_log
    # which we build empty so it reports "nothing to undo")
    from taskvm.execution.rollback import RollbackLog
    from taskvm.task_state.entity_binding import TaskBinding
    # build a minimal binding from the fixture's bindings (compiler-free)
    binding = _build_minimal_binding(driver.task)
    vm_state = VMStateSnapshot(
        sid="dryrun", binding=binding, adapters={}, rollback_log=RollbackLog(),
        checkpoints=driver.task.checkpoints)
    while True:
        ev = driver.next_event()
        if ev is None:
            break
        print(f"  - {ev.event_type} {ev.payload}")
        try:
            subgoals = interp.interpret(ev, vm_state, task=driver.task)
            for sg in subgoals:
                print(f"      → subgoal: {sg.natural_language[:100]}"
                      f"{'…' if len(sg.natural_language) > 100 else ''}")
                print(f"        patch_ops: {[op.to_dict() for op in sg.patch_ops]}")
                print(f"        criterion: {sg.verification_criterion}")
                if sg.manual_review_needed:
                    print(f"        [MANUAL REVIEW NEEDED — first-run LLM output]")
                out.append(sg.to_dict())
        except Exception as e:
            print(f"      [interpret error: {type(e).__name__}: {e}]")
            out.append({"error": f"{type(e).__name__}: {e}",
                        "event": ev.event_type})
    print(f"\n=== dry-run done: {len(out)} subgoals produced ===")
    print(json.dumps({"task_id": task_id, "subgoals": out},
                     ensure_ascii=False, indent=2))
    return 0


def _build_minimal_binding(task: CanonicalTaskGraph) -> "TaskBinding":
    """Build a minimal TaskBinding from a fixture's CanonicalBinding list,
    WITHOUT calling the frontier-model compiler (for dry-run only)."""
    from taskvm.task_state.entity_binding import TaskBinding
    variables: list[dict] = []
    seen: set[str] = set()
    for cb in task.bindings:
        if cb.var_id not in seen:
            variables.append({"var_id": cb.var_id, "label": cb.var_id,
                              "value": "", "editable": True, "bindings": []})
            seen.add(cb.var_id)
        v = variables[-1]
        v["bindings"].append({"var_id": cb.var_id, "app": cb.app,
                              "entity_id": cb.entity_id, "field": cb.field,
                              "operator": cb.operator})
    return TaskBinding(task_id=task.task_id, variables=variables)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="ScriptedUserDriver dry-run (mock-mode pipeline check)")
    parser.add_argument("--task", required=True,
                        help="task id (e.g. release_reschedule, social_morning_brief)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the event sequence + interpreted subgoals")
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error("only --dry-run is supported in this CLI")
    return _dry_run(args.task)


if __name__ == "__main__":
    sys.exit(main())
