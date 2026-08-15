"""Runtime-visible verifier contract tests (runtime.md §6).

The ``VisibleVerifier`` is the single owner of verification CONTENT:
``VerificationResult.passed`` reflects an independent visible-world check —
the CUA may say DONE while the screen still shows the old value; the
verifier catches that. Evidence is the captured screenshot ref (never an
internal id). The kernel never re-runs or re-proves content.

Test taxonomy:
  §6.1  CUA done ≠ verified — desired_state NOT met ⇒ fail
  §6.2  CUA done = verified — desired_state met in fresh observation ⇒ pass
  §6.3  evidence_ref is the screenshot from the observation, not an internal id
  §6.4  VERIFY node — predicate-referenced variables checked
  §6.5  VERIFY node — no referenced variables ⇒ all desired checked
  §6.6  before/after come from fresh observations (immutability / honesty)
  §6.7  irreversible contract — verifier reports honestly regardless of
        reversibility (irreversibility is about *whether you can undo* not
        *whether you did it*)
"""
from __future__ import annotations

from typing import Any, Mapping

from taskvm.domain.contract import ActionContract, Reversibility
from taskvm.domain.results import VerificationResult
from taskvm.domain.workflow import NodeKind, WorkflowNode
from taskvm.substrate import Observation, SurfaceInfo
from taskvm.verifier.visible import VisibleVerifier

import pytest


# ── helpers ─────────────────────────────────────────────────────────────────
def _action_node(node_id="n1", desired=None, reversibility="reversible",
                 completion=""):
    return WorkflowNode(
        node_id=node_id, kind=NodeKind.ACTION, label=node_id,
        contract=ActionContract(
            contract_id=f"c-{node_id}",
            semantic_goal=f"realise {node_id}",
            desired_state=dict(desired or {}),
            completion_condition=completion,
            reversibility=Reversibility(reversibility)))


def _verify_node(node_id="v1", predicate=""):
    return WorkflowNode(
        node_id=node_id, kind=NodeKind.VERIFY, label=node_id,
        verification=predicate or "all desired met")


def _obs(ref="shot://app/1"):
    return Observation(
        surface=SurfaceInfo(surface_id="app", display_name="app"),
        revision=1, timestamp=0.0, screenshot_ref=ref,
        visible_text="x=A y=B", fingerprint="fp:1")


# ── §6.1 CUA done ≠ verified — fail ────────────────────────────────────────
def test_cua_done_but_desired_not_met_fails():
    """The CUA may report DONE, but if the fresh observation shows the old
    value, the verifier MUST fail (runtime.md §6, the load-bearing
    CUA-done-≠-verified discipline)."""
    v = VisibleVerifier()
    node = _action_node(desired={"x": "A"})
    # after_observed shows x is STILL "x0" (old value) — CUA lied or was wrong
    vr = v.verify(node=node, before_observed={"x": "x0"},
                  after_observed={"x": "x0"}, desired={"x": "A"},
                  observation=_obs(), action_id="a1", epoch=1)
    assert not vr.passed
    assert "x" in vr.detail or "unmet" in vr.detail


# ── §6.2 CUA done = verified — pass ────────────────────────────────────────
def test_cua_done_and_desired_met_passes():
    """When the fresh observation confirms the desired state, verification
    passes — but only because the VALUES match, not because the CUA said
    DONE."""
    v = VisibleVerifier()
    node = _action_node(desired={"x": "A"})
    vr = v.verify(node=node, before_observed={"x": "x0"},
                  after_observed={"x": "A"}, desired={"x": "A"},
                  observation=_obs(), action_id="a1", epoch=1)
    assert vr.passed
    assert vr.detail == "ok"


# ── §6.3 evidence_ref is the screenshot ────────────────────────────────────
def test_evidence_ref_is_screenshot_not_internal_id():
    """The evidence_ref is the screenshot_ref from the observation — never
    an internal entity_id or data-* attribute (runtime.md §6, E16/E21)."""
    v = VisibleVerifier()
    node = _action_node(desired={"x": "A"})
    vr = v.verify(node=node, before_observed={"x": "x0"},
                  after_observed={"x": "A"}, desired={"x": "A"},
                  observation=_obs(ref="shot://app/42"),
                  action_id="a1", epoch=1)
    assert vr.evidence_ref == "shot://app/42"
    # no internal id leaks
    assert "entity_id" not in vr.evidence_ref
    assert "data-" not in vr.evidence_ref


def test_evidence_ref_empty_when_no_screenshot():
    """If the observation has no screenshot_ref, evidence_ref is empty —
    never fabricated."""
    v = VisibleVerifier()
    node = _action_node(desired={"x": "A"})
    obs = Observation(
        surface=SurfaceInfo(surface_id="app", display_name="app"),
        revision=1, timestamp=0.0, screenshot_ref="",
        visible_text="x=A", fingerprint="fp:1")
    vr = v.verify(node=node, before_observed={"x": "x0"},
                  after_observed={"x": "A"}, desired={"x": "A"},
                  observation=obs, action_id="a1", epoch=1)
    assert vr.evidence_ref == ""


