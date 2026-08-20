"""GATE-G0 r9 receipt-feedback contract tests.

The substrate's ``ActionReceipt`` of the PREVIOUS gesture must reach a
CUA adapter that OPTS IN (duck-typed ``accepts_action_receipt``); the
model otherwise never sees WHY its gesture had no effect and can only
guess at an unchanged screen (r9 postmortem: the 'unknown app' receipt
never reached the model). Adapters WITHOUT the declaration keep the
frozen call surface — the kwarg is never passed, so test fakes stay
signature-compatible on every path.
"""
from __future__ import annotations

from taskvm.architect.port import ModelReply
from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode
from taskvm.runtime import CUADecision, CUADecisionKind
from taskvm.substrate import ActionReceipt, Observation
from taskvm.substrate.port import SurfaceInfo
from taskvm.workspace_ui.composition import HttpCUAModel

from tests.runtime.conftest import (
    CLICK, DONE, FakeSubstrate, ScriptedCUA, action_node, make_kernel,
    make_runtime, type_kv, var,
)


# ── local doubles ──────────────────────────────────────────────────────────
class ReceiptRecordingCUA(ScriptedCUA):
    """A CUA adapter that OPTS IN to receipt feedback and records what
    the runtime handed it on EVERY prediction (None before the first
    gesture, the previous gesture's receipt afterwards)."""

    accepts_action_receipt = True

    def __init__(self, script):
        super().__init__(script)
        self.receipts: list = []

    def predict_action(self, *, goal, observation, labels=None,
                       attempt: int = 1, model=None, last_receipt=None):
        self.receipts.append(last_receipt)
        return super().predict_action(
            goal=goal, observation=observation, labels=labels,
            attempt=attempt, model=model)


class FailingSubstrate(FakeSubstrate):
    """Every gesture lands a FAILED receipt ('unknown app') — the world's
    honest answer; the screen does not change."""

    def act(self, surface, action, *, epoch: str) -> ActionReceipt:
        base = super().act(surface, action, epoch=epoch)
        return ActionReceipt(action=action, status="failed",
                             surface_id=base.surface_id, epoch=epoch,
                             detail="unknown app")


def _graph():
    return WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        action_node("a1", desired={"x": "A"}, parent_id="root"),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("a1", "root")),
    ))


# ── forward autonomy: the receipt loop ─────────────────────────────────────
def test_opted_in_adapter_receives_previous_gesture_receipt():
    """An adapter declaring ``accepts_action_receipt`` sees the previous
    gesture's honest substrate receipt: the FIRST prediction gets None
    (no gesture yet), the SECOND gets the failed receipt of gesture #1
    (status + detail verbatim — the rendered world's factual answer)."""
    k = make_kernel([var("x", "x0", "A")], _graph())
    sub = FailingSubstrate({"app": {"x": "x0"}})
    cua = ReceiptRecordingCUA([type_kv("x", "A"), DONE])
    rt = make_runtime(k, sub, cua)

    rt.run()

    assert len(cua.receipts) == 2, "two predictions: gesture, then done"
    assert cua.receipts[0] is None, "no gesture before the first prediction"
    second = cua.receipts[1]
    assert isinstance(second, ActionReceipt)
    assert second.status == "failed"
    assert second.detail == "unknown app"
    assert second.action.kind == "type"


def test_opted_out_adapter_keeps_frozen_call_surface():
    """An adapter WITHOUT the declaration is NEVER passed the kwarg —
    the ScriptedCUA signature has no ``last_receipt`` parameter, so a
    TypeError here would prove the runtime broke the frozen surface. The
    failing substrate changes nothing: the loop behaves exactly as
    before r9."""
    k = make_kernel([var("x", "x0", "A")], _graph())
    sub = FailingSubstrate({"app": {"x": "x0"}})
    cua = ScriptedCUA([type_kv("x", "A"), DONE])
    rt = make_runtime(k, sub, cua)

    rt.run()

    assert len(cua.calls) == 2, "script consumed normally (no kwarg crash)"
    assert cua.calls[0]["attempt"] == 1 and cua.calls[1]["attempt"] == 2


