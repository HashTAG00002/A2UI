"""taskvm.projection.services.driver — threaded autonomy driver port
(contract §6/§7: governance UX drives autonomy through this port).

The default ``ThreadedRuntimeDriver`` runs the
``AutonomyRuntime.run(step_budget=1)`` in a background thread with
pause/resume/stop controls. The composition root may inject a different
driver (e.g. a process-based driver) as long as it satisfies
``DriverPortLike`` in ``store.py``.

Wiring contract:
- ``AutonomyRuntime`` has NO ``step()`` method — its public stepping API is
  ``run(step_budget=N)``. One node per tick keeps pause/stop responsive.
- lifecycle methods (``start``/``pause``/``resume``/``stop``) return their
  DETERMINISTIC lifecycle value ("running"/"paused"/"running"/"stopped").
  ``status()`` ALSO prefers lifecycle state: if ``_pause_event`` is set or
  ``_lifecycle_stopped`` is True, ``status()`` returns "paused"/"stopped"
  regardless of the per-tick disposition the worker last wrote. This closes
  the race where ``run()`` returns "step_budget" and the worker overwrites
  ``_status`` *after* ``pause()`` set the event but *before* the worker
  reaches the pause check at the top of the loop. Per-tick dispositions are
  still observable when no lifecycle flag is set (the normal running state).
- ``execute_compensation`` now returns the RUNTIME's disposition
  ("complete" | "partial" | "failed" | ...), not the driver's internal
  status — the rollback route reports it honestly (§8).
- each tick forwards NEW ``runtime_events()`` to ``on_event`` so the SSE
  bus can push live runtime deltas (D consumes runtime events by public
  facade only).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from taskvm.projection.store import DriverPortLike  # noqa: F401 (re-export)

#: run() stop conditions that mean "nothing more to do now" — the thread
#: keeps polling (a resume / a landed compensation plan may unblock it),
#: but the status tells the UI why autonomy is not advancing.
_SOFT_STOPS = frozenset({"done", "no_ready", "blocked", "step_budget"})


class RuntimeDriverPort:
    """Protocol alias for type-checking (matches ``DriverPortLike``)."""
    def start(self) -> str: ...
    def pause(self) -> str: ...
    def resume(self) -> str: ...
    def stop(self) -> str: ...
    def status(self) -> str: ...
    def execute_compensation(self, plan: Any) -> str: ...
    def join(self, timeout: float | None = None) -> None: ...


class ThreadedRuntimeDriver:
    """Drives ``AutonomyRuntime.run(step_budget=1)`` in a background thread.

    The runtime is structurally typed: any object with ``run`` (stepping),
    ``runtime_events()`` and optionally ``execute_compensation`` works.
    """

    def __init__(self, runtime: Any,
                 step_interval: float = 0.5,
                 on_event: Callable[[Any], None] | None = None,
                 heartbeat_seconds: float | None = None,
                 kernel: Any = None) -> None:
        self._runtime = runtime
        self._step_interval = step_interval
        self._on_event = on_event
        # explicit cadence override — wins over the runtime budget.
        # ``None`` means "derive from the runtime's
        # budgets.inactive_heartbeat_seconds"; a runtime exposing neither
        # disables the heartbeat (structural typing, same as run()).
        self._heartbeat_override = heartbeat_seconds
        self._next_heartbeat = time.monotonic()
        # optional duck-typed kernel (``.events()``) so heartbeat-
        # caused kernel events (OBSERVATION_RECEIVED / CONFLICT_DETECTED)
        # reach SSE. ONLY the heartbeat path forwards kernel events — the
        # HTTP handlers push their own commands' kernel events
        # (``_push_kernel_events``), and this split keeps the two paths
        # from double-forwarding.
        self._kernel = kernel
        self._last_kernel_event_count = 0
        self._thread: threading.Thread | None = None
        self._pause_event = threading.Event()
        self._stop_flag = threading.Event()
        self._lifecycle_stopped = False  # persistent stop — start() refuses to revive
        self._status = "idle"
        self._last_event_count = 0

    # ── inactive-surface heartbeat cadence ─────────────────────
    def heartbeat_seconds(self) -> float | None:
        """Resolved heartbeat cadence (run manifests record this — the
        number is configuration, never a hardcoded constant): explicit
        ctor override > the runtime's budgets.inactive_heartbeat_seconds
        > None (heartbeat disabled, e.g. a fake runtime in tests)."""
        if self._heartbeat_override is not None:
            return float(self._heartbeat_override)
        budgets = getattr(self._runtime, "budgets", None)
        sec = getattr(budgets, "inactive_heartbeat_seconds", None)
        if sec is not None and float(sec) > 0:
            return float(sec)
        return None

    # ── lifecycle (single-owner path — driver → runtime → kernel) ───
    def start(self) -> str:
        # a stopped driver is persistent — start() cannot revive it.
        # The frozen contract does not define restart-from-stopped on the
        # same runtime object; a fresh composition is required.
        if self._lifecycle_stopped:
            return "stopped"
        if self._thread is not None and self._thread.is_alive():
            # already started: report the lifecycle state deterministically.
            # A paused driver STAYS paused (call resume to continue) — only
            # ``pause_event`` gates this, and the worker never writes it,
            # so there is no race with in-flight ticks.
            return "paused" if self._pause_event.is_set() else "running"
        self._pause_event.clear()
        self._stop_flag.clear()
        self._status = "running"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return "running"

    def pause(self) -> str:
        # single-owner: driver sets pause_event → runtime writes
        # kernel governance state. No second kernel.write path.
        if self._lifecycle_stopped:
            return "stopped"
        self._pause_event.set()
        self._status = "paused"
        request_pause = getattr(self._runtime, "request_pause", None)
        if callable(request_pause):
            try:
                request_pause()
            except Exception:
                pass
        return "paused"

    def resume(self) -> str:
        # single-owner: driver clears pause_event → runtime writes
        # kernel governance state. Resume from stopped is refused.
        if self._lifecycle_stopped:
            return "stopped"
        self._pause_event.clear()
        self._status = "running"
        request_resume = getattr(self._runtime, "request_resume", None)
        if callable(request_resume):
            try:
                request_resume()
            except Exception:
                pass
        return "running"

    def stop(self) -> str:
        # persistent lifecycle stop — the driver thread exits, the
        # runtime's _stopped flag prevents any further GUI action, and
        # start() can never revive this driver instance.
        self._stop_flag.set()
        self._pause_event.clear()  # unblock if paused
        self._lifecycle_stopped = True
        self._status = "stopped"
        request_stop = getattr(self._runtime, "request_stop", None)
        if callable(request_stop):
            try:
                request_stop()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return "stopped"

    def status(self) -> str:
        # race fix: lifecycle states (paused/stopped) are authoritative —
        # a per-tick disposition ("step_budget"/"done"/…) that the worker
        # wrote *before* pause() landed must not leak through status().
        # The worker checks _pause_event/_stop_flag AFTER run() returns and
        # skips its _status write if a lifecycle change raced in, but a tiny
        # window still exists between the worker's check and its write.
        # This guard makes status() deterministic: lifecycle always wins.
        if self._lifecycle_stopped:
            return "stopped"
        if self._pause_event.is_set():
            return "paused"
        return self._status

    # ── compensation (blocking, in the caller's thread) ──────────────────
    def execute_compensation(self, plan: Any) -> str:
        """Execute a compensation plan synchronously; returns the RUNTIME's
        honest disposition ("complete"/"partial"/"failed"/...). The kernel
        lands the typed result inside the runtime; the projection reports
        what actually happened (§8: an honest PARTIAL is shown as partial)."""
        self._status = "compensating"
        disposition = "failed"
        try:
            fn = getattr(self._runtime, "execute_compensation", None)
            if callable(fn):
                disposition = fn(plan)
        finally:
            self._forward_new_events()
            if self._status == "compensating":
                self._status = ("stopped" if self._stop_flag.is_set()
                                else "running")
        return str(disposition)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ── internals ────────────────────────────────────────────────────────
    def _maybe_heartbeat(self, now: float) -> None:
        """Monotonic inactive-surface heartbeat (runtime.md §8).

        READ-ONLY: ``poll_inactive_surfaces()`` observes every surface
        the CUA is NOT driving; an unchanged fingerprint costs 0 model
        calls and 0 compiler calls by construction (the sync fast path).
        New runtime events flow through the same ``_forward_new_events``
        → SSE path; kernel events the poll produced (observation folds /
        conflicts) are forwarded here — no HTTP handler owns them.

        Runs even while the ACTION loop is governance-paused: the
        projection must keep reflecting world changes (contract §0 —
        "用户不操作 ≠ TaskVM 静止" governs autonomy; live projection
        governs the read side). Only ``stop()`` ends it (terminal)."""
        interval = self.heartbeat_seconds()
        if interval is None:
            return
        if now < self._next_heartbeat:
            return
        self._next_heartbeat = now + interval
        poll = getattr(self._runtime, "poll_inactive_surfaces", None)
        if callable(poll):
            try:
                poll()
            except Exception:
                pass  # best-effort; a heartbeat failure never kills the loop
        self._forward_new_events()
        self._forward_new_kernel_events()

    def _forward_new_kernel_events(self) -> None:
        """Push kernel events produced since the last heartbeat-
        driven forward (heartbeat-only; HTTP handlers push their own
        commands' events via ``_push_kernel_events``)."""
        if self._on_event is None or self._kernel is None:
            return
        events_fn = getattr(self._kernel, "events", None)
        if not callable(events_fn):
            return
        try:
            current = tuple(events_fn())
        except Exception:
            return
        for ev in current[self._last_kernel_event_count:]:
            try:
                self._on_event(ev)
            except Exception:
                pass  # best-effort; identical event_id frames are idempotent
        self._last_kernel_event_count = len(current)

    def _forward_new_events(self) -> None:
        """Push runtime events produced since the last forward to
        ``on_event`` (public facade only — ``runtime_events()``)."""
        if self._on_event is None:
            return
        events = getattr(self._runtime, "runtime_events", None)
        if not callable(events):
            return
        try:
            current = tuple(events())
        except Exception:
            return
        for ev in current[self._last_event_count:]:
            try:
                self._on_event(ev)
            except Exception:
                pass  # SSE push is best-effort; never kill the loop
        self._last_event_count = len(current)

    def _run(self) -> None:
        while not self._stop_flag.is_set():
            # the heartbeat cadence is independent of the action
            # loop — it fires while paused too (read-only world sync).
            self._maybe_heartbeat(time.monotonic())
            if self._pause_event.is_set():
                # race fix: ensure status reflects paused even if a
                # per-tick disposition was written after pause() set the
                # event but before the worker reached this check.
                self._status = "paused"
                time.sleep(self._step_interval)
                continue
            try:
                stop = self._runtime.run(step_budget=1)
                self._forward_new_events()
                # if the runtime itself returned STOPPED (its own
                # _stopped flag was set, e.g. by an external governance
                # stop), the driver must also enter the persistent stopped
                # state — not just a soft stop.
                if stop is not None and str(stop) == "stopped":
                    self._lifecycle_stopped = True
                    self._status = "stopped"
                    break
                # race fix: re-check lifecycle flags AFTER run() returns
                # but BEFORE writing per-tick disposition. If pause() or stop()
                # was called while run() was in-flight, the lifecycle state
                # must not be overwritten by the per-tick disposition.
                if self._stop_flag.is_set():
                    self._lifecycle_stopped = True
                    self._status = "stopped"
                    break
                if self._pause_event.is_set():
                    self._status = "paused"
                    time.sleep(self._step_interval)
                    continue
                if stop is not None and str(stop) in _SOFT_STOPS:
                    self._status = str(stop)
                elif stop is not None:
                    # hard stop condition (budget exhausted / error …)
                    self._status = str(stop)
            except Exception as e:
                self._status = f"error: {e}"
                break
            time.sleep(self._step_interval)
