"""A-03 — production inactive-surface heartbeat scheduler (integration).

The frozen contract (runtime.md §8): the surface the CUA is driving is
synced by its own act→re-observe loop; every OTHER surface is filled in
by a low-frequency READ-ONLY heartbeat. Before A-03 the production
``ThreadedRuntimeDriver`` only looped ``runtime.run(step_budget=1)`` and
never scheduled ``poll_inactive_surfaces()`` — the live-projection claim
did not close on the real driver path (only the benchmark harness called
it by hand).

These tests pin the production behaviour:

1. user idles on surface A, agent takes NO GUI action, surface B changes
   externally → within the heartbeat deadline the kernel's OBSERVED plane
   updates and the projection-side SSE path receives the correct delta;
2. zero provider requests (fingerprint fast path — runtime.md §8);
3. zero GUI actions on any surface;
4. the heartbeat keeps running while the ACTION loop is governance-paused
   (live projection is the read side; pause governs autonomy, not truth);
5. cadence resolution: explicit ctor override > runtime budgets > None.
"""
from __future__ import annotations

import time

from taskvm.domain.intent import TaskIntent
from taskvm.domain.state import TaskVariable
from taskvm.kernel import TaskVMKernel
from taskvm.projection.events import sse_envelope
from taskvm.projection.services.driver import ThreadedRuntimeDriver
from taskvm.runtime import AutonomyRuntime, RuntimeBudgets
from taskvm.runtime.sync import StructureInvalidation  # noqa: F401 (doc ref)
from taskvm.verifier.visible import VisibleVerifier

# the deterministic fakes live in tests/runtime/conftest.py (Agent E's
# suite); importing them keeps ONE set of fakes (no second world model)
from tests.runtime.conftest import (
    FakeExtractor, FakeLedger, FakeSerializer, FakeSubstrate, ScriptedCUA,
)

# deadline discipline: with heartbeat_seconds=0.1 the world change must be
# projected well inside this bound (2 cadences + scheduling slack).
_SSE_DEADLINE = 1.5


def _two_surface_kernel() -> TaskVMKernel:
    """x lives on app (active), y on desktop (inactive). y's desired is
    "y1" while the world still shows "y0" — the external world later
    reaches "y1" on its own, which is the fold path (runtime.md §8:
    known-handle value change → apply_observation; a value that DRIFTS
    from desired would be the conflict path, pinned in test_sync.py)."""
    kernel = TaskVMKernel("hb-a03", TaskIntent(goal="同步两个面板"))
    kernel.init_task_state([
        TaskVariable(semantic_key="x", label="面板A", observed="x0",
                     desired="x0"),
        TaskVariable(semantic_key="y", label="面板B", observed="y0",
                     desired="y1"),
    ])
    # NOTE: no set_plan() — the action loop has nothing to do, which is
    # exactly the A-03 premise: autonomy is NOT driving any surface.
    return kernel


def _make_runtime(kernel, substrate, ledger) -> AutonomyRuntime:
    return AutonomyRuntime(
        kernel, substrate,
        cua_model=ScriptedCUA([]),
        serializer=FakeSerializer(),
        extractor=FakeExtractor(),
        verifier=VisibleVerifier(),
        ledger=ledger,
        budgets=RuntimeBudgets(inactive_heartbeat_seconds=5.0),
        surfaces=["app", "desktop"])


class _SSECollector:
    """Mimics the projection SSE chokepoint: every event the driver
    forwards must pass ``sse_envelope`` (frozen vocabulary assertion)."""

    def __init__(self):
        self.envelopes: list[dict] = []
        self.error: Exception | None = None

    def __call__(self, ev):
        try:
            self.envelopes.append(sse_envelope(ev))
        except Exception as e:  # vocabulary violation would fail loudly
            self.error = e

    def types(self) -> list[str]:
        return [e["sse_type"] for e in self.envelopes]


