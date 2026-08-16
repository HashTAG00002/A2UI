"""OOD (held-out) canonical task graphs — the W4 kill-test ground-truth (verifier-only).

Holds the GT binding for the two held-out OOD tasks the W4 kill-test measures:
  1. ``send_launch_announcement`` — a **truly-unseen app** task (Mail). The model
     has never seen the ``mail`` app, its ``message`` entity kind, its
     ``data-mail-id`` DOM attribute (the A2UI compiler prompt names only
     calendar/taskboard/drive id attrs), or its ``set_state`` operator (which
     mutates a finite-state field, not a scalar). Tests raw OOD generalization.
  2. ``outlook_release_reschedule`` — a **reskin** task (Outlook_Cal, a renamed
     calendar). Same conceptual operation as ``release_reschedule`` (move the
     release meeting + sync dependent task deadlines) but on a renamed
     substrate (appointment/scheduled_for/reschedule_appointment). Tests
     substrate-independence: same op, new skin.

**No-leak boundary (load-bearing)**: same as ``fixtures.py`` — imported ONLY by
the verifier path + the orchestrator (``evaluation/run_ood_recon`` /
the legacy W4 entry, now deleted). MUST NOT be imported by the compiler path (``task_state/``,
``execution/``). The compiler sees only rendered observations.

These are MINIMAL recon-grade fixtures (2 tasks, ~2-3 bindings each) — enough to
get the first real OOD binding-F1 number today (handoff §1: the B-class OOD
recon is the highest-priority signal, and a 1-2 task / 3-sample probe suffices
to decide go/no-go before scaling to the full 40-template/800-instance bench).
The full benchmark generator (``benchmark/generator.py``) scales these into the
40-template/800-instance/OOD~20% bench later in the week.
"""
from __future__ import annotations

from dataclasses import dataclass

from taskvm.benchmark.fixtures import CanonicalBinding, CanonicalTaskGraph


# ── Task OOD-1: send_launch_announcement (truly-unseen Mail app) ──────────────
# The user wants to send the launch announcement NOW (it was scheduled) and also
# send the reminder that was still a draft. Both messages → "sent". This is a
# 2-binding task on the UNSEEN mail app via the novel set_state operator:
#   announcement_send_state → mail.M1.state (scheduled→sent) via set_state
#   announcement_send_state → mail.M3.state (draft→sent)     via set_state
# Non-interference: M2 (the weekly digest, unrelated) + every OTHER field of
# M1/M3 (subject/from/to/priority/received) must not change. A single new_value
# ("sent") applies to both bindings (compile_patch rule) — M1 goes scheduled→sent,
# M3 goes draft→sent, both valid state transitions.
SEND_LAUNCH_ANNOUNCEMENT = CanonicalTaskGraph(
    task_id="send_launch_announcement",
    goal="把项目发布公告邮件现在就发出去（原来定时发送），同时把还没写的提醒邮件也直接发出去。"
         "（公告 M1 原状态 scheduled，提醒 M3 原状态 draft，都改成 sent。）",
    seed_state={
        "mail": {"messages": [
            {"mid": "M1", "subject": "项目发布公告", "from_addr": "pm@x.com",
             "to_addr": "team@x.com", "state": "scheduled", "received": "2026-08-12",
             "priority": "high"},
            {"mid": "M2", "subject": "周报", "from_addr": "bo@x.com",
             "to_addr": "team@x.com", "state": "draft", "received": "2026-08-11",
             "priority": "normal"},
            {"mid": "M3", "subject": "发布提醒", "from_addr": "pm@x.com",
             "to_addr": "team@x.com", "state": "draft", "received": "2026-08-13",
             "priority": "high"},
        ]},
    },
    user_edit={"var_id": "announcement_send_state", "old": "scheduled", "new": "sent"},
    bindings=[
        CanonicalBinding("announcement_send_state", "mail", "M1", "state",
                         "set_state", "sent"),
        CanonicalBinding("announcement_send_state", "mail", "M3", "state",
                         "set_state", "sent"),
    ],
    non_interference_set=[("mail", "M2")],   # M2 (weekly digest) fully untouched
    expected_diff={
        "mail": {"M1": {"state": "sent"},
                 "M3": {"state": "sent"}},
    },
    description="User sends the scheduled launch announcement (M1 scheduled→sent) "
                "AND the draft reminder (M3 draft→sent) now — both via the UNSEEN "
                "mail app's set_state operator. M2 (weekly digest) untouched. "
                "OOD probe: can the compiler discover bindings on an app whose "
                "kind/operator/id-attr it has never seen? "
                "Note: field-level non-interference (only M1/M3.state changes, "
                "not their other fields) is NOT asserted by the W1 non-interference "
                "checker (it checks whole-entity-unchanged for the set); an FP "
                "binding that edits M1.priority would be caught by binding-F1 "
                "(precision drop), which is the gate signal here.",
)


