"""Active-surface per-gesture synchronization regression tests
(runtime.md §3/§8/§14 — the P0-1 contract defect: each atomic GUI action
must fold a fresh observation into the kernel's OBSERVED plane AND emit a
runtime event + visual artifact, and the action history's ``before`` must
come from a FRESH pre-action observation, not a stale kernel cache).

The pre-fix runtime did ``substrate.act → observe_active (fingerprint only)
→ next CUA prediction`` — no extract, no ``apply_observation``, no runtime
event until CUA DONE; and ``before = task_state().observed_values()`` read a
stale cache. These two tests pin the per-gesture fold + the fresh-before.
"""
from __future__ import annotations

from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode

from tests.runtime.conftest import (
    DONE, FakeSubstrate, ScriptedCUA, action_node, make_kernel,
    make_runtime, status_of, type_kv, var,
)
from taskvm.runtime import RuntimeEventKind


def _multi_graph():
    """One ACTION node whose desired needs TWO gestures (x=A then y=B)."""
    return WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        action_node("a1", desired={"x": "A", "y": "B"}, parent_id="root"),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("a1", "root")),
    ))


def _single_graph():
    return WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        action_node("a1", desired={"x": "A"}, parent_id="root"),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("a1", "root")),
    ))


def test_multi_gesture_folds_each_action_into_kernel_and_emits_event():
    """runtime.md §8: EACH atomic action folds a fresh observation into the
    kernel's OBSERVED plane and emits an ACTION_OBSERVED event — reality is
    folded back per gesture, NOT only at the final DONE.

    Two gestures (type x=A, then type y=B) then DONE. After gesture 1 and
    BEFORE gesture 2's prediction, the kernel's observed plane must already
    reflect x=A (the per-gesture fold); and exactly two ACTION_OBSERVED
    events (one per gesture, each with a screenshot artifact) must be
    published — a DONE-only fold would publish zero."""
    k = make_kernel([var("x", "x0", "A"), var("y", "y0", "B")], _multi_graph())
    sub = FakeSubstrate({"app": {"x": "x0", "y": "y0"}})
    seen_after_g1: dict = {}

    def gesture2(cua_self, obs):
        # BEFORE gesture 2 is predicted: the kernel's OBSERVED plane must
        # already reflect gesture 1 (x=A) — the per-gesture fold landed.
        seen_after_g1["observed"] = dict(k.task_state().observed_values())
        return type_kv("y", "B")

    cua = ScriptedCUA([type_kv("x", "A"), gesture2, DONE])
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert reason == "done"
    # gesture 1 folded mid-contract — NOT deferred to DONE
    assert seen_after_g1["observed"]["x"] == "A"
    assert k.task_state().observed_values() == {"x": "A", "y": "B"}
    # one ACTION_OBSERVED per gesture (DONE publishes ACTION_LANDED, not this)
    observed = [e for e in rt.runtime_events()
                if e.kind is RuntimeEventKind.ACTION_OBSERVED]
    assert len(observed) == 2
    assert all(e.artifact_ref for e in observed)   # screenshot artifact each
    assert all(e.surface_id == "app" for e in observed)


def test_fresh_before_observation_not_stale_kernel_cache():
    """runtime.md §3/§6: the action history's ``before`` comes from a FRESH
    pre-action observation, not the kernel's possibly-stale observed cache.
    The visible world was externally changed to B; the kernel cache is stale
    x0; the CUA starts a new contract — ``start_action`` must record
    ``before_observed=B`` (the fresh visible truth), so a later rollback's
    'before' is honest. (Pre-fix: ``before`` read the stale cache → x0.)"""
    k = make_kernel([var("x", "x0", "A")], _single_graph())   # cache x0, desired A
    sub = FakeSubstrate({"app": {"x": "B"}})                 # real world is B
    cua = ScriptedCUA([DONE])                                 # CUA: done over B
    rt = make_runtime(k, sub, cua)

    rt.run()

    # the action handle recorded the FRESH before (B), not the stale cache (x0)
    handle = next(iter(k._actions.values()))
    assert handle["before_observed"]["x"] == "B"
    # the kernel's observed plane now reflects the fresh world too
    assert k.task_state().observed_values()["x"] == "B"
    # CUA done ≠ verified: world is B, desired is A → honest verification fail
    assert status_of(k, "a1").value == "failed"
