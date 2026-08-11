"""browser_controller — Playwright-backed browser for the GUI Agent WRITE path
(E10 rework, P2).

This replaces the W1 stub. The GUI executor (``taskvm/execution/gui_executor.py``)
drives a real browser through this controller to perform writes/rollbacks via
real GUI gestures (clicks / typing / Enter) — NOT via ``requests.post`` to the
app's internal API. The non-invasive write/rollback boundary (``.mrules`` E7,
memory taskvm-non-invasive-write-rollback-boundary): the only legitimate write
path is the app's own UI; ``mutate`` no longer calls the Flask ``/api/.../mutate``
route directly.

**Chromium launch recipe** (memory taskvm-chromium-launch-recipe, verified
2026-08-10 LAUNCH_OK + re-verified 2026-08-11): the senseact conda env ships
chromium-1117 at ``opt/ms-playwright``; the 4 missing libs (alsa-lib /
at-spi2-atk / at-spi2-core / libxkbcommon) are in ``.chromelibs/lib`` from the
internal sankuai conda-forge mirror. ``ensure_chromium_env()`` sets
``PLAYWRIGHT_BROWSERS_PATH`` + ``LD_LIBRARY_PATH`` so any launch works.

**Resident browser**: one shared Playwright + chromium instance per process
(lazy singleton); each ``BrowserController`` gets its own ``Page``. The browser
lives for the process lifetime (killed on interpreter exit). sync_playwright is
used because the desktop-app adapters are called synchronously (Flask request
handlers + kill-test scripts); the mobilegym bridge is async because it runs its
own aiohttp server (a separate substrate — see ``harness/mobilegym_bridge.py``).

**Atomic ops** mirror the OSWorld mm_agents ``Action`` vocabulary
(CLICK/TYPE/SCROLL/ENTER) + the Playwright grounding surface (``elementFromPoint``,
DOM read, a11y snapshot) the grounding model's actions are translated into:
  - ``click_norm(x, y)``  — [0,1000] normalized → viewport px → ``page.mouse.click``
  - ``type_text(text)``   — ``page.keyboard.type`` (types to current focus)
  - ``press_key(key)``    — ``page.keyboard.press`` (Enter / Tab / Escape)
  - ``scroll_norm(...)``  — ``page.mouse.wheel``
  - ``element_at_point_norm(x, y)`` — grounding verify (what's under the click?)
  - ``screenshot_data_url()`` — PNG → base64 data URL for the vision model
  - ``goto / current_url / read_dom / accessibility_tree``

Coordinates are NORMALIZED to [0,1000] (UITARS convention, see
``OSWorld/mm_agents/uitars_agent.py`` + ``prompts.py``: ``start_box`` divided by
``factor=1000``). The probe (``eval_results/p2_vision_probe/``) confirmed
gpt-5.6-sol grounds at ~1px accuracy with this format.
"""
from __future__ import annotations

import base64
import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── chromium launch recipe (memory: taskvm-chromium-launch-recipe) ───────────
_SENSEACT = "/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/conda/envs/senseact"
_CHROMELIBS = "/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui/.chromelibs/lib"
_CHROME_RECIPE_APPLIED = False


def ensure_chromium_env() -> None:
    """Set PLAYWRIGHT_BROWSERS_PATH + LD_LIBRARY_PATH so chromium-1117 launches.
    Idempotent (only sets if not already set). Safe to call from any thread."""
    global _CHROME_RECIPE_APPLIED
    if _CHROME_RECIPE_APPLIED:
        return
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _SENSEACT + "/opt/ms-playwright")
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    if _CHROMELIBS not in existing:
        os.environ["LD_LIBRARY_PATH"] = _CHROMELIBS + ":" + existing
    _CHROME_RECIPE_APPLIED = True
    logger.info(f"[browser_controller] chromium recipe applied "
                f"(PLAYWRIGHT_BROWSERS_PATH + LD_LIBRARY_PATH={_CHROMELIBS})")


# ── resident Playwright + chromium singleton ─────────────────────────────────
_pw = None                 # playwright sync Playwright instance
_browser = None            # chromium Browser
_browser_lock = threading.Lock()


def _get_browser():
    """Lazy singleton: start sync_playwright + launch chromium (headless)."""
    global _pw, _browser
    if _browser is not None:
        return _browser
    with _browser_lock:
        if _browser is not None:
            return _browser
        ensure_chromium_env()
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(headless=True)
        logger.info("[browser_controller] chromium launched (headless, resident)")
    return _browser


def shutdown_browser() -> None:
    """Close the resident browser + stop playwright (process-exit cleanup)."""
    global _pw, _browser
    with _browser_lock:
        if _browser is not None:
            try: _browser.close()
            except Exception: pass
            _browser = None
        if _pw is not None:
            try: _pw.stop()
            except Exception: pass
            _pw = None


