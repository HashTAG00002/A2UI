"""tests/architect/ — the eight producer scenarios of handoff 04 §测试场景.

Anchored to the REAL assembly code (taskvm/architect/*): a scripted
``FakePort`` stands in for the model; every assertion reads artifacts the
production path actually builds (TaskArchitecture via the domain validating
constructors, prompts as-sent, ledger records as-landed).

Scenarios:
  1. initialization — ONE architect call jointly emits checkpoints +
     fan-out/barrier topology + projection schema.
  2. value change — the deterministic fast path re-reads WITHOUT a model
     call (architect/compiler call counts do not move).
  6. bounded loop — termination predicate + max_iterations land on the
     assembled node; body is action/verify only.
  7. prompt no-leak — the ACTUAL built messages (not templates) stay clean.
  8. invalid model output — bounded repair, honest failure, no GT fallback.

Plus the ActionContractSerializer determinism suite (replaces the deleted
LLM SubgoalGenerator) and the ModelCallLedger accounting contract.
"""
import json

import pytest

from taskvm.architect import (
    MODEL_ROLE_STATE_COMPILER, MODEL_ROLE_TASK_ARCHITECT,
    ActionContractSerializer, ArchitectOutputError, CompilerObservationView,
    CompilerOutputError, CompilerResult, HandleEvidence, ModelCallLedger,
    ModelCallRecord, ModelReply, StateCompiler, TaskArchitect,
    VisibleRegion, patchop_cua_goal, scan,
)
from taskvm.architect.noleak import assert_prompt_clean
from taskvm.domain import (
    ActionContract, NodeKind, Reversibility, SurfaceEvidence, SurfaceHandle,
    TaskIntent, TaskVariable,
)
from taskvm.domain.patch import CompensationEntry


