"""Renderer — compile the task-state graph + binding into a structured-text surface.

W1: structured text (no fancy UI — handoff §10 de-prioritizes fancy UI). The
renderer walks the binding's variables and emits one block per variable
(label + current value + which apps it touches). This is what the user
"manipulates" (the scripted edit addresses a var_id directly).

Also used by the verifier's ``check_interface_resynced``: re-render from the
POST-state canonical graph and assert the edited variable now shows the new
value (structural re-sync check, no model).
"""
from __future__ import annotations

from typing import Any

from taskvm.task_state.entity_binding import TaskBinding


def render(binding: TaskBinding, *, values: dict[str, Any] | None = None) -> str:
    """Render the task surface as structured text.

    ``values``: optional override of each variable's displayed value (used by
    the verifier's re-sync check to render from the post-state canonical value
    rather than the compiler's stale pre-edit value).
    """
    values = values or {}
    lines = [f"# Task: {binding.task_id}", ""]
    for v in binding.variables:
        vid = v.get("var_id")
        label = v.get("label", vid)
        editable = v.get("editable", True)
        value = values.get(vid, v.get("value"))
        marker = "✎" if editable else "·"
        lines.append(f"{marker} {label}  [{vid}] = {value}")
        for b in v.get("bindings") or []:
            app = b.get("app", "?")
            eid = b.get("entity_id", "?")
            field = b.get("field", "?")
            op = b.get("operator", "?")
            lines.append(f"    → {app}.{eid}.{field}  (operator: {op})")
        deps = [d for d in binding.dependencies if d.from_var == vid]
        if deps:
            for d in deps:
                lines.append(f"    ↳ {d.relation}: {d.to_app}.{d.to_entity_id}")
        lines.append("")
    return "\n".join(lines)


def render_variable_value(binding: TaskBinding, var_id: str,
                          value: Any) -> str:
    """One-line render of a single variable's value (for the re-sync check)."""
    v = next((x for x in binding.variables if x.get("var_id") == var_id), None)
    label = (v or {}).get("label", var_id)
    return f"{label} [{var_id}] = {value}"


def edited_variable_shows_value(rendered: str, var_id: str, expected: Any) -> bool:
    """Structural re-sync check: does the rendered surface show ``expected`` for
    ``var_id``? Used by the verifier's ``check_interface_resynced``."""
    # match "[var_id] = <value>" capturing the FULL value (rest of line), so
    # multi-word values (e.g. a wechat message text with spaces) match too.
    # Scalar values (dates, statuses) are unaffected — full-line == single
    # token when there are no spaces.
    import re
    pat = re.compile(rf"\[{re.escape(var_id)}\]\s*=\s*(.+)$")
    m = pat.search(rendered)
    if not m:
        return False
    return str(m.group(1)).strip().lower() == str(expected).strip().lower()
