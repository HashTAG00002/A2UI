"""Token accounting in the aggregate (audit A-05): report schema /2
replaces the old ``mean_tokens_by_role`` — which output raw sums under a
mean name — with explicit totals, per-trial / per-request means and the
request counts that name their denominators.

Missing usage stays missing: a request whose tokens the provider never
reported never enters a token sum or a token-mean denominator (never
zero-filled), while still counting in ``n_requests_by_role``.
"""
from __future__ import annotations

import pytest

from taskvm_bench.evaluation.aggregation import (
    REPORT_SCHEMA, aggregate_trials, report_from_trials,
)


def _rec(task_id: str, *, calls: dict, tokens: dict, **over) -> dict:
    base = dict(run_id="t", task_id=task_id, family="sequence",
                split="id", condition="taskvm", seed=0,
                stop_reason="done", verdict={"success": True},
                evaluation_error=None, harness_crash=None,
                model_calls_by_role=calls, model_tokens_by_role=tokens,
                gui_actions=1, total_interactions=1, required_ops=1,
                elapsed_ms=1.0, system_writes=1, injections_fired=[],
                trace=[], detail="", extras={})
    base.update(over)
    return base


def _token_trials() -> list[dict]:
    """3 records under one condition (repair-driven trials included —
    ``is_repair`` is pass-through data the aggregate must not choke on)."""
    return [
        # trial 1 — fully metered
        _rec("t1", calls={"planner": 2, "cua": 3},
             tokens={"planner": [100, 40], "cua": [200, 60]}),
        # trial 2 — cua made calls but reported no usage (None pair:
        # honest missing, never 0); repair role is metered
        _rec("t2", calls={"planner": 1, "cua": 2, "repair": 1},
             tokens={"planner": [50, 20], "cua": [None, None],
                     "repair": [30, 10]},
             is_repair=True),
        # trial 3 — repair-driven trial with no token meter at all
        _rec("t3", calls={"cua": 2}, tokens={}, is_repair=True),
    ]


def test_token_fields_hand_computed():
    b = aggregate_trials(_token_trials())["by_condition"]["taskvm"]
    # the misnamed field is gone — sums may never pose as means again
    assert "mean_tokens_by_role" not in b
    # totals: only metered (trial, role) pairs contribute
    assert b["total_tokens_by_role"] == {
        "planner": [150, 60], "cua": [200, 60], "repair": [30, 10]}
    # every request counts, metered or not
    assert b["n_requests_by_role"] == {"planner": 3, "cua": 7, "repair": 1}
    # only requests inside metered trials enter the per-request mean
    assert b["n_requests_with_usage_by_role"] == {
        "planner": 3, "cua": 3, "repair": 1}
    # per-trial mean: denominator = trials with metered usage for the role
    assert b["mean_tokens_per_trial_by_role"] == {
        "planner": [75.0, 30.0],      # 150/2, 60/2 (t1+t2 metered)
        "cua": [200.0, 60.0],         # only t1 metered → /1
        "repair": [30.0, 10.0]}
    # per-request mean: denominator = requests in metered trials
    assert b["mean_tokens_per_request_by_role"] == {
        "planner": [50.0, 20.0],      # 150/3, 60/3
        "cua": pytest.approx([200 / 3, 20.0]),   # 200/3, 60/3
        "repair": [30.0, 10.0]}
    # the (already correct) call means are unchanged by this fix
    assert b["total_model_calls"] == 11
    assert b["mean_model_calls_by_role"] == pytest.approx(
        {"planner": 1.5, "cua": 7 / 3, "repair": 1.0})


def test_report_schema_version_bumped():
    """/2 is a real schema change: a /1 consumer must refuse the new
    token fields instead of silently re-reading them with old semantics."""
    assert REPORT_SCHEMA == "taskvm_bench.evaluation.report/2"
    rep = report_from_trials({"run_id": "t"}, _token_trials())
    assert rep["schema"] == REPORT_SCHEMA
    b = rep["by_condition"]["taskvm"]
    assert "mean_tokens_by_role" not in b
    for f in ("total_tokens_by_role", "mean_tokens_per_trial_by_role",
              "mean_tokens_per_request_by_role", "n_requests_by_role"):
        assert f in b, f"schema/2 must expose {f}"


def test_missing_usage_never_zero_filled():
    """A role whose every trial lacks usage reports no token figures at
    all (absent, not zero), while its requests still count."""
    recs = [
        _rec("t1", calls={"cua": 3}, tokens={}),
        _rec("t2", calls={"cua": 2}, tokens={"cua": [None, None]}),
    ]
    b = aggregate_trials(recs)["by_condition"]["taskvm"]
    assert b["n_requests_by_role"] == {"cua": 5}
    assert b["total_tokens_by_role"] == {}
    assert b["n_requests_with_usage_by_role"] == {}
    assert b["mean_tokens_per_trial_by_role"] == {}
    assert b["mean_tokens_per_request_by_role"] == {}
