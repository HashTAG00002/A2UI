"""gui_executor — the GUI Agent WRITE/ROLLBACK CUA loop (E10 rework; Agent B
substrate isolation rework).

The model-driven action loop that performs writes + rollbacks via REAL GUI
gestures through a **SubstrateSession port** (``taskvm.substrate``): the
loop is substrate-blind — give it any port session (Web / MobileGym /
OSWorld / fake) and it drives screenshot → grounding model → gesture →
re-observe until ``done`` / ``fail`` / ``max_steps``. The legacy direct
``BrowserController`` dependency (a Web-specific implementation import in
the execution layer) is gone.

**Action DSL** (UITARS-style, normalized [0,1000] coordinates):
  - ``{"action":"click","coordinate":[x,y]}``
  - ``{"action":"type","text":"..."}``            (types to current focus)
  - ``{"action":"press","key":"Enter"}``
  - ``{"action":"scroll","coordinate":[x,y],"direction":"down"}``
  - ``{"action":"done"}`` / ``{"action":"fail","reason":"..."}``
  - ``{"action":"wait"}``

**Honest verification (Agent B change)**: the old flow re-read the app's
canonical state (``read_canonical`` — an evaluation-plane power) to verify
the write landed. That is no longer reachable from the runtime. The
executor instead re-observes through the session (visible observation),
and the caller (``execution.gui_driver``) judges from visible text. If
the change is not visibly verifiable, that is reported honestly — never
papered over with an oracle read.

**Rollback**: same loop with an "undo" instruction. Honest
irreversibility: model outputs ``fail`` → ``GuiExecutorFailure`` → the
caller marks ``partial_failure`` (the wechat 409 pattern, generalized).
"""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Optional

from taskvm.benchmark import model_client
from taskvm.benchmark.cost_model import CostModel
from taskvm.execution.grounding_backend import (GROUNDING_SYSTEM, GroundingBackend,
                                                make_grounding_backend)
from taskvm.substrate import GuiAction, SubstrateSession
# GG.3: instructions are generated from the VISIBLE locator — zero internal
# id, zero operator jargon (see taskvm.architect.serializer — the LLM
# SubgoalGenerator was deleted in the Agent-C role collapse).
# Agent-C role collapse: the LLM SubgoalGenerator hot path is DELETED.
# The CUA instruction is now deterministically serialised from the visible
# locator (taskvm.architect.serializer.patchop_cua_goal) — zero model calls,
# zero candidates, and NO mock=True/False runtime fork.
from taskvm.architect.serializer import patchop_cua_goal
from taskvm.governance.translate import FIELD_DISPLAY, entity_id_to_locator

logger = logging.getLogger(__name__)

MODEL_ROLE = "compute_use"   # separate from compiler's 'compiler' role
DEFAULT_MODEL = None         # None → model_client.TASKVM_DEFAULT_MODEL (gpt-5.6-sol)
DEFAULT_MAX_STEPS = 18
DEFAULT_VIEWPORT = (1100, 760)
DEFAULT_BACKEND = "gpt56sol"   # EE.6: the pre-EE.6 behavior (E13/E15 baseline)


def _artifact_to_data_url(artifact) -> str:
    """VisualArtifact → data URL (inline bytes or ref passthrough)."""
    if artifact.data:
        return ("data:image/png;base64,"
                + base64.b64encode(artifact.data).decode())
    return artifact.ref or ""