class TestHeartbeatScheduler:

    def test_world_change_on_inactive_surface_projects_within_deadline(self):
        """The core A-03 acceptance: surface B changes externally while the
        user/agent are idle on A → observed plane + SSE delta in time,
        with 0 provider calls and 0 GUI actions."""
        kernel = _two_surface_kernel()
        substrate = FakeSubstrate({"app": {"x": "x0"},
                                   "desktop": {"y": "y0"}})
        ledger = FakeLedger()
        runtime = _make_runtime(kernel, substrate, ledger)
        runtime._sync.set_active("app")

        sse = _SSECollector()
        driver = ThreadedRuntimeDriver(
            runtime, step_interval=0.05, heartbeat_seconds=0.1,
            on_event=sse, kernel=kernel)
        driver.start()
        try:
            # settle: the first heartbeat folds the (desired-matching) x
            # and honestly conflicts y (world y0 ≠ desired y1) — wait for
            # the first envelope, then take the baseline.
            deadline = time.monotonic() + _SSE_DEADLINE
            while time.monotonic() < deadline and not sse.envelopes:
                time.sleep(0.02)
            assert sse.envelopes, (
                f"the first heartbeat produced no SSE delta: {sse.types()}")
            baseline_envelopes = len(sse.envelopes)
            baseline_observes = len(substrate.observe_log)

            # ── the external world reaches y's desired on its own ───────
            substrate.world["desktop"]["y"] = "y1"

            # ── within the deadline the projection must catch up ───────
            deadline = time.monotonic() + _SSE_DEADLINE
            projected = None
            while time.monotonic() < deadline:
                projected = kernel.task_state().variable("y")
                if projected is not None and projected.observed == "y1":
                    break
                time.sleep(0.02)
            assert projected is not None and projected.observed == "y1", (
                f"observed plane never caught up: {projected}")
            # … and the SSE stream carried the delta
            new_types = sse.types()[baseline_envelopes:]
            assert "observation.received" in new_types, (
                f"heartbeat delta missing from SSE: {new_types}")
            assert sse.error is None

            # ── A-03 read-only guarantees ──────────────────────────────
            assert ledger.total() == 0, "heartbeat must cost 0 model calls"
            assert substrate.act_log == [], "heartbeat must take 0 GUI actions"
            # fast path: after the fold, further heartbeats re-observe the
            # unchanged desktop but add NO new kernel observation deltas
            time.sleep(0.25)
            obs_deltas = [t for t in sse.types()[baseline_envelopes:]
                          if t == "observation.received"]
            assert len(obs_deltas) == 1, (
                "unchanged fingerprint must not re-fold observations "
                f"(runtime.md §8 fast path): {sse.types()}")
            assert len(substrate.observe_log) > baseline_observes  # did poll
        finally:
            driver.stop()

    def test_heartbeat_runs_while_paused(self):
        """Pause governs the ACTION loop, not the live projection: the
        world keeps changing and the observed plane keeps up."""
        kernel = _two_surface_kernel()
        substrate = FakeSubstrate({"app": {"x": "x0"},
                                   "desktop": {"y": "y0"}})
        ledger = FakeLedger()
        runtime = _make_runtime(kernel, substrate, ledger)
        runtime._sync.set_active("app")

        sse = _SSECollector()
        driver = ThreadedRuntimeDriver(
            runtime, step_interval=0.05, heartbeat_seconds=0.1,
            on_event=sse, kernel=kernel)
        driver.start()
        assert driver.pause() == "paused"
        try:
            substrate.world["desktop"]["y"] = "y1"  # externally reaches desired
            deadline = time.monotonic() + _SSE_DEADLINE
            projected = None
            while time.monotonic() < deadline:
                projected = kernel.task_state().variable("y")
                if projected is not None and projected.observed == "y1":
                    break
                time.sleep(0.02)
            assert projected is not None and projected.observed == "y1", (
                "a paused action loop must not pause the live projection "
                f"(observed={projected})")
            assert substrate.act_log == []
            assert ledger.total() == 0
        finally:
            driver.stop()

    def test_stop_ends_the_heartbeat(self):
        """Stop is terminal (A-02): the driver thread exits and the world
        is no longer polled — no zombie heartbeats after stop()."""
        kernel = _two_surface_kernel()
        substrate = FakeSubstrate({"app": {"x": "x0"},
                                   "desktop": {"y": "y0"}})
        runtime = _make_runtime(kernel, substrate, FakeLedger())
        runtime._sync.set_active("app")
        driver = ThreadedRuntimeDriver(
            runtime, step_interval=0.05, heartbeat_seconds=0.1,
            kernel=kernel)
        driver.start()
        driver.stop()
        observes_after_stop = len(substrate.observe_log)
        substrate.world["desktop"]["y"] = "y9"
        time.sleep(0.4)
        assert len(substrate.observe_log) == observes_after_stop, (
            "the heartbeat kept polling after a terminal stop")
        y = kernel.task_state().variable("y")
        assert y.observed == "y0"

    def test_cadence_resolution_order(self):
        """Explicit ctor override > runtime budgets > None (disabled)."""
        kernel = _two_surface_kernel()
        substrate = FakeSubstrate({"app": {}, "desktop": {}})
        runtime = _make_runtime(kernel, substrate, FakeLedger())

        # 1. explicit override wins over the 5.0s budget
        d = ThreadedRuntimeDriver(runtime, heartbeat_seconds=0.5)
        assert d.heartbeat_seconds() == 0.5
        # 2. no override → the runtime's budget (runtime.md §5)
        d = ThreadedRuntimeDriver(runtime)
        assert d.heartbeat_seconds() == 5.0
        # 3. a runtime exposing no budgets → heartbeat disabled
        class _Bare:
            def run(self, step_budget=None):
                return "done"

            def runtime_events(self):
                return ()

        d = ThreadedRuntimeDriver(_Bare())
        assert d.heartbeat_seconds() is None
