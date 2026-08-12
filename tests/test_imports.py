"""L0 CI test — import & interface integrity (E17 §5.1).

Verifies all old import paths (incl. E17-C re-export shims) still work + the
new E17-B governance interfaces have correct signatures. No external services
required (<30s). Run: ``python -m pytest tests/test_imports.py -x -q``.

This is the L0 layer of the CI matrix (handoff §5.1): it catches any import
breakage from the E17-C refactor + any governance interface drift.
"""
import pytest


# ── E17-C: old import paths must still work (re-export shims) ──────────────
def test_harness_state_adapter_reexport():
    """Old path (13 callers) — StateAdapter + make_adapters via the shim."""
    from taskvm.harness.state_adapter import StateAdapter, make_adapters
    assert StateAdapter is not None
    assert callable(make_adapters)


def test_substrate_base_new_path():
    """New path — the real home for StateAdapter."""
    from taskvm.substrate.base import StateAdapter, make_adapters, make_adapter
    assert StateAdapter is not None
    assert callable(make_adapters)
    assert callable(make_adapter)


def test_substrate_base_adapter_classes():
    """All 7 adapter subclasses moved to substrate.base."""
    from taskvm.substrate.base import (
        CalendarAdapter, TaskBoardAdapter, DriveAdapter,
        MailAdapter, OutlookCalAdapter, WechatAdapter, AlipayAdapter,
        _ADAPTER_CLASSES, DEFAULT_PORTS,
    )
    assert set(_ADAPTER_CLASSES) == {
        "calendar", "taskboard", "drive", "mail",
        "outlook_cal", "wechat", "alipay"}
    assert DEFAULT_PORTS["wechat"] == 3019


def test_harness_mobilegym_bridge_reexport():
    """Old path — MobileGymBridge + main via the shim."""
    from taskvm.harness.mobilegym_bridge import MobileGymBridge, main, build_app
    assert MobileGymBridge is not None
    assert callable(main)
    assert callable(build_app)


def test_substrate_mobilegym_bridge_new_path():
    """New path — the real home for MobileGymBridge."""
    from taskvm.substrate.mobilegym.bridge import MobileGymBridge, build_app, main
    assert MobileGymBridge is not None
    assert callable(build_app)
    assert callable(main)


def test_vm_state_reexport():
    """New vm_state path aliases the old task_state path."""
    from taskvm.vm_state import TaskBinding, EntityBinding, compile_binding
    from taskvm.task_state.entity_binding import TaskBinding as TB_old
    assert TaskBinding is TB_old  # same object (re-export)


def test_task_state_old_path_unbroken():
    """Old task_state path is the real home (not moved)."""
    from taskvm.task_state.entity_binding import TaskBinding, OPERATOR_REGISTRY
    from taskvm.task_state.compiler import compile_binding
    from taskvm.task_state.representation import TaskStateGraph
    assert TaskBinding is not None
    assert "move_event" in OPERATOR_REGISTRY


def test_verifier_old_path_unbroken():
    """Old verifier path is the real home (not moved)."""
    from taskvm.verifier.round_trip_checks import check_round_trip
    from taskvm.verifier.rollback_verify import check_rollback_fidelity
    from taskvm.verifier.canonical_state import snapshot, field_matches
    assert callable(check_round_trip)
    assert callable(check_rollback_fidelity)
    assert callable(snapshot)


# ── E17-A: Checkpoint + checkpoints field ──────────────────────────────────
def test_checkpoint_dataclass():
    from taskvm.benchmark.fixtures import Checkpoint, CanonicalTaskGraph
    import dataclasses
    fields = {f.name for f in dataclasses.fields(Checkpoint)}
    assert fields == {"id", "description", "criterion"}
    cp = Checkpoint("C1", description="x", criterion={"a": 1})
    assert cp.id == "C1" and cp.description == "x" and cp.criterion == {"a": 1}


def test_canonical_task_graph_checkpoints_default_empty():
    from taskvm.benchmark.fixtures import RELEASE_RESCHEDULE
    assert RELEASE_RESCHEDULE.checkpoints == []  # default — zero break


def test_mobilegym_tasks_registered():
    from taskvm.benchmark.mobilegym_fixtures import all_mobilegym_tasks
    ids = set(all_mobilegym_tasks().keys())
    assert {"top3_expense_to_wechat", "social_morning_brief",
            "expense_and_notify"} <= ids


# ── E17-B: governance interfaces ───────────────────────────────────────────
def test_governance_public_surface():
    from taskvm.governance import (
        UserBehaviorDriver, UserBehaviorEvent, EVENT_TYPES, VMStateSnapshot,
        SubgoalInstruction, CheckpointGraph, CheckpointDirection,
        ScriptedUserDriver, make_scripted_driver, GovernanceInterpreter,
    )
    assert "edit_field" in EVENT_TYPES
    assert "rollback_to" in EVENT_TYPES


def test_user_behavior_event_validates_type():
    from taskvm.governance import UserBehaviorEvent
    with pytest.raises(ValueError):
        UserBehaviorEvent("bogus_type", {})
    ev = UserBehaviorEvent("edit_field", {"var_id": "x", "new_value": "y"})
    assert ev.event_type == "edit_field"


