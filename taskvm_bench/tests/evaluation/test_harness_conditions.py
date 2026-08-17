"""B-06 — the real-model condition definitions.

Covers the work order's minimum test obligations:

* every condition id exists and resolves through the registry AND
  ``make_harness`` (main / diagnostic / baselines / control);
* ``taskvm-real-full`` REALLY drives the ``bootstrap_real_full`` chain
  (B-07's single composition path — no second orchestration), proven
  with a monkeypatch spy + a scripted ModelPort over a REAL benchmark
  world, ending in a real GUI gesture and the three-role shared ledger;
* ``taskvm-real-cua-only`` is loudly labelled diagnostic (registry set
  + condition id) and can never be mistaken for real-full;
* within one condition the model is PINNED — a provider failure
  propagates with the SAME port object and SAME model id (no silent
  switch, no fallback reconstruction);
* ``make_harness`` rejects model pinning on template conditions.

Scripted ports are contract-wiring only: a scripted pass here is NEVER
a real-model claim (the real-provider leg needs credentials and is the
environment-gated smoke's business, not this file's).
"""
from __future__ import annotations

import json

import pytest

from taskvm.architect import ModelReply
from taskvm.architect.http_port import DEFAULT_MODEL
from taskvm_bench.benchmark.registry import (
    ABLATION_CONDITIONS, DIAGNOSTIC_ONLY_CONDITIONS, PRIMARY_CONDITIONS,
    REAL_MODEL_CONDITIONS, TEMPLATE_CONTROL_CONDITIONS, Condition,
    all_conditions, condition_of,
)
from taskvm_bench.benchmark.schema import Family, Split, TaskSpec
from taskvm_bench.evaluation.harness import (
    DirectCUARealHarness, PlannerCUARealHarness, RealCUAOnlyHarness,
    RealFullHarness, TaskVMHarness, TrialBudget, make_harness,
)
from taskvm_bench.evaluation.world import BenchmarkWorld, WorldSubstrate


# ── the scripted provider (schemas mirror the B-07 wiring test) ────────────

GOAL = ("Set taskboard_release_status to approved. "
        "Set mail_digest_headline to approved.")

COMPILER_REPLY = {
    "variables": [
        {"semantic_key": "taskboard_release_status",
         "label": "taskboard_release_status", "value_type": "str",
         "mutability": "editable", "observed": "draft", "confidence": 0.98,
         "evidence": [{
             "surface_label": "desktop",
             "visible_label": "taskboard_release_status",
             "visible_context": "taskboard_release_status=draft",
             "value_pattern": r"taskboard_release_status=(\S+)"}]},
        {"semantic_key": "mail_digest_headline",
         "label": "mail_digest_headline", "value_type": "str",
         "mutability": "editable", "observed": "pending", "confidence": 0.98,
         "evidence": [{
             "surface_label": "desktop",
             "visible_label": "mail_digest_headline",
             "visible_context": "mail_digest_headline=pending",
             "value_pattern": r"mail_digest_headline=(\S+)"}]},
    ],
    "ambiguities": [], "needs_clarification": False,
}

ARCHITECT_REPLY = {
    "variables": [
        {"semantic_key": "taskboard_release_status",
         "label": "taskboard_release_status", "value_type": "str",
         "mutability": "editable", "desired": "approved"},
        {"semantic_key": "mail_digest_headline",
         "label": "mail_digest_headline", "value_type": "str",
         "mutability": "editable", "desired": "approved"},
    ],
    "workflow": {"nodes": [
        {"kind": "action", "label": "通过发布状态",
         "semantic_goal": "approve the release",
         "sets": {"taskboard_release_status": "approved"},
         "completion": "taskboard_release_status==approved",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["taskboard_release_status"]},
        {"kind": "action", "label": "更新邮件摘要",
         "semantic_goal": "set the digest headline",
         "sets": {"mail_digest_headline": "approved"},
         "completion": "mail_digest_headline==approved",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["mail_digest_headline"]},
        {"kind": "terminal", "label": "完成",
         "after": ["通过发布状态", "更新邮件摘要"]},
    ]},
}


def _cua_act(key: str, value: str) -> dict:
    return {"kind": "act",
            "action": {"kind": "type", "text": f"{key}={value}"}}


CUA_DONE = {"kind": "done"}


class ScriptedPort:
    """One scripted reply per provider request, in call order."""

    default_model = "scripted-b06"

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []
        self.models: list = []      # the model arg each request received

    def complete_json(self, *, system, user, model=None, max_tokens=3072,
                      temperature=None, image_data_url=None):
        self.calls.append(system + "\n--\n" + user)
        self.models.append(model)
        item = self.script.pop(0) if self.script else CUA_DONE
        return ModelReply(parsed=item,
                          raw=json.dumps(item, ensure_ascii=False),
                          model=model or self.default_model,
                          prompt_tokens=5, completion_tokens=3)


