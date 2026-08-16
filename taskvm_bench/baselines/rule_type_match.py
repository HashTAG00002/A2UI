"""Baseline 1 — rule/type-match (no model). The "is this just a dashboard?"
floor (handoff §6 item 3).

Deterministic heuristics over the observed a11y/DOM: for each app, find the
entity whose field name matches a date-like / location-like / state-like
pattern, and bind it to the operator in OPERATOR_REGISTRY that writes that
field. No model call. This is the LOWER bound — if the frontier compiler
doesn't beat rule/type-match, the "compiler" claim is hollow (it's a hardcoded
dashboard, handoff §10 negative sample).

No-leak: reads ONLY ``trace.final_obs`` (a11y/DOM) + ``OPERATOR_REGISTRY``
(compiler-visible operator signatures). Never imports fixtures.
"""
from __future__ import annotations

from typing import Any

from taskvm_bench.baselines.base import _ok, register
from taskvm_bench.harness.observations import TraceFixture
from taskvm_bench.task_state.entity_binding import OPERATOR_REGISTRY

# field-name → "kind of quantity" heuristics (no var_id; var_id is assigned per-app)
_FIELD_KIND = {
    "date": "date", "deadline": "date", "scheduled_for": "date",
    "parent": "location", "folder": "location",
    "state": "state", "status": "state",
    "priority": "priority", "assignee": "assignee", "owner": "assignee",
}


def discover(trace: TraceFixture, observed_entity_ids: dict[str, set[str]],
             *, model: str | None = None, cost_model=None, **kw) -> dict:
    """Rule/type-match: for each app, pick the FIRST entity whose field matches a
    known kind, bind it to the operator that writes that field. One var per
    (app, kind). Returns a task_binding dict (same shape as the compiler)."""
    variables: list[dict] = []
    # group operators by app+field
    ops_by_app_field: dict[tuple[str, str], str] = {}
    for op, meta in OPERATOR_REGISTRY.items():
        ops_by_app_field[(meta["app"], meta["field"])] = op
    for app, ids in observed_entity_ids.items():
        obs = trace.final_obs.get(app)
        if obs is None:
            continue
        # parse the a11y to find each entity's fields (reuse replay_engine)
        from taskvm_bench.harness.replay_engine import parse_dom_entities
        entities = parse_dom_entities(obs.dom_html)
        for eid in ids:
            fields = entities.get(eid) or {}
            for fname, fval in fields.items():
                kind = _FIELD_KIND.get(fname)
                if kind is None:
                    continue
                op = ops_by_app_field.get((app, fname))
                if op is None:
                    continue
                var_id = f"{app}_{kind}"
                # one var per (app, kind) — merge entities of the same kind
                v = next((v for v in variables if v["var_id"] == var_id), None)
                if v is None:
                    v = {"var_id": var_id, "label": f"{app} {kind}",
                         "value": fval, "editable": True, "bindings": []}
                    variables.append(v)
                v["bindings"].append({"var_id": var_id, "app": app, "entity_id": eid,
                                      "field": fname, "operator": op})
    tb = {"task_id": trace.task_id, "variables": variables, "dependencies": []}
    return _ok(raw=f"(rule_type_match: {len(variables)} vars)", tb=tb)


register("rule_type_match", discover, role="floor", uses_model=False,
         description="deterministic field-name/type heuristics, no model — the "
                    "'is this just a dashboard?' floor")