def test_subgoal_instruction_fields():
    from taskvm.governance import SubgoalInstruction
    import dataclasses
    fields = {f.name for f in dataclasses.fields(SubgoalInstruction)}
    assert {"natural_language", "patch_ops", "verification_criterion",
            "source_event_type", "llm_generated",
            "manual_review_needed"} <= fields


def test_governance_interpreter_signature():
    import inspect
    from taskvm.governance import GovernanceInterpreter
    sig = inspect.signature(GovernanceInterpreter.interpret)
    params = set(sig.parameters)
    assert {"event", "vm_state"} <= params


def test_bridge_mutate_x_signature():
    """E17-A Option B + E17-B instruction_override params on mutate_x."""
    import inspect
    from taskvm.substrate.mobilegym.bridge import MobileGymBridge
    sig = inspect.signature(MobileGymBridge.mutate_x)
    params = sig.parameters
    assert "verify_mode" in params
    assert params["verify_mode"].default == "specific"
    assert "instruction_override" in params
    assert params["instruction_override"].default is None


# ── E17-B: scripted driver dry-run (mock pipeline, no model) ───────────────
def test_scripted_driver_dry_run_release_reschedule():
    """L1 mock pipeline: ScriptedUserDriver + GovernanceInterpreter produce
    subgoals for a builtin task (no model, no MobileGym)."""
    from taskvm.governance import ScriptedUserDriver, GovernanceInterpreter, VMStateSnapshot
    from taskvm.governance.scripted_driver import make_scripted_driver
    from taskvm.execution.rollback import RollbackLog
    driver = make_scripted_driver("release_reschedule")
    # minimal binding from the fixture (no compiler call)
    from taskvm.governance.scripted_driver import _build_minimal_binding
    binding = _build_minimal_binding(driver.task)
    vm_state = VMStateSnapshot(
        sid="t", binding=binding, adapters={}, rollback_log=RollbackLog(),
        checkpoints=driver.task.checkpoints)
    interp = GovernanceInterpreter(enable_llm_rollback_nl=False)
    n_subgoals = 0
    while True:
        ev = driver.next_event()
        if ev is None:
            break
        sgs = interp.interpret(ev, vm_state, task=driver.task)
        n_subgoals += len(sgs)
    assert n_subgoals >= 3  # release_reschedule: 1 edit → ≥3 patch-op subgoals


# ── E18: run_mg_vm_killtest new killtest ───────────────────────────────────
def test_mg_vm_killtest_importable():
    """run_mg_vm_killtest must be importable (E18 sanity)."""
    from taskvm.evaluation.run_mg_vm_killtest import run_one_sample, main
    assert callable(run_one_sample)
    assert callable(main)


def test_mg_vm_killtest_dry_run_mg1():
    """MG-1 dry-run: social_morning_brief produces ≥3 subgoals incl. toggle_like
    + send_message (no bridge needed)."""
    from taskvm.evaluation.run_mg_vm_killtest import run_one_sample
    result = run_one_sample("social_morning_brief", "localhost", sample_i=0,
                            dry_run=True)
    assert result["PASS"], f"MG-1 dry-run failed: {result.get('error')}"
    ops = [op["operator"]
           for s in result["subgoals"]
           for op in s.get("patch_ops", [])]
    assert "toggle_like" in ops
    assert "send_message" in ops


def test_mg_vm_killtest_dry_run_mg2():
    """MG-2 dry-run: expense_and_notify produces rollback_to subgoal
    (no bridge needed)."""
    from taskvm.evaluation.run_mg_vm_killtest import run_one_sample
    result = run_one_sample("expense_and_notify", "localhost", sample_i=0,
                            dry_run=True)
    assert result["PASS"], f"MG-2 dry-run failed: {result.get('error')}"
    event_types = [s["source_event_type"] for s in result["subgoals"]]
    assert "rollback_to" in event_types, (
        f"MG-2 must have a rollback_to subgoal; got: {event_types}")
    assert "checkpoint" in event_types


# ── EE.3: four_step_arc demo script ─────────────────────────────────────────
def test_four_step_arc_importable():
    """run_four_step_arc must be importable + its 4 step fns + main callable."""
    from taskvm.evaluation.run_four_step_arc import (
        step1_write, step2_reconciliation, step3_rollback, step4_jvm_moment, main,
        _gt_binding, _apps_for)
    for fn in (step1_write, step2_reconciliation, step3_rollback,
               step4_jvm_moment, main, _gt_binding, _apps_for):
        assert callable(fn)


def test_four_step_arc_apps_for_launch_full():
    """_apps_for(launch_full) returns the 4-App fanout set (calendar+taskboard+
    drive+mail) — the EE.2 task the arc's Step 1 uses."""
    from taskvm.evaluation.run_four_step_arc import _apps_for
    from taskvm.benchmark.fixtures import get_task
    apps = _apps_for(get_task("launch_full"))
    assert apps == ["calendar", "taskboard", "drive", "mail"], (
        f"launch_full should need 4 apps in order; got {apps}")


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-q"])
