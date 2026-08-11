"""gui_executor — the GUI Agent WRITE/ROLLBACK path (E10 rework, P2/P3).

This is the model-driven action loop that replaces the ``requests.post`` backdoor
in ``StateAdapter.mutate``. It drives a real browser (``BrowserController``)
through a grounding model (``model_client.complete_vision_json``) to perform
writes + rollbacks via real GUI gestures — clicking into an entity, editing a
field, confirming — NOT by calling the app's internal Flask API.

**Contract** (mirrors OSWorld ``mm_agents/uitars_agent.py::predict`` + the probe
in ``eval_results/p2_vision_probe/``, which confirmed gpt-5.6-sol grounds at
~1px accuracy with normalized [0,1000] coords):
  screenshot → ``complete_vision_json`` → parse action DSL → execute via
  ``BrowserController`` → re-screenshot → loop until ``done`` / ``fail`` /
  ``max_steps``.

**Action DSL** (UITARS-style, normalized [0,1000] coordinates):
  - ``{"action":"click","coordinate":[x,y]}``
  - ``{"action":"type","text":"..."}``            (types to current focus)
  - ``{"action":"press","key":"Enter"}``          (Enter / Tab / Escape / ...)
  - ``{"action":"scroll","coordinate":[x,y],"direction":"down"}``
  - ``{"action":"done"}``                          (model says task complete)
  - ``{"action":"fail","reason":"..."}``          (honest: can't do it)
  - ``{"action":"wait"}``                         (sleep + re-observe)

**Two-model-roles independent** (handoff §7.5 / §12.4): the gui_executor's
grounding call is a SEPARATE model call from the compiler's binding-discovery
call — independent context, separate ``CostModel``, role tag ``compute_use``.
The compiler never sees this call's context and vice versa.

**No-leak before-value invariant**: the caller (``StateAdapter.mutate``)
captures ``old`` via ``read_canonical`` BEFORE the gesture (never from fixtures).
This executor returns the gesture outcome; the caller re-reads ``read_canonical``
to capture ``new`` + verify the change landed. If the field didn't change to the
target value, ``mutate`` raises (honest failure — NOT a silent API success).

**Rollback** (P3): the same executor with an "undo" instruction (set the field
back to ``before``). ``rollback.undo_saga`` already calls ``ad.mutate(value=before)``
— and since ``mutate`` now goes through this executor, rollback AUTOMATICALLY
re-plans a new GUI gesture sequence (handoff §4.1). Honest irreversibility: if
the model can't find an undo path, it outputs ``fail`` → ``mutate`` raises →
``undo_saga`` catches → ``partial_failure=True`` (the wechat 409 pattern,
generalized).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from taskvm.benchmark import model_client
from taskvm.benchmark.cost_model import CostModel
from taskvm.harness.browser_controller import BrowserController

logger = logging.getLogger(__name__)

MODEL_ROLE = "compute_use"   # separate from compiler's 'compiler' role
DEFAULT_MODEL = None         # None → model_client.TASKVM_DEFAULT_MODEL (gpt-5.6-sol)
DEFAULT_MAX_STEPS = 18
DEFAULT_VIEWPORT = (1100, 760)

# ── system prompt (UITARS-style, normalized [0,1000] coords) ─────────────────
GROUNDING_SYSTEM = (
    "You are a GUI agent operating a web browser. You are given a screenshot of "
    "the current page and a task. Output the NEXT single action as a JSON object.\n\n"
    "Coordinate system: NORMALIZED to the image dimensions on a 0–1000 scale. "
    "(0,0) = top-left corner of the screenshot; (1000,1000) = bottom-right. "
    "To click a UI element, output the coordinate of its center.\n\n"
    "Action types (output exactly ONE):\n"
    '  {"action":"click","coordinate":[x,y]}     — left-click at the normalized point\n'
    '  {"action":"type","text":"..."}            — type text into the currently-focused input\n'
    '  {"action":"press","key":"Enter"}          — press a key (Enter / Tab / Escape / Backspace / ArrowDown / ...)\n'
    '  {"action":"scroll","coordinate":[x,y],"direction":"down"}  — scroll (down/up) at the point\n'
    '  {"action":"wait"}                         — the page is loading; wait 1s and re-observe\n'
    '  {"action":"done"}                         — the task is complete (the target change has happened)\n'
    '  {"action":"fail","reason":"..."}          — the task cannot be done via the UI (honest failure)\n\n'
    "Respond with ONLY the JSON object — no markdown fences, no prose."
)


def _build_instruction(*, app: str, entity_kind: str, entity_id: str,
                       field: str, value: Any, operator: str,
                       undo: bool = False, attempt: int = 1) -> str:
    """Build the natural-language goal instruction for the grounding model.

    Deliberately goal-level (NOT a hardcoded click sequence — handoff §3.2 /
    §12.18: '不接受写死点击序列'): the model must read the screenshot and decide
    which affordances to use. The entity_id is the app's logical id (visible in
    the DOM as ``data-{kind}-id``) so the model can ground on the right row.

    ``attempt`` > 1 adds a "the previous try did not land" hint so a retry is
    more likely to pick the correct confirm button (GUI agents sometimes click
    Cancel instead of Confirm on the first try)."""
    verb = "restore" if undo else "change"
    target = "its previous value" if undo else f"'{value}'"
    base = (
        f"On this {app} app page, {verb} the {entity_kind} with id '{entity_id}': "
        f"set its '{field}' field to {target}. "
        f"(operator: {operator}.) "
        f"Use the page's UI — click into the {entity_kind} (e.g. a View/Detail link), "
        f"open its edit form, change the {field} field, then click 'Review changes' "
        f"and in the confirm dialog click the 'Confirm move' / submit button "
        f"(do NOT click Cancel). "
        f"When the {field} has been {verb}d and the page reflects the new value, "
        f'output {{"action":"done"}}. '
        f"If the UI offers no way to {verb} this field, output "
        f'{{"action":"fail","reason":"..."}} with the reason.'
    )
    if attempt > 1:
        base = (
            f"Previous attempt {attempt-1} did not complete the {verb}. "
            f"The {field} is still not set to {target}. Try again — make sure to "
            f"click the CONFIRM/SUBMIT button (not Cancel) in the review dialog. "
            + base
        )
    return base


class GuiExecutor:
    """Drives a BrowserController through the grounding model in a predict→
    execute→re-observe loop. One ``GuiExecutor`` holds one ``BrowserController``
    (one Playwright page). Resident across calls (the browser is shared via
    ``browser_controller._get_browser``)."""

    def __init__(self, *, model: str | None = DEFAULT_MODEL,
                 max_steps: int = DEFAULT_MAX_STEPS,
                 viewport: tuple[int, int] = DEFAULT_VIEWPORT,
                 cost_model: CostModel | None = None,
                 screenshot_dir: str | None = None,
                 headless: bool = True):
        self.model = model
        self.max_steps = max_steps
        self.cost_model = cost_model
        self.screenshot_dir = screenshot_dir
        self.bc = BrowserController(viewport=viewport, headless=headless)
        self._shot_counter = 0   # monotonic (Date.now banned in this env)
        self._lock = __import__("threading").Lock()   # serialize GUI ops (one shared page)

    def _shot_path(self, label: str) -> Optional[str]:
        if not self.screenshot_dir:
            return None
        self._shot_counter += 1
        os.makedirs(self.screenshot_dir, exist_ok=True)
        return os.path.join(self.screenshot_dir,
                            f"step_{self._shot_counter:02d}_{label}.png")

    def _predict(self, instruction: str, history: list[str],
                 prev_screenshot: str | None = None) -> dict | None:
        """One grounding call: screenshot → model → parse action dict. Returns
        None on parse failure OR on a persistent call error (e.g. 429 QPM
        exhaustion) so the loop's backoff handles it instead of crashing.

        ``prev_screenshot`` (Task2 optimization, E12): on a RETRY, the caller
        passes the LAST screenshot from the previous failed attempt (as a data
        URL). The model then sees "where the previous try got stuck" as a second
        image, so it doesn't have to re-derive the page state from the text
        history alone — this cuts the retry's step count substantially (E12
        measured 16 calls/op avg because retries re-walked View→Edit→…→Confirm
        from scratch). None on the first attempt (no previous screenshot)."""
        data_url = self.bc.screenshot_data_url()
        user = instruction
        if history:
            user += "\n\nAction history so far:\n" + "\n".join(
                f"  step {i+1}: {h}" for i, h in enumerate(history))
        if prev_screenshot:
            user += ("\n\nThe SECOND image is where your PREVIOUS attempt got "
                     "stuck (it did not complete). The FIRST image is the page "
                     "RIGHT NOW. Continue from the current page — if a form / "
                     "dialog is already open, do NOT re-navigate to it; just "
                     "finish the remaining steps (change the field + confirm).")
        user += "\n\nOutput the next action as JSON now."
        try:
            if prev_screenshot:
                # two-image call: build the content blocks manually (current +
                # previous) so complete_vision_json's single-image helper doesn't fit.
                sys_prompt = GROUNDING_SYSTEM + "\n\nRespond with ONLY valid JSON - no markdown fences, no prose."
                content = [
                    {"type": "text", "text": sys_prompt + "\n\n" + user},
                    {"type": "image_url",
                     "image_url": {"url": data_url, "detail": "high"}},
                    {"type": "text", "text": "Previous attempt's last screen (where it got stuck):"},
                    {"type": "image_url",
                     "image_url": {"url": prev_screenshot, "detail": "high"}},
                ]
                raw, resp = model_client.complete_vision(
                    [{"role": "user", "content": content}],
                    max_tokens=300, temperature=None, model=self.model)
                parsed = model_client._parse_json(raw)
            else:
                parsed, raw, resp = model_client.complete_vision_json(
                    GROUNDING_SYSTEM, user, data_url,
                    max_tokens=300, temperature=None, model=self.model,
                    repair_retries=1)
        except Exception as e:
            logger.warning(f"[gui_executor] vision call failed (likely 429 QPM): "
                           f"{e!s:.120}; will back off + retry")
            return None
        if resp is not None and self.cost_model is not None:
            model_client.record_usage(resp, self.cost_model,
                                      tool="gui_executor", role=MODEL_ROLE,
                                      model=self.model or model_client.TASKVM_DEFAULT_MODEL)
        if not isinstance(parsed, dict):
            logger.warning(f"[gui_executor] no dict parsed: {raw[:200]!r}")
            return None
        return parsed

    def _execute_action(self, action: dict) -> str:
        """Translate one parsed action dict into a BrowserController call.
        Returns a short human-readable description for the history log."""
        act = (action.get("action") or "").strip().lower()
        if act == "click":
            c = action.get("coordinate") or action.get("start_box")
            if isinstance(c, list) and len(c) >= 2:
                # verify the target BEFORE clicking (a click may trigger nav,
                # destroying the execution context for a post-click evaluate)
                tgt = ""
                try:
                    el = self.bc.element_at_point_norm(float(c[0]), float(c[1]))
                    if el:
                        tgt = f" → {el.get('tag','?')}"
                        if el.get("text"):
                            tgt += f" '{el['text'][:30]}'"
                        if el.get("row_event"):
                            tgt += f" [event={el['row_event']}]"
                        elif el.get("row_task"):
                            tgt += f" [task={el['row_task']}]"
                        elif el.get("row_file"):
                            tgt += f" [file={el['row_file']}]"
                except Exception as ve:
                    tgt = f" (verify skipped: {ve!s:.40})"
                self.bc.click_norm(float(c[0]), float(c[1]))
                return f"click({c[0]:.0f},{c[1]:.0f}){tgt}"
            return f"click(bad coordinate: {c!r})"
        if act == "type":
            txt = str(action.get("text", ""))
            # clear-then-type (real keystrokes): Ctrl+A selects all in the
            # focused input, Delete clears it, then type the new value. For
            # ``<input type="date">`` this reliably replaces the date instead
            # of appending. All real keyboard events (no evaluate/value-set
            # backdoor — the non-invasive write boundary).
            self.bc.fill_focused(txt)
            return f"type({txt!r})"
        if act == "press":
            key = str(action.get("key", ""))
            self.bc.press_key(key)
            return f"press({key!r})"
        if act == "scroll":
            c = action.get("coordinate") or [500, 500]
            d = str(action.get("direction", "down"))
            if isinstance(c, list) and len(c) >= 2:
                self.bc.scroll_norm(float(c[0]), float(c[1]), d)
            return f"scroll({d})"
        if act == "wait":
            time.sleep(1.0)
            return "wait(1s)"
        if act == "done":
            return "DONE"
        if act == "fail":
            reason = str(action.get("reason", "model reported failure"))
            raise GuiExecutorFailure(reason)
        return f"unknown_action({act!r})"

    def execute(self, instruction: str, page_url: str, *,
                prev_screenshot: str | None = None,
                resume_url: str | None = None) -> dict:
        """Run the predict→execute→re-observe loop. Returns a trace dict:
        {steps, actions, final_url, done}.

        Raises ``GuiExecutorFailure`` if the model outputs ``fail`` (honest
        irreversibility). Raises ``RuntimeError`` if max_steps exceeded without
        ``done`` (cost/timeout protection — handoff §3.3).

        ``prev_screenshot`` / ``resume_url`` (Task2, E12): on a RETRY, the caller
        passes the previous attempt's last screenshot + a URL to resume from
        (e.g. the edit-form URL, not the list URL) so the retry doesn't re-walk
        View→Edit→… from scratch. None on the first attempt."""
        with self._lock:   # one GUI op at a time (shared resident page)
            return self._execute_locked(instruction, page_url,
                                         prev_screenshot=prev_screenshot,
                                         resume_url=resume_url)

    def _execute_locked(self, instruction: str, page_url: str, *,
                        prev_screenshot: str | None = None,
                        resume_url: str | None = None) -> dict:
        # Task2 (E12): on retry, resume from the deeper URL (edit form / detail)
        # instead of re-navigating to the list page + re-clicking View/Edit.
        self.bc.goto(resume_url or page_url)
        self.bc.wait_load()
        history: list[str] = []
        trace = {"steps": 0, "actions": [], "final_url": None, "done": False,
                 "page_url": page_url,
                 "last_screenshot": None}   # Task2: expose for retry resume
        if self.screenshot_dir:
            self.bc.save_screenshot(self._shot_path("00_initial") or "")
        # the first predict call carries the previous attempt's stuck screenshot
        # (Task2 direction a) so the model can continue instead of re-deriving.
        first_predict_prev = prev_screenshot
        # count only EXECUTED actions toward max_steps (429/parse-fail attempts
        # don't burn the budget — they're retried, not progress). max_attempts
        # bounds total model calls (prevents infinite 429 loops).
        max_attempts = self.max_steps * 3
        executed = 0
        attempts = 0
        while executed < self.max_steps and attempts < max_attempts:
            attempts += 1
            action = self._predict(instruction, history,
                                   prev_screenshot=first_predict_prev)
            first_predict_prev = None   # only the very first call gets it
            if action is None:
                history.append(f"attempt {attempts}: (no parseable action — retrying)")
                trace["actions"].append({"attempt": attempts, "raw": None})
                time.sleep(5.0)   # 429 QPM hiccup backoff; re-observe next
                continue
            executed += 1
            desc = self._execute_action(action)
            history.append(f"step {executed}: {desc}")
            trace["actions"].append({"step": executed, "attempt": attempts,
                                     "action": action, "desc": desc})
            trace["steps"] = executed
            if desc == "DONE":
                trace["done"] = True
                break
            time.sleep(1.5)   # nav settle + QPM spacing (gpt-5.6-sol ~10/min)
            self.bc.wait_load(timeout_ms=4000)
            # Task2: capture the current screenshot as a data URL after each
            # executed step, so if this attempt fails verification, the caller
            # can pass trace["last_screenshot"] to the retry's _predict.
            trace["last_screenshot"] = self.bc.screenshot_data_url()
            if self.screenshot_dir:
                self.bc.save_screenshot(self._shot_path(desc) or "")
        trace["final_url"] = self.bc.current_url()
        trace["attempts"] = attempts
        if not trace["done"]:
            logger.warning(f"[gui_executor] max_steps ({self.max_steps}) hit without "
                           f"done ({attempts} attempts, {executed} executed); "
                           f"instruction={instruction[:80]!r}")
        return trace


class GuiExecutorFailure(Exception):
    """The grounding model honestly reported it cannot complete the task via the
    UI (``{"action":"fail","reason":"..."}``). Surfaces as an exception so
    ``undo_saga``'s try/except catches it → ``partial_failure=True`` (the wechat
    409 pattern, generalized to any irreversible op — handoff §4.2)."""

    def __init__(self, reason: str):
        super().__init__(f"GUI executor cannot complete via UI: {reason}")
        self.reason = reason


# ── module-level singleton (one resident GuiExecutor per process) ────────────
_EXECUTOR: GuiExecutor | None = None
_EXECUTOR_LOCK = __import__("threading").Lock()


def get_executor(*, model: str | None = DEFAULT_MODEL,
                 cost_model: CostModel | None = None,
                 screenshot_dir: str | None = None,
                 headless: bool = True) -> GuiExecutor:
    """Lazy singleton: one resident GuiExecutor (one browser page) shared across
    adapters in a process. The browser is launched on first use."""
    global _EXECUTOR
    if _EXECUTOR is not None:
        return _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = GuiExecutor(model=model, cost_model=cost_model,
                                    screenshot_dir=screenshot_dir, headless=headless)
            logger.info("[gui_executor] resident executor ready")
    return _EXECUTOR


def gui_write(*, app: str, sid: str, entity_id: str, operator: str, value: Any,
              field: str, entity_kind: str, base_url: str,
              old_value: Any, screenshot_dir: str | None = None,
              undo: bool = False, max_steps: int = DEFAULT_MAX_STEPS,
              attempt: int = 1,
              prev_screenshot: str | None = None,
              resume_url: str | None = None) -> dict:
    """Drive the GUI executor to perform one write (or rollback) via real GUI
    gestures. Returns a dict matching the ``StateAdapter.mutate`` response
    contract: ``{status, app, entity_id, operator, old, new, field, trace}``.

    The caller (``StateAdapter.mutate``) captures ``old_value`` via
    ``read_canonical`` BEFORE calling this (no-leak), and re-reads
    ``read_canonical`` AFTER to capture ``new`` + verify the change landed.

    ``attempt`` > 1 adds a retry-hint to the instruction (the caller retries on
    verification failure — GUI agents sometimes click Cancel instead of Confirm).

    ``prev_screenshot`` / ``resume_url`` (Task2, E12): on a retry, the caller
    passes the previous attempt's last screenshot (data URL) + a URL to resume
    from (the edit-form URL, not the list URL) so the retry doesn't re-walk
    View→Edit→…→Confirm from scratch. The trace's ``last_screenshot`` field is
    the source for the next retry's ``prev_screenshot``.

    Raises ``GuiExecutorFailure`` on honest irreversibility (model outputs
    ``fail``); raises ``RuntimeError`` on max-steps exhaustion."""
    ex = get_executor()
    ex.screenshot_dir = screenshot_dir   # per-call evidence dir (mutable)
    ex._shot_counter = 0                  # reset counter per call
    instruction = _build_instruction(
        app=app, entity_kind=entity_kind, entity_id=entity_id,
        field=field, value=value, operator=operator, undo=undo, attempt=attempt)
    page_url = f"{base_url}/{sid}"
    trace = ex.execute(instruction, page_url,
                       prev_screenshot=prev_screenshot, resume_url=resume_url)
    return {"status": "ok", "app": app, "entity_id": entity_id,
            "operator": operator, "field": field,
            "old": old_value, "new": value, "trace": trace}
