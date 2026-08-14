"""builtin_web.browser — Playwright-backed browser surface (Agent B).

Migrated from ``harness/browser_controller.py`` (which no longer exists —
Web/Playwright specifics may not live in generic harness). Changes in the
migration (handoff 03 §Built-in Web 实现):

1. NO hardcoded user absolute paths. The chromium launch recipe is
   discovered from (in order): explicit config, environment variables
   (``TASKVM_PLAYWRIGHT_BROWSERS_PATH`` / ``TASKVM_CHROMELIBS_PATH``), or
   left to the default Playwright installation discovery. A repo test
   asserts no ``/mnt/...`` style absolute paths ship here.
2. ``element_at_point_norm`` no longer returns ``data-*-id`` row identity
   (the E21 leak class). It reports only what a user can see: tag,
   visible text, class.
3. Write surface unchanged and GUI-only: real mouse clicks, real keyboard,
   real wheel events. No evaluate()-based value setting, no DOM mutation,
   no ``page.fill`` shortcuts that bypass focus semantics.
"""
from __future__ import annotations

import base64
import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── chromium launch recipe (configurable, portable) ─────────────────────────


def _discover_browsers_path() -> str | None:
    """Where a playwright chromium install may live, without hardcoding a
    user directory. Order: env var > sibling conda env convention (only if
    it actually exists on this machine) > None (let Playwright decide)."""
    env = os.environ.get("TASKVM_PLAYWRIGHT_BROWSERS_PATH")
    if env:
        return env
    # conventional install inside the *current* environment's prefix
    # (works for any user; no absolute username path is embedded)
    prefix = os.environ.get("CONDA_PREFIX")
    if prefix:
        cand = os.path.join(prefix, "opt", "ms-playwright")
        if os.path.isdir(cand):
            return cand
    return None


def _discover_chromelibs() -> str | None:
    """Optional LD_LIBRARY_PATH augmentation for chromium's runtime libs
    (alsa/atk/xkbcommon…). Env var > repo-local ``.chromelibs/lib``."""
    env = os.environ.get("TASKVM_CHROMELIBS_PATH")
    if env and os.path.isdir(env):
        return env
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))))
    cand = os.path.join(here, ".chromelibs", "lib")
    if os.path.isdir(cand):
        return cand
    return None


_RECIPE_APPLIED = False


def ensure_chromium_env() -> None:
    """Idempotently point PLAYWRIGHT_BROWSERS_PATH / LD_LIBRARY_PATH at a
    usable chromium, using discovery (see above) instead of hardcoded
    user paths. Safe to call from any thread."""
    global _RECIPE_APPLIED
    if _RECIPE_APPLIED:
        return
    bp = _discover_browsers_path()
    if bp:
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", bp)
    cl = _discover_chromelibs()
    if cl:
        existing = os.environ.get("LD_LIBRARY_PATH", "")
        if cl not in existing:
            os.environ["LD_LIBRARY_PATH"] = cl + ":" + existing
    _RECIPE_APPLIED = True
    logger.info("[builtin_web.browser] chromium env ready "
                "(browsers=%s, chromelibs=%s)", bp or "playwright-default", cl)


# ── resident Playwright + chromium singleton ─────────────────────────────────

_pw = None
_browser = None
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
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "playwright is not installed in this environment; the "
                "builtin_web substrate cannot launch a browser") from e
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(headless=True)
        logger.info("[builtin_web.browser] chromium launched (headless, resident)")
    return _browser


def shutdown_browser() -> None:
    """Close the resident browser + stop playwright (process-exit cleanup)."""
    global _pw, _browser
    with _browser_lock:
        if _browser is not None:
            try:
                _browser.close()
            except Exception:
                pass
            _browser = None
        if _pw is not None:
            try:
                _pw.stop()
            except Exception:
                pass
            _pw = None