class GuiExecutor:
    """Substrate-blind predict→execute→re-observe loop. One executor may
    drive any port session; sessions are passed per ``execute`` call."""

    def __init__(self, *, model: str | None = DEFAULT_MODEL,
                 max_steps: int = DEFAULT_MAX_STEPS,
                 cost_model: CostModel | None = None,
                 screenshot_dir: str | None = None,
                 backend: GroundingBackend | None = None,
                 backend_name: str = DEFAULT_BACKEND):
        self.model = model
        self.max_steps = max_steps
        self.cost_model = cost_model
        self.screenshot_dir = screenshot_dir
        # EE.6: hot-swappable grounding backend (default gpt56sol = the
        # pre-EE.6 E13/E15 baseline → zero regression).
        self.backend = backend or make_grounding_backend(
            backend_name, model=model, cost_model=cost_model)
        self._shot_counter = 0   # monotonic (Date.now banned in this env)
        self._lock = __import__("threading").Lock()

    def _shot_path(self, label: str) -> Optional[str]:
        if not self.screenshot_dir:
            return None
        self._shot_counter += 1
        os.makedirs(self.screenshot_dir, exist_ok=True)
        return os.path.join(self.screenshot_dir,
                            f"step_{self._shot_counter:02d}_{label}.png")

    def _screenshot_data_url(self, session: SubstrateSession) -> str:
        surface = self._primary_surface(session)
        return _artifact_to_data_url(session.capture(surface))

    def _primary_surface(self, session: SubstrateSession):
        surfaces = session.list_surfaces()
        return surfaces[0] if surfaces else None

    def _predict(self, session: SubstrateSession, instruction: str,
                 history: list[str],
                 prev_screenshot: str | None = None) -> dict | None:
        """One grounding call: screenshot → model → parse action dict.
        Returns None on parse failure OR persistent call error so the
        loop's backoff handles it instead of crashing.

        ``prev_screenshot`` (Task2, E12): on a RETRY the caller passes the
        LAST screenshot from the previous failed attempt so the model sees
        where the prior try got stuck."""
        data_url = self._screenshot_data_url(session)
        return self.backend.predict_action(data_url, instruction, history,
                                            prev_screenshot)

    def _execute_action(self, session: SubstrateSession, action: dict,
                        epoch: str) -> str:
        """Translate one model action dict into a port ``GuiAction`` and
        perform it through the session. Returns a short description for
        the history log."""
        act = (action.get("action") or "").strip().lower()
        surface = self._primary_surface(session)
        if act == "click":
            c = action.get("coordinate") or action.get("start_box")
            if isinstance(c, list) and len(c) >= 2:
                ga = GuiAction(kind="click",
                               coordinate=(float(c[0]), float(c[1])))
                receipt = session.act(surface, ga, epoch=epoch)
                return f"click({c[0]:.0f},{c[1]:.0f}){(' → ' + receipt.detail) if receipt.detail else ''}"
            return f"click(bad coordinate: {c!r})"
        if act == "type":
            txt = str(action.get("text", ""))
            ga = GuiAction(kind="type", text=txt)
            session.act(surface, ga, epoch=epoch)
            return f"type({txt!r})"
        if act == "press":
            key = str(action.get("key", ""))
            ga = GuiAction(kind="key", key=key)
            session.act(surface, ga, epoch=epoch)
            return f"press({key!r})"
        if act == "scroll":
            c = action.get("coordinate") or [500, 500]
            d = str(action.get("direction", "down"))
            ga = GuiAction(kind="scroll",
                           coordinate=(float(c[0]), float(c[1]))
                           if isinstance(c, list) and len(c) >= 2 else None,
                           direction=d)
            session.act(surface, ga, epoch=epoch)
            return f"scroll({d})"
        if act == "wait":
            ga = GuiAction(kind="wait", duration_ms=1000)
            session.act(surface, ga, epoch=epoch)
            return "wait(1s)"
        if act == "done":
            return "DONE"
        if act == "fail":
            reason = str(action.get("reason", "model reported failure"))
            raise GuiExecutorFailure(reason)
        return f"unknown_action({act!r})"

    def execute(self, instruction: str, page_url: str | None = None, *,
                session: SubstrateSession,
                prev_screenshot: str | None = None,
                epoch: str = "") -> dict:
        """Run the predict→execute→re-observe loop on one port session.

        ``page_url`` (Web sessions): the surface entry URL — the loop
        navigates there first with a real ``open`` gesture. Mobile/OS
        sessions ignore it (their surfaces open via app ``open`` actions).

        Raises ``GuiExecutorFailure`` on honest model-reported
        irreversibility; returns ``done=False`` when max_steps is hit."""
        with self._lock:   # one GUI op at a time per executor
            return self._execute_locked(instruction, page_url,
                                        session=session,
                                        prev_screenshot=prev_screenshot,
                                        epoch=epoch)

    def _execute_locked(self, instruction: str, page_url: str | None, *,
                        session: SubstrateSession,
                        prev_screenshot: str | None = None,
                        epoch: str = "") -> dict:
        surface = self._primary_surface(session)
        trace = {"steps": 0, "actions": [], "final_url": None, "done": False,
                 "page_url": page_url, "last_screenshot": None,
                 "epoch": epoch}
        if page_url:
            # GG.4: navigate from the app root / list page only — never a
            # deep link synthesized from an internal id.
            session.act(surface, GuiAction(kind="open", target=page_url),
                        epoch=epoch)
            session.act(surface, GuiAction(kind="wait", duration_ms=500),
                        epoch=epoch)
        history: list[str] = []
        if self.screenshot_dir:
            path = self._shot_path("00_initial")
            if path:
                art = session.capture(surface)
                if art.data:
                    with open(path, "wb") as f:
                        f.write(art.data)
        first_predict_prev = prev_screenshot
        max_attempts = self.max_steps * 3
        executed = 0
        attempts = 0
        while executed < self.max_steps and attempts < max_attempts:
            attempts += 1
            action = self._predict(session, instruction, history,
                                   prev_screenshot=first_predict_prev)
            first_predict_prev = None   # only the very first call gets it
            if action is None:
                history.append(f"attempt {attempts}: (no parseable action — retrying)")
                trace["actions"].append({"attempt": attempts, "raw": None})
                time.sleep(5.0)   # 429 QPM hiccup backoff; re-observe next
                continue
            executed += 1
            desc = self._execute_action(session, action, epoch=epoch or "anon")
            history.append(f"step {executed}: {desc}")
            trace["actions"].append({"step": executed, "attempt": attempts,
                                     "action": action, "desc": desc})
            trace["steps"] = executed
            if desc == "DONE":
                trace["done"] = True
                break
            time.sleep(1.5)   # nav settle + QPM spacing
            trace["last_screenshot"] = self._screenshot_data_url(session)
            if self.screenshot_dir:
                path = self._shot_path(desc)
                if path:
                    art = session.capture(surface)
                    if art.data:
                        with open(path, "wb") as f:
                            f.write(art.data)
        trace["attempts"] = attempts
        if not trace["done"]:
            logger.warning(f"[gui_executor] max_steps ({self.max_steps}) hit without "
                           f"done ({attempts} attempts, {executed} executed); "
                           f"instruction={instruction[:80]!r}")
        return trace