class FailingPort:
    """A pinned port whose provider is down — failures must propagate
    with the SAME port/model, never a silent switch."""

    default_model = "pinned-m1"

    def complete_json(self, **kw):
        raise RuntimeError("provider down (B-06 pinning test)")


def _world() -> WorldSubstrate:
    spec = TaskSpec(
        task_id="b06-wiring", family=Family.SEQUENCE, split=Split.ID,
        goal=GOAL, surfaces=("desktop",),
        seed={"desktop": {"taskboard_release_status": "draft",
                          "mail_digest_headline": "pending"}},
        success={"desktop": {"taskboard_release_status": "approved",
                             "mail_digest_headline": "approved"}},
    )
    world = BenchmarkWorld(spec)
    world.begin_trial()
    return WorldSubstrate(world)


# ── 1. every condition id exists and resolves ─────────────────────────────

EXPECTED_HARNESS_BY_ID = {
    "taskvm-real-full": RealFullHarness,
    "taskvm-real-cua-only": RealCUAOnlyHarness,
    "direct-cua-real": DirectCUARealHarness,
    "planner-cua-real": PlannerCUARealHarness,
    "taskvm-template-control": TaskVMHarness,
}


class TestConditionIdsResolve:
    @pytest.mark.parametrize("cid", sorted(EXPECTED_HARNESS_BY_ID))
    def test_id_resolves_to_the_right_harness(self, cid):
        cond = condition_of(cid)          # ValueError if unknown
        harness = make_harness(cid)
        assert isinstance(harness, EXPECTED_HARNESS_BY_ID[cid])
        assert harness.condition is cond
        # the template control keeps the TaskVM structure, explicitly
        # labelled by its own condition id
        if cid == "taskvm-template-control":
            assert harness.condition.value == "taskvm-template-control"

    def test_real_series_registered_in_registry_groups(self):
        ids = {c.value for c in REAL_MODEL_CONDITIONS}
        assert ids == {"direct-cua-real", "planner-cua-real",
                       "taskvm-real-full"}
        assert {c.value for c in TEMPLATE_CONTROL_CONDITIONS} == \
            {"taskvm-template-control"}
        # the real ids are also part of the registry's known universe
        universe = {c.value for c in all_conditions()}
        assert ids <= universe
        assert "taskvm-real-cua-only" in universe
        # the primary/ablation taxonomy is untouched by B-06
        assert len(PRIMARY_CONDITIONS) == 4
        assert len(ABLATION_CONDITIONS) == 2

    def test_make_harness_rejects_model_pinning_on_template_conditions(self):
        for cid in ("taskvm", "direct-cua", "planner-cua",
                    "taskvm-template-control"):
            with pytest.raises(ValueError, match="only apply to real-model"):
                make_harness(cid, model="some-model")


# ── 2. taskvm-real-full REALLY drives bootstrap_real_full ─────────────────

