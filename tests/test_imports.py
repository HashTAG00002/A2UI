"""L0 CI test — import & interface integrity (E17 §5.1; migrated E29).

Wave-B/C contract version. The legacy E17-C re-export shims
(harness/state_adapter, harness/mobilegym_bridge, substrate/base) are GONE by
design (Agent B substrate isolation): the write plane is
``taskvm.execution.gui_driver.make_task_adapters`` (GUI-only, no executor
knob); the read/seed/oracle plane is the physically separate
``WebEvaluationEnvironment`` / ``MobileGymEvaluationEnvironment``. The legacy
governance event-source stack (ScriptedUserDriver / UISimDriver /
UserBehaviorDriver / GovernanceInterpreter / SubgoalGenerator) is a TEST
FIXTURE now and lives in ``tests/fakes/`` — nothing under ``taskvm/`` may
import it (Agent-C role collapse; docs/contracts/architect.md).

Run: ``python -m pytest tests/test_imports.py -x -q``.
"""
import pytest


# ── E29: deleted legacy paths stay deleted (no zombie shims) ───────────────
def test_harness_state_adapter_deleted():
    """harness/state_adapter.py is deleted BY DESIGN (Agent B). Importing it
    must fail — a re-export shim here would resurrect the API-write backdoor."""
    with pytest.raises(ModuleNotFoundError):
        import taskvm.harness.state_adapter  # noqa: F401


def test_substrate_base_deleted():
    """substrate/base.py is deleted BY DESIGN (Agent B)."""
    with pytest.raises(ModuleNotFoundError):
        import taskvm.substrate.base  # noqa: F401


def test_harness_mobilegym_bridge_deleted():
    """harness/mobilegym_bridge.py is deleted; the real home is
    substrate/mobilegym/bridge.py (covered by its own test below)."""
    with pytest.raises(ModuleNotFoundError):
        import taskvm.harness.mobilegym_bridge  # noqa: F401


# ── Agent B: the two successor planes ───────────────────────────────────────
def test_gui_driver_write_plane():
    """TRANSITIONAL smoke (B-3, Oracle audit 2026-08-15): make_task_adapters
    still exists — the evaluation killtest scripts use it — but this test no
    longer ASSERTS the per-platform dispatch shape (GUITaskAdapter vs
    MobileGymTaskAdapter). Locking that shape endorsed the transitional
    architecture the contract requires deleting (upper layer knowing "this
    is web, that is mobilegym"). The platform tables in gui_driver.py are
    registered §6 violations (Transitional Debt Register, contract §8),
    mirrored shrink-only in tests/substrate/test_no_api_backdoor.py;
    Agent E's runtime wave deletes the whole file (formal substrate LOCK
    fails while the register is non-empty — TASKVM_SUBSTRATE_LOCK_AUDIT=1)."""
    import inspect
    from taskvm.execution import gui_driver
    assert callable(gui_driver.make_task_adapters)
    assert "executor" not in inspect.signature(
        gui_driver.make_task_adapters).parameters, (
        "the executor knob (API-write backdoor selector) must not exist")
    a = gui_driver.make_task_adapters(apps=["calendar"], host="localhost")
    assert set(a) == {"calendar"}      # smoke only — no isinstance locks


def test_gui_driver_operator_tables():
    """TRANSITIONAL debt acknowledgment (B-3, Oracle audit): the per-app
    operator tables (_OP_FIELD/_ENTITY_KIND) and platform tuples
    (_WEB_APPS/_MOBILEGYM_APPS) in the execution layer are KNOWN DEBT,
    not correct architecture — the frozen contract bans app-specific
    operator dispatch above the substrate. They are enumerated shrink-only
    in the Transitional Debt Register (tests/substrate/
    test_no_api_backdoor.py mirrors docs/contracts/substrate.md §8);
    Agent E's runtime wave deletes them with the whole file. This test
    only locks what must stay true in ANY architecture: no port table
    and no executor knob in the execution layer."""
    import taskvm.execution.gui_driver as gd
    assert not hasattr(gd, "DEFAULT_PORTS"), \
        "port table must not live in the execution layer"
    import inspect
    assert "executor" not in inspect.signature(
        gd.make_task_adapters).parameters