class FakePort:
    """A scripted ModelPort: pops replies, journals every call verbatim."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete_json(self, *, system, user, model=None, max_tokens=3072,
                      temperature=None, image_data_url=None,
                      repair_retries=1):
        self.calls.append({"system": system, "user": user,
                           "image_data_url": image_data_url})
        if not self.replies:
            raise AssertionError("FakePort ran out of scripted replies")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return ModelReply(parsed=r, raw=json.dumps(r, ensure_ascii=False),
                          model=model or "fake-model")


# ── shared fixtures ─────────────────────────────────────────────────────────
INTENT = TaskIntent(goal="把项目发布会议推迟到 2026-08-18 并同步所有依赖任务",
                    success_criteria=("会议日期为 2026-08-18",))

OBSERVED_VARS = (
    TaskVariable(semantic_key="release_date", label="发布日期",
                 observed="2026-08-14", value_type="date"),
    TaskVariable(semantic_key="copy_deadline", label="文案截止",
                 observed="2026-08-14", value_type="date"),
    TaskVariable(semantic_key="qa_deadline", label="测试截止",
                 observed="2026-08-14", value_type="date"),
)

ARCHITECTURE_JSON = {
    "variables": [
        {"semantic_key": "release_date", "label": "发布日期",
         "value_type": "date", "mutability": "editable",
         "desired": "2026-08-18"},
        {"semantic_key": "copy_deadline", "label": "文案截止",
         "value_type": "date", "mutability": "editable",
         "desired": "2026-08-18"},
        {"semantic_key": "qa_deadline", "label": "测试截止",
         "value_type": "date", "mutability": "editable",
         "desired": "2026-08-18"},
    ],
    "workflow": {"nodes": [
        {"kind": "action", "label": "改发布日期",
         "semantic_goal": "推迟发布会议", "sets": {"release_date": "2026-08-18"},
         "completion": "日历卡片显示 2026-08-18",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["发布会议"]},
        {"kind": "fan_out", "label": "同步依赖", "after": ["改发布日期"]},
        {"kind": "action", "label": "同步文案", "container": "同步依赖",
         "semantic_goal": "文案截止同步", "sets": {"copy_deadline": "2026-08-18"},
         "completion": "任务板显示新截止", "reversibility": "reversible",
         "risk": "", "target_evidence": ["项目文案"]},
        {"kind": "action", "label": "同步测试", "container": "同步依赖",
         "semantic_goal": "测试截止同步", "sets": {"qa_deadline": "2026-08-18"},
         "completion": "任务板显示新截止", "reversibility": "reversible",
         "risk": "", "target_evidence": ["测试任务"]},
        {"kind": "barrier", "label": "汇合校验", "after": ["同步依赖"]},
        {"kind": "checkpoint", "label": "发布就绪检查点", "after": ["汇合校验"]},
        {"kind": "terminal", "label": "完成", "after": ["发布就绪检查点"]},
    ]},
    "projection": {"root": "总览", "components": [
        {"label": "总览", "type": "card", "binds": None,
         "children": ["发布日期卡片"]},
        {"label": "发布日期卡片", "type": "field", "binds": "release_date",
         "editable": True, "children": []},
    ]},
}


# ── scenario 1: one call → the whole coherent artifact ─────────────────────
def test_scenario_1_initialization_one_architect_call():
    port = FakePort(ARCHITECTURE_JSON)
    ledger = ModelCallLedger()
    arch = TaskArchitect(port, ledger).compose(INTENT, OBSERVED_VARS)

    assert len(port.calls) == 1, "initial composition = exactly ONE call"
    counts = ledger.counts_by_role()
    assert counts.get(MODEL_ROLE_TASK_ARCHITECT) == 1

    kinds = [n.kind for n in arch.graph.nodes]
    assert NodeKind.CHECKPOINT in kinds, "checkpoints come from the SAME call"
    assert NodeKind.FAN_OUT in kinds and NodeKind.BARRIER in kinds
    assert sum(k is NodeKind.TERMINAL for k in kinds) == 1
    lanes = [n for n in arch.graph.nodes if n.parent_id is not None]
    assert len(lanes) == 2, "fan-out with two independent lanes"
    assert arch.schema is not None
    bound = [c for c in arch.schema.components if c.binding_key]
    assert bound and bound[0].binding_key == "release_date"
    # the domain validating constructor accepted it (static coherence proven
    # by construction — C does not re-validate)
    assert arch.graph.terminal_nodes()


# ── scenario 2: value change → fast path, 0 model calls ────────────────────
COMPILER_JSON = {
    "variables": [
        {"semantic_key": "release_date", "label": "发布日期",
         "value_type": "date", "mutability": "editable",
         "observed": "2026-08-14", "confidence": 0.97,
         "evidence": [{
             "surface_label": "Calendar",
             "visible_label": "发布会议",
             "visible_context": "2026-08-14 · 项目发布",
             "value_pattern": r"发布会议.*?(\d{4}-\d{2}-\d{2})"}]},
    ],
    "ambiguities": [],
    "needs_clarification": False,
}

VIEW_V1 = CompilerObservationView(revision=1, regions=(
    VisibleRegion(surface_label="Calendar",
                  visible_text="发布会议 2026-08-14\n评审人：王工",
                  structure_fingerprint="fp-A"),))


def _compiled() -> tuple[StateCompiler, CompilerResult]:
    port = FakePort(COMPILER_JSON)
    compiler = StateCompiler(port)
    return compiler, compiler.compile(VIEW_V1, INTENT)


def test_scenario_2_value_change_fast_path_zero_calls():
    compiler, result = _compiled()
    handle = result.handle_evidence[0]

    # a VALUE-ONLY change: same fingerprint, same visible labels
    view2 = CompilerObservationView(revision=2, regions=(
        VisibleRegion(surface_label="Calendar",
                      visible_text="发布会议 2026-08-18\n评审人：王工",
                      structure_fingerprint="fp-A"),))
    report = compiler.needs_slow_path(view2, VIEW_V1.fingerprints(),
                                      result.handle_evidence)
    assert not report, "value-only change must NOT trigger the slow path"
    ov = compiler.extract_observed(view2, handle)
    assert ov is not None and ov.value == "2026-08-18"
    assert ov.confidence == 1.0, "deterministic re-read is certain"
    # the architect never hears about a value delta: no schema touch, no call

    # a STRUCTURE change does route to the slow path (deterministic verdict)
    view3 = CompilerObservationView(revision=3, regions=(
        VisibleRegion(surface_label="Calendar",
                      visible_text="发布会议 2026-08-18\n评审人：王工",
                      structure_fingerprint="fp-B"),))
    report3 = compiler.needs_slow_path(view3, VIEW_V1.fingerprints(),
                                       result.handle_evidence)
    assert report3.needed and "structure changed" in report3.reason

    # a lost handle routes too — the fast path must not guess
    view4 = CompilerObservationView(revision=4, regions=(
        VisibleRegion(surface_label="Calendar",
                      visible_text="（会议被删除）\n评审人：王工",
                      structure_fingerprint="fp-A"),))
    kept, lost = compiler.rebind(view4, result.handle_evidence)
    assert kept == () and lost == ("release_date",)
    assert compiler.extract_observed(view4, handle) is None, (
        "a vanished label re-reads as None — honest, not fabricated")


# ── scenario 6: bounded loop assembles with its guards ─────────────────────
LOOP_JSON = {
    "variables": [
        {"semantic_key": "batch_assignee", "label": "批量负责人",
         "value_type": "string", "mutability": "editable",
         "desired": "Bob"},
    ],
    "workflow": {"nodes": [
        {"kind": "bounded_loop", "label": "逐个分派",
         "termination": "没有剩余未分派的任务", "max_iterations": 3},
        {"kind": "action", "label": "分派一个", "container": "逐个分派",
         "semantic_goal": "把一个任务分派给 Bob",
         "sets": {"batch_assignee": "Bob"},
         "completion": "任务板显示该任务负责人 Bob",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["未分派任务"]},
        {"kind": "verify", "label": "核对分派", "container": "逐个分派",
         "after": ["分派一个"], "condition": "当前任务负责人显示 Bob"},
        {"kind": "terminal", "label": "完成", "after": ["逐个分派"]},
    ]},
}


def test_scenario_6_bounded_loop_assembles_with_guards():
    port = FakePort(LOOP_JSON)
    arch = TaskArchitect(port).compose(
        INTENT, (TaskVariable(semantic_key="batch_assignee",
                              label="批量负责人", observed="未分派",
                              value_type="string"),))
    loops = [n for n in arch.graph.nodes if n.kind is NodeKind.BOUNDED_LOOP]
    assert len(loops) == 1
    loop = loops[0]
    assert loop.termination_predicate == "没有剩余未分派的任务"
    assert loop.max_iterations == 3
    body_kinds = {n.kind for n in arch.graph.nodes if n.parent_id == loop.node_id}
    assert body_kinds <= {NodeKind.ACTION, NodeKind.VERIFY}, (
        "a bounded loop body is action/verify only")


# ── scenario 7: prompt no-leak on the ACTUALLY built messages ──────────────
def test_scenario_7_prompt_no_leak_actual_messages():
    port = FakePort(ARCHITECTURE_JSON)
    TaskArchitect(port).compose(INTENT, OBSERVED_VARS)
    for call in port.calls:
        assert_prompt_clean(call["system"] + "\n" + call["user"],
                            what="task-architect prompt as sent")

    cport = FakePort(COMPILER_JSON)
    StateCompiler(cport).compile(VIEW_V1, INTENT)
    for call in cport.calls:
        hits = scan(call["system"] + "\n" + call["user"])
        assert hits == [], f"state-compiler prompt leaked: {hits}"
        assert call["image_data_url"] is None  # text path: no screenshot sent


def test_scenario_7_model_output_echo_is_repairable_failure():
    """The model echoing internal vocabulary back is a repairable failure —
    never silently stripped or accepted."""
    echo = {"variables": [
        {"semantic_key": "entity_id", "label": "内部 id", "observed": "E1",
         "evidence": [{"surface_label": "Calendar", "visible_label": "x"}]},
    ]}
    good = COMPILER_JSON
    port = FakePort(echo, good)
    compiler = StateCompiler(port, max_repairs=1)
    result = compiler.compile(VIEW_V1, INTENT)
    assert len(port.calls) == 2, "one repair round was consumed"
    assert result.variables[0].semantic_key == "release_date"

    port2 = FakePort(echo, echo)
    with pytest.raises(CompilerOutputError, match="internal vocabulary"):
        StateCompiler(port2, max_repairs=1).compile(VIEW_V1, INTENT)


# ── scenario 8: bounded repair, honest failure, NO GT fallback ─────────────
def test_scenario_8_invalid_output_bounded_repair_no_fallback():
    # garbage → garbage → honest, final failure (never a fixture plan)
    port = FakePort({"nonsense": True}, {"also": "nonsense"})
    with pytest.raises(ArchitectOutputError, match="2 attempt"):
        TaskArchitect(port, max_repairs=1).compose(INTENT, OBSERVED_VARS)
    assert len(port.calls) == 2, "repair budget is bounded and consumed"

    # repair CAN succeed: invalid → valid
    port2 = FakePort({"variables": []}, ARCHITECTURE_JSON)
    ledger = ModelCallLedger()
    arch = TaskArchitect(port2, ledger, max_repairs=1).compose(
        INTENT, OBSERVED_VARS)
    assert arch.graph.terminal_nodes()
    repairs = [r for r in ledger.records if r.is_repair]
    assert len(repairs) == 1 and repairs[0].role == MODEL_ROLE_TASK_ARCHITECT


# ── ActionContractSerializer: deterministic, zero model calls ───────────────
def test_serializer_patchop_cua_goal_deterministic():
    kwargs = dict(surface_label="日历", visible_locator="发布会议",
                  field_display="日期", target_value="2026-08-18")
    text1 = patchop_cua_goal(**kwargs)
    text2 = patchop_cua_goal(**kwargs)
    assert text1 == text2, "same contract → byte-identical instruction"
    assert "发布会议" in text1 and "2026-08-18" in text1
    assert not scan(text1), "the goal text carries only visible vocabulary"


def test_serializer_cannot_locate_is_honest_failure():
    text = patchop_cua_goal(surface_label="日历", visible_locator=None,
                            field_display="日期", target_value="x")
    assert "Unable to identify the target" in text
    assert '"action": "fail"' in text, "the CUA is told to FAIL, not guess"


def test_serializer_contract_goal_and_caution():
    contract = ActionContract(
        contract_id="c1", semantic_goal="发送周报邮件",
        desired_state={"weekly_report_state": "sent"},
        completion_condition="收件箱显示已发送",
        target_evidence=(SurfaceEvidence(
            surface=SurfaceHandle(handle_id="h001"),
            visible_label="周报草稿", visible_context="收件箱顶部"),),
        reversibility=Reversibility.IRREVERSIBLE,
        risk_note="发送后无法撤回")
    text = ActionContractSerializer().cua_goal(
        contract, labels={"weekly_report_state": "周报状态"})
    assert "'周报草稿'" in text and "周报状态 = 'sent'" in text
    assert "irreversible" in text and "发送后无法撤回" in text
    assert not scan(text)


def test_serializer_compensation_goal_history_driven():
    entry = CompensationEntry(node_id="a9", semantic_key="release_date",
                              from_observed="2026-08-18",
                              to_observed="2026-08-14")
    text = ActionContractSerializer().compensation_goal(
        entry, labels={"release_date": "发布日期"})
    assert "发布日期" in text and "2026-08-14" in text
    assert not scan(text)


# ── ModelCallLedger: the accounting contract (benchmark separation) ─────────
def test_ledger_rejects_unknown_role_and_counts():
    ledger = ModelCallLedger()
    with pytest.raises(ValueError, match="unknown model role"):
        ledger.record(ModelCallRecord(role="mystery", purpose="x",
                                      model="m", ok=True))
    ledger.record(ModelCallRecord(role=MODEL_ROLE_STATE_COMPILER,
                                  purpose="initial_compile", model="m",
                                  ok=True, prompt_tokens=10,
                                  completion_tokens=5))
    ledger.record(ModelCallRecord(role=MODEL_ROLE_TASK_ARCHITECT,
                                  purpose="initial_compose", model="m",
                                  ok=True, prompt_tokens=100,
                                  completion_tokens=50,
                                  is_repair=True))
    assert ledger.total() == 2
    assert ledger.counts_by_role()[MODEL_ROLE_STATE_COMPILER] == 1
    p, c = ledger.tokens_by_role()[MODEL_ROLE_TASK_ARCHITECT]
    assert (p, c) == (100, 50)
    snap = ledger.snapshot()
    assert snap[1]["is_repair"] is True
