"""builtin_web.session — the Web SubstrateSession (Agent B).

Implements the unified port (``taskvm.substrate.port``) over one Playwright
page on a builtin web app:

  * ``observe``  → screenshot + scrubbed visible text + a11y + TaskVM-owned
                   handle candidates + visible-structure fingerprint;
  * ``act``      → real mouse / keyboard / wheel gestures ONLY;
  * ``capture``  → screenshot artifact.

Zero-exposure judgement (contract §2 / GG §0): every string that leaves
this module must be one a real user can see on the rendered screen. DOM
and accessibility ARE used (a real browser exposes them to assistive tech
— contract §四), but only their visible content; ``data-*-id`` attributes
and hidden identity never enter an Observation.

The session is substrate-local: it knows the app URL (from config) and the
page; it does NOT know what a "calendar" is semantically, and it has no
reset/seed/oracle powers (those are ``builtin_web.evaluation``).
"""
from __future__ import annotations

import hashlib
import itertools
import logging
import threading
import time
from typing import Any

from taskvm.substrate.builtin_web.browser import BrowserController
from taskvm.substrate.port import (
    ActionReceipt,
    GuiAction,
    IrreversibleAction,
    Observation,
    SurfaceHandle,
    SurfaceInfo,
    SubstrateUnavailable,
    VisualArtifact,
    scrub_hidden_ids,
)

logger = logging.getLogger(__name__)

_handle_seq = itertools.count(1)


