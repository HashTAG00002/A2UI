"""Runtime contract tests — autonomy loop, verification gate, repair
budget (runtime.md §0, §5, §14).

The load-bearing assertions:
  * with NO user governance event the loop advances MULTIPLE nodes (not one);
  * ``CUA done`` is NOT verification — a done-signal over an unchanged
    screen fails the visible verifier and never commits;
  * verifier failure triggers at most ``max_repairs_per_contract``
    context-preserving repairs (requeue + discrepancy context, no
    back-to-home full rerun), then an honest escalate;
  * invalid predictions (provider failures) are model calls but never GUI
    actions and are bounded by their own small ceiling.
"""
from __future__ import annotations

from taskvm.domain.events import EventKind
from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode
from taskvm.runtime import CUADecision, CUADecisionKind
from taskvm.substrate import GuiAction

from tests.runtime.conftest import (
    CLICK, DONE, FakeExtractor, FakeSubstrate, ScriptedCUA, action_node,
    make_kernel, make_runtime, status_of, type_kv, var,
)


def _seq_graph():
    return WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        action_node("a1", desired={"x": "A"}, parent_id="root"),
        action_node("a2", desired={"y": "B"}, parent_id="root",
                    depends_on=("a1",)),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("a2", "root")),
    ))


def _single_graph():
    return WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        action_node("a1", desired={"x": "A"}, parent_id="root"),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("a1", "root")),
    ))


# ── autonomy: multiple nodes, no user event ────────────────────────────────
def test_autonomy_advances_multiple_nodes_without_user_events():
    k = make_kernel([var("x", "x0", "A"), var("y", "y0", "B")], _seq_graph())
    sub = FakeSubstrate({"app": {"x": "x0", "y": "y0"}})
    cua = ScriptedCUA([type_kv("x", "A"), DONE, type_kv("y", "B"), DONE])
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert reason == "done"
    assert status_of(k, "a1").value == "committed"
    assert status_of(k, "a2").value == "committed"
    assert status_of(k, "term").value == "committed"
    # both variables' OBSERVED plane caught up through fresh observations
    assert k.task_state().observed_values() == {"x": "A", "y": "B"}
    # real GUI gestures only — the fake world moved by type actions
    assert [a[1] for a in sub.act_log] == ["type", "type"]


# ── CUA done ≠ verified ────────────────────────────────────────────────────
def test_cua_done_without_world_change_is_not_verified():
    """CUA claims done while the screen still shows the old value: the
    visible verifier FAILS the node; nothing commits (runtime.md §6)."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})      # world never moves
    cua = ScriptedCUA([DONE])                       # ...but the CUA says done
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert status_of(k, "a1").value == "failed"     # honest verification fail
    assert k.task_state().observed_values()["x"] == "x0"
    assert sub.act_log == []                          # nothing landed
    # one repair attempt happened (budget 1) before the escalate
    assert len(cua.calls) == 2
    assert "repair" in cua.calls[1]["goal"]           # discrepancy carried


def test_verify_failure_repaired_from_current_world():
    """First attempt: CUA says done prematurely → verify fail. The runtime
    requeues and the repair pass, carrying the discrepancy context, actually
    moves the world → verified → commit."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = ScriptedCUA([DONE, type_kv("x", "A"), DONE])
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert reason == "done"
    assert status_of(k, "a1").value == "committed"
    assert sub.act_log == [("app", "type")]           # repair acted once
    kinds = [e.kind for e in k.events()]
    assert EventKind.VERIFICATION_FAILED in kinds     # the honest first fail
    assert EventKind.ACTION_REQUEUED in kinds         # context-preserving


def test_repair_budget_exhausted_escalates_and_pauses():
    """Verify fails twice with max_repairs=1: after the repair budget is
    spent the runtime escalates (pause + NODE_FAILED), never a blind rerun."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = ScriptedCUA([DONE, DONE, DONE])
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert status_of(k, "a1").value == "failed"
    # exactly first attempt + 1 repair — the third call never happened
    assert len(cua.calls) == 2
    assert any(e.kind.value == "node_failed"
               for e in rt.runtime_events())
    paused = [e for e in k.events()
              if e.kind is EventKind.GOVERNANCE_REQUESTED
              and e.payload.get("action") == "pause"]
    assert paused                                # safe-pause reached kernel


# ── invalid predictions: model calls, never GUI actions ────────────────────
def test_invalid_prediction_budget_bounds_provider_failures():
    from taskvm.runtime import RuntimeBudgets
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = ScriptedCUA([TimeoutError("provider timeout"),
                       ValueError("invalid JSON")])
    rt = make_runtime(k, sub, cua, budgets=RuntimeBudgets(
        max_invalid_predictions_per_contract=2))

    reason = rt.run()

    assert reason == "budget_exhausted"
    assert sub.act_log == []                        # a failed parse never acts
    bad = [r for r in rt._ledger.records
           if r.role == "cua" and not r.ok]
    assert len(bad) == 2                            # both counted honestly
    assert all(r.error for r in bad)


def test_invalid_prediction_then_success_continues():
    """A transient provider failure does not kill the contract — the loop
    continues (bounded) and the next valid prediction proceeds."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = ScriptedCUA([TimeoutError("transient"), type_kv("x", "A"), DONE])
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert reason == "done"
    assert status_of(k, "a1").value == "committed"


