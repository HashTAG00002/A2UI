"""GroundingBackend ABC — hot-swappable vision grounding models (EE.6).

Abstracts the GUI executor's ``_predict`` model call so different vision models
can be swapped without touching the predict→execute→re-observe loop. This solves
the "results only hold on gpt-5.6-sol" reviewer attack (handoff EE.6) + provides
the model-ablation table's backbone (gpt56sol vs glm5v vs uitars).

All backends implement the SAME contract:
  input  = screenshot_dataurl (str) + instruction (str) + history (list[str])
           + prev_screenshot (str|None, the retry's stuck-screen, E12 Task2)
  output = dict {"action": "click"|"type"|"press"|"scroll"|"done"|"fail"|"wait",
                  "coordinate": [x,y] (normalized 0-1000), "text": str|None, ...}
           OR None (parse failure / 429 QPM — the loop's backoff handles it)

Backends:
  GPT56SolBackend — gpt-5.6-sol (current default, E13/E15 verified, ~1px accuracy)
  GLM5VBackend    — glm-5v-turbo (公司网关畅通, 大纲附录 B.2; vision-capable)
  UITarsBackend   — UI-TARS (stub: raise NotImplementedError with the OSWorld
                    ``mm_agents/uitars_agent.py`` reference path — interface complete
                    but no weight download, per handoff "不用真实跑 UITarsBackend")

The two-image retry path (current + prev_screenshot) is built into the contract
so the E12 Task2 optimization survives the refactor.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

from taskvm.benchmark import model_client
from taskvm.benchmark.cost_model import CostModel

logger = logging.getLogger(__name__)

MODEL_ROLE = "compute_use"   # separate from compiler's 'compiler' role (§7.5)

# ── system prompt (UITARS-style, normalized [0,1000] coords) ─────────────────
# Lives here (not in gui_executor) because it's the backend's contract — the
# prompt that turns a screenshot into one action dict. gui_executor builds the
# GOAL instruction; the backend owns the action-format prompt.
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


class GroundingBackend(ABC):
    """One vision grounding model that turns a screenshot + instruction into one
    action dict. Hot-swappable — the GUI executor's loop is backend-agnostic."""

    @abstractmethod
    def predict_action(self, screenshot: str, instruction: str,
                       history: list[str],
                       prev_screenshot: str | None = None) -> dict | None:
        """One grounding call. Returns the parsed action dict, or None on parse
        failure / persistent call error (429 QPM) so the loop backoffs."""
        ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def model(self) -> str | None:
        return getattr(self, "_model", None)


class _OpenAIVisionBackend(GroundingBackend):
    """Shared impl for OpenAI-compatible vision models on the company gateway
    (gpt-5.6-sol, glm-5v-turbo). Subclasses set ``_default_model``. The two-image
    retry path (current + prev_screenshot, E12 Task2) builds content blocks
    manually since ``complete_vision_json`` is single-image only."""

    _default_model: str | None = None   # None → model_client.TASKVM_DEFAULT_MODEL

    def __init__(self, *, model: str | None = None,
                 cost_model: CostModel | None = None):
        self._model = model or self._default_model
        self.cost_model = cost_model

    def predict_action(self, screenshot: str, instruction: str,
                       history: list[str],
                       prev_screenshot: str | None = None) -> dict | None:
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
                sys_prompt = GROUNDING_SYSTEM + ("\n\nRespond with ONLY valid "
                                                 "JSON - no markdown fences, no prose.")
                content = [
                    {"type": "text", "text": sys_prompt + "\n\n" + user},
                    {"type": "image_url",
                     "image_url": {"url": screenshot, "detail": "high"}},
                    {"type": "text", "text": "Previous attempt's last screen (where it got stuck):"},
                    {"type": "image_url",
                     "image_url": {"url": prev_screenshot, "detail": "high"}},
                ]
                raw, resp = model_client.complete_vision(
                    [{"role": "user", "content": content}],
                    max_tokens=300, temperature=None, model=self._model)
                parsed = model_client._parse_json(raw)
            else:
                parsed, raw, resp = model_client.complete_vision_json(
                    GROUNDING_SYSTEM, user, screenshot,
                    max_tokens=300, temperature=None, model=self._model,
                    repair_retries=1)
        except Exception as e:
            logger.warning(f"[{self.name}] vision call failed (likely 429 QPM): "
                           f"{e!s:.120}; will back off + retry")
            return None
        if resp is not None and self.cost_model is not None:
            model_client.record_usage(resp, self.cost_model,
                                      tool=f"gui_executor:{self.name}",
                                      role=MODEL_ROLE,
                                      model=self._model or model_client.TASKVM_DEFAULT_MODEL)
        if not isinstance(parsed, dict):
            logger.warning(f"[{self.name}] no dict parsed: {raw[:200]!r}")
            return None
        return parsed


