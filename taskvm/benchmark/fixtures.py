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

W2 adds ``doc_handoff`` — a Drive-using task (third app). It is a single-app
single-step edit (move_file F1 parent: personal→shared), which makes it the
canonical undo target for the W2 rollback gate. The 2 W1 tasks stay for
regression + two-zone coverage.
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
class Checkpoint:
    """A governance milestone the user can advance to / roll back to.

    E17: a task's progress is modeled as a sequence of checkpoints. Each
    checkpoint carries a ``criterion`` — an ``expected_diff``-shaped dict the
    verifier can check against live canonical state to decide whether the
    milestone has been reached. This is what ``GovernanceInterpreter`` uses to
    infer advance/rollback subgoals (``set_milestone`` / ``rollback_to`` events)
    and what the VM5-property killtest reports as ``governance_checkpoint``.

    ``criterion`` is verifier-only GT (same no-leak boundary as ``expected_diff``):
    it is consumed by the verifier path, NEVER fed to the CUA prompt.
    """
    id: str                            # "C1" | "C2"  (stable label the driver/interpreter address)
    description: str = ""              # human-facing "已 like 帖子"
    criterion: dict = field(default_factory=dict)  # {app: {entity_id: {field: expected}}} OR {"_any_new_in": {...}}


@dataclass
class CanonicalTaskGraph:
    """The full GT for one task: seed state + the user edit + the GT bindings +
    what must change (expected_diff) + what must NOT (non_interference_set).

    E17 adds ``checkpoints`` — the governance milestone sequence (see
    ``Checkpoint``). It defaults to an empty list so all pre-E17 tasks
    (positional construction up to ``description``) keep working unchanged. """
    task_id: str
    goal: str
    seed_state: dict                   # {calendar: {events:[...]}, taskboard: {tasks:[...]}}
    user_edit: dict                    # {var_id, old, new}
    bindings: list[CanonicalBinding]   # GT var_id → entities/operators
    non_interference_set: list[tuple[str, str]]  # [(app, entity_id), ...] must NOT change
    expected_diff: dict                # {app: {entity_id: {field: expected_value}}}
    description: str = ""
    checkpoints: list[Checkpoint] = field(default_factory=list)  # E17 governance milestones (empty = single-step task)


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


# ── Task 3: doc_handoff (W2 — Drive, the third app; single-app single-step) ──
# A Drive-only edit: move the launch announcement doc F1 from the personal
# folder to the shared folder. One binding, one app, one op — the canonical
# undo target for the W2 rollback gate (undo_last → F1.parent reverts to
# "personal" via the real app's write API). Calendar+TaskBoard are seeded so the
# 3-app mount is exercised and appear in the two-zone UI, but are protected
# (non_interference_set) — only Drive F1.parent changes.
DOC_HANDOFF = CanonicalTaskGraph(
    task_id="doc_handoff",
    goal="把项目发布公告文档移到共享文件夹，供团队审阅。（当前文档在 personal 文件夹。）",
    seed_state={
        "calendar": {"events": [
            {"eid": "E1", "title": "项目发布会议", "date": "2026-08-14",
             "time": "14:00-15:00", "calendar": "work", "rsvp": "accepted"},
        ]},
        "taskboard": {"tasks": [
            {"tid": "T1", "title": "审阅发布公告", "status": "todo",
             "assignee": "Alex", "deadline": "2026-08-14", "depends_on": []},
        ]},
        "drive": {"files": [
            {"fid": "F1", "name": "发布公告.doc", "content": "v1", "parent": "personal",
             "owner": "Alex", "modified": "2026-08-12", "type": "doc"},
            {"fid": "F2", "name": "设计稿.png", "content": "", "parent": "shared",
             "owner": "Bo", "modified": "2026-08-10", "type": "image"},
            {"fid": "F3", "name": "会议纪要.doc", "content": "draft", "parent": "shared",
             "owner": "Cara", "modified": "2026-08-11", "type": "doc"},
        ]},
    },
    user_edit={"var_id": "launch_doc_location", "old": "personal", "new": "shared"},
    bindings=[
        CanonicalBinding("launch_doc_location", "drive", "F1", "parent",
                         "move_file", "shared"),
    ],
    non_interference_set=[("drive", "F2"), ("drive", "F3"),
                          ("calendar", "E1"), ("taskboard", "T1")],
    expected_diff={
        "drive": {"F1": {"parent": "shared"}},
    },
    description="User moves launch doc F1 personal→shared folder; only "
                "Drive F1.parent changes; F2/F3/E1/T1 untouched. Single-app "
                "single-step → the W2 rollback gate's canonical undo target.",
)


