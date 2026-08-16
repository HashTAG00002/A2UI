"""taskvm.projection.services.driver — threaded autonomy driver port
(contract §6/§7: governance UX drives autonomy through this port).

The default ``ThreadedRuntimeDriver`` runs Agent E's
``AutonomyRuntime.run(step_budget=1)`` in a background thread with
pause/resume/stop controls. The composition root may inject a different
driver (e.g. a process-based driver) as long as it satisfies
``DriverPortLike`` in ``store.py``.

D-F2 repair notes:
- ``AutonomyRuntime`` has NO ``step()`` method — its public stepping API is
  ``run(step_budget=N)``. The pre-repair driver called a method that does
  not exist (dead wiring: nothing ever started the driver over a real
  runtime). One node per tick keeps pause/stop responsive.
- lifecycle methods (``start``/``pause``/``resume``/``stop``) return their
  DETERMINISTIC lifecycle value ("running"/"paused"/"running"/"stopped") —
  never ``self._status``, which the worker thread overwrites with per-tick
  dispositions ("step_budget"/"done"/…) and would make HTTP responses
  racy. Per-tick dispositions stay observable via ``status()`` and SSE.
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
                 on_event: Callable[[Any], None] | None = None) -> None:
        self._runtime = runtime
        self._step_interval = step_interval
        self._on_event = on_event
        self._thread: threading.Thread | None = None
        self._pause_event = threading.Event()
        self._stop_flag = threading.Event()
        self._status = "idle"
        self._last_event_count = 0

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> str:
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
        self._pause_event.set()
        self._status = "paused"
        # the runtime's own soft pause (kernel governance event) — best
        # effort; the runtime may not expose it in every composition
        request_pause = getattr(self._runtime, "request_pause", None)
        if callable(request_pause):
            try:
                request_pause()
            except Exception:
                pass
        return "paused"

    def resume(self) -> str:
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
        self._stop_flag.set()
        self._pause_event.clear()  # unblock if paused
        self._status = "stopped"
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return "stopped"

    def status(self) -> str:
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
            if self._pause_event.is_set():
                time.sleep(self._step_interval)
                continue
            try:
                stop = self._runtime.run(step_budget=1)
                self._forward_new_events()
                if stop is not None and str(stop) in _SOFT_STOPS:
                    self._status = str(stop)
                elif stop is not None:
                    # hard stop condition (budget exhausted / error …)
                    self._status = str(stop)
            except Exception as e:
                self._status = f"error: {e}"
                break
            time.sleep(self._step_interval)
