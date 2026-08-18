"""Task C/D — the OPEN prototype launcher (``demo_open``) contract tests.

Deterministic, no real bridge / no real sim / no real model calls:

* Task C — ``--substrate {mobilegym,builtin_web}``, default mobilegym;
  both routes go through ``substrate_registry.create_session`` and the
  same ``bootstrap_real_full``; the mobilegym glue is fail-closed with
  explicit what-is-missing messages and NO builtin_web fallback; the
  optional ``--start-bridge`` spawns from the CLOSED B-09 flag whitelist
  (no CUA-loop injection flag can ever appear).
* Task D — evidence that the open launcher and the benchmark factory
  consume the SAME composition entry: ``demo_open``'s module-level
  bootstrap is (``is``) ``taskvm.workspace_ui.composition.
  bootstrap_real_full``, and the bench factory's default lazy import
  targets the very same function (asserted against its source, which the
  test reads as a FILE — tests here never import ``taskvm_bench``; the
  repo-wide gate ``tests/architecture/test_import_boundaries.py::
  test_taskvm_never_imports_taskvm_bench_repo_wide`` owns the reverse
  direction).
"""
from __future__ import annotations

import argparse
import subprocess

import pytest

from taskvm.runtime.ports import Observation
from taskvm.substrate import SurfaceInfo
from taskvm.substrate.port import SubstrateUnavailable
from taskvm.workspace_ui import demo_open
from taskvm.workspace_ui.composition import bootstrap_real_full


# ── helpers ──────────────────────────────────────────────────────────────────

class FakeProc:
    def __init__(self, *, rc_while_starting: int | None = None):
        self._rc = rc_while_starting
        self.terminated = False
        self.killed = False

    @property
    def returncode(self) -> int | None:
        return self._rc

    def poll(self) -> int | None:
        return self._rc

    def terminate(self) -> None:
        self.terminated = True
        self._rc = -15

    def kill(self) -> None:
        self.killed = True
        self._rc = -9

    def wait(self, timeout: float | None = None) -> int:
        return self._rc or 0


class FakeSession:
    """Duck-typed SubstrateSession — list_surfaces + observe succeed."""

    def __init__(self, *, observe_error: Exception | None = None):
        self.observe_error = observe_error
        self.observed: list = []

    def list_surfaces(self):
        return [SurfaceInfo(surface_id="s:1", display_name="One",
                            surface_kind="app")]

    def observe(self, surface=None, previous_fingerprint=None):
        if self.observe_error is not None:
            raise self.observe_error
        self.observed.append(surface)
        return Observation(surface=self.list_surfaces()[0], revision=1,
                           timestamp=0.0, visible_text="", fingerprint="fp")


def _args(**kw) -> argparse.Namespace:
    base = dict(goal="g", substrate="mobilegym", sid="open-demo",
                host="127.0.0.1", port=3016, app=None, seed_json=None,
                app_host="127.0.0.1", app_port=None, bridge_url=None,
                bridge_port=3019, sim_url="http://localhost:3000",
                start_bridge=False, offline=False, model=None)
    base.update(kw)
    ns = argparse.Namespace(**base)
    if ns.app is None:
        ns.app = demo_open._APP_DEFAULTS[ns.substrate]
    return ns


# ── Task C: argparse surface ─────────────────────────────────────────────────

def test_default_substrate_is_mobilegym_and_builtin_web_available():
    args = demo_open._build_arg_parser().parse_args(["--goal", "g"])
    assert args.substrate == "mobilegym"
    choices = demo_open._build_arg_parser()._actions
    substrate_action = next(a for a in choices
                            if a.dest == "substrate")
    assert set(substrate_action.choices) == {"mobilegym", "builtin_web"}


def test_app_default_is_substrate_owned():
    assert demo_open._APP_DEFAULTS == {"mobilegym": "wechat",
                                       "builtin_web": "calendar"}


def test_unknown_substrate_is_rejected_by_argparse():
    with pytest.raises(SystemExit):
        demo_open._build_arg_parser().parse_args(
            ["--goal", "g", "--substrate", "osworld"])


# ── Task C: bridge glue is fail-closed, closed-whitelist, no fallback ────────

def test_bridge_argv_closed_whitelist_no_cua_loop_injection():
    argv = demo_open._spawn_bridge_argv(_args())
    flags = {a for a in argv if a.startswith("--")}
    assert flags <= {"--port", "--sim-url", "--screenshot-dir"}
    assert "--cua-loop" not in flags and "--headed" not in flags
    assert argv[:3] == [demo_open.sys.executable, "-m",
                        "taskvm.substrate.mobilegym.bridge"]


def test_ensure_bridge_connects_to_healthy_bridge_without_spawning(monkeypatch):
    monkeypatch.setattr(demo_open, "_http_ok", lambda url, timeout=1.5: True)
    spawned = demo_open._ensure_mobilegym_bridge(_args())
    assert spawned is None          # connected — launcher owns nothing


