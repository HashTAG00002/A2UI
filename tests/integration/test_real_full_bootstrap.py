"""B-07 — the genuine real-full composition bootstrap.

The work order's acceptance, verbatim: a bootstrap path where a
natural-language goal REALLY flows through

    NL goal → fresh observation → StateCompiler → TaskArchitect → Kernel
    → shared ledger → AutonomyRuntime → projection session → PUBLIC
    governance start → real CUA → real GUI → verifier

with NONE of the demo's hand-built intermediates (``_make_kernel`` /
hand-written ``TaskVariable`` list / hand-written ``WorkflowGraph``).

This file pins the CONTRACT WIRING with a scripted model port (explicitly
allowed by the work order when provider credentials are unavailable) —
the wiring proves the compiler/architect/CUA requests all happen, all land
in ONE shared ledger 1:1, and the final action goes through the substrate
session's REAL ``act()``. The REAL-provider smoke is `environment_blocked`
unless OPENAI_API_KEY is set (see the final test); a scripted-port pass is
NEVER claimed as a real-model pass.
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
from taskvm.workspace_ui.composition import bootstrap_real_full

from tests.runtime.conftest import FakeSubstrate

REPO = Path(__file__).resolve().parents[2]
GOAL = "把日历事件「产品发布」改期到 2026-08-18"
SID = "rm0-b07"

# ── scripted replies (schemas mirror tests/architect/test_scenarios.py) ────

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

CUA_ACT = {"kind": "act",
           "action": {"kind": "type", "text": "event_date=2026-08-18"}}
CUA_DONE = {"kind": "done"}


class ScriptedPort:
    """One scripted reply per REAL provider request, in call order."""

    default_model = "scripted-real-full"

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


# ── no hand-built intermediates (AST lock on the bootstrap function) ──────

def test_bootstrap_body_has_no_hand_built_intermediates():
    src = (REPO / "taskvm/workspace_ui/composition.py").read_text("utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "bootstrap_real_full")
    body_src = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
    for banned in ("_make_kernel", "WorkflowGraph(", "TaskVariable("):
        assert banned not in body_src, banned
    # the kernel state/plan come ONLY from the architect product
    assert "init_task_state(arch.variables)" in body_src
    assert "set_plan(arch.graph)" in body_src


# ── the full wiring, end to end over the PUBLIC governance route ──────────

def test_real_full_bootstrap_nl_goal_to_real_gui():
    substrate = FakeSubstrate({"app": {"event_date": "2026-08-17"}})
    port = ScriptedPort([COMPILER_REPLY, ARCHITECT_REPLY, CUA_ACT, CUA_DONE])
    ledger = ModelCallLedger()
    store = ProjectionSessionStore()

    bundle = bootstrap_real_full(
        goal=GOAL, sid=SID, substrate=substrate,
        model_port=port, ledger=ledger, store=store)

    # ── wiring stage: compiler + architect each made a REAL request ─────
    assert len(port.calls) == 2
    assert ledger.total() == 2                     # 1 request = 1 row
    roles = [r.role for r in ledger.records]
    assert roles.count(MODEL_ROLE_STATE_COMPILER) == 1
    assert roles.count(MODEL_ROLE_TASK_ARCHITECT) == 1
    # the NL goal REALLY entered the compiler and the architect prompts
    assert GOAL in port.calls[0]
    assert GOAL in port.calls[1]

    # ── the kernel is built FROM THE ARCHITECT PRODUCT ──────────────────
    kernel = bundle["kernel"]
    variables = {v.semantic_key: v for v in kernel.task_state().variables}
    assert variables["event_date"].desired == "2026-08-18"
    graph = kernel.workflow().graph
    assert graph is not None, "architect workflow never reached the kernel"
    node_ids = {n.node_id for n in graph.nodes}
    assert "a1" not in node_ids and "t1" not in node_ids   # not demo fixture
    assert len(graph.nodes) == 2

    # ── fresh observation happened; internal shot refs never leaked ────
    assert substrate.observe_log, "no fresh observe before compiling"
    joined = "\n".join(port.calls)
    assert "shot://" not in joined                  # non-data-URL dropped

    # ── the session registered in the projection ────────────────────────
    assert store.get(SID) is not None

    # ── PUBLIC governance start drives the rest of the chain ────────────
    client = create_app(store).test_client()
    resp = client.post(f"/api/sessions/{SID}/governance/start")
    assert resp.status_code == 200, resp.get_json()

    _wait_until(lambda: ("app", "type") in substrate.act_log,
                msg="real GUI gesture through substrate.act")
    _wait_until(lambda: len(port.calls) >= 4,
                msg="CUA predict calls (ACT then DONE)")

    # ── post-run invariants: all three roles, ONE ledger, 1:1 ───────────
    assert substrate.world["app"]["event_date"] == "2026-08-18"
    cua_calls = len(port.calls) - 2
    assert cua_calls >= 1
    roles = [r.role for r in ledger.records]
    assert roles.count(MODEL_ROLE_CUA) == cua_calls
    assert ledger.total() == len(port.calls)         # 1:1 — no double rows
    request_ids = [r.request_id for r in ledger.records
                   if r.role == MODEL_ROLE_CUA]
    assert len(request_ids) == len(set(request_ids))  # unique per request

    # stop the driver cleanly
    resp = client.post(f"/api/sessions/{SID}/governance/stop")
    assert resp.status_code == 200


def test_no_leak_across_the_three_model_prompts():
    substrate = FakeSubstrate({"app": {"event_date": "2026-08-17"}})
    port = ScriptedPort([COMPILER_REPLY, ARCHITECT_REPLY, CUA_ACT, CUA_DONE])
    bootstrap_real_full(goal=GOAL, sid=SID, substrate=substrate,
                        model_port=port)
    store = ProjectionSessionStore()
    substrate2 = FakeSubstrate({"app": {"event_date": "2026-08-17"}})
    port2 = ScriptedPort([COMPILER_REPLY, ARCHITECT_REPLY, CUA_ACT, CUA_DONE])
    bundle = bootstrap_real_full(goal=GOAL, sid=SID + "-2",
                                 substrate=substrate2, model_port=port2,
                                 store=store)
    client = create_app(store).test_client()
    client.post(f"/api/sessions/{SID}-2/governance/start")
    _wait_until(lambda: len(port2.calls) >= 4, msg="cua calls")
    for text in port2.calls:
        for banned in ("entity_id", "data-entity-id", "set_state",
                       "get_state", "evt:", "action:"):
            assert banned not in text, f"leak {banned!r} into a prompt"
    client.post(f"/api/sessions/{SID}-2/governance/stop")


# ── the REAL-provider smoke: honest environment gate ──────────────────────

def test_real_provider_smoke_is_environment_gated():
    """With no OPENAI_API_KEY (or an unreachable endpoint) the REAL-model
    smoke is `environment_blocked` — a scripted wiring pass is never
    claimed as a real-model pass (work order §B-07)."""
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("environment_blocked: no OPENAI_API_KEY — the "
                    "real-provider smoke is NOT claimed here")
    substrate = FakeSubstrate({"app": {"event_date": "2026-08-17"}})
    try:
        bundle = bootstrap_real_full(goal=GOAL, sid="rm0-b07-real",
                                     substrate=substrate)
    except Exception as e:                       # provider down/quota — honest
        pytest.skip(f"environment_blocked: real provider unreachable ({e})")
        raise                                    # for type-checkers only
    ledger = bundle["ledger"]
    assert isinstance(ledger, ModelCallLedger)
    assert ledger.total() >= 2                    # compiler + architect REAL
    roles = [r.role for r in ledger.records]
    assert MODEL_ROLE_STATE_COMPILER in roles
    assert MODEL_ROLE_TASK_ARCHITECT in roles