def test_ok_receipt_also_reaches_adapter():
    """An 'ok' receipt is handed to the opted-in adapter too (transport
    is unconditional; RENDERING is the adapter's policy — the production
    ``HttpCUAModel._receipt_note`` omits clean receipts)."""
    k = make_kernel([var("x", "x0", "A")], _graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})      # ok receipts
    cua = ReceiptRecordingCUA([type_kv("x", "A"), DONE])
    rt = make_runtime(k, sub, cua)

    rt.run()

    second = cua.receipts[1]
    assert isinstance(second, ActionReceipt)
    assert second.status == "ok"


# ── compensation: the same loop on the reverse path ────────────────────────
def test_compensation_loop_feeds_receipt_back():
    """The reverse-GUI compensation loop hands the previous compensation
    gesture's receipt to the opted-in adapter as well (GATE-G0 r8: the
    one CUA call inside the rollback window was a compensation gesture
    flying blind)."""
    from taskvm.domain.patch import CompensationPatch
    from tests.runtime.conftest import status_of

    def _rollback_graph():
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

    k = make_kernel([var("x", "x0", "A"), var("y", "y0", "B")],
                    _rollback_graph())
    sub = FakeSubstrate({"app": {"x": "x0", "y": "y0"}})
    cua = ReceiptRecordingCUA(
        [type_kv("x", "A"), DONE, type_kv("y", "B"), DONE])
    rt = make_runtime(k, sub, cua)
    rt.run(step_budget=3)               # commit a1 + checkpoint(cp) + a2
    assert status_of(k, "a2").value == "committed"

    # rollback to cp: a2 (y: B→y0) enters the compensation plan
    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:cp"))
    assert plan is not None and len(plan.entries) == 1

    # the compensation CUA: a blind CLICK first (changes nothing), then
    # the reverse type that restores y — the SECOND prediction must see
    # the FIRST gesture's receipt
    cua_comp = ReceiptRecordingCUA([CLICK, type_kv("y", "y0")])
    rt._cua = cua_comp
    rt.execute_compensation(plan, surface_id="app")

    # the compensation predictions: first gets None (no gesture yet),
    # the second gets the blind click's honest receipt
    assert cua_comp.receipts[0] is None
    assert isinstance(cua_comp.receipts[1], ActionReceipt)
    assert cua_comp.receipts[1].action.kind == "click"


# ── the production adapter's rendering policy ─────────────────────────────
def _obs(text: str = "屏幕文本") -> Observation:
    return Observation(
        surface=SurfaceInfo(surface_id="app", display_name="app"),
        revision=1, timestamp=0.0, screenshot_ref=None, visible_text=text)


def _receipt(status: str, detail: str = "",
             kind: str = "open") -> ActionReceipt:
    from taskvm.substrate.port import GuiAction
    return ActionReceipt(
        action=GuiAction(kind=kind, target="X"), status=status,
        surface_id="app", epoch="e1", detail=detail)


def test_receipt_note_renders_failure_only():
    """``_receipt_note`` renders ONLY non-ok receipts — a clean 'ok'
    carries no information the fresh screenshot doesn't."""
    note = HttpCUAModel._receipt_note(_receipt("failed", "unknown app"))
    assert "上一次操作回执" in note
    assert "failed" in note and "unknown app" in note
    assert "open" in note, "the gesture kind names what did not take effect"
    assert HttpCUAModel._receipt_note(_receipt("ok")) == ""
    assert HttpCUAModel._receipt_note(None) == ""


def test_predict_action_prompt_carries_receipt():
    """End to end: the failed receipt of the previous gesture lands in
    the user prompt the provider actually receives (the feedback loop's
    whole point — the model SEES why its gesture had no effect)."""
    class _Port:
        default_model = "stub"
        def __init__(self):
            self.users: list[str] = []

        def complete_json(self, *, system, user, model=None,
                          max_tokens=3072, temperature=None,
                          image_data_url=None) -> ModelReply:
            self.users.append(user)
            return ModelReply(parsed={"kind": "act", "action": {
                "kind": "tap", "coordinate": [10, 10]}},
                raw="{}", model="stub")

    port = _Port()
    adapter = HttpCUAModel(port=port)
    adapter.predict_action(
        goal="g", observation=_obs(),
        last_receipt=_receipt("failed", "unknown app", kind="open"))
    assert len(port.users) == 1
    assert "上一次操作回执" in port.users[0]
    assert "unknown app" in port.users[0]
    # … and a clean first call (no receipt) renders no receipt section
    adapter.predict_action(goal="g", observation=_obs())
    assert "上一次操作回执" not in port.users[1]
