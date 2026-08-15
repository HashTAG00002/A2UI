"""Runtime contract tests — real-GUI rollback execution (runtime.md §7).

Rollback is NOT a snapshot restore: the kernel built the
``CompensationPlan`` from its OWN committed action history; the runtime
lands each reversible entry through the SAME real execution path as
forward work (CUA → substrate.act → fresh observation → judged).
``CompensationEntryResult.compensated=True`` only when a fresh observation
confirms the target. IRREVERSIBLE work is honestly reported as NOT
compensated, never disguised as a revertible value change. A stale plan
(epoch bumped by governance mid-compensation) lands as ``"discarded"``.
"""
from __future__ import annotations

from taskvm.domain.events import EventKind
from taskvm.domain.patch import CompensationPatch
from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode

from tests.runtime.conftest import (
    DONE, FakeSubstrate, ScriptedCUA, action_node, make_kernel,
    make_runtime, status_of, type_kv, var,
)
from taskvm.runtime import RuntimeEventKind


def _rollback_graph():
    """a1 → cp(checkpoint) → a2 → term."""
    return WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        action_node("a1", desired={"x": "A"}, parent_id="root"),
        WorkflowNode("cp", NodeKind.CHECKPOINT, "cp",
                     parent_id="root", depends_on=("a1",)),
        action_node("a2", desired={"y": "B"}, parent_id="root",
                    depends_on=("cp",)),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("a2", "root")),
    ))


# ── forward → checkpoint → rollback → real-GUI compensation ────────────────
def test_rollback_runs_real_gui_compensation_actions():
    """The compensation executor lands each reversible entry through real
    substrate.act gestures and a fresh observation confirms the target
    (runtime.md §7). No DB snapshot, no hidden write.

    The kernel's rollback scope starts at the checkpoint boundary: only
    actions committed *after* the checkpoint are in the plan.  Here that
    is just ``a2`` (y:B→y0).  ``a1`` (x:x0→A) was committed *before* the
    checkpoint, so it is outside the rollback scope and the kernel does
    not include it in ``plan.entries``.
    """
    k = make_kernel([var("x", "x0", "A"), var("y", "y0", "B")],
                    _rollback_graph())
    sub = FakeSubstrate({"app": {"x": "x0", "y": "y0"}})
    cua = ScriptedCUA([type_kv("x", "A"), DONE, type_kv("y", "B"), DONE])
    rt = make_runtime(k, sub, cua)
    rt.run(step_budget=3)               # commit a1 + checkpoint(cp) + a2
    assert status_of(k, "a1").value == "committed"
    assert status_of(k, "a2").value == "committed"

    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:cp"))
    assert plan.entries                      # history-driven, has work
    # Only a2 is in the rollback scope (a1 was committed before the checkpoint)
    assert len(plan.entries) == 1
    assert plan.entries[0].node_id == "a2"

    # the compensation CUA: undo a2 (y:B→y0)
    cua_comp = ScriptedCUA([
        type_kv("y", "y0"), DONE,
    ])
    rt._cua = cua_comp
    disposition = rt.execute_compensation(plan, surface_id="app")

    # a2 compensated through real GUI → complete
    assert disposition == "complete"
    assert k.task_state().observed_values()["y"] == "y0"
    # x was committed before the checkpoint — not in rollback scope
    assert k.task_state().observed_values()["x"] == "A"
    # the rollback gesture is a real substrate.act call (type)
    comp_acts = [a[1] for a in sub.act_log if a[0] == "app"]
    assert "type" in comp_acts                # real GUI, not a DB restore
    # COMPENSATION_ENTRY events were published
    assert any(e.kind is RuntimeEventKind.COMPENSATION_ENTRY
               for e in rt.runtime_events())