def test_ensure_bridge_missing_without_start_bridge_fails_closed(monkeypatch):
    monkeypatch.setattr(demo_open, "_http_ok",
                        lambda url, timeout=1.5: False)
    calls: list = []
    monkeypatch.setattr(
        demo_open.subprocess, "Popen",
        lambda *a, **k: calls.append((a, k)) or FakeProc())
    with pytest.raises(SystemExit) as ei:
        demo_open._ensure_mobilegym_bridge(_args())
    msg = str(ei.value)
    assert "no healthy MobileGym bridge" in msg
    assert "--start-bridge" in msg            # tells the user what to do
    assert "taskvm.substrate.mobilegym.bridge" in msg
    assert not calls                          # no secret spawn, no fallback


def test_ensure_bridge_spawn_becomes_healthy_and_is_owned(monkeypatch):
    state = {"healthy": False}
    monkeypatch.setattr(
        demo_open, "_http_ok",
        lambda url, timeout=1.5: state["healthy"])
    argv_seen: list = []
    proc = FakeProc()

    def fake_spawn(argv, **kw):
        argv_seen.append((tuple(argv), kw))
        state["healthy"] = True               # healthy right after spawn
        return proc

    owned = demo_open._ensure_mobilegym_bridge(_args(start_bridge=True),
                                               spawn=fake_spawn)
    assert owned is proc
    assert argv_seen and argv_seen[0][1]["cwd"] == demo_open._REPO_ROOT
    assert "PYTHONPATH" in argv_seen[0][1]["env"]


def test_ensure_bridge_spawn_dies_early_fails_closed(monkeypatch):
    monkeypatch.setattr(demo_open, "_http_ok",
                        lambda url, timeout=1.5: False)
    with pytest.raises(SystemExit) as ei:
        demo_open._ensure_mobilegym_bridge(
            _args(start_bridge=True, sim_url="http://nope:1"),
            spawn=lambda argv, **kw: FakeProc(rc_while_starting=1))
    assert "exited rc=1" in str(ei.value)
    assert "sim" in str(ei.value).lower()     # names the likely cause


def test_ensure_bridge_spawn_never_healthy_terminates_and_fails(monkeypatch):
    monkeypatch.setattr(demo_open, "_http_ok",
                        lambda url, timeout=1.5: False)
    proc = FakeProc()

    class Clock:                              # fast-forward past deadline
        t = 0.0

        def __call__(self):
            Clock.t += 1000.0
            return Clock.t

    with pytest.raises(SystemExit) as ei:
        demo_open._ensure_mobilegym_bridge(
            _args(start_bridge=True), spawn=lambda argv, **kw: proc,
            sleep=lambda s: None, monotonic=Clock())
    assert "did not become healthy" in str(ei.value)
    assert proc.terminated                    # the owned spawn is cleaned up


# ── Task C: both substrates route through the ONE registry entry ─────────────

def test_mobilegym_route_uses_registry_and_probes_observe(monkeypatch):
    seen: list = []
    session = FakeSession()

    def fake_create(name, cfg):
        seen.append((name, cfg))
        return session

    monkeypatch.setattr(demo_open.substrate_registry, "create_session",
                        fake_create)
    substrate, world = demo_open._open_substrate(_args(sid="s1", app="x"))
    assert substrate is session
    assert seen == [("mobilegym", {"sid": "s1",
                                   "bridge_url": "http://127.0.0.1:3019",
                                   "app": "x"})]
    assert session.observed                  # the fail-fast probe ran
    assert "mobilegym" in world


def test_mobilegym_probe_inactive_sid_fails_closed_with_activation_hint(
        monkeypatch):
    monkeypatch.setattr(
        demo_open.substrate_registry, "create_session",
        lambda name, cfg: FakeSession(observe_error=SubstrateUnavailable(
            "mobilegym bridge unreachable at http://127.0.0.1:3019: "
            "409 Client Error: session mismatch: active='other'")))
    with pytest.raises(SystemExit) as ei:
        demo_open._open_substrate(_args(sid="s1"))
    msg = str(ei.value)
    assert "not activated" in msg
    assert "/api/reset/s1" in msg             # the concrete unblock step
    assert "NO reset/seed power" in msg       # honest capability boundary


def test_mobilegym_probe_sim_down_fails_closed_with_sim_hint(monkeypatch):
    monkeypatch.setattr(
        demo_open.substrate_registry, "create_session",
        lambda name, cfg: FakeSession(observe_error=SubstrateUnavailable(
            "mobilegym bridge unreachable: connection refused")))
    with pytest.raises(SystemExit) as ei:
        demo_open._open_substrate(_args())
    msg = str(ei.value)
    assert "sim" in msg.lower()
    assert "--sim-url" in msg
    assert "no fallback substrate" in msg     # never silently switches


