"""Canonical task graphs — the hidden ground-truth (verifier-only).

Holds the GT binding (which task variable binds to which real app entity, with
which operator + expected post-edit value) + expected_diff + non_interference_set.
This is what the verifier compares the real post-edit state against.

**No-leak boundary (load-bearing)**: this module is imported ONLY by the
verifier path (``verifier/``) and the orchestrator (``evaluation/run_w1_killtest``).
It MUST NOT be imported by the compiler path (``task_state/``, ``execution/``).
The compiler sees only rendered observations (screenshot/DOM/a11y/tool-schema)
captured by ``harness/replay_engine``. The ``CanonicalBinding.operator`` field
(the var_id→operator mapping) is verifier-only GT; the compiler-visible
``OPERATOR_REGISTRY`` (in ``task_state/entity_binding.py``) carries ONLY operator
signatures, no var_ids. See W1 plan §deliverable 3 + Verification step 6.

W1 ships 2 tasks: ``release_reschedule`` + ``design_review_delay``. The 2-task
minimum is the sub-kill-3 defense ("only hand-written binding works → custom
dashboard, not a compiler").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CanonicalBinding:
    """One GT edge: a task variable bound to one real app entity, with the
    executable operator + the value the verifier expects to see after the edit."""
    var_id: str                       # "release_date"
    app: str                           # "calendar" | "taskboard"
    entity_id: str                     # "E1" | "T1"  (must appear in the DOM)
    field: str                         # "date" | "deadline" | "status" | "assignee"
    operator: str                      # "move_event" | "set_deadline" | ...  (verifier-only GT)
    expected_value_after_edit: Any     # GT the verifier checks against real post-state


@dataclass
class CanonicalTaskGraph:
    """The full GT for one task: seed state + the user edit + the GT bindings +
    what must change (expected_diff) + what must NOT (non_interference_set)."""
    task_id: str
    goal: str
    seed_state: dict                   # {calendar: {events:[...]}, taskboard: {tasks:[...]}}
    user_edit: dict                    # {var_id, old, new}
    bindings: list[CanonicalBinding]   # GT var_id → entities/operators
    non_interference_set: list[tuple[str, str]]  # [(app, entity_id), ...] must NOT change
    expected_diff: dict                # {app: {entity_id: {field: expected_value}}}
    description: str = ""


# ── Task 1: release_reschedule (the canonical doc example) ───────────────────
RELEASE_RESCHEDULE = CanonicalTaskGraph(
    task_id="release_reschedule",
    goal="帮我准备下周五的项目发布：安排发布会议、整理材料、创建剩余任务。"
         "（当前发布日期为 8/14；任务面板里依赖发布日期的截止日期已同步。）",
    seed_state={
        "calendar": {"events": [
            {"eid": "E1", "title": "项目发布会议", "date": "2026-08-14",
             "time": "14:00-15:00", "calendar": "work", "rsvp": "accepted"},
            {"eid": "E2", "title": "周会", "date": "2026-08-12",
             "time": "10:00-10:30", "calendar": "work", "rsvp": "accepted"},
            {"eid": "E7", "title": "牙医", "date": "2026-08-13",
             "time": "09:00-09:30", "calendar": "personal", "rsvp": "accepted"},
        ]},
        "taskboard": {"tasks": [
            {"tid": "T1", "title": "最终检查演示文档", "status": "todo",
             "assignee": "Alex", "deadline": "2026-08-14", "depends_on": ["release_date"]},
            {"tid": "T2", "title": "确认发布公告", "status": "todo",
             "assignee": "Bo", "deadline": "2026-08-14", "depends_on": ["release_date"]},
            {"tid": "T3", "title": "整理会议纪要", "status": "done",
             "assignee": "Cara", "deadline": "2026-08-10", "depends_on": []},
        ]},
    },
    user_edit={"var_id": "release_date", "old": "2026-08-14", "new": "2026-08-18"},
    bindings=[
        CanonicalBinding("release_date", "calendar",  "E1", "date",     "move_event",   "2026-08-18"),
        CanonicalBinding("release_date", "taskboard", "T1", "deadline", "set_deadline", "2026-08-18"),
        CanonicalBinding("release_date", "taskboard", "T2", "deadline", "set_deadline", "2026-08-18"),
    ],
    non_interference_set=[("calendar", "E2"), ("calendar", "E7"),
                          ("taskboard", "T3")],
    expected_diff={
        "calendar":  {"E1": {"date": "2026-08-18"}},
        "taskboard": {"T1": {"deadline": "2026-08-18"},
                      "T2": {"deadline": "2026-08-18"}},
    },
    description="User moves release_date 8/14→8/18; Calendar meeting E1 moves + "
                "TaskBoard tasks T1/T2 (whose deadlines depend on release_date) sync; "
                "E2/E7/T3 (unrelated) must not change.",
)


# ── Task 2: design_review_delay (different var + entities — sub-kill-3 defense) ─
DESIGN_REVIEW_DELAY = CanonicalTaskGraph(
    task_id="design_review_delay",
    goal="设计评审推迟两天（8/20 → 8/22），同步相关会议和依赖该日期的任务截止日期。",
    seed_state={
        "calendar": {"events": [
            {"eid": "E3", "title": "设计评审", "date": "2026-08-20",
             "time": "15:00-16:30", "calendar": "work", "rsvp": "accepted"},
            {"eid": "E1", "title": "项目发布会议", "date": "2026-08-14",
             "time": "14:00-15:00", "calendar": "work", "rsvp": "accepted"},
            {"eid": "E7", "title": "牙医", "date": "2026-08-13",
             "time": "09:00-09:30", "calendar": "personal", "rsvp": "accepted"},
        ]},
        "taskboard": {"tasks": [
            {"tid": "T4", "title": "设计文档定稿", "status": "todo",
             "assignee": "Dana", "deadline": "2026-08-20", "depends_on": ["design_review_date"]},
            {"tid": "T5", "title": "评审反馈整理", "status": "todo",
             "assignee": "Evan", "deadline": "2026-08-20", "depends_on": ["design_review_date"]},
            {"tid": "T1", "title": "最终检查演示文档", "status": "todo",
             "assignee": "Alex", "deadline": "2026-08-14", "depends_on": ["release_date"]},
        ]},
    },
    user_edit={"var_id": "design_review_date", "old": "2026-08-20", "new": "2026-08-22"},
    bindings=[
        CanonicalBinding("design_review_date", "calendar",  "E3", "date",     "move_event",   "2026-08-22"),
        CanonicalBinding("design_review_date", "taskboard", "T4", "deadline", "set_deadline", "2026-08-22"),
        CanonicalBinding("design_review_date", "taskboard", "T5", "deadline", "set_deadline", "2026-08-22"),
    ],
    non_interference_set=[("calendar", "E1"), ("calendar", "E7"),
                          ("taskboard", "T1")],
    expected_diff={
        "calendar":  {"E3": {"date": "2026-08-22"}},
        "taskboard": {"T4": {"deadline": "2026-08-22"},
                      "T5": {"deadline": "2026-08-22"}},
    },
    description="User moves design_review_date 8/20→8/22; Calendar E3 moves + "
                "TaskBoard T4/T5 sync; E1/E7/T1 (unrelated) must not change. "
                "Different var + entities than task 1 — tests compiler generalization.",
)


TASKS: dict[str, CanonicalTaskGraph] = {
    RELEASE_RESCHEDULE.task_id: RELEASE_RESCHEDULE,
    DESIGN_REVIEW_DELAY.task_id: DESIGN_REVIEW_DELAY,
}


def get_task(task_id: str) -> CanonicalTaskGraph:
    if task_id not in TASKS:
        raise KeyError(f"unknown task {task_id!r}; known: {list(TASKS)}")
    return TASKS[task_id]


def all_tasks() -> list[CanonicalTaskGraph]:
    return list(TASKS.values())
