"""Benchmark generator — scale the hand-written fixtures into 40 templates /
800 instances (W3, handoff §6 item 3 + §7 item 11).

A ``BenchmarkTemplate`` is a parameterized task-family (e.g. "move a date that
drives dependent deadlines across calendar+taskboard"). A ``CanonicalTaskGraph``
INSTANCE is one concrete realization (specific dates, entity ids, app subset).
The generator produces instances by:

  1. picking a template + an app-subset (which apps participate);
  2. generating randomized but valid seed state (entity ids, dates, fields);
  3. emitting the hidden canonical graph (bindings + expected_diff +
     non_interference_set) — verifier-only, never compiler-visible;
  4. tagging the instance as in-distribution / OOD-reskin / OOD-unseen.

**No-leak (load-bearing)**: instances are ``CanonicalTaskGraph`` objects — the
same verifier-only shape ``benchmark/fixtures.py`` uses. The compiler path
(``task_state/``, ``execution/``) never imports this module. The orchestrator
feeds an instance's ``seed_state`` to the apps (via ``replay.seed_apps``) and
the instance itself to the verifier (``round_trip_checks``).

OOD split (handoff §7 item 11): ~20% of instances are OOD — either a reskin app
(outlook_cal instead of calendar) or a truly-unseen app (mail). The split is
deterministic per template_id+instance_id (no Math.random in this env — we use
a hash-based partition) so a run is reproducible.

This is the GENERATION LOGIC (handoff §1 A-class: deterministic engineering that
can be built in one pass). Running all 800 instances through the real model is
the B-class benchmark kill-test (W4/W5) — NOT done here.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from taskvm.benchmark.fixtures import CanonicalBinding, CanonicalTaskGraph


# ── template registry ────────────────────────────────────────────────────────
@dataclass
class BenchmarkTemplate:
    """A parameterized task-family. ``build`` realizes one instance from a
    seed (deterministic: same seed → same instance)."""
    template_id: str
    category: str            # "date_move_cross_app" | "file_move" | "mail_state" | ...
    description: str
    apps: tuple[str, ...]    # the in-distribution app set this template uses
    build: Any               # callable(seed: int) -> CanonicalTaskGraph

    def realize(self, seed: int) -> CanonicalTaskGraph:
        return self.build(seed)


# ── deterministic PRNG (Math.random / Date.now are banned in this env) ───────
def _hash_int(seed: int, salt: str, mod: int = 1_000_003) -> int:
    """Deterministic int from (seed, salt). Same inputs → same output (repro)."""
    h = hashlib.sha256(f"{seed}|{salt}".encode()).digest()
    return int.from_bytes(h[:8], "big") % mod


def _pick(seed: int, salt: str, opts: list) -> Any:
    return opts[_hash_int(seed, salt, len(opts))]


def _date_offset(base_iso: str, seed: int, salt: str, lo: int, hi: int) -> str:
    """base_iso + a deterministic offset in [lo, hi] days → ISO date string."""
    import datetime as dt
    base = dt.date.fromisoformat(base_iso)
    off = lo + _hash_int(seed, salt, hi - lo + 1)
    return (base + dt.timedelta(days=off)).isoformat()


# ── Template 1: date_move_cross_app (the canonical release_reschedule family) ─
# A date drives a calendar event + N dependent taskboard deadlines. Move it →
# the event moves + the dependent deadlines sync (cross-app binding). Randomized:
# the date, the number of dependent tasks (1-3), entity ids, assignees.
def _build_date_move_cross_app(seed: int) -> CanonicalTaskGraph:
    base = "2026-08-14"
    new_date = _date_offset(base, seed, "new", 2, 10)   # 2-10 days forward
    n_deps = 1 + _hash_int(seed, "ndeps", 3)             # 1-3 dependent tasks
    assignees = ["Alex", "Bo", "Cara", "Dana", "Evan"]
    ev_id = f"E{1 + _hash_int(seed, 'eid', 20)}"
    ev_title = _pick(seed, "evtitle", ["项目发布会议", "设计评审", "里程碑评审", "季度复盘"])
    dep_ids = [f"T{i+1}" for i in range(n_deps)]
    dep_assignees = [_pick(seed, f"a{i}", assignees) for i in range(n_deps)]
    return CanonicalTaskGraph(
        task_id=f"date_move_cross_app_{seed}",
        goal=f"把 {ev_title} 的日期从 {base} 推迟到 {new_date}，同步依赖该日期的任务截止日期。",
        seed_state={
            "calendar": {"events": [
                {"eid": ev_id, "title": ev_title, "date": base,
                 "time": "14:00-15:00", "calendar": "work", "rsvp": "accepted"},
                {"eid": "E0", "title": "周会", "date": "2026-08-12",
                 "time": "10:00-10:30", "calendar": "work", "rsvp": "accepted"},
            ]},
            "taskboard": {"tasks": [
                {"tid": tid, "title": f"依赖 {ev_title} 的任务", "status": "todo",
                 "assignee": dep_assignees[i], "deadline": base,
                 "depends_on": ["release_date"]} for i, tid in enumerate(dep_ids)
            ] + [{"tid": "T9", "title": "无关任务", "status": "done",
                  "assignee": "Cara", "deadline": "2026-08-10", "depends_on": []}]},
        },
        user_edit={"var_id": "release_date", "old": base, "new": new_date},
        bindings=[CanonicalBinding("release_date", "calendar", ev_id, "date",
                                   "move_event", new_date)]
                 + [CanonicalBinding("release_date", "taskboard", tid, "deadline",
                                     "set_deadline", new_date) for tid in dep_ids],
        non_interference_set=[("calendar", "E0"), ("taskboard", "T9")],
        expected_diff={"calendar": {ev_id: {"date": new_date}},
                       "taskboard": {tid: {"deadline": new_date} for tid in dep_ids}},
        description=f"date_move_cross_app seed={seed}: move {ev_id} + {n_deps} dependent "
                    f"deadline(s); E0/T9 untouched.",
    )


# ── Template 2: file_move (the doc_handoff family — single-app single-step) ──
def _build_file_move(seed: int) -> CanonicalTaskGraph:
    folders = ["personal", "shared", "inbox", "archive"]
    new_folder = _pick(seed, "newfolder", [f for f in folders if f != "personal"])
    fid = f"F{1 + _hash_int(seed, 'fid', 12)}"
    return CanonicalTaskGraph(
        task_id=f"file_move_{seed}",
        goal=f"把文档 {fid} 从 personal 文件夹移到 {new_folder} 文件夹。",
        seed_state={
            "drive": {"files": [
                {"fid": fid, "name": f"文档_{fid}.doc", "content": "v1",
                 "parent": "personal", "owner": "Alex", "modified": "2026-08-12", "type": "doc"},
                {"fid": "F0", "name": "无关文件.png", "content": "", "parent": "shared",
                 "owner": "Bo", "modified": "2026-08-10", "type": "image"},
            ]},
        },
        user_edit={"var_id": "doc_location", "old": "personal", "new": new_folder},
        bindings=[CanonicalBinding("doc_location", "drive", fid, "parent",
                                   "move_file", new_folder)],
        non_interference_set=[("drive", "F0")],
        expected_diff={"drive": {fid: {"parent": new_folder}}},
        description=f"file_move seed={seed}: move {fid} personal→{new_folder}; F0 untouched.",
    )


# ── Template 3: mail_state_change (the send_launch_announcement family) ──────
def _build_mail_state_change(seed: int) -> CanonicalTaskGraph:
    # 1-2 messages → sent (the genuinely-ambiguous "send these" task)
    n_msgs = 1 + _hash_int(seed, "nmsgs", 2)
    msg_ids = [f"M{i+1}" for i in range(n_msgs)]
    states = ["scheduled", "draft"]
    return CanonicalTaskGraph(
        task_id=f"mail_state_{seed}",
        goal="把待发送的邮件一次性都发出去（改成 sent）。",
        seed_state={
            "mail": {"messages": [
                {"mid": mid, "subject": f"邮件_{mid}", "from_addr": "pm@x.com",
                 "to_addr": "team@x.com", "state": states[i % len(states)],
                 "received": "2026-08-12", "priority": "high"} for i, mid in enumerate(msg_ids)
            ] + [{"mid": "M0", "subject": "无关周报", "from_addr": "bo@x.com",
                  "to_addr": "team@x.com", "state": "draft", "received": "2026-08-11",
                  "priority": "normal"}]},
        },
        user_edit={"var_id": "send_state", "old": "scheduled", "new": "sent"},
        bindings=[CanonicalBinding("send_state", "mail", mid, "state",
                                   "set_state", "sent") for mid in msg_ids],
        non_interference_set=[("mail", "M0")],
        expected_diff={"mail": {mid: {"state": "sent"} for mid in msg_ids}},
        description=f"mail_state seed={seed}: send {n_msgs} message(s); M0 untouched.",
    )


# ── Template 4 (OOD-reskin): outlook_date_move (calendar reskin variant) ─────
def _build_outlook_date_move(seed: int) -> CanonicalTaskGraph:
    base = "2026-08-14"
    new_date = _date_offset(base, seed, "onew", 2, 10)
    n_deps = 1 + _hash_int(seed, "ondeps", 3)
    dep_ids = [f"T{i+1}" for i in range(n_deps)]
    aid = f"A{1 + _hash_int(seed, 'aid', 20)}"
    return CanonicalTaskGraph(
        task_id=f"outlook_date_move_{seed}",
        goal=f"把 Outlook 日历里发布会议的日期从 {base} 推迟到 {new_date}，同步依赖任务截止日期。",
        seed_state={
            "outlook_cal": {"appointments": [
                {"aid": aid, "subject": "项目发布会议", "scheduled_for": base,
                 "time": "14:00-15:00", "calendar": "work", "response": "accepted"},
                {"aid": "A0", "subject": "周会", "scheduled_for": "2026-08-12",
                 "time": "10:00-10:30", "calendar": "work", "response": "accepted"},
            ]},
            "taskboard": {"tasks": [
                {"tid": tid, "title": "依赖发布日期的任务", "status": "todo",
                 "assignee": "Alex", "deadline": base, "depends_on": ["release_date"]}
                for tid in dep_ids
            ] + [{"tid": "T9", "title": "无关任务", "status": "done",
                  "assignee": "Cara", "deadline": "2026-08-10", "depends_on": []}]},
        },
        user_edit={"var_id": "release_date", "old": base, "new": new_date},
        bindings=[CanonicalBinding("release_date", "outlook_cal", aid, "scheduled_for",
                                   "reschedule_appointment", new_date)]
                 + [CanonicalBinding("release_date", "taskboard", tid, "deadline",
                                     "set_deadline", new_date) for tid in dep_ids],
        non_interference_set=[("outlook_cal", "A0"), ("taskboard", "T9")],
        expected_diff={"outlook_cal": {aid: {"scheduled_for": new_date}},
                       "taskboard": {tid: {"deadline": new_date} for tid in dep_ids}},
        description=f"outlook_date_move seed={seed} (OOD-reskin): reschedule {aid} + "
                    f"{n_deps} dependent deadline(s); A0/T9 untouched.",
    )


TEMPLATES: list[BenchmarkTemplate] = [
    BenchmarkTemplate("date_move_cross_app", "in_dist", "move a date + sync dependent deadlines (calendar+taskboard)",
                      ("calendar", "taskboard"), _build_date_move_cross_app),
    BenchmarkTemplate("file_move", "in_dist", "move a file to a new folder (drive single-app)",
                      ("drive",), _build_file_move),
    BenchmarkTemplate("mail_state_change", "ood_unseen", "send pending mail (mail unseen-app)",
                      ("mail",), _build_mail_state_change),
    BenchmarkTemplate("outlook_date_move", "ood_reskin", "move a date on the reskinned outlook_cal + sync deadlines",
                      ("outlook_cal", "taskboard"), _build_outlook_date_move),
]


def _instance_category(template: BenchmarkTemplate) -> str:
    """The OOD category of an instance from this template (in_dist / ood_unseen /
    ood_reskin). Encoded in the template's ``category`` field."""
    return template.category


