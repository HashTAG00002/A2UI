"""Entity binding + OPERATOR_REGISTRY.

**No-coupling boundary (load-bearing — per user feedback)**: ``OPERATOR_REGISTRY``
is COMPILER-VISIBLE and contains ONLY "what operations exist" — operator
signatures (name, app, field). It carries **NO var_id mappings**. The
verifier-only GT (which var_id binds to which operator) lives in
``benchmark/fixtures.py::CanonicalBinding.operator``. These two must never be
hardcoded-coupled: the registry must not name var_ids; ``fixtures.py`` must not
be imported by this module or any compiler-path module (``task_state/``,
``execution/``). See W1 plan §deliverable 3 + Verification step 6.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EntityBinding:
    """One edge: a task variable bound to one real app entity + operator.
    Emitted by the compiler (discovered from observations); mirrored in shape by
    the verifier-only ``CanonicalBinding`` (GT)."""
    var_id: str
    app: str           # "calendar" | "taskboard" | "drive"
    entity_id: str     # "E1" | "T1" | "F1"  (must appear in the DOM)
    field: str         # "date" | "deadline" | "status" | "assignee" | "parent" | ...
    operator: str      # one of OPERATOR_REGISTRY keys

    def to_dict(self) -> dict:
        return {"var_id": self.var_id, "app": self.app,
                "entity_id": self.entity_id, "field": self.field,
                "operator": self.operator}


@dataclass
class Dependency:
    """Effect-propagation edge: when ``from_var`` changes, ``to_entity`` must
    sync (e.g. a deadline tracks a release date)."""
    from_var: str
    to_app: str
    to_entity_id: str
    relation: str = "tracks"   # "deadline_tracks_release_date" | ...

    def to_dict(self) -> dict:
        return {"from_var": self.from_var,
                "to_entity": {"app": self.to_app, "entity_id": self.to_entity_id},
                "relation": self.relation}


@dataclass
class TaskBinding:
    """The compiler's full binding output: variables (each with its entity
    bindings) + dependencies. Mirrors the ``task_binding`` JSON the compiler
    emits (see ``benchmark/a2ui_spec.py`` TASKVM_BINDING_CONTRACT)."""
    task_id: str
    variables: list[dict] = field(default_factory=list)        # [{var_id,label,value,editable,bindings:[EntityBinding]}]
    dependencies: list[Dependency] = field(default_factory=list)

    def bindings_for(self, var_id: str) -> list[EntityBinding]:
        """All EntityBindings for a given variable."""
        out: list[EntityBinding] = []
        for v in self.variables:
            if v.get("var_id") == var_id:
                for b in v.get("bindings") or []:
                    out.append(EntityBinding(
                        var_id=var_id, app=b["app"], entity_id=b["entity_id"],
                        field=b["field"], operator=b["operator"]))
        return out

    def all_entity_bindings(self) -> list[EntityBinding]:
        out: list[EntityBinding] = []
        for v in self.variables:
            out.extend(self.bindings_for(v["var_id"]))
        return out

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "variables": [
                {**{k: v[k] for k in v if k != "bindings"},
                 "bindings": [b.to_dict() if isinstance(b, EntityBinding) else b
                              for b in v.get("bindings") or []]}
                for v in self.variables
            ],
            "dependencies": [d.to_dict() for d in self.dependencies],
        }


# ── OPERATOR_REGISTRY (compiler-visible; NO var_ids) ─────────────────────────
# Each operator: which app it targets, which field it writes, and its signature.
# This is the tool schema the compiler sees. The verifier-only GT mapping
# (var_id → operator) is in benchmark/fixtures.py::CanonicalBinding — NOT here.
OPERATOR_REGISTRY: dict[str, dict[str, str]] = {
    "move_event":   {"app": "calendar",  "field": "date",
                     "signature": "move_event(eid, new_date) — move calendar event to new_date"},
    "set_deadline": {"app": "taskboard", "field": "deadline",
                     "signature": "set_deadline(tid, new_date) — set a task's deadline"},
    "set_status":   {"app": "taskboard", "field": "status",
                     "signature": "set_status(tid, new_status) — set a task's status (todo/doing/done)"},
    "set_assignee": {"app": "taskboard", "field": "assignee",
                     "signature": "set_assignee(tid, new_assignee) — reassign a task"},
    # W2 — Drive app (file/document store). move_file is the single-app
    # single-step operator the W2 rollback gate undoes (parent: personal→shared).
    "move_file":    {"app": "drive", "field": "parent",
                     "signature": "move_file(fid, new_parent) — move a file to a new folder"},
    "rename":       {"app": "drive", "field": "name",
                     "signature": "rename(fid, new_name) — rename a file"},
    "set_owner":    {"app": "drive", "field": "owner",
                     "signature": "set_owner(fid, new_owner) — reassign file ownership"},
    # W4 held-out — Mail (truly-unseen app). A message lifecycle: set_state
    # mutates a finite-state field (draft/sent/scheduled), not a scalar. This is
    # the operator the W4 OOD mail task edits (scheduled → sent).
    "set_state":    {"app": "mail", "field": "state",
                     "signature": "set_state(mid, new_state) — set a message's lifecycle state (draft/sent/scheduled)"},
    "set_priority": {"app": "mail", "field": "priority",
                     "signature": "set_priority(mid, new_priority) — set a message's priority (high/normal/low)"},
    "set_to":       {"app": "mail", "field": "to_addr",
                     "signature": "set_to(mid, new_to_addr) — change a message's recipient"},
    # W4 held-out — Outlook_Cal (calendar reskin). Same semantics as move_event
    # (move a meeting to a new date) but renamed substrate: the field is
    # ``scheduled_for`` (not ``date``), the kind is ``appointment`` (not
    # ``event``). Tests substrate-independence: same conceptual op, new skin.
    "reschedule_appointment": {"app": "outlook_cal", "field": "scheduled_for",
                     "signature": "reschedule_appointment(aid, new_scheduled_for) — move an appointment to a new date"},
    # MobileGym demo — Wechat (Playwright-driven React sim via the bridge).
    # send_message is APPEND-style (not a scalar field-setter): it appends a
    # text message to a chat thread. Rollback is snapshot-based (the bridge
    # captures a pre-state snapshot and set_state-restores it on undo), NOT a
    # field-setter inverse — the operator's semantics differ from the W1/W2
    # field-setters, which is the point of exercising a new substrate.
    "send_message":  {"app": "wechat", "field": "messages",
                     "signature": "send_message(chat_id, text) — append a new text message to a wechat chat thread"},
}


def build_tool_schema(apps: list[str] | None = None) -> str:
    """The operator catalog passed to the compiler's system prompt / input.
    Lists ONLY operator signatures (no var_ids, no GT)."""
    apps = apps or list(dict.fromkeys(meta["app"]
                                      for meta in OPERATOR_REGISTRY.values()))
    lines = ["# Tool schema — executable operators each app exposes (use these as `operator`):"]
    for op, meta in OPERATOR_REGISTRY.items():
        if meta["app"] in apps:
            lines.append(f"- {meta['signature']}  [app={meta['app']}, field={meta['field']}]")
    return "\n".join(lines)


def known_operators() -> list[str]:
    return list(OPERATOR_REGISTRY)


def operator_app(op: str) -> str | None:
    m = OPERATOR_REGISTRY.get(op)
    return m["app"] if m else None
