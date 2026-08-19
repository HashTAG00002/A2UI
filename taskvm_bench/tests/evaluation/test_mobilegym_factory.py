"""B-08 tests — the MobileGym factory's every stage, over a FAKE bridge.

No real browser, no real MobileGymEnv: the fake bridge below implements
exactly the routes ``MobileGymSubstrateSession`` (L1) and
``MobileGymEvaluationEnvironment`` (setup/oracle) already speak, so the
factory's orchestration is exercised over REAL HTTP against a scripted
in-process server. The real-environment smoke is Smoke 3 (development
only, ``eval_results/rm0/smoke3.json``) — these unit tests are the
contract that smoke stands on.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from taskvm_bench.benchmark.mobilegym_fixtures import (
    TOP3_EXPENSE_TO_WECHAT,
)
from taskvm_bench.evaluation.mobilegym_factory import (
    _BRIDGE_ALLOWED_FLAGS,
    BridgeUnavailableError,
    MobileGymFactory,
    MobileGymTrialSpec,
    ledger_role_counts,
)
from taskvm_bench.evaluation.results import TrialRecord
from taskvm_bench.evaluation.user_ops import OpOutcome, UserOp
from taskvm.substrate.port import GuiAction


# ── the fake bridge (an in-process HTTP server) ─────────────────────────────

class FakeBridge:
    """Scriptable stand-in for taskvm.substrate.mobilegym.bridge.

    Serves the L1 + setup/oracle routes the substrate clients use, keeps
    a request log, and lets a test flip responses (e.g. make two oracle
    reads disagree) or go dark (health failure)."""
    revision = 0
    alive = True
    last_seed: dict | None = None
    #: die (503) after this many total requests (None = never)
    die_after_requests: int | None = None
    _n_requests = 0

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.state: dict = {"wechat_chats": [
            {"id": "wxid_huangyong_demo", "peer_name": "黄勇",
             "n_messages": 0, "last_message": "", "messages": ""}]}
        # the X store slice (toggle id lists — what /api/x_state serves)
        self.x_state: dict = {"likedPostIds": [], "retweetedPostIds": [],
                              "bookmarkedPostIds": []}
        # per-test hooks: oracle_mutator(state_dict) -> state_dict
        self.oracle_mutator = None
        self.observe_fingerprint = "fake-fp-0001"
        srv = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        srv.daemon_threads = True
        self._server = srv
        self.port = srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    # -- the handler ------------------------------------------------------
    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):      # silence
                pass

            def _json(self, code: int, body: dict):
                blob = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

            def do_GET(self):
                outer.requests.append(("GET", self.path))
                outer._n_requests += 1
                if (outer.die_after_requests is not None
                        and outer._n_requests > outer.die_after_requests):
                    outer.alive = False
                if not outer.alive:
                    self._json(503, {"status": "down"})
                    return
                if self.path == "/health":
                    self._json(200, {"status": "ok", "site": "mobilegym"})
                elif self.path.startswith("/api/observe/"):
                    outer.revision += 1
                    self._json(200, {
                        "sid": self.path.rsplit("/", 1)[-1],
                        "revision": outer.revision,
                        "screenshot": "data:image/png;base64,"
                                      "iVBORw0KGgo=",
                        "visible_text": "微信 黄勇 通讯录",
                        "fingerprint": outer.observe_fingerprint,
                        "timestamp": time.time()})
                elif self.path.startswith("/api/wechat_chats/"):
                    rows = outer.oracle_mutator(outer.state) \
                        if outer.oracle_mutator else outer.state
                    self._json(200, {
                        "site": "mobilegym",
                        "sid": self.path.rsplit("/", 1)[-1],
                        "wechat_chats": rows["wechat_chats"]})
                elif self.path.startswith("/api/x_state/"):
                    payload = {"site": "mobilegym",
                               "sid": self.path.rsplit("/", 1)[-1]}
                    payload.update(outer.x_state)
                    self._json(200, payload)
                elif self.path.startswith("/api/x_posts/"):
                    # the semantic oracle route (oracle_state →
                    # /api/x_posts/<sid>): return the tracked posts
                    # with their toggle states derived from x_state.
                    # The tracked post ids are the ones that appear in
                    # any toggle list OR were seeded via inject_task.
                    tracked = set(outer.x_state.get("likedPostIds", []))
                    tracked |= set(outer.x_state.get("bookmarkedPostIds", []))
                    tracked |= set(outer.x_state.get("retweetedPostIds", []))
                    # also include seeded posts (from the seed_state
                    # that inject_task received)
                    if outer.last_seed:
                        x_user = ((outer.last_seed.get("seed_state")
                                   or {}).get("x") or {}).get("user") or {}
                        for key in ("likedPostIds", "bookmarkedPostIds",
                                    "retweetedPostIds"):
                            tracked |= set(x_user.get(key, []))
                    # also always include the RM-C04-01 anchor's
                    # tracked posts (so oracle_state sees them even
                    # before any toggle fires)
                    from taskvm_bench.benchmark.rm_anchor_tasks import (
                        CPI_POST_ID, PROTECTED_POST_ID)
                    tracked.add(CPI_POST_ID)
                    tracked.add(PROTECTED_POST_ID)
                    posts = []
                    liked = set(outer.x_state.get("likedPostIds") or [])
                    bmd = set(outer.x_state.get("bookmarkedPostIds") or [])
                    rtd = set(outer.x_state.get("retweetedPostIds") or [])
                    for pid in sorted(tracked):
                        posts.append({
                            "id": pid,
                            "liked": pid in liked,
                            "bookmarked": pid in bmd,
                            "retweeted": pid in rtd,
                        })
                    self._json(200, {
                        "site": "mobilegym",
                        "sid": self.path.rsplit("/", 1)[-1],
                        "x_posts": posts})
                elif self.path.startswith("/api/session_state/"):
                    self._json(200, {
                        "site": "mobilegym", "sid":
                            self.path.rsplit("/", 1)[-1],
                        "has_task": True,
                        "summary": {"n_chats": 1, "n_contacts": 1,
                                    "n_tx": 0, "balance": 0}})
                else:
                    self._json(404, {"error": f"no route {self.path}"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(("POST", self.path))
                outer._n_requests += 1
                if (outer.die_after_requests is not None
                        and outer._n_requests > outer.die_after_requests):
                    outer.alive = False
                if not outer.alive:
                    self._json(503, {"status": "down"})
                    return
                if self.path.startswith("/api/reset/"):
                    outer.x_state = {"likedPostIds": [],
                                     "retweetedPostIds": [],
                                     "bookmarkedPostIds": []}
                    self._json(200, {"status": "ok", "reset": True,
                                     "sid": self.path.rsplit("/", 1)[-1]})
                elif self.path.startswith("/api/inject_task/"):
                    outer.last_seed = body
                    # the real bridge deep-merges the store slice; the
                    # x.user toggle slices are the ones RM anchors seed
                    x_user = ((body.get("seed_state") or {}).get("x")
                              or {}).get("user") or {}
                    for key in ("likedPostIds", "bookmarkedPostIds",
                                "retweetedPostIds"):
                        if key in x_user:
                            outer.x_state[key] = list(x_user[key])
                    self._json(200, {"status": "ok",
                                     "sid": self.path.rsplit("/", 1)[-1],
                                     "task_id": body.get("task_id")})
                elif self.path.startswith("/api/act/"):
                    self._json(200, {"status": "ok", "detail": "tap"})
                else:
                    self._json(404, {"error": f"no route {self.path}"})

        return Handler

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def fake_bridge():
    bridge = FakeBridge()
    yield bridge
    bridge.close()


def _factory(fake: FakeBridge, **kw) -> MobileGymFactory:
    return MobileGymFactory(bridge_url=fake.url, connect_only=True, **kw)


def _spec(**kw) -> MobileGymTrialSpec:
    return MobileGymTrialSpec(fixture=TOP3_EXPENSE_TO_WECHAT, **kw)


# ── stage: bridge launch discipline (B-09 behavioural anchor) ───────────────

def test_bridge_argv_closed_whitelist():
    """The bridge launch line is built from a CLOSED flag whitelist — no
    injection flag (the legacy nested CUA loop) can ever appear on it."""
    factory = MobileGymFactory()
    argv = factory._bridge_argv(3021)
    flags = {a for a in argv if a.startswith("--")}
    assert flags <= _BRIDGE_ALLOWED_FLAGS
    joined = " ".join(argv)
    assert "taskvm.substrate.mobilegym.bridge" in joined
    assert "--port" in flags and "--sim-url" in flags


def test_bridge_module_source_has_no_cua_loop_injection():
    """The factory module never spells an injection flag — the RM runner
    structurally cannot ask the bridge for a nested CUA loop."""
    import taskvm_bench.evaluation.mobilegym_factory as mod
    src = open(mod.__file__, encoding="utf-8").read()
    assert "cua-loop" not in src and "cua_loop" not in src


def test_ensure_bridge_connects_to_healthy(fake_bridge):
    factory = _factory(fake_bridge)
    handle = factory.ensure_bridge()
    assert handle.started_by_factory is False
    assert handle.pid is None
    assert handle.instance_id.startswith("connected:")
    factory.close()          # must NOT touch a bridge it did not spawn
    assert factory._probe_health() is not None


def test_ensure_bridge_connect_only_missing(fake_bridge):
    """connect_only + no healthy bridge → honest error, never a spawn."""
    factory = MobileGymFactory(
        bridge_url="http://127.0.0.1:1",      # nothing listens here
        connect_only=True, bridge_startup_timeout_s=1.0)
    with pytest.raises(BridgeUnavailableError):
        factory.ensure_bridge()


def test_ensure_bridge_spawns_when_unhealthy(monkeypatch, fake_bridge):
    """No healthy bridge + connect_only=False → the factory spawns one
    (argv recorded; the fake 'subprocess' is the in-process server)."""
    spawned = {}

    class FakeProc:
        pid = 424242

        def poll(self):
            return None

    def fake_spawn(argv):
        spawned["argv"] = argv
        fake_bridge.alive = True            # the 'process' becomes healthy
        return FakeProc()

    factory = MobileGymFactory(
        bridge_url=fake_bridge.url, bridge_port=fake_bridge.port,
        bridge_startup_timeout_s=5.0)
    fake_bridge.alive = False               # first probe: unhealthy
    monkeypatch.setattr(factory, "_spawn_bridge", fake_spawn)
    # ensure the health probe flips only after spawn: keep-alive is set
    # inside fake_spawn, but the first probe already failed → spawn path
    handle = factory.ensure_bridge()
    assert handle.started_by_factory is True
    assert spawned["argv"][1:3] == ["-m", "taskvm.substrate.mobilegym.bridge"]
    assert spawned["argv"] == factory._bridge_argv(fake_bridge.port)


# ── stage: setup plane (reset / seed / hash) ────────────────────────────────

def test_setup_trial_reset_seed_and_hash(fake_bridge):
    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    sid = "top3-e0-s0"
    spec = MobileGymTrialSpec(fixture=TOP3_EXPENSE_TO_WECHAT, sid=sid)
    oracles = factory.build_oracles(spec.apps, sid)
    setup = factory.setup_trial(spec, oracles)
    assert any(p == f"/api/reset/{sid}" for m, p in fake_bridge.requests)
    assert any(p == f"/api/inject_task/{sid}"
               for m, p in fake_bridge.requests)
    assert fake_bridge.last_seed["task_id"] == TOP3_EXPENSE_TO_WECHAT.task_id
    assert fake_bridge.last_seed["seed_state"] == \
        TOP3_EXPENSE_TO_WECHAT.seed_state
    assert len(setup.reset_state_hash) == 64            # sha256 hex
    # deterministic: same world → same hash (the oracle is read-only)
    setup2 = factory.setup_trial(spec, oracles)
    assert setup2.reset_state_hash == setup.reset_state_hash


def test_setup_apps_and_surface(fake_bridge):
    spec = _spec()
    assert spec.apps == ("wechat",)          # top3 binds wechat only
    assert spec.surface_app == "wechat"
    assert spec.resolve_sid().endswith("-e0-s0")


# ── stage: the L1 session (the system-under-test's only face) ───────────────

def test_l1_session_observe_and_act(fake_bridge):
    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    session = factory.build_session("sid-l1", "wechat")
    surfaces = session.list_surfaces()
    assert len(surfaces) == 1
    obs = session.observe(surfaces[0])
    assert obs.fingerprint == "fake-fp-0001"
    shot = obs.screenshot_ref or ""
    assert shot.startswith("data:image/")
    receipt = session.act(surfaces[0],
                          GuiAction(kind="tap", coordinate=(500, 500)),
                          epoch="e1")
    assert receipt.status == "ok"
    assert any(p == "/api/act/sid-l1" for m, p in fake_bridge.requests)


# ── stage: bootstrap (caller of B-07 — parameter passthrough) ───────────────

def test_bootstrap_session_passes_substrate_through(fake_bridge):
    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    spec = _spec(sid="sid-bs", model="test-model")
    session = factory.build_session("sid-bs", "wechat")
    seen = {}

    def fake_bootstrap_fn(**kw):
        seen.update(kw)
        return {"sid": kw["sid"]}

    bundle = factory.bootstrap_session(
        spec, session=session, ledger="LEDGER", store="STORE",
        model_port="PORT", bootstrap_fn=fake_bootstrap_fn)
    assert bundle == {"sid": "sid-bs"}
    assert seen["goal"] == TOP3_EXPENSE_TO_WECHAT.goal
    assert seen["sid"] == "sid-bs"
    assert seen["substrate"] is session          # L1 session injected
    assert seen["ledger"] == "LEDGER" and seen["store"] == "STORE"
    assert seen["model"] == "test-model" and seen["model_port"] == "PORT"


# ── stage: driver + close + integrity (fake driver) ─────────────────────────

class FakeDriver:
    def __init__(self, verdict="applied"):
        self.executed: list[UserOp] = []
        self.verdict = verdict

    def execute(self, op: UserOp) -> OpOutcome:
        self.executed.append(op)
        return OpOutcome(op=op, verdict=self.verdict, http_status=200,
                         response={"faked": True})


def test_run_trial_full_chain_over_fakes(fake_bridge):
    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    driver = FakeDriver()
    spec = _spec(model="m1", condition="taskvm-real-full")
    record = factory.run_trial(
        spec, bootstrap_fn=lambda **kw: {"sid": kw["sid"]},
        driver=driver)
    # the default RM plumbing ops ran (start + honest stop)
    assert [op.kind for op in driver.executed] == ["start", "stop"]
    # per-op records landed on the B-05 trial record
    assert [op["kind"] for op in record.user_ops] == ["start", "stop"]
    # R1 semantics: no evidence recorder was injected, so no grade
    # landed — all-applied alone is HONESTLY "pending", never "pass"
    assert record.trial_verdict == "pending"
    assert record.failure_class == "ungraded"
    assert record.contract_verdict is None
    assert record.evaluation_error is None
    assert record.substrate == "mobilegym"
    assert record.condition == "taskvm-real-full"
    assert record.environment_seed == 0 and record.sample_index == 0
    assert record.development_only is True
    # B-10 manifest block (fields present, hash shapes honest)
    mf = factory.manifest_fields(spec)
    assert mf["bridge_instance_id"] and \
        mf["bridge_instance_id"].startswith("connected:")
    assert mf["sid"] == spec.resolve_sid()
    assert mf["environment_seed"] == 0
    assert len(mf["reset_state_hash"]) == 64
    assert mf["initial_state_fingerprint"] == "fake-fp-0001"
    assert mf["final_integrity_status"] == "ok"
    assert mf["final_state_hash"] == mf["reset_state_hash"]


def test_run_trial_integrity_unavailable_is_honest(fake_bridge):
    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    driver = FakeDriver()
    spec = _spec()
    record = factory.run_trial(
        spec, bootstrap_fn=lambda **kw: {"sid": kw["sid"]},
        driver=driver)
    fake_bridge.alive = False                    # bridge dies post-trial
    integrity = factory.integrity_check(spec, factory.last_oracles)
    assert integrity["status"] == "unavailable"
    assert "unhealthy" in integrity["detail"]


def test_user_ops_override(fake_bridge):
    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    driver = FakeDriver()
    record = factory.run_trial(
        _spec(), bootstrap_fn=lambda **kw: {},
        driver=driver,
        user_ops=[UserOp.pause("plumbing check")])
    assert [op.kind for op in driver.executed] == ["pause"]


# ── smoke-4 helper ──────────────────────────────────────────────────────────

def test_ledger_role_counts():
    class FakeLedger:
        def counts_by_role(self):
            return {"state_compiler": 2, "task_architect": 1, "cua": 5}
    assert ledger_role_counts(FakeLedger()) == {
        "state_compiler": 2, "task_architect": 1, "cua": 5}
    assert ledger_role_counts(None) == {
        "state_compiler": 0, "task_architect": 0, "cua": 0}


def test_trial_record_schema_is_b05():
    """The factory's record IS the B-05 schema object (no shadow type)."""
    from taskvm_bench.evaluation.results import SCHEMA_VERSION
    assert TrialRecord().schema_version == SCHEMA_VERSION


