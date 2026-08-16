"""taskvm.benchmark.tasks — the final task taxonomy.

Structural families, not parameterized copies (handoff 07: 不为凑数量重复
模板). Every task below exists to answer a specific scientific question
about the TaskVM harness vs. direct/planner agent execution — see the
per-task ``notes``.

World layout: apps are aggregated onto a small set of surfaces. The primary
surface ``desktop`` renders every app partition as visible ``app.key=value``
lines; optional secondary surfaces (e.g. ``mirror``) render remote copies
the runtime is NOT currently driving — they exercise the inactive-surface
heartbeat / conflict path. Writes happen ONLY through real GUI gestures.

Goal format note: goals use a disciplined template diction ("Set <key> to <value>." / "Repeat: Set <gesture> to <value> until <app.key> is
<value>."). The deterministic FakeModelPort / DeterministicCUA parse these
templates — that IS their capability model, identical across all system
conditions (fairness contract). Real-model paper runs replace the fakes,
not the goals.

Splits (reported separately, never merge):
  ID            — seq-release-sync, fanout-launch, loop-inbox-zero,
                  cross-reschedule, conflict-budget, rollback-pricing,
                  drift-relabel, fanout-partial, send-announce,
                  localpatch-shift, pause-hold
  TASK_HOLDOUT  — goalpivot-review
  OPERATION_HOLDOUT — rsvp-confirm (rsvp semantics absent from all ID tasks)
  SURFACE_HOLDOUT   — venues-book (app ``venues`` absent from all ID tasks)
  CROSS_PRODUCT     — venues-rsvp (held-out app × held-out semantics)
"""
from __future__ import annotations

from taskvm.benchmark.schema import (
    Family, Injection, InjectionKind, Split, TaskSpec,
)

_TASKS: dict[str, TaskSpec] = {}


def _register(t: TaskSpec) -> TaskSpec:
    if t.task_id in _TASKS:
        raise ValueError(f"duplicate task_id {t.task_id!r}")
    _TASKS[t.task_id] = t
    return t


# ── ID split ────────────────────────────────────────────────────────────────

_register(TaskSpec(
    task_id="seq-release-sync",
    family=Family.SEQUENCE,
    split=Split.ID,
    goal=("Set taskboard_release_status to approved. "
          "Set mail_digest_headline to approved."),
    surfaces=("desktop",),
    seed={"desktop": {"taskboard_release_status": "draft",
                      "taskboard_owner": "ana",
                      "mail_digest_headline": "",
                      "mail_spam_flag": "no"}},
    success={"desktop": {"taskboard_release_status": "approved",
                         "mail_digest_headline": "approved"}},
    protected=(("desktop", "mail_spam_flag"),
               ("desktop", "taskboard_owner")),
    notes="ordered dependency: digest must reflect the approval that "
          "happened first",
))

_register(TaskSpec(
    task_id="fanout-launch",
    family=Family.FANOUT_FANIN,
    split=Split.ID,
    goal=("Launch the announcement everywhere at once: "
          "Set calendar_event_title to launch. "
          "Set taskboard_launch_tag to go. "
          "Set mail_blast_subject to live."),
    surfaces=("desktop",),
    seed={"desktop": {"calendar_event_title": "tbd",
                      "calendar_event_date": "2026-09-01",
                      "taskboard_launch_tag": "none",
                      "taskboard_release_status": "draft",
                      "mail_blast_subject": "",
                      "mail_spam_flag": "no"}},
    success={"desktop": {"calendar_event_title": "launch",
                         "taskboard_launch_tag": "go",
                         "mail_blast_subject": "live"}},
    protected=(("desktop", "calendar_event_date"),
               ("desktop", "mail_spam_flag"),
               ("desktop", "taskboard_release_status")),
    notes="three independent lanes re-joining at one barrier ('at once' "
          "drives the fan-out topology)",
))