def test_evaluation_environments_read_plane():
    """builtin_web.evaluation owns the 5 exam-room environments (reset/seed/
    oracle_state); the runtime session grants none of these powers."""
    from taskvm.substrate.builtin_web.evaluation import (
        _EVAL_CLASSES, make_evaluation_environment, make_evaluation_environments)
    assert set(_EVAL_CLASSES) == {"calendar", "taskboard", "drive", "mail",
                                  "outlook_cal"}
    env = make_evaluation_environment("calendar", host="localhost")
    for power in ("reset", "seed", "oracle_state", "session_state", "health"):
        assert hasattr(env, power), f"evaluation env must expose {power}"
    assert not hasattr(env, "mutate"), "evaluation env must NOT write"
    assert make_evaluation_environments([]) == {}


def test_substrate_mobilegym_bridge_new_path():
    """New path — the real home for MobileGymBridge."""
    from taskvm.substrate.mobilegym.bridge import MobileGymBridge, build_app, main
    assert MobileGymBridge is not None
    assert callable(build_app)
    assert callable(main)


def test_mobilegym_evaluation_environment():
    """mobilegym.evaluation: set_state powers are setup-only (exam room), the
    runtime MobileGymTaskAdapter has no canonical read."""
    from taskvm.substrate.mobilegym.evaluation import (
        make_mobilegym_environments, MobileGymEvaluationEnvironment)
    envs = make_mobilegym_environments(["wechat", "alipay"], sid="t_eval",
                                       host="localhost")
    assert set(envs) == {"wechat", "alipay"}
    assert all(isinstance(e, MobileGymEvaluationEnvironment)
               for e in envs.values())
    from taskvm.execution.gui_driver import MobileGymTaskAdapter
    with pytest.raises(RuntimeError):
        MobileGymTaskAdapter(app="wechat", bridge_url="http://x").read_canonical("s")


# ── stable old paths (unmoved real homes) ───────────────────────────────────
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


# ── governance: production surface (Agent C) + fakes (tests/fakes) ─────────
def test_governance_public_surface():
    """Production surface = GovernanceService + the six governance events +
    the staged legacy survivors. The event-source stack is NOT here anymore."""
    from taskvm.governance import (
        GovernanceService, GovernanceOutcome, BootstrapResult,
        GoalRecomposeFailed, GovernanceEvent,
        PauseRequested, ResumeRequested, LocalPatchRequested,
        GoalPatchRequested, RollbackRequested, ConflictResolutionRequested,
        VMStateSnapshot, SubgoalInstruction, CheckpointGraph,
        CheckpointDirection,
    )
    assert GovernanceService is not None
    # the fake stack must NOT be re-exported from production
    import taskvm.governance as g
    for gone in ("UserBehaviorDriver", "UserBehaviorEvent", "EVENT_TYPES",
                 "ScriptedUserDriver", "make_scripted_driver",
                 "GovernanceInterpreter", "UISimDriver"):
        assert not hasattr(g, gone), (
            f"{gone} must live in tests/fakes, not the production surface")


def test_fakes_importable_and_honest():
    """The whole legacy event-source stack is importable from tests/fakes —
    as TEST fixtures (nothing under taskvm/ may import them)."""
    from tests.fakes.user_behavior_driver import (
        UserBehaviorDriver, UserBehaviorEvent, EVENT_TYPES)
    from tests.fakes.scripted_driver import (
        ScriptedUserDriver, make_scripted_driver, _build_minimal_binding)
    from tests.fakes.governance_interpreter import GovernanceInterpreter
    from tests.fakes.ui_sim_driver import UISimDriver
    from tests.fakes.subgoal_generator import generate_subgoal, instruction_for_op
    assert callable(generate_subgoal) and callable(instruction_for_op)
    assert issubclass(UISimDriver, UserBehaviorDriver)
    assert "edit_field" in EVENT_TYPES
    assert "rollback_to" in EVENT_TYPES
    assert "loop_field" in EVENT_TYPES


def test_user_behavior_event_validates_type():
    from tests.fakes.user_behavior_driver import UserBehaviorEvent
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
    from tests.fakes.governance_interpreter import GovernanceInterpreter
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
    """L1 mock pipeline (fakes): ScriptedUserDriver + GovernanceInterpreter
    produce subgoals for a builtin task (no model, no MobileGym)."""
    from tests.fakes.scripted_driver import (
        make_scripted_driver, _build_minimal_binding)
    from tests.fakes.governance_interpreter import GovernanceInterpreter
    from taskvm.governance.vm_state import VMStateSnapshot
    from taskvm.execution.rollback import RollbackLog
    driver = make_scripted_driver("release_reschedule")
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
        f"launch_full should need 4 apps in order; got: {apps}")


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
        f"3 merge strategies must be covered; got: {opts}")
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