# ── the CLI surface (B-08) ──────────────────────────────────────────────────

def test_cli_parser_accepts_mobilegym_and_seed_semantics():
    from taskvm_bench.evaluation.cli import build_parser
    p = build_parser()
    # the target invocation from re-prompt §B-08 parses cleanly
    args = p.parse_args([
        "run", "--suite", "rm-smoke", "--substrate", "mobilegym",
        "--condition", "taskvm-real-full", "--model", "gpt-5.6-sol",
        "--samples", "1"])
    assert args.substrate == "mobilegym"
    assert args.condition == ["taskvm-real-full"]
    assert args.model == "gpt-5.6-sol"
    assert args.samples == 1 and args.env_seed == 0
    # B-07 (Task E): bare `run` no longer hard-defaults to world —
    # the substrate/suite defaults are resolved by
    # resolve_substrate_and_suite (mobilegym + rm-smoke; explicit world
    # keeps the legacy smoke default). The parser itself stays neutral.
    args_w = p.parse_args(["run"])
    assert args_w.substrate is None and args_w.suite is None
    assert args_w.seeds == 1
    with pytest.raises(SystemExit):
        p.parse_args(["run", "--substrate", "nosuch"])


def test_cli_mobilegym_run_routes_to_factory(monkeypatch, fake_bridge,
                                              tmp_path):
    import taskvm_bench.evaluation.mobilegym_factory as factory_mod
    from taskvm_bench.evaluation.cli import main

    calls = {}

    class RecordingFactory(factory_mod.MobileGymFactory):
        """Records the spec the CLI built; returns a pre-made record so
        this test stays about CLI ORCHESTRATION (spec passthrough,
        per-trial driver wiring, run-dir layout), not about the factory
        chain itself (covered above over the fake bridge)."""

        def run_trial(self, spec, **kw):
            calls["spec"] = spec
            calls["kw"] = kw
            assert kw["driver"] is not None      # real UserOpDriver wired
            assert kw["store"] is not None       # real store wired
            rec = factory_mod.UserOpTrialRecord(
                model=spec.model or "", substrate="mobilegym",
                condition=spec.condition,
                environment_seed=spec.environment_seed,
                sample_index=spec.sample_index)
            from taskvm_bench.evaluation.results import UserOpRecord
            rec.add_op(UserOpRecord(op_id="uop-0001", kind="start",
                                    verdict="applied"))
            rec.finalize()
            self.last_setup = factory_mod.TrialSetup(sid=spec.resolve_sid())
            self.last_integrity = {"status": "ok"}
            return rec

    monkeypatch.setattr(factory_mod, "MobileGymFactory", RecordingFactory)
    rc = main([
        "run", "--suite", "rm-smoke", "--substrate", "mobilegym",
        "--condition", "taskvm-real-full", "--model", "test-m",
        "--samples", "1",
        "--bridge-port", str(fake_bridge.port),
        "--projection-port", "0",
        "--out", str(tmp_path)])
    assert rc == 0
    assert calls["spec"].condition == "taskvm-real-full"   # verbatim
    assert calls["spec"].model == "test-m"
    assert calls["spec"].environment_seed == 0
    assert calls["spec"].sample_index == 0
    # the run directory landed (manifest + trial), gitignored plane
    import json as _json
    manifest = _json.load(open(
        tmp_path / "rm-mobilegym-run" / "manifest.json"
        if (tmp_path / "rm-mobilegym-run").exists() else
        _first_run_dir(tmp_path) / "manifest.json", encoding="utf-8"))
    assert manifest["substrate"] == "mobilegym"
    assert manifest["condition"] == "taskvm-real-full"
    assert manifest["development_only"] is True
    assert manifest["trials"][0]["sid"] == calls["spec"].resolve_sid()