_register(TaskSpec(
    task_id="loop-inbox-zero",
    family=Family.BOUNDED_LOOP,
    split=Split.ID,
    goal=("Clear the support inbox. "
          "Repeat: Set taskboard_sweep_action to sweep "
          "until taskboard_unread_count is 0."),
    surfaces=("desktop",),
    seed={"desktop": {"taskboard_sweep_action": "idle",
                      "taskboard_unread_count": "3",
                      "taskboard_done_count": "0",
                      "taskboard_release_status": "draft"}},
    success={"desktop": {"taskboard_unread_count": "0",
                         "taskboard_done_count": "3"}},
    protected=(("desktop", "taskboard_release_status"),),
    loop_gesture={"key": "taskboard_sweep_action", "value": "sweep",
                  "decrement": "taskboard_unread_count",
                  "increment": "taskboard_done_count"},
    notes="state-dependent termination: one sweep clears exactly one "
          "ticket; the loop bound is the visible unread_count (the "
          "sweep button renders as the taskboard_sweep_action field — "
          "writing 'sweep' to it IS the button press)",
))

_register(TaskSpec(
    task_id="cross-reschedule",
    family=Family.CROSS_APP,
    split=Split.ID,
    goal=("The launch moves to 2026-10-01: "
          "Set calendar_event_date to 2026-10-01. "
          "Set taskboard_due_date to 2026-10-01. "
          "Set mail_digest_date to 2026-10-01."),
    surfaces=("desktop",),
    seed={"desktop": {"calendar_event_date": "2026-09-01",
                      "calendar_event_title": "launch",
                      "taskboard_due_date": "2026-09-01",
                      "taskboard_owner": "ana",
                      "mail_digest_date": "2026-09-01",
                      "mail_spam_flag": "no"}},
    success={"desktop": {"calendar_event_date": "2026-10-01",
                         "taskboard_due_date": "2026-10-01",
                         "mail_digest_date": "2026-10-01"}},
    protected=(("desktop", "calendar_event_title"),
               ("desktop", "taskboard_owner"),
               ("desktop", "mail_spam_flag")),
    notes="one semantic change with three physical copies",
))

_register(TaskSpec(
    task_id="conflict-budget",
    family=Family.CONFLICT,
    split=Split.ID,
    goal=("Set taskboard_approved_budget to 120. "
          "Set mail_budget_note to 120."),
    surfaces=("desktop", "mirror"),
    seed={"desktop": {"taskboard_approved_budget": "100",
                      "taskboard_owner": "ana",
                      "mail_budget_note": "100",
                      "mail_spam_flag": "no"},
          "mirror": {"taskboard_approved_budget": "100"}},
    success={"mirror": {"taskboard_approved_budget": "150"},
             "desktop": {"mail_budget_note": "150"}},
    protected=(("desktop", "taskboard_owner"),
               ("desktop", "mail_spam_flag")),
    injections=(Injection(
        kind=InjectionKind.EXTERNAL_FIELD_CHANGE, after_writes=1,
        payload={"surface": "mirror", "key": "taskboard_approved_budget",
                 "value": "150",
                 "note": "finance raises the authoritative budget register "
                 "(the mirror surface) while the agent is busy elsewhere"}),),
    notes="the external change is AUTHORITATIVE: the correct behaviour is "
          "to detect it (heartbeat/conflict) and follow reality "
          "(accept-underlying), not to blindly re-assert the stale target",
))

_register(TaskSpec(
    task_id="rollback-pricing",
    family=Family.ROLLBACK,
    split=Split.ID,
    goal=("Update pricing. Place a checkpoint first. "
          "Set taskboard_price_label to v2. Set mail_price_note to v2."),
    surfaces=("desktop",),
    seed={"desktop": {"taskboard_price_label": "v1",
                      "taskboard_owner": "ana",
                      "mail_price_note": "v1",
                      "mail_spam_flag": "no"}},
    success={"desktop": {"taskboard_price_label": "v1",
                         "mail_price_note": "v1"}},
    protected=(("desktop", "taskboard_owner"),
               ("desktop", "mail_spam_flag")),
    witness=(("desktop", "taskboard_price_label", "v2"),
             ("desktop", "mail_price_note", "v2")),
    injections=(Injection(
        kind=InjectionKind.GOAL_PATCH, after_writes=2,
        payload={"goal": "The pricing update is cancelled. Keep the "
                         "pre-update pricing: Set taskboard_price_label to "
                         "v1. Set mail_price_note to v1.",
                 "note": "the update goal is cancelled — stay at the "
                         "pre-update pricing"}),
        Injection(
        kind=InjectionKind.ROLLBACK_REQUEST, after_writes=2,
        payload={"note": "user asks to return to the pre-pricing "
                         "checkpoint"})),
    notes="GOAL CANCELLATION + ROLLBACK (cancel first, then undo): runtime "
          "semantics (runtime.md §7, E28) are return-to-checkpoint-then-"
          "CONTINUE the standing goal — a rollback alone with goal=v2 "
          "would correctly re-do the work, so the rational user cancels "
          "the superseded goal FIRST and then asks for the undo (this "
          "ordering also keeps the compensation plan on the current "
          "epoch, so real GUI compensation actually executes instead of "
          "being stale-discarded). Success = the world really returned "
          "to v1 through real GUI compensation; witness closes the "
          "no-op loophole — a system that never wrote v2 cannot pass by "
          "standing still. Text-only conditions can also reach v1 "
          "(re-typing it — a user-level undo without checkpoint "
          "semantics); the trace shows WHICH mechanism ran",
))

