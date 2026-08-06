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

from taskvm.benchmark.a2ui_spec import compiler_system_prompt
from taskvm.benchmark import model_client
from taskvm.benchmark.cost_model import CostModel
from taskvm.harness.observations import TraceFixture
from taskvm.task_state.entity_binding import build_tool_schema

logger = logging.getLogger(__name__)

MODEL_ROLE = "compiler"   # role 1 (UI-gen); role 2 = "compute_use" (W2+)


def build_user_prompt(trace: TraceFixture, observed_entity_ids: dict[str, set[str]]) -> str:
    """The user-message body: task goal + per-app observations + tool schema +
    the entity-id whitelist (so the model knows which ids are valid)."""
    lines = [f"# Task", f"task_id: {trace.task_id}", f"goal: {trace.goal}", ""]
    lines.append("# Observed application states (read-only — compile from these)")
    for app, obs in trace.final_obs.items():
        lines.append(f"\n## App: {app}")
        lines.append("### Accessibility tree (primary text input):")
        lines.append(obs.a11y_text)
        lines.append("### DOM (rendered, with data-event-id/data-task-id + data-field):")
        # truncate very long DOMs to keep the call bounded (a11y is the primary input)
        dom = obs.dom_html
        if len(dom) > 8000:
            dom = dom[:8000] + "\n<!-- ...truncated... -->"
        lines.append(dom)
    lines.append("")
    lines.append(build_tool_schema(list(trace.final_obs.keys())))
    lines.append("")
    lines.append("# Valid entity ids (use ONLY these as entity_id):")
    for app, ids in observed_entity_ids.items():
        lines.append(f"- {app}: {sorted(ids)}")
    lines.append("")
    lines.append("Compile the task_binding now. Output ONLY the JSON object: "
                 '{"text_response": "...", "a2ui": [...], "task_binding": {...}}')
    return "\n".join(lines)


def compile_binding(trace: TraceFixture, observed_entity_ids: dict[str, set[str]],
                    *, model: str | None = None, temperature: float | None = None,
                    max_tokens: int = 3072, cost_model: CostModel | None = None,
                    binding_only: bool = True) -> dict:
    """Call the frontier model and return:
        {"text_response", "a2ui", "task_binding", "raw", "parsed", "ok", "error"}
    ``task_binding`` is the gate-critical output (None on parse/structure failure).
    ``ok`` is True iff a binding was parsed (structural validation is the caller's
    job via ``render_check.validate_binding``).

    ``binding_only=True`` (W1 default) directs the model to emit task_binding
    FIRST and treat a2ui as optional/minimal — the documented W1 fallback when
    the model over-spends tokens on the A2UI surface and starves the binding
    (doc §10 de-prioritizes fancy UI). Pass False for the full-A2UI check.
    """
    sys_prompt = compiler_system_prompt(binding_only=binding_only)
    user_prompt = build_user_prompt(trace, observed_entity_ids)

    parsed, raw, resp = model_client.complete_json(
        sys_prompt, user_prompt, max_tokens=max_tokens,
        temperature=temperature, model=model)

    # record cost (real token usage) if a cost model is attached
    if cost_model is not None and resp is not None:
        model_client.record_usage(
            resp, cost_model, tool="compile_binding", role=MODEL_ROLE,
            model=model or model_client.TASKVM_DEFAULT_MODEL)

    out: dict[str, Any] = {"raw": raw, "parsed": parsed, "ok": False,
                           "text_response": None, "a2ui": None, "task_binding": None,
                           "error": None}
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
