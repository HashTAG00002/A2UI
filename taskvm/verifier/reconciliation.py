"""reconciliation — re-read-on-action + conflict marking (W3, handoff §5 inv 4-5
+ §7 item 13 + §11 limit 6).

When an external concurrent change happens to an app's state BETWEEN the
workspace's last projection and the user's next action, the read-only zone must
NOT silently overwrite either side. Instead it marks the conflicting field
AMBER and presents a merge option — exactly the gap SaC names as future work
("frontend state synchronisation", §3.5 SaC核对) and TaskVM claims to close.

**The two load-bearing governance constraints (handoff §5 inv 4-5, §9-7)**:
  1. NEVER silently overwrite. If the underlying app state (X) differs from the
     projected value (Y) the user is looking at, the read-only zone MUST display
     both ("底层已变: 现 X（你投影的是 Y）") + offer merge options. It must NOT
     auto-pick X (that would hide a concurrent change the user hasn't seen) and
     must NOT auto-pick Y (that would lie about the real world).
  2. NEVER block on human confirmation. The agent is NOT paused waiting for the
     user to resolve conflicts — governance, not approval (handoff §7 item 7,
     §10). The conflict is surfaced as a visible affordance the user MAY act on
     (accept underlying / keep mine / merge); the agent continues. This is the
     anti-Sidekick boundary.

**Detection source (handoff §5 inv 5)**: conflicts are detected by RE-READING
real canonical state (``read_canonical``) — never by compiler/verifier guess or
cache. This module computes the diff between the last-projected snapshot and a
fresh canonical read; ``live_sync.resync_with_conflicts`` wires it into the
read-only zone rendering.

No model, no GT import — pure dict diff over visible app state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from taskvm.harness.state_adapter import StateAdapter
from taskvm.task_state.entity_binding import TaskBinding


@dataclass
class FieldConflict:
    """One field whose underlying app state diverged from the projected value."""
    app: str
    entity_id: str
    field: str
    projected: Any          # the value the read-only zone is showing (Y)
    underlying: Any         # the real app state now (X)
    var_id: str | None = None   # the variable whose binding points here (for merge)

    @property
    def is_conflict(self) -> bool:
        return not _eq(self.projected, self.underlying)

    def to_dict(self) -> dict:
        return {"app": self.app, "entity_id": self.entity_id, "field": self.field,
                "projected": self.projected, "underlying": self.underlying,
                "var_id": self.var_id}


@dataclass
class ReconciliationResult:
    """The set of conflicts detected by re-reading canonical state vs the last
    projected snapshot. ``conflicts`` = the divergent fields (amber-marked);
    ``clean`` = fields that agree (re-projected normally)."""
    conflicts: list[FieldConflict] = field(default_factory=list)
    clean: list[dict] = field(default_factory=list)
    n_conflicts: int = 0

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    def to_dict(self) -> dict:
        return {"n_conflicts": self.n_conflicts,
                "conflicts": [c.to_dict() for c in self.conflicts],
                "n_clean": len(self.clean)}


def _eq(a: Any, b: Any) -> bool:
    """Tolerant equality (string-trim + case-insensitive), mirroring
    canonical_state._eq so a trailing-space/case difference is not a false conflict."""
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def detect_conflicts(binding: TaskBinding,
                     projected_snapshot: dict[str, dict[str, Any]],
                     fresh_canonical: dict[str, dict]) -> ReconciliationResult:
    """Re-read canonical state and diff against the last-projected values, for
    EVERY binding of every variable (not just the primary).

    A variable may bind multiple entities across apps (e.g. ``release_date`` →
    calendar.E1.date + taskboard.T1/T2.deadline). The projection's ``value``
    (Y) comes from the primary binding, but a concurrent external change to ANY
    bound entity is a conflict — the variable's cross-app linkage is broken.
    So we check EVERY binding's underlying value against the variable's
    projected value Y: if any bound entity's underlying ≠ Y, that's a conflict
    on that (app, entity_id, field).

    ``projected_snapshot`` = {var_id: {value (Y), bindings: [...], ...}} from
    ``project_readonly``. ``fresh_canonical`` = fresh {app: read_canonical(sid)}.
    Both visible app state — no GT.
    """
    res = ReconciliationResult()
    for vid, info in projected_snapshot.items():
        projected = info.get("value")
        bindings = info.get("bindings") or []
        if not bindings:
            # fall back to the primary (app/entity_id/field) on the info dict
            bindings = [{"app": info.get("app"), "entity_id": info.get("entity_id"),
                         "field": info.get("field")}]
        for b in bindings:
            app = b.get("app")
            eid = b.get("entity_id")
            field = b.get("field")
            if not (app and eid and field):
                continue
            entities = (fresh_canonical.get(app) or {}).get("entities") or {}
            underlying = (entities.get(eid) or {}).get(field)
            fc = FieldConflict(app=app, entity_id=eid, field=field,
                               projected=projected, underlying=underlying, var_id=vid)
            if fc.is_conflict:
                res.conflicts.append(fc)
            else:
                res.clean.append({"var_id": vid, "app": app, "entity_id": eid,
                                  "field": field, "value": underlying})
    res.n_conflicts = len(res.conflicts)
    return res


def merge_options(conflict: FieldConflict) -> list[dict]:
    """The merge affordances for one conflict — presented to the user, NEVER
    auto-applied (handoff §5 inv 4). The user picks one; the agent is not
    blocked either way (handoff §5 inv 4 / §7 item 7).

    Three options:
      - ``accept_underlying``: adopt the real app state X (the world moved; user
        accepts it). Re-projects X, drops the stale Y.
      - ``keep_projected``: write the projected Y back to the app (the user's
        view wins; re-dispatch Y via the binding's operator).
      - ``merge``: user supplies a resolved value Z (neither X nor Y); re-dispatch Z.
    Each option names the executable operator + value so the workspace can apply
    it on the user's click WITHOUT consulting the model (governance, not LLM)."""
    return [
        {"option": "accept_underlying", "label": "采用底层值（现 X）",
         "app": conflict.app, "entity_id": conflict.entity_id,
         "field": conflict.field, "value": conflict.underlying,
         "effect": "re-project X; drop stale Y (no app write — just resync)"},
        {"option": "keep_projected", "label": "保留我的投影（写回 Y）",
         "app": conflict.app, "entity_id": conflict.entity_id,
         "field": conflict.field, "value": conflict.projected,
         "effect": "re-dispatch Y via the binding's operator (app ← Y)"},
        {"option": "merge", "label": "合并（输入新值 Z）",
         "app": conflict.app, "entity_id": conflict.entity_id,
         "field": conflict.field, "value": None,
         "effect": "user supplies Z; re-dispatch Z via the binding's operator"},
    ]


def apply_merge_option(conflict: FieldConflict, option: str,
                       resolved_value: Any | None,
                       adapters: dict[str, StateAdapter], sid: str,
                       binding: TaskBinding) -> dict:
    """Apply ONE merge option the user picked. Finds the executable operator for
    the conflict's binding in ``binding`` (the var_id → operator map the compiler
    already discovered) and re-dispatches. ``accept_underlying`` writes nothing
    (just re-projects). Returns the mutate response or a no-op marker.

    This is the ONLY place reconciliation writes to an app, and ONLY on explicit
    user choice — never auto, never silent (handoff §5 inv 4)."""
    # find the operator for this conflict's binding
    operator = None
    for v in binding.variables:
        for b in (v.get("bindings") or []):
            if b.get("app") == conflict.app and b.get("entity_id") == conflict.entity_id \
                    and b.get("field") == conflict.field:
                operator = b.get("operator"); break
        if operator: break
    if option == "accept_underlying":
        return {"option": "accept_underlying", "wrote": False,
                "value": conflict.underlying,
                "note": "re-projected underlying; no app write"}
    if option == "keep_projected":
        value = conflict.projected
    elif option == "merge":
        value = resolved_value
        if value is None:
            return {"option": "merge", "wrote": False,
                    "error": "merge requires resolved_value"}
    else:
        return {"option": option, "wrote": False, "error": f"unknown option {option}"}
    if operator is None:
        return {"option": option, "wrote": False,
                "error": f"no operator found for {conflict.app}.{conflict.entity_id}.{conflict.field}"}
    ad = adapters.get(conflict.app)
    if ad is None:
        return {"option": option, "wrote": False,
                "error": f"no adapter for app {conflict.app}"}
    resp = ad.mutate(sid, conflict.entity_id, operator, value)
    return {"option": option, "wrote": True, "value": value,
            "operator": operator, "response": resp}
