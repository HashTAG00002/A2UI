"""cross_app_checks — cross-application consistency checks (W3, handoff §4.1).

W1's ``round_trip_checks`` verifies changed-happened per binding + non-
interference per entity. This module adds DEEPER cross-app consistency: after a
dispatch, do the apps AGREE about the quantities that link them?

The load-bearing cross-app invariant: when a task variable drives multiple
entities across apps (e.g. ``release_date`` → calendar.E1.date AND
taskboard.T1/T2.deadline), ALL of them must hold the SAME post-edit value. If
the dispatcher applied the edit to calendar but a dependent taskboard deadline
lagged (partial dispatch), this check catches the inconsistency that per-binding
changed-happened might miss (T1 changed to the new value, but T2 didn't — both
are "changed-happened" per-binding, but the cross-app linkage is broken).

Also checks the taskboard ``depends_on`` semantic: a task whose ``depends_on``
names the edited variable must have its deadline track that variable's new value
(a VM dependency edge made concrete in the app's own data).

Honesty: reads ONLY canonical state + the fixture's GT linkage (the
``dependencies`` + ``bindings`` in ``CanonicalTaskGraph``). No model self-judge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from taskvm.benchmark.fixtures import CanonicalTaskGraph
from taskvm.verifier.canonical_state import entity_value


@dataclass
class CrossAppCheckResult:
    score: float
    n_linkages: int
    n_consistent: int
    inconsistent: list[dict] = field(default_factory=list)
    info: dict = field(default_factory=dict)


def _variable_post_values(post: dict, fixture: CanonicalTaskGraph) -> dict[str, Any]:
    """For each task variable, the set of post-edit values across its bindings.
    A consistent variable has exactly one distinct value across all its bindings
    (all bound entities agree)."""
    var_values: dict[str, list[Any]] = {}
    for b in fixture.bindings:
        val = entity_value(post, b.app, b.entity_id, b.field)
        var_values.setdefault(b.var_id, []).append(val)
    return var_values


def check_cross_app_consistency(post: dict,
                                fixture: CanonicalTaskGraph) -> CrossAppCheckResult:
    """Check that every task variable's bound entities hold a CONSISTENT
    post-edit value across apps (the cross-app linkage invariant).

    A variable is consistent iff all its bindings' post-state values are equal
    (after tolerant string compare). An inconsistent variable means the dispatch
    propagated to some bound entities but not others — a partial cross-app write
    that per-binding changed-happened alone would not flag.

    Score = fraction of variables that are consistent. This is a diagnostic
    (NOT a hard gate like non-interference) — partial cross-app writes are
    honest partial credit per the AOHP-weighted round-trip score, but this check
    surfaces WHICH variable linkage broke so the failure analysis is precise.
    """
    var_values = _variable_post_values(post, fixture)
    inconsistent = []
    for vid, vals in var_values.items():
        # tolerant equality (string-trim + case-insensitive), mirroring canonical_state._eq
        norm = []
        for v in vals:
            norm.append(v.strip().lower() if isinstance(v, str) else v)
        if len(set(norm)) > 1:
            # find the bindings that disagree
            bindings = [b for b in fixture.bindings if b.var_id == vid]
            inconsistent.append({
                "var_id": vid,
                "post_values": [{"app": b.app, "entity_id": b.entity_id,
                                 "field": b.field, "value": entity_value(post, b.app, b.entity_id, b.field)}
                                for b in bindings]})
    n_linkages = len(var_values)
    n_consistent = n_linkages - len(inconsistent)
    score = n_consistent / n_linkages if n_linkages else 1.0
    return CrossAppCheckResult(
        score=round(score, 4), n_linkages=n_linkages, n_consistent=n_consistent,
        inconsistent=inconsistent,
        info={"n_linkages": n_linkages, "n_consistent": n_consistent,
              "n_inconsistent": len(inconsistent)})


def check_dependency_tracking(post: dict,
                              fixture: CanonicalTaskGraph) -> CrossAppCheckResult:
    """Check the taskboard ``depends_on`` semantic: a task whose ``depends_on``
    names a variable must have its deadline track that variable's post-edit value.

    This is the VM dependency edge (``Dependency.from_var → to_entity``) made
    concrete in the app's own data: if T1.depends_on=['release_date'] and
    release_date was edited to 8/18, then T1.deadline must be 8/18. Catches a
    dispatch that moved the calendar date but forgot to sync the dependent
    taskboard deadline (a real cross-app coordination failure).

    Uses the fixture's ``bindings`` to find each variable's post value, then
    checks every taskboard task whose ``depends_on`` mentions that variable."""
    var_post_value: dict[str, Any] = {}
    for b in fixture.bindings:
        var_post_value.setdefault(b.var_id, entity_value(post, b.app, b.entity_id, b.field))

    tb_tasks = (post.get("taskboard") or {}).get("entities") or {}
    inconsistent = []
    n_checked = 0
    for tid, task in tb_tasks.items():
        deps = task.get("depends_on") or []
        deadline = task.get("deadline")
        for dep_var in deps:
            if dep_var not in var_post_value:
                continue   # not an edited variable; nothing to check
            n_checked += 1
            expected = var_post_value[dep_var]
            actual = deadline
            ok = (str(expected).strip().lower() == str(actual).strip().lower()) \
                if expected is not None else False
            if not ok:
                inconsistent.append({"taskboard": tid, "depends_on": dep_var,
                                     "expected_deadline": expected,
                                     "actual_deadline": actual})
    n_consistent = n_checked - len(inconsistent)
    score = n_consistent / n_checked if n_checked else 1.0
    return CrossAppCheckResult(
        score=round(score, 4), n_linkages=n_checked, n_consistent=n_consistent,
        inconsistent=inconsistent,
        info={"n_dependency_links_checked": n_checked,
              "n_consistent": n_consistent, "n_inconsistent": len(inconsistent)})