# ── EE.10: compiler vision input path ───────────────────────────────────────
def test_compiler_vision_signature():
    """compile_binding accepts a screenshots param (EE.10 §7.1 screenshot+a11y
    encoder); run_one_sample accepts a vision flag + _png_to_data_url works."""
    import inspect
    from taskvm.task_state.compiler import compile_binding
    from taskvm.evaluation.run_w1_killtest import run_one_sample, _png_to_data_url
    sig = inspect.signature(compile_binding)
    assert "screenshots" in sig.parameters, "compile_binding must accept screenshots"
    assert sig.parameters["screenshots"].default is None, "default None = text path"
    sig2 = inspect.signature(run_one_sample)
    assert "vision" in sig2.parameters, "run_one_sample must accept vision"
    # _png_to_data_url: tiny PNG → data:image/png;base64,...
    import tempfile, os
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png); p = f.name
    try:
        url = _png_to_data_url(p)
        assert url.startswith("data:image/png;base64,"), url[:40]
    finally:
        os.remove(p)


# ── FF.2: UISimDriver + full-loop killtest (fakes + GUI-only writes) ────────
def test_ui_sim_driver_importable():
    """UISimDriver is a FAKE now (tests/fakes) + is a UserBehaviorDriver +
    parses rw-field forms (FF.2 §3.1)."""
    from tests.fakes.ui_sim_driver import UISimDriver
    from tests.fakes.user_behavior_driver import UserBehaviorDriver
    assert issubclass(UISimDriver, UserBehaviorDriver)
    # form parser: both the f-string rw-field form & the GenUI genui-field form
    # carry name="var_id" + name="new_value" → both parse to the same {var_id}.
    from tests.fakes.ui_sim_driver import _parse_edit_forms
    html = ('<form class="rw-field" method="post" action="edit">'
            '<input type="hidden" name="var_id" value="release_date">'
            '<input type="text" name="new_value" value="2026-08-14">'
            '<button>apply</button></form>'
            '<form class="genui-field" method="post" action="/sid_x/edit">'
            '<input type="hidden" name="var_id" value="design_review_date">'
            '<input type="date" name="new_value"></form>')
    forms = _parse_edit_forms(html)
    assert set(forms.keys()) == {"release_date", "design_review_date"}
    assert forms["release_date"]["has_value_input"] is True


def test_full_loop_killtest_importable():
    """run_full_loop_killtest importable + run_one_sample + main callable.

    E29 honest note: the FF.2 api-mode 1-sample acceptance run is GONE — the
    API write executor is deleted from the runtime (Agent B). The full loop
    now only runs in GUI mode (real gestures; needs model + browser + apps
    online), which is an operator-invoked evaluation, not an L0 CI assert."""
    from taskvm.evaluation.run_full_loop_killtest import (
        run_one_sample, run_neg_control, summarize, main)
    for fn in (run_one_sample, run_neg_control, summarize, main):
        assert callable(fn)


# ── FF.3: milestone suggestion (Agent-C role collapse contract) ─────────────
def test_suggest_milestones_wiring_and_degrade():
    """FF.3 → Agent-C contract: the seed-time Milestone Suggester LLM call is
    DELETED (milestones come from the one Task Architect call, wired by Agent
    D/E). What remains real: the session fields, the adopt_milestone route,
    and the milestone_suggest_html renderer (采纳 button + adopted ✓ state)."""
    import taskvm.workspace_ui.server as s
    from taskvm.workspace_ui.editable_components import milestone_suggest_html
    # 1. fields on WorkspaceSession
    assert "suggested_milestones" in s.WorkspaceSession.__dataclass_fields__
    assert "adopted_milestones" in s.WorkspaceSession.__dataclass_fields__
    # 2. route registered
    assert any(r.rule.endswith("/<sid>/adopt_milestone")
               for r in s.app.url_map.iter_rules())
    # 3. the seed-time LLM suggester is GONE (no silent model call at seed)
    assert not hasattr(s, "_suggest_milestones"), (
        "the seed-time milestone LLM call is deleted (Agent-C role collapse); "
        "milestones come from the one Task Architect call")
    # 4. HTML: 采纳 button + adopted ✓ state (the renderer stays real)
    html = milestone_suggest_html(
        [{"id": "C1", "name": "会议定", "description": "同步"}], adopted_ids=[])
    assert 'action="adopt_milestone"' in html
    assert 'name="milestone_id" value="C1"' in html
    html_adopted = milestone_suggest_html(
        [{"id": "C1", "name": "会议定", "description": "同步"}], adopted_ids=["C1"])
    assert "已采纳" in html_adopted and "disabled" in html_adopted
    assert milestone_suggest_html([], []) == ""   # graceful: no render


