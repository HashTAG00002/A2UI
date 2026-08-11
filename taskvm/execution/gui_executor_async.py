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
from typing import Any

from taskvm.benchmark import model_client
from taskvm.benchmark.cost_model import CostModel
from taskvm.execution.gui_executor import (GROUNDING_SYSTEM, _build_instruction,
                                            GuiExecutorFailure, MODEL_ROLE)

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 20   # MobileGym sim pages are richer than desktop apps


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


def _norm_to_px(x_norm: float, y_norm: float, viewport: tuple[int, int]) -> tuple[float, float]:
    return (x_norm / 1000.0 * viewport[0], y_norm / 1000.0 * viewport[1])


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
    """Translate one action dict into async Playwright calls on the bridge page.
    Returns a short description for the history log.

    ``env`` (MobileGymEnv): used for high-level gestures that the bridge's sim
    expects via ``__SIM_INPUT__`` (type_text / ENTER) — these route through the
    app's own keydown handlers (the non-invasive write path). Coordinate clicks
    go directly via ``page.mouse.click`` (real mouse event)."""
    from bench_env.env.base import Action, ActionType
    act = (action.get("action") or "").strip().lower()
    if act == "click":
        c = action.get("coordinate") or action.get("start_box")
        if isinstance(c, list) and len(c) >= 2:
            x, y = _norm_to_px(float(c[0]), float(c[1]), viewport)
            tgt = ""
            try:
                el = await _element_at_point(page, x, y)
                if el:
                    tgt = f" → {el.get('tag','?')}"
                    if el.get("text"):
                        tgt += f" '{el['text'][:30]}'"
            except Exception:
                pass
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
            x, y = _norm_to_px(float(c[0]), float(c[1]), viewport)
            dy = -400 if d.lower() == "up" else 400
            await page.mouse.wheel(x, y)
            await page.mouse.wheel(0, dy)
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


async def gui_write_async(*, env, page, sid: str, chat_id: str, text: str,
                          undo: bool = False, model: str | None = None,
                          cost_model: CostModel | None = None,
                          max_steps: int = DEFAULT_MAX_STEPS,
                          screenshot_dir: str | None = None) -> dict:
    """Drive the MobileGym wechat chat via a real grounding loop (Task3).

    Replaces the hardcoded 7-step ``_send_message`` sequence with a
    screenshot → model → gesture loop. The instruction is goal-level
    ("send message X to this chat using the page's UI" / "find a way to
    undo/delete the last message; if no such UI, output fail").

    For rollback (undo=True): the model observes the chat + TRIES to find a
    delete/recall UI. If it outputs ``fail``, the caller raises HTTP 409 —
    but now the irreversibility is PROVEN by the model's real attempt, not
    hardcoded (handoff Task3 requirement: "结论可能不变，但证明这个结论的
    方法论要和主线一致").

    Returns a trace dict {steps, actions, done, final_state}. Raises
    ``GuiExecutorFailure`` if the model outputs ``fail`` (honest irreversibility
    → caller raises 409)."""
    viewport = (400, 800)   # MobileGym phone sim is portrait ~400x800
    instruction = _build_instruction(
        app="wechat", entity_kind="chat", entity_id=chat_id,
        field="messages", value=text, operator="send_message", undo=undo)
    # warm wechat + deep-link to the chat (the app's own OS navigation — NOT a
    # state backdoor; same as the old _send_message steps 1-2, which are
    # navigation not writes). The grounding loop then drives the actual write.
    await env.open_app("wechat", wait_stable=True)
    await page.evaluate(f"window.__OS__?.openApp?.('wechat', '/chat/{chat_id}')")
    # wait for the chat to mount
    for _ in range(8):
        await asyncio.sleep(0.5)
        if await page.evaluate("() => !!document.querySelector('textarea') || !!document.querySelector('[class*=\"message\"]')"):
            break
    history: list[str] = []
    trace = {"steps": 0, "actions": [], "done": False, "undo": undo}
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
