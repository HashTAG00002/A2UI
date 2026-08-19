"""osworld.session — the OSWorld SubstrateSession (minimal).

Contract §5 minimum viable adapter:
  - connect to one OSWorld session;
  - list at least one desktop surface;
  - capture screenshots;
  - execute click / type / key / scroll through the OSWorld runtime's
    REAL input pipeline (pyautogui over the VM's VNC/agent channel —
    exactly what a human's mouse/keyboard would produce);
  - return unified Observation / ActionReceipt.

Honesty: this environment has no OSWorld VM
attached during development, so the integration entrypoint here has NOT
been exercised against a live OSWorld deployment. What IS verified:
contract tests against a fake runtime transport (``tests/substrate/
test_osworld_contract.py``) and a clear ``SubstrateUnavailable`` error
when the VM endpoint is missing. The remaining blocker is recorded in
the OSWorld report.

Transport: OSWorld exposes its desktop via a remote-agent HTTP service
(screenshot + action endpoints). ``OSWorldRuntime`` is a thin HTTP client
for that service; tests substitute a fake transport object with the same
three methods (``screenshot()``, ``perform(action_dict)``, ``alive``).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import time
from typing import Any, Protocol

import requests

from taskvm.substrate.port import (
    ActionReceipt,
    GuiAction,
    Observation,
    SurfaceInfo,
    SubstrateUnavailable,
    VisualArtifact,
    scrub_hidden_ids,
)

logger = logging.getLogger(__name__)


class OSWorldRuntime(Protocol):
    """The minimal transport the session needs. The real one talks to the
    OSWorld remote agent service; tests provide a fake."""
    def screenshot(self) -> bytes: ...
    def perform(self, action: dict) -> dict: ...
    @property
    def alive(self) -> bool: ...


class HttpOSWorldRuntime:
    """HTTP transport over an OSWorld remote-agent service."""

    def __init__(self, endpoint: str, token: str | None = None,
                 timeout: float = 30.0):
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    @property
    def alive(self) -> bool:
        try:
            r = requests.get(f"{self.endpoint}/health",
                             headers=self._headers, timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def screenshot(self) -> bytes:
        r = requests.get(f"{self.endpoint}/screenshot",
                         headers=self._headers, timeout=self.timeout)
        r.raise_for_status()
        return r.content

    def perform(self, action: dict) -> dict:
        r = requests.post(f"{self.endpoint}/action", json=action,
                          headers=self._headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()


class OSWorldSubstrateSession:
    """One desktop surface (the VM screen) through the unified port."""

    _DESKTOP = SurfaceInfo(surface_id="osworld:desktop",
                           display_name="Desktop", surface_kind="screen")

    def __init__(self, runtime: OSWorldRuntime):
        self._rt = runtime
        self._revision = 0
        self._last_visible_text = ""

    # ── port: list_surfaces ────────────────────────────────────────────────
    def list_surfaces(self) -> list[SurfaceInfo]:
        if not self._rt.alive:
            raise SubstrateUnavailable(
                "OSWorld runtime is not alive (no VM attached?)")
        return [self._DESKTOP]

    # ── port: observe ──────────────────────────────────────────────────────
    def observe(self, surface=None, previous_fingerprint: str | None = None
                ) -> Observation:
        try:
            png = self._rt.screenshot()
        except Exception as e:
            raise SubstrateUnavailable(
                f"OSWorld screenshot failed: {e}") from e
        self._revision += 1
        # OSWorld exposes no DOM; visible text arrives via OCR/a11y in the
        # runtime enrichment layer. The substrate contributes the pixels +
        # fingerprint; text enrichment is an upper-layer concern.
        fingerprint = hashlib.sha1(png).hexdigest()[:16]
        return Observation(
            surface=SurfaceInfo(surface_id=self._DESKTOP.surface_id,
                                display_name=self._DESKTOP.display_name,
                                surface_kind="screen",
                                revision=self._revision),
            revision=self._revision,
            timestamp=time.time(),
            screenshot_ref=("data:image/png;base64,"
                            + base64.b64encode(png).decode()),
            visible_text=scrub_hidden_ids(self._last_visible_text),
            fingerprint=fingerprint,
            previous_fingerprint_matched=(
                previous_fingerprint == fingerprint
                if previous_fingerprint is not None else None),
        )

    # ── port: act ─────────────────────────────────────────────────────────
    def act(self, surface, action: GuiAction, *, epoch: str) -> ActionReceipt:
        if not isinstance(action, GuiAction):
            raise TypeError("act() takes a port.GuiAction")
        if not self._rt.alive:
            return ActionReceipt(action=action, status="unavailable",
                                 surface_id=self._DESKTOP.surface_id,
                                 epoch=epoch,
                                 detail="OSWorld runtime is not alive")
        # OSWorld action vocabulary (mm_agents convention): normalized
        # [0,1000] coordinates, real pyautogui events inside the VM.
        payload: dict[str, Any] = {}
        if action.kind in ("click", "tap"):
            if not action.coordinate:
                return ActionReceipt(action=action, status="failed",
                                     surface_id=self._DESKTOP.surface_id,
                                     epoch=epoch,
                                     detail="click requires coordinate")
            payload = {"type": "click",
                       "coordinate": list(action.coordinate)}
        elif action.kind == "type":
            payload = {"type": "type", "text": action.text or ""}
        elif action.kind == "key":
            payload = {"type": "key", "key": action.key or "Enter"}
        elif action.kind == "scroll":
            payload = {"type": "scroll",
                       "coordinate": list(action.coordinate or (500, 500)),
                       "direction": action.direction or "down"}
        elif action.kind == "wait":
            time.sleep((action.duration_ms or 1000) / 1000.0)
            return ActionReceipt(action=action, status="ok",
                                 surface_id=self._DESKTOP.surface_id,
                                 epoch=epoch, detail="wait")
        elif action.kind == "open":
            payload = {"type": "open", "target": action.target or ""}
        try:
            resp = self._rt.perform(payload)
        except Exception as e:
            return ActionReceipt(action=action, status="failed",
                                 surface_id=self._DESKTOP.surface_id,
                                 epoch=epoch,
                                 detail=f"{type(e).__name__}: {e}")
        self._revision += 1
        return ActionReceipt(
            action=action,
            status=str(resp.get("status", "ok")),
            surface_id=self._DESKTOP.surface_id,
            epoch=epoch,
            detail=str(resp.get("detail", "")),
        )

    # ── port: capture ─────────────────────────────────────────────────────
    def capture(self, surface) -> VisualArtifact:
        try:
            png = self._rt.screenshot()
        except Exception as e:
            raise SubstrateUnavailable(
                f"OSWorld screenshot failed: {e}") from e
        return VisualArtifact(surface_id=self._DESKTOP.surface_id,
                              mime="image/png", data=png,
                              captured_at=time.time())

    # ── port: close ───────────────────────────────────────────────────────
    def close(self) -> None:
        return None   # VM lifecycle is the deployment's business