def _first_run_dir(root):
    import os
    for name in sorted(os.listdir(root)):
        if os.path.isdir(os.path.join(root, name)):
            return root / name
    raise AssertionError("no run dir created")


# ── B-10: trial isolation + honest evaluation_error ───────────────────────

def test_trial_isolation_rejects_second_concurrent(fake_bridge):
    """A busy factory REFUSES a second trial — never a silent shared
    mutable foreground/session (one worker ⇔ one bridge instance)."""
    from taskvm_bench.evaluation.mobilegym_factory import TrialIsolationError
    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    factory._trial_lock.acquire()          # simulate an active trial
    factory._active_trial_key = "other/e0/s0"
    with pytest.raises(TrialIsolationError) as ei:
        factory.run_trial(_spec(), bootstrap_fn=lambda **kw: {},
                          driver=FakeDriver())
    assert "other/e0/s0" in str(ei.value)
    assert factory._busy_rejections == 1
    factory._trial_lock.release()
    # after release the next trial runs normally (R1: ungraded honest
    # pending — the grading plane has not spoken)
    rec = factory.run_trial(_spec(), bootstrap_fn=lambda **kw: {},
                            driver=FakeDriver())
    assert rec.trial_verdict == "pending"


def test_trial_isolation_true_concurrency_is_rejected(fake_bridge):
    """Two REAL concurrent run_trial threads: exactly one wins, the
    other gets the honest busy error (never a shared session)."""
    import threading as _t
    from taskvm_bench.evaluation.mobilegym_factory import TrialIsolationError
    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    results = []

    class SlowDriver(FakeDriver):
        def execute(self, op):
            time.sleep(0.4)              # hold the trial gate open
            return super().execute(op)

    def run():
        try:
            results.append(factory.run_trial(
                _spec(), bootstrap_fn=lambda **kw: {},
                driver=SlowDriver()))
        except TrialIsolationError as e:
            results.append(e)

    t1 = _t.Thread(target=run)
    t1.start()
    time.sleep(0.15)                       # t1 is inside its slow trial
    try:
        factory.run_trial(_spec(), bootstrap_fn=lambda **kw: {},
                          driver=FakeDriver())
        results.append("unexpected-no-error")
    except TrialIsolationError as e:
        results.append(e)
    t1.join(timeout=10)
    assert "TrialIsolationError" in [type(r).__name__ for r in results]
    assert any(isinstance(r, TrialRecord) for r in results)
    assert factory._busy_rejections >= 1


