"""baseline — the generic deterministic fallback surface (A4 · workplan
§7-P3: "Fallback 不能是 task-specific 模板。允许的 fallback 是一个完全
通用的『变量列表 + 状态文本』Basic Catalog surface").

The baseline is a PURE function ``TaskSurfaceContext → components``:

- generic: works for ANY goal / variable set / mutability mix — there is
  no task template, no app key, no ``if semantic_key ==`` branch;
- structure-only, zero facts: every dynamic value (goal, status, labels,
  observed/desired planes) is a ``{"path": ...}`` binding against the
  deterministic data model — the same invariant the decoder model must
  obey, so ordinary value changes stay at 0 GenUI calls here too;
- editable variables get a type-matched input bound to
  ``/variables/<key>/desired`` plus a ``taskvm.local_patch`` button;
  readonly variables render label + observed text only;
- honest under pressure: affordances degrade tier-by-tier (full →
  input-only → label-only) as the 80-component policy limit tightens,
  but EVERY variable always keeps at least its label — silent drops are
  a form of dishonest UI and never happen here.

The output passes the SAME two-layer gate (validator.validate_components)
the model output must pass — the fallback is never a validation bypass.
"""
from __future__ import annotations

import re
from typing import Any

from taskvm.genui.context import TaskSurfaceContext
from taskvm.genui.policy import MAX_COMPONENTS
from taskvm.genui.protocol import (
    ACTION_LOCAL_PATCH, ROOT_COMPONENT_ID,
)

#: Static chrome text (UI wording, not a task fact) for the apply button.
_APPLY_LABEL = "更新"

_ID_CLEAN_RE = re.compile(r"[^a-z0-9-]+")

#: Fixed scaffolding: root + goal + status + divider.
_FIXED_COMPONENTS = 4

_TIER_FULL = "full"        # editable: input+observed+apply+label+row (5)
_TIER_INPUT = "input"      # editable: input+row (2); readonly: 3 as above
_TIER_LABEL = "label"      # every var: one label Text on the root (1)


def baseline_components(context: TaskSurfaceContext) -> list[dict[str, Any]]:
    """TaskSurfaceContext → a generic variable-list component tree.

    Pure and deterministic: equal contexts produce deeply-equal trees;
    every call returns freshly-built dicts (callers may mutate freely).
    """
    variables = list(context.variables)
    editable_n = sum(1 for v in variables if v.editable)
    readonly_n = len(variables) - editable_n

    # richest tier that fits the policy budget (else honest overgeneration
    # — validate_components will reject rather than us silently dropping
    # variables; >76 variables cannot occur in the current task domain)
    if _FIXED_COMPONENTS + 5 * editable_n + 3 * readonly_n <= MAX_COMPONENTS:
        tier = _TIER_FULL
    elif _FIXED_COMPONENTS + 2 * editable_n + 3 * readonly_n <= MAX_COMPONENTS:
        tier = _TIER_INPUT
    else:
        tier = _TIER_LABEL

    used_ids: set[str] = set()

    def _uid(key: str) -> str:
        base = _ID_CLEAN_RE.sub("-", key.strip().lower()).strip("-") or "var"
        candidate = f"var-{base}"
        n = 2
        while candidate in used_ids:
            candidate = f"var-{base}-{n}"
            n += 1
        used_ids.add(candidate)
        return candidate

    def _bind(path: str) -> dict[str, str]:
        return {"path": path}

    components: list[dict[str, Any]] = []
    children: list[str] = ["goal", "status", "baseline-divider"]
    components.append({"id": "goal", "component": "Text",
                       "text": _bind("/task/goal"), "variant": "h3"})
    components.append({"id": "status", "component": "Text",
                       "text": _bind("/task/status"), "variant": "caption"})
    components.append({"id": "baseline-divider", "component": "Divider",
                       "axis": "horizontal"})

    for v in variables:
        vid = _uid(v.semantic_key)
        label_path = f"/variables/{v.semantic_key}/label"
        observed_path = f"/variables/{v.semantic_key}/observed"
        desired_path = f"/variables/{v.semantic_key}/desired"

        if tier == _TIER_LABEL:
            components.append({"id": f"{vid}-label", "component": "Text",
                               "text": _bind(label_path)})
            children.append(f"{vid}-label")
            continue

        if v.editable:
            input_id = f"{vid}-input"
            components.append(_input_component(
                input_id, v.value_type, label_path, desired_path))
            row_children = [input_id]
            if tier == _TIER_FULL:
                observed_id = f"{vid}-observed"
                apply_id = f"{vid}-apply"
                apply_label_id = f"{vid}-apply-label"
                components.append({"id": observed_id, "component": "Text",
                                   "text": _bind(observed_path),
                                   "variant": "caption"})
                components.append({"id": apply_id, "component": "Button",
                                   "child": apply_label_id,
                                   "variant": "primary",
                                   "action": {"event": {
                                       "name": ACTION_LOCAL_PATCH,
                                       "context": {
                                           "semanticKey": v.semantic_key}}}})
                # the label Text is BOTH the Button's child and a tree node
                # (the validator requires every component reachable from
                # root via children edges)
                components.append({"id": apply_label_id, "component": "Text",
                                   "text": _APPLY_LABEL})
                row_children += [observed_id, apply_id, apply_label_id]
        else:
            label_id = f"{vid}-label"
            observed_id = f"{vid}-observed"
            components.append({"id": label_id, "component": "Text",
                               "text": _bind(label_path)})
            components.append({"id": observed_id, "component": "Text",
                               "text": _bind(observed_path),
                               "variant": "caption"})
            row_children = [label_id, observed_id]

        row_id = f"{vid}-row"
        components.append({"id": row_id, "component": "Row",
                           "children": row_children, "align": "center"})
        children.append(row_id)

    root = {"id": ROOT_COMPONENT_ID, "component": "Column",
            "children": children}
    return [root] + components


def _input_component(cid: str, value_type: Any, label_path: str,
                     desired_path: str) -> dict[str, Any]:
    """Type-matched write affordance for one editable variable.

    boolean → CheckBox · date → DateTimeInput · number/integer →
    TextField(variant=number) · anything else → TextField(shortText).
    Every dynamic value binds through the data model — never a literal.
    """
    vt = str(value_type or "string").strip().lower()
    if vt == "boolean":
        return {"id": cid, "component": "CheckBox",
                "label": {"path": label_path},
                "value": {"path": desired_path}}
    if vt == "date":
        return {"id": cid, "component": "DateTimeInput",
                "label": {"path": label_path},
                "value": {"path": desired_path},
                "enableDate": True}
    if vt in ("number", "integer"):
        return {"id": cid, "component": "TextField",
                "label": {"path": label_path},
                "value": {"path": desired_path},
                "variant": "number"}
    return {"id": cid, "component": "TextField",
            "label": {"path": label_path},
            "value": {"path": desired_path},
            "variant": "shortText"}