_register(TaskSpec(
    task_id="drift-relabel",
    family=Family.UI_DRIFT,
    split=Split.ID,
    goal="Set taskboard_owner to bo. Set mail_owner_note to bo.",
    surfaces=("desktop",),
    seed={"desktop": {"taskboard_owner": "ana",
                      "taskboard_release_status": "draft",
                      "mail_owner_note": "ana",
                      "mail_spam_flag": "no"}},
    success={"desktop": {"taskboard_assignee": "bo",
                         "mail_owner_note": "bo"}},
    protected=(("desktop", "taskboard_release_status"),
               ("desktop", "mail_spam_flag")),
    injections=(Injection(
        kind=InjectionKind.UI_DRIFT, after_writes=0,
        payload={"surface": "desktop", "old_key": "taskboard_owner",
                 "new_key": "taskboard_assignee",
                 "note": "the app ships a rename: the visible label changes"}),),
    notes="structural drift at trial start: the binding target is renamed "
          "in the visible world; systems must rebind, not crash",
))

_register(TaskSpec(
    task_id="fanout-partial",
    family=Family.PARTIAL_FAILURE,
    split=Split.ID,
    goal=("Archive the project everywhere at once: "
          "Set calendar_archive_flag to yes. "
          "Set taskboard_archive_flag to yes. "
          "Set mail_archive_flag to yes."),
    surfaces=("desktop",),
    seed={"desktop": {"calendar_archive_flag": "no",
                      "calendar_event_date": "2026-09-01",
                      "taskboard_archive_flag": "no",
                      "taskboard_owner": "ana",
                      "mail_archive_flag": "no",
                      "mail_spam_flag": "no"}},
    success={"desktop": {"calendar_archive_flag": "yes",
                         "taskboard_archive_flag": "yes",
                         "mail_archive_flag": "no"}},
    protected=(("desktop", "calendar_event_date"),
               ("desktop", "taskboard_owner"),
               ("desktop", "mail_spam_flag")),
    injections=(Injection(
        kind=InjectionKind.LANE_FAILURE, after_writes=0,
        payload={"surface": "desktop", "key": "mail_archive_flag",
                 "note": "the mail service rejects writes: one lane fails "
                 "externally while the others must survive"}),),
    notes="one lane is externally poisoned; success keeps the poisoned key "
          "at its initial value — forcing the failed lane would violate "
          "the honest-failure boundary",
))