class TestRealFullWiring:
    def test_real_full_reuses_bootstrap_real_full(self, monkeypatch):
        """Contract wiring: the composition path is EXACTLY B-07's
        bootstrap (spied via monkeypatch), the provider requests are
        real (scripted replies, real request bookkeeping), the final
        gesture goes through the world's real GUI substrate, and all
        three roles land in ONE shared ledger."""
        import taskvm_bench.evaluation.harness as harness_mod

        calls = {"n": 0}

        def _spy(**kwargs):
            calls["n"] += 1
            # the REAL function, pre-bound — the spy only observes
            return _REAL_BOOTSTRAP(**kwargs)

        _REAL_BOOTSTRAP = harness_mod.bootstrap_real_full
        monkeypatch.setattr(harness_mod, "bootstrap_real_full", _spy)

        port = ScriptedPort([
            COMPILER_REPLY, ARCHITECT_REPLY,
            _cua_act("taskboard_release_status", "approved"),
            _cua_act("mail_digest_headline", "approved"),
            CUA_DONE, CUA_DONE, CUA_DONE, CUA_DONE,
        ])
        harness = RealFullHarness(model_port=port)
        substrate = _world()

        outcome = harness.run(substrate, GOAL, budget=TrialBudget(
            max_rounds=8, max_turns=8))

        assert calls["n"] == 1, "real-full must compose via bootstrap_real_full"

        # the model chain really happened: compiler + architect requests
        # (goal text in both prompts), then CUA requests
        assert len(port.calls) >= 2
        assert GOAL.split(".")[0] in port.calls[0]      # compiler prompt
        assert GOAL.split(".")[0] in port.calls[1]      # architect prompt
        roles = outcome.model_calls_by_role
        assert roles.get("state_compiler", 0) >= 1
        assert roles.get("task_architect", 0) >= 1

        # the final action went through the REAL GUI substrate
        assert outcome.gui_actions >= 1
        world = substrate.world
        assert world.visible_text("desktop").count("approved") == 2

        # ONE shared ledger across all three roles: every provider
        # request has exactly one row (A-13); the model is PINNED — every
        # request the port received carried the same model argument
        # (None → the port's pinned default), and every CUA ledger row
        # records that same pinned model id (compiler/architect rows
        # leave it empty when no pin was passed — an honest blank, not a
        # switch).
        ledger = harness._rm_ledger
        assert ledger.total() == len(port.calls)
        assert len(set(port.models)) == 1, (
            f"model argument drifted across requests: {port.models}")
        from taskvm.architect import MODEL_ROLE_CUA
        cua_rows = [r for r in ledger.records if r.role == MODEL_ROLE_CUA]
        assert cua_rows, "no CUA rows in the shared ledger"
        assert {r.model for r in cua_rows} == {ScriptedPort.default_model}
        cua_ids = [r.request_id for r in cua_rows]
        assert len(cua_ids) == len(set(cua_ids))   # unique per request

    def test_real_full_architect_failure_is_honest_no_plan(self):
        """No fixture plan fallback: an INVALID architect product (empty
        workflow) fails loudly at composition (ArchitectOutputError
        after its bounded repair attempts) — nothing hand-built is ever
        substituted (B-06 forbidden list). The runner records a harness
        crash honestly; the pinning below stays untouched."""
        from taskvm.architect.architect import ArchitectOutputError
        no_plan_architect = dict(ARCHITECT_REPLY)
        no_plan_architect["workflow"] = {"nodes": []}
        port = ScriptedPort([COMPILER_REPLY, no_plan_architect])
        harness = RealFullHarness(model_port=port)
        substrate = _world()
        with pytest.raises(ArchitectOutputError):
            harness.run(substrate, GOAL, budget=TrialBudget(
                max_rounds=2, max_turns=2))
        assert harness._rm_port is port        # no re-construction either
        assert harness.pinned_model == ScriptedPort.default_model

    def test_real_full_uses_template_free_objects(self):
        """The composed chain holds NO template capability objects."""
        port = ScriptedPort([COMPILER_REPLY, ARCHITECT_REPLY])
        harness = RealFullHarness(model_port=port)
        substrate = _world()
        ledger, used_port, compiler, architect, kernel, gov, runtime, cua = \
            harness._compose(substrate, GOAL)
        from taskvm_bench.evaluation.actors import (
            TemplateCUA, TemplateModelPort,
        )
        assert not isinstance(used_port, TemplateModelPort)
        assert not isinstance(cua.inner, TemplateCUA)
        # the kernel's plan came from the architect product (no demo ids)
        graph = kernel.workflow().graph
        assert graph is not None and len(graph.nodes) == 3


# ── 3. taskvm-real-cua-only is loudly diagnostic ───────────────────────────

class TestRealCUAOnlyDiagnostic:
    def test_registered_as_diagnostic_only(self):
        assert Condition.TASKVM_REAL_CUA_ONLY in DIAGNOSTIC_ONLY_CONDITIONS
        assert Condition.TASKVM_REAL_FULL not in DIAGNOSTIC_ONLY_CONDITIONS

    def test_condition_id_never_disguises_itself_as_real_full(self):
        h = make_harness("taskvm-real-cua-only")
        assert h.condition.value == "taskvm-real-cua-only"
        assert h.condition.value != "taskvm-real-full"
        assert h.condition is not Condition.TASKVM_REAL_FULL

    def test_wiring_template_planner_real_cua(self):
        """compiler/architect stay template; ONLY the CUA leg is the
        pinned real port (scripted here) — verified through _compose."""
        port = ScriptedPort([_cua_act("taskboard_release_status",
                                      "approved"),
                             _cua_act("mail_digest_headline", "approved"),
                             CUA_DONE, CUA_DONE, CUA_DONE, CUA_DONE])
        harness = RealCUAOnlyHarness(model_port=port)
        substrate = _world()
        ledger, used_port, compiler, architect, kernel, gov, runtime, cua = \
            harness._compose(substrate, GOAL)
        from taskvm_bench.evaluation.actors import TemplateModelPort
        # the compiler/architect port IS template in this diagnostic
        assert isinstance(used_port, TemplateModelPort)
        # the CUA port is the pinned real one (scripted stand-in)
        assert cua.inner._port is port
        # one shared ledger object for both halves
        assert ledger is harness._rm_ledger


# ── 4. model pinning — no silent switch ────────────────────────────────────

