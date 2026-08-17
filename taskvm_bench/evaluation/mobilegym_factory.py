"""B-08 — the MobileGym RM runner/factory (taskvm_bench plane).

The evaluation-side orchestrator that owns ONE MobileGym trial end to end
(re-prompt RM-0.B §B-08), strictly reusing what earlier waves already
landed — nothing here re-implements a lower layer:

* bridge lifecycle  → ``taskvm.substrate.mobilegym.bridge`` (started as a
  SUBPROCESS; this module only speaks HTTP to it);
* reset / seed / oracle reads → ``MobileGymEvaluationEnvironment``
  (``taskvm/substrate/mobilegym/evaluation.py``, the B-03 non-invasive
  oracle — grading never moves the sim's foreground);
* the runtime's ONLY MobileGym face → ``MobileGymSubstrateSession``
  (``taskvm/substrate/mobilegym/session.py``, the L1 observe/act port);
* the real-full TaskVM session → ``taskvm.workspace_ui.composition.
  bootstrap_real_full`` (B-07, the parallel agent's file — this module is
  a CALLER ONLY and never edits it);
* the user-op plane → ``UserOpDriver``/``ProjectionClient`` (B-04) over
  the projection PUBLIC HTTP API;
* per-trial persistence → ``taskvm_bench.evaluation.results`` (B-05).

B-09 start discipline (load-bearing): the bridge subprocess is launched
from a CLOSED flag whitelist that contains NO CUA-loop injection flag —
under the RM configuration the legacy semantic mutate routes have no
nested CUA loop behind them and answer honest 501s. The RM main path uses
ONLY the L1 observe/act port; ``taskvm_bench/evaluation`` never references
the semantic mutate routes (locked by the B-09 static anti-bypass test).

Stage list (re-prompt §B-08, each stage is a plain method so tests can
drive them one at a time against a fake bridge):

    ensure_bridge → health → reset → seed → (reset-state hash) →
    MobileGymSubstrateSession → isolation oracle environments →
    bootstrap_real_full → projection session registration →
    driver start → trial close → state integrity check.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import requests

from taskvm.substrate.mobilegym.evaluation import (
    MobileGymEvaluationEnvironment,
)
from taskvm.substrate.mobilegym.session import MobileGymSubstrateSession
from taskvm_bench.evaluation.results import (
    TrialRecord as UserOpTrialRecord,
    UserOpRecord,
)
from taskvm_bench.evaluation.user_ops import UserOp

__all__ = [
    "BridgeUnavailableError", "DependencyMissingError", "FactoryError",
    "TrialIsolationError", "MobileGymFactory", "MobileGymTrialSpec",
    "BridgeHandle", "ledger_role_counts", "REPO_ROOT",
]

#: repo root (this file is <repo>/taskvm_bench/evaluation/mobilegym_factory.py)
REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".."))

#: bridge defaults mirror taskvm/substrate/mobilegym/bridge.py
DEFAULT_BRIDGE_PORT = 3019
DEFAULT_SIM_URL = "http://localhost:3000"

#: the closed flag whitelist for the bridge subprocess (B-09). If a flag
#: is not in this table the factory refuses to pass it — by construction
#: the launch line can therefore never carry a CUA-loop injection.
_BRIDGE_ALLOWED_FLAGS = frozenset({"--port", "--sim-url", "--screenshot-dir"})


class FactoryError(RuntimeError):
    """Base class for honest factory failures (never a silent fallback)."""


class BridgeUnavailableError(FactoryError):
    """The MobileGym bridge could not be reached or started."""


class DependencyMissingError(FactoryError):
    """A prototype-side dependency the factory only CALLS is absent
    (e.g. the B-07 bootstrap before the parallel agent lands it) —
    reported honestly, never stubbed."""


class TrialIsolationError(FactoryError):
    """B-10: two active trials would share ONE mutable foreground/session.

    The bridge holds exactly ONE live browser state (``_active_sid``);
    a second concurrent trial would silently context-switch reality
    underneath the first (or share its session). The factory therefore
    runs trials SERIALLY by default — a busy factory refuses the second
    trial with this error (or queues it, when ``wait_for_trial_lock`` is
    set). One worker ⇔ one bridge instance; never a shared session."""


# ── the bridge handle ───────────────────────────────────────────────────────

@dataclass
class BridgeHandle:
    """One live (or owned) bridge process the factory may talk to."""

    url: str
    port: int
    #: ``None`` when the factory CONNECTED to an already-healthy bridge
    #: (a deployment the factory did not start and must not kill).
    pid: int | None
    #: stable instance identity for trial manifests (B-10)
    instance_id: str
    started_by_factory: bool
    process: subprocess.Popen | None = None

    def close(self) -> None:
        """Terminate ONLY a process this factory spawned."""
        if not self.started_by_factory or self.process is None:
            return
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:      # honest escalation
                self.process.kill()
                self.process.wait(timeout=5)
        except Exception:
            pass


# ── the trial spec ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MobileGymTrialSpec:
    """One (task, environment seed, model sample) evaluation cell.

    ``environment_seed`` (world initialisation) and ``sample_index``
    (stochastic real-model replicate) are TWO different concepts carried
    explicitly (B-05 discipline) — the CLI must never conflate them.
    """

    fixture: Any                    # duck-typed CanonicalTaskGraph
    environment_seed: int = 0
    sample_index: int = 0
    condition: str = "taskvm-real-full"
    model: str | None = None
    sid: str | None = None

    def resolve_sid(self) -> str:
        if self.sid:
            return self.sid
        task_id = getattr(self.fixture, "task_id", "task")
        return f"{task_id}-e{self.environment_seed}-s{self.sample_index}"

    @property
    def apps(self) -> tuple[str, ...]:
        """The apps this fixture touches (oracle coverage)."""
        return tuple(sorted({b.app for b in self.fixture.bindings}))

    @property
    def surface_app(self) -> str:
        """The primary app the L1 substrate session is bound to (the
        fixture's first binding — its write surface)."""
        return self.fixture.bindings[0].app


# ── smoke-4 helper (ledger invariant, per role) ─────────────────────────────

def ledger_role_counts(ledger: Any) -> dict[str, int]:
    """``{state_compiler, task_architect, cua}`` request counts from the
    SHARED ledger (Smoke 4: provider request count == ledger rows, per
    role). Works on any ledger exposing ``counts_by_role()``."""
    if ledger is None:
        return {"state_compiler": 0, "task_architect": 0, "cua": 0}
    counts = ledger.counts_by_role()
    return {
        "state_compiler": int(counts.get("state_compiler", 0)),
        "task_architect": int(counts.get("task_architect", 0)),
        "cua": int(counts.get("cua", 0)),
    }


# ── setup state (what the setup plane produced, honestly recorded) ─────────

@dataclass
class TrialSetup:
    """Everything the setup plane did before the system under test ran."""

    sid: str
    reset_response: dict = field(default_factory=dict)
    seed_response: dict = field(default_factory=dict)
    reset_state_hash: str = ""
    initial_state_fingerprint: str = ""
    #: oracle reads performed while grading nothing — the B-03 oracle is
    #: non-invasive, and the factory records the fingerprint around each
    #: oracle read so tests can assert the world did not move.
    oracle_noninvasive_checks: list[dict] = field(default_factory=list)
    #: B-10: None when the reset/state invariant HOLDS; otherwise a human
    #: description of the violation (unstable oracle double-read, seeded
    #: entities not visible) — the trial becomes evaluation_error, never
    #: a system failure and never a success.
    invariant_violation: str | None = None


# ── the factory ─────────────────────────────────────────────────────────────

class MobileGymFactory:
    """Owns the bridge handle + the trial lifecycle for MobileGym RM runs.

    The factory is deliberately BORING: every interesting behaviour lives
    in a lower layer it merely calls. Its own contributions are (a) the
    closed-whitelist bridge launch (B-09), (b) the honest stage-by-stage
    orchestration (B-08) and (c) trial isolation + integrity accounting
    (B-10, layered on in a follow-up commit of this same file).
    """

    def __init__(self, *,
                 bridge_url: str | None = None,
                 bridge_port: int = DEFAULT_BRIDGE_PORT,
                 sim_url: str = DEFAULT_SIM_URL,
                 bridge_python: str | None = None,
                 bridge_env: dict[str, str] | None = None,
                 bridge_log: str | None = None,
                 connect_only: bool = False,
                 bridge_startup_timeout_s: float = 90.0,
                 request_timeout_s: float = 10.0,
                 keep_bridge: bool = False,
                 wait_for_trial_lock: float = 0.0) -> None:
        self._bridge_url = bridge_url.rstrip("/") if bridge_url else None
        self._bridge_port = int(bridge_port)
        self._sim_url = sim_url
        self._bridge_python = bridge_python or os.environ.get(
            "TASKVM_PYTHON") or "python3"
        self._bridge_env = dict(bridge_env or {})
        self._bridge_log = bridge_log
        self._connect_only = connect_only
        self._startup_timeout_s = float(bridge_startup_timeout_s)
        self._timeout = float(request_timeout_s)
        self._keep_bridge = keep_bridge
        # B-10: serial execution — ONE active trial per factory (⇔ one
        # bridge instance) at a time. ``wait_for_trial_lock`` > 0 turns
        # a busy refusal into a bounded queue-wait instead.
        self._wait_for_trial_lock = float(wait_for_trial_lock)
        self._trial_lock = threading.Lock()
        self._active_trial_key: str | None = None
        self._busy_rejections = 0
        self.bridge: BridgeHandle | None = None

    # ── stage: bridge lifecycle ────────────────────────────────────────
    def bridge_url(self) -> str:
        return self._bridge_url or f"http://127.0.0.1:{self._bridge_port}"

    def _bridge_argv(self, port: int) -> list[str]:
        """The bridge subprocess launch line — CLOSED whitelist (B-09).

        Only ``--port`` / ``--sim-url`` / ``--screenshot-dir`` may ever
        appear; there is no code path that adds an injection flag, and the
        unit test asserts the whitelist so a future edit cannot widen it
        silently."""
        argv = [self._bridge_python, "-m",
                "taskvm.substrate.mobilegym.bridge",
                "--port", str(port),
                "--sim-url", self._sim_url,
                "--screenshot-dir", ""]      # '' disables per-step PNGs
        flags = {a for a in argv if a.startswith("--")}
        unknown = flags - _BRIDGE_ALLOWED_FLAGS
        assert not unknown, f"bridge launch flags not whitelisted: {unknown}"
        return argv

    def _spawn_bridge(self, argv: list[str]) -> subprocess.Popen:
        """Start the bridge subprocess (monkeypatched in unit tests)."""
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env.update(self._bridge_env)
        sink = open(self._bridge_log, "ab") if self._bridge_log else \
            subprocess.DEVNULL          # noqa: SIM115 — closed via Popen
        return subprocess.Popen(argv, cwd=REPO_ROOT, env=env,
                                stdout=sink, stderr=subprocess.STDOUT)

    def _probe_health(self, timeout: float = 2.0) -> dict | None:
        try:
            r = requests.get(f"{self.bridge_url()}/health", timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            return None
        return None

    def ensure_bridge(self) -> BridgeHandle:
        """Connect to a healthy bridge, or start one (closed whitelist).

        ``connect_only`` refuses to spawn — used when the deployment owns
        the bridge process (the factory then never kills it)."""
        if self.bridge is not None:
            return self.bridge
        url = self.bridge_url()
        if self._probe_health() is not None:
            self.bridge = BridgeHandle(
                url=url, port=self._bridge_port, pid=None,
                instance_id=f"connected:{url}",
                started_by_factory=False)
            return self.bridge
        if self._connect_only:
            raise BridgeUnavailableError(
                f"no healthy MobileGym bridge at {url} and connect_only=True"
                " — start it (see taskvm/substrate/mobilegym/bridge.py) or"
                " drop connect_only")
        argv = self._bridge_argv(self._bridge_port)
        proc = self._spawn_bridge(argv)
        deadline = time.monotonic() + self._startup_timeout_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise BridgeUnavailableError(
                    f"bridge subprocess exited rc={proc.returncode} before"
                    f" becoming healthy (argv={argv}; log={self._bridge_log})")
            if self._probe_health(timeout=1.0) is not None:
                self.bridge = BridgeHandle(
                    url=url, port=self._bridge_port, pid=proc.pid,
                    instance_id=f"spawned:pid{proc.pid}:port{self._bridge_port}",
                    started_by_factory=True, process=proc)
                return self.bridge
            time.sleep(0.5)
        proc.terminate()
        raise BridgeUnavailableError(
            f"bridge at {url} did not become healthy within"
            f" {self._startup_timeout_s}s (argv={argv};"
            f" log={self._bridge_log})")

    def close(self) -> None:
        """Stop the owned bridge (a CONNECTED bridge is never touched)."""
        if self.bridge is not None:
            self.bridge.close()
            self.bridge = None

    # ── stage: evaluation-plane clients (B-03 oracle, reused as-is) ────
    def build_oracles(self, apps: Sequence[str], sid: str
                      ) -> dict[str, MobileGymEvaluationEnvironment]:
        """Per-app isolation oracle environments over the SAME bridge.

        These are the exam-room powers (reset/seed/oracle read); the
        runtime never sees them (physical separation, contract §4)."""
        url = self.bridge_url()
        return {app: MobileGymEvaluationEnvironment(app, sid, url,
                                                    timeout=self._timeout)
                for app in apps}

    def build_session(self, sid: str, surface_app: str
                      ) -> MobileGymSubstrateSession:
        """The system-under-test's ONLY MobileGym face: the L1 port."""
        return MobileGymSubstrateSession(
            sid=sid, bridge_url=self.bridge_url(),
            surface_app=surface_app, timeout=max(self._timeout, 30.0))

    # ── stage: reset + seed + hashes ───────────────────────────────────
    def _oracle_state_all(self, oracles: dict[str,
                                              MobileGymEvaluationEnvironment],
                          sid: str) -> dict[str, Any]:
        return {app: env.oracle_state(sid) for app, env in oracles.items()}

    @staticmethod
    def _hash_state(state: dict[str, Any]) -> str:
        blob = json.dumps(state, sort_keys=True, ensure_ascii=False,
                          default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def setup_trial(self, spec: MobileGymTrialSpec,
                    oracles: dict[str, MobileGymEvaluationEnvironment]
                    ) -> TrialSetup:
        """reset → seed → post-seed oracle hashes + the B-10 invariant.

        Invariant (re-prompt §B-10): the freshly-seeded world must be
        (a) STABLE — two consecutive non-invasive oracle reads agree —
        and (b) REALLY SEEDED — every entity the fixture's seed_state
        adds is visible in the oracle. A violation lands in
        ``TrialSetup.invariant_violation``; the orchestrator turns that
        into ``evaluation_error`` (never a system failure, never a
        success) and skips the SUT stages (a world that cannot be
        trusted must not be graded, nor burn provider calls)."""
        sid = spec.resolve_sid()
        fixture = spec.fixture
        primary = oracles[spec.surface_app]
        setup = TrialSetup(sid=sid)
        setup.reset_response = primary.reset(sid)
        setup.seed_response = primary.seed(
            sid, task_id=fixture.task_id, goal=fixture.goal,
            seed_state=dict(fixture.seed_state or {}))
        first = self._oracle_state_all(oracles, sid)
        setup.reset_state_hash = self._hash_state(first)
        # (a) stability double-read — the oracle is non-invasive (B-03),
        # so any disagreement means the WORLD moved during setup.
        second = self._oracle_state_all(oracles, sid)
        if self._hash_state(second) != setup.reset_state_hash:
            setup.invariant_violation = (
                "oracle state not stable across two consecutive reads "
                f"after seed (first={setup.reset_state_hash[:12]}…, "
                f"second={self._hash_state(second)[:12]}…) — the world "
                "drifted during setup; reset/state invariant violated")
            return setup
        # (b) seeded entities really visible
        missing = self._seed_entities_missing(spec, first)
        if missing:
            setup.invariant_violation = (
                f"seeded entities not visible in the oracle: {missing} — "
                "the seed did not land; reset/state invariant violated")
        return setup

    @staticmethod
    def _seed_entities_missing(spec: MobileGymTrialSpec,
                               oracle_state: dict[str, Any]) -> list[str]:
        """Fixture ``add_chats`` ids that the oracle does NOT show.

        Best-effort by construction: only the wechat merge directives
        (``add_chats``) are checkable this way — X posts are not seedable
        (documented in the fixtures) and alipay is read-from-default."""
        seed = dict(getattr(spec.fixture, "seed_state", None) or {})
        wc = seed.get("wechat") or {}
        add_chats = wc.get("add_chats") or []
        if not add_chats:
            return []
        entities = ((oracle_state.get("wechat") or {}).get("entities")) or {}
        return [str(c.get("id")) for c in add_chats
                if c.get("id") and c.get("id") not in entities]

    # ── stage: the real-full bootstrap (CALLER of B-07, never an editor)
    def bootstrap_session(self, spec: MobileGymTrialSpec, *,
                          session: MobileGymSubstrateSession,
                          ledger: Any = None,
                          store: Any = None,
                          model_port: Any = None,
                          bootstrap_fn: Callable[..., dict] | None = None,
                          ) -> dict:
        """Natural-language goal → real-full TaskVM session, substrate
        injected. ``bootstrap_fn`` defaults to the composition root's
        ``bootstrap_real_full`` (B-07) — injectable for contract-wiring
        tests; the default is imported lazily so its absence (the B-06/
        B-07 wave still in flight) is an honest DependencyMissingError,
        never a stub."""
        if bootstrap_fn is not None:
            return bootstrap_fn(
                goal=spec.fixture.goal, sid=spec.resolve_sid(),
                substrate=session, ledger=ledger, store=store,
                model=spec.model, model_port=model_port)
        try:
            from taskvm.workspace_ui.composition import bootstrap_real_full
        except ImportError as e:      # pragma: no cover — parallel wave
            raise DependencyMissingError(
                "taskvm.workspace_ui.composition.bootstrap_real_full is not"
                " importable (B-07 real-full bootstrap belongs to the"
                " parallel Real-Model agent); inject bootstrap_fn for"
                f" contract-wiring tests. ({e})") from e
        return bootstrap_real_full(
            goal=spec.fixture.goal, sid=spec.resolve_sid(),
            substrate=session, ledger=ledger, store=store,
            model=spec.model, model_port=model_port)

    # ── stage: driver + close + integrity ──────────────────────────────
    DEFAULT_USER_OPS: tuple[str, ...] = ("start", "stop")

    def make_driver_ops(self) -> list[UserOp]:
        """The minimal RM plumbing sequence: one real governance start +
        the matching stop (honest lifecycle close, never a hung driver)."""
        return [UserOp.start(), UserOp.stop()]

    def integrity_check(self, spec: MobileGymTrialSpec,
                        oracles: dict[str, MobileGymEvaluationEnvironment]
                        ) -> dict:
        """Post-trial integrity: bridge health + a fresh oracle read.

        Returns an honest status dict — ``{"status": "ok", ...}`` or
        ``{"status": "unavailable", "detail": ...}`` — plus the final
        state hash for the manifest (never fabricated)."""
        sid = spec.resolve_sid()
        out: dict[str, Any] = {"status": "ok", "detail": ""}
        health = self._probe_health()
        if health is None:
            return {"status": "unavailable",
                    "detail": f"bridge {self.bridge_url()} unhealthy after"
                              " trial"}
        try:
            final_hash = self._hash_state(
                self._oracle_state_all(oracles, sid))
        except Exception as e:              # honest, never a guess
            return {"status": "unavailable",
                    "detail": f"oracle read failed after trial: {e}"}
        out["final_state_hash"] = final_hash
        return out

    # ── the one-trial orchestrator ─────────────────────────────────────
    def run_trial(self, spec: MobileGymTrialSpec, *,
                  model_port: Any = None,
                  bootstrap_fn: Callable[..., dict] | None = None,
                  driver: Any = None,
                  user_ops: Sequence[UserOp] | None = None,
                  ledger: Any = None,
                  store: Any = None,
                  ) -> UserOpTrialRecord:
        """The full B-08 stage chain for ONE trial, honestly recorded.

        ``driver`` (a ``UserOpDriver``) is injectable for tests; the
        production path builds one over a ``ProjectionClient`` pointed at
        the projection server that serves ``store`` (the caller owns that
        server process — the factory never hosts HTTP itself except via
        the injected ``driver``'s base URL)."""
        bridge = self.ensure_bridge()
        # ── B-10: serial execution gate ──────────────────────────────
        # ONE active trial per factory/bridge — a second concurrent
        # trial would share (or silently context-switch) the single
        # mutable foreground session the bridge holds.
        trial_key = (f"{spec.fixture.task_id}/e{spec.environment_seed}"
                     f"/s{spec.sample_index}")
        if self._wait_for_trial_lock > 0:
            acquired = self._trial_lock.acquire(
                timeout=self._wait_for_trial_lock)
        else:
            acquired = self._trial_lock.acquire(blocking=False)
        if not acquired:
            self._busy_rejections += 1
            raise TrialIsolationError(
                f"another trial is active on this factory "
                f"({self._active_trial_key!r}); serial execution only — "
                f"one worker ⇔ one bridge instance, never a shared "
                f"mutable foreground/session")
        self._active_trial_key = trial_key
        try:
            return self._run_trial_locked(spec, model_port=model_port,
                                          bootstrap_fn=bootstrap_fn,
                                          driver=driver, user_ops=user_ops,
                                          ledger=ledger, store=store)
        finally:
            self._active_trial_key = None
            self._trial_lock.release()

    def _run_trial_locked(self, spec: MobileGymTrialSpec, *,
                          model_port: Any = None,
                          bootstrap_fn: Callable[..., dict] | None = None,
                          driver: Any = None,
                          user_ops: Sequence[UserOp] | None = None,
                          ledger: Any = None,
                          store: Any = None,
                          ) -> UserOpTrialRecord:
        """The B-08 stage chain, already holding the trial gate."""
        bridge = self.bridge
        assert bridge is not None      # ensured by run_trial's caller path
        sid = spec.resolve_sid()
        record = UserOpTrialRecord(
            model=spec.model or "", substrate="mobilegym",
            condition=spec.condition,
            environment_seed=spec.environment_seed,
            sample_index=spec.sample_index,
            task_version=str(getattr(spec.fixture, "task_id", "task")),
        )

        oracles = self.build_oracles(spec.apps, sid)
        setup = self.setup_trial(spec, oracles)
        integrity: dict = {"status": "skipped", "detail": ""}
        bundle: dict = {"sid": sid, "skipped": True}
        session = None

        if setup.invariant_violation is not None:
            # B-10 honest dead-end: the reset/state invariant does NOT
            # hold — evaluation_error, not a system failure, not a
            # success. The SUT stages are SKIPPED: a world that cannot
            # be trusted must not be graded (nor burn provider calls).
            record.evaluation_error = setup.invariant_violation
        else:
            session = self.build_session(sid, spec.surface_app)
            initial_obs = session.observe(session.list_surfaces()[0])
            setup.initial_state_fingerprint = initial_obs.fingerprint

            bundle = self.bootstrap_session(
                spec, session=session, ledger=ledger, store=store,
                model_port=model_port, bootstrap_fn=bootstrap_fn)

            if driver is not None:
                ops = list(user_ops) if user_ops is not None else \
                    self.make_driver_ops()
                for op in ops:
                    outcome = driver.execute(op)
                    record.add_op(UserOpRecord(**outcome.to_record()))

            integrity = self.integrity_check(spec, oracles)
            if integrity["status"] != "ok":
                record.evaluation_error = (
                    f"post-trial integrity: {integrity['status']}"
                    f" ({integrity.get('detail', '')})")

        record.finalize()
        # B-10: an evaluation_error is NEVER a success — force the
        # honest error verdict even if the per-op verdicts were green.
        if record.evaluation_error:
            record.trial_verdict = "error"
        # keep the produced artifacts reachable for callers (smoke
        # scripts assemble the B-10 manifest from them)
        self.last_bundle = bundle
        self.last_setup = setup
        self.last_integrity = integrity
        self.last_session = session
        self.last_oracles = oracles
        self.last_record = record
        return record

    def manifest_fields(self, spec: MobileGymTrialSpec) -> dict:
        """The B-10 per-trial manifest block (bridge instance id, sid,
        environment seed, reset state hash, initial fingerprint, final
        integrity) — assembled from the LAST run trial's artifacts."""
        if getattr(self, "last_setup", None) is None:
            raise FactoryError("no trial has run on this factory yet")
        return {
            "bridge_instance_id": self.bridge.instance_id if self.bridge
            else None,
            "sid": self.last_setup.sid,
            "environment_seed": spec.environment_seed,
            "sample_index": spec.sample_index,
            "reset_state_hash": self.last_setup.reset_state_hash,
            "initial_state_fingerprint":
                self.last_setup.initial_state_fingerprint,
            "final_integrity_status": self.last_integrity["status"],
            "final_state_hash": self.last_integrity.get("final_state_hash"),
            # B-10 isolation accounting
            "isolation": {
                "mode": "serial",
                "wait_for_trial_lock_s": self._wait_for_trial_lock,
                "busy_rejections": self._busy_rejections,
                "invariant_violation": self.last_setup.invariant_violation,
            },
        }
