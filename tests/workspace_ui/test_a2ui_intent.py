"""A6 free-text intent endpoint + §20.2 model routing + the frozen
governance SSE contract — server-side locks.

Everything runs on a hand-built kernel (zero model calls, zero substrate,
zero network — the same discipline as test_a2ui_transport.py). The model
port is a scripted fake; the ledger is a list. The locks:

  - POST /api/app/a2ui/intent: free text → IntentParser → ONE structured
    governance command through the session's PUBLIC port (local_patch is
    RE-VALIDATED by the ActionRouter — defense in depth; rollback
    resolves the user-visible LABEL, the model never sees ids);
  - the four HONEST paths the handover mandates: clarify 200 (executes
    NOTHING), no parser wired 501, router re-validation 400/403 (nothing
    written), unknown checkpoint label 404;
  - §20.2 routing (exactly as app_open wires it): TASKVM_ROLE_MODELS /
    TASKVM_INTENT_PARSER_MODEL env → resolve_role_models() → the
    IntentParser's model argument → the provider request; one provider
    request = one ledger row (role=intent_parser);
  - the governance SSE contract FROZEN with agentAPP.7: the closed kind
    vocabulary, the kernel/runtime → kind mapping, user-visible LABELS
    only (repo contract §3 — ids never ride a frame), the wire format.
"""
from __future__ import annotations

import json
import time
import types

import pytest

from taskvm.domain import (
    ActionContract, NodeKind, TaskIntent, TaskVariable,
    WorkflowGraph, WorkflowNode,
)
from taskvm.domain.events import Event, EventKind
from taskvm.genui import (
    INTENT_PARSER_MODEL_ROLE, IntentParser, ParsedIntent,
    TaskSurfaceContextBuilder,
)
from taskvm.kernel import TaskVMKernel
from taskvm.projection.store import ProjectionSessionStore
from taskvm.projection.view_models import snapshot_view
from taskvm.runtime.ports import RuntimeEvent, RuntimeEventKind
from taskvm.workspace_ui import serve
from taskvm.workspace_ui.a2ui_transport import (
    GOVERNANCE_SSE_KINDS, A2uiTransport, A2uiTransportError,
    GovernanceEventBridge, register_a2ui_routes,
)
from taskvm.workspace_ui.composition import resolve_role_models


# ── the scripted model port / ledger (the genui test discipline) ────────────


class _Reply:
    def __init__(self, parsed, model="", raw=""):
        self.parsed = parsed
        self.model = model      # "" = provider did not report one → the
        #                         ledger row falls back to the REQUESTED
        #                         model id (honest accounting either way)
        self.raw = raw
        self.prompt_tokens = 111
        self.completion_tokens = 7


class ScriptedPort:
    """Returns the scripted replies in order; records every call."""

    def __init__(self, *replies):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def complete_json(self, *, system, user, model=None, max_tokens=1024,
                      temperature=None, image_data_url=None):
        self.calls.append({"system": system, "user": user, "model": model,
                           "max_tokens": max_tokens,
                           "temperature": temperature})
        return self._replies.pop(0)


class ListLedger:
    def __init__(self):
        self.records: list = []

    def record(self, rec):
        self.records.append(rec)


class FakeParser:
    """Returns a hand-built ParsedIntent verbatim — used to prove the
    TRANSPORT's own defenses (the router re-validation, the checkpoint
    label resolution) fire even when a parser already "approved" the
    intent (rule-set drift / a rogue parser must never reach the
    kernel unvalidated)."""

    def __init__(self, intent: ParsedIntent):
        self._intent = intent

    def parse(self, text, context):       # the IntentParser duck shape
        return self._intent


# ── the hand-built stack ────────────────────────────────────────────────────