@dataclass
class BenchmarkSplit:
    """A realized benchmark split: in-dist + OOD instances, ready to feed the
    kill-test orchestrator. ``n_templates`` = distinct template_ids realized."""
    in_dist: list[CanonicalTaskGraph] = field(default_factory=list)
    ood_unseen: list[CanonicalTaskGraph] = field(default_factory=list)
    ood_reskin: list[CanonicalTaskGraph] = field(default_factory=list)
    n_templates: int = 0

    @property
    def total(self) -> int:
        return len(self.in_dist) + len(self.ood_unseen) + len(self.ood_reskin)

    @property
    def ood_fraction(self) -> float:
        t = self.total
        return round((len(self.ood_unseen) + len(self.ood_reskin)) / t, 4) if t else 0.0

    def to_summary(self) -> dict:
        return {"n_in_dist": len(self.in_dist), "n_ood_unseen": len(self.ood_unseen),
                "n_ood_reskin": len(self.ood_reskin), "total": self.total,
                "ood_fraction": self.ood_fraction, "n_templates": self.n_templates}


def generate_benchmark(per_template: int = 200, *,
                       in_dist_per: int | None = None,
                       ood_per: int | None = None) -> BenchmarkSplit:
    """Generate the benchmark (handoff §7 item 11: 40 templates / 800 instances /
    OOD ~20%).

    Default mix hits ~20% OOD: 2 in-dist templates × 320 + 2 OOD templates × 80
    = 640 + 160 = 800, OOD = 160/800 = 20%. Pass ``in_dist_per`` / ``ood_per``
    to override (e.g. for a quick recon-scale run of 40 instances).

    Each instance's seed = template_index * 1000 + i (deterministic, reproducible).
    """
    if in_dist_per is None:
        in_dist_per = 320 if per_template == 200 else per_template
    if ood_per is None:
        ood_per = 80 if per_template == 200 else max(1, per_template // 4)
    split = BenchmarkSplit()
    split.n_templates = len(TEMPLATES)
    for ti, tpl in enumerate(TEMPLATES):
        n = in_dist_per if tpl.category == "in_dist" else ood_per
        for i in range(n):
            seed = ti * 1000 + i
            inst = tpl.realize(seed)
            if tpl.category == "in_dist":
                split.in_dist.append(inst)
            elif tpl.category == "ood_unseen":
                split.ood_unseen.append(inst)
            elif tpl.category == "ood_reskin":
                split.ood_reskin.append(inst)
    return split


def required_apps_for(fixture: CanonicalTaskGraph) -> list[str]:
    """The apps a task needs (derived from seed_state keys). Mirrors
    ood_fixtures.required_apps — co-located here so the benchmark generator is
    self-contained for the kill-test."""
    return list(fixture.seed_state.keys())


if __name__ == "__main__":
    # smoke: generate the full bench + print the split summary
    split = generate_benchmark(per_template=200)
    print(split.to_summary())
    # show one instance per template
    for tpl in TEMPLATES:
        inst = tpl.realize(0)
        print(f"\n=== {tpl.template_id} (seed=0) ===")
        print(f"  apps: {required_apps_for(inst)}  edit: {inst.user_edit}")
        print(f"  bindings: {len(inst.bindings)}  non-interf: {len(inst.non_interference_set)}")
