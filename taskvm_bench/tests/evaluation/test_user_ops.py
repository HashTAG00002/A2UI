"""B-04/B-05 — ProjectionClient + UserOpDriver + per-op barrier + result
schema (unit level over a fake HTTP layer; the REAL-HTTP journey is the
RM-0.B Smoke 2 suite).

Iron rules pinned here (RM-0 work order §B-04):
  * the driver only speaks the projection PUBLIC routes — verified by
    asserting the exact path each op kind POSTs to;
  * it structurally holds ONLY a ProjectionClient — no Kernel/Runtime/
    CUAModel/governance-service handle can even be attached (AST-level
    import scan of the module — docstring prose does not count);
  * the barrier settles ONLY on public signals: HTTP return, the
    registered ``governance.applied`` SSE ack, the /events page total,
    the quiet window — never a hidden /test/accepted API;
  * per-op timeline records op_issued → http_accepted → first/last GUI
    action → verifier_completed → first_correct_projection → settled;
  * B-05: TrialRecord carries schema_version/git_sha/... and the TWO
    DISTINCT concepts environment_seed vs sample_index; the verdict is
    majority-honest (all-applied ⇒ pass, any-error ⇒ error).
"""
from __future__ import annotations

import ast
import json
import threading
import time
from pathlib import Path

import pytest

from taskvm_bench.evaluation.projection_client import ProjectionClient
from taskvm_bench.evaluation.results import (
    SCHEMA_VERSION, RunDirectory, TrialRecord, UserOpRecord,
)
from taskvm_bench.evaluation.user_ops import (
    SettlePolicy, USER_OP_KINDS, UserOp, UserOpDriver, next_op_id,
)

REPO = Path(__file__).resolve().parents[3]


# ── fake HTTP layer (duck-types the requests.Session surface used) ────────