def test_trial_isolation_bounded_wait_queues(fake_bridge):
    """``wait_for_trial_lock > 0``: the second trial QUEUES instead of
    being refused — both complete, strictly serially."""
    import threading as _t
    factory = MobileGymFactory(
        bridge_url=fake_bridge.url, connect_only=True,
        wait_for_trial_lock=10.0)
    factory.ensure_bridge()
    order = []
    lock = _t.Lock()

    class SlowDriver(FakeDriver):
        def execute(self, op):
            with lock:
                order.append(("start", op.op_id))
            time.sleep(0.25)
            r = super().execute(op)
            with lock:
                order.append(("end", op.op_id))
            return r

    def run(tag):
        factory.run_trial(_spec(sid=f"sid-{tag}"),
                          bootstrap_fn=lambda **kw: {},
                          driver=SlowDriver())

    t1 = _t.Thread(target=run, args=("a",))
    t1.start()
    time.sleep(0.1)
    t2 = _t.Thread(target=run, args=("b",))
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    # strictly serial: every 'start' is followed by its own 'end'
    # before the next 'start' begins
    events = [e for kind, e in order if True]
    kinds = [k for k, _ in order]
    assert kinds[0] == "start" and kinds[-1] == "end"
    assert kinds.count("start") == 4      # 2 trials × (start+stop ops)
    # no interleaving: start,end,start,end,...
    depth = 0
    for k in kinds:
        depth += 1 if k == "start" else -1
        assert depth in (0, 1)