class TestModelPinning:
    @pytest.mark.parametrize("cid", ["taskvm-real-full", "direct-cua-real",
                                     "planner-cua-real",
                                     "taskvm-real-cua-only"])
    def test_provider_failure_keeps_the_same_port_and_model(self, cid):
        """A failing provider either propagates (direct composition
        paths raise out of run()) or is honestly contained by the
        runtime's error taxonomy (invalid-prediction accounting — every
        CUA ledger row lands ok=False). Either way the harness keeps the
        SAME port object and the SAME pinned model id: there is no
        failure-triggered re-construction, no fallback model."""
        from taskvm.architect import MODEL_ROLE_CUA
        harness = make_harness(cid, model_port=FailingPort())
        port_before = harness._rm_port
        substrate = _world()
        try:
            harness.run(substrate, GOAL, budget=TrialBudget(
                max_rounds=1, max_turns=1))
            raised = False
        except RuntimeError:
            raised = True
        # honest either way: raise, or contained with ok=False rows only
        if not raised:
            cua_rows = [r for r in harness._rm_ledger.records
                        if r.role == MODEL_ROLE_CUA]
            assert all((not r.ok) for r in cua_rows), (
                "a failed provider produced ok=True rows")
            assert all(r.model == "pinned-m1" for r in cua_rows), (
                "a failed provider switched models mid-trial")
        assert harness._rm_port is port_before
        assert harness.pinned_model == "pinned-m1"
        # a second run still uses the same pinned port (no switch)
        try:
            harness.run(substrate, GOAL, budget=TrialBudget(
                max_rounds=1, max_turns=1))
        except RuntimeError:
            pass
        assert harness._rm_port is port_before
        assert harness.pinned_model == "pinned-m1"

    def test_pinned_model_defaults_to_the_env_model(self):
        """Without an explicit pin the port resolves the standard
        TASKVM_MODEL / DEFAULT_MODEL chain — one fixed id per trial."""
        harness = make_harness("taskvm-real-full")
        assert harness.pinned_model == DEFAULT_MODEL or \
            isinstance(harness.pinned_model, str) and harness.pinned_model
        assert harness.pinned_model == harness._rm_port.default_model

    def test_single_model_id_across_a_full_trial(self):
        """Every CUA ledger row of one real-full trial carries the same
        pinned model id, and every provider request received the same
        model argument (pinning observable in the accounting)."""
        from taskvm.architect import MODEL_ROLE_CUA
        port = ScriptedPort([
            COMPILER_REPLY, ARCHITECT_REPLY,
            _cua_act("taskboard_release_status", "approved"),
            _cua_act("mail_digest_headline", "approved"),
            CUA_DONE, CUA_DONE, CUA_DONE, CUA_DONE])
        harness = RealFullHarness(model_port=port)
        substrate = _world()
        harness.run(substrate, GOAL, budget=TrialBudget(
            max_rounds=8, max_turns=8))
        assert len(set(port.models)) == 1, (
            f"model argument drifted: {port.models}")
        cua_models = {r.model for r in harness._rm_ledger.records
                      if r.role == MODEL_ROLE_CUA}
        assert cua_models == {ScriptedPort.default_model}, (
            f"model ids drifted within one condition: {cua_models}")


# ── 5. the real baselines share the template structure ────────────────────

class TestRealBaselines:
    def test_direct_cua_real_wiring(self):
        port = ScriptedPort([
            _cua_act("taskboard_release_status", "approved"),
            _cua_act("mail_digest_headline", "approved"),
            CUA_DONE, CUA_DONE])
        harness = DirectCUARealHarness(model_port=port)
        substrate = _world()
        outcome = harness.run(substrate, GOAL, budget=TrialBudget(
            max_turns=6))
        # the bare loop drove the real (scripted) CUA to real GUI acts
        assert outcome.gui_actions >= 1
        assert outcome.model_calls_by_role.get("cua", 0) >= 1
        assert "approved" in substrate.world.visible_text("desktop")

    def test_planner_cua_real_wiring(self):
        port = ScriptedPort([
            _cua_act("taskboard_release_status", "approved"),
            _cua_act("mail_digest_headline", "approved"),
            CUA_DONE, CUA_DONE])
        harness = PlannerCUARealHarness(model_port=port)
        substrate = _world()
        outcome = harness.run(substrate, GOAL, budget=TrialBudget(
            max_turns=6))
        assert outcome.gui_actions >= 1
        # planner layer still counts its per-turn instruction emission
        assert outcome.model_calls_by_role.get("planner", 0) >= 1
        assert outcome.model_calls_by_role.get("cua", 0) >= 1

    def test_template_twins_unchanged(self):
        """The template conditions behave exactly as before B-06 —
        the capability seams did not alter their wiring."""
        for cid, cls in (("direct-cua", DirectCUARealHarness),
                         ("planner-cua", PlannerCUARealHarness)):
            h = make_harness(cid)
            assert type(h).__name__ == cls.__name__.replace("Real", "")
            substrate = _world()
            outcome = h.run(substrate, GOAL, budget=TrialBudget(max_turns=8))
            assert outcome.gui_actions >= 1
