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


# ── EE.4: substrate_invariance killtest ─────────────────────────────────────
def test_substrate_invariance_importable():
    """run_substrate_invariance_killtest importable + DEFAULT_PAIR is the
    calendar vs outlook_cal reskin pair (the JVM-moment task pair)."""
    from taskvm.evaluation.run_substrate_invariance_killtest import (
        run_stack, genui_semantic_sim, main, DEFAULT_PAIR,
        BINDING_F1_MAX_DIFF, ROUND_TRIP_MAX_DIFF)
    assert DEFAULT_PAIR == ("release_reschedule", "outlook_release_reschedule")
    assert BINDING_F1_MAX_DIFF == 0.2
    assert ROUND_TRIP_MAX_DIFF == 0.15
    for fn in (run_stack, genui_semantic_sim, main):
        assert callable(fn)


# ── EE.5: reconciliation killtest ───────────────────────────────────────────
def test_reconciliation_killtest_importable():
    """run_reconciliation_killtest importable + 3 scenarios cover the merge
    options (accept_underlying / keep_projected / merge)."""
    from taskvm.evaluation.run_reconciliation_killtest import (
        run_scenario, main, SCENARIOS)
    assert len(SCENARIOS) == 3
    opts = {s["option"] for s in SCENARIOS}
    assert opts == {"accept_underlying", "keep_projected", "merge"}, (
        f"3 merge strategies must be covered; got {opts}")
    assert callable(run_scenario) and callable(main)


# ── EE.6: GroundingBackend ABC + hot-swap ───────────────────────────────────
def test_grounding_backend_factory_and_names():
    """make_grounding_backend builds the 3 named backends; GPT56Sol/GLM5V carry
    the right default model ids."""
    from taskvm.execution.grounding_backend import (make_grounding_backend,
        GPT56SolBackend, GLM5VBackend, UITarsBackend, GroundingBackend)
    b = make_grounding_backend("gpt56sol")
    assert isinstance(b, GPT56SolBackend) and isinstance(b, GroundingBackend)
    assert b.name == "gpt56sol"
    g = make_grounding_backend("glm5v")
    assert isinstance(g, GLM5VBackend) and g.name == "glm5v"
    assert g.model == "glm-5v-turbo"   # 大纲附录 B.2 vision-capable backup
    u = make_grounding_backend("uitars")
    assert isinstance(u, UITarsBackend) and u.name == "uitars"


def test_uitars_backend_is_stub():
    """UITarsBackend must raise NotImplementedError (interface-only, no weights
    downloaded — handoff EE.6: '不用真实跑 UITarsBackend ... stub 实现')."""
    from taskvm.execution.grounding_backend import make_grounding_backend
    u = make_grounding_backend("uitars")
    try:
        u.predict_action("data:url", "do thing", [])
        assert False, "UITarsBackend stub must raise NotImplementedError"
    except NotImplementedError:
        pass  # correct — interface-only


def test_get_executor_caches_by_backend_name():
    """get_executor returns the same singleton for the same backend_name (EE.6
    hot-swap: different backend_names get different executors)."""
    from taskvm.execution.gui_executor import get_executor
    e1 = get_executor(backend_name="gpt56sol")
    e2 = get_executor(backend_name="gpt56sol")
    assert e1 is e2, "same backend_name must return the cached singleton"
    assert e1.backend.name == "gpt56sol"


# ── EE.7: GenUI form-wired controls ─────────────────────────────────────────
def test_genui_form_wired_controls():
    """render_a2ui_to_html with sid form-wires rw-zone editable controls +
    undo/checkpoint buttons (EE.7: model-decoded component = live governance)."""
    from taskvm.workspace_ui.genui_decoder import render_a2ui_to_html
    messages = [
        {"createSurface": {"surface": {"id": "s1", "version": "v0.9"}}},
        {"updateComponents": {"components": [
            {"id": "root", "component": "Column", "children": ["rw_zone"]},
            {"id": "rw_zone", "component": "Column", "children": ["f1", "b1", "b2"]},
            {"id": "f1", "component": "TextField", "label": "发布日期",
             "dataBinding": "release_date", "editable": True},
            {"id": "b1", "component": "Button", "label": "撤销"},
            {"id": "b2", "component": "Button", "label": "设检查点"},
        ]}},
        {"updateDataModel": {"value": {"release_date": "2026-08-14"}}},
    ]
    sid = "t_ee7"
    html = render_a2ui_to_html(messages, sid=sid)
    assert f'action="/{sid}/edit"' in html, "TextField must form-post to /<sid>/edit"
    assert 'name="var_id" value="release_date"' in html, "hidden var_id must carry the binding"
    assert f'action="/{sid}/undo"' in html, "undo Button must form-post to /<sid>/undo"
    assert f'action="/{sid}/checkpoint"' in html, "checkpoint Button must form-post to /<sid>/checkpoint"
    # backward compat: no sid → bare inputs (no forms)
    html_nosid = render_a2ui_to_html(messages, sid="")
    assert f'action="/{sid}/edit"' not in html_nosid
    assert 'data-var="release_date"' in html_nosid, "no-sid still renders the bare input"


# ── EE.9: interaction compression killtest ──────────────────────────────────
def test_interaction_compression_baseline_model():
    """_baseline_actions: launch_full (4 apps, 5 bindings) → 4 navigates + 15
    per-binding = 19 baseline actions; TaskVM = 1 → compression 19x (≥4x)."""
    from taskvm.evaluation.run_interaction_compression import _baseline_actions
    from taskvm.benchmark.fixtures import get_task
    b = _baseline_actions(get_task("launch_full"))
    assert b["n_apps"] == 4 and b["n_bindings"] == 5
    assert b["total"] == 4 + 5 * 3, f"baseline = n_apps + 3*n_bindings; got {b['total']}"
    # compression = baseline / 1 (taskvm) = baseline total
    assert b["total"] >= 4, "4-App task must compress ≥4x"


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-q"])