def test_reset_invariant_hash_instability_is_evaluation_error(fake_bridge):
    """Two consecutive oracle reads DISAGREE after seed → the world
    drifted during setup → evaluation_error, SUT stages SKIPPED, never
    a system crash and never a success."""
    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    calls = {"n": 0}

    def drifting(state):
        calls["n"] += 1
        if calls["n"] >= 2:               # second oracle read differs
            import copy
            s = copy.deepcopy(state)
            s["wechat_chats"][0]["n_messages"] = 99
            return s
        return state

    fake_bridge.oracle_mutator = drifting
    driver = FakeDriver()
    record = factory.run_trial(
        _spec(), bootstrap_fn=lambda **kw: {"never": "called"},
        driver=driver)
    assert not driver.executed            # SUT stages skipped
    assert record.user_ops == []
    assert record.evaluation_error and "not stable" in record.evaluation_error
    assert record.trial_verdict == "error"   # NOT a success
    assert factory.last_integrity["status"] == "skipped"
    mf = factory.manifest_fields(_spec())
    assert mf["isolation"]["invariant_violation"]


def test_seed_entity_missing_is_evaluation_error(fake_bridge):
    """The fixture's seeded chat is NOT visible in the oracle → the
    seed did not land → evaluation_error naming the missing entity."""
    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    fake_bridge.state = {"wechat_chats": []}      # seed did not land
    driver = FakeDriver()
    record = factory.run_trial(
        _spec(), bootstrap_fn=lambda **kw: {"never": "called"},
        driver=driver)
    assert not driver.executed
    assert record.evaluation_error
    assert "wxid_huangyong_demo" in record.evaluation_error
    assert record.trial_verdict == "error"


def test_integrity_failure_forces_error_verdict(fake_bridge):
    """Post-trial integrity unavailable → evaluation_error AND verdict
    error even though every op verdict was 'applied' (a green op list
    can never launder a broken world)."""
    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    # request order: health(1) reset(2) seed(3) oracle(4,5) observe(6)
    # — the L1 observe must still succeed; the bridge dies only for the
    # post-trial integrity probes (health 7, …) → honest unavailable.
    fake_bridge.die_after_requests = 6
    record = factory.run_trial(
        _spec(), bootstrap_fn=lambda **kw: {}, driver=FakeDriver())
    assert record.evaluation_error and "unavailable" in record.evaluation_error
    assert record.trial_verdict == "error"
    assert all(op["verdict"] == "applied" for op in record.user_ops)
    assert factory.manifest_fields(_spec())["final_integrity_status"] == \
        "unavailable"


def test_cli_mobilegym_rejects_unknown_suite():
    from taskvm_bench.evaluation.cli import main
    with pytest.raises(SystemExit):
        main(["run", "--suite", "final", "--substrate", "mobilegym",
              "--bridge-port", "1"])