def _make_kernel(sid: str = "s1") -> TaskVMKernel:
    intent = TaskIntent(goal="发布产品")
    kernel = TaskVMKernel(sid, intent)
    kernel.init_task_state([
        TaskVariable(semantic_key="release_note", label="发布备注",
                     observed="v1", desired="v1", value_type="string"),
        TaskVariable(semantic_key="budget", label="预算",
                     observed=2000, desired=2000, value_type="number",
                     mutability="readonly"),
    ])
    kernel.set_plan(WorkflowGraph(nodes=(
        WorkflowNode(node_id="seq1", kind=NodeKind.SEQUENCE, label="发布流程"),
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="写发布备注",
                     parent_id="seq1",
                     contract=ActionContract(
                         contract_id="c1",
                         semantic_goal="set release_note",
                         desired_state={"release_note": "v1"},
                         completion_condition="release_note shows v1")),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a1",)),
    )))
    return kernel


@pytest.fixture()
def stack():
    store = ProjectionSessionStore()
    kernel = _make_kernel("s1")
    store.register("s1", kernel)
    transport = A2uiTransport(session_lookup=store.get)
    state = types.SimpleNamespace(sid="s1")
    app = serve(store)
    register_a2ui_routes(app, transport, store, state)
    return types.SimpleNamespace(
        app=app, store=store, transport=transport, kernel=kernel,
        client=app.test_client())


def _context(stack):
    sess = stack.store.get("s1")
    return TaskSurfaceContextBuilder().build(snapshot_view(sess))


def _desired_of(sess, key):
    for v in snapshot_view(sess)["variables"]:
        if v["key"] == key:
            return v["desired"]
    raise AssertionError(f"variable {key!r} not in snapshot")


def _post_intent(stack, text, parser):
    stack.transport.set_intent_parser(parser)
    return stack.client.post("/api/app/a2ui/intent",
                             json={"text": text})


def _gov_frames(stack):
    return [ev for _, ev in stack.transport.governance_after("s1", 0)]


# ── the four HONEST paths (handover acceptance) ─────────────────────────────


def test_intent_without_parser_is_honest_501(stack):
    resp = stack.client.post("/api/app/a2ui/intent",
                             json={"text": "把发布备注改成 v2"})
    assert resp.status_code == 501
    body = resp.get_json()
    assert body["ok"] is False
    assert "not configured" in body["error"]


def test_intent_clarify_is_200_and_executes_nothing(stack):
    """A clarify reply is a 200 (the parse SUCCEEDED — it honestly
    concluded "ask the user") and executes NOTHING: no governance write,
    no desired move, no ledger fabrication."""
    port = ScriptedPort(_Reply({"kind": "clarify",
                                "question": "你想把备注改成什么？"}))
    ledger = ListLedger()
    parser = IntentParser(port, ledger)
    resp = _post_intent(stack, "帮我改一下备注", parser)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["kind"] == "clarify"
    assert body["question"] == "你想把备注改成什么？"
    assert body["intent"]["source"] == "model"
    # NOTHING was written — the kernel's desired never moved
    sess = stack.store.get("s1")
    assert _desired_of(sess, "release_note") == "v1"
    # the parse itself was one honest provider request = one row
    assert len(ledger.records) == 1
    assert ledger.records[0].role == INTENT_PARSER_MODEL_ROLE


@pytest.mark.parametrize("updates,status,fragment", [
    # readonly variable → the router's 403 (ownership)
    ({"budget": 999999}, 403, "readonly"),
    # unknown semantic key → the router's 400
    ({"ghost": "x"}, 400, "unknown semantic key"),
])
def test_intent_router_revalidation_rejects_unwritable(stack, updates,
                                                        status, fragment):
    """The LAST enforcement point: even a parser that already approved
    the pairs gets them re-validated by the ActionRouter against the
    SAME rule set — nothing reaches the kernel unvalidated, and a mixed
    intent writes NOTHING (all-or-nothing)."""
    sess = stack.store.get("s1")
    parser = FakeParser(ParsedIntent(kind="local_patch", updates=updates,
                                     rationale=" rogue parser output"))
    resp = _post_intent(stack, "改一下", parser)
    assert resp.status_code == status
    body = resp.get_json()
    assert body["ok"] is False
    assert fragment in body["error"]
    # NOTHING was written — both variables stand
    assert _desired_of(sess, "release_note") == "v1"
    assert _desired_of(sess, "budget") == 2000