# ── Task 4: launch_full (EE.2 — 4-App fanout: calendar+taskboard+drive+mail) ──
# 1 var_id release_date → 5 bindings across 4 apps (the §2 "项目发布" scenario
# fully realized). This is VM property 2 (bidirectional executable binding: one
# variable fans out to N heterogeneous apps) core evidence + the §4.4 four-step
# arc's Step 1 fixture. E2/T3/F2/M2 are the non-interference set (unrelated to
# release_date → must not change). Two checkpoints split the fanout into
# "meeting+tasks sync" (C1) and "doc+mail sync" (C2) for the governance arc.
LAUNCH_FULL = CanonicalTaskGraph(
    task_id="launch_full",
    goal="把项目发布日期从8/14推迟到8/18：日历会议同步移动，任务截止日同步延期，"
         "Drive发布文档的publish_date同步更新，Mail发布公告的send_date同步更新。",
    seed_state={
        "calendar": {"events": [
            {"eid": "E1", "title": "项目发布会议", "date": "2026-08-14",
             "time": "14:00-15:00", "calendar": "work", "rsvp": "accepted"},
            {"eid": "E2", "title": "周会", "date": "2026-08-12",
             "time": "10:00-10:30", "calendar": "work", "rsvp": "accepted"},
        ]},
        "taskboard": {"tasks": [
            {"tid": "T1", "title": "最终检查演示文档", "status": "todo",
             "assignee": "Alex", "deadline": "2026-08-14", "depends_on": ["release_date"]},
            {"tid": "T2", "title": "确认发布公告", "status": "todo",
             "assignee": "Bo", "deadline": "2026-08-14", "depends_on": ["release_date"]},
            {"tid": "T3", "title": "整理会议纪要", "status": "done",
             "assignee": "Cara", "deadline": "2026-08-10", "depends_on": []},
        ]},
        "drive": {"files": [
            {"fid": "F1", "name": "发布计划.doc", "content": "v1", "parent": "shared",
             "owner": "Alex", "modified": "2026-08-12", "type": "doc",
             "publish_date": "2026-08-14"},
            {"fid": "F2", "name": "设计稿.png", "content": "", "parent": "shared",
             "owner": "Bo", "modified": "2026-08-10", "type": "image",
             "publish_date": None},
        ]},
        "mail": {"messages": [
            {"mid": "M1", "subject": "项目发布公告", "from_addr": "pm@x.com",
             "to_addr": "team@x.com", "state": "draft", "received": "2026-08-12",
             "priority": "high", "send_date": "2026-08-14"},
            {"mid": "M2", "subject": "周报", "from_addr": "bo@x.com",
             "to_addr": "team@x.com", "state": "draft", "received": "2026-08-11",
             "priority": "normal", "send_date": None},
        ]},
    },
    user_edit={"var_id": "release_date", "old": "2026-08-14", "new": "2026-08-18"},
    bindings=[
        CanonicalBinding("release_date", "calendar",  "E1", "date",         "move_event",       "2026-08-18"),
        CanonicalBinding("release_date", "taskboard", "T1", "deadline",     "set_deadline",     "2026-08-18"),
        CanonicalBinding("release_date", "taskboard", "T2", "deadline",     "set_deadline",     "2026-08-18"),
        CanonicalBinding("release_date", "drive",     "F1", "publish_date", "set_publish_date", "2026-08-18"),
        CanonicalBinding("release_date", "mail",      "M1", "send_date",    "set_send_date",    "2026-08-18"),
    ],
    non_interference_set=[("calendar", "E2"), ("taskboard", "T3"),
                          ("drive", "F2"), ("mail", "M2")],
    expected_diff={
        "calendar":  {"E1": {"date": "2026-08-18"}},
        "taskboard": {"T1": {"deadline": "2026-08-18"},
                      "T2": {"deadline": "2026-08-18"}},
        "drive":     {"F1": {"publish_date": "2026-08-18"}},
        "mail":      {"M1": {"send_date": "2026-08-18"}},
    },
    description="EE.2: 1 var_id release_date → 4 App fanout (calendar+taskboard+"
                "drive+mail). VM property 2 (bidirectional executable binding) core "
                "evidence. 5 bindings, 4-App fanout, E2/T3/F2/M2 non-interference.",
    checkpoints=[
        Checkpoint("C1", "会议+任务同步",
                   {"calendar": {"E1": {"date": "2026-08-18"}},
                    "taskboard": {"T1": {"deadline": "2026-08-18"},
                                  "T2": {"deadline": "2026-08-18"}}}),
        Checkpoint("C2", "文档+邮件同步",
                   {"drive": {"F1": {"publish_date": "2026-08-18"}},
                    "mail": {"M1": {"send_date": "2026-08-18"}}}),
    ],
)


TASKS: dict[str, CanonicalTaskGraph] = {
    RELEASE_RESCHEDULE.task_id: RELEASE_RESCHEDULE,
    DESIGN_REVIEW_DELAY.task_id: DESIGN_REVIEW_DELAY,
    DOC_HANDOFF.task_id: DOC_HANDOFF,
    LAUNCH_FULL.task_id: LAUNCH_FULL,
}


def get_task(task_id: str) -> CanonicalTaskGraph:
    if task_id not in TASKS:
        raise KeyError(f"unknown task {task_id!r}; known: {list(TASKS)}")
    return TASKS[task_id]


def all_tasks() -> list[CanonicalTaskGraph]:
    return list(TASKS.values())