# ── IRREVERSIBLE: honestly not compensated, never faked ─────────────────────
def test_irreversible_entry_is_not_reported_as_compensated():
    """An IRREVERSIBLE committed action (a Send) committed AFTER the
    rollback target checkpoint goes to ``plan.uncompensatable``; the
    runtime's per-entry result for it is absent (it never attempts) and
    the plan disposition is partial/failed, never ``complete``
    (runtime.md §7)."""
    from taskvm.domain.contract import Reversibility
    k = make_kernel([var("x", "x0", "A"), var("sent", "no", "yes")],
                    WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        action_node("a1", desired={"x": "A"}, parent_id="root"),
        WorkflowNode("cp", NodeKind.CHECKPOINT, "cp",
                     parent_id="root", depends_on=("a1",)),
        action_node("a2", desired={"sent": "yes"}, parent_id="root",
                    depends_on=("cp",), reversibility="irreversible"),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("a2", "root")),
    )))
    sub = FakeSubstrate({"app": {"x": "x0", "sent": "no"}})
    cua = ScriptedCUA([type_kv("x", "A"), DONE, type_kv("sent", "yes"), DONE])
    rt = make_runtime(k, sub, cua)
    rt.run(step_budget=3)   # commit a1 → cp(checkpoint) → a2(irreversible)
    assert status_of(k, "a1").value == "committed"
    assert status_of(k, "a2").value == "committed"

    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:cp"))
    # a2's IRREVERSIBLE send is uncompensatable; a1 (before cp) is NOT in
    # the rollback scope (it was committed before the checkpoint boundary)
    assert plan.uncompensatable
    assert not plan.entries              # nothing revertible for the runtime

    disposition = rt.execute_compensation(plan, surface_id="app")
    assert disposition != "complete"     # honest — NOT faked
    assert sub.world["app"]["sent"] == "yes"   # the Send was NOT undone


# ── stale plan: governance bumped the epoch → discarded ─────────────────────
def test_stale_compensation_plan_is_discarded():
    """A GoalPatch mid-compensation bumps the epoch; the in-flight plan lands
    as ``"discarded"`` — never a fake success (runtime.md §7)."""
    k = make_kernel([var("x", "x0", "A"), var("y", "y0", "B")],
                    _rollback_graph())
    sub = FakeSubstrate({"app": {"x": "x0", "y": "y0"}})
    cua = ScriptedCUA([type_kv("x", "A"), DONE, type_kv("y", "B"), DONE])
    rt = make_runtime(k, sub, cua)
    rt.run(step_budget=3)               # commits a1, cp(checkpoint), a2
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:cp"))

    # bump the epoch via governance BEFORE landing the result
    from taskvm.domain.intent import TaskIntent
    from taskvm.domain.patch import GoalPatch
    k.apply_goal_patch(GoalPatch(
        patch_id="gp", new_intent=TaskIntent(goal="abandon the rollback")))

    # the compensation executor sees the epoch mismatch and lands discarded
    rt._cua = ScriptedCUA([type_kv("y", "y0")])
    disposition = rt.execute_compensation(plan, surface_id="app")
    assert disposition == "discarded"
    kinds = [e.kind for e in k.events()]
    assert EventKind.COMPENSATION_DISCARDED in kinds


# ── compensation CUA done ≠ compensated ────────────────────────────────────
def test_compensation_done_signal_without_target_is_not_compensated():
    """A CUA ``DONE`` while the visible value has NOT returned to
    ``to_observed`` is NOT compensated — the fresh observation judges it
    (runtime.md §7, the load-bearing CUA-done-≠-verified discipline)."""
    k = make_kernel([var("x", "x0", "A"), var("y", "y0", "B")],
                    _rollback_graph())
    sub = FakeSubstrate({"app": {"x": "x0", "y": "y0"}})
    cua = ScriptedCUA([type_kv("x", "A"), DONE, type_kv("y", "B"), DONE])
    rt = make_runtime(k, sub, cua)
    rt.run(step_budget=3)               # commits a1, cp(checkpoint), a2
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:cp"))

    # the CUA immediately says DONE without actually undoing anything
    rt._cua = ScriptedCUA([DONE, DONE])
    disposition = rt.execute_compensation(plan, surface_id="app")
    # nothing was actually undone → not complete
    assert disposition != "complete"
    assert k.task_state().observed_values()["x"] == "A"
    assert k.task_state().observed_values()["y"] == "B"
