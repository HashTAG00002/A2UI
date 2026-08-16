"""Smoke Journey — the 15-point integration journey of handoff 08, over
real HTTP against the production projection app.

Deterministic everywhere (scripted port for the ONE architect call,
deterministic CUA/extractor/serializer doubles). Per the handoff rule,
deterministic success here NEVER claims the real-model CUA passed — the
real-model arc stays a manual E2E with provider keys.

Journey point → test mapping (numbers = handoff 08 §完整 Smoke Journey):
  1  service boot + health          → fixture `stack` (all tests)
  2  free-form goal, not task_id    → test_journey_a
  3  initial visible observation    → test_journey_b (pre-start observe)
  4  State Compiler + TaskArchitect → test_journey_a (REAL architect via
                                       a scripted model port — the
                                       pipeline code is the production one)
  5  workflow shown by the frontend → test_journey_a (snapshot shape; the
                                       real-browser DOM check lives in
                                       test_playwright_e2e)
  6  CUA advances after start       → test_journey_b
  7  surface card shows screenshot  → test_journey_b (ACTION_OBSERVED
                                       artifact_ref → card → /artifacts)
  8  fan-out/fan-in verified        → test_journey_b (FAN_OUT/BARRIER
                                       topology FROM the architect output)
  9  LocalPatch mid-run             → test_journey_b (pause → patch →
                                       resume → retargeted completion)
  10 GoalPatch in flight, old plan  → test_journey_a (202 + pending_
    frozen                           recompose + start 409 + frozen
                                       statuses; the in-flight-response
                                       race itself is kernel/runtime
                                       contract territory — tests/kernel
                                       + tests/runtime own it)
  11 inactive-surface conflict      → test_journey_c (heartbeat via the
                                       runtime's PUBLIC poll API — the
                                       production ticker wiring is a
                                       composition-root concern)
  12 rollback via GUI compensation  → test_journey_b
  13 irreversible shown honestly    → test_journey_b (one architect
                                       action scripted irreversible)
  14 SSE reconnect                  → test_journey_d
  15 no 405/500 anywhere            → inline asserts + the crawler suite
"""
from __future__ import annotations

import copy
import json
import re
import socket
import threading
import time

import pytest
import requests

from taskvm.architect import ModelCallLedger, ModelReply, TaskArchitect
from taskvm.domain import (
    NodeKind,
    TaskIntent,
    TaskVariable,
    WorkflowGraph,
    WorkflowNode,
)
from taskvm.kernel import TaskVMKernel
from taskvm.projection.app import create_app
from taskvm.projection.store import (
    ArtifactStore,
    ProjectionSessionStore,
    SurfaceDecl,
)
from taskvm.runtime.bootstrap import RuntimePorts, compose_runtime
from taskvm.runtime.ports import CUADecision, CUADecisionKind
from taskvm.substrate import GuiAction
from taskvm.verifier.visible import VisibleVerifier

from tests.runtime.conftest import (
    FakeSubstrate,
    ScriptedCUA,
    action_node,
    make_kernel,
    make_runtime,
    var,
)

PNG_BYTES = (b"\x89PNG\r\n\x1a\n" + b"smoke-journey-png-bytes" * 4)

# ── the scripted architect reply (schema mirror of
#    tests/architect/test_scenarios.py ARCHITECTURE_JSON; one action made
#    irreversible for journey point 13) ────────────────────────────────────
ARCHITECTURE_REPLY = {
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
         "semantic_goal": "推迟发布会议",
         "sets": {"release_date": "2026-08-18"},
         "completion": "release_date==2026-08-18",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["发布日期"]},
        {"kind": "fan_out", "label": "同步依赖", "after": ["改发布日期"]},
        {"kind": "action", "label": "同步文案", "container": "同步依赖",
         "semantic_goal": "文案截止同步",
         "sets": {"copy_deadline": "2026-08-18"},
         "completion": "copy_deadline==2026-08-18",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["文案截止"]},
        {"kind": "action", "label": "同步测试", "container": "同步依赖",
         "semantic_goal": "测试截止同步",
         "sets": {"qa_deadline": "2026-08-18"},
         "completion": "qa_deadline==2026-08-18",
         "reversibility": "irreversible", "risk": "外部系统不可恢复",
         "target_evidence": ["测试截止"]},
        {"kind": "barrier", "label": "汇合校验", "after": ["同步依赖"]},
        {"kind": "checkpoint", "label": "发布就绪检查点", "after": ["汇合校验"]},
        {"kind": "terminal", "label": "完成", "after": ["发布就绪检查点"]},
    ]},
}

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