class FakeResponse:
    #: test knob — whether a write bumps the fake events total (the
    #: public settle fallback signal)
    ok_bumps_events = True

    def __init__(self, status=200, body=None, lines=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self._lines = lines or []

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    @property
    def text(self):
        return json.dumps(self._body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSSEResponse(FakeResponse):
    """A STREAMING stand-in: iter_lines polls the shared frame list until
    the fake's stop event (mirrors a real long-lived SSE connection)."""

    def __init__(self, lines, lock, stop):
        super().__init__(200, {})
        self._lines = lines
        self._lock = lock
        self._stop = stop

    def iter_lines(self, decode_unicode=True):
        deadline = time.time() + 10.0
        i = 0
        while not self._stop.is_set() and time.time() < deadline:
            with self._lock:
                snap = list(self._lines)
            while i < len(snap):
                yield snap[i]
                i += 1
            time.sleep(0.01)


class FakeHTTP:
    """Scripted transport: records every request, serves canned routes."""

    def __init__(self):
        self.requests: list[dict] = []
        self.routes: dict = {}          # (method, path-suffix) -> Response
        self.events_total = 0
        self.push_sse_on_post = True
        self._sse_lock = threading.Lock()
        self._sse_lines: list = []
        self._sse_stop = threading.Event()

    def get(self, url, **kw):
        path = url.split("/api/sessions/")[-1]
        self.requests.append(dict(method="GET", url=url))
        if path.endswith("/sse"):
            return FakeSSEResponse(self._sse_lines, self._sse_lock,
                                   self._sse_stop)
        if path.split("?")[0].endswith("/events"):
            return FakeResponse(200, {"events": [], "total": self.events_total,
                                      "offset": 0, "limit": 1})
        return FakeResponse(200, {"revision": 1, "status": "idle",
                                  "variables": {"release_date":
                                                {"desired": "2026-08-18",
                                                 "observed": "2026-08-17"}}})

    def post(self, url, json=None, **kw):
        path = url.split("/api/sessions/")[-1]
        self.requests.append(dict(method="POST", url=url, body=json))
        resp = self.routes.get(path, FakeResponse(200, {"ok": True}))
        # a write bumps the events total — the public settle fallback
        if getattr(resp, "ok_bumps_events", True):
            self.events_total += 1
        if self.push_sse_on_post:
            self.push_sse("governance.applied")
        return resp

    def push_sse(self, sse_type: str) -> None:
        with self._sse_lock:
            self._sse_lines.append(
                "data: " + json.dumps({"sse_type": sse_type,
                                       "event_id": f"e{len(self._sse_lines)}"}))


def _client(http: FakeHTTP) -> ProjectionClient:
    return ProjectionClient("http://fake", "s1", http=http,
                            timeout_s=2.0)


# ── B-04 iron rules ────────────────────────────────────────────────────────

def test_driver_holds_only_a_client():
    http = FakeHTTP()
    driver = UserOpDriver(_client(http))
    assert set(vars(driver)) == {"_client", "_poll"}   # no kernel/runtime


_BANNED_IMPORT_PREFIXES = (
    "taskvm.runtime", "taskvm.kernel", "taskvm.governance",
    "taskvm.substrate", "taskvm.projection.services",
)


def _imported_modules(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


@pytest.mark.parametrize("module", [
    "taskvm_bench/evaluation/user_ops.py",
    "taskvm_bench/evaluation/projection_client.py",
])
def test_user_op_plane_never_imports_prototype_internals(module):
    """The user-op plane may only speak HTTP — no Python handle on the
    SUT's runtime/kernel/governance/substrate objects (AST-level import
    lock; prose in docstrings does not count as a reference)."""
    mods = _imported_modules(REPO / module)
    offenders = {m for m in mods
                 if m.startswith(_BANNED_IMPORT_PREFIXES)}
    assert offenders == set(), offenders


def test_no_hidden_test_accepted_api():
    """The barrier must not rely on any prototype-only accepted-probe."""
    for module in ("taskvm_bench/evaluation/user_ops.py",
                   "taskvm_bench/evaluation/projection_client.py"):
        src = (REPO / module).read_text(encoding="utf-8")
        # code-level check: the string appears nowhere outside docstrings
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "/test/accepted" in node.value and not (
                        node.lineno and src.count('"""') and False):
                    # docstrings are Constant strings too — skip module/class/
                    # function docstrings via exclusion below
                    pass
        assert "/test/accepted" not in _code_only(src)


def _code_only(src: str) -> str:
    """Source minus module/class/function docstrings (string constants in
    expression-statement position)."""
    tree = ast.parse(src)
    doc_ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc_ranges.append(
                    (body[0].lineno, body[0].end_lineno))
    lines = src.splitlines()
    for start, end in doc_ranges:
        for i in range(start - 1, end):
            lines[i] = ""
    return "\n".join(lines)


# ── every op kind routes to its PUBLIC governance route ───────────────────

ROUTE_BY_KIND = {
    "start": "/governance/start",
    "pause": "/governance/pause",
    "resume": "/governance/resume",
    "stop": "/governance/stop",
    "local_patch": "/governance/local_patch",
    "goal_patch": "/governance/goal_patch",
    "checkpoint": "/governance/checkpoint",
    "rollback": "/governance/rollback",
}


@pytest.mark.parametrize("op", [
    UserOp.start(),
    UserOp.pause("wait a moment"),
    UserOp.resume(),
    UserOp.stop("done for today"),
    UserOp.local_patch({"release_date": "2026-08-19"}),
    UserOp.goal_patch("改期到 2026-08-19"),
    UserOp.checkpoint("before the change"),
    UserOp.rollback("ckpt-1"),
])
def test_every_kind_posts_its_public_route(op):
    assert op.kind in USER_OP_KINDS
    http = FakeHTTP()
    http.routes[f"s1{ROUTE_BY_KIND[op.kind]}"] = FakeResponse(
        200, {"ok": True, "action": op.kind})
    outcome = UserOpDriver(_client(http)).execute(op)
    posts = [r for r in http.requests if r["method"] == "POST"
             and r["url"].endswith(ROUTE_BY_KIND[op.kind])]
    assert len(posts) == 1
    assert outcome.verdict == "applied"
    assert outcome.timeline["settled"] is not None


def test_payload_travels_verbatim():
    http = FakeHTTP()
    http.routes["s1/governance/local_patch"] = FakeResponse(200, {"ok": True})
    UserOpDriver(_client(http)).execute(
        UserOp.local_patch({"release_date": "2026-08-19"}, "用户改主意"))
    post = next(r for r in http.requests if r["method"] == "POST")
    assert post["body"] == {"updates": {"release_date": "2026-08-19"},
                            "rationale": "用户改主意"}


# ── the barrier: settle semantics per policy ───────────────────────────────

def test_sse_policy_settles_on_governance_applied():
    http = FakeHTTP()
    http.push_sse_on_post = True
    resp = FakeResponse(200, {"ok": True})
    resp.ok_bumps_events = False          # SSE is the ONLY signal here
    http.routes["s1/governance/pause"] = resp
    outcome = UserOpDriver(_client(http)).execute(UserOp.pause())
    assert outcome.verdict == "applied"
    assert outcome.timeline["settled"] is not None
    assert any(e["sse_type"] == "governance.applied"
               for e in outcome.sse_window)


def test_sse_policy_falls_back_to_events_page_growth():
    http = FakeHTTP()
    http.push_sse_on_post = False         # the stream misses the ack —
    resp = FakeResponse(200, {"ok": True})  # only /events total grows
    resp.ok_bumps_events = True
    http.routes["s1/governance/checkpoint"] = resp
    outcome = UserOpDriver(_client(http)).execute(
        UserOp.checkpoint("标记"))
    assert outcome.verdict == "applied"   # settled via /events total growth


def test_quiet_policy_settles_after_silence():
    http = FakeHTTP()
    http.push_sse_on_post = False          # frames come from the timeline
    http.routes["s1/governance/resume"] = FakeResponse(200, {"ok": True})
    # schedule a couple of late progress frames, then silence
    def _progress():
        time.sleep(0.05)
        http.push_sse("action.observed")
        time.sleep(0.05)
        http.push_sse("action.landed")
    threading.Thread(target=_progress, daemon=True).start()
    outcome = UserOpDriver(_client(http)).execute(UserOp.resume())
    assert outcome.verdict == "applied"
    assert outcome.timeline["first_gui_action"] is not None
    assert outcome.timeline["last_gui_action"] is not None


def test_barrier_timeout_is_honest_unsettled():
    http = FakeHTTP()
    http.push_sse_on_post = False
    resp = FakeResponse(200, {"ok": True})
    resp.ok_bumps_events = False          # nothing will ever move
    http.routes["s1/governance/pause"] = resp
    op = UserOp.pause(settle_policy=SettlePolicy("sse", timeout_s=0.3))
    outcome = UserOpDriver(_client(http)).execute(op)
    assert outcome.verdict == "unsettled"
    assert "timed out" in outcome.detail
    assert outcome.timeline["settled"] is None


def test_unexpected_http_class_is_rejected():
    http = FakeHTTP()
    http.routes["s1/governance/checkpoint"] = FakeResponse(  # unstable → 409
        409, {"ok": False, "error": "unstable"})
    outcome = UserOpDriver(_client(http)).execute(UserOp.checkpoint("x"))
    assert outcome.verdict == "rejected"
    assert "HTTP 409" in outcome.detail


def test_full_timeline_is_populated():
    http = FakeHTTP()
    http.push_sse_on_post = False
    http.routes["s1/governance/stop"] = FakeResponse(200, {"ok": True})
    http.push_sse("governance.applied")   # the route's own public ack
    http.push_sse("action.observed")
    http.push_sse("action.landed")
    http.push_sse("state.updated")
    outcome = UserOpDriver(_client(http)).execute(UserOp.stop())
    t = outcome.timeline
    assert t["op_issued"] is not None
    assert t["http_accepted"] is not None
    assert t["first_gui_action"] is not None
    assert t["last_gui_action"] is not None
    assert t["verifier_completed"] is not None      # state.updated
    assert t["first_correct_projection"] is not None
    assert t["settled"] is not None
    assert t["op_issued"] <= t["http_accepted"] <= t["settled"]


# ── op correlation stays client-side ──────────────────────────────────────

def test_request_log_pins_op_correlation():
    http = FakeHTTP()
    http.routes["s1/governance/pause"] = FakeResponse(200, {"ok": True})
    client = _client(http)
    UserOpDriver(client).execute(UserOp.pause())
    posts = [e for e in client.request_log if e["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["path"] == "/governance/pause"
    assert posts[0]["status"] == 200


# ── B-05: result schema + run directory ───────────────────────────────────

def test_trial_record_schema_fields():
    rec = TrialRecord(model="gpt-5.6-sol", substrate="builtin_web",
                      condition="taskvm-real-full",
                      environment_seed=42, sample_index=3)
    d = rec.__dict__
    for key in ("schema_version", "git_sha", "task_version",
                "harness_version", "model", "substrate", "condition",
                "environment_seed", "sample_index", "user_ops",
                "trial_verdict", "failure_class", "evaluation_error"):
        assert key in d, key
    assert d["schema_version"] == SCHEMA_VERSION
    # two DISTINCT concepts, both carried — never conflated
    assert d["environment_seed"] == 42 and d["sample_index"] == 3


def test_trial_verdict_aggregation_is_majority_honest():
    def op(verdict):
        return UserOpRecord(op_id=next_op_id(), kind="pause",
                            verdict=verdict)

    all_applied = TrialRecord()
    for _ in range(3):
        all_applied.add_op(op("applied"))
    all_applied.finalize()
    assert all_applied.trial_verdict == "pass"

    one_lucky = TrialRecord()
    one_lucky.add_op(op("applied"))
    one_lucky.add_op(op("rejected"))
    one_lucky.finalize()
    assert one_lucky.trial_verdict == "fail"        # 1 lucky ≠ PASS

    with_error = TrialRecord()
    with_error.add_op(op("applied"))
    with_error.add_op(op("error"))
    with_error.finalize()
    assert with_error.trial_verdict == "error"


def test_run_directory_layout(tmp_path):
    run = RunDirectory("rm0-b04b05-unit", root=str(tmp_path))
    rec = TrialRecord(model="m", substrate="builtin_web",
                      environment_seed=1, sample_index=0)
    rec.add_op(UserOpRecord(op_id=next_op_id(), kind="stop",
                            verdict="applied"))
    rec.finalize()
    trial_path = run.write_trial(rec, 0)
    art_path = run.write_artifact("shots/a.png", b"PNGDATA")
    report_path = run.write_report("summary.json", {"n": 1})
    manifest_path = run.write_manifest(model="m", condition="c")

    for p in (trial_path, art_path, report_path, manifest_path):
        assert Path(p).exists()
    assert Path(trial_path).parent.name == "trials"
    assert "artifacts" in Path(art_path).parts
    assert Path(report_path).parent.name == "reports"
    manifest = json.loads(Path(manifest_path).read_text())
    assert manifest["run_id"] == "rm0-b04b05-unit"
    assert manifest["development_only"] is True
    persisted = json.loads(Path(trial_path).read_text())
    assert persisted["user_ops"][0]["verdict"] == "applied"
    # B-05 honest-missing fields exist and are None (not fabricated)
    for key in ("world_diff", "protected_diff", "ledger_request_ids"):
        assert key in persisted["user_ops"][0]
        assert persisted["user_ops"][0][key] is None


def test_outcome_to_record_shape():
    http = FakeHTTP()
    http.routes["s1/governance/stop"] = FakeResponse(200, {"ok": True})
    outcome = UserOpDriver(_client(http)).execute(UserOp.stop("done"))
    record = outcome.to_record()
    for key in ("op_id", "kind", "verdict", "world_diff", "protected_diff",
                "projection", "rollback", "ledger_request_ids", "timeline",
                "artifacts"):
        assert key in record, key
    assert record["kind"] == "stop"
    assert record["rollback"] is None       # only rollback ops carry one
