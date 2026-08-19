"""builtin_web.browser — Playwright-backed browser surface.

Web/Playwright specifics live here, never in generic harness. Invariants:

1. NO hardcoded user absolute paths. The chromium launch recipe is
   discovered from (in order): explicit config, environment variables
   (``TASKVM_PLAYWRIGHT_BROWSERS_PATH`` / ``TASKVM_CHROMELIBS_PATH``), or
   left to the default Playwright installation discovery. A repo test
   asserts no ``/mnt/...`` style absolute paths ship here.
2. ``element_at_point_norm`` never returns ``data-*-id`` row identity.
   It reports only what a user can see: tag, visible text, class.
3. Write surface is GUI-only: real mouse clicks, real keyboard, real
   wheel events. No evaluate()-based value setting, no DOM mutation,
   no ``page.fill`` shortcuts that bypass focus semantics.

Threading model: the resident chromium lives on ONE dedicated
owner thread. The sync Playwright API is thread-affine (its greenlet may
only switch in the thread that started it), and production drives the same
page from several threads — the composition root observes during
``bootstrap_real_full``, then the ``ThreadedRuntimeDriver`` worker acts,
then Flask handlers may observe again. Without a single owner this dies
with ``greenlet.error: cannot switch to a different thread``. Every page
operation is submitted to the owner as a closure; only the plain-Python
result crosses the thread boundary.
"""
from __future__ import annotations

import base64
import itertools
import logging
import os
import queue
import threading
from typing import Any, Optional, cast

logger = logging.getLogger(__name__)

# ── chromium launch recipe (configurable, portable) ─────────────────────────


def _discover_browsers_path() -> str | None:
    """Where a playwright chromium install may live, without hardcoding
    a user directory. Order: env var > sibling conda env convention (only if
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


# ── resident Playwright owner thread + chromium singleton ──────────────────


class _BrowserOwner:
    """Owns ``sync_playwright`` + one headless chromium on a single thread.

    Every operation is a ``(fn, box, done)`` item on a queue; ``fn`` runs
    INSIDE the owner thread (the only thread that ever touches a live
    playwright object) and the result value crosses back via ``box``. A
    ``None`` sentinel stops the loop (process-exit cleanup).
    """

    def __init__(self) -> None:
        self._queue: "queue.Queue" = queue.Queue()
        self._pages: dict[int, Any] = {}
        self._specs: dict[int, tuple[int, int]] = {}
        self._ready = threading.Event()
        self._startup_error: Optional[BaseException] = None
        self._pw: Any = None
        self._browser: Any = None
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="taskvm-builtinweb-browser")
        self._thread.start()
        self._ready.wait(timeout=120)
        # cast resets pyright's per-method attribute narrowing (the owner
        # thread's assignment is invisible to this method's flow analysis)
        err = cast(Optional[BaseException], self._startup_error)
        if err is not None:
            raise err

    def _run(self) -> None:
        try:
            ensure_chromium_env()
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            logger.info("[builtin_web.browser] chromium launched "
                        "(headless, resident owner thread)")
        except BaseException as e:      # env issues — surface to the caller
            self._startup_error = e
            self._ready.set()
            return
        self._ready.set()
        while True:
            item = self._queue.get()
            if item is None:
                break
            fn, box, done = item
            try:
                box["value"] = fn(self._browser)
            except BaseException as e:
                box["error"] = e
            done.set()
        for page in list(self._pages.values()):
            try:
                page.context().close()
            except Exception:
                pass
        self._pages.clear()
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass

    # ── public (callable from ANY thread) ─────────────────────────────
    def call(self, fn, timeout_s: float = 120.0):
        """Run ``fn(browser)`` on the owner thread; return its value."""
        if not self._thread.is_alive():
            raise RuntimeError("builtin_web browser owner thread is gone")
        box: dict = {}
        done = threading.Event()
        self._queue.put((fn, box, done))
        if not done.wait(timeout_s):
            raise TimeoutError(
                "builtin_web browser owner did not answer within "
                f"{timeout_s}s")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def register_page_spec(self, key: int, viewport: tuple[int, int]) -> None:
        self._specs[key] = (int(viewport[0]), int(viewport[1]))

    def page_call(self, key: int, fn, timeout_s: float = 120.0):
        """Run ``fn(page)`` on the controller's page (created lazily)."""
        def run(browser):
            page = self._pages.get(key)
            if page is None or page.is_closed():
                vw, vh = self._specs.get(key, (1100, 760))
                ctx = browser.new_context(
                    viewport={"width": vw, "height": vh})
                page = ctx.new_page()
                self._pages[key] = page
                logger.info("[builtin_web.browser] new page viewport=%s",
                            (vw, vh))
            return fn(page)
        return self.call(run, timeout_s=timeout_s)

    def drop_page(self, key: int) -> None:
        def run(_browser):
            page = self._pages.pop(key, None)
            if page is not None:
                try:
                    page.context().close()
                except Exception:
                    pass
        try:
            self.call(run)
        except Exception:
            pass


_owner: Optional[_BrowserOwner] = None
_owner_lock = threading.Lock()
_page_seq = itertools.count(1)


def _get_owner() -> _BrowserOwner:
    """Lazy singleton owner thread (started on first builtin_web use)."""
    global _owner
    if _owner is not None:
        return _owner
    with _owner_lock:
        if _owner is None:
            _owner = _BrowserOwner()
        return _owner


