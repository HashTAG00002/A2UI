"""Baseline 2 — prompt-only (frontier model + A2UI spec, NO TaskVM binding
contract). Ablation: does the task_binding emerge from generic agentic-UI
generation alone, or does the TaskVM contract do the work? (handoff §6 item 3).

Calls the frontier model with the A2UI v0.9 spec + the same observations, but
WITHOUT the ``TASKVM_BINDING_CONTRACT`` (so the model is asked for an A2UI
surface only — no typed task_binding). We then try to extract a binding from the
A2UI ``updateDataModel`` paths heuristically (the closest a generic GenUI model
gets to a binding without being told to emit one). If it can't, ``ok=False`` —
honest: prompt-only GenUI does not produce an executable binding.

No-leak: same as the compiler — only trace + observed ids + OPERATOR_REGISTRY.
"""
from __future__ import annotations

import json
from typing import Any

from taskvm_bench.baselines.base import _ok, register
from taskvm_bench.benchmark import model_client
from taskvm_bench.benchmark.a2ui_spec import A2UI_V09_SPEC
from taskvm_bench.benchmark.cost_model import CostModel
from taskvm_bench.harness.observations import TraceFixture
from taskvm_bench.task_state.entity_binding import build_tool_schema

_BASELINE_ROLE = "prompt_only"


def _build_prompt(trace: TraceFixture, observed_entity_ids: dict[str, set[str]]) -> str:
    """A2UI-spec-only prompt (NO TaskVM binding contract). Ask for an A2UI
    surface that reflects the task; do NOT ask for a task_binding."""
    lines = [f"# Task", f"task_id: {trace.task_id}", f"goal: {trace.goal}", ""]
    lines.append("# Observed application states (read-only)")
    for app, obs in trace.final_obs.items():
        lines.append(f"\n## App: {app}")
        lines.append("### Accessibility tree:")
        lines.append(obs.a11y_text)
    lines.append("")
    lines.append(build_tool_schema(list(trace.final_obs.keys())))
    lines.append("")
    lines.append("# Valid entity ids")
    for app, ids in observed_entity_ids.items():
        lines.append(f"- {app}: {sorted(ids)}")
    lines.append("")
    lines.append("Generate an A2UI v0.9 surface (createSurface + updateComponents + "
                 "updateDataModel) that lets the user accomplish the task. "
                 "Respond with ONLY the JSON a2ui message array.")
    return "\n".join(lines)


def _flatten_update_data_model(path: str, value: Any, out: list[tuple[str, Any]]) -> None:
    """v0.9's updateDataModel.value is a plain (possibly nested) JSON object —
    no array-of-typed-pairs wrapper like v0.8's ``contents``. Recursively walk
    it into (json_pointer_path, leaf_value) pairs so the same heuristic below
    can inspect leaves regardless of nesting depth."""
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten_update_data_model(f"{path}/{k}", v, out)
    else:
        out.append((path, value))


def _extract_binding_from_a2ui(parsed: Any, trace: TraceFixture) -> dict | None:
    """Heuristic: try to read a binding out of the A2UI v0.9 updateDataModel
    paths. A leaf path like ``/calendar/E1/date`` hints at a binding; we map it
    to the matching operator in OPERATOR_REGISTRY. This is the BEST a
    prompt-only GenUI model can do without being told to emit a task_binding —
    usually it returns None (honest: no executable binding from generic
    GenUI)."""
    if not isinstance(parsed, list):
        return None
    from taskvm_bench.task_state.entity_binding import OPERATOR_REGISTRY
    ops_by_app_field = {(m["app"], m["field"]): op for op, m in OPERATOR_REGISTRY.items()}
    variables: list[dict] = []
    for msg in parsed:
        if not isinstance(msg, dict):
            continue
        udm = msg.get("updateDataModel") or {}
        base_path = udm.get("path") or ""
        leaves: list[tuple[str, Any]] = []
        _flatten_update_data_model(base_path.rstrip("/"), udm.get("value"), leaves)
        for path, leaf_value in leaves:
            parts = [p for p in str(path).split("/") if p]
            if len(parts) < 3:
                continue
            app, eid, field = parts[0], parts[1], parts[2]
            op = ops_by_app_field.get((app, field))
            if op is None:
                continue
            var_id = f"{app}_{field}"
            v = next((v for v in variables if v["var_id"] == var_id), None)
            if v is None:
                v = {"var_id": var_id, "label": var_id, "value": leaf_value,
                     "editable": True, "bindings": []}
                variables.append(v)
            v["bindings"].append({"var_id": var_id, "app": app, "entity_id": eid,
                                  "field": field, "operator": op})
    if not variables:
        return None
    return {"task_id": trace.task_id, "variables": variables, "dependencies": []}


def discover(trace: TraceFixture, observed_entity_ids: dict[str, set[str]],
             *, model: str | None = None, cost_model: CostModel | None = None,
             temperature: float | None = None, **kw) -> dict:
    """Prompt-only: A2UI spec, no TaskVM contract. Extract a binding heuristically
    from the dataModelUpdate paths (usually None → ok=False, the honest result)."""
    sys_prompt = A2UI_V09_SPEC + "\n\nRespond with ONLY valid JSON — no prose."
    user_prompt = _build_prompt(trace, observed_entity_ids)
    parsed, raw, resp = model_client.complete_json(
        sys_prompt, user_prompt, max_tokens=3072, temperature=temperature, model=model)
    if cost_model is not None and resp is not None:
        model_client.record_usage(resp, cost_model, tool="baseline_prompt_only",
                                  role=_BASELINE_ROLE,
                                  model=model or model_client.TASKVM_DEFAULT_MODEL)
    tb = _extract_binding_from_a2ui(parsed, trace)
    if tb is None:
        return _ok(raw=raw or "", tb=None,
                   error="no_binding_extractable_from_a2ui (prompt-only GenUI "
                         "does not emit an executable task_binding)")
    return _ok(raw=raw or "", tb=tb)


register("prompt_only", discover, role="ablation", uses_model=True,
         description="frontier model + A2UI spec, NO TaskVM binding contract — "
                    "ablates whether the binding emerges from generic GenUI alone")
