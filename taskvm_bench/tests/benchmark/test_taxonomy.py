"""Benchmark taxonomy integrity: 15 tasks, 12 structural families, 5
open-world splits, holdout honesty (a held-out surface/operation must be
absent from every ID task), suite/condition registry completeness."""
from taskvm_bench.benchmark.registry import (
    ABLATION_CONDITIONS, PRIMARY_CONDITIONS, SUITES, all_conditions,
    list_suites,
)
from taskvm_bench.benchmark.schema import Family, Split
from taskvm_bench.benchmark.tasks import all_tasks, get_task, tasks_in_split


def test_taxonomy_shape():
    tasks = all_tasks()
    assert len(tasks) == 15
    ids = [t.task_id for t in tasks]
    assert len(set(ids)) == len(ids), "task ids must be unique"
    # all 12 structural families covered
    fams = {t.family for t in tasks}
    assert fams == set(Family), (
        f"families missing: {set(Family) - fams}")
    # all 5 open-world splits covered
    splits = {t.split for t in tasks}
    assert splits == set(Split), (
        f"splits missing: {set(Split) - splits}")


def test_holdout_honesty_surface_and_operation():
    """SURFACE_HOLDOUT integrity: the held-out app (venues) must appear in
    NO ID task (seed, goal, or surfaces); OPERATION_HOLDOUT integrity: the
    held-out semantics (rsvp) must appear in NO ID task goal."""
    id_tasks = tasks_in_split(Split.ID)
    assert id_tasks, "ID split is empty"
    for t in id_tasks:
        blob = (t.goal + " " + repr(t.seed) + " " + repr(t.success))
        assert "venues" not in blob, (
            f"{t.task_id}: held-out surface leaked into an ID task")
        assert "rsvp" not in blob.lower(), (
            f"{t.task_id}: held-out operation leaked into an ID task")
    # the holdout tasks themselves exist and are labelled correctly
    assert get_task("venues-book").split is Split.SURFACE_HOLDOUT
    assert get_task("rsvp-confirm").split is Split.OPERATION_HOLDOUT
    assert get_task("venues-rsvp").split is Split.CROSS_PRODUCT


def test_spec_internal_consistency():
    for t in all_tasks():
        # success/protected reference declared surfaces (schema enforces;
        # this pins the contract against accidental schema weakening)
        for surf in t.success:
            assert surf in t.surfaces
        for surf, _key in t.protected:
            assert surf in t.surfaces, (
                f"{t.task_id}: protected references {surf!r}")
        # every task must answer a scientific question
        assert t.notes.strip(), f"{t.task_id} has no notes (no question)"


def test_registry_suites_and_conditions():
    for s in list_suites():
        assert s.task_ids and all(s.task_ids)
        for tid in s.task_ids:
            get_task(tid)            # KeyError if unknown
    assert set(SUITES) == {"smoke", "final", "open-world", "governance"}
    # final covers the whole taxonomy exactly
    final = SUITES["final"]
    assert set(final.task_ids) == {t.task_id for t in all_tasks()}
    # 4 primary + 2 ablations; the diagnostic upper bound is registered
    conds = all_conditions()
    assert len(conds) == 6
    assert len(PRIMARY_CONDITIONS) == 4
    assert len(ABLATION_CONDITIONS) == 2
    from taskvm_bench.benchmark.registry import (
        Condition, DIAGNOSTIC_ONLY_CONDITIONS,
    )
    assert DIAGNOSTIC_ONLY_CONDITIONS == frozenset(
        {Condition.TASKVM_ORACLE_UPPER_BOUND})