# ── FF.4: workflow planner (Sequential / Parallel / Loop) ────────────────────
def test_workflow_types_importable():
    """FF.4 §5.2: WorkflowNodeType/WorkflowNode/WorkflowPlan importable + the
    enum has the 3 shapes + to_dict round-trips. EVENT_TYPES lives in the
    fakes now (rule-based classifier = test fixture)."""
    from taskvm.governance import (WorkflowNodeType, WorkflowNode, WorkflowPlan,
                                    SubgoalInstruction)
    assert {t.value for t in WorkflowNodeType} == {"sequential", "parallel", "loop"}
    # WorkflowNode + WorkflowPlan to_dict
    n = WorkflowNode(node_type=WorkflowNodeType.LOOP, loop_count=3,
                     loop_values=["T1", "T2", "T3"], display_name="batch")
    assert n.to_dict()["node_type"] == "loop" and n.to_dict()["loop_count"] == 3
    p = WorkflowPlan(task_id="t", nodes=[n], workflow_type="loop")
    assert p.to_dict()["workflow_type"] == "loop" and len(p.to_dict()["nodes"]) == 1
    # EVENT_TYPES (fixture) has loop_field
    from tests.fakes.user_behavior_driver import EVENT_TYPES
    assert "loop_field" in EVENT_TYPES
    # WorkflowExecutor importable + the loop instantiator substitutes entity_id
    from taskvm.execution.workflow_executor import (WorkflowExecutor,
        _instantiate_loop_subgoal, WorkflowResult, NodeResult, SubgoalResult)
    from taskvm.execution.patch_compiler import PatchOp
    tmpl = SubgoalInstruction(natural_language="x",
        patch_ops=[PatchOp(app="taskboard", entity_id="T1", field="assignee",
                           operator="set_assignee", value="Bob")])
    sg_i = _instantiate_loop_subgoal(tmpl, 1, "T2")
    assert sg_i.patch_ops[0].entity_id == "T2"   # substituted
    assert sg_i.patch_ops[0].app == "taskboard" and sg_i.patch_ops[0].value == "Bob"


def test_classify_workflow_parallel_loop_sequential():
    """FF.4 §5.3 _classify_workflow rules (fixture interpreter now): loop_field
    → LOOP; 1 edit_field + bindings ≥2 apps → PARALLEL; else → SEQUENTIAL."""
    from tests.fakes.governance_interpreter import GovernanceInterpreter
    from tests.fakes.scripted_driver import (make_scripted_driver,
                                             _build_minimal_binding)
    from taskvm.governance.vm_state import VMStateSnapshot
    from taskvm.execution.rollback import RollbackLog
    interp = GovernanceInterpreter(enable_llm_rollback_nl=False)

    def classify(task_id):
        drv = make_scripted_driver(task_id)
        binding = _build_minimal_binding(drv.task)
        vm_state = VMStateSnapshot(sid="t", binding=binding, adapters={},
                                    rollback_log=RollbackLog(),
                                    checkpoints=drv.task.checkpoints)
        events = []
        while True:
            ev = drv.next_event()
            if ev is None: break
            events.append(ev)
        return interp._classify_workflow(events, drv.task)

    # launch_fanout_parallel: 1 edit_field + 4-app bindings → PARALLEL
    assert classify("launch_fanout_parallel").value == "parallel"
    # batch_task_assign: loop_field → LOOP
    assert classify("batch_task_assign").value == "loop"
    # doc_handoff: 1 edit_field + 1-app → SEQUENTIAL
    assert classify("doc_handoff").value == "sequential"
    # interpret_as_workflow on batch_task_assign → a LOOP node with loop_values
    drv = make_scripted_driver("batch_task_assign")
    binding = _build_minimal_binding(drv.task)
    vm_state = VMStateSnapshot(sid="t", binding=binding, adapters={},
                               rollback_log=RollbackLog(), checkpoints=drv.task.checkpoints)
    events = []
    while True:
        ev = drv.next_event()
        if ev is None: break
        events.append(ev)
    plan = interp.interpret_as_workflow(events, vm_state, task=drv.task)
    assert plan.workflow_type == "loop"
    assert len(plan.nodes) == 1 and plan.nodes[0].node_type.value == "loop"
    assert plan.nodes[0].loop_values == ["T1", "T2", "T3"]


