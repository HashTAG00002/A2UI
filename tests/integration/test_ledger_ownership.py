"""A-13 single-owner ledger — C-2 invariant: 1 provider request = 1 ledger row.

Pre-fix reality (DSA audit): the transport adapter (HttpCUAModel) recorded
a row per request AND the runtime recorded a SECOND row per decision on the
SAME shared ledger — provider requests=2, ledger rows=4. Token costs and
model-overhead metrics doubled silently.

Post-fix ownership (A-13):
  * the ADAPTER (composition ``HttpCUAModel``, ``records_own_ledger=True``)
    mints a unique ``request_id`` per REAL provider request and lands
    exactly one row on every path: success / transport exception /
    unparseable reply;
  * the RUNTIME never appends its own row for that adapter — it ANNOTATES
    the adapter's row (node / attempt / repair context) through the
    decision's ``request_id``; rows are never created or dropped by
    annotation;
  * a pre-flight harness bug (prompt leak) issues NO request and lands NO
    row — rows count provider requests, not harness failures.

The acceptance invariant under test, per the RM-0 work order:

    provider_stub.request_count == ledger.total()

across the five situations: success / timeout / illegal JSON / repair /
temperature downgrade.
"""
from __future__ import annotations

import pytest

from taskvm.architect import ModelCallLedger, ModelReply
from taskvm.architect.port import ModelCallRecord
from taskvm.runtime import AutonomyRuntime
from taskvm.runtime.ports import MODEL_ROLE_CUA
from taskvm.substrate import Observation
from taskvm.verifier.visible import VisibleVerifier
from taskvm.workspace_ui.composition import CUAReplySchemaError, HttpCUAModel

from tests.runtime.conftest import (
    FakeExtractor, FakeSerializer, FakeSubstrate, action_node, make_kernel,
    status_of, var,
)


ACT_TOPIC_NEW = {"kind": "act",
                 "action": {"kind": "type", "text": "topic=new"}}
ACT_TOPIC_WRONG = {"kind": "act",
                   "action": {"kind": "type", "text": "topic=wrong"}}


class StubModelPort:
    """Deterministic HttpModelPort stand-in: ONE provider request per
    ``complete_json`` (the real port's C-2 discipline), scripted replies,
    honest request counter."""

    default_model = "stub-model"

    def __init__(self, script: list | None = None):
        self.script = list(script or [])
        self.request_count = 0
        self.last_temperature: float | None = None

    def complete_json(self, *, system: str, user: str,
                      model: str | None = None, max_tokens: int = 3072,
                      temperature: float | None = None,
                      image_data_url: str | None = None) -> ModelReply:
        self.request_count += 1
        self.last_temperature = temperature
        item = self.script.pop(0) if self.script else None
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):                       # raw unparseable text
            return ModelReply(parsed=None, raw=item, model=model or "stub")
        return ModelReply(parsed=item, raw=str(item), model=model or "stub",
                          prompt_tokens=11, completion_tokens=7)


def _obs(text: str = "topic=old") -> Observation:
    from taskvm.substrate import SurfaceInfo
    return Observation(surface=SurfaceInfo(surface_id="app", display_name="app"),
                       revision=1, timestamp=0.0, screenshot_ref="shot://app",
                       visible_text=text, fingerprint="fp")


# ── situation 1: success — one request, one row, request_id round-trip ──────
def test_success_one_provider_request_is_exactly_one_row():
    stub, ledger = StubModelPort([ACT_TOPIC_NEW]), ModelCallLedger()
    cua = HttpCUAModel(port=stub, ledger=ledger)

    decision = cua.predict_action(goal="set topic=new", observation=_obs())

    assert decision.action is not None and decision.action.kind == "type"
    assert stub.request_count == 1
    assert ledger.total() == 1                      # THE invariant
    row = ledger.records[0]
    assert row.request_id == decision.request_id    # decision → row link
    assert row.ok is True and row.role == MODEL_ROLE_CUA
    assert (row.prompt_tokens, row.completion_tokens) == (11, 7)


# ── situation 2: transport timeout — row landed, exception propagates ───────
def test_timeout_lands_one_row_and_propagates():
    stub = StubModelPort([TimeoutError("gateway timed out")])
    ledger = ModelCallLedger()
    cua = HttpCUAModel(port=stub, ledger=ledger)

    with pytest.raises(TimeoutError):
        cua.predict_action(goal="set topic=new", observation=_obs())

    assert stub.request_count == 1
    assert ledger.total() == 1                      # row for the failed request
    row = ledger.records[0]
    assert row.ok is False and "timed out" in row.error


# ── situation 3: illegal JSON — one row, schema error propagates ──────────
def test_illegal_json_reply_is_one_row_and_raises():
    """An unparseable reply is an INVALID PREDICTION: the row lands
    FIRST (one provider request = one row, on every path — same as the
    transport exception), then ``CUAReplySchemaError`` propagates so
    the runtime's §5 loop owns the bounded re-ask."""
    stub = StubModelPort(["模型说的不是 JSON <<<"])
    ledger = ModelCallLedger()
    cua = HttpCUAModel(port=stub, ledger=ledger)

    with pytest.raises(CUAReplySchemaError):
        cua.predict_action(goal="set topic=new", observation=_obs())

    assert stub.request_count == 1
    assert ledger.total() == 1
    assert ledger.records[0].ok is False
    assert ledger.records[0].request_id     # the row is still linkable


