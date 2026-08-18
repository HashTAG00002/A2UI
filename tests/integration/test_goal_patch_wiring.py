"""GoalPatch production wiring — the closure the frozen contracts mandate.

projection.md §1 (ownership table): "GoalPatch recomposition
(architect-owned, **injected**)" + architect contract §5 model-call table:
GoalPatchRequested → 0 compiler / 1 architect call. The chain, verbatim:

    POST /governance/goal_patch   (the PUBLIC projection route)
      → GovernanceServicePort.goal_patch
      → GovernanceService.handle(GoalPatchRequested)
      → kernel.apply_goal_patch   (invalidate + block)
      → ONE architect.recompose_future
      → kernel.recompose          (atomic close + unblock)

Pre-fix (audit 2026-08-19): ``bootstrap_real_full`` registered the session
WITHOUT ``governance=``, so the route fell back to ``KernelGovernancePort``
whose ``goal_patch`` only applied the patch — ``pending_recompose`` stayed
set, every later ``start`` returned 409 forever, and no retry path was
wired into the production bundle. These tests pin the CLOSED chain over
the real bootstrap + the real Flask routes (scripted provider ports prove
CONTRACT WIRING, never model quality — same convention as B-07).
"""
from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest

from taskvm.architect import (
    MODEL_ROLE_CUA, MODEL_ROLE_STATE_COMPILER, MODEL_ROLE_TASK_ARCHITECT,
    ModelCallLedger, ModelReply,
)
from taskvm.projection.app import create_app
from taskvm.projection.store import ProjectionSessionStore
from taskvm.workspace_ui.composition import (
    GovernanceServicePort, bootstrap_real_full,
)

from tests.runtime.conftest import FakeSubstrate

REPO = Path(__file__).resolve().parents[2]
GOAL = "把日历事件「产品发布」改期到 2026-08-18"
GOAL2 = "把日历事件「产品发布」改期到 2026-08-19"
SID = "rm0-gp-wiring"

COMPILER_REPLY = {
    "variables": [{
        "semantic_key": "event_date", "label": "event_date",
        "value_type": "date", "mutability": "editable",
        "observed": "2026-08-17", "confidence": 0.97,
        "evidence": [{
            "surface_label": "app", "visible_label": "event_date",
            "visible_context": "event_date=2026-08-17",
            "value_pattern": r"event_date=(\S+)"}]}],
    "ambiguities": [], "needs_clarification": False,
}

ARCHITECT_REPLY = {
    "variables": [{
        "semantic_key": "event_date", "label": "event_date",
        "value_type": "date", "mutability": "editable",
        "desired": "2026-08-18"}],
    "workflow": {"nodes": [
        {"kind": "action", "label": "改期「产品发布」",
         "semantic_goal": "把发布事件改期",
         "sets": {"event_date": "2026-08-18"},
         "completion": "event_date==2026-08-18",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["event_date"]},
        {"kind": "terminal", "label": "完成", "after": ["改期「产品发布」"]},
    ]},
}

#: the recomposition reply for the NEW goal (2026-08-19) — same shape as
#: the governance-layer RECOMPOSE_JSON (carried variables appear as bare
#: semantic_key entries; only the retargeted field carries a new desired).
RECOMPOSE_REPLY = {
    "variables": [
        {"semantic_key": "event_date", "desired": "2026-08-19"}],
    "workflow": {"nodes": [
        {"kind": "action", "label": "改期「产品发布」到新日期",
         "semantic_goal": "把发布事件改期到 2026-08-19",
         "sets": {"event_date": "2026-08-19"},
         "completion": "event_date==2026-08-19",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["event_date"]},
        {"kind": "terminal", "label": "完成",
         "after": ["改期「产品发布」到新日期"]},
    ]},
}

CUA_ACT_19 = {"kind": "act",
              "action": {"kind": "type", "text": "event_date=2026-08-19"}}
CUA_DONE = {"kind": "done"}
BAD_REPLY = {"nonsense": True}


class ScriptedPort:
    """One scripted reply per REAL provider request, in call order."""

    default_model = "scripted-goalpatch"

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []

    def complete_json(self, *, system, user, model=None, max_tokens=3072,
                      temperature=None, image_data_url=None):
        self.calls.append(system + "\n--\n" + user)
        item = self.script.pop(0) if self.script else CUA_DONE
        return ModelReply(parsed=item, raw=json.dumps(item, ensure_ascii=False),
                          model=model or "scripted", prompt_tokens=5,
                          completion_tokens=3)