def test_intent_unknown_checkpoint_label_is_404(stack):
    parser = FakeParser(ParsedIntent(kind="rollback",
                                     checkpoint_label="不存在的点"))
    resp = _post_intent(stack, "回到不存在的点", parser)
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["ok"] is False
    assert "no checkpoint labelled" in body["error"]
    assert "不存在的点" in body["error"]


# ── the executable kinds land through the PUBLIC port ──────────────────────


def test_intent_local_patch_lands_exactly_one_governance_write(stack):
    port = ScriptedPort(_Reply({"kind": "local_patch",
                                "updates": {"release_note": "v2"},
                                "rationale": "用户说改成 v2"}))
    ledger = ListLedger()
    parser = IntentParser(port, ledger)
    resp = _post_intent(stack, "把发布备注改成 v2", parser)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["kind"] == "local_patch"
    assert body["intent"]["updates"] == {"release_note": "v2"}
    # the kernel's desired ACTUALLY moved (the only real write)
    sess = stack.store.get("s1")
    assert _desired_of(sess, "release_note") == "v2"
    # one provider request = one ledger row, honestly accounted
    assert len(ledger.records) == 1
    assert ledger.records[0].role == INTENT_PARSER_MODEL_ROLE
    assert ledger.records[0].ok is True


def test_intent_goal_patch_lands_on_the_kernel(stack):
    port = ScriptedPort(_Reply({
        "kind": "goal_patch", "goal": "先把通知发出去再说",
        "constraints": ["不改预算"], "rationale": "优先级调整"}))
    parser = IntentParser(port, ListLedger())
    resp = _post_intent(stack, "目标改成先发通知", parser)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["kind"] == "goal_patch"
    sess = stack.store.get("s1")
    assert sess.kernel.task_state().intent.goal == "先把通知发出去再说"


def test_intent_checkpoint_lands_and_emits_governance_sse(stack):
    """checkpoint through the intent path is the SAME governance write
    the fixed shell's route runs — and the SAME landing signal the
    bridge mirrors onto the governance SSE ring."""
    sess = stack.store.get("s1")
    bridge = GovernanceEventBridge(stack.transport, "s1", sess)
    assert bridge.scan_once() == 0            # fresh: nothing landed yet
    port = ScriptedPort(_Reply({"kind": "checkpoint",
                                "checkpoint_label": "改备注前"}))
    parser = IntentParser(port, ListLedger())
    resp = _post_intent(stack, "先存个检查点", parser)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["kind"] == "checkpoint"
    labels = [c.label for c in sess.kernel.checkpoints()]
    assert "改备注前" in labels
    # the bridge mirrors the landing (driven synchronously here)
    assert bridge.scan_once() == 1
    frames = _gov_frames(stack)
    assert [f["kind"] for f in frames] == ["checkpoint_added"]
    assert frames[0]["label"] == "改备注前"
    # repo contract §3: the checkpoint id never rides the frame
    ckpt_id = sess.kernel.checkpoints()[0].checkpoint_id
    assert ckpt_id not in json.dumps(frames)


def test_intent_rollback_lands_by_user_visible_label(stack):
    """rollback resolves the LABEL the model produced into the kernel's
    checkpoint id HERE (the model never sees ids); the compensation plan
    is honestly pending without a driver (never a fake success)."""
    sess = stack.store.get("s1")
    sess.governance_port().checkpoint("存档点")     # a real checkpoint
    bridge = GovernanceEventBridge(stack.transport, "s1", sess)
    assert bridge.scan_once() == 0            # the pre-existing landing
    #                                          is state, not a replay
    parser = FakeParser(ParsedIntent(kind="rollback",
                                     checkpoint_label="存档点",
                                     rationale="回到存档点"))
    resp = _post_intent(stack, "回到存档点", parser)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["kind"] == "rollback"
    # the plan object never leaks into the HTTP response
    assert "plan" not in body["result"]
    assert body["result"]["disposition"] == "pending"
    # the bridge announces it with the user-visible label only
    assert bridge.scan_once() == 1
    frames = _gov_frames(stack)
    assert [f["kind"] for f in frames] == ["rollback"]
    assert frames[0]["label"] == "存档点"