class BrowserController:
    """One Playwright ``Page`` on the shared resident chromium browser. A
    ``BrowserController`` is held by the GUI executor (one page per write
    sequence). The page is created lazily on first use (so importing the module
    + constructing a controller has no side effect)."""

    def __init__(self, viewport: tuple[int, int] = (1100, 760),
                 headless: bool = True):
        self.viewport = viewport
        self.headless = headless
        self._page: Any = None   # lazy

    @property
    def page(self):
        if self._page is None:
            b = _get_browser()
            ctx = b.new_context(viewport={"width": self.viewport[0],
                                          "height": self.viewport[1]})
            self._page = ctx.new_page()
            logger.info(f"[browser_controller] new page viewport={self.viewport}")
        return self._page

    # ── navigation + observation ─────────────────────────────────────────────
    def goto(self, url: str, wait: str = "networkidle") -> None:
        self.page.goto(url, wait_until=wait)

    def current_url(self) -> str:
        return self.page.url

    def read_dom(self) -> str:
        return self.page.content()

    def accessibility_tree(self) -> Any:
        return self.page.accessibility.snapshot()

    def screenshot_bytes(self, full_page: bool = False) -> bytes:
        return self.page.screenshot(type="png", full_page=full_page)

    def screenshot_data_url(self, full_page: bool = False) -> str:
        png = self.screenshot_bytes(full_page=full_page)
        return f"data:image/png;base64,{base64.b64encode(png).decode()}"

    def save_screenshot(self, path: str, full_page: bool = True) -> str:
        self.page.screenshot(path=path, full_page=full_page)
        return path

    # ── atomic GUI actions (coords normalized [0,1000] → viewport px) ────────
    def _norm_to_px(self, x_norm: float, y_norm: float) -> tuple[float, float]:
        return (x_norm / 1000.0 * self.viewport[0],
                y_norm / 1000.0 * self.viewport[1])

    def click_norm(self, x_norm: float, y_norm: float, *, delay_ms: int = 0) -> None:
        x, y = self._norm_to_px(x_norm, y_norm)
        logger.info(f"[browser_controller] click ({x_norm:.0f},{y_norm:.0f}) → px ({x:.0f},{y:.0f})")
        self.page.mouse.click(x, y, delay=delay_ms)

    def type_text(self, text: str) -> None:
        logger.info(f"[browser_controller] type {text!r}")
        self.page.keyboard.type(text)

    def press_key(self, key: str) -> None:
        # key: "Enter" | "Tab" | "Escape" | "Control+a" | "ArrowDown" | ...
        # normalize model-emitted key names (CTRL/ENTER/SHIFT/ESC/...) to
        # Playwright's expected names (Control/Enter/Shift/Escape/...).
        norm = self._normalize_key(key)
        logger.info(f"[browser_controller] press {key!r} → {norm!r}")
        self.page.keyboard.press(norm)

    # model key name → Playwright key name (covers UITARS/computer_13 conventions)
    _KEY_MAP = {
        "CTRL": "Control", "CMD": "Meta", "META": "Meta", "WIN": "Meta",
        "ENTER": "Enter", "RETURN": "Enter",
        "SHIFT": "Shift", "TAB": "Tab",
        "ESC": "Escape", "ESCAPE": "Escape",
        "BACKSPACE": "Backspace", "DEL": "Delete", "DELETE": "Delete",
        "UP": "ArrowUp", "DOWN": "ArrowDown",
        "LEFT": "ArrowLeft", "RIGHT": "ArrowRight",
        "SPACE": "Space", "PGUP": "PageUp", "PGDN": "PageDown",
        "HOME": "Home", "END": "End", "INS": "Insert",
    }

    def _normalize_key(self, key: str) -> str:
        parts = [p.strip() for p in key.split("+")]
        out = []
        for p in parts:
            up = p.upper()
            out.append(self._KEY_MAP.get(up, p))   # fallback: pass through (F1, a, etc.)
        return "+".join(out)

    def fill_focused(self, text: str) -> None:
        """Clear the currently-focused input + type the new text (for date/text
        inputs). Click the field first (via click_norm) to focus it, then call
        this. For ``<input type="date">`` Chromium accepts typed digits."""
        self.page.keyboard.press("Control+a")
        self.page.keyboard.press("Delete")
        self.page.keyboard.type(text)

    def scroll_norm(self, x_norm: float, y_norm: float, direction: str,
                    magnitude: int = 400) -> None:
        x, y = self._norm_to_px(x_norm, y_norm)
        dy = -magnitude if direction.lower() == "up" else magnitude
        logger.info(f"[browser_controller] scroll {direction} at ({x:.0f},{y:.0f}) dy={dy}")
        self.page.mouse.wheel(x, y)
        # wheel(x, y) scrolls by (x, y) from the current position; for vertical
        # scroll we want page.mouse.wheel(0, dy) but Playwright's wheel takes
        # (x, y) deltas — so call it with the vertical delta.
        self.page.mouse.wheel(0, dy)

    def element_at_point_norm(self, x_norm: float, y_norm: float) -> Optional[dict]:
        """Grounding verify: what element is under the (normalized) point?"""
        x, y = self._norm_to_px(x_norm, y_norm)
        return self.page.evaluate(
            "(args) => { const e = document.elementFromPoint(args[0], args[1]); "
            "if (!e) return null; "
            "return {tag: e.tagName, text: (e.textContent||'').trim().slice(0,60), "
            "cls: e.className, id: e.id, "
            "row_event: e.closest('tr')?.getAttribute('data-event-id'), "
            "row_task: e.closest('tr')?.getAttribute('data-task-id'), "
            "row_file: e.closest('tr')?.getAttribute('data-file-id')}; }",
            [x, y])

    def wait_load(self, timeout_ms: int = 5000) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass   # networkidle can be racy on redirect chains; non-fatal

    def close_page(self) -> None:
        if self._page is not None:
            try: self._page.context().close()
            except Exception: pass
            self._page = None