def _wait_until(pred, timeout=20.0, msg="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.1)
    pytest.fail(f"timed out waiting for: {msg}")


# ── the chain closes over the PUBLIC route, then execution FOLLOWS ─────────

def test_goal_patch_closes_recompose_over_public_route():
    substrate = FakeSubstrate({"app": {"event_date": "2026-08-17"}})
    port = ScriptedPort([COMPILER_REPLY, ARCHITECT_REPLY,
                         RECOMPOSE_REPLY, CUA_ACT_19, CUA_DONE])
    ledger = ModelCallLedger()
    store = ProjectionSessionStore()

    bundle = bootstrap_real_full(
        goal=GOAL, sid=SID, substrate=substrate,
        model_port=port, ledger=ledger, store=store)
    kernel = bundle["kernel"]
    client = create_app(store).test_client()

    # the production registration carries the service-backed port (the
    # wiring this file pins — pre-fix this was the KernelGovernancePort
    # fallback with NO architect closure)
    sess = store.get(SID)
    assert sess is not None
    assert isinstance(sess.governance_port(), GovernanceServicePort)

    # ── the public goal_patch runs the FROZEN closure chain ─────────────
    resp = client.post(f"/api/sessions/{SID}/governance/goal_patch",
                       json={"goal": GOAL2, "rationale": "改到 19 日"})
    assert resp.status_code == 202, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True and body["action"] == "goal_patch"
    assert body["result"]["architect_calls"] == 1
    assert body["result"]["recompose_closed"] is True

    # exactly ONE recompose request; the NEW goal entered its prompt
    assert len(port.calls) == 3
    assert GOAL2 in port.calls[2]
    # ledger 1:1 across the chain: 1 compiler + 2 architect rows
    assert ledger.total() == 3 == len(port.calls)
    roles = [r.role for r in ledger.records]
    assert roles.count(MODEL_ROLE_STATE_COMPILER) == 1
    assert roles.count(MODEL_ROLE_TASK_ARCHITECT) == 2
    # the transition REALLY closed — unblocked by construction
    assert kernel.pending_recompose is None
    variables = {v.semantic_key: v for v in kernel.task_state().variables}
    assert variables["event_date"].desired == "2026-08-19"

    # ── execution under the NEW goal: start succeeds (no 409) and the
    #    redirected CUA lands the new target through the REAL substrate ─
    resp = client.post(f"/api/sessions/{SID}/governance/start")
    assert resp.status_code == 200, resp.get_json()
    _wait_until(lambda: ("app", "type") in substrate.act_log,
                msg="CUA gesture for the NEW goal")
    _wait_until(lambda: substrate.world["app"]["event_date"] == "2026-08-19",
                msg="world moved to the NEW target date")
    client.post(f"/api/sessions/{SID}/governance/stop")


# ── failure is honest (blocked, inspectable), retry closes ─────────────────

def test_goal_patch_failure_honest_then_retry_closes():
    substrate = FakeSubstrate({"app": {"event_date": "2026-08-17"}})
    # BAD reply + its bounded repair both fail; the GOOD reply serves retry
    port = ScriptedPort([COMPILER_REPLY, ARCHITECT_REPLY,
                         BAD_REPLY, BAD_REPLY,
                         RECOMPOSE_REPLY, CUA_ACT_19, CUA_DONE])
    ledger = ModelCallLedger()
    store = ProjectionSessionStore()

    bundle = bootstrap_real_full(
        goal=GOAL, sid=SID + "-f", substrate=substrate,
        model_port=port, ledger=ledger, store=store)
    kernel = bundle["kernel"]
    client = create_app(store).test_client()

    resp = client.post(f"/api/sessions/{SID}-f/governance/goal_patch",
                       json={"goal": GOAL2})
    assert resp.status_code == 409, resp.get_json()   # GoalRecomposeFailed
    assert kernel.pending_recompose is not None, "failure inspectable"
    # execution honestly blocked while the transition is pending
    resp = client.post(f"/api/sessions/{SID}-f/governance/start")
    assert resp.status_code == 409

    # the production bundle exposes the service — retry closes the SAME
    # transition (no fallback plan was ever installed)
    service = bundle["governance_service"]
    assert service is not None
    service.retry_goal_recompose()
    assert kernel.pending_recompose is None
    variables = {v.semantic_key: v for v in kernel.task_state().variables}
    assert variables["event_date"].desired == "2026-08-19"

    resp = client.post(f"/api/sessions/{SID}-f/governance/start")
    assert resp.status_code == 200, resp.get_json()
    _wait_until(lambda: ("app", "type") in substrate.act_log,
                msg="post-retry CUA gesture")
    client.post(f"/api/sessions/{SID}-f/governance/stop")


# ── static anti-regression lock on the bootstrap wiring ────────────────────

def test_bootstrap_body_registers_service_governance_port():
    src = (REPO / "taskvm/workspace_ui/composition.py").read_text("utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "bootstrap_real_full")
    body_src = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
    assert "GovernanceService(" in body_src
    assert "governance=GovernanceServicePort(" in body_src
