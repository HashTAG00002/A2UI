"""live_sync — project + re-sync the two-zone surface from real app state.

W2 scope (handoff §6 item 2 + §7 item 14): the read-only zone is projected
**bottom-up** from ``StateAdapter.read_canonical`` (the real visible app state)
through the binding's ``app/entity_id/field`` — NOT from the compiler's possibly
stale ``variable.value`` (which is frequently ``None`` — see the W1 ``_to_task_binding``
gap). Re-sync is **re-read-on-action** (the user edits → dispatch → re-render
reads canonical again). This is the heartbeat/re-read half of reconciliation;
conflict-marking on concurrent external edits is W3 (``verifier/reconciliation``)
and is NOT built here.

**No-leak invariant**: ``project_readonly`` / ``resync_values`` read ONLY
``read_canonical`` (visible app state). They never import ``benchmark/fixtures``
and never read ``expected_diff`` or ``user_edit.old`` — those are verifier-only
GT. The binding (var→entity mapping) is the same shape the compiler emits; it
is not secret.
"""
from __future__ import annotations

from typing import Any

from taskvm.substrate import EvaluationEnvironment   # port type only (Agent B)
from taskvm.task_state.entity_binding import TaskBinding


def project_readonly(binding: TaskBinding,
                     canonical: dict[str, dict]) -> dict[str, dict[str, Any]]:
    """Build the read-only zone's data: for each variable, its current value
    read from the canonical snapshot via its (primary) binding.

    ``canonical`` = ``{app: read_canonical(sid)}`` = ``{app: {"entities": {id: {field: val}}}}``.

    Returns ``{var_id: {"label", "value", "app", "entity_id", "field",
                         "editable", "bindings": [...]}}``. The ``value`` is the
    REAL current app state (not the compiler's stale value).
    """
    out: dict[str, dict[str, Any]] = {}
    for v in binding.variables:
        vid = v.get("var_id")
        bindings = v.get("bindings") or []
        primary = bindings[0] if bindings else {}
        app = primary.get("app")
        eid = primary.get("entity_id")
        field = primary.get("field")
        value = None
        if app and eid and field:
            entities = (canonical.get(app) or {}).get("entities") or {}
            value = (entities.get(eid) or {}).get(field)
        out[vid] = {"label": v.get("label", vid), "value": value,
                    "app": app, "entity_id": eid, "field": field,
                    "editable": v.get("editable", True),
                    "bindings": [{"app": b.get("app"), "entity_id": b.get("entity_id"),
                                  "field": b.get("field"), "operator": b.get("operator")}
                                 for b in bindings]}
    return out


def resync_values(binding: TaskBinding,
                  adapters: dict[str, EvaluationEnvironment],
                  sid: str) -> dict[str, dict[str, Any]]:
    """Re-read canonical state from every app and project the read-only zone.
    This is the re-read-on-action / heartbeat re-sync (W2; no conflict-marking).
    (Agent B note — registered blocker for Agent D: the production
    projection should resync from SubstrateSession.observe() (visible
    observation), not from the evaluation plane. Kept on the evaluation
    read for behavior continuity in this wave.)"""
    canonical = {name: ad.oracle_state(sid) for name, ad in adapters.items()}
    return project_readonly(binding, canonical)


def resync_with_conflicts(binding: TaskBinding,
                          projected_snapshot: dict[str, dict[str, Any]],
                          adapters: dict[str, EvaluationEnvironment],
                          sid: str) -> tuple[dict[str, dict[str, Any]], "ReconciliationResult"]:
    """Re-read canonical state and detect concurrent-external-change conflicts
    (W3 reconciliation, handoff §5 inv 4-5).

    ``projected_snapshot`` = the read-only zone's CURRENT projected values (from
    ``project_readonly`` — what the user is looking at, Y). We re-read canonical
    (X) and diff: any field where X ≠ Y is a conflict (amber-marked, NOT
    silently overwritten); agreeing fields are re-projected normally.

    Returns (updated_projection, reconciliation_result) where the updated
    projection keeps the user's projected Y for conflicting fields (so the UI
    shows both Y and X) and re-projects X for clean fields. The reconciliation
    result carries the conflict list + merge options for the template to render.

    No silent overwrite (§5 inv 4): conflicting fields show BOTH values. No
    human-block (§5 inv 4 / §7 item 7): the agent is not paused — conflicts are
    surfaced as affordances the user MAY act on.
    """
    from taskvm.verifier.reconciliation import detect_conflicts
    fresh_canonical = {name: ad.oracle_state(sid) for name, ad in adapters.items()}
    recon = detect_conflicts(binding, projected_snapshot, fresh_canonical)
    # build the updated projection: clean fields → fresh value; conflicts → keep
    # projected (Y) but tag with the conflict (so the template shows Y + X + merge)
    conflict_by_var = {c.var_id: c for c in recon.conflicts}
    out: dict[str, dict[str, Any]] = {}
    for vid, info in projected_snapshot.items():
        if vid in conflict_by_var:
            c = conflict_by_var[vid]
            out[vid] = {**info, "conflict": {
                "underlying": c.underlying, "projected": c.projected,
                "app": c.app, "entity_id": c.entity_id, "field": c.field}}
        else:
            # clean: re-project the fresh canonical value
            app, eid, field = info.get("app"), info.get("entity_id"), info.get("field")
            entities = (fresh_canonical.get(app) or {}).get("entities") or {}
            out[vid] = {**info, "value": (entities.get(eid) or {}).get(field)}
    return out, recon


def canonical_snapshot(adapters: dict[str, EvaluationEnvironment], sid: str) -> dict[str, dict]:
    """{app: oracle_state(sid)} — the raw canonical snapshot (for checkpointing
    / comparing pre vs post)."""
    return {name: ad.oracle_state(sid) for name, ad in adapters.items()}