class GPT56SolBackend(_OpenAIVisionBackend):
    """The current primary grounding model (gpt-5.6-sol). E13/E15 verified at
    ~1px accuracy with normalized [0,1000] coords (eval_results/p2_vision_probe/).
    This is the DEFAULT backend — hot-swapping it for GLM5V/UITars is the point
    of EE.6's model-ablation defense."""
    _default_model = None   # → model_client.TASKVM_DEFAULT_MODEL (gpt-5.6-sol)

    @property
    def name(self) -> str:
        return "gpt56sol"


class GLM5VBackend(_OpenAIVisionBackend):
    """glm-5v-turbo — the company-gateway vision-capable backup (大纲附录 B.2:
    `glm-5v-turbo` 已验证支持 vision image_url + data:image base64). Same
    OpenAI-compatible contract as gpt-5.6-sol, so the same _OpenAIVisionBackend
    impl — only the model name differs. Used in the model-ablation table to
    defend against "results only hold on one model" reviewer attacks."""
    _default_model = "glm-5v-turbo"

    @property
    def name(self) -> str:
        return "glm5v"


class UITarsBackend(GroundingBackend):
    """UI-TARS grounding backend — STUB (interface complete, no weight download).

    The handoff (EE.6) says: "不用真实跑 UITarsBackend（它需要大模型权重），只
    需要 stub 实现（raise NotImplementedError with helpful message），让接口完整
    即可". The reference implementation is
    ``/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/OSWorld/mm_agents/uitars_agent.py``
    — its ``predict`` takes a screenshot + instruction and returns a UITARS-style
    action string. Wiring it for real requires the UI-TARS model weights (not
    downloaded). The stub keeps the interface complete so the ablation table can
    list UITars as a "pluggable" backend without claiming a result we didn't run.
    """

    UITARS_REF = ("/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/"
                  "OSWorld/mm_agents/uitars_agent.py")

    def __init__(self, *, model: str | None = None,
                 cost_model: CostModel | None = None):
        self._model = model or "uitars-7b"
        self.cost_model = cost_model

    @property
    def name(self) -> str:
        return "uitars"

    def predict_action(self, screenshot: str, instruction: str,
                       history: list[str],
                       prev_screenshot: str | None = None) -> dict | None:
        raise NotImplementedError(
            f"UITarsBackend is a stub (EE.6). The reference implementation is at\n"
            f"  {self.UITARS_REF}\n"
            f"To wire it for real: study its `predict(screenshot, instruction) -> "
            f"action_string` contract, load the UI-TARS model weights, and adapt "
            f"its action-string format to this backend's dict output. The model "
            f"weights are not downloaded in this environment, so this backend is "
            f"interface-only — do NOT cite a UITars result without running it.")


# ── factory ──────────────────────────────────────────────────────────────────
_BACKENDS = {"gpt56sol": GPT56SolBackend, "glm5v": GLM5VBackend,
             "uitars": UITarsBackend}


def make_grounding_backend(name: str = "gpt56sol", *,
                            model: str | None = None,
                            cost_model: CostModel | None = None) -> GroundingBackend:
    """Factory. ``name`` ∈ {'gpt56sol','glm5v','uitars'}. ``model`` overrides the
    backend's default model id (e.g. for pointing gpt56sol at a different snapshot).
    Default 'gpt56sol' preserves the pre-EE.6 behavior (E13/E15 baseline)."""
    cls = _BACKENDS.get(name)
    if cls is None:
        raise ValueError(f"unknown grounding backend {name!r}; known: {list(_BACKENDS)}")
    return cls(model=model, cost_model=cost_model)