# ── RM-C04-01 anchor: end-to-end integration over FakeBridge ───────────────
#
# The anchor task (bench_design §九, "social_mark_and_true_rollback"):
# on the X app, find the post mentioning 核心CPI下降, like AND bookmark
# it, then roll the whole thing back through a real checkpoint.
#
# This test exercises the FULL R1 grader loop through the MobileGymFactory:
#   1. MobileGymFixtureView adapter → TaskSpec-speaking fixture
#   2. EvidenceRecorder auto-built from .spec + .oracle_read
#   3. Deferred rollback op (_rollback_to_first_checkpoint) resolving
#      checkpoint_id from the prior op's public HTTP response
#   4. Five-field ContractVerdict landing on the TrialRecord
#   5. Witness predicate: the forward work (like+bookmark) MUST appear
#      on the oracle timeline even though the final state == seed
#   6. Protected predicate: the pre-bookmarked sibling post survives
#      untouched across the whole arc


from taskvm_bench.benchmark.rm_anchor_tasks import (
    RM_C04_01,
    mobilegym_fixture_view,
    rm_c04_01_user_ops,
    CPI_POST_ID,
    PROTECTED_POST_ID,
)


class ScriptedXDriver:
    """A scripted driver that simulates the CUA doing real GUI work
    (like + bookmark on the CPI post) between checkpoint and rollback.

    The FakeBridge holds the X store slice (toggle id lists). This driver
    mutates that store to simulate GUI actions landing, then restores it
    on rollback (simulating reverse GUI gestures — toggle-like /
    toggle-bookmark are reversible store toggles).

    The checkpoint op returns a real ``checkpoint_id`` in its public HTTP
    response — the deferred rollback entry resolves it from
    ``previous_outcomes`` (no hidden id source).
    """

    def __init__(self, bridge: FakeBridge) -> None:
        self.bridge = bridge
        self.executed: list[UserOp] = []
        self._checkpoint_counter = 0
        #: the X store snapshot at checkpoint time (for honest rollback)
        self._ckpt_store: dict | None = None

    def execute(self, op: UserOp) -> OpOutcome:
        self.executed.append(op)
        if op.kind == "checkpoint":
            self._checkpoint_counter += 1
            cid = f"ckpt-{self._checkpoint_counter}"
            # snapshot the X store at checkpoint time
            self._ckpt_store = {
                "likedPostIds": list(self.bridge.x_state["likedPostIds"]),
                "retweetedPostIds": list(self.bridge.x_state["retweetedPostIds"]),
                "bookmarkedPostIds": list(self.bridge.x_state["bookmarkedPostIds"]),
            }
            return OpOutcome(
                op=op, verdict="applied", http_status=200,
                response={"checkpoint_id": cid, "label": op.payload.get("label", "")},
                sse_window=[{"sse_type": "governance.applied", "event_id": "e1"}],
                projection_before={"revision": 1, "variables": {}},
                projection_after={"revision": 2, "variables": {}},
            )
        if op.kind == "start":
            # simulate the CUA doing the forward work: like + bookmark
            # the CPI post (the witness values)
            if CPI_POST_ID not in self.bridge.x_state["likedPostIds"]:
                self.bridge.x_state["likedPostIds"].append(CPI_POST_ID)
            if CPI_POST_ID not in self.bridge.x_state["bookmarkedPostIds"]:
                self.bridge.x_state["bookmarkedPostIds"].append(CPI_POST_ID)
            return OpOutcome(
                op=op, verdict="applied", http_status=200,
                response={"ok": True},
                sse_window=[
                    {"sse_type": "action.observed", "event_id": "e2"},
                    {"sse_type": "action.landed", "event_id": "e3"},
                    {"sse_type": "governance.applied", "event_id": "e4"},
                ],
                projection_before={"revision": 2, "variables": {}},
                projection_after={"revision": 3, "variables": {}},
            )
        if op.kind == "rollback":
            # simulate reverse GUI gestures: remove the like + bookmark
            # (toggle-like / toggle-bookmark are reversible store toggles)
            cid = op.payload.get("target_checkpoint_id", "")
            if self._ckpt_store is not None:
                self.bridge.x_state["likedPostIds"] = \
                    list(self._ckpt_store["likedPostIds"])
                self.bridge.x_state["bookmarkedPostIds"] = \
                    list(self._ckpt_store["bookmarkedPostIds"])
                self.bridge.x_state["retweetedPostIds"] = \
                    list(self._ckpt_store["retweetedPostIds"])
            return OpOutcome(
                op=op, verdict="applied", http_status=200,
                response={"ok": True, "disposition": "complete",
                          "checkpoint_id": cid},
                sse_window=[
                    {"sse_type": "action.observed", "event_id": "e5"},
                    {"sse_type": "action.landed", "event_id": "e6"},
                    {"sse_type": "governance.applied", "event_id": "e7"},
                ],
                projection_before={"revision": 3, "variables": {}},
                projection_after={"revision": 4, "variables": {}},
            )
        if op.kind == "stop":
            return OpOutcome(
                op=op, verdict="applied", http_status=200,
                response={"ok": True},
                sse_window=[{"sse_type": "governance.applied", "event_id": "e8"}],
                projection_before={"revision": 4, "variables": {}},
                projection_after={"revision": 5, "variables": {}},
            )
        # default: applied
        return OpOutcome(op=op, verdict="applied", http_status=200,
                         response={"ok": True})


def _cua_ledger(model: str = "test-model"):
    """A ledger double with one landed CUA row. In a real trial the CUA
    model calls inside the execution stage land these rows themselves;
    the scripted driver REPLACES the model, so the test injects the row
    — the grader's ledger-integrity check then sees real telemetry
    backing the GUI actions (record()'s real signature takes a
    ModelCallRecord, taskvm/architect/port.py)."""
    from taskvm.architect import (
        MODEL_ROLE_CUA, ModelCallLedger, ModelCallRecord)
    ledger = ModelCallLedger()
    ledger.record(ModelCallRecord(
        role=MODEL_ROLE_CUA, purpose="trial_gesture",
        model=model, ok=True))
    return ledger


