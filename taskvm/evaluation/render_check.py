"""render_check — validate the compiler's emitted task_binding (W1: JSON parse +
binding-schema + entity_id existence + operator-in-registry).

Pure-Python structural validation (no Flask, no test_client). Full L1
5-sub-dimension A2UI render-check is W3; W1 validates the GATE-CRITICAL output
(the task_binding) — that it's well-formed JSON, every binding's ``entity_id``
exists in the observed DOM, and every ``operator`` is in the OPERATOR_REGISTRY.
The A2UI surface is checked only for JSON validity (it's not gate-critical — W1
de-prioritizes fancy UI, doc §10).

This is the deterministic downstream mitigation for the #1 reliability risk
(compiler emits wrong/non-existent entity_id). It does NOT judge correctness
against GT — that's the verifier's job (no-leak). It only checks structural
validity against the observations the compiler was given.
"""
from __future__ import annotations

from typing import Any

from taskvm.task_state.entity_binding import OPERATOR_REGISTRY, operator_app


def parse_compiler_output(raw: str | None, parsed: Any) -> dict | None:
    """Return the ``task_binding`` dict from the model output, or None.

    Accepts either: (a) a dict with a top-level ``task_binding`` key (the
    contract output), or (b) a bare dict that IS the task_binding. ``parsed``
    takes precedence; ``raw`` is a fallback (re-parse not needed — model_client
    already parsed). Returns None if no binding can be found.
    """
    obj = parsed
    if obj is None:
        return None
    if isinstance(obj, dict) and "task_binding" in obj:
        obj = obj["task_binding"]
    if not isinstance(obj, dict):
        return None
    return obj


def validate_binding(binding: dict, observed_entity_ids: dict[str, set[str]] | None,
                     task_id: str | None = None) -> tuple[bool, list[str]]:
    """Structural validation of the compiler's task_binding (NO GT comparison).

    GG red-line §0: the model emits ``locator`` (a VISIBLE TITLE), NOT
    ``entity_id``. A binding must carry a ``locator`` that names a real visible
    entity OR an already-resolved ``entity_id`` (the GT/mock path). The #1
    reliability check is now "does the locator resolve to a real visible entity"
    — ``observed_entity_ids`` is repurposed to accept either a ``{app: {eid}}``
    set (legacy/GT path, entity_id checked) OR ``None`` (model path: locator
    presence is checked here; resolution happens upstream in the orchestrator
    via ``governance.translate.resolve_locator`` against the locator_index).

    Returns (is_valid, errors). errors is empty iff valid.
    """
    errors: list[str] = []
    if not isinstance(binding, dict):
        return False, ["task_binding is not a dict"]
    if task_id and binding.get("task_id") and binding["task_id"] != task_id:
        errors.append(f"task_id mismatch: binding={binding['task_id']!r} expected={task_id!r}")

    variables = binding.get("variables")
    if not isinstance(variables, list) or not variables:
        errors.append("task_binding.variables must be a non-empty list")
        return False, errors

    seen_var_ids = set()
    for vi, v in enumerate(variables):
        if not isinstance(v, dict):
            errors.append(f"variables[{vi}] is not a dict")
            continue
        vid = v.get("var_id")
        if not vid:
            errors.append(f"variables[{vi}] missing var_id")
            continue
        if vid in seen_var_ids:
            errors.append(f"variables[{vi}] duplicate var_id {vid!r}")
        seen_var_ids.add(vid)
        bnds = v.get("bindings")
        if not isinstance(bnds, list):
            errors.append(f"variables[{vi}] ({vid}): bindings must be a list")
            continue
        for bi, b in enumerate(bnds):
            if not isinstance(b, dict):
                errors.append(f"variables[{vi}].bindings[{bi}] is not a dict")
                continue
            for key in ("app", "field", "operator"):
                if not b.get(key):
                    errors.append(f"variables[{vi}].bindings[{bi}] missing {key}")
            # GG: a binding must identify its entity via locator (model path) or
            # entity_id (GT/mock path). At least one must be present.
            if not b.get("locator") and not b.get("entity_id"):
                errors.append(f"variables[{vi}].bindings[{bi}] missing both locator "
                              f"and entity_id (need a visible locator or resolved id)")
            app = b.get("app")
            eid = b.get("entity_id")
            op = b.get("operator")
            # legacy entity_id existence check (only when observed_entity_ids is
            # the old {app: {eid}} shape AND the binding carries entity_id)
            if (observed_entity_ids and eid and app
                    and app in observed_entity_ids
                    and eid not in observed_entity_ids.get(app, set())):
                errors.append(f"variables[{vi}].bindings[{bi}]: entity_id {eid!r} "
                              f"not present in observed {app} DOM")
            if op and op not in OPERATOR_REGISTRY:
                errors.append(f"variables[{vi}].bindings[{bi}]: unknown operator {op!r}; "
                              f"known: {list(OPERATOR_REGISTRY)}")
            elif op and app and operator_app(op) != app:
                errors.append(f"variables[{vi}].bindings[{bi}]: operator {op!r} targets "
                              f"app {operator_app(op)!r}, not {app!r}")

    return (len(errors) == 0), errors


def validate_a2ui_surface(obj: Any) -> tuple[bool, list[str]]:
    """W1: the A2UI surface is checked only for JSON validity + that ``a2ui``
    (if present) is a list. Full L1 5-sub-dim render-check is W3. NOT
    gate-critical — the task_binding is."""
    errors: list[str] = []
    if obj is None:
        return False, ["compiler output is None (no JSON parsed)"]
    if not isinstance(obj, dict):
        return False, ["compiler output is not a JSON object"]
    a2ui = obj.get("a2ui")
    if a2ui is not None and not isinstance(a2ui, list):
        errors.append("a2ui must be a list of messages (or omitted)")
    return (len(errors) == 0), errors