class BrowserController:
    """One Playwright ``Page`` on the shared resident chromium browser.

    This is the Web substrate's own driver. Upper layers never see it —
    they talk to ``builtin_web.session.WebSubstrateSession`` which maps
    port ``GuiAction``s onto these primitives."""

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
            logger.info("[builtin_web.browser] new page viewport=%s", self.viewport)
        return self._page

    # ── navigation + observation ────────────────────────────────────────────
    def goto(self, url: str, wait: str = "networkidle") -> None:
        self.page.goto(url, wait_until=wait)

    def current_url(self) -> str:
        return self.page.url

    def visible_text(self) -> str:
        """Rendered text only (``innerText`` of body) — no HTML source, so
        no hidden attributes can leak through."""
        return self.page.inner_text("body")

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

    def dom_digest(self) -> str:
        """A stable fingerprint of the VISIBLE structure: tag outline +
        rendered text of the body (no ids/classes/attributes — attribute
        values are where hidden identity hides)."""
        return self.page.evaluate(
            "() => { const walk = (n, d) => { if (!n || d > 18) return ''; "
            "let s = ''; for (const c of n.children || []) { "
            "s += c.tagName + '(' + ((c.innerText || '').trim().slice(0, 24)) "
            "+ ')' + walk(c, d + 1); } return s; }; "
            "return walk(document.body, 0).slice(0, 4000); }")

    # ── atomic GUI actions (coords normalized [0,1000] → viewport px) ───────
    def _norm_to_px(self, x_norm: float, y_norm: float) -> tuple[float, float]:
        return (x_norm / 1000.0 * self.viewport[0],
                y_norm / 1000.0 * self.viewport[1])

    def click_norm(self, x_norm: float, y_norm: float, *, delay_ms: int = 0) -> None:
        x, y = self._norm_to_px(x_norm, y_norm)
        logger.info("[builtin_web.browser] click (%.0f,%.0f) → px (%.0f,%.0f)",
                    x_norm, y_norm, x, y)
        self.page.mouse.click(x, y, delay=delay_ms)

    def type_text(self, text: str) -> None:
        logger.info("[builtin_web.browser] type %r", text)
        self.page.keyboard.type(text)

    def press_key(self, key: str) -> None:
        norm = self._normalize_key(key)
        logger.info("[builtin_web.browser] press %r → %r", key, norm)
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
            out.append(self._KEY_MAP.get(up, p))   # fallback: pass through (F1, a, …)
        return "+".join(out)

    def fill_focused(self, text: str) -> None:
        """Clear the currently-focused input + type the new text. All real
        keyboard events — no evaluate()/value-set backdoor (non-invasive
        write boundary)."""
        self.page.keyboard.press("Control+a")
        self.page.keyboard.press("Delete")
        self.page.keyboard.type(text)

    def scroll_norm(self, x_norm: float, y_norm: float, direction: str,
                    magnitude: int = 400) -> None:
        dy = -magnitude if direction.lower() == "up" else magnitude
        logger.info("[builtin_web.browser] scroll %s dy=%d", direction, dy)
        self.page.mouse.wheel(0, dy)

    def element_at_point_norm(self, x_norm: float,
                              y_norm: float) -> Optional[dict]:
        """Grounding verify: what VISIBLE element is under the point?
        Returns tag / visible text / class only. Hidden identity
        (``data-*-id`` row keys) is intentionally NOT read — that was the
        E21 leak class; a user cannot see those attributes."""
        x, y = self._norm_to_px(x_norm, y_norm)
        return self.page.evaluate(
            "(args) => { const e = document.elementFromPoint(args[0], args[1]); "
            "if (!e) return null; "
            "return {tag: e.tagName, text: (e.textContent||'').trim().slice(0,60), "
            "cls: typeof e.className === 'string' ? e.className : ''}; }",
            [x, y])

    def wait_load(self, timeout_ms: int = 5000) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass   # networkidle can be racy on redirect chains; non-fatal

    def close_page(self) -> None:
        if self._page is not None:
            try:
                self._page.context().close()
            except Exception:
                pass
            self._page = None
