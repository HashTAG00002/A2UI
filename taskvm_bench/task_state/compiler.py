"""Compiler — the gate-critical model step.

Calls the frontier UI-generation model (role 1) to compile rendered app
observations (screenshot/DOM/a11y/tool-schema) into a typed task-state graph +
binding. This is what W1 tests: can a frontier model, given only rendered
observations, discover the correct binding? PASS requires "no hand-written
binding."

**No-leak**: imports ONLY ``harness/observations`` (compiler-input types) +
``benchmark/a2ui_spec`` (the spec string) + ``benchmark/model_client`` (the API
client) + ``task_state/entity_binding`` (OPERATOR_REGISTRY for the tool schema).
It NEVER imports ``benchmark/fixtures`` (verifier-only GT) or ``verifier/*``.

**Two model roles**: W1 invokes only role (1) UI-gen/compiler live. Role (2)
compute-use is specified (same client, independent call, no shared context) but
its live invocation = W2+; W1 execution is app-API (``execution/``).
"""
from __future__ import annotations

import logging
from typing import Any

from taskvm_bench.benchmark.a2ui_spec import compiler_system_prompt
from taskvm_bench.benchmark import model_client
from taskvm_bench.benchmark.cost_model import CostModel
from taskvm_bench.harness.observations import TraceFixture
from taskvm_bench.task_state.entity_binding import build_tool_schema

logger = logging.getLogger(__name__)

MODEL_ROLE = "compiler"   # role 1 (UI-gen); role 2 = "compute_use" (W2+)


def build_user_prompt(trace: TraceFixture, observed_entity_ids: dict[str, set[str]] | None = None) -> str:
    """The user-message body: task goal + per-app observations + tool schema.

    GG red-line §0: the prompt contains ONLY what a user sees on screen — the
    accessibility-tree text (visible title + visible fields). The raw DOM HTML
    is NO LONGER included (it carried data-field attrs + URL entity_id paths —
    not screen-visible). The old ``# Valid entity ids`` whitelist is GONE (it
    leaked entity_id into the model input); the model addresses entities by
    their visible title (``locator``), which it reads from the a11y.

    ``observed_entity_ids`` is kept in the signature for backward-compat but is
    IGNORED (GG removed the whitelist). Callers still pass it; it is unused."""
    lines = [f"# Task", f"task_id: {trace.task_id}", f"goal: {trace.goal}", ""]
    lines.append("# Observed application states (read-only — compile from these)")
    for app, obs in trace.final_obs.items():
        lines.append(f"\n## App: {app}")
        lines.append("### Accessibility tree (primary text input — visible screen content):")
        lines.append(obs.a11y_text)
    lines.append("")
    lines.append(build_tool_schema(list(trace.final_obs.keys())))
    lines.append("")
    lines.append("Compile the task_binding now. Address each entity by its VISIBLE "
                 "TITLE (the `locator` field), NOT any internal id. Output ONLY the "
                 'JSON object: {"text_response": "...", "a2ui": [...], '
                 '"task_binding": {...}}')
    return "\n".join(lines)


def compile_binding(trace: TraceFixture, observed_entity_ids: dict[str, set[str]] | None = None,
                    *, model: str | None = None, temperature: float | None = None,
                    max_tokens: int = 3072, cost_model: CostModel | None = None,
                    binding_only: bool = True,
                    screenshots: dict[str, str] | None = None) -> dict:
    """Call the frontier model and return:
        {"text_response", "a2ui", "task_binding", "raw", "parsed", "ok", "error"}
    ``task_binding`` is the gate-critical output (None on parse/structure failure).
    ``ok`` is True iff a binding was parsed (structural validation is the caller's
    job via the (now deleted) legacy W1 validation entry).

    ``binding_only=True`` (W1 default) directs the model to emit task_binding
    FIRST and treat a2ui as optional/minimal — the documented W1 fallback when
    the model over-spends tokens on the A2UI surface and starves the binding
    (doc §10 de-prioritizes fancy UI). Pass False for the full-A2UI check.

    ``screenshots`` (EE.10, §7.1): ``{app: data_url}`` base64 screenshots of each
    app's rendered page. When non-empty, the compiler calls
    ``complete_vision_json`` (screenshot + a11y + DOM — the §7.1 "screenshot+a11y
    encoder") instead of the text-only ``complete_json``. The FIRST app's
    screenshot is the image input (one-image API limit; the a11y/DOM for ALL apps
    stays in the text prompt so the model sees every app's state). None/empty →
    the text-only path (backward compat, the W1 baseline)."""
    sys_prompt = compiler_system_prompt(binding_only=binding_only)
    user_prompt = build_user_prompt(trace, observed_entity_ids)

    if screenshots:
        # EE.10: vision path — pick the first app's screenshot as the image input
        # (complete_vision_json is single-image; all apps' a11y/DOM stay in text).
        first_app = next(iter(trace.final_obs))
        img_url = screenshots.get(first_app) or next(iter(screenshots.values()))
        parsed, raw, resp = model_client.complete_vision_json(
            sys_prompt, user_prompt, img_url, max_tokens=max_tokens,
            temperature=temperature, model=model, repair_retries=1)
        vision_used = True
    else:
        parsed, raw, resp = model_client.complete_json(
            sys_prompt, user_prompt, max_tokens=max_tokens,
            temperature=temperature, model=model)
        vision_used = False

    # record cost (real token usage) if a cost model is attached
    if cost_model is not None and resp is not None:
        model_client.record_usage(
            resp, cost_model,
            tool="compile_binding" + (":vision" if vision_used else ""),
            role=MODEL_ROLE,
            model=model or model_client.TASKVM_DEFAULT_MODEL)

    out: dict[str, Any] = {"raw": raw, "parsed": parsed, "ok": False,
                           "text_response": None, "a2ui": None, "task_binding": None,
                           "error": None, "vision": vision_used}
    if parsed is None:
        out["error"] = "json_parse_failure"
        return out
    out["text_response"] = parsed.get("text_response") if isinstance(parsed, dict) else None
    out["a2ui"] = parsed.get("a2ui") if isinstance(parsed, dict) else None
    tb = parsed.get("task_binding") if isinstance(parsed, dict) else None
    if tb is None and isinstance(parsed, dict) and "variables" in parsed:
        # bare binding dict (model omitted the wrapper)
        tb = parsed
    out["task_binding"] = tb
    out["ok"] = tb is not None
    if tb is None:
        out["error"] = "no_task_binding_in_output"
    return out


# NOTE: building the observed-entity-id set (for validation) is the orchestrator's
# job — it calls ``replay_engine.parse_dom_entities``. This module deliberately
# does NOT import ``replay_engine`` (which imports the verifier-only fixtures) so
# the no-leak boundary is statically enforceable: the compiler path never
# transitively touches ground-truth.