def test_intent_kernel_refusal_rides_honest_status_not_500(stack):
    """A kernel-level refusal (here: a goal_patch left the kernel
    awaiting recompose, so a checkpoint would cross an unstable
    boundary) rides the frozen route-matrix statuses — 409, with the
    kernel's own reason verbatim — NEVER a 500."""
    sess = stack.store.get("s1")
    # a goal_patch (kernel-only port on the hand-built stack) blocks
    # execution pending recompose
    sess.governance_port().goal_patch(goal="先发通知", rationale="调整")
    parser = FakeParser(ParsedIntent(kind="checkpoint",
                                     checkpoint_label="被阻塞的点"))
    resp = _post_intent(stack, "存个检查点", parser)
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False
    assert "ValidationError" in body["error"]
    # NOTHING was written — no checkpoint landed
    assert sess.kernel.checkpoints() == []


def test_intent_requires_live_session(stack):
    import flask
    empty_store = ProjectionSessionStore()
    empty_transport = A2uiTransport()
    app = flask.Flask(__name__)
    register_a2ui_routes(app, empty_transport, empty_store,
                         types.SimpleNamespace(sid="s1"))
    resp = app.test_client().post("/api/app/a2ui/intent",
                                  json={"text": "改一下"})
    assert resp.status_code == 404
    assert "no active session" in resp.get_json()["error"]


def test_intent_rejects_malformed_text(stack):
    for payload in ({}, {"text": ""}, {"text": "   "},
                    {"text": 123}):
        resp = stack.client.post("/api/app/a2ui/intent", json=payload)
        assert resp.status_code == 400
        assert "text must be a non-empty string" in \
            resp.get_json()["error"]


# ── §20.2 model routing: env → IntentParser model → the request ────────────


def test_role_models_env_routes_the_intent_parser_model(stack, monkeypatch):
    """The §20.2 chain EXACTLY as app_open wires it: the
    TASKVM_ROLE_MODELS slot → resolve_role_models() → the constructor
    argument → the provider request + the ledger row's model field."""
    monkeypatch.setenv("TASKVM_ROLE_MODELS", "intent_parser=small-fast")
    assert resolve_role_models().get("intent_parser") == "small-fast"
    port = ScriptedPort(_Reply({"kind": "clarify", "question": "?"}))
    ledger = ListLedger()
    parser = IntentParser(port, ledger,
                          model=resolve_role_models().get("intent_parser"))
    parser.parse("随便", _context(stack))
    assert port.calls[0]["model"] == "small-fast"
    assert len(ledger.records) == 1
    assert ledger.records[0].role == INTENT_PARSER_MODEL_ROLE
    # the provider did not report a model name → the row falls back to
    # the REQUESTED id: the routing choice lands in the accounting
    assert ledger.records[0].model == "small-fast"


def test_intent_parser_env_direct_routes_when_slot_empty(stack, monkeypatch):
    """Without a TASKVM_ROLE_MODELS slot the constructor model is None
    and the parser's OWN env fallback (TASKVM_INTENT_PARSER_MODEL)
    decides — the priority chain the protocol pins."""
    monkeypatch.delenv("TASKVM_ROLE_MODELS", raising=False)
    monkeypatch.setenv("TASKVM_INTENT_PARSER_MODEL", "env-direct")
    assert resolve_role_models().get("intent_parser") is None
    port = ScriptedPort(_Reply({"kind": "clarify", "question": "?"}))
    IntentParser(port, ListLedger(),
                 model=resolve_role_models().get("intent_parser")
                 ).parse("x", _context(stack))
    assert port.calls[0]["model"] == "env-direct"


def test_ledger_rows_equal_provider_requests(stack):
    """The shared-ledger discipline at the transport grain: N provider
    requests (parse + bounded repair) = exactly N rows, every row
    role=intent_parser, honest ok/repaired accounting."""
    port = ScriptedPort(
        _Reply({"kind": "magic"}),                     # round 1 rejected
        _Reply({"kind": "local_patch",                 # repair round ok
                "updates": {"release_note": "v9"}, "rationale": "r"}),
    )
    ledger = ListLedger()
    parser = IntentParser(port, ledger)
    result = parser.parse("把备注改成 v9", _context(stack))
    assert result.kind == "local_patch"
    assert len(port.calls) == 2
    assert len(ledger.records) == 2          # one row per request
    roles = {r.role for r in ledger.records}
    purposes = [r.purpose for r in ledger.records]
    assert roles == {INTENT_PARSER_MODEL_ROLE}
    assert purposes == ["intent_parse", "intent_repair"]
    assert ledger.records[0].ok and ledger.records[1].ok


