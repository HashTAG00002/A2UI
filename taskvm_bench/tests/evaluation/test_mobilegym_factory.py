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
                    self._json(200, {"status": "ok", "reset": True,
                                     "sid": self.path.rsplit("/", 1)[-1]})
                elif self.path.startswith("/api/inject_task/"):
                    outer.last_seed = body
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