def test_rm_c04_01_deferred_rollback_resolves_checkpoint_id(fake_bridge):
    """The deferred rollback entry resolves ``checkpoint_id`` from the
    FIRST checkpoint op's public HTTP response — no hidden id source.

    Verifies the callable ``_rollback_to_first_checkpoint`` correctly
    scans ``previous_outcomes`` for a checkpoint kind op and extracts
    the ``checkpoint_id`` field from its response."""
    from taskvm_bench.benchmark.rm_anchor_tasks import \
        _rollback_to_first_checkpoint

    ckpt_op = UserOp.checkpoint("C0")
    ckpt_outcome = OpOutcome(
        op=ckpt_op, verdict="applied", http_status=200,
        response={"checkpoint_id": "ckpt-1", "label": "C0"})
    # the deferred op resolves from prior outcomes
    resolved = _rollback_to_first_checkpoint([ckpt_outcome])
    assert resolved.kind == "rollback"
    assert resolved.payload["target_checkpoint_id"] == "ckpt-1"

    # no checkpoint in prior outcomes → honest ValueError
    start_outcome = OpOutcome(op=UserOp.start(), verdict="applied")
    with pytest.raises(ValueError, match="checkpoint"):
        _rollback_to_first_checkpoint([start_outcome])


def test_rm_c04_01_full_chain_e2e(fake_bridge):
    """RM-C04-01 end-to-end: checkpoint → start (forward work) →
    rollback (reverse GUI) → stop, graded by the R1 deterministic grader.

    The trial MUST pass: the final state equals the seed (rollback
    restored both effects), the witness values appeared (like+bookmark
    held on the CPI post at the start op's after-state), the protected
    post survived untouched, and GUI compensation happened inside the
    rollback bracket.
    """
    fixture = mobilegym_fixture_view(RM_C04_01)
    spec = MobileGymTrialSpec(
        fixture=fixture, sid="rm-c04-01-e0-s0", model="test-model",
        condition="taskvm-real-full")
    driver = ScriptedXDriver(fake_bridge)
    factory = _factory(fake_bridge)
    factory.ensure_bridge()

    # seed the protected post as pre-bookmarked (the fixture's seed_state
    # expresses this as the x.user.bookmarkedPostIds store slice)
    # — the FakeBridge's /api/inject_task/ handler already deep-merges
    # this from the seed_state the factory sends during setup_trial

    # inject a ledger with a landed CUA row — the ScriptedXDriver
    # simulates GUI actions (action.observed/action.landed SSE); the
    # grader's ledger-integrity check requires telemetry to back them
    # (in a real trial the CUA model calls land these rows themselves)
    ledger = _cua_ledger()

    record = factory.run_trial(
        spec,
        bootstrap_fn=lambda **kw: {"sid": kw["sid"]},
        driver=driver,
        user_ops=rm_c04_01_user_ops(),
        ledger=ledger,
    )

    # ── 1. the op program ran in the correct order ──────────────────
    assert [op.kind for op in driver.executed] == \
        ["checkpoint", "start", "rollback", "stop"]

    # ── 2. the deferred rollback resolved the checkpoint_id ──────────
    rollback_op = driver.executed[2]
    assert rollback_op.kind == "rollback"
    assert rollback_op.payload["target_checkpoint_id"] == "ckpt-1"

    # ── 3. per-op records landed on the TrialRecord ──────────────────
    assert [op["kind"] for op in record.user_ops] == \
        ["checkpoint", "start", "rollback", "stop"]
    assert all(op["verdict"] == "applied" for op in record.user_ops)

    # ── 4. the R1 grader produced a five-field verdict ───────────────
    assert record.contract_verdict is not None
    cv = record.contract_verdict
    assert set(cv) >= {
        "world_contract", "governance_contract",
        "projection_consistency", "progress", "failure_codes",
        "passed"}

    # ── 5. the trial PASSED ───────────────────────────────────────────
    # The final state == seed (rollback restored both effects), the
    # witness values appeared (like+bookmark on CPI post at start's
    # after-state), the protected post survived, GUI compensation
    # happened in the rollback bracket.
    assert record.trial_verdict == "pass", (
        f"expected pass, got {record.trial_verdict}; "
        f"failure_codes={cv.get('failure_codes')}; "
        f"world={cv.get('world_contract', {}).get('failed_codes')}; "
        f"gov={cv.get('governance_contract', {}).get('failed_codes')}; "
        f"proj={cv.get('projection_consistency', {}).get('failed_codes')}; "
        f"progress={cv.get('progress', {}).get('failed_codes')}")
    assert cv["passed"] is True
    assert cv["failure_codes"] == []

    # ── 6. evidence bundle landed on the factory ──────────────────────
    bundle = factory.last_evidence
    assert bundle is not None
    assert bundle.task_id == RM_C04_01.task_id

    # ── 7. witness: the forward work appeared on the oracle timeline ─
    timeline = bundle.oracle_timeline()
    # find the start op's after-state — the CPI post should be
    # liked AND bookmarked at that point
    start_after = None
    for label, state in timeline:
        if "start" in label and "after" in label:
            start_after = state
            break
    assert start_after is not None, "start op after-state not found"
    x_rows = start_after.get("x", {})
    assert x_rows.get(f"{CPI_POST_ID}.liked") == "true"
    assert x_rows.get(f"{CPI_POST_ID}.bookmarked") == "true"

    # ── 8. protected: the sibling post survived untouched ─────────────
    final_state = bundle.oracle_final
    x_final = final_state.get("x", {})
    assert x_final.get(f"{PROTECTED_POST_ID}.bookmarked") == "true"
    assert x_final.get(f"{PROTECTED_POST_ID}.liked") == "false"

    # ── 9. the final state == the seed (rollback truly restored) ──────
    seed_state = bundle.oracle_seed
    x_seed = seed_state.get("x", {})
    for key, expected in x_seed.items():
        assert x_final.get(key) == expected, \
            f"final state mismatch on {key}: " \
            f"seed={expected}, final={x_final.get(key)}"

    # ── 10. GUI compensation in the rollback bracket ──────────────────
    rollback_brackets = bundle.rollback_brackets()
    assert len(rollback_brackets) == 1
    rb = rollback_brackets[0]
    assert rb.gui_actions >= 2  # action.observed + action.landed

    # ── 11. checkpoint bracket exists and has the right response ─────
    ckpt_brackets = bundle.checkpoint_brackets()
    assert len(ckpt_brackets) == 1
    assert ckpt_brackets[0].response.get("checkpoint_id") == "ckpt-1"