# ── FF.5: workflow_progress SSE pubsub + _wf_progress_event builder ─────────
def test_workflow_progress_pubsub_and_event():
    """FF.5 §6.3-6.4: the per-sid workflow_progress pubsub (subscribe/push/drain)
    + the event builder shape. Pure (no live execution)."""
    import taskvm.workspace_ui.server as s
    # event builder: {plan_type, nodes:[{idx,type,app,status}], barrier_status}
    ev = s._wf_progress_event("parallel",
        [{"app": "calendar", "status": "running"},
         {"app": "taskboard", "status": "done"}], "waiting")
    assert ev["plan_type"] == "parallel"
    assert ev["nodes"][0]["app"] == "calendar" and ev["nodes"][0]["status"] == "running"
    assert ev["nodes"][1]["status"] == "done"
    assert ev["barrier_status"] == "waiting"
    # pubsub: subscribe → push → drain → unsubscribe
    sid = "test_ff5_pubsub"
    q1 = s.subscribe_workflow_progress(sid)
    q2 = s.subscribe_workflow_progress(sid)
    assert sid in s._workflow_progress_queues
    s.push_workflow_progress(sid, ev)
    drained1 = q1.get_nowait()
    drained2 = q2.get_nowait()
    assert drained1 == ev and drained2 == ev   # both subscribers got it
    s.unsubscribe_workflow_progress(sid, q1)
    s.unsubscribe_workflow_progress(sid, q2)
    assert sid not in s._workflow_progress_queues
    # push to a sid with no subscribers is a no-op (no crash)
    s.push_workflow_progress("no-such-sid", ev)


# ── FF.6: checkpoint celebration trigger (confetti + milestone_reached) ─────
def test_checkpoint_celebration_trigger():
    """FF.6 §7.2 (Agent B API): seeding goes through the evaluation plane
    (``oracle`` envs); the write drivers are GUI-only. The celebration fires
    when the /<sid>/checkpoint + /<sid>/adopt_milestone routes return
    ``milestone_reached: {id, name}`` (JSON) OR redirect with
    ``?celebrate=<name>`` (browser form flow). Requires the builtin apps
    online (same requirement as before — the reads were always HTTP)."""
    import taskvm.workspace_ui.server as s
    from taskvm.benchmark.fixtures import get_task
    from taskvm.execution.gui_driver import make_task_adapters
    from taskvm.substrate.builtin_web.evaluation import (
        make_evaluation_environments,
    )
    JSON_HDR = {"Accept": "application/json"}
    fixture = get_task("release_reschedule")
    adapters = make_task_adapters(apps=["calendar", "taskboard"], host="localhost")
    envs = make_evaluation_environments(["calendar", "taskboard"],
                                        host="localhost")
    sess = s.seed_session(fixture, adapters, oracle=envs, host="localhost")
    sid = sess.sid
    client = s.app.test_client()
    # 1. checkpoint JSON → checkpoint_reached + milestone_reached
    r = client.post(f"/{sid}/checkpoint", data={"format": "json"}, headers=JSON_HDR)
    d = r.get_json() or {}
    assert d.get("checkpoint_reached") is True
    assert isinstance(d.get("milestone_reached"), dict)
    assert d["milestone_reached"].get("id") and d["milestone_reached"].get("name")
    # 2. checkpoint HTML flow → 302 redirect with ?celebrate=<name>
    r2 = client.post(f"/{sid}/checkpoint")
    assert r2.status_code == 302
    assert "celebrate=" in (r2.headers.get("Location") or "")
    # 3. adopt_milestone → milestone_reached (if a suggestion exists)
    sess.suggested_milestones = [{"id": "C1", "name": "会议+任务同步",
                                   "description": "sync"}]
    r3 = client.post(f"/{sid}/adopt_milestone", data={"milestone_id": "C1"},
                     headers=JSON_HDR)
    d3 = r3.get_json() or {}
    assert d3.get("adopted") is True
    assert d3.get("milestone_reached", {}).get("id") == "C1"
    # 4. the celebrate assets are served + the page wires them
    page = client.get(f"/{sid}").get_data(as_text=True)
    assert "/static/confetti.min.js" in page   # local, no CDN
    assert "/static/timeline.js" in page
    # confetti.min.js is non-empty + defines window.confetti
    cj = client.get("/static/confetti.min.js").get_data(as_text=True)
    assert "window.confetti" in cj and "particleCount" in cj
    tj = client.get("/static/timeline.js").get_data(as_text=True)
    assert "celebrateCheckpoint" in tj and "confetti(" in tj
    for env in envs.values():
        env.reset(sid)


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-q"])