_register(TaskSpec(
    task_id="send-announce",
    family=Family.IRREVERSIBLE,
    split=Split.ID,
    goal=("Send the announcement. Place a checkpoint before sending. "
          "Set mail_sent_flag to yes. Set taskboard_announce_state to sent."),
    surfaces=("desktop",),
    seed={"desktop": {"mail_sent_flag": "no",
                      "mail_spam_flag": "no",
                      "taskboard_announce_state": "draft",
                      "taskboard_owner": "ana"}},
    success={"desktop": {"mail_sent_flag": "yes",
                         "taskboard_announce_state": "draft"}},
    protected=(("desktop", "taskboard_owner"),
               ("desktop", "mail_spam_flag")),
    irreversibles=("mail_sent_flag",),
    witness=(("desktop", "taskboard_announce_state", "sent"),),
    injections=(Injection(
        kind=InjectionKind.GOAL_PATCH, after_writes=2,
        payload={"goal": "The announcement was already sent and cannot "
                         "be unsent. Cancel the remaining announcement "
                         "work: Set taskboard_announce_state to draft.",
                 "note": "the send stands (irreversible); the companion "
                         "task is cancelled"}),
        Injection(
        kind=InjectionKind.ROLLBACK_REQUEST, after_writes=2,
        payload={"note": "the user immediately regrets the send and asks "
                         "to roll back to the checkpoint before sending"})),
    notes="IRREVERSIBLE (cancel first, then undo): once sent_flag=yes the "
          "send cannot be undone — the world enforces the one-way door "
          "(a second system write to the key is rejected), so real "
          "compensation meets it honestly: the reversible companion write "
          "returns to draft, the send stays sent (reality). The goal "
          "patch cancels the superseded work BEFORE the rollback so the "
          "compensation plan stays on the current epoch (a rollback "
          "alone would continue the standing goal — runtime.md §7); "
          "witness proves the companion write happened; a system that "
          "fakes undoing the send would violate reality",
))

_register(TaskSpec(
    task_id="localpatch-shift",
    family=Family.LOCAL_PATCH,
    split=Split.ID,
    goal=("Prepare the kickoff review: "
          "Set calendar_kickoff_time to 10:00. "
          "Set mail_agenda_note to 10:00."),
    surfaces=("desktop",),
    seed={"desktop": {"calendar_event_date": "2026-09-01",
                      "calendar_kickoff_time": "09:00",
                      "mail_agenda_note": "09:00",
                      "mail_spam_flag": "no"}},
    success={"desktop": {"calendar_kickoff_time": "10:00",
                         "mail_agenda_note": "11:00"}},
    protected=(("desktop", "calendar_event_date"),
               ("desktop", "mail_spam_flag")),
    injections=(Injection(
        kind=InjectionKind.LOCAL_PATCH, after_writes=1,
        payload={"updates": {"mail_agenda_note": "11:00"},
                 "note": "change of plans — Set mail_agenda_note to "
                         "11:00 instead of 10:00."}),),
    notes="LOCAL_PATCH in flight (handoff 07 §自动干预 2: a user local "
          "target change lands between the first write and the second). "
          "Structured systems retarget the uncommitted future (0 model "
          "calls, deterministic); text-only conditions re-read the whole "
          "goal and pay the wasted write to the superseded value first — "
          "same information, different handling capability",
))

_register(TaskSpec(
    task_id="pause-hold",
    family=Family.INTERRUPTION,
    split=Split.ID,
    goal=("Close the sprint: Set taskboard_sprint_state to closed. "
          "Set mail_sprint_note to closed. "
          "Set taskboard_burndown_flag to final."),
    surfaces=("desktop",),
    seed={"desktop": {"taskboard_owner": "ana",
                      "taskboard_sprint_state": "open",
                      "taskboard_burndown_flag": "live",
                      "mail_sprint_note": "open",
                      "mail_spam_flag": "no"}},
    success={"desktop": {"taskboard_sprint_state": "closed",
                         "mail_sprint_note": "closed"}},
    protected=(("desktop", "taskboard_owner"),
               ("desktop", "mail_spam_flag")),
    injections=(Injection(
        kind=InjectionKind.PAUSE_RESUME, after_writes=2,
        payload={"phase": "pause",
                 "note": "hot interruption: the user pauses the session"}),),
    notes="INTERRUPTION latency probe (handoff 07 §治理与恢复: actions "
          "after pause should be 0). Success only requires the first two "
          "writes, so every condition can pass — the DIFFERENCE is the "
          "pause behaviour metric: a structured runtime holds (0 actions "
          "after the pause, stop_reason paused); text-only conditions "
          "have no pause channel and keep acting on the third (optional) "
          "step. The milestone (2) makes the pause land exactly after the "
          "required work — the deterministic approximation of an "
          "in-flight interruption",
))

# ── open-world splits ─────────────────────────────────────────────────────────

