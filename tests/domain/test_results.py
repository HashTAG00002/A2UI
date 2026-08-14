"""Typed landing results (layered ownership protocol §2): the TYPE makes
bad input inexpressible.

- ``VerificationResult`` binds ONE node, ONE epoch, and for ACTION work
  exactly ONE finished attempt — there is no bare-bool channel.
- ``CompensationResult`` can reference ONLY entries of its plan
  (``for_plan`` construction) — there is no free-form
  ``dict[semantic_key, value]`` synchronisation channel, no way to smuggle
  an extra key, no way to report on a foreign node.

The kernel-side landing rules (epoch / single-use / coverage /
disposition) live in tests/kernel/test_timeline_governance.py.
"""
import pytest

from taskvm.domain import (
    CompensationEntry,
    CompensationEntryResult,
    CompensationPlan,
    CompensationResult,
    Reversibility,
    ValidationError,
    VerificationResult,
)


def _plan():
    return CompensationPlan(
        plan_id="comp:00001", target_checkpoint_id="ckpt:C0", epoch=1,
        entries=(
            CompensationEntry(node_id="a2", semantic_key="x",
                              from_observed=3, to_observed=2, to_desired=2,
                              reversibility=Reversibility.REVERSIBLE),
            CompensationEntry(node_id="a1", semantic_key="x",
                              from_observed=2, to_observed=1, to_desired=1,
                              reversibility=Reversibility.REVERSIBLE),
        ))


# ── VerificationResult shape ───────────────────────────────────────────────
def test_verification_result_requires_identity():
    with pytest.raises(ValidationError):
        VerificationResult(node_id="", epoch=0, passed=True)
    with pytest.raises(ValidationError):
        VerificationResult(node_id="a1", epoch=-1, passed=True)


def test_verification_result_binds_one_attempt():
    r = VerificationResult(node_id="a1", epoch=2, passed=True,
                           action_id="action:00007", evidence_ref="shot#42")
    assert r.action_id == "action:00007"
    assert r.passed is True


# ── CompensationResult: only plan entries are expressible ──────────────────
def test_compensation_result_cannot_name_foreign_entries():
    plan = _plan()
    with pytest.raises(ValidationError, match="do not correspond"):
        CompensationResult.for_plan(plan, epoch=1, outcomes=[
            CompensationEntryResult(node_id="a2", semantic_key="y",
                                    final_observed=1, compensated=True)])
    with pytest.raises(ValidationError, match="do not correspond"):
        CompensationResult.for_plan(plan, epoch=1, outcomes=[
            CompensationEntryResult(node_id="a9", semantic_key="x",
                                    final_observed=1, compensated=True)])


def test_compensation_result_rejects_duplicate_entry_results():
    with pytest.raises(ValidationError, match="duplicate"):
        CompensationResult(plan_id="comp:00001", epoch=1, entry_results=(
            CompensationEntryResult(node_id="a2", semantic_key="x",
                                    final_observed=2, compensated=True),
            CompensationEntryResult(node_id="a2", semantic_key="x",
                                    final_observed=2, compensated=True),
        ))


def test_compensation_result_for_plan_roundtrip():
    plan = _plan()
    res = CompensationResult.for_plan(plan, epoch=1, outcomes=[
        CompensationEntryResult(node_id="a2", semantic_key="x",
                                final_observed=2, compensated=True),
        CompensationEntryResult(node_id="a1", semantic_key="x",
                                final_observed=1, compensated=True),
    ])
    assert res.plan_id == "comp:00001"
    assert [r.compensated for r in res.entry_results] == [True, True]


def test_no_freeform_dict_channel():
    """The old ``observed_values: dict[str, Any]`` synchronisation entry is
    gone — the type system has no parameter that could carry it."""
    with pytest.raises(TypeError):
        CompensationResult(plan_id="comp:00001", epoch=1,
                           observed_values={"x": 1})
    with pytest.raises(TypeError):
        VerificationResult(node_id="a1", epoch=0, passed=True,
                           observed_values={"x": 1})


def test_compensation_result_requires_plan_identity():
    with pytest.raises(ValidationError):
        CompensationResult(plan_id="", epoch=1)
    with pytest.raises(ValidationError):
        CompensationResult(plan_id="comp:00001", epoch=-1)
