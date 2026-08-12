"""``HumanWebSocketDriver`` — the real-human L4 implementation (E17-B).

Connects to the workspace_ui WebSocket endpoint (added by E17-B — the server
gains a flask-socketio WS endpoint that pushes VMStateSnapshot to the browser
and receives UserBehaviorEvent dicts back). The scripted driver and this
driver are interchangeable (handoff §1.1 "无缝切换").

Recon (area 9) confirmed workspace_ui was pure Flask HTTP with NO WebSocket.
E17-B adds the WS endpoint (workspace_ui/server.py gains socketio + routes
for ``user_event`` recv + ``vm_state`` push). This driver is the client side.

Event protocol (JSON over WS):
  - browser → server: ``{"type": "user_event", "event_type": "...",
    "payload": {...}}``  → the driver's ``next_event`` returns it.
  - server → browser: ``{"type": "vm_state", "snapshot": {...}}``  → sent by
    ``on_state_update``.

This driver is GREENFIELD but functional: it uses the ``socketio`` client
protocol. If ``flask-socketio`` is not installed in the env, the driver
raises a clear ImportError on construction (so mock/killtest runs that use
ScriptedUserDriver are unaffected).
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any

from taskvm.governance.user_behavior_driver import UserBehaviorDriver, UserBehaviorEvent
from taskvm.governance.vm_state import VMStateSnapshot

logger = logging.getLogger(__name__)


class HumanWebSocketDriver(UserBehaviorDriver):
    """Real-human driver: receives events from a browser via WebSocket."""

    def __init__(self, ws_url: str, *, namespace: str = "/governance",
                 connect_timeout: float = 10.0) -> None:
        try:
            # imported lazily so ScriptedUserDriver users don't need socketio
            import socketio  # type: ignore
        except ImportError as e:  # pragma: no cover - env-dependent
            raise ImportError(
                "HumanWebSocketDriver requires the `socketio` client package "
                "(pip install python-socketio[client]). ScriptedUserDriver "
                "does NOT need it.") from e
        self._sio = socketio.Client()
        self._ws_url = ws_url
        self._namespace = namespace
        self._connect_timeout = connect_timeout
        self._event_q: "queue.Queue[UserBehaviorEvent | None]" = queue.Queue()
        self._connected = threading.Event()

        # wire handlers
        self._sio.on("connect", self._on_connect, namespace=namespace)
        self._sio.on("disconnect", self._on_disconnect, namespace=namespace)
        self._sio.on("user_event", self._on_user_event, namespace=namespace)

    # ── connection lifecycle ──────────────────────────────────────────────
    def start(self) -> None:
        """Connect to the workspace_ui WS endpoint (blocking until connected
        or timeout)."""
        self._sio.connect(self._ws_url, namespaces=[self._namespace],
                          wait_timeout=self._connect_timeout)
        self._connected.wait(timeout=self._connect_timeout)
        if not self._connected.is_set():
            raise TimeoutError(
                f"did not connect to {self._ws_url}{self._namespace} within "
                f"{self._connect_timeout}s")

    def stop(self) -> None:
        """Disconnect + signal next_event to return None (task done)."""
        self._event_q.put(None)
        try:
            self._sio.disconnect()
        except Exception:
            pass

    # ── UserBehaviorDriver interface ──────────────────────────────────────
    def next_event(self) -> UserBehaviorEvent | None:
        """Block until the browser sends a user_event, or None if stopped."""
        return self._event_q.get()

    def on_state_update(self, vm_state: VMStateSnapshot) -> None:
        """Push the new VM state to the browser (for the human to see)."""
        try:
            self._sio.emit("vm_state", {"snapshot": vm_state.to_dict()},
                           namespace=self._namespace)
        except Exception as e:
            logger.warning("failed to push vm_state to browser: %s", e)

    # ── WS handlers ───────────────────────────────────────────────────────
    def _on_connect(self) -> None:
        logger.info("HumanWebSocketDriver connected to %s%s",
                    self._ws_url, self._namespace)
        self._connected.set()

    def _on_disconnect(self) -> None:
        logger.info("HumanWebSocketDriver disconnected")
        self._connected.clear()
        self._event_q.put(None)

    def _on_user_event(self, data: Any) -> None:
        """Receive a user_event from the browser, enqueue it."""
        try:
            if isinstance(data, str):
                data = json.loads(data)
            et = data.get("event_type")
            payload = data.get("payload", {})
            ev = UserBehaviorEvent(et, payload)
            self._event_q.put(ev)
        except Exception as e:
            logger.warning("invalid user_event received: %r (%s)", data, e)