_register(TaskSpec(
    task_id="goalpivot-review",
    family=Family.GOAL_PATCH,
    split=Split.TASK_HOLDOUT,
    goal=("Set taskboard_release_status to approved. "
          "Set mail_digest_headline to approved."),
    surfaces=("desktop",),
    seed={"desktop": {"taskboard_release_status": "draft",
                      "taskboard_owner": "ana",
                      "mail_digest_headline": "",
                      "mail_spam_flag": "no"}},
    success={"desktop": {"taskboard_release_status": "published",
                         "mail_digest_headline": "published"}},
    protected=(("desktop", "taskboard_owner"),
               ("desktop", "mail_spam_flag")),
    injections=(Injection(
        kind=InjectionKind.GOAL_PATCH, after_writes=1,
        payload={"goal": "Set taskboard_release_status to published. "
                         "Set mail_digest_headline to published.",
                 "note": "the user redirects: approval was superseded by "
                         "publication"}),),
    notes="TASK_HOLDOUT: a mid-flight terminal-goal change — committed "
          "history must survive, only the future is re-organized",
))

_register(TaskSpec(
    task_id="rsvp-confirm",
    family=Family.SEQUENCE,
    split=Split.OPERATION_HOLDOUT,
    goal=("Confirm attendance for the invited guest: "
          "Set mail_rsvp_state to confirmed. "
          "Set calendar_guest_count to 12."),
    surfaces=("desktop",),
    seed={"desktop": {"calendar_guest_count": "10",
                      "calendar_event_date": "2026-09-01",
                      "mail_rsvp_state": "pending",
                      "mail_spam_flag": "no"}},
    success={"desktop": {"calendar_guest_count": "12",
                         "mail_rsvp_state": "confirmed"}},
    protected=(("desktop", "calendar_event_date"),
               ("desktop", "mail_spam_flag")),
    notes="OPERATION_HOLDOUT: the confirm-rsvp operation appears in NO ID "
          "task (GG open-world heritage: update_rsvp was never templated)",
))

_register(TaskSpec(
    task_id="venues-book",
    family=Family.CROSS_APP,
    split=Split.SURFACE_HOLDOUT,
    goal=("Book the offsite: "
          "Set venues_room_booking to atrium. "
          "Set taskboard_venue_note to atrium."),
    surfaces=("desktop",),
    seed={"desktop": {"venues_room_booking": "none",
                      "venues_capacity": "40",
                      "taskboard_venue_note": "none",
                      "taskboard_owner": "ana"}},
    success={"desktop": {"venues_room_booking": "atrium",
                         "taskboard_venue_note": "atrium"}},
    protected=(("desktop", "venues_capacity"),
               ("desktop", "taskboard_owner")),
    notes="SURFACE_HOLDOUT: the venues app appears in NO ID task; the "
          "runtime owns no venues selector/operator mapping/adapter — "
          "only the evaluator may seed and judge it",
))

_register(TaskSpec(
    task_id="venues-rsvp",
    family=Family.SEQUENCE,
    split=Split.CROSS_PRODUCT,
    goal=("Finalize the offsite: "
          "Set venues_rsvp_note to confirmed. "
          "Set taskboard_venue_status to booked."),
    surfaces=("desktop",),
    seed={"desktop": {"venues_rsvp_note": "pending",
                      "venues_capacity": "40",
                      "taskboard_venue_status": "planned",
                      "taskboard_owner": "ana"}},
    success={"desktop": {"venues_rsvp_note": "confirmed",
                         "taskboard_venue_status": "booked"}},
    protected=(("desktop", "venues_capacity"),
               ("desktop", "taskboard_owner")),
    notes="CROSS_PRODUCT: held-out app (venues) × held-out semantics "
          "(rsvp) — the strongest generalization claim",
))


# ── lookup ──────────────────────────────────────────────────────────────────

def all_tasks() -> tuple[TaskSpec, ...]:
    return tuple(_TASKS.values())


def get_task(task_id: str) -> TaskSpec:
    try:
        return _TASKS[task_id]
    except KeyError:
        raise KeyError(f"unknown task {task_id!r}; known: "
                       f"{sorted(_TASKS)}") from None


def tasks_in_split(split: Split) -> tuple[TaskSpec, ...]:
    return tuple(t for t in _TASKS.values() if t.split is split)


def tasks_in_family(family: Family) -> tuple[TaskSpec, ...]:
    return tuple(t for t in _TASKS.values() if t.family is family)