# ── the frozen governance SSE contract (agentAPP.7) ─────────────────────────


def test_push_governance_rejects_unknown_kind(stack):
    """The kind vocabulary is FROZEN — an unknown kind raises honestly
    instead of being silently dropped or renamed (contract changes are
    issue tickets, never private edits)."""
    with pytest.raises(ValueError, match="frozen"):
        stack.transport.push_governance("s1", "goal_patched",
                                        label="x")
    assert _gov_frames(stack) == []


def test_governance_sse_pause_resume_stop_from_kernel_events(stack):
    """pause/resume/stop land on the kernel log regardless of the entry
    (the fixed shell's routes, the driver, …) — the bridge maps each
    GOVERNANCE_REQUESTED onto its contract kind."""
    sess = stack.store.get("s1")
    bridge = GovernanceEventBridge(stack.transport, "s1", sess)
    assert bridge.scan_once() == 0            # no replay of history
    sess.kernel.request_governance("pause", "soft pause requested")
    sess.kernel.request_governance("resume")
    sess.kernel.request_governance("stop", "user pressed stop")
    assert bridge.scan_once() == 3
    frames = _gov_frames(stack)
    assert [f["kind"] for f in frames] == ["pause", "resume", "stop"]
    for f in frames:
        assert f["type"] == "governance"
        assert isinstance(f["rev"], int) and isinstance(f["ts"], int)
        assert isinstance(f["detail"], dict)
    assert frames[0]["detail"]["action"] == "pause"


def test_governance_sse_bridge_starts_at_now_no_history_replay(stack):
    """Pre-surface history is STATE (the island reads it from the data
    model), not events to replay — a fresh bridge announces nothing."""
    sess = stack.store.get("s1")
    sess.kernel.request_governance("pause")   # predates the surface
    bridge = GovernanceEventBridge(stack.transport, "s1", sess)
    assert bridge.scan_once() == 0
    assert _gov_frames(stack) == []


def test_governance_sse_node_verdicts_from_runtime_events(stack):
    """node_verified / node_failed mirror the runtime's ACTION_LANDED
    verdicts, addressed by the node's USER-VISIBLE label (the node id
    never rides the frame — repo contract §3)."""
    sess = stack.store.get("s1")
    bridge = GovernanceEventBridge(stack.transport, "s1", sess)
    sess.runtime = types.SimpleNamespace(runtime_events=lambda: (
        RuntimeEvent(kind=RuntimeEventKind.ACTION_LANDED, epoch=1,
                     node_id="a1", detail="verified"),
        RuntimeEvent(kind=RuntimeEventKind.ACTION_LANDED, epoch=1,
                     node_id="a1", detail="verify-failed"),
        RuntimeEvent(kind=RuntimeEventKind.ACTION_LANDED, epoch=1,
                     node_id="a1", detail="something else"),
    ))
    assert bridge.scan_once() == 2            # the third carries no kind
    frames = _gov_frames(stack)
    assert [f["kind"] for f in frames] == ["node_verified", "node_failed"]
    assert all(f["label"] == "写发布备注" for f in frames)
    assert "a1" not in json.dumps(frames)     # the node id never rides


def test_governance_sse_final_fail_from_runtime_node_failed(stack):
    sess = stack.store.get("s1")
    bridge = GovernanceEventBridge(stack.transport, "s1", sess)
    sess.runtime = types.SimpleNamespace(runtime_events=lambda: (
        RuntimeEvent(kind=RuntimeEventKind.NODE_FAILED, epoch=2,
                     node_id="a1", detail="repair budget spent"),
    ))
    assert bridge.scan_once() == 1
    frames = _gov_frames(stack)
    assert frames[0]["kind"] == "final_fail"
    assert frames[0]["label"] == "写发布备注"
    assert frames[0]["detail"] == {"reason": "repair budget spent"}