class GuiExecutorFailure(Exception):
    """The grounding model honestly reported it cannot complete the task via
    the UI (``{"action":"fail","reason":"..."}``). Surfaces as an exception so
    callers catch it → ``partial_failure=True`` (honest irreversibility)."""

    def __init__(self, reason: str):
        super().__init__(f"GUI executor cannot complete via UI: {reason}")
        self.reason = reason


# ── module-level singleton (one resident executor per backend per process) ──
_EXECUTORS: dict[str, GuiExecutor] = {}
_EXECUTOR_LOCK = __import__("threading").Lock()


def get_executor(*, model: str | None = DEFAULT_MODEL,
                 cost_model: CostModel | None = None,
                 screenshot_dir: str | None = None,
                 backend_name: str = DEFAULT_BACKEND,
                 backend: GroundingBackend | None = None) -> GuiExecutor:
    """Lazy singleton per backend_name (EE.6 model-ablation support). The
    executor is substrate-blind; sessions are passed per execute call."""
    key = backend.name if backend is not None else backend_name
    if key in _EXECUTORS:
        return _EXECUTORS[key]
    with _EXECUTOR_LOCK:
        if key not in _EXECUTORS:
            _EXECUTORS[key] = GuiExecutor(
                model=model, cost_model=cost_model,
                screenshot_dir=screenshot_dir,
                backend=backend, backend_name=backend_name)
            logger.info(f"[gui_executor] resident executor ready (backend={key})")
    return _EXECUTORS[key]


def gui_write(*, app: str, sid: str, entity_id: str, operator: str, value: Any,
              field: str, entity_kind: str,
              session: SubstrateSession,
              base_url: str | None = None,
              old_value: Any = None, screenshot_dir: str | None = None,
              undo: bool = False, max_steps: int = DEFAULT_MAX_STEPS,
              attempt: int = 1,
              prev_screenshot: str | None = None,
              resume_url: str | None = None,   # GG.4-deprecated, ignored
              backend_name: str = DEFAULT_BACKEND,
              instruction: str | None = None,
              visible_anchor_title: str | None = None) -> dict:
    """Drive the CUA loop to perform one write (or rollback) via real GUI
    gestures on a port session. Returns a dict matching the legacy
    ``mutate`` response contract: ``{status, app, entity_id, operator,
    old, new, field, trace}``.

    Instruction sources (GG.3 + Agent-C — visible-locator text, zero
      internal ids, zero model calls for instruction generation):
      - ``instruction``: caller-supplied NL, used verbatim;
      - else DETERMINISTIC serialisation from the entity's VISIBLE title
        (``visible_anchor_title``, provided by the composition root's
        anchor lookup — never an oracle read inside the executor);
      - without a title the subgoal generator emits the honest
        cannot-locate sentinel and the model is told so.

    ``old_value``/``new`` in the response are caller-context only — the
    executor verifies through the session's VISIBLE observation, not a
    canonical/oracle read."""
    ex = get_executor(backend_name=backend_name)
    ex.screenshot_dir = screenshot_dir   # per-call evidence dir (mutable)
    ex._shot_counter = 0                  # reset counter per call
    if instruction is None:
        canonical_entities: dict[str, dict[str, Any]] = {}
        if visible_anchor_title:
            # minimal visible-anchor view: the TITLE only (screen-visible,
            # GG.3-legal); no hidden fields travel into the prompt
            canonical_entities = {entity_id: {"title": visible_anchor_title}}
        locator = entity_id_to_locator(app, entity_id, canonical_entities,
                                       field=field)
        instruction = patchop_cua_goal(
            surface_label=app, visible_locator=locator,
            field_display=FIELD_DISPLAY.get(field, field),
            target_value=value, restore=undo, attempt=attempt)
    if attempt > 1:
        instruction += (f" (attempt {attempt}: a previous try got stuck — "
                        f"use the visible page to get further this time)")
    page_url = f"{base_url.rstrip('/')}/{sid}" if base_url else None
    trace = ex.execute(instruction, page_url, session=session,
                       prev_screenshot=prev_screenshot,
                       epoch=f"write:{app}:{sid}")
    return {"status": "ok", "app": app, "entity_id": entity_id,
            "operator": operator, "field": field,
            "old": old_value, "new": value, "trace": trace}