def test_builtin_web_route_uses_registry_with_explicit_base_url(monkeypatch):
    seen: list = []
    monkeypatch.setattr(demo_open.substrate_registry, "create_session",
                        lambda name, cfg: seen.append((name, cfg))
                        or FakeSession())
    monkeypatch.setattr(demo_open, "_wait_app", lambda url, what: None)
    monkeypatch.setattr(demo_open, "_seed_app",
                        lambda url, sid, seed: None)
    substrate, world = demo_open._open_substrate(
        _args(substrate="builtin_web", app="calendar", app_port=3001))
    name, cfg = seen[0]
    assert name == "builtin_web"
    assert cfg == {"app": "calendar", "host": "127.0.0.1",
                   "sid": "open-demo", "base_url": "http://127.0.0.1:3001"}
    assert "calendar" in world


# ── Task C: end-to-end main() wiring (fake registry/serve/model key) ─────────

def test_main_mobilegym_end_to_end_passes_substrate_to_bootstrap(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(demo_open, "_http_ok",
                        lambda url, timeout=1.5: True)
    monkeypatch.setattr(demo_open.substrate_registry, "create_session",
                        lambda name, cfg: session)
    boot_seen: list = []

    def fake_bootstrap(**kw):
        boot_seen.append(kw)
        return {"sid": kw["sid"]}

    monkeypatch.setattr(demo_open, "bootstrap_real_full", fake_bootstrap)

    class FakeApp:
        def run(self, **kw):
            pass                              # never actually serve

    monkeypatch.setattr(demo_open, "serve", lambda store: FakeApp())
    monkeypatch.setattr(demo_open.os.environ, "get",
                        lambda k, d=None: "key" if k == "OPENAI_API_KEY"
                        else d)

    demo_open.main(["--goal", "给妈妈发一条微信", "--sid", "open1"])
    assert boot_seen and boot_seen[0]["substrate"] is session
    assert boot_seen[0]["goal"] == "给妈妈发一条微信"
    assert boot_seen[0]["sid"] == "open1"
    assert boot_seen[0]["cua_model"] is None   # not offline -> real CUA


def test_main_offline_uses_honest_fail_placeholder(monkeypatch):
    monkeypatch.setattr(demo_open, "_http_ok",
                        lambda url, timeout=1.5: True)
    monkeypatch.setattr(demo_open.substrate_registry, "create_session",
                        lambda name, cfg: FakeSession())

    def fake_bootstrap(**kw):
        assert isinstance(kw["cua_model"], demo_open._OfflineCUA)
        return {}

    monkeypatch.setattr(demo_open, "bootstrap_real_full", fake_bootstrap)

    class FakeApp:
        def run(self, **kw):
            pass

    monkeypatch.setattr(demo_open, "serve", lambda store: FakeApp())
    demo_open.main(["--goal", "g", "--offline"])   # no key needed offline


def test_main_spawned_bridge_is_terminated_on_exit(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "launcher-test")
    monkeypatch.setattr(
        demo_open, "_http_ok",
        lambda url, timeout=1.5: url.endswith("/health"))
    proc = FakeProc()
    monkeypatch.setattr(demo_open, "_ensure_mobilegym_bridge",
                        lambda args: proc)
    monkeypatch.setattr(demo_open, "_open_substrate",
                        lambda args: (FakeSession(), "fake world"))
    monkeypatch.setattr(demo_open, "bootstrap_real_full", lambda **kw: {})

    class FakeApp:
        def run(self, **kw):
            pass

    monkeypatch.setattr(demo_open, "serve", lambda store: FakeApp())
    demo_open.main(["--goal", "g", "--start-bridge"])
    assert proc.terminated                     # finally-clause owned cleanup


# ── Task D: single open execution path — UI and bench, ONE composition ───────

def test_demo_open_bootstrap_is_the_composition_root_entry():
    """The open launcher consumes exactly ``bootstrap_real_full`` —
    no private variant, no second bootstrap."""
    assert demo_open.bootstrap_real_full is bootstrap_real_full


def test_benchmark_factory_consumes_the_same_bootstrap():
    """Static evidence, read as a FILE (this test never imports
    ``taskvm_bench``): the benchmark's ``MobileGymFactory.
    bootstrap_session`` default path lazily imports and calls the very
    same ``taskvm.workspace_ui.composition.bootstrap_real_full`` the
    launcher uses. Together with the repo-wide gate (taskvm/ never
    imports taskvm_bench) this is the single-execution-path proof:
    benchmark and Human UI share ONE composition/runtime abstraction,
    with evaluation powers (reset/seed/oracle) physically absent from
    the prototype plane."""
    from pathlib import Path
    factory_src = (Path(demo_open._REPO_ROOT) / "taskvm_bench"
                   / "evaluation" / "mobilegym_factory.py"
                   ).read_text(encoding="utf-8")
    assert "from taskvm.workspace_ui.composition import " \
           "bootstrap_real_full" in factory_src
    # the default (non-injected) path CALLS it with the L1 session:
    assert "return bootstrap_real_full(" in factory_src
    # and the bench factory reaches the session only through the L1 port
    # (MobileGymSubstrateSession), never a private bypass:
    assert "from taskvm.substrate.mobilegym.session import " \
           "MobileGymSubstrateSession" in factory_src
