"""gui_executor_async — the GUI Agent WRITE/ROLLBACK path for ASYNC substrates
(Task3, E10 rework, MobileGym migration).

The sync ``gui_executor.py`` (used by the desktop apps) uses ``sync_playwright``
via ``BrowserController``. The MobileGym bridge (``harness/mobilegym_bridge.py``)
is an **aiohttp async server** holding a ``MobileGymEnv`` whose ``page`` is an
async Playwright page — the two cannot share a browser (separate event loops).

This module mirrors the sync ``GuiExecutor`` contract (screenshot →
``complete_vision_json`` → parse action DSL → execute via async Playwright →
re-screenshot → loop) but operates on the bridge's resident ``env.page`` + uses
``env.get_state()`` for verify-after-write (the trusted MobileGym read path).

Why a separate async module (not a flag on the sync one): the sync executor's
``BrowserController`` owns its browser; the async one must NOT launch a browser
(it reuses the bridge's already-started ``env.page``). Mixing them would
double-launch Playwright. The action DSL + grounding prompt are shared (imported
from ``gui_executor``) so the model contract is identical — only the execution
substrate differs (sync vs async Playwright).

Task3 scope (handoff):
  a) write path: ``_send_message`` replaced by ``gui_write_async`` — a real
     grounding loop (screenshot → model → gesture), NOT the hardcoded 7-step
     sequence. Instruction is goal-level ("send message X to chat Y using the
     page's UI").
  b) rollback path: ``gui_write_async(undo=True)`` — the model observes the
     current chat page + tries to find a delete/recall UI; if it outputs
     ``fail``, the bridge raises HTTP 409 (honest irreversibility, but now
     PROVEN by the model's real attempt, not hardcoded).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, Awaitable, Callable

from taskvm.benchmark import model_client
from taskvm.benchmark.cost_model import CostModel
from taskvm.execution.gui_executor import (GROUNDING_SYSTEM,
                                            GuiExecutorFailure, MODEL_ROLE)

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 20   # MobileGym sim pages are richer than desktop apps

# ── E14-core ablation switch (Task A, .mrules) ──────────────────────────────
# Set TASKVM_ABLATION_COORD=old to reproduce the PRE-E14 coordinate pipeline
# (hardcoded _norm_to_px(x,y,(400,800)) + page.mouse.click, bypassing
# MobileGym's own norm_0_1000 calibration) for a clean before/after
# measurement of "how much did the coordinate-pipeline fix alone contribute".
# Default (unset / "new") uses the E14 fix: env.step(Action.click(...)).
# This is a TEMPORARY ablation-only switch (not a permanent config knob) —
# added to answer .mrules E14-core's "no ablation was ever run" gap without
# requiring parallel git worktrees. Remove once the ablation is documented in
# E15 (kept for reproducibility until then).
import os as _os_ablation
ABLATION_COORD_MODE = _os_ablation.environ.get("TASKVM_ABLATION_COORD", "new")


def _norm_to_px_old_wrong_viewport(x_norm: float, y_norm: float) -> tuple[float, float]:
    """The EXACT pre-E14 buggy transform (commit 4e7d34b), reproduced verbatim
    for the ablation: hardcoded (400,800) viewport guess (real is (360,800),
    an 11% systematic X error)."""
    viewport = (400, 800)
    return (x_norm / 1000.0 * viewport[0], y_norm / 1000.0 * viewport[1])


async def _screenshot_data_url(page) -> str:
    """Async Playwright page → PNG → base64 data URL for the vision model."""
    png = await page.screenshot(type="png")
    return f"data:image/png;base64,{base64.b64encode(png).decode()}"


async def _element_at_point(page, x_px: float, y_px: float) -> dict | None:
    """What element is under the (pixel) point? (grounding verify)"""
    return await page.evaluate(
        "(args) => { const e = document.elementFromPoint(args[0], args[1]); "
        "if (!e) return null; "
        "return {tag: e.tagName, text: (e.textContent||'').trim().slice(0,60), "
        "cls: e.className, id: e.id, "
        "is_textarea: e.tagName==='TEXTAREA', "
        "is_button: e.tagName==='BUTTON' || e.getAttribute('role')==='button'}; }",
        [x_px, y_px])


def _norm_to_css(x_norm: float, y_norm: float, env) -> tuple[float, float]:
    """Normalized-0-1000 coordinate -> CSS pixel coordinate, using MobileGym's
    OWN calibration (``env.physical_width/height`` + ``env._viewport_size``),
    NOT a hand-guessed viewport constant.

    Bug this replaces (2026-08-11 debug, user-flagged 'harness may not match
    GPT's coordinate convention'): the previous ``_norm_to_px(x, y, (400, 800))``
    used a HARDCODED, WRONG viewport guess — MobileGym's real CSS viewport is
    (360, 800) (``pool.py``/``factory.py`` default ``viewport_size=(360, 800)``,
    ``physical_size=(1080, 2400)``, ``device_scale_factor=3``). 400 != 360 is an
    11% systematic X error — enough to miss small tap targets (e.g. a ~24px
    heart icon). This is the SAME transform MobileGym's own ``ClickHandler``
    does internally (``_parse_point`` norm->physical, then ``_p2c``
    physical->css) — mirrored here ONLY for the diagnostic
    ``_element_at_point`` probe (which needs page-space CSS px for
    ``elementFromPoint``); the ACTUAL click/scroll dispatch below goes through
    ``env.step(Action.click(...))`` so it uses MobileGym's calibration
    directly and can never drift from this mirror."""
    phys_w = getattr(env, "physical_width", 1080)
    phys_h = getattr(env, "physical_height", 2400)
    css_w, css_h = getattr(env, "_viewport_size", (360, 800))
    px = x_norm / 1000.0 * phys_w
    py = y_norm / 1000.0 * phys_h
    return (px / phys_w * css_w, py / phys_h * css_h)


async def _predict_async(page, instruction: str, history: list[str],
                         model: str | None, cost_model: CostModel | None,
                         viewport: tuple[int, int]) -> dict | None:
    """One async grounding call: screenshot → model → parse action dict."""
    data_url = await _screenshot_data_url(page)
    user = instruction
    if history:
        user += "\n\nAction history so far:\n" + "\n".join(
            f"  step {i+1}: {h}" for i, h in enumerate(history))
    user += ("\n\nNote: this is a MOBILE phone simulator (portrait). The page "
             "may have a composer textarea below the visible viewport — if you "
             "need to type, first check if a textarea is focused; if not, the "
             "harness may need to focus it. Output the next action as JSON now.")
    try:
        parsed, raw, resp = model_client.complete_vision_json(
            GROUNDING_SYSTEM, user, data_url,
            max_tokens=300, temperature=None, model=model, repair_retries=1)
    except Exception as e:
        logger.warning(f"[gui_executor_async] vision call failed (likely 429): "
                       f"{e!s:.120}; will back off + retry")
        return None
    if resp is not None and cost_model is not None:
        model_client.record_usage(resp, cost_model, tool="gui_executor_async",
                                  role=MODEL_ROLE,
                                  model=model or model_client.TASKVM_DEFAULT_MODEL)
    if not isinstance(parsed, dict):
        logger.warning(f"[gui_executor_async] no dict parsed: {raw[:200]!r}")
        return None
    return parsed


async def _execute_action_async(page, action: dict, viewport: tuple[int, int],
                                env=None) -> str:
    """Translate one action dict into MobileGym ``env.step(Action(...))`` calls
    (NOT raw Playwright mouse events). Returns a short description for the
    history log.

    ``env`` (MobileGymEnv): 2026-08-11 fix — click/scroll now dispatch via
    ``env.step(Action.click/swipe(...))``, i.e. MobileGym's OWN
    ``coord_space="norm_0_1000"`` -> physical -> CSS pipeline
    (``_parse_point``/``_p2c`` in ``mobile_gym.py``), delivered through
    ``__SIM_INPUT__.tap`` — the SAME dispatch path a real agent submission
    uses. This replaces the old hand-rolled ``_norm_to_px(norm, (400,800))``
    + ``page.mouse.click(css_x,css_y)``, which used a WRONG hardcoded viewport
    (400 vs the real CSS width 360 — an 11% systematic X error) and bypassed
    MobileGym's calibration entirely. type_text/ENTER already went through
    ``env.step`` (unaffected by this bug) — only click/scroll needed the fix."""
    from bench_env.env.base import Action, ActionType
    act = (action.get("action") or "").strip().lower()
    if act == "click":
        c = action.get("coordinate") or action.get("start_box")
        if isinstance(c, list) and len(c) >= 2:
            x_norm, y_norm = float(c[0]), float(c[1])
            tgt = ""
            if ABLATION_COORD_MODE == "old":
                # E14-core ablation (Task A): reproduce the EXACT pre-E14
                # coordinate bug — hardcoded wrong (400,800) viewport +
                # page.mouse.click (bypasses MobileGym's own calibration).
                x, y = _norm_to_px_old_wrong_viewport(x_norm, y_norm)
                try:
                    el = await _element_at_point(page, x, y)
                    if el:
                        tgt = f" → {el.get('tag','?')}"
                        if el.get("text"):
                            tgt += f" '{el['text'][:30]}'"
                except Exception:
                    pass
                await page.mouse.click(x, y)
            elif env is not None:
                try:
                    css_x, css_y = _norm_to_css(x_norm, y_norm, env)
                    el = await _element_at_point(page, css_x, css_y)
                    if el:
                        tgt = f" → {el.get('tag','?')}"
                        if el.get("text"):
                            tgt += f" '{el['text'][:30]}'"
                except Exception:
                    pass
                await env.step(Action.click([x_norm, y_norm]))
            else:
                # no env (unit-test/standalone path) — fall back to a CSS-px
                # guess using the passed-in viewport (legacy behavior).
                x, y = (x_norm / 1000.0 * viewport[0], y_norm / 1000.0 * viewport[1])
                await page.mouse.click(x, y)
            return f"click({c[0]:.0f},{c[1]:.0f}){tgt}"
        return f"click(bad coordinate: {c!r})"
    if act == "type":
        txt = str(action.get("text", ""))
        # type via the sim's __SIM_INPUT__ (routes through handleKeyDown → the
        # app's own input handling) — non-invasive (NOT page.evaluate value-set).
        if env is not None:
            await env.step(Action.type_text(txt))
        else:
            await page.keyboard.type(txt)
        return f"type({txt!r})"
    if act == "press":
        key = str(action.get("key", ""))
        # Enter is special on MobileGym (handleKeyDown → handleSend); route via
        # __SIM_INPUT__ for fidelity to the app's own send pipeline.
        if key.lower() in ("enter", "return") and env is not None:
            await env.step(Action(ActionType.ENTER, {}))
            return "press(Enter via __SIM_INPUT__)"
        # normalize + async press
        norm = key
        mapping = {"CTRL": "Control", "ENTER": "Enter", "ESC": "Escape",
                   "SHIFT": "Shift", "TAB": "Tab", "BACKSPACE": "Backspace"}
        norm = mapping.get(key.upper(), key)
        await page.keyboard.press(norm)
        return f"press({norm!r})"
    if act == "scroll":
        c = action.get("coordinate") or [500, 500]
        d = str(action.get("direction", "down"))
        if isinstance(c, list) and len(c) >= 2:
            x_norm, y_norm = float(c[0]), float(c[1])
            # scroll DOWN (see more content below) = swipe finger UP (content
            # moves up), i.e. point2.y < point1.y in norm space, and vice versa.
            dy_norm = -150 if d.lower() == "down" else 150
            p1 = [x_norm, y_norm]
            p2 = [x_norm, max(0.0, min(1000.0, y_norm + dy_norm))]
            if env is not None:
                await env.step(Action.swipe(p1, p2))
            else:
                x, y = (x_norm / 1000.0 * viewport[0], y_norm / 1000.0 * viewport[1])
                await page.mouse.wheel(x, y)
                await page.mouse.wheel(0, -400 if d.lower() == "up" else 400)
        return f"scroll({d})"
    if act == "wait":
        await asyncio.sleep(1.0)
        return "wait(1s)"
    if act == "done":
        return "DONE"
    if act == "fail":
        reason = str(action.get("reason", "model reported failure"))
        raise GuiExecutorFailure(reason)
    return f"unknown_action({act!r})"


async def gui_act_async(*, env, page, instruction: str,
                          navigate: Callable[[], Awaitable[None]] | None = None,
                          wait_ready: Callable[[], Awaitable[bool]] | None = None,
                          model: str | None = None,
                          cost_model: CostModel | None = None,
                          max_steps: int = DEFAULT_MAX_STEPS,
                          screenshot_dir: str | None = None) -> dict:
    """Generic GUI Agent grounding loop on MobileGym (E14 — 2026-08-11 fix).

    A screenshot -> model -> ``env.step(Action.click/type/swipe/...)`` loop,
    with clicks/scrolls dispatched via MobileGym's OWN ``coord_space=norm_0_1000``
    -> physical -> CSS calibration (``env.step(Action.click(...))``), NOT a
    hand-rolled ``_norm_to_px`` + ``page.mouse.click`` (which used a wrong
    hardcoded viewport — see the 2026-08-11 root-cause comment in
    ``_execute_action_async``).

    ``navigate``: an optional coroutine that opens + deep-links to the target
      page BEFORE the grounding loop starts (e.g. for wechat: ``open_app`` +
      ``openApp('wechat','/chat/<id>')``; for X: ``open_app('x')`` to land on
      the timeline). If None, the loop starts from whatever page is currently
      live. This is OS navigation (NOT a write/rollback), so it's outside the
      non-invasive-write boundary.
    ``wait_ready``: optional coroutine returning True when the target page is
      mounted (e.g. wechat waits for a ``textarea``; X may wait for a tweet
      article). Defaults to a 4s poll for any visible interactive element.

    Returns a trace dict {steps, actions, done}. Raises ``GuiExecutorFailure``
    if the model outputs ``fail`` (caller decides what to do with that — e.g.
    wechat rollback raises HTTP 409 honest-irreversibility)."""
    # Ground-truth viewport from the env itself — NEVER a hardcoded guess.
    # (``env._viewport_size`` is MobileGym's own (css_width, css_height), set
    # from ``viewport_size=(360,800)`` default in pool/factory. Used only as
    # a fallback in ``_execute_action_async`` when env is None — with env it's
    # unused because clicks go through env.step directly.)
    viewport = getattr(env, "_viewport_size", (360, 800))
    if navigate is not None:
        await navigate()
    if wait_ready is not None:
        for _ in range(8):
            await asyncio.sleep(0.5)
            if await wait_ready():
                break
    else:
        await asyncio.sleep(1.0)
    history: list[str] = []
    trace = {"steps": 0, "actions": [], "done": False}
    shot_counter = 0
    if screenshot_dir:
        import os
        os.makedirs(screenshot_dir, exist_ok=True)
    max_attempts = max_steps * 3
    executed = 0
    attempts = 0
    while executed < max_steps and attempts < max_attempts:
        attempts += 1
        action = await _predict_async(page, instruction, history, model,
                                      cost_model, viewport)
        if action is None:
            history.append(f"attempt {attempts}: (no parseable action — retrying)")
            trace["actions"].append({"attempt": attempts, "raw": None})
            await asyncio.sleep(5.0)
            continue
        executed += 1
        try:
            desc = await _execute_action_async(page, action, viewport, env=env)
        except GuiExecutorFailure:
            raise   # honest irreversibility — model said "can't do it"
        history.append(f"step {executed}: {desc}")
        trace["actions"].append({"step": executed, "attempt": attempts,
                                 "action": action, "desc": desc})
        trace["steps"] = executed
        if desc == "DONE":
            trace["done"] = True
            break
        await asyncio.sleep(1.5)
        if screenshot_dir:
            shot_counter += 1
            await page.screenshot(path=f"{screenshot_dir}/step_{shot_counter:02d}_{desc[:30]}.png")
    trace["attempts"] = attempts
    return trace


def _build_wechat_instruction(chat_id: str, text: str, undo: bool) -> str:
    """Wechat-specific goal instruction (Task C fix, .mrules E15).

    Bug this replaces: ``gui_write_async`` used the SHARED
    ``gui_executor._build_instruction`` template, which is written for the
    DESKTOP apps' edit-form pattern ("click into the {entity} (e.g. a
    View/Detail link), open its edit form, ... click 'Review changes' and in
    the confirm dialog click the 'Confirm move' / submit button"). Wechat has
    NO view/detail link, NO edit form, and NO confirm dialog — it's already
    the chat detail page (the bridge's ``_navigate_wechat`` deep-links there
    BEFORE the grounding loop starts), and sending a message is just
    type-into-composer + Enter. Observed failure mode (E15, screenshot
    evidence in ``eval_results/mobilegym_wechat_postcss_*``): the model,
    faced with an instruction describing a UI pattern that doesn't exist,
    searched for a "way to open an edit form" (repeatedly tapping a
    search-like icon) and ended up typing the entity_id string itself into a
    search/contact box, never reaching the actual composer — a goal-level
    instruction MISMATCH bug, not a coordinate/viewport bug (the composer WAS
    reachable after Task C's CSS fix)."""
    if undo:
        return (
            "On this wechat chat page (already open — do NOT navigate away), "
            "try to UNDO the most recently sent message. Look for a long-press "
            "menu, a delete/recall option, or any other real UI affordance on "
            "the last message bubble that lets you remove or recall it. "
            "If you find one, use it, then output {\"action\":\"done\"}. "
            "If wechat's chat UI offers NO way to delete/recall a sent "
            "message (no long-press menu, no recall button), output "
            "{\"action\":\"fail\",\"reason\":\"...\"} — do NOT type into any "
            "search box or navigate to a different chat.")
    return (
        "You are ALREADY on the correct wechat chat detail page (the one "
        "chat this task is about) — do NOT search for a contact, do NOT "
        "navigate to a different chat, and do NOT type the chat id anywhere. "
        "At the bottom of the screen there is a message composer text box "
        "(an empty white input field) next to a '+' icon and a smiley icon. "
        f"1) Tap directly on that composer text box to focus it. "
        f"2) Type EXACTLY this message text: {text!r} "
        f"3) Press Enter to send it (or tap the send button that appears). "
        f"4) After sending, verify the message bubble now appears in the chat "
        f"history above the composer. "
        f"Output {{\"action\":\"done\"}} only once you can see the sent "
        f"message bubble in the chat. If the composer is not visible, it may "
        f"be at the very bottom of the screen — look there first before "
        f"trying anything else. If sending is truly impossible via this "
        f"page's UI, output {{\"action\":\"fail\",\"reason\":\"...\"}}.")


async def gui_write_async(*, env, page, sid: str, chat_id: str, text: str,
                          undo: bool = False, model: str | None = None,
                          cost_model: CostModel | None = None,
                          max_steps: int = DEFAULT_MAX_STEPS,
                          screenshot_dir: str | None = None) -> dict:
    """Drive the MobileGym wechat chat via a real grounding loop (Task3).

    Thin wechat-specific wrapper over the generic ``gui_act_async``: builds
    the wechat send_message instruction + the navigate/wait_ready hooks,
    then delegates to ``gui_act_async``. Kept for backward compatibility with
    ``mobilegym_bridge.mutate_wechat`` (which still calls this signature).

    Instruction: uses ``_build_wechat_instruction`` (Task C / E15 fix), NOT
    the shared desktop-app-style ``gui_executor._build_instruction`` — see
    that function's docstring for why the generic template caused the model
    to search for a nonexistent "edit form" instead of just typing into the
    already-visible composer."""
    instruction = _build_wechat_instruction(chat_id, text, undo)

    async def _navigate_wechat():
        # warm wechat + deep-link to the chat (the app's own OS navigation —
        # NOT a state backdoor). Same as the old _send_message steps 1-2.
        await env.open_app("wechat", wait_stable=True)
        await page.evaluate(
            f"window.__OS__?.openApp?.('wechat', '/chat/{chat_id}')")

    async def _wait_wechat_ready():
        return await page.evaluate(
            "() => !!document.querySelector('textarea') "
            "|| !!document.querySelector('[class*=\"message\"]')")

    return await gui_act_async(
        env=env, page=page, instruction=instruction,
        navigate=_navigate_wechat, wait_ready=_wait_wechat_ready,
        model=model, cost_model=cost_model, max_steps=max_steps,
        screenshot_dir=screenshot_dir)
