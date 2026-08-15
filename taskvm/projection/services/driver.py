"""taskvm.projection.services.driver — threaded autonomy driver port
(contract §7: governance UX drives autonomy through this port).

The default ``ThreadedRuntimeDriver`` runs Agent E's
``AutonomyRuntime.step()`` in a background thread with pause/resume/stop
controls. The composition root may inject a different driver (e.g. a
process-based driver) as long as it satisfies ``DriverPortLike`` in
``store.py``.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from taskvm.projection.store import DriverPortLike  # noqa: F401 (re-export)


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
    """Drives ``AutonomyRuntime.step()`` in a background thread.

    The runtime is structurally typed: any object with ``step()``,
    ``pause()``, ``resume()``, ``stop()``, and ``status()`` works.
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

    def start(self) -> str:
        if self._thread is not None and self._thread.is_alive():
            return self._status
        self._pause_event.clear()
        self._stop_flag.clear()
        self._status = "running"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self._status

    def pause(self) -> str:
        self._pause_event.set()
        self._status = "paused"
        return self._status

    def resume(self) -> str:
        self._pause_event.clear()
        self._status = "running"
        return self._status

    def stop(self) -> str:
        self._stop_flag.set()
        self._pause_event.clear()  # unblock if paused
        self._status = "stopped"
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return self._status

    def status(self) -> str:
        return self._status

    def execute_compensation(self, plan: Any) -> str:
        """Execute a compensation plan synchronously (blocking)."""
        self._status = "compensating"
        try:
            if hasattr(self._runtime, "execute_compensation"):
                self._runtime.execute_compensation(plan)
        finally:
            self._status = "running" if not self._stop_flag.is_set(
                ) else "stopped"
        return self._status

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop_flag.is_set():
            if self._pause_event.is_set():
                time.sleep(self._step_interval)
                continue
            try:
                events = self._runtime.step()
                if events and self._on_event is not None:
                    for ev in events:
                        self._on_event(ev)
            except Exception as e:
                self._status = f"error: {e}"
                break
            time.sleep(self._step_interval)