class WebSubstrateSession:
    """One Playwright page = one surface (the app's list view)."""

    def __init__(self, app: str, url: str, sid: str = "",
                 viewport: tuple[int, int] = (1100, 760),
                 screenshot_dir: str | None = None,
                 browser: BrowserController | None = None):
        self._app = app
        self._url = url
        self._sid = sid
        self._screenshot_dir = screenshot_dir
        self._bc = browser or BrowserController(viewport=viewport)
        self._revision = 0
        self._opened = False
        self._lock = threading.Lock()

    # ── SurfaceInfo ────────────────────────────────────────────────────────
    @property
    def surface(self) -> SurfaceInfo:
        return SurfaceInfo(
            surface_id=f"web:{self._app}",
            display_name=self._app.replace("_", " ").title(),
            surface_kind="tab",
            revision=self._revision,
        )

    def _ensure_open(self) -> None:
        if not self._opened:
            try:
                self._bc.goto(self._url if not self._sid
                              else f"{self._url}/{self._sid}")
                self._bc.wait_load()
            except Exception as e:
                raise SubstrateUnavailable(
                    f"builtin_web cannot open {self._app!r} at {self._url}: {e}"
                ) from e
            self._opened = True

    # ── port: list_surfaces ────────────────────────────────────────────────
    def list_surfaces(self) -> list[SurfaceInfo]:
        return [self.surface]

    # ── port: observe ──────────────────────────────────────────────────────
    def observe(self, surface=None, previous_fingerprint: str | None = None
                ) -> Observation:
        with self._lock:
            self._ensure_open()
            self._revision += 1
            rev = self._revision
            screenshot_ref = None
            if self._screenshot_dir:
                import os
                os.makedirs(self._screenshot_dir, exist_ok=True)
                path = os.path.join(self._screenshot_dir,
                                    f"obs_{rev:03d}.png")
                self._bc.save_screenshot(path)
                screenshot_ref = path
            else:
                screenshot_ref = self._bc.screenshot_data_url()
            visible_text = scrub_hidden_ids(self._bc.visible_text())
            fingerprint = hashlib.sha1(
                self._bc.dom_digest().encode("utf-8")).hexdigest()[:16]
            handles = self._handle_candidates(visible_text, fingerprint, rev)
            return Observation(
                surface=self.surface,
                revision=rev,
                timestamp=time.time(),
                screenshot_ref=screenshot_ref,
                visible_text=visible_text,
                accessibility=None,   # a11y snapshot is bulky; opt-in below
                handle_candidates=handles,
                fingerprint=fingerprint,
                previous_fingerprint_matched=(
                    previous_fingerprint == fingerprint
                    if previous_fingerprint is not None else None),
            )

    def observe_accessibility(self, surface=None) -> dict:
        """Richer a11y observation for consumers that want it (still
        scrubbed — a11y names are visible content, ids are not)."""
        self._ensure_open()
        tree = self._bc.accessibility_tree() or {}

        def _scrub(node: Any) -> Any:
            if isinstance(node, dict):
                return {k: (_scrub(v) if isinstance(v, (dict, list)) else v)
                        for k, v in node.items()
                        if k in ("role", "name", "value", "checked",
                                 "level", "children")}
            if isinstance(node, list):
                return [_scrub(n) for n in node]
            return node

        return _scrub(tree)

    # ── handle candidates from visible structure ──────────────────────────
    def _handle_candidates(self, visible_text: str, fingerprint: str,
                           revision: int) -> tuple[SurfaceHandle, ...]:
        """TaskVM-owned handles for prominent VISIBLE interactive elements.
        Derived from what is rendered (a11y roles/names); no DOM ids, no
        data attributes. This is the substrate's contribution to the
        runtime's handle cache (the cache itself lives above)."""
        out: list[SurfaceHandle] = []
        try:
            tree = self._bc.accessibility_tree() or {}

            def walk(node: Any) -> None:
                if not isinstance(node, dict):
                    return
                role = str(node.get("role") or "")
                name = str(node.get("name") or "").strip()
                if role in ("button", "link", "textbox", "menuitem",
                            "tab", "checkbox") and name:
                    out.append(SurfaceHandle(
                        handle_id=f"h{next(_handle_seq)}",
                        surface_id=self.surface.surface_id,
                        anchor_role=role,
                        anchor_text=name[:80],
                        fingerprint=fingerprint,
                        last_seen_revision=revision))
                for child in node.get("children") or []:
                    walk(child)

            walk(tree)
        except Exception as e:   # a11y is best-effort; visible_text remains
            logger.debug("[builtin_web.session] a11y walk failed: %s", e)
        return tuple(out[:64])

    # ── port: act ─────────────────────────────────────────────────────────
    def act(self, surface, action: GuiAction, *, epoch: str) -> ActionReceipt:
        if not isinstance(action, GuiAction):
            raise TypeError("act() takes a port.GuiAction")
        with self._lock:
            self._ensure_open()
            try:
                detail = self._perform(action)
            except IrreversibleAction:
                raise
            except Exception as e:
                return ActionReceipt(action=action, status="failed",
                                     surface_id=self.surface.surface_id,
                                     epoch=epoch, detail=f"{type(e).__name__}: {e}")
            self._revision += 1
            return ActionReceipt(action=action, status="ok",
                                 surface_id=self.surface.surface_id,
                                 epoch=epoch, detail=detail)

    def _perform(self, action: GuiAction) -> str:
        k = action.kind
        if k in ("click", "tap"):
            if not action.coordinate:
                raise ValueError("click requires a normalized coordinate")
            x, y = action.coordinate
            desc = ""
            try:
                el = self._bc.element_at_point_norm(float(x), float(y))
                if el:
                    desc = f" → {el.get('tag', '?')}"
                    if el.get("text"):
                        desc += f" '{str(el['text'])[:30]}'"
            except Exception:
                desc = " (verify skipped)"
            self._bc.click_norm(float(x), float(y))
            return f"click({x:.0f},{y:.0f}){desc}"
        if k == "type":
            if action.text is None:
                raise ValueError("type requires text")
            # clear-then-type: real keystrokes only (non-invasive write)
            self._bc.fill_focused(action.text)
            return f"type({action.text!r})"
        if k == "key":
            if not action.key:
                raise ValueError("key action requires a key name")
            self._bc.press_key(action.key)
            return f"press({action.key!r})"
        if k == "scroll":
            c = action.coordinate or (500.0, 500.0)
            d = action.direction or "down"
            m = action.magnitude or 400
            self._bc.scroll_norm(float(c[0]), float(c[1]), d, m)
            return f"scroll({d},{m})"
        if k == "wait":
            time.sleep((action.duration_ms or 1000) / 1000.0)
            return f"wait({action.duration_ms or 1000}ms)"
        if k == "open":
            # navigate like a user typing an address: only to the
            # configured app URL (config-owned; no arbitrary deep links
            # synthesized from internal ids)
            url = action.target or self._url
            allowed = url == self._url or url.startswith(self._url.rstrip("/") + "/")
            if not allowed:
                raise IrreversibleAction(
                    f"open() refuses URLs outside the configured app root "
                    f"{self._url!r} (deep-link backdoors are banned, GG.4)")
            self._bc.goto(url)
            self._bc.wait_load()
            return f"open({url})"
        raise ValueError(f"unsupported gesture {k!r}")   # unreachable (frozen vocab)

    # ── port: capture ─────────────────────────────────────────────────────
    def capture(self, surface) -> VisualArtifact:
        with self._lock:
            self._ensure_open()
            return VisualArtifact(
                surface_id=self.surface.surface_id,
                mime="image/png",
                data=self._bc.screenshot_bytes(),
                captured_at=time.time())

    # ── port: close ───────────────────────────────────────────────────────
    def close(self) -> None:
        self._bc.close_page()
