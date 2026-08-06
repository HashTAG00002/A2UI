"""Round-trip checks — the AUTHORITATIVE cross-app GT judgment.

Three checks, all reading hidden canonical state (``read_canonical``) compared
against ``CanonicalTaskGraph`` (verifier-only GT from ``benchmark/fixtures.py``):

  1. changed-happened: each binding's ``expected_value_after_edit`` is met in
     the real post-state.
  2. non-interference: every entity in ``non_interference_set`` is unchanged
     (HARD constraint — fails the run if violated).
  3. interface-re-synced: re-render the surface from the post-state canonical
     value of the edited variable; assert it shows the new value (structural,
     no model).

Score = 0.5·changed + 0.3·untouched + 0.2·resynced (AOHP-style checkpoint-
weighted, doc §3 AOHP borrow — partial credit for partial cross-app success,
since there's no atomic cross-app txn, §14-6). **non-interference is a hard
gate**: if it fails, the score is clamped to ≤0.3 regardless of the other
checks (an over-applied dispatch must not pass).

Honesty invariant: reads ONLY canonical state + the fixture GT; never consults
the compiler's binding. No model self-judgment.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from taskvm.benchmark.fixtures import CanonicalTaskGraph
from taskvm.harness.state_adapter import StateAdapter
from taskvm.verifier.canonical_state import (entity_value, field_matches,
                                              entity_record)
from taskvm.verifier.non_interference import check_non_interference
from taskvm.workspace_ui.renderer import (render, edited_variable_shows_value,
                                           render_variable_value)


@dataclass
class CheckResult:
    fraction: float
    info: dict


@dataclass
class RoundTripResult:
    score: float
    changed: CheckResult
    untouched: CheckResult
    resynced: CheckResult
    non_interference_passed: bool
    hard_fail: bool          # True iff non-interference violated (score clamped)
    info: dict


def check_changed_happened(post: dict, fixture: CanonicalTaskGraph) -> CheckResult:
    """Each binding's expected_value_after_edit met in the real post-state."""
    bindings = fixture.bindings
    if not bindings:
        return CheckResult(1.0, {"n_bindings": 0, "met": [], "missed": []})
    met, missed = [], []
    for b in bindings:
        ok = field_matches(post, b.app, b.entity_id, b.field,
                           b.expected_value_after_edit)
        actual = entity_value(post, b.app, b.entity_id, b.field)
        (met if ok else missed).append({
            "var_id": b.var_id, "app": b.app, "entity_id": b.entity_id,
            "field": b.field, "operator": b.operator,
            "expected": b.expected_value_after_edit, "actual": actual, "met": ok})
    fraction = len(met) / len(bindings)
    return CheckResult(fraction, {"n_bindings": len(bindings),
                                   "n_met": len(met), "met": met, "missed": missed})


def check_interface_resynced(post: dict, fixture: CanonicalTaskGraph) -> CheckResult:
    """Re-render the surface from the post-state canonical value of the edited
    variable; assert it shows the new value. Structural, no model.

    W1: we check the edited variable's new value is reflected in the re-rendered
    surface (the surface WOULD re-sync). A full re-compile (model) is W2; the
    verifier uses the GT graph + the real post-state value here.
    """
    edit = fixture.user_edit
    var_id = edit.get("var_id")
    new_value = edit.get("new")
    # find the edited variable's canonical post-state value (from its primary
    # binding — the first binding for this var_id)
    primary = next((b for b in fixture.bindings if b.var_id == var_id), None)
    if primary is None:
        return CheckResult(0.0, {"error": f"no binding for edited var {var_id}"})
    real_post_value = entity_value(post, primary.app, primary.entity_id, primary.field)
    # render one line from the real post-state value
    line = render_variable_value(_DummyBinding(fixture), var_id, real_post_value)
    ok = edited_variable_shows_value(line, var_id, new_value) and \
         str(real_post_value).strip().lower() == str(new_value).strip().lower()
    return CheckResult(1.0 if ok else 0.0, {
        "var_id": var_id, "expected_new": new_value,
        "real_post_value": real_post_value, "rendered_line": line, "met": ok})


@dataclass
class _DummyBinding:
    """Minimal stand-in so render_variable_value can look up a label. The
    verifier renders from GT, so it knows the variable's label from the fixture
    bindings (not the compiler's binding)."""
    fixture: CanonicalTaskGraph

    @property
    def variables(self):
        # group fixture bindings by var_id to synthesize variable records
        seen = {}
        for b in self.fixture.bindings:
            if b.var_id not in seen:
                seen[b.var_id] = {"var_id": b.var_id, "label": b.var_id,
                                  "value": None, "editable": True, "bindings": []}
        return list(seen.values())

    @property
    def dependencies(self):
        return []

    @property
    def task_id(self):
        return self.fixture.task_id


def check_round_trip(sid: str, fixture: CanonicalTaskGraph,
                     adapters: dict[str, StateAdapter],
                     pre_snapshot: dict) -> RoundTripResult:
    """Run all three checks. Returns RoundTripResult with score + per-check info.

    ``pre_snapshot`` = ``canonical_state.snapshot(adapters, sid)`` taken BEFORE
    dispatch (the orchestrator captures it).
    """
    post = {name: ad.read_canonical(sid) for name, ad in adapters.items()}

    changed = check_changed_happened(post, fixture)
    untouched = check_non_interference(pre_snapshot, post, fixture.non_interference_set)
    resynced = check_interface_resynced(post, fixture)

    score = (0.5 * changed.fraction
             + 0.3 * untouched.fraction
             + 0.2 * resynced.fraction)

    hard_fail = not untouched.passed
    # non-interference is a HARD gate: clamp score to ≤0.3 if violated
    if hard_fail:
        score = min(score, 0.3)

    return RoundTripResult(
        score=round(score, 4),
        changed=changed, untouched=CheckResult(untouched.fraction, untouched.info),
        resynced=resynced,
        non_interference_passed=untouched.passed, hard_fail=hard_fail,
        info={"task_id": fixture.task_id,
              "changed": changed.info, "untouched": untouched.info,
              "resynced": resynced.info, "hard_fail": hard_fail,
              "weights": {"changed": 0.5, "untouched": 0.3, "resynced": 0.2}})


def binding_accuracy(compiler_binding, fixture: CanonicalTaskGraph) -> dict:
    """Compare the compiler's discovered binding against the GT bindings (for the
    binding-accuracy metric — NOT a gate, a diagnostic). Counts how many GT
    (var_id, app, entity_id, operator) tuples the compiler recovered."""
    gt = {(b.var_id, b.app, b.entity_id, b.operator) for b in fixture.bindings}
    got = set()
    if compiler_binding is not None:
        for v in (compiler_binding.get("variables") or []):
            vid = v.get("var_id")
            for b in (v.get("bindings") or []):
                got.add((vid, b.get("app"), b.get("entity_id"), b.get("operator")))
    tp = len(gt & got)
    fp = len(got - gt)
    fn = len(gt - got)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"n_gt": len(gt), "tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(f1, 4),
            "gt": sorted([list(t) for t in gt]),
            "got": sorted([list(t) for t in got])}