# ── situation 4: runtime repair loop — every re-ask is its own row ──────────
def test_runtime_repair_never_double_counts_requests():
    """Full runtime over the REAL adapter: wrong first gesture → verify
    fails → ONE repair (budget) → correct gesture → committed. The runtime
    must ANNOTATE the adapter's rows, never append its own: after the run
    ledger.total() == stub.request_count == 4 (act, done, act, done)."""
    from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode

    k = make_kernel(
        [var("topic", "old", "new")],
        WorkflowGraph(nodes=(
            WorkflowNode("root", NodeKind.SEQUENCE, "task"),
            action_node("a1", desired={"topic": "new"}),
            WorkflowNode("term", NodeKind.TERMINAL, "done",
                         parent_id="root", depends_on=("a1",)),
        )))
    sub = FakeSubstrate({"app": {"topic": "old"}})
    stub = StubModelPort([ACT_TOPIC_WRONG, {"kind": "done"},
                          ACT_TOPIC_NEW, {"kind": "done"}])
    ledger = ModelCallLedger()
    cua = HttpCUAModel(port=stub, ledger=ledger)
    rt = AutonomyRuntime(
        k, sub, cua_model=cua, serializer=FakeSerializer(),
        extractor=FakeExtractor(), verifier=VisibleVerifier(), ledger=ledger)

    rt.run(step_budget=4)

    assert status_of(k, "a1").value == "committed"
    assert sub.world["app"]["topic"] == "new"
    # THE invariant — the runtime added ZERO rows of its own
    assert stub.request_count == 4
    assert ledger.total() == 4
    # every CUA row carries the adapter's request_id + runtime annotation
    rows = ledger.records
    assert all(r.request_id for r in rows)
    assert all(r.node_id == "a1" for r in rows)     # annotated, not duplicated
    assert [r.is_repair for r in rows] == [False, False, True, True]
    assert len({r.request_id for r in rows}) == 4   # ids are unique


# ── situation 5: temperature downgrade — each attempt is its own row ────────
def test_temperature_downgrade_attempts_are_separate_rows():
    """The downgrade policy (B-02) re-asks with a lower temperature: every
    real re-ask is a fresh provider request and MUST land its own row —
    the accounting cannot collapse the two attempts into one."""
    stub = StubModelPort([TimeoutError("temperature not supported"),
                          ACT_TOPIC_NEW])
    ledger = ModelCallLedger()
    cua = HttpCUAModel(port=stub, ledger=ledger)

    with pytest.raises(TimeoutError):               # attempt 1: rejected
        cua.predict_action(goal="set topic=new", observation=_obs(),
                           attempt=1)
    decision = cua.predict_action(goal="set topic=new", observation=_obs(),
                                  attempt=2)        # attempt 2: downgraded re-ask

    assert decision.action is not None
    assert stub.request_count == 2
    assert ledger.total() == 2                      # both attempts accounted
    assert len({r.request_id for r in ledger.records}) == 2


# ── ledger guards: annotation can never create or drop rows ─────────────────
def test_ledger_annotate_and_duplicate_guards():
    ledger = ModelCallLedger()
    ledger.record(ModelCallRecord(role=MODEL_ROLE_CUA, purpose="first",
                                  model="m", ok=True, request_id="req-1"))

    # annotate attaches context IN PLACE — still one row
    annotated = ledger.annotate("req-1", node_id="a1", attempt=2,
                                is_repair=True)
    assert annotated is not None and annotated.node_id == "a1"
    assert ledger.total() == 1 and ledger.records[0].attempt == 2

    # unknown request_id: honest no-op, never a new row
    assert ledger.annotate("req-unknown", node_id="a1") is None
    assert ledger.total() == 1

    # non-annotatable field: rejected loudly (a typo must not corrupt rows)
    with pytest.raises(ValueError):
        ledger.annotate("req-1", ok=False, model="other")

    # duplicate request_id: refused — one request, one row, ever
    with pytest.raises(ValueError):
        ledger.record(ModelCallRecord(role=MODEL_ROLE_CUA, purpose="dup",
                                      model="m", ok=True, request_id="req-1"))


# ── pre-flight leak: no provider request, no ledger row ─────────────────────
def test_prompt_leak_issues_no_request_and_lands_no_row():
    """The no-leak gate runs BEFORE the request: a harness bug must not be
    billed as a provider call (rows count real requests only)."""
    stub = StubModelPort([])
    ledger = ModelCallLedger()
    cua = HttpCUAModel(port=stub, ledger=ledger)

    from unittest.mock import patch
    from taskvm.workspace_ui.composition import assert_prompt_clean \
        as _gate
    with patch("taskvm.workspace_ui.composition.assert_prompt_clean",
               side_effect=ValueError("leaked: entity_id=42")):
        decision = cua.predict_action(goal="x", observation=_obs())

    assert decision.kind.value == "fail"
    assert decision.request_id == ""               # no request was made
    assert stub.request_count == 0
    assert ledger.total() == 0