# ── §6.4 VERIFY node — predicate-referenced variables ──────────────────────
def test_verify_node_checks_referenced_variables():
    """A VERIFY node's predicate references known variables; only those are
    checked (not all desired)."""
    v = VisibleVerifier()
    # predicate references x only — y can be anything
    node = _verify_node(predicate="x equals A when done")
    vr = v.verify(node=node, before_observed={"x": "x0"},
                  after_observed={"x": "A", "y": "WRONG"},
                  desired={"x": "A", "y": "B"},
                  observation=_obs(), action_id=None, epoch=1)
    assert vr.passed   # x=A matches; y not referenced


def test_verify_node_referenced_variable_mismatch_fails():
    """If a referenced variable does NOT match its desired value, the VERIFY
    node fails."""
    v = VisibleVerifier()
    node = _verify_node(predicate="x equals A")
    vr = v.verify(node=node, before_observed={"x": "x0"},
                  after_observed={"x": "x0"},  # x NOT at desired
                  desired={"x": "A"}, observation=_obs(),
                  action_id=None, epoch=1)
    assert not vr.passed


# ── §6.5 VERIFY node — no referenced variables ⇒ all desired checked ───────
def test_verify_node_no_references_checks_all_desired():
    """When no known variable is referenced in the predicate, ALL desired
    variables are checked."""
    v = VisibleVerifier()
    node = _verify_node(predicate="all values are correct")
    vr = v.verify(node=node, before_observed={"x": "x0", "y": "y0"},
                  after_observed={"x": "A", "y": "B"},
                  desired={"x": "A", "y": "B"},
                  observation=_obs(), action_id=None, epoch=1)
    assert vr.passed

    # one mismatch
    vr2 = v.verify(node=node, before_observed={"x": "x0", "y": "y0"},
                   after_observed={"x": "A", "y": "WRONG"},
                   desired={"x": "A", "y": "B"},
                   observation=_obs(), action_id=None, epoch=1)
    assert not vr2.passed


# ── §6.6 before/after immutability ─────────────────────────────────────────
def test_before_after_dont_mutate_inputs():
    """The verifier must not mutate the input mappings (honesty contract —
    the caller's observed/desired dicts are read-only)."""
    v = VisibleVerifier()
    node = _action_node(desired={"x": "A"})
    before = {"x": "x0"}
    after = {"x": "A"}
    desired = {"x": "A"}
    v.verify(node=node, before_observed=before, after_observed=after,
             desired=desired, observation=_obs(), action_id="a1", epoch=1)
    assert before == {"x": "x0"}   # unchanged
    assert after == {"x": "A"}
    assert desired == {"x": "A"}


# ── §6.7 irreversible contract — verifier reports honestly ─────────────────
def test_irreversible_contract_still_verified_on_success():
    """An irreversible action (e.g. Send) is still subject to verification:
    if the desired state IS met in the fresh observation, it passes.
    Irreversibility is about *whether you can undo*, not *whether you did
    it* — the verifier reports the visible truth."""
    v = VisibleVerifier()
    node = _action_node(desired={"sent": "yes"}, reversibility="irreversible")
    vr = v.verify(node=node, before_observed={"sent": "no"},
                  after_observed={"sent": "yes"}, desired={"sent": "yes"},
                  observation=_obs(), action_id="a1", epoch=1)
    assert vr.passed   # the Send happened; the screen shows "yes"


def test_irreversible_contract_still_fails_on_mismatch():
    """Conversely, an irreversible action whose desired state is NOT met
    still fails — irreversibility does not grant a free pass."""
    v = VisibleVerifier()
    node = _action_node(desired={"sent": "yes"}, reversibility="irreversible")
    vr = v.verify(node=node, before_observed={"sent": "no"},
                  after_observed={"sent": "no"},  # NOT sent
                  desired={"sent": "yes"},
                  observation=_obs(), action_id="a1", epoch=1)
    assert not vr.passed


# ── multi-key desired_state ────────────────────────────────────────────────
def test_multi_key_partial_mismatch_fails():
    """When desired_state has multiple keys and one is mismatched, the
    verifier fails and names the offending key in the detail."""
    v = VisibleVerifier()
    node = _action_node(desired={"x": "A", "y": "B"})
    vr = v.verify(node=node, before_observed={"x": "x0", "y": "y0"},
                  after_observed={"x": "A", "y": "WRONG"},
                  desired={"x": "A", "y": "B"},
                  observation=_obs(), action_id="a1", epoch=1)
    assert not vr.passed
    assert "y" in vr.detail


