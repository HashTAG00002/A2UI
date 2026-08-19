"""mobilegym.session — the MobileGym SubstrateSession.

A thin HTTP client over the resident MobileGym bridge
(``taskvm.substrate.mobilegym.bridge``, one aiohttp process holding a
``MobileGymEnv``). Implements the unified port:

  * ``observe`` → bridge ``GET /api/observe/<sid>`` (screenshot + scrubbed
    visible text + visible-structure fingerprint — zero-exposure);
  * ``act``     → bridge ``POST /api/act/<sid>`` → ``env.step(Action...)``
    REAL gestures with MobileGym's own coordinate calibration;
  * ``capture`` → screenshot artifact.

The bridge's operator-mutate routes (wechat send_message, X toggle)
are NOT used by this session — those wrap an injected L2 CUA loop; the
runtime reaches MobileGym only through this session's observe/act port.
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

import requests

from taskvm.substrate.mobilegym.app_catalog import get_display_name
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


class MobileGymSubstrateSession:
    """Port session over the bridge's L1 primitive routes."""

    def __init__(self, sid: str, bridge_url: str, surface_app: str,
                 timeout: float = 30.0):
        """``surface_app`` is REQUIRED (catalog-validated at the provider;
        the display name comes from the catalog's user-visible Chinese
        name, e.g. "wechat" → "微信"). The surface is the session's home
        label only — every catalog app remains reachable at runtime via
        GuiAction(kind="open", target=<app_id>)."""
        self._sid = sid
        self._bridge = bridge_url.rstrip("/")
        self._app = surface_app
        self._timeout = timeout
        self._surface = SurfaceInfo(
            surface_id=f"mobilegym:{surface_app}",
            display_name=get_display_name(surface_app),
            surface_kind="app",
        )
        self._revision = 0

    # ── port: list_surfaces ────────────────────────────────────────────────
    def list_surfaces(self) -> list[SurfaceInfo]:
        return [self._surface]

    # ── port: observe ──────────────────────────────────────────────────────
    def observe(self, surface=None, previous_fingerprint: str | None = None
                ) -> Observation:
        try:
            r = requests.get(f"{self._bridge}/api/observe/{self._sid}",
                             timeout=self._timeout)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            raise SubstrateUnavailable(
                f"mobilegym bridge unreachable at {self._bridge}: {e}") from e
        self._revision = int(data.get("revision") or self._revision + 1)
        fp = str(data.get("fingerprint") or "")
        return Observation(
            surface=SurfaceInfo(surface_id=self._surface.surface_id,
                                display_name=self._surface.display_name,
                                surface_kind="app",
                                revision=self._revision),
            revision=self._revision,
            timestamp=float(data.get("timestamp") or time.time()),
            screenshot_ref=data.get("screenshot"),
            visible_text=scrub_hidden_ids(str(data.get("visible_text") or "")),
            fingerprint=fp,
            previous_fingerprint_matched=(
                previous_fingerprint == fp
                if previous_fingerprint is not None else None),
        )

    # ── port: act ─────────────────────────────────────────────────────────
    def act(self, surface, action: GuiAction, *, epoch: str) -> ActionReceipt:
        if not isinstance(action, GuiAction):
            raise TypeError("act() takes a port.GuiAction")
        payload: dict[str, Any] = {"kind": action.kind}
        if action.coordinate:
            payload["coordinate"] = list(action.coordinate)
        if action.text is not None:
            payload["text"] = action.text
        if action.key:
            payload["key"] = action.key
        if action.direction:
            payload["direction"] = action.direction
        if action.magnitude:
            payload["magnitude"] = action.magnitude
        if action.duration_ms:
            payload["duration_ms"] = action.duration_ms
        if action.target:
            payload["target"] = action.target
        try:
            r = requests.post(f"{self._bridge}/api/act/{self._sid}",
                              json=payload, timeout=self._timeout)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            return ActionReceipt(action=action, status="unavailable",
                                 surface_id=self._surface.surface_id,
                                 epoch=epoch,
                                 detail=f"bridge unreachable: {e}")
        self._revision += 1
        return ActionReceipt(
            action=action,
            status=str(data.get("status", "failed")),
            surface_id=self._surface.surface_id,
            epoch=epoch,
            detail=str(data.get("detail", "")),
        )

    # ── port: capture ─────────────────────────────────────────────────────
    def capture(self, surface) -> VisualArtifact:
        obs = self.observe(surface)
        data: bytes | None = None
        ref = obs.screenshot_ref
        if ref and ref.startswith("data:image/png;base64,"):
            data = base64.b64decode(ref.split(",", 1)[1])
            ref = None
        return VisualArtifact(surface_id=self._surface.surface_id,
                              mime="image/png", data=data, ref=ref,
                              captured_at=obs.timestamp)

    # ── port: close ───────────────────────────────────────────────────────
    def close(self) -> None:
        return None   # the bridge process owns the env lifecycle