def test_governance_sse_final_pass_on_terminal_commit(stack):
    """The mapping unit: a TERMINAL node committing is the task-level
    final_pass signal (the fan-in confirmation — the goal is reached)."""
    sess = stack.store.get("s1")
    bridge = GovernanceEventBridge(stack.transport, "s1", sess)
    ev = Event(event_id="evt:99999", session_id="s1",
               kind=EventKind.NODE_COMMITTED, revision=7, epoch=3,
               timestamp=time.time(),
               payload={"node_id": "t1", "kind": "terminal"})
    frames = bridge._kernel_frames(ev, {}, {"t1": "完成"})
    assert frames == [{"kind": "final_pass", "label": "完成",
                       "rev": 7, "detail": {}}]
    # non-terminal control commits carry no island kind
    ev2 = Event(event_id="evt:99998", session_id="s1",
                kind=EventKind.NODE_COMMITTED, revision=7, epoch=3,
                timestamp=time.time(),
                payload={"node_id": "seq1", "kind": "sequence"})
    assert bridge._kernel_frames(ev2, {}, {"seq1": "发布流程"}) == []


def test_governance_sse_checkpoint_reached_from_compensation(stack):
    """The mapping unit: COMPENSATION_APPLIED (reality is back AT the
    checkpoint) → checkpoint_reached, labelled by the plan's target —
    the label was resolved when the plan was REQUESTED (the applied
    event only names the plan)."""
    sess = stack.store.get("s1")
    bridge = GovernanceEventBridge(stack.transport, "s1", sess)
    bridge._plan_labels["comp:00001"] = "存档点"
    ev = Event(event_id="evt:99997", session_id="s1",
               kind=EventKind.COMPENSATION_APPLIED, revision=5, epoch=2,
               timestamp=time.time(), correlation_id="comp:00001",
               payload={"disposition": "complete"})
    frames = bridge._kernel_frames(ev, {}, {})
    assert frames == [{"kind": "checkpoint_reached", "label": "存档点",
                       "rev": 5, "detail": {"disposition": "complete"}}]


def test_governance_sse_wire_frame_format(stack):
    """The wire contract verbatim: named event `governance`, the payload
    carries type/kind/label/rev/ts/detail — nothing else."""
    stack.transport.push_governance("s1", "pause",
                                    detail={"action": "pause"})
    with stack.app.test_request_context("/api/app/a2ui/sse?after=0"):
        resp = stack.app.view_functions["a2ui_sse"]()
    frame = next(resp.response)
    chunks = [c for c in frame.split("\n\n") if c]
    gov = [c for c in chunks if c.startswith("event: governance\n")]
    assert len(gov) == 1
    payload = json.loads(gov[0][len("event: governance\ndata: "):])
    assert payload == {"type": "governance", "kind": "pause", "label": "",
                       "rev": 0, "ts": payload["ts"],
                       "detail": {"action": "pause"}}
    assert isinstance(payload["ts"], int)


def test_governance_sse_kind_vocabulary_is_frozen():
    """The contract vocabulary pinned byte-for-byte (agentAPP.7 frozen
    2026-08-20 — any change is an issue ticket, not a private edit)."""
    assert GOVERNANCE_SSE_KINDS == frozenset({
        "checkpoint_added", "checkpoint_reached", "rollback", "pause",
        "resume", "stop", "node_verified", "node_failed",
        "final_pass", "final_fail",
    })


def test_attach_starts_bridge_and_drop_stops_it(stack):
    """The bridge lifecycle mirrors the data poller's: attach starts it,
    a session drop retires it (a new goal gets a fresh bridge against
    the new session's event logs)."""
    sess = stack.store.get("s1")
    stack.transport.attach_session("s1", sess)
    bridge = stack.transport._gov_bridges["s1"]
    assert bridge.is_alive()
    stack.transport.drop_session("s1")
    assert bridge._stop_evt.is_set()
    assert "s1" not in stack.transport._gov_bridges