def test_multi_key_all_match_passes():
    """All desired keys match in the fresh observation ⇒ pass."""
    v = VisibleVerifier()
    node = _action_node(desired={"x": "A", "y": "B"})
    vr = v.verify(node=node, before_observed={"x": "x0", "y": "y0"},
                  after_observed={"x": "A", "y": "B"},
                  desired={"x": "A", "y": "B"},
                  observation=_obs(), action_id="a1", epoch=1)
    assert vr.passed


# ── empty desired_state ────────────────────────────────────────────────────
def test_empty_desired_state_passes():
    """An ACTION node with empty desired_state (e.g. a pure navigation click)
    trivially passes — there is nothing to verify."""
    v = VisibleVerifier()
    node = _action_node(desired={})
    vr = v.verify(node=node, before_observed={"x": "x0"},
                  after_observed={"x": "x0"}, desired={},
                  observation=_obs(), action_id="a1", epoch=1)
    assert vr.passed


# ── VerificationResult is a frozen dataclass ───────────────────────────────
def test_verification_result_is_frozen():
    """The typed verdict is immutable — once produced, it cannot be tampered
    with (layered ownership §1: the kernel stores it, E owns the content)."""
    vr = VerificationResult(node_id="n1", epoch=1, passed=True,
                            action_id="a1")
    with pytest.raises(Exception):
        vr.passed = False  # type: ignore


# ── §6.8 completion_condition is actually checked (P0-2; RFC-003) ───────────
def test_completion_condition_satisfied_when_value_matches():
    """runtime.md §6: passed requires the completion_condition's visible
    criterion to be satisfied — here it IS (key==value matches the fresh
    observation), alongside the desired_state match."""
    v = VisibleVerifier()
    node = _action_node(desired={"x": "A"}, completion="x == A")
    vr = v.verify(node=node, before_observed={"x": "x0"},
                  after_observed={"x": "A"}, desired={"x": "A"},
                  observation=_obs(), action_id="a1", epoch=1)
    assert vr.passed
    assert vr.detail == "ok"


def test_completion_condition_not_satisfied_fails_even_if_desired_matches():
    """The load-bearing P0-2 negative control (runtime.md §6): desired_state
    is FULLY met, BUT the completion_condition's visible criterion is NOT
    satisfied → passed MUST be False. Pre-fix the verifier checked only
    desired_state and would have passed this (a doc-vs-code lie). CUA done ≠
    verified survives: an independent completion criterion can still fail."""
    v = VisibleVerifier()
    # desired x=A is met (after x=A); completion demands x==Z, which is NOT
    node = _action_node(desired={"x": "A"}, completion="x == Z")
    vr = v.verify(node=node, before_observed={"x": "x0"},
                  after_observed={"x": "A"}, desired={"x": "A"},
                  observation=_obs(), action_id="a1", epoch=1)
    assert not vr.passed
    assert "completion" in vr.detail.lower()
    assert "Z" in vr.detail


def test_completion_condition_referencing_unobserved_key_fails():
    """A completion_condition naming a key absent from the fresh observation
    cannot be grounded → fail (never silently satisfied)."""
    v = VisibleVerifier()
    node = _action_node(desired={"x": "A"}, completion="y == B")
    vr = v.verify(node=node, before_observed={"x": "x0"},
                  after_observed={"x": "A"}, desired={"x": "A"},
                  observation=_obs(), action_id="a1", epoch=1)
    assert not vr.passed
    assert "y" in vr.detail


def test_non_conforming_completion_condition_fails_honestly():
    """RFC-003: a non-empty completion_condition not in the minimal
    'key == value' form CANNOT be deterministically verified → the verifier
    fails honestly (passed=False) rather than silently passing. This is NOT
    an NLP parser — composition must produce the minimal form (or leave it
    empty). 'CUA done ≠ verified' includes 'cannot establish the criterion'."""
    v = VisibleVerifier()
    node = _action_node(desired={"x": "A"}, completion="inbox visibly shows sent")
    vr = v.verify(node=node, before_observed={"x": "x0"},
                  after_observed={"x": "A"}, desired={"x": "A"},
                  observation=_obs(), action_id="a1", epoch=1)
    assert not vr.passed
    assert "RFC-003" in vr.detail or "deterministic" in vr.detail


def test_empty_completion_condition_means_no_extra_criterion():
    """Empty completion_condition = no additional visible criterion beyond
    desired_state (RFC-003) — satisfied, NOT a silent skip. This is what the
    shared _action_node helper now defaults to."""
    v = VisibleVerifier()
    node = _action_node(desired={"x": "A"}, completion="")
    vr = v.verify(node=node, before_observed={"x": "x0"},
                  after_observed={"x": "A"}, desired={"x": "A"},
                  observation=_obs(), action_id="a1", epoch=1)
    assert vr.passed
    assert vr.detail == "ok"