def test_rm_c04_01_no_rollback_fails_witness(fake_bridge):
    """Without the rollback (forward work stays), the final state does
    NOT match the frozen success state → world_contract fails.

    This verifies the witness predicate: a system that does the forward
    work but never rolls back cannot pass — the success state requires
    the effects to be restored."""
    fixture = mobilegym_fixture_view(RM_C04_01)
    spec = MobileGymTrialSpec(
        fixture=fixture, sid="rm-c04-01-no-rb", model="test-model")
    driver = ScriptedXDriver(fake_bridge)
    factory = _factory(fake_bridge)
    factory.ensure_bridge()

    # run WITHOUT the rollback op — just checkpoint, start, stop
    record = factory.run_trial(
        spec,
        bootstrap_fn=lambda **kw: {"sid": kw["sid"]},
        driver=driver,
        user_ops=[
            UserOp.checkpoint("C0"),
            UserOp.start(),
            UserOp.stop(),
        ],
    )
    # the forward work landed (like+bookmark) but the rollback never
    # ran → the final state has CPI post liked+bookmarked, NOT the
    # frozen success state (which requires them false) → FAIL
    assert record.trial_verdict == "fail"
    assert record.contract_verdict is not None
    assert not record.contract_verdict["passed"]
    # the world contract must have caught the mismatch
    assert "WORLD_REQUIRED_WRITE_MISSING" in \
        record.contract_verdict["failure_codes"]


def test_rm_c04_01_noop_fails_witness(fake_bridge):
    """A pure no-op trial (checkpoint + stop, no forward work) must
    FAIL the witness predicate: the like+bookmark values never appeared
    on the oracle timeline → the no-op loophole stays closed."""
    fixture = mobilegym_fixture_view(RM_C04_01)
    spec = MobileGymTrialSpec(
        fixture=fixture, sid="rm-c04-01-noop", model="test-model")
    driver = ScriptedXDriver(fake_bridge)
    factory = _factory(fake_bridge)
    factory.ensure_bridge()

    record = factory.run_trial(
        spec,
        bootstrap_fn=lambda **kw: {"sid": kw["sid"]},
        driver=driver,
        user_ops=[
            UserOp.checkpoint("C0"),
            UserOp.stop(),
        ],
    )
    assert record.trial_verdict == "fail"
    assert "WORLD_WITNESS_MISSING" in \
        record.contract_verdict["failure_codes"]


def test_rm_c04_01_protected_violation_detected(fake_bridge):
    """A sloppy CUA whose tamper on the protected post SURVIVES to the
    post-trial snapshot → the non-interference predicate catches it.

    The frozen semantics (schema.py): a protected field must be
    byte-identical between the PRE-trial and POST-trial snapshots. A
    transient tamper that a CORRECT rollback restores (final == seed)
    does not fire the code — the checkpoint restore legitimately brings
    the field back. So the tamper here lands AFTER the rollback, during
    the CUA's post-rollback verification sweep: the sibling post's
    bookmark toggle gets sloppily hit and the violation is still in
    place at trial end → FAIL."""
    fixture = mobilegym_fixture_view(RM_C04_01)
    spec = MobileGymTrialSpec(
        fixture=fixture, sid="rm-c04-01-prot", model="test-model")

    class DirtyDriver(ScriptedXDriver):
        # tamper during the STOP op (after the rollback): a tamper in
        # the forward window would be restored by the checkpoint
        # rollback — final would equal seed and the frozen snapshot
        # comparison would rightly NOT fire
        def execute(self, op):
            outcome = super().execute(op)
            if op.kind == "stop":
                # the sloppy verification sweep hits the sibling post's
                # bookmark toggle — the tamper survives to trial end
                if PROTECTED_POST_ID in \
                        self.bridge.x_state["bookmarkedPostIds"]:
                    self.bridge.x_state["bookmarkedPostIds"].remove(
                        PROTECTED_POST_ID)
            return outcome

    driver = DirtyDriver(fake_bridge)
    factory = _factory(fake_bridge)
    factory.ensure_bridge()

    record = factory.run_trial(
        spec,
        bootstrap_fn=lambda **kw: {"sid": kw["sid"]},
        driver=driver,
        user_ops=rm_c04_01_user_ops(),
        ledger=_cua_ledger(),
    )
    assert record.trial_verdict == "fail"
    # the protected field changed between the pre-trial and post-trial
    # snapshots (bookmarked: true → false)
    assert "WORLD_PROTECTED_CHANGED" in \
        record.contract_verdict["failure_codes"]
    # the ledger double backs the GUI actions — integrity is NOT
    # broken: the failure is attributed to the world contract, not to
    # missing telemetry
    assert "LEDGER_INTEGRITY_BROKEN" not in \
        record.contract_verdict["failure_codes"]
