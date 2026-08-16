"""Baseline 4 — human-binding upper bound (the GT binding itself).

NOT a baseline to beat — a calibration anchor (handoff §6 item 3). Confirms the
scoring pipeline tops out at F1=1.0 and that the gate is reachable. The
"binding" is built from the fixture's GT — but to preserve the no-leak boundary,
this baseline is ONLY usable from the VERIFIER/orchestrator path (which already
holds the fixture), NEVER from the compiler path. The orchestrator must pass the
fixture in via ``gt_fixture=``; if it doesn't, this baseline returns ok=False
(refuses to fabricate a binding without the GT).

This mirrors W1's ``_gt_task_binding`` / ``_mock_compiler_output`` (the mock
path) — co-located as a baseline so the benchmark can report the upper-bound F1
alongside the real methods.
"""
from __future__ import annotations

from taskvm_bench.baselines.base import _ok, register
from taskvm_bench.benchmark.fixtures import CanonicalTaskGraph
from taskvm_bench.harness.observations import TraceFixture


def _gt_binding_dict(fixture: CanonicalTaskGraph) -> dict:
    var_groups: dict[str, dict] = {}
    for b in fixture.bindings:
        g = var_groups.setdefault(b.var_id, {
            "var_id": b.var_id, "label": b.var_id,
            "value": fixture.user_edit.get("old"), "editable": True, "bindings": []})
        g["bindings"].append({"var_id": b.var_id, "app": b.app,
                              "entity_id": b.entity_id, "field": b.field,
                              "operator": b.operator})
    return {"task_id": fixture.task_id, "variables": list(var_groups.values()),
            "dependencies": []}


def discover(trace: TraceFixture, observed_entity_ids: dict[str, set[str]],
             *, gt_fixture: CanonicalTaskGraph | None = None, **kw) -> dict:
    """The GT binding (upper bound). Requires ``gt_fixture`` from the
    orchestrator (verifier path only — preserves no-leak for the compiler path)."""
    if gt_fixture is None:
        return _ok(raw="", tb=None,
                   error="human_upper_bound requires gt_fixture (verifier path only)")
    tb = _gt_binding_dict(gt_fixture)
    return _ok(raw="(human_upper_bound: GT binding)", tb=tb)


register("human_upper_bound", discover, role="upper_bound", uses_model=False,
         description="the GT binding itself — calibration anchor confirming the "
                    "scoring tops out at F1=1.0 (verifier-path only, no-leak)")
