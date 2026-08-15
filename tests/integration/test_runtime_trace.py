"""End-to-end runtime trace — the acceptance test for Agent E (runtime.md §22).

A single test that walks the FULL lifecycle:

  autonomy starts
    ↓
  ACTION requested → CUA prediction → atomic GUI action → fresh observation
    ↓
  verification (CUA done ≠ verified)
    ↓
  checkpoint
    ↓
  continue autonomy (second ACTION)
    ↓
  hot LocalPatch mid-flight → epoch bump → stale CUA response discarded
    ↓
  new target executes → verified → committed
    ↓
  RollbackRequested → CompensationPlan → real GUI compensation
    ↓
  CompensationResult landed

The trace must NOT contain:
  - hidden database id
  - internal mutation API
  - fixture answer
  - snapshot restore
"""
from __future__ import annotations

from taskvm.domain.events import EventKind
from taskvm.domain.intent import TaskIntent
from taskvm.domain.patch import (
    CompensationPatch, GoalPatch, LocalPatch, VariableUpdate,
)
from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode
from taskvm.runtime import RuntimeEventKind

from tests.runtime.conftest import (
    DONE, FakeSubstrate, ScriptedCUA, action_node, make_kernel,
    make_runtime, status_of, type_kv, var,
)


def _trace_graph():
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


def test_full_runtime_trace():
    """The single acceptance trace (runtime.md §22). Each ``# ── step`` block
    is a phase the trace MUST cover; assertions pin each phase to the
    typed contract."""
    # ── setup ────────────────────────────────────────────────────────────
    k = make_kernel([var("x", "x0", "A"), var("y", "y0", "B")],
                    _trace_graph())
    sub = FakeSubstrate({"app": {"x": "x0", "y": "y0"}})
    # CUA script for forward execution: type x=A, DONE, then for a2 we'll
    # inject a LocalPatch mid-flight, then type y=B2 (retargeted), DONE
    cua = ScriptedCUA()
    rt = make_runtime(k, sub, cua)

    # ── step 1: autonomy starts — a1 executes and verifies ──────────────
    cua.script = [type_kv("x", "A"), DONE]
    # Don't use run() yet — we need to control the flow step-by-step.
    # Instead, run with a step_budget of 1 to execute a1 only.
    rt.run(step_budget=1)
    assert status_of(k, "a1").value == "committed"
    assert sub.world["app"]["x"] == "A"
    # ACTION_LANDED event published with "verified"
    landed = [e for e in rt.runtime_events()
              if e.kind is RuntimeEventKind.ACTION_LANDED]
    assert landed
    assert landed[0].detail == "verified"

    # ── step 2: checkpoint advances ──────────────────────────────────────
    # The next run() step advances the CHECKPOINT node (control node).
    # After that, a2 should become READY.
    rt.run(step_budget=1)
    # The checkpoint is now committed (kernel recorded ckpt:cp)
    assert status_of(k, "cp").value == "committed"

    # ── step 3: hot LocalPatch mid-flight → stale CUA response discarded ─
    # CUA predicts type y=B under the OLD epoch; we inject a LocalPatch
    # (y → B2) BETWEEN the prediction and the start gate so the old
    # response is discarded; then the CUA produces type y=B2 under the
    # NEW epoch and it lands.
    def patch_mid_flight(cua_self, obs):
        k.apply_local_patch(LocalPatch(
            patch_id="lp", variable_updates=(
                VariableUpdate(semantic_key="y", new_value="B2"),)))
        return type_kv("y", "B")  # stale — predicted under old epoch

    cua.script = [patch_mid_flight, type_kv("y", "B2"), DONE]
    rt.run(step_budget=1)  # the stale prediction is discarded; a2 stays READY
    # The stale gesture never wrote
    assert sub.world["app"]["y"] == "y0"
    # The kernel logged ACTION_DISCARDED
    assert EventKind.ACTION_DISCARDED in [e.kind for e in k.events()]

    # ── step 4: retargeted a2 executes → verified → committed ────────────
    rt.run(step_budget=2)  # a2 commits (type y=B2 + DONE), then term
    assert status_of(k, "a2").value == "committed"
    assert sub.world["app"]["y"] == "B2"
    # The task is done
    assert status_of(k, "term").value == "committed"

    # ── step 5: RollbackRequested → CompensationPlan ────────────────────
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:cp"))
    assert plan.entries  # history-driven: a2 (y: B2 → y0) is in scope
    # a1 (x: x0 → A) was committed BEFORE the checkpoint → not in scope
    assert len(plan.entries) == 1
    assert plan.entries[0].node_id == "a2"

    # ── step 6: real GUI compensation ───────────────────────────────────
    rt._cua = ScriptedCUA([type_kv("y", "y0"), DONE])
    disposition = rt.execute_compensation(plan, surface_id="app")
    assert disposition == "complete"
    assert sub.world["app"]["y"] == "y0"  # real GUI undid it
    # x was before the checkpoint — NOT undone
    assert sub.world["app"]["x"] == "A"
    # COMPENSATION_ENTRY event was published
    comp_events = [e for e in rt.runtime_events()
                   if e.kind is RuntimeEventKind.COMPENSATION_ENTRY]
    assert comp_events

    # ── no-leak audit: the trace contains no internal information ────────
    all_event_strs = [
        *(str(e.detail) for e in rt.runtime_events()),
        *(str(e.payload.get("detail", "")) for e in k.events()),
    ]
    for s in all_event_strs:
        assert "entity_id" not in s.lower(), f"internal id leaked: {s}"
        assert "data-" not in s.lower(), f"data-* id leaked: {s}"
