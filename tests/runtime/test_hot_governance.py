"""Runtime contract tests — hot governance (runtime.md §4, §14).

The minimal interruption boundary is ONE atomic GUI action:
  * soft pause blocks the NEXT action after the current one completes;
  * a CUA response that returns AFTER a GoalPatch/Pause bumped the kernel
    epoch is discarded — ``substrate.act`` is NEVER called for it;
  * a governance event mid-contract (between two atomic actions) discards
    the remainder of that contract;
  * irreversible contracts re-check the epoch right before acting;
  * a pending compensation plan of the current epoch blocks forward
    autonomy — the runtime returns ``blocked`` instead of spinning;
  * an EXTERNAL kernel pause (composition called the kernel directly) is
    honoured by the runtime loop on the next tick.
"""
from __future__ import annotations

import pytest

from taskvm.domain.events import EventKind
from taskvm.domain.intent import TaskIntent
from taskvm.domain.patch import CompensationPatch, GoalPatch, LocalPatch, \
    VariableUpdate
from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode

from tests.runtime.conftest import (
    CLICK, DONE, FakeSubstrate, ScriptedCUA, action_node, make_kernel,
    make_runtime, status_of, type_kv, var,
)


def _single_graph(desired_x="A"):
    return WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        action_node("a1", desired={"x": desired_x}, parent_id="root"),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("a1", "root")),
    ))


# ── soft pause: the next atomic action is blocked ──────────────────────────
def test_soft_pause_blocks_the_next_atomic_action():
    """Gesture 1 is already in the actuator when a soft pause lands. The
    in-flight gesture completes (no hard kill); the NEXT gesture — predicted
    after the pause — never reaches ``substrate.act`` (runtime.md §4)."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = ScriptedCUA()

    def second_gesture(cua_self, obs):
        rt.request_pause()          # governance lands BETWEEN the gestures
        return type_kv("x", "IGNORED")   # predicted under the old epoch

    cua.script = [type_kv("x", "A"), second_gesture, DONE]
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert reason == "paused"
    assert sub.act_log == [("app", "type")]   # only the in-flight gesture
    assert sub.world["app"]["x"] == "A"       # the paused one never wrote


# ── stale CUA response: the epoch race ─────────────────────────────────────
def test_stale_cua_response_after_goal_patch_never_acts():
    """request at epoch N → CUA request in flight → GoalPatch bumps the
    kernel epoch → the old CUA response returns → it must be DISCARDED
    before substrate.act (runtime.md §4, the load-bearing race)."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = ScriptedCUA([type_kv("x", "A"), DONE])   # the stale response

    def goal_patch_mid_flight(cua_self):
        k.apply_goal_patch(GoalPatch(
            patch_id="gp", new_intent=TaskIntent(goal="a different goal")))
        cua.on_predict = None

    cua.on_predict = goal_patch_mid_flight
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert sub.act_log == []            # the stale response NEVER acted
    kinds = [e.kind for e in k.events()]
    assert EventKind.ACTION_DISCARDED in kinds
    # GoalPatch two-phase: execution blocked until recompose
    assert reason == "pending_recompose"
    assert status_of(k, "a1").value == "invalidated"


def test_stale_cua_response_after_external_pause_never_acts():
    """Same race, but the governance gesture is an external kernel pause
    (composition called request_governance directly)."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = ScriptedCUA([type_kv("x", "A")])

    def pause_mid_flight(cua_self):
        k.request_governance("pause", "user hit the stop button")
        cua.on_predict = None

    cua.on_predict = pause_mid_flight
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert sub.act_log == []
    assert reason == "paused"           # honoured on the next tick


# ── mid-contract governance: the remainder is discarded ────────────────────
def test_goal_patch_between_two_gestures_discards_remainder():
    """Gesture 1 lands; governance bumps the epoch before gesture 2 — the
    second gesture must NOT execute (every atomic action re-enters the
    kernel gate)."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = ScriptedCUA()

    def second_gesture(cua_self, obs):
        k.apply_goal_patch(GoalPatch(
            patch_id="gp", new_intent=TaskIntent(goal="changed mid-flight")))
        return type_kv("x", "WRONG")   # would corrupt if executed

    cua.script = [type_kv("x", "A"), second_gesture, DONE]
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert sub.act_log == [("app", "type")]      # only gesture 1 (x=A)
    assert sub.world["app"]["x"] == "A"          # the stale one never wrote
    assert reason == "pending_recompose"