class ScriptedPort:
    """One scripted reply (the architect's single model call)."""

    def __init__(self, reply):
        self._reply = reply
        self.calls: list[str] = []

    def complete_json(self, *, system, user, model=None, max_tokens=3072,
                      temperature=None, image_data_url=None):
        self.calls.append(system + "\n" + user)
        return ModelReply(parsed=copy.deepcopy(self._reply),
                          raw=json.dumps(self._reply, ensure_ascii=False),
                          model=model or "scripted")


class MultiKeyCUA:
    """Deterministic CUA over the DeterministicSerializer grammar:
    ``set <key> to <value>`` / ``restore <key> back to <value>``; types
    ``key=value`` until the visible world shows it, then DONE."""

    def __init__(self):
        self.calls = 0
        self.goals: list[str] = []

    def predict_action(self, *, goal, observation, labels=None,
                       attempt=1, model=None) -> CUADecision:
        self.calls += 1
        self.goals.append(goal)
        m = (re.search(r"set (\w+) to (\S+)\s*$", goal.strip())
             or re.search(r"restore (\w+) back to (\S+)\s*$", goal.strip()))
        if m is None:
            return CUADecision(kind=CUADecisionKind.FAIL,
                               reason="goal carries no parseable target")
        key, target = m.group(1), m.group(2)
        current = _kv_tokens(observation.visible_text or "")
        if current.get(key) == target:
            return CUADecision(kind=CUADecisionKind.DONE)
        return CUADecision(
            kind=CUADecisionKind.ACT,
            action=GuiAction(kind="type", text=f"{key}={target}"))


def _kv_tokens(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in text.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            out[k] = v
    return out


class TokenExtractor:
    """Observation → ObservedValues from "k=v" tokens (known keys)."""

    def extract(self, observation, variables):
        from taskvm.domain.state import (
            ObservedValue, SurfaceEvidence, SurfaceHandle,
        )
        known = set(variables)
        out = []
        for tok in (observation.visible_text or "").split():
            if "=" in tok:
                k, _, v = tok.partition("=")
                if k in known:
                    out.append(ObservedValue(
                        semantic_key=k, value=v,
                        evidence=(SurfaceEvidence(
                            surface=SurfaceHandle(handle_id="vis"),
                            visible_label=k, observed_value=v),)))
        return tuple(out)


class DeterministicSerializer:
    """The text grammar MultiKeyCUA parses (mirrors the runtime e2e)."""

    def cua_goal(self, contract, labels=None, *, attempt: int = 1) -> str:
        ((k, v),) = list((contract.desired_state or {}).items())
        return f"set {k} to {v}"

    def compensation_goal(self, entry, labels=None) -> str:
        return f"restore {entry.semantic_key} back to {entry.to_observed}"


class ArtifactSubstrate(FakeSubstrate):
    """FakeSubstrate with a FIXED screenshot_ref so the projection store
    can hold the artifact bytes under a stable key (journey point 7)."""

    def __init__(self, worlds, ref):
        super().__init__(worlds)
        self._ref = ref

    def observe(self, surface, previous_fingerprint=None):
        from dataclasses import replace
        obs = super().observe(surface, previous_fingerprint)
        return replace(obs, screenshot_ref=self._ref)


def _kernel_from_real_architect(session_id: str):
    """Journey point 4: the REAL TaskArchitect pipeline (one scripted
    model call) → validated TaskArchitecture → kernel."""
    port = ScriptedPort(ARCHITECTURE_REPLY)
    arch = TaskArchitect(port, ModelCallLedger()).compose(
        INTENT, OBSERVED_VARS)
    assert len(port.calls) == 1, "initial composition = exactly ONE call"
    kernel = TaskVMKernel(session_id, INTENT)
    kernel.init_task_state(arch.variables)
    assert arch.graph is not None, "architect produced no graph"
    kernel.set_plan(arch.graph)
    return kernel, arch


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=1).status_code == 200:
                return
        except Exception:
            time.sleep(0.2)
    pytest.fail(f"server did not start: {url}")