# ── action budget ──────────────────────────────────────────────────────────
def test_action_budget_exhaustion_is_a_safe_pause():
    from taskvm.runtime import RuntimeBudgets
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = ScriptedCUA()  # never DONE
    cua.script = [CLICK] * 100
    rt = make_runtime(k, sub, cua,
                      budgets=RuntimeBudgets(max_actions_per_contract=4))

    reason = rt.run()

    assert reason == "budget_exhausted"
    assert len(sub.act_log) == 4                    # ceiling honoured exactly
    assert any(e.kind.value == "budget_exhausted"
               for e in rt.runtime_events())


# ── CUA fail is an honest node failure ─────────────────────────────────────
def test_cua_fail_lands_honest_failure():
    from taskvm.runtime import CUADecisionKind, CUADecision
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = ScriptedCUA([CUADecision(kind=CUADecisionKind.FAIL,
                                   reason="cannot find the field")])
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert status_of(k, "a1").value == "failed"
    assert any(e.kind.value == "node_failed" for e in rt.runtime_events())


def test_mid_contract_cua_fail_lands_failed():
    """A CUA FAIL AFTER gestures already executed (handle STARTED) must land
    an honest FAILED verdict too. The kernel protocol is request → start →
    FINISH → verify: the verdict can only land on a FINISHED attempt, so
    ``_land_fail`` must finish the handle before landing — pre-fix it called
    ``land_verification`` on a STARTED handle, the kernel raised, the runtime
    swallowed it, and the node hung in RUNNING forever (a silently dropped
    failure is not an honest failure)."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = ScriptedCUA([type_kv("x", "A"),
                       CUADecision(kind=CUADecisionKind.FAIL,
                                   reason="lost the field")])
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert status_of(k, "a1").value == "failed"   # not stuck RUNNING
    assert any(e.kind.value == "node_failed" for e in rt.runtime_events())


def test_mid_contract_structure_invalidation_lands_failed():
    """Same honest-failure contract for a mid-contract structure
    invalidation (per-gesture fold raises ``StructureInvalidation`` after a
    gesture landed): the node must land FAILED, not hang in RUNNING."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})

    def gesture2(cua_self, obs):
        # the visible world externally lost the anchor right after gesture 1
        sub.world["app"]["STRUCTURE-GONE"] = "1"
        return CUADecision(kind=CUADecisionKind.ACT,
                           action=GuiAction(kind="click", coordinate=(1, 1)))

    cua = ScriptedCUA([type_kv("x", "A"), gesture2, DONE])
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert status_of(k, "a1").value == "failed"   # not stuck RUNNING
    assert any(e.kind.value == "node_failed" for e in rt.runtime_events())


# ── no surface: honest stop, no fallback path ──────────────────────────────
def test_no_surface_is_an_honest_stop():
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate()          # surfaces=[] → world empty dict
    sub.world = {}                 # no surfaces at all
    cua = ScriptedCUA([DONE])
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert cua.calls == []                          # no model call burned
    assert any("no surface" in e.detail
               for e in rt.runtime_events())


# ── model-call accounting: paper number == provider number ─────────────────
def test_ledger_counts_equal_real_predict_calls():
    k = make_kernel([var("x", "x0", "A"), var("y", "y0", "B")], _seq_graph())
    sub = FakeSubstrate({"app": {"x": "x0", "y": "y0"}})
    cua = ScriptedCUA([type_kv("x", "A"), DONE, type_kv("y", "B"), DONE])
    rt = make_runtime(k, sub, cua)

    rt.run()

    assert rt.model_calls == len(cua.calls) == rt._ledger.total()
    assert rt._ledger.counts_by_role() == {"cua": 4}