def shutdown_browser() -> None:
    """Stop the resident owner thread + chromium (process-exit cleanup)."""
    global _owner
    with _owner_lock:
        owner, _owner = _owner, None
    if owner is not None:
        owner._queue.put(None)
        owner._thread.join(timeout=15)


class BrowserController:
    """One Playwright ``Page`` on the shared resident chromium browser —
    driven through the single owner thread (thread-affinity safe).

    This is the Web substrate's own driver. Upper layers never see it —
    they talk to ``builtin_web.session.WebSubstrateSession`` which maps
    port ``GuiAction``s onto these primitives. Every method may be called
    from ANY thread; the operation is serialized onto the owner thread and
    only its plain-Python result crosses back."""

    def __init__(self, viewport: tuple[int, int] = (1100, 760),
                 headless: bool = True):
        self.viewport = viewport
        self.headless = headless
        self._key = next(_page_seq)
        _get_owner().register_page_spec(
            self._key, (int(viewport[0]), int(viewport[1])))

    def _submit(self, fn, timeout_s: float = 120.0) -> Any:
        """Run ``fn(page)`` on the owner thread against THIS page."""
        return _get_owner().page_call(self._key, fn, timeout_s=timeout_s)

    # ── navigation + observation ────────────────────────────────────────────
    def goto(self, url: str, wait: str = "networkidle") -> None:
        self._submit(lambda page: page.goto(url, wait_until=wait),
                     timeout_s=180.0)

    def current_url(self) -> str:
        return self._submit(lambda page: page.url)

    def visible_text(self) -> str:
        """Rendered text only (``innerText`` of body) — no HTML source, so
        no hidden attributes can leak through."""
        return self._submit(lambda page: page.inner_text("body"))

    def accessibility_tree(self) -> Any:
        return self._submit(lambda page: page.accessibility.snapshot())

    def screenshot_bytes(self, full_page: bool = False) -> bytes:
        return self._submit(
            lambda page: page.screenshot(type="png", full_page=full_page))

    def screenshot_data_url(self, full_page: bool = False) -> str:
        png = self.screenshot_bytes(full_page=full_page)
        return f"data:image/png;base64,{base64.b64encode(png).decode()}"

    def save_screenshot(self, path: str, full_page: bool = True) -> str:
        def run(page):
            page.screenshot(path=path, full_page=full_page)
            return path
        return self._submit(run)

    def dom_digest(self) -> str:
        """A stable fingerprint of the VISIBLE structure: tag outline +
        rendered text of the body (no ids/classes/attributes — attribute
        values are where hidden identity hides)."""
        return self._submit(lambda page: page.evaluate(
            "() => { const walk = (n, d) => { if (!n || d > 18) return ''; "
            "let s = ''; for (const c of n.children || []) { "
            "s += c.tagName + '(' + ((c.innerText || '').trim().slice(0, 24)) "
            "+ ')' + walk(c, d + 1); } return s; }; "
            "return walk(document.body, 0).slice(0, 4000); }"))

    # ── atomic GUI actions (coords normalized [0,1000] → viewport px) ───────
    def _norm_to_px(self, x_norm: float, y_norm: float) -> tuple[float, float]:
        return (x_norm / 1000.0 * self.viewport[0],
                y_norm / 1000.0 * self.viewport[1])

    def click_norm(self, x_norm: float, y_norm: float, *, delay_ms: int = 0) -> None:
        x, y = self._norm_to_px(x_norm, y_norm)
        logger.info("[builtin_web.browser] click (%.0f,%.0f) → px (%.0f,%.0f)",
                    x_norm, y_norm, x, y)
        self._submit(lambda page: page.mouse.click(x, y, delay=delay_ms))

    def type_text(self, text: str) -> None:
        logger.info("[builtin_web.browser] type %r", text)
        self._submit(lambda page: page.keyboard.type(text))

    def press_key(self, key: str) -> None:
        norm = self._normalize_key(key)
        logger.info("[builtin_web.browser] press %r → %r", key, norm)
        self._submit(lambda page: page.keyboard.press(norm))

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
        def run(page):
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")
            page.keyboard.type(text)
        self._submit(run)

    def scroll_norm(self, x_norm: float, y_norm: float, direction: str,
                    magnitude: int = 400) -> None:
        dy = -magnitude if direction.lower() == "up" else magnitude
        logger.info("[builtin_web.browser] scroll %s dy=%d", direction, dy)
        self._submit(lambda page: page.mouse.wheel(0, dy))

    def element_at_point_norm(self, x_norm: float,
                              y_norm: float) -> Optional[dict]:
        """Grounding verify: what VISIBLE element is under the point?
        Returns tag / visible text / class only. Hidden identity
        (``data-*-id`` row keys) is intentionally NOT read — a user
        cannot see those attributes."""
        x, y = self._norm_to_px(x_norm, y_norm)
        return self._submit(lambda page: page.evaluate(
            "(args) => { const e = document.elementFromPoint(args[0], args[1]); "
            "if (!e) return null; "
            "return {tag: e.tagName, text: (e.textContent||'').trim().slice(0,60), "
            "cls: typeof e.className === 'string' ? e.className : ''}; }",
            [x, y]))

    def wait_load(self, timeout_ms: int = 5000) -> None:
        def run(page):
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                pass   # networkidle can be racy on redirect chains; non-fatal
        self._submit(run)

    def close_page(self) -> None:
        _get_owner().drop_page(self._key)
