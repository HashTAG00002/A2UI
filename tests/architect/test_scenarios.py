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

Plus the W0.2 / RFC-A01 schema-liberation anchors: observation/trigger
actions with empty 'sets' are legal at NODE level (task-level handle),
the sequence chain is completed in listed order (phantom-fork shape
from the 2026-08-19 demo baseline replays verbatim), a contradictory
'after' edge is rejected with specific guidance, and the default
bounded-repair budget is four attempts.
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
    ActionContract, NodeKind, NodeStatus, Reversibility, SurfaceEvidence,
    SurfaceHandle, TaskIntent, TaskVariable,
)
from taskvm.domain.patch import CompensationEntry


class FakePort:
    """A scripted ModelPort: pops replies, journals every call verbatim."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete_json(self, *, system, user, model=None, max_tokens=3072,
                      temperature=None, image_data_url=None):
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

    # C-F1 (Oracle audit): a RECOVERABLE structure change — fingerprint
    # drifted fp-A→fp-B but the handle's visible label is still present
    # exactly once — must NOT trigger the slow path. The deterministic
    # rebind answers it with 0 model calls (handoff-04 ladder level 2).
    # The old test oracle asserted `report3.needed` here, locking the
    # buggy unconditional-fingerprint-slow-path as correct behavior.
    view3 = CompilerObservationView(revision=3, regions=(
        VisibleRegion(surface_label="Calendar",
                      visible_text="发布会议 2026-08-18\n评审人：王工",
                      structure_fingerprint="fp-B"),))
    report3 = compiler.needs_slow_path(view3, VIEW_V1.fingerprints(),
                                       result.handle_evidence)
    assert not report3.needed, (
        "recoverable structural change must stay on the deterministic "
        "rebind fast path (0 model calls), not route to the slow path")
    assert "recovered" in report3.reason and report3.changed_surfaces
    kept3, lost3 = compiler.rebind(view3, result.handle_evidence)
    assert kept3 == result.handle_evidence and lost3 == ()
    ov3 = compiler.extract_observed(view3, handle)
    assert ov3 is not None and ov3.value == "2026-08-18"

    # an UNRECOVERABLE structure change — fingerprint drifted AND the
    # handle's label vanished — does route to the slow path (ladder 3).
    view3b = CompilerObservationView(revision=3, regions=(
        VisibleRegion(surface_label="Calendar",
                      visible_text="（会议被删除）\n评审人：王工",
                      structure_fingerprint="fp-B"),))
    report3b = compiler.needs_slow_path(view3b, VIEW_V1.fingerprints(),
                                        result.handle_evidence)
    assert report3b.needed and "lost" in report3b.reason

    # same-name AMBIGUITY is also unrecoverable deterministically: the
    # substring match cannot tell two instances apart, so it must route
    # to the slow path rather than bind to a guessed instance.
    view3c = CompilerObservationView(revision=3, regions=(
        VisibleRegion(surface_label="Calendar",
                      visible_text="发布会议 2026-08-18\n发布会议 2026-08-20",
                      structure_fingerprint="fp-B"),))
    report3c = compiler.needs_slow_path(view3c, VIEW_V1.fingerprints(),
                                        result.handle_evidence)
    assert report3c.needed and "ambiguous" in report3c.reason

    # a lost handle routes too — the fast path must not guess
    view4 = CompilerObservationView(revision=4, regions=(
        VisibleRegion(surface_label="Calendar",
                      visible_text="（会议被删除）\n评审人：王工",
                      structure_fingerprint="fp-A"),))
    report4 = compiler.needs_slow_path(view4, VIEW_V1.fingerprints(),
                                       result.handle_evidence)
    assert report4.needed and "lost" in report4.reason
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
         "after": ["分派一个"], "condition": "batch_assignee == Bob"},
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


# ── scenario 1b: the SEQUENCE primitive from the same one-shot compose ──────
# (frozen contract architect.md §9: "compose 一次成型（三 primitive 各一）" —
#  fan-out has scenario 1, bounded loop has scenario 6; this anchors the
#  third producer: a sequence container + its listed-order single chain)
SEQUENCE_JSON = {
    "variables": [
        {"semantic_key": "release_date", "label": "发布日期",
         "value_type": "date", "mutability": "editable",
         "desired": "2026-08-18"},
        {"semantic_key": "copy_deadline", "label": "文案截止",
         "value_type": "date", "mutability": "editable",
         "desired": "2026-08-18"},
    ],
    "workflow": {"nodes": [
        {"kind": "sequence", "label": "排期推进"},
        {"kind": "action", "label": "改发布日期", "container": "排期推进",
         "semantic_goal": "推迟发布会议",
         "sets": {"release_date": "2026-08-18"},
         "completion": "日历卡片显示 2026-08-18",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["发布会议"]},
        {"kind": "verify", "label": "核对新日期", "container": "排期推进",
         "condition": "release_date == 2026-08-18"},
        {"kind": "action", "label": "同步文案截止", "container": "排期推进",
         "semantic_goal": "文案截止同步",
         "sets": {"copy_deadline": "2026-08-18"},
         "completion": "任务板显示新截止", "reversibility": "reversible",
         "risk": "", "target_evidence": ["项目文案"]},
        {"kind": "terminal", "label": "完成", "after": ["同步文案截止"]},
    ]},
}


def test_scenario_1b_sequence_composes_one_call_ordered_chain():
    """The SEQUENCE primitive comes out of the SAME single compose call:
    the container lands as NodeKind.SEQUENCE, its children chain in the
    LISTED order (deterministic order-fill — no explicit intra-sequence
    edges were supplied), and the architecture closes on one sink
    TERMINAL."""
    port = FakePort(SEQUENCE_JSON)
    ledger = ModelCallLedger()
    arch = TaskArchitect(port, ledger).compose(
        INTENT, (OBSERVED_VARS[0], OBSERVED_VARS[1]))

    assert len(port.calls) == 1, "sequence composition = exactly ONE call"
    assert ledger.total() == 1
    seqs = [n for n in arch.graph.nodes if n.kind is NodeKind.SEQUENCE]
    assert len(seqs) == 1, "the sequence container landed in the graph"
    seq = seqs[0]
    children = [n for n in arch.graph.nodes if n.parent_id == seq.node_id]
    assert [c.label for c in children] == ["改发布日期", "核对新日期",
                                           "同步文案截止"]
    # the listed order IS the chain: every later step waits on its
    # predecessor inside the sequence (2 edges over 3 children)
    for earlier, later in zip(children, children[1:]):
        assert earlier.node_id in later.depends_on, (
            f"sequence children must chain in listed order: "
            f"{earlier.label!r} -> {later.label!r}")
    # valid completion structure: exactly one sink TERMINAL the last
    # sequence step feeds (the validating constructor accepted the whole
    # architecture — re-asserted here as the frozen completion anchor)
    terminals = arch.graph.terminal_nodes()
    assert len(terminals) == 1
    assert children[-1].node_id in terminals[0].depends_on


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
    # C-1 (Oracle audit): the repair message actually sent must ALSO pass
    # the no-leak gate — the offending tokens are never repeated to the
    # model, not even to tell it what went wrong.
    for call in port.calls:
        assert_prompt_clean(call["system"] + "\n" + call["user"],
                            what="state-compiler prompt as sent")

    port2 = FakePort(echo, echo)
    with pytest.raises(CompilerOutputError, match="internal vocabulary"):
        StateCompiler(port2, max_repairs=1).compile(VIEW_V1, INTENT)
    for call in port2.calls:
        assert_prompt_clean(call["system"] + "\n" + call["user"],
                            what="state-compiler prompt as sent")


def test_scenario_7_architect_leak_repair_message_stays_clean():
    """C-1 counterexample (Oracle audit), architect side: after the model
    leaks internal vocabulary, the REPAIR message sent back still passes
    the no-leak gate; the bounded repair can then succeed."""
    leak_echo = dict(ARCHITECTURE_JSON,
                     variables=[{"semantic_key": "entity_id",
                                 "desired": "E1"}])
    port = FakePort(leak_echo, ARCHITECTURE_JSON)
    arch = TaskArchitect(port, max_repairs=1).compose(INTENT, OBSERVED_VARS)
    assert len(port.calls) == 2, "one bounded repair round was consumed"
    assert arch.graph.terminal_nodes(), "the repair landed a valid plan"
    for call in port.calls:
        assert_prompt_clean(call["system"] + "\n" + call["user"],
                            what="task-architect prompt as sent")


def test_scenario_7_domain_error_repair_note_never_carries_internal_ids():
    """C-4 counterexample (Oracle audit, E32): a NORMAL model mistake — a
    fan-out lane waiting on a sibling lane — makes the domain validator
    reject the topology in terms of TaskVM-internal node ids (n001/n002/…).
    The repair note built from that error must carry ONLY a business-level
    category: no internal id may reach the second model message, and the
    gate must agree. The bounded repair can still succeed."""
    sibling_dep = {
        "variables": ARCHITECTURE_JSON["variables"],
        "workflow": {"nodes": [
            {"kind": "action", "label": "改发布日期",
             "semantic_goal": "推迟发布会议",
             "sets": {"release_date": "2026-08-18"},
             "completion": "日历卡片显示 2026-08-18",
             "reversibility": "reversible", "risk": "",
             "target_evidence": ["发布会议"]},
            {"kind": "fan_out", "label": "同步依赖", "after": ["改发布日期"]},
            # INVALID on purpose: lane 同步文案 waits for sibling lane 同步测试
            {"kind": "action", "label": "同步文案", "container": "同步依赖",
             "after": ["同步测试"],
             "semantic_goal": "文案截止同步",
             "sets": {"copy_deadline": "2026-08-18"},
             "completion": "任务板显示新截止", "reversibility": "reversible",
             "risk": "", "target_evidence": ["项目文案"]},
            {"kind": "action", "label": "同步测试", "container": "同步依赖",
             "semantic_goal": "测试截止同步",
             "sets": {"qa_deadline": "2026-08-18"},
             "completion": "任务板显示新截止", "reversibility": "reversible",
             "risk": "", "target_evidence": ["测试任务"]},
            {"kind": "barrier", "label": "汇合校验", "after": ["同步依赖"]},
            {"kind": "terminal", "label": "完成", "after": ["汇合校验"]},
        ]},
    }
    port = FakePort(sibling_dep, ARCHITECTURE_JSON)
    arch = TaskArchitect(port, max_repairs=1).compose(INTENT, OBSERVED_VARS)

    assert len(port.calls) == 2, "the invalid topology consumed one repair"
    second = port.calls[1]["system"] + "\n" + port.calls[1]["user"]
    # the raw domain error quoted internal node ids (n001..n006 from the
    # deterministic next_id minting) — NONE may reach the model, and the
    # no-leak gate must agree on the exact second message as sent
    for tok in ("n001", "n002", "n003", "n004", "n005", "n006"):
        assert tok not in second, f"repair note leaked internal id {tok!r}"
    assert_prompt_clean(second, what="repair message as sent")
    # guidance is still USEFUL: the category (independent lanes) is stated
    assert "independent" in second
    # and the bounded repair landed a valid plan
    assert arch.graph.terminal_nodes()


def test_http_port_one_provider_request_per_ledger_record(monkeypatch):
    """C-2 counterexample (Oracle audit): provider requests == ledger
    records across an invalid-JSON first reply and a successful repair —
    no hidden port-level retry may under-report the true call count."""
    from taskvm.architect.http_port import HttpModelPort
    provider_raw = iter([
        "sorry, plain prose with no JSON object at all",   # invalid reply
        json.dumps(COMPILER_JSON, ensure_ascii=False),     # valid on repair
    ])
    provider_messages: list[str] = []

    def fake_chat(self, system, user, model, max_tokens, temperature,
                  image_data_url):
        provider_messages.append(system + "\n" + user)
        return next(provider_raw)

    monkeypatch.setattr(HttpModelPort, "_chat", fake_chat)
    ledger = ModelCallLedger()
    compiler = StateCompiler(HttpModelPort(), ledger, max_repairs=1)
    result = compiler.compile(VIEW_V1, INTENT)

    assert result.variables[0].semantic_key == "release_date"
    assert len(provider_messages) == 2, "initial + repair, ONE request each"
    assert ledger.total() == 2, "ledger sees every real provider request"
    assert [r.is_repair for r in ledger.records] == [False, True]
    # C-1: the repair message built from the parse-failure note is clean
    assert_prompt_clean(provider_messages[1], what="repair message as sent")


def test_http_port_carries_provider_usage_into_ledger(monkeypatch):
    """C-5 counterexample (Oracle audit, E32): the provider's usage block
    (prompt_tokens/completion_tokens) must survive into ModelReply and the
    ledger record — token accounting is the ledger's frozen contract; the
    port reading usage and then dropping it reports None cost for every
    real call, silently breaking the benchmark's true-overhead metric."""
    import io

    import taskvm.architect.http_port as hp

    payload = {
        "choices": [{"message": {"content": json.dumps(
            COMPILER_JSON, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 123, "completion_tokens": 45},
    }

    class _FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        return _FakeResp(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(hp.urllib.request, "urlopen", fake_urlopen)
    port = hp.HttpModelPort(api_key="test-key")
    ledger = ModelCallLedger()
    result = StateCompiler(port, ledger).compile(VIEW_V1, INTENT)

    assert result.variables[0].semantic_key == "release_date"
    rec = ledger.records[0]
    assert rec.prompt_tokens == 123, (
        "usage.prompt_tokens must reach the ledger through ModelReply")
    assert rec.completion_tokens == 45, (
        "usage.completion_tokens must reach the ledger through ModelReply")
    assert port.last_usage == (123, 45)


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


# ── W0.2 / RFC-A01: schema liberation anchors ─────────────────────
#
# Baseline (eval_results/taskvm_demo_run_20260819): 6/6 real goals died
# at the architect stage, 0 CUA calls. The three rigid rules below are
# the fixed rejection points, locked here as regression anchors.

DEMO_GOAL3_VARS = (
    TaskVariable(semantic_key="message_recipient", label="消息接收人",
                 observed=None, value_type="string"),
    TaskVariable(semantic_key="message_content", label="消息内容",
                 observed=None, value_type="string"),
)

# The VERBATIM workflow shape of the demo goal-3 failure
# (call_012_task_architect.txt — the model's repair round): a sequence
# container, a checkpoint OUTSIDE the container (phantom fork), partial
# intra-sequence 'after' edges, and a trailing TRIGGER action whose
# 'sets' is empty because earlier steps already wrote the variables.
DEMO_GOAL3_JSON = {
    "variables": [
        {"semantic_key": "message_recipient", "label": "消息接收人",
         "value_type": "string", "mutability": "editable",
         "desired": "黄勇"},
        {"semantic_key": "message_content", "label": "消息内容",
         "value_type": "string", "mutability": "editable",
         "desired": "明天上午十点开会"},
    ],
    "workflow": {"nodes": [
        {"kind": "sequence", "label": "发送微信消息流程"},
        {"kind": "action", "label": "打开黄勇聊天",
         "container": "发送微信消息流程", "after": [],
         "semantic_goal": "当前打开的微信聊天对象是黄勇",
         "sets": {"message_recipient": "黄勇"},
         "completion": "聊天页面顶部可见联系人名称“黄勇”",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["黄勇"]},
        {"kind": "action", "label": "填写消息内容",
         "container": "发送微信消息流程", "after": ["打开黄勇聊天"],
         "semantic_goal": "消息输入框中的完整内容为“明天上午十点开会”",
         "sets": {"message_content": "明天上午十点开会"},
         "completion": "消息输入框中可见完整文字“明天上午十点开会”",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["明天上午十点开会"]},
        {"kind": "checkpoint", "label": "发送前确认",
         "after": ["填写消息内容"]},
        {"kind": "action", "label": "发送消息",
         "container": "发送微信消息流程", "after": ["发送前确认"],
         "semantic_goal": "将“明天上午十点开会”发送给黄勇",
         "sets": {},
         "completion": "黄勇聊天页面中出现内容为“明天上午十点开会”的已发送消息气泡",
         "reversibility": "partially_reversible", "risk": ""},
        {"kind": "verify", "label": "核验消息已发送",
         "container": "发送微信消息流程", "after": ["发送消息"],
         "condition": "message_recipient == 黄勇 and message_content ~= 明天上午十点开会"},
        {"kind": "terminal", "label": "消息发送完成"},
    ]},
    "projection": {"root": "微信消息任务卡", "components": [
        {"label": "微信消息任务卡", "type": "card", "binds": None,
         "children": ["接收人字段", "消息内容字段"]},
        {"label": "接收人字段", "type": "field", "binds": "message_recipient",
         "editable": False, "children": []},
        {"label": "消息内容字段", "type": "field", "binds": "message_content",
         "editable": False, "children": []},
    ]},
}


def test_w02_demo_goal3_replay_assembles():
    """The demo baseline's goal-3 model output (semantically correct,
    twice rejected: phantom fork + trigger action with empty 'sets')
    assembles VERBATIM after W0.2 — this is the regression anchor for
    GATE-ARCH (baseline 0/6 → gate ≥5/6)."""
    port = FakePort(DEMO_GOAL3_JSON)
    arch = TaskArchitect(port).compose(
        TaskIntent(goal="给微信里的黄勇发一条消息，内容是：明天上午十点开会"),
        DEMO_GOAL3_VARS)

    assert len(port.calls) == 1, "no repair round needed anymore"
    by_label = {n.label: n for n in arch.graph.nodes}
    # the trigger action keeps its empty desired_state — the governance
    # handle is task-level, not node-level
    assert by_label["发送消息"].contract.desired_state == {}
    # the sequence chain completed in LISTED order …
    seq = next(n for n in arch.graph.nodes
               if n.kind is NodeKind.SEQUENCE)
    children = [n for n in arch.graph.nodes if n.parent_id == seq.node_id]
    assert [c.label for c in children] == ["打开黄勇聊天", "填写消息内容",
                                           "发送消息", "核验消息已发送"]
    for earlier, later in zip(children, children[1:]):
        assert earlier.node_id in later.depends_on
    # … while the EXTERNAL checkpoint dependency is preserved verbatim
    assert by_label["发送前确认"].node_id in \
        by_label["发送消息"].depends_on
    # the validating constructor accepted the whole architecture
    assert len(arch.graph.terminal_nodes()) == 1


def test_w02_navigation_and_observation_actions_need_no_sets():
    """Navigation ('open the bill page') and observation ('read the
    largest expense') steps write no variable: empty 'sets' is legal at
    node level. The plan stays valid because one action DOES write."""
    plan = {
        "variables": [
            {"semantic_key": "largest_expense_amount", "label": "最大支出",
             "value_type": "number", "mutability": "readonly",
             "desired": "最近账单中的最大支出金额"},
        ],
        "workflow": {"nodes": [
            {"kind": "sequence", "label": "查账"},
            {"kind": "action", "label": "打开支付宝账单",
             "container": "查账", "after": [],
             "semantic_goal": "支付宝账单页可见",
             "sets": {}, "completion": "账单页可见",
             "reversibility": "reversible", "risk": ""},
            {"kind": "action", "label": "记录最大支出",
             "container": "查账", "after": ["打开支付宝账单"],
             "semantic_goal": "最大支出金额已知",
             "sets": {"largest_expense_amount": "最近账单中的最大支出金额"},
             "completion": "金额字段已填",
             "reversibility": "reversible", "risk": ""},
            {"kind": "action", "label": "发送汇报",
             "container": "查账", "after": ["记录最大支出"],
             "semantic_goal": "汇报已发出",
             "sets": {}, "completion": "消息已发出",
             "reversibility": "partially_reversible", "risk": ""},
            {"kind": "terminal", "label": "完成", "after": ["查账"]},
        ]},
    }
    port = FakePort(plan)
    arch = TaskArchitect(port).compose(
        TaskIntent(goal="查最大支出并发汇报"),
        (TaskVariable(semantic_key="largest_expense_amount",
                      label="最大支出", observed=None,
                      value_type="number"),))
    by_label = {n.label: n for n in arch.graph.nodes}
    assert by_label["打开支付宝账单"].contract.desired_state == {}
    assert by_label["发送汇报"].contract.desired_state == {}
    assert by_label["记录最大支出"].contract.desired_state == {
        "largest_expense_amount": "最近账单中的最大支出金额"}


def test_w02_all_observation_plan_lacks_governance_handle():
    """A plan where NO action ever writes a variable has no governance
    handle — rejected at TASK level with guidance specific enough to
    repair (and the repair note stays leak-clean)."""
    all_empty = {
        "variables": [
            {"semantic_key": "message_recipient", "label": "接收人",
             "value_type": "string", "mutability": "editable",
             "desired": "黄勇"},
        ],
        "workflow": {"nodes": [
            {"kind": "action", "label": "打开聊天",
             "semantic_goal": "聊天页可见",
             "sets": {}, "completion": "聊天页可见",
             "reversibility": "reversible", "risk": ""},
            {"kind": "terminal", "label": "完成", "after": ["打开聊天"]},
        ]},
    }
    fixed = DEMO_GOAL3_JSON
    port = FakePort(all_empty, fixed)
    arch = TaskArchitect(port, max_repairs=1).compose(
        TaskIntent(goal="给黄勇发消息"), DEMO_GOAL3_VARS)
    assert len(port.calls) == 2, "one bounded repair round"
    second = port.calls[1]["system"] + "\n" + port.calls[1]["user"]
    assert "non-empty 'sets'" in second, (
        "the repair note must tell the model exactly what is missing "
        "(at least one writing action)")
    assert_prompt_clean(second, what="repair message as sent")
    assert len(arch.graph.terminal_nodes()) == 1


def test_w02_contradictory_after_edge_rejected_with_specific_guidance():
    """An explicit intra-sequence 'after' edge that CONTRADICTS the
    listed order is an honest rejection — but the repair note now
    names the actual repair action (execution order / drop the edge /
    fan-out for parallel steps), unlike the old guidance that merely
    restated the rule the model had already followed."""
    contradictory = {
        "variables": [
            {"semantic_key": "release_date", "label": "发布日期",
             "value_type": "date", "mutability": "editable",
             "desired": "2026-08-18"},
        ],
        "workflow": {"nodes": [
            {"kind": "sequence", "label": "排期"},
            {"kind": "action", "label": "改发布日期",
             "container": "排期", "after": ["核对新日期"],
             "semantic_goal": "推迟发布会议",
             "sets": {"release_date": "2026-08-18"},
             "completion": "日历卡片显示 2026-08-18",
             "reversibility": "reversible", "risk": ""},
            {"kind": "verify", "label": "核对新日期",
             "container": "排期", "condition": "release_date == 2026-08-18"},
            {"kind": "terminal", "label": "完成", "after": ["排期"]},
        ]},
    }
    port = FakePort(contradictory, SEQUENCE_JSON)
    arch = TaskArchitect(port, max_repairs=1).compose(
        INTENT, (OBSERVED_VARS[0], OBSERVED_VARS[1]))
    assert len(port.calls) == 2
    second = port.calls[1]["system"] + "\n" + port.calls[1]["user"]
    assert "listed order" in second and "fan-out" in second, (
        "guidance must state the repair action, not just the rule")
    assert_prompt_clean(second, what="repair message as sent")
    assert arch.graph.terminal_nodes()


def test_w02_default_repair_budget_is_four_attempts():
    """RFC-A01: the default bounded-repair budget is now 3 repairs (4
    attempts total) — two attempts starved semantically-correct output
    that merely needed one more round."""
    port = FakePort({"nonsense": True}, {"also": "nonsense"},
                    {"still": "no"}, {"nope": True})
    with pytest.raises(ArchitectOutputError, match="4 attempt"):
        TaskArchitect(port).compose(INTENT, OBSERVED_VARS)
    assert len(port.calls) == 4, "default budget consumed exactly 4 calls"


# ── r8 regression: _chain_fill must not create deadlock cycles ──────────
#
# The r8 test failure (WORLD_WITNESS_MISSING) was caused by _chain_fill
# auto-chaining top-level SEQUENCE and CHECKPOINT nodes that were already
# transitively linked through a FAN_OUT + BARRIER path.  The auto-chain
# added CHECKPOINT → SEQUENCE_CONTAINER, creating a cycle:
#
#   CHECKPOINT → SEQUENCE(all children COMMITTED) → … → BARRIER → FAN_OUT → CHECKPOINT
#
# This deadlocked the scheduler (no_ready_work).  The fix: skip the
# auto-chain when consecutive top-level pairs are already transitively
# connected.

R8_DEADLOCK_JSON = {
    "variables": [
        {"semantic_key": "post_liked", "label": "点赞状态",
         "value_type": "boolean", "mutability": "editable",
         "desired": True},
        {"semantic_key": "post_favorited", "label": "收藏状态",
         "value_type": "boolean", "mutability": "editable",
         "desired": True},
    ],
    "workflow": {"nodes": [
        # SEQUENCE container (top-level)
        {"kind": "sequence", "label": "操作流程"},
        # children of the sequence
        {"kind": "action", "label": "搜索帖子",
         "container": "操作流程",
         "semantic_goal": "找到目标帖子",
         "sets": {},
         "completion": "帖子可见",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["帖子标题"]},
        {"kind": "action", "label": "点赞",
         "container": "操作流程",
         "semantic_goal": "点赞目标帖子",
         "sets": {"post_liked": True},
         "completion": "post_liked == true",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["点赞按钮"]},
        {"kind": "action", "label": "收藏",
         "container": "操作流程",
         "semantic_goal": "收藏目标帖子",
         "sets": {"post_favorited": True},
         "completion": "post_favorited == true",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["收藏按钮"]},
        {"kind": "verify", "label": "核验操作",
         "container": "操作流程",
         "condition": "post_liked == true and post_favorited == true"},
        # FAN_OUT (top-level, depends on the last verify inside the seq)
        {"kind": "fan_out", "label": "并行验证", "after": ["核验操作"]},
        {"kind": "action", "label": "验证点赞",
         "container": "并行验证",
         "semantic_goal": "确认点赞已生效",
         "sets": {},
         "completion": "点赞标记可见",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["点赞标记"]},
        {"kind": "action", "label": "验证收藏",
         "container": "并行验证",
         "semantic_goal": "确认收藏已生效",
         "sets": {},
         "completion": "收藏标记可见",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["收藏标记"]},
        # BARRIER (inside the sequence, fans in the FAN_OUT)
        {"kind": "barrier", "label": "汇合", "container": "操作流程",
         "after": ["并行验证"]},
        # CHECKPOINT (top-level)
        {"kind": "checkpoint", "label": "完成检查点", "after": ["汇合"]},
        {"kind": "terminal", "label": "完成", "after": ["完成检查点"]},
    ]},
    "projection": {"root": "总览", "components": [
        {"label": "总览", "type": "card", "binds": None,
         "children": ["点赞状态", "收藏状态"]},
        {"label": "点赞状态", "type": "field", "binds": "post_liked",
         "editable": False, "children": []},
        {"label": "收藏状态", "type": "field", "binds": "post_favorited",
         "editable": False, "children": []},
    ]},
}


def test_r8_chain_fill_no_deadlock_for_transitively_linked_tops():
    """The r8 regression: a SEQUENCE container and a CHECKPOINT are both
    top-level.  The CHECKPOINT depends on a BARRIER inside the SEQUENCE,
    which depends on a FAN_OUT that is also top-level and depends on a
    VERIFY inside the SEQUENCE.  The old _chain_fill added
    CHECKPOINT → SEQUENCE (auto-chain), creating a deadlock cycle.

    After the fix, _chain_fill detects the transitive connection and
    skips the auto-chain — the graph validates as acyclic and the first
    action node is READY (execution can start)."""
    port = FakePort(R8_DEADLOCK_JSON)
    arch = TaskArchitect(port).compose(
        TaskIntent(goal="搜索帖子并点赞和收藏"),
        (TaskVariable(semantic_key="post_liked", label="点赞状态",
                      observed=False, value_type="boolean"),
         TaskVariable(semantic_key="post_favorited", label="收藏状态",
                      observed=False, value_type="boolean")))

    by_label = {n.label: n for n in arch.graph.nodes}
    seq = by_label["操作流程"]
    cp = by_label["完成检查点"]

    # The CRITICAL assertion: the checkpoint must NOT depend on the
    # sequence container (that would create a deadlock cycle).
    assert seq.node_id not in cp.depends_on, (
        "_chain_fill must not add CHECKPOINT → SEQUENCE_CONTAINER "
        "when they are already transitively linked")

    # The graph must validate (acyclic — checked by WorkflowGraph.__init__)
    assert len(arch.graph.terminal_nodes()) == 1

    # Simulate execution: the first action must be READY
    from taskvm.kernel.workflow_store import WorkflowStore
    store = WorkflowStore()
    store.install_graph(arch.graph, epoch=0)
    snap = store.snapshot()
    ready_ids = {n.node_id for n in snap.graph.ready_nodes(snap.statuses)
                 if snap.statuses.get(n.node_id) == NodeStatus.READY}
    first_action = by_label["搜索帖子"]
    assert first_action.node_id in ready_ids, (
        "The first action in the sequence must be READY — "
        "if _chain_fill created a cycle, no node would be READY")


def test_r8_chain_fill_still_chains_unrelated_tops():
    """Sanity check: when top-level nodes are truly unrelated (no
    transitive path between them), _chain_fill still auto-chains them
    in listed order.  This confirms the fix does not over-suppress."""
    simple_json = {
        "variables": [
            {"semantic_key": "x", "label": "x",
             "value_type": "number", "mutability": "editable",
             "desired": 1},
        ],
        "workflow": {"nodes": [
            {"kind": "action", "label": "step1",
             "semantic_goal": "set x", "sets": {"x": 1},
             "completion": "x == 1",
             "reversibility": "reversible", "risk": "",
             "target_evidence": ["x"]},
            {"kind": "checkpoint", "label": "cp1"},
            {"kind": "terminal", "label": "done", "after": ["cp1"]},
        ]},
        "projection": {"root": "root", "components": [
            {"label": "root", "type": "card", "binds": None,
             "children": ["x_field"]},
            {"label": "x_field", "type": "field", "binds": "x",
             "editable": False, "children": []},
        ]},
    }
    port = FakePort(simple_json)
    arch = TaskArchitect(port).compose(
        TaskIntent(goal="set x to 1"),
        (TaskVariable(semantic_key="x", label="x",
                      observed=0, value_type="number"),))
    by_label = {n.label: n for n in arch.graph.nodes}
    # step1 and cp1 are both top-level and unrelated → auto-chained
    assert by_label["step1"].node_id in by_label["cp1"].depends_on, (
        "Unrelated top-level nodes should still be auto-chained")


# ── r9: free-prose verify conditions are rejected at assembly ─────────────
#
# GATE-G0 r8 root cause: a Chinese free-text condition names no declared
# variable, so VisibleVerifier._referenced_keys grounds NOTHING and the
# check silently degrades to "every desired variable of the whole task" —
# a mid-task verify node then fails until the ENTIRE task is finished and
# the plan deadlocks downstream. The architect now rejects such a
# condition at assembly (bounded repair round fixes it).

FREE_PROSE_VERIFY_JSON = {
    "variables": [
        {"semantic_key": "bill_paid", "label": "账单支付状态",
         "value_type": "boolean", "mutability": "editable",
         "desired": True},
    ],
    "workflow": {"nodes": [
        {"kind": "sequence", "label": "缴费流程"},
        {"kind": "action", "label": "支付账单",
         "container": "缴费流程",
         "semantic_goal": "完成本月账单支付",
         "sets": {"bill_paid": True},
         "completion": "支付成功提示可见",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["支付按钮"]},
        {"kind": "verify", "label": "确认支付",
         "container": "缴费流程", "after": ["支付账单"],
         "condition": "页面上显示支付成功的提示信息"},
        {"kind": "terminal", "label": "完成", "after": ["缴费流程"]},
    ]},
    "projection": {"root": "总览", "components": [
        {"label": "总览", "type": "card", "binds": None,
         "children": ["账单"]},
        {"label": "账单", "type": "field", "binds": "bill_paid",
         "editable": False, "children": []},
    ]},
}


def test_free_prose_verify_condition_is_rejected():
    """A verify condition that names NO declared variable cannot be
    grounded by the deterministic verifier (it degrades to checking the
    whole task's desired state — the r8 deadlock). The architect rejects
    it at assembly with a specific, repairable message."""
    port = FakePort(*([FREE_PROSE_VERIFY_JSON] * 4))
    with pytest.raises(ArchitectOutputError,
                       match="names no declared variable"):
        TaskArchitect(port).compose(
            TaskIntent(goal="支付本月账单"),
            (TaskVariable(semantic_key="bill_paid", label="账单支付状态",
                          observed=False, value_type="boolean"),))


def test_keyed_verify_condition_assembles():
    """The SAME plan with the condition referencing the declared variable
    ('bill_paid == true') assembles fine — the check is about grounding,
    not about prose style."""
    keyed = json.loads(json.dumps(FREE_PROSE_VERIFY_JSON,
                                  ensure_ascii=False))
    keyed["workflow"]["nodes"][2]["condition"] = "bill_paid == true"
    port = FakePort(keyed)
    arch = TaskArchitect(port).compose(
        TaskIntent(goal="支付本月账单"),
        (TaskVariable(semantic_key="bill_paid", label="账单支付状态",
                      observed=False, value_type="boolean"),))
    verify = next(n for n in arch.graph.nodes
                  if n.kind is NodeKind.VERIFY)
    assert verify.verification == "bill_paid == true"