# ── Task OOD-2: outlook_release_reschedule (reskin — Outlook_Cal + TaskBoard) ─
# The DIRECT analog of ``release_reschedule`` but on the reskinned calendar
# (outlook_cal: appointment/scheduled_for/reschedule_appointment instead of
# event/date/move_event). Cross-app: the release meeting moves in outlook_cal
# AND the dependent task deadlines sync in taskboard (a SEEN app). Tests:
#   - substrate-independence: discover the reskinned calendar binding
#     (outlook_cal.A1.scheduled_for via reschedule_appointment) under the new skin
#   - cross-app OOD: still correctly bind taskboard.T1/T2 (seen) alongside it
# 3 bindings, same new_value (2026-08-18) applies to all (compile_patch rule).
OUTLOOK_RELEASE_RESCHEDULE = CanonicalTaskGraph(
    task_id="outlook_release_reschedule",
    goal="帮我准备下周五的项目发布：在 Outlook 日历里安排发布会议、整理材料、创建剩余任务。"
         "（当前发布日期为 8/14；任务面板里依赖发布日期的截止日期已同步。现把发布日期推迟到 8/18。）",
    seed_state={
        "outlook_cal": {"appointments": [
            {"aid": "A1", "subject": "项目发布会议", "scheduled_for": "2026-08-14",
             "time": "14:00-15:00", "calendar": "work", "response": "accepted"},
            {"aid": "A2", "subject": "周会", "scheduled_for": "2026-08-12",
             "time": "10:00-10:30", "calendar": "work", "response": "accepted"},
            {"aid": "A7", "subject": "牙医", "scheduled_for": "2026-08-13",
             "time": "09:00-09:30", "calendar": "personal", "response": "accepted"},
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
        CanonicalBinding("release_date", "outlook_cal", "A1", "scheduled_for",
                         "reschedule_appointment", "2026-08-18"),
        CanonicalBinding("release_date", "taskboard", "T1", "deadline",
                         "set_deadline", "2026-08-18"),
        CanonicalBinding("release_date", "taskboard", "T2", "deadline",
                         "set_deadline", "2026-08-18"),
    ],
    non_interference_set=[("outlook_cal", "A2"), ("outlook_cal", "A7"),
                          ("taskboard", "T3")],
    expected_diff={
        "outlook_cal": {"A1": {"scheduled_for": "2026-08-18"}},
        "taskboard": {"T1": {"deadline": "2026-08-18"},
                      "T2": {"deadline": "2026-08-18"}},
    },
    description="User moves release_date 8/14→8/18; Outlook_Cal appointment A1 "
                "moves (via reschedule_appointment — the reskinned move_event) + "
                "TaskBoard T1/T2 deadlines sync (seen app). A2/A7/T3 untouched. "
                "OOD probe: substrate-independence — same conceptual op under a "
                "renamed skin (appointment/scheduled_for), plus cross-app binding.",
)


# ── Task OOD-3: set_mail_priorities (reverse-check — 2 DISTINCT var_ids) ──────
# The user sets TWO DIFFERENT priorities: announcement M1 → high, weekly digest
# M2 → low. These are semantically-independent quantities with DIFFERENT target
# values, so the GT requires TWO SEPARATE var_ids:
#   announcement_priority → mail.M1.priority (high)
#   digest_priority       → mail.M2.priority (low)
# If the var_id-granularity prompt heuristic (rule 8) is too aggressive and
# wrongly merges M1+M2 into one var_id (e.g. "mail_priority"), binding-F1 drops
# (the merged var_id matches neither GT var_id) → the regression signal.
#
# compile_patch applies ONLY the edited var's edit (user_edit targets
# announcement_priority=high); the round-trip therefore only changes M1.priority.
# The GATE signal here is binding_f1 (does the model discover BOTH separate
# var_ids?), not round-trip (which is intentionally partial — the second edit is
# a future user action). non_interference: M3 (reminder, untouched).
SET_MAIL_PRIORITIES = CanonicalTaskGraph(
    task_id="set_mail_priorities",
    goal="调整邮件优先级：把项目发布公告邮件 M1 的优先级设为 high，把周报邮件 M2 的优先级设为 low。"
         "（M3 提醒邮件保持 high 不变。两个优先级是独立调整的。）",
    seed_state={
        "mail": {"messages": [
            {"mid": "M1", "subject": "项目发布公告", "from_addr": "pm@x.com",
             "to_addr": "team@x.com", "state": "scheduled", "received": "2026-08-12",
             "priority": "normal"},
            {"mid": "M2", "subject": "周报", "from_addr": "bo@x.com",
             "to_addr": "team@x.com", "state": "draft", "received": "2026-08-11",
             "priority": "normal"},
            {"mid": "M3", "subject": "发布提醒", "from_addr": "pm@x.com",
             "to_addr": "team@x.com", "state": "draft", "received": "2026-08-13",
             "priority": "high"},
        ]},
    },
    user_edit={"var_id": "announcement_priority", "old": "normal", "new": "high"},
    bindings=[
        CanonicalBinding("announcement_priority", "mail", "M1", "priority",
                         "set_priority", "high"),
        CanonicalBinding("digest_priority", "mail", "M2", "priority",
                         "set_priority", "low"),
    ],
    non_interference_set=[("mail", "M3")],
    expected_diff={
        "mail": {"M1": {"priority": "high"}},   # only the edited var's binding
    },
    description="Reverse-check: 2 DISTINCT var_ids (announcement_priority=high, "
                "digest_priority=low) on the unseen mail app. Guards against "
                "over-merging from the var_id-granularity prompt heuristic — "
                "the model MUST keep them as separate var_ids (binding_f1 is the "
                "gate; round-trip is intentionally partial).",
)


OOD_TASKS: dict[str, CanonicalTaskGraph] = {
    SEND_LAUNCH_ANNOUNCEMENT.task_id: SEND_LAUNCH_ANNOUNCEMENT,
    OUTLOOK_RELEASE_RESCHEDULE.task_id: OUTLOOK_RELEASE_RESCHEDULE,
    SET_MAIL_PRIORITIES.task_id: SET_MAIL_PRIORITIES,
}


@dataclass
class OODCategory:
    """Which OOD category a held-out task belongs to (for the F1 breakdown)."""
    task_id: str
    category: str        # "unseen_app" | "reskin"
    description: str


OOD_CATEGORIES = {
    "send_launch_announcement": OODCategory(
        "send_launch_announcement", "unseen_app",
        "truly-unseen Mail app (message kind, data-mail-id, set_state operator)"),
    "outlook_release_reschedule": OODCategory(
        "outlook_release_reschedule", "reskin",
        "calendar reskin (appointment/scheduled_for/reschedule_appointment)"),
    # reverse-check: GT EXPLICITLY requires 2 separate var_ids (distinct target
    # values). If the var_id-granularity prompt heuristic (rule 8) causes
    # false-positive over-merging, this task's strict F1 drops — the regression
    # signal. Category is "unseen_app_reverse" so the verdict treats it as a
    # must-pass-high bar (the model MUST split here).
    "set_mail_priorities": OODCategory(
        "set_mail_priorities", "unseen_app_reverse",
        "reverse-check: 2 distinct var_ids (announcement_priority=high, "
        "digest_priority=low) on the unseen mail app — guards against "
        "over-merging from the granularity heuristic"),
}


def get_ood_task(task_id: str) -> CanonicalTaskGraph:
    if task_id not in OOD_TASKS:
        raise KeyError(f"unknown OOD task {task_id!r}; known: {list(OOD_TASKS)}")
    return OOD_TASKS[task_id]


def all_ood_tasks() -> list[CanonicalTaskGraph]:
    return list(OOD_TASKS.values())


def required_apps(fixture: CanonicalTaskGraph) -> list[str]:
    """The apps a task needs (derived from its seed_state keys — no schema change).
    The recon script builds adapters for exactly these apps so the compiler sees
    a focused context (not 3 empty seen-app pages alongside the held-out app)."""
    return list(fixture.seed_state.keys())


if __name__ == "__main__":
    # smoke: print the OOD task summaries
    for t in all_ood_tasks():
        print(f"\n=== {t.task_id} ({OOD_CATEGORIES[t.task_id].category}) ===")
        print(f"  apps: {required_apps(t)}")
        print(f"  edit: {t.user_edit}")
        print(f"  bindings ({len(t.bindings)}):")
        for b in t.bindings:
            print(f"    {b.var_id} → {b.app}.{b.entity_id}.{b.field} via {b.operator}")
        print(f"  non-interference: {t.non_interference_set}")