# ── irreversible: re-check right before acting ─────────────────────────────
def test_irreversible_contract_rechecks_epoch_before_acting():
    """A requires_confirmation contract whose prediction came back AFTER a
    governance epoch bump must not land the irreversible gesture."""
    k = make_kernel([var("sent", "no", "yes")], WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        action_node("a1", desired={"sent": "yes"}, parent_id="root",
                    reversibility="irreversible"),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("a1", "root")),
    )))
    sub = FakeSubstrate({"app": {"sent": "no"}})
    cua = ScriptedCUA([type_kv("sent", "yes"), DONE])

    def pause_mid_flight(cua_self):
        k.request_governance("pause", "user hesitated on Send")
        cua.on_predict = None

    cua.on_predict = pause_mid_flight
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert sub.act_log == []            # the irreversible gesture NEVER landed
    assert sub.world["app"]["sent"] == "no"


def test_irreversible_contract_executes_when_epoch_stable():
    """The same irreversible contract with NO governance race lands normally
    (the gate must not veto healthy work)."""
    k = make_kernel([var("sent", "no", "yes")], WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        action_node("a1", desired={"sent": "yes"}, parent_id="root",
                    reversibility="irreversible"),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("a1", "root")),
    )))
    sub = FakeSubstrate({"app": {"sent": "no"}})
    cua = ScriptedCUA([type_kv("sent", "yes"), DONE])
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    assert reason == "done"
    assert sub.world["app"]["sent"] == "yes"


# ── LocalPatch: epoch bump, work continues with the retargeted contract ────
def test_local_patch_retargets_and_execution_continues():
    """A LocalPatch mid-flight bumps the epoch; the runtime re-pulls the
    node (still READY) and executes the RETARGETED contract (runtime.md §4:
    'epoch 更新后继续受影响节点')."""
    k = make_kernel([var("x", "x0", "A")], _single_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    cua = ScriptedCUA()

    def patched_gesture(cua_self, obs):
        k.apply_local_patch(LocalPatch(patch_id="lp", variable_updates=(
            VariableUpdate(semantic_key="x", new_value="A2"),)))
        return type_kv("x", "A")     # predicted under the OLD epoch — the
                                         # kernel start-gate must discard it

    cua.script = [patched_gesture, type_kv("x", "A2"), DONE]
    rt = make_runtime(k, sub, cua)

    reason = rt.run()

    # the world ends at the RETARGETED value (the second pass verified A2)
    assert k.task_state().observed_values()["x"] == "A2"
    assert status_of(k, "a1").value == "committed"
    # the stale first gesture (typed under the pre-patch epoch) never wrote
    assert sub.world["app"]["x"] == "A2"
    assert EventKind.ACTION_DISCARDED in [e.kind for e in k.events()]


# ── pending compensation blocks forward autonomy (no hot retry loop) ───────
def test_pending_compensation_blocks_forward_without_spinning():
    """After request_compensation the kernel rejects forward autonomy; the
    runtime must return ``blocked`` promptly (this test would hang forever
    on the pre-fix hot retry loop)."""
    k = make_kernel([var("x", "x0", "A"), var("y", "y0", "B")],
                    WorkflowGraph(nodes=(
                        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
                        action_node("a1", desired={"x": "A"}, parent_id="root"),
                        WorkflowNode("cp", NodeKind.CHECKPOINT, "cp",
                                     parent_id="root", depends_on=("a1",)),
                        action_node("a2", desired={"y": "B"}, parent_id="root",
                                    depends_on=("cp",)),
                        WorkflowNode("term", NodeKind.TERMINAL, "done",
                                     depends_on=("a2", "root")),
                    )))
    sub = FakeSubstrate({"app": {"x": "x0", "y": "y0"}})
    cua = ScriptedCUA([type_kv("x", "A"), DONE, type_kv("y", "B"), DONE])
    rt = make_runtime(k, sub, cua)
    rt.run(step_budget=3)                    # commit a1 + checkpoint

    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:cp"))
    assert plan.entries                      # history-driven, has work

    reason = rt.run()                        # forward must stop, not spin

    assert reason == "blocked"
    assert cua.calls[-1]["goal"]             # no infinite call storm:
    assert len(cua.calls) <= 5               # ~the pre-block attempts only