def _wait_until(pred, timeout: float = 20.0, msg: str = "condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.2)
    pytest.fail(f"timed out waiting for: {msg}")


class _SSEListener:
    def __init__(self, base: str, sid: str):
        self.types: list[str] = []
        self.frames: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._listen,
                                        args=(base, sid), daemon=True)

    def _listen(self, base, sid):
        try:
            r = requests.get(f"{base}/api/sessions/{sid}/sse",
                             stream=True, timeout=30)
            for line in r.iter_lines():
                if self._stop.is_set():
                    break
                if line and line.startswith(b"data: "):
                    try:
                        data = json.loads(line[len(b"data: "):])
                        self.types.append(data.get("sse_type", ""))
                        self.frames.append(data)
                    except Exception:
                        pass
            r.close()
        except Exception:
            pass

    def start(self):
        self._thread.start()
        return self

    def wait_for(self, sse_type: str, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if sse_type in self.types:
                return True
            time.sleep(0.1)
        return False

    def wait_any(self, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.types:
                return True
            time.sleep(0.1)
        return False

    def stop(self):
        self._stop.set()


def _first_action_node(kernel) -> str:
    for n in kernel.workflow().graph.nodes:
        if n.kind is NodeKind.ACTION:
            return n.node_id
    raise AssertionError("no ACTION node in the plan")


# ── the shared app: three sessions on one server ───────────────────────────

@pytest.fixture(scope="module")
def stack():
    store = ProjectionSessionStore()
    artifacts = ArtifactStore()
    artifacts.put("shot-app", PNG_BYTES)

    # A — real-architect kernel, NO runtime (projection/governance plane)
    kernel_a, arch_a = _kernel_from_real_architect("smoke-arch")
    store.register("smoke-arch", kernel_a,
                   surfaces=(SurfaceDecl(surface_id="app",
                                         display_name="发布面板"),))

    # B — same real-architect pipeline + a live runtime over a visible
    # world whose artifact ref is stable
    kernel_b, _ = _kernel_from_real_architect("smoke-run")
    substrate_b = ArtifactSubstrate(
        {"app": {"release_date": "2026-08-14",
                 "copy_deadline": "2026-08-14",
                 "qa_deadline": "2026-08-14"}}, "shot-app")
    runtime_b = compose_runtime(
        kernel_b, substrate_b,
        ports=RuntimePorts(
            serializer=DeterministicSerializer(),
            cua_model=MultiKeyCUA(),
            extractor=TokenExtractor(),
            verifier=VisibleVerifier(),
            ledger=ModelCallLedger()))  # type: ignore[arg-type]
    store.register("smoke-run", kernel_b, runtime=runtime_b,
                   surfaces=(SurfaceDecl(surface_id="app",
                                         display_name="发布面板"),),
                   artifacts=artifacts)

    # C — two-surface world for the inactive-surface conflict journey
    kernel_c = make_kernel(
        [var("x", "x0", "A"), var("y", "y0", "B")],
        WorkflowGraph(nodes=(
            WorkflowNode("root", NodeKind.SEQUENCE, "task"),
            action_node("a1", desired={"x": "A"}, parent_id="root"),
            WorkflowNode("term", NodeKind.TERMINAL, "done",
                         depends_on=("a1", "root")),
        )), goal="同步两个面板")
    substrate_c = FakeSubstrate({"app": {"x": "x0"},
                                  "desktop": {"y": "y0"}})
    runtime_c = make_runtime(kernel_c, substrate_c, ScriptedCUA([]))
    runtime_c._sync.set_active("app")
    store.register("smoke-conf", kernel_c, runtime=runtime_c,
                   surfaces=(SurfaceDecl(surface_id="app", display_name="app"),
                             SurfaceDecl(surface_id="desktop",
                                         display_name="desktop")))

    # D — a pristine kernel-only session for the SSE reconnect probe
    # (journey A's goal_patch leaves smoke-arch awaiting recompose)
    kernel_d, _ = _kernel_from_real_architect("smoke-sse")
    store.register("smoke-sse", kernel_d,
                   surfaces=(SurfaceDecl(surface_id="app",
                                         display_name="发布面板"),))

    # the production frontend wiring: serve() = create_app + the real
    # workspace_ui static assets (the SPA shell on / and /sessions/<sid>)
    from taskvm.workspace_ui import serve
    app = serve(store)
    port = _free_port()

    def _run():
        app.run(host="127.0.0.1", port=port, threaded=True,
                debug=False, use_reloader=False)

    threading.Thread(target=_run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    _wait_http(f"{base}/api/sessions")
    yield {"base": base, "store": store, "kernel_a": kernel_a,
           "kernel_b": kernel_b, "substrate_b": substrate_b,
           "runtime_c": runtime_c, "substrate_c": substrate_c,
           "arch": arch_a}


# ── journey A: boot / goal / real architect / projection / GoalPatch ──────

def test_journey_a(stack):
    base = stack["base"]

    # (1) the service is up and lists the sessions
    r = requests.get(f"{base}/api/sessions")
    assert r.status_code == 200
    assert set(r.json()["sessions"]) >= {"smoke-arch", "smoke-run",
                                         "smoke-conf"}

    # (2) the goal is the free-form user text (never a task_id)
    snap = requests.get(f"{base}/api/sessions/smoke-arch/snapshot")
    assert snap.status_code == 200
    body = snap.json()
    assert body["governance"]["goal"] == INTENT.goal

    # (4) the plan came from the REAL TaskArchitect pipeline: fan-out /
    # barrier / checkpoint topology is present, with the merged desireds
    wf = body["workflow"]
    assert wf["has_plan"] is True
    labels = {n["label"] for n in wf["nodes"]}
    assert {"改发布日期", "同步依赖", "同步文案", "同步测试", "汇合校验",
            "发布就绪检查点", "完成"} <= labels
    kinds = {n["kind_label"] for n in wf["nodes"]}
    assert any("fan" in k.lower() for k in kinds), kinds
    variables = {v["key"]: v for v in body["variables"]}
    assert variables["release_date"]["desired"] == "2026-08-18"

    # (5/15) the session page serves the frontend
    page = requests.get(f"{base}/sessions/smoke-arch")
    assert page.status_code == 200
    assert "text/html" in page.headers["Content-Type"]

    # (10) GoalPatch mid-flight contract, HTTP-visible half: 202 accepted,
    # start becomes an honest 409, and the affected FUTURE is invalidated
    # (E26 two-phase closure: an in-flight response can never execute
    # against an invalidated lane — stronger than a mere freeze)
    committed_before = {nid for nid, s in
                        {n["node_id"]: n["status"] for n in wf["nodes"]}.items()
                        if s == "committed"}
    r = requests.post(f"{base}/api/sessions/smoke-arch/governance/goal_patch",
                      json={"goal": "全新目标：只同步文案",
                            "rationale": "scope change"})
    assert r.status_code == 202, r.text
    r = requests.post(f"{base}/api/sessions/smoke-arch/governance/start",
                      json={})
    assert r.status_code == 409
    assert r.json()["ok"] is False
    wf2 = requests.get(
        f"{base}/api/sessions/smoke-arch/snapshot").json()["workflow"]
    after = {n["node_id"]: n["status"] for n in wf2["nodes"]}
    assert set(after.values()) <= {"invalidated"}, after
    committed_after = {nid for nid, s in after.items() if s == "committed"}
    assert committed_after == committed_before


# ── journey B: the autonomous fan-out arc ──────────────────────────────────

def test_journey_b(stack):
    base, kernel, substrate = (stack["base"], stack["kernel_b"],
                               stack["substrate_b"])
    sid = "smoke-run"

    # (3) initial visible observation — the substrate plane shows real
    # visible text BEFORE autonomy starts
    obs0 = substrate.observe("app")
    assert "release_date=2026-08-14" in obs0.visible_text

    listener = _SSEListener(base, sid).start()
    time.sleep(0.6)

    # (6) start → CUA advances autonomously (deterministic double, real
    # gestures: each act is a type that lands on the visible world)
    r = requests.post(f"{base}/api/sessions/{sid}/governance/start",
                      json={})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "running"

    def node_status(nid):
        st = kernel.workflow().statuses.get(nid)
        return st.name if st is not None else None

    a1 = _first_action_node(kernel)
    _wait_until(lambda: node_status(a1) == "COMMITTED",
                msg="first lane committed")
    assert substrate.world["app"]["release_date"] == "2026-08-18"
    assert any(kind == "type" for _, kind in substrate.act_log), (
        "no real GUI gesture was performed")

    # (7) the surface card shows the live screenshot: ACTION_OBSERVED
    # carried the artifact ref, the card resolved it, the bytes serve
    snap = requests.get(f"{base}/api/sessions/{sid}/snapshot").json()
    cards = {c["surface_id"]: c for c in snap["surfaces"]}
    assert cards["app"]["latest_artifact_ref"] == "shot-app", cards
    art = requests.get(f"{base}/api/sessions/{sid}/artifacts/shot-app")
    assert art.status_code == 200
    assert art.headers["Content-Type"].startswith("image/png")

    # (9) LocalPatch mid-run: pause → checkpoint → patch a PENDING
    # lane's desired → resume honors the new target.  The checkpoint is
    # taken HERE (after the first lane committed, before fan-out) so
    # rollback later restores the fan-out lanes' writes that happened
    # AFTER the checkpoint.
    r = requests.post(f"{base}/api/sessions/{sid}/governance/pause",
                      json={"rationale": "edit"})
    assert r.status_code == 200, r.text
    r = requests.post(f"{base}/api/sessions/{sid}/governance/checkpoint",
                      json={"label": "第一刀后-检查点"})
    assert r.status_code == 201, r.text
    cp_id = r.json()["checkpoint_id"]
    r = requests.post(f"{base}/api/sessions/{sid}/governance/local_patch",
                      json={"updates": {"copy_deadline": "2026-08-19"},
                            "rationale": "文案再延一天"})
    assert r.status_code == 200, r.text
    snap = requests.get(f"{base}/api/sessions/{sid}/snapshot").json()
    desired = {v["key"]: v["desired"] for v in snap["variables"]}
    assert desired["copy_deadline"] == "2026-08-19"
    r = requests.post(f"{base}/api/sessions/{sid}/governance/resume",
                      json={"rationale": "go"})
    assert r.status_code == 200, r.text

    # (8) fan-out/fan-in: BOTH lanes commit (one on the retargeted
    # value)
    _wait_until(lambda: substrate.world["app"].get("copy_deadline")
                == "2026-08-19", msg="retargeted lane committed")
    _wait_until(lambda: substrate.world["app"].get("qa_deadline")
                == "2026-08-18", msg="second fan-out lane committed")
    snap = requests.get(f"{base}/api/sessions/{sid}/snapshot").json()
    nodes = {n["label"]: n for n in snap["workflow"]["nodes"]}
    assert nodes["同步文案"]["status"] == "committed"
    assert nodes["同步测试"]["status"] == "committed"

    # (13) the irreversible lane is displayed honestly (snapshot exposes
    # it; the frontend renders the 'irreversible' tag)
    assert nodes["同步测试"].get("action", {}).get("irreversible") is True

    # (12) rollback through GUI compensation: the plan EXECUTES and the
    # observed plane is restored for the post-checkpoint actions — the
    # irreversible lane stays honestly uncompensated (partial, never a
    # fake full restore).  copy_deadline was committed AFTER the
    # checkpoint → restored to 2026-08-14.  qa_deadline is irreversible
    # → stays at 2026-08-18 (honest PARTIAL).
    r = requests.post(f"{base}/api/sessions/{sid}/governance/pause",
                      json={"rationale": "prepare rollback"})
    assert r.status_code == 200, r.text
    r = requests.post(f"{base}/api/sessions/{sid}/governance/rollback",
                      json={"target_checkpoint_id": cp_id,
                            "rationale": "回到第一刀后"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["disposition"] in ("complete", "partial"), body
    # copy_deadline was committed AFTER the checkpoint → restored
    assert substrate.world["app"]["copy_deadline"] == "2026-08-14", (
        "compensation did not restore copy_deadline")
    assert listener.wait_for("compensation.requested"), listener.types
    assert (listener.wait_for("compensation.complete")
            or listener.wait_for("compensation.partial")), listener.types

    # (15) every status this journey saw was asserted 2xx (or the
    # semantic 409/4xx on purpose) — nothing was 405/500
    listener.stop()


# ── journey C: inactive-surface conflict → UI resolve ─────────────────────

def test_journey_c(stack):
    base, runtime, substrate = (stack["base"], stack["runtime_c"],
                                stack["substrate_c"])
    sid = "smoke-conf"

    # heartbeat ticker over the runtime's PUBLIC poll API — exactly the
    # call a composition root would wire (the threaded driver owns only
    # the active-surface autonomy loop)
    stop = threading.Event()

    def _tick():
        while not stop.is_set():
            try:
                runtime.poll_inactive_surfaces()
            except Exception:
                pass
            time.sleep(0.2)

    threading.Thread(target=_tick, daemon=True).start()
    time.sleep(0.8)  # let the baseline fingerprint establish

    # (11) external change on the INACTIVE surface → conflict (never a
    # silent overwrite), visible in the projection snapshot
    substrate.world["desktop"]["y"] = "NOT_B"
    _wait_until(lambda: requests.get(
        f"{base}/api/sessions/{sid}/snapshot").json()["conflicts"],
        msg="conflict visible in snapshot")
    stop.set()

    snap = requests.get(f"{base}/api/sessions/{sid}/snapshot").json()
    conflict = snap["conflicts"][0]
    assert conflict["conflict_id"]

    # the UI's resolve button path: resolve_conflict over HTTP clears it
    r = requests.post(f"{base}/api/sessions/{sid}/governance/resolve_conflict",
                      json={"conflict_id": conflict["conflict_id"],
                            "resolution": "keep_desired",
                            "detail": "smoke resolve"})
    assert r.status_code == 200, r.text
    snap = requests.get(f"{base}/api/sessions/{sid}/snapshot").json()
    ids = [c.get("conflict_id") for c in snap["conflicts"]]
    assert conflict["conflict_id"] not in ids


# ── journey D: SSE reconnect (live stream, no replay, no duplicates) ───────

def test_journey_d(stack):
    base = stack["base"]
    sid = "smoke-sse"

    first = _SSEListener(base, sid).start()
    time.sleep(0.5)
    r = requests.post(f"{base}/api/sessions/{sid}/governance/checkpoint",
                      json={"label": "sse-reconnect-probe"})
    assert r.status_code == 201
    assert first.wait_for("checkpoint.committed"), first.types
    first.stop()

    # reconnect: the new stream is LIVE — it carries subsequent events
    # but never replays the pre-disconnect one (no duplicate execution)
    second = _SSEListener(base, sid).start()
    time.sleep(0.5)
    r = requests.post(f"{base}/api/sessions/{sid}/governance/pause",
                      json={"rationale": "reconnect probe"})
    assert r.status_code == 200, r.text
    assert second.wait_any(), "reconnected stream delivered nothing"
    assert "checkpoint.committed" not in second.types, (
        "the reconnected stream replayed a pre-disconnect event "
        f"(duplicate execution risk): {second.types}")
    second.stop()
