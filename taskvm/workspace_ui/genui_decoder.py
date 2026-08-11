"""genui_decoder — the GenUI model role (E10 rework, P4).

This is the SECOND model role in TaskVM (separate from the compiler — handoff
§7.5 / §12.4: two independent calls, no shared context). Given a VM-state
(``TaskBinding`` + current values), it calls a frontier model with the A2UI v0.9
spec + a two-zone governance directive, and emits an A2UI v0.9 message stream
(``createSurface`` / ``updateComponents`` / ``updateDataModel``) that a THIN
renderer translates to HTML.

**Why this exists** (``.mrules`` E10): until P4, ``workspace_ui/renderer.py`` was
pure f-string concatenation — the "GenUI decoder" was a hardcoded template, not a
real model call. This module makes the decoder real: the model decides the
component tree (Card vs List, TextField vs ChoicePicker, layout); the renderer
only maps A2UI component types to HTML (no "field X uses control Y" logic).

**Two-zone semantics preserved** (handoff §5.1): the model's output must mark
each component ``editable: true/false`` — read-only zone (projected app state) =
``editable: false``; read-write zone (governance controls) = ``editable: true``.
The renderer applies the zone styling based on this flag, NOT on hardcoded field
knowledge.

**Acceptance** (handoff §5.2): same VM-state → different calls may yield
different component trees (generative), but the functional semantics (which
fields are editable, what the read-only zone shows) must be consistent + correct.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from taskvm.benchmark import model_client
from taskvm.benchmark.a2ui_spec import genui_decoder_system_prompt
from taskvm.benchmark.cost_model import CostModel
from taskvm.task_state.entity_binding import TaskBinding

logger = logging.getLogger(__name__)

MODEL_ROLE = "genui_decoder"   # separate from compiler's 'compiler' + executor's 'compute_use'


def _parse_jsonl(text: str) -> list[dict]:
    """Parse a JSONL stream (one JSON object per line) into a list of dicts.
    Tolerates blank lines, ```json fences, and trailing prose. Used because the
    GenUI decoder emits A2UI v0.9 messages as JSONL (createSurface /
    updateComponents / updateDataModel on separate lines), but
    ``model_client.complete_json`` only returns the FIRST balanced object."""
    import re
    msgs: list[dict] = []
    # strip code fences some proxies wrap around the stream
    cleaned = re.sub(r"```(?:jsonl|json)?\s*", "", text or "").strip()
    # try line-by-line first (true JSONL)
    for line in cleaned.splitlines():
        line = line.strip().rstrip(",").strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                msgs.append(obj)
        except Exception:
            # the line might be a multi-line object; fall through to balanced scan
            pass
    if msgs:
        return msgs
    # fallback: balanced-scan for each top-level {...} object in the text
    depth = 0
    in_str = False
    esc = False
    start = -1
    for i, ch in enumerate(cleaned):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(cleaned[start:i + 1])
                    if isinstance(obj, dict):
                        msgs.append(obj)
                except Exception:
                    pass
                start = -1
    return msgs


def _build_user_prompt(binding: TaskBinding, values: dict[str, Any]) -> str:
    """Serialize the VM-state (TaskBinding + current values) as the user message.
    The model reads this + emits the A2UI message stream."""
    lines = ["# Task state to render as a two-zone A2UI v0.9 surface",
             f"task_id: {binding.task_id}", ""]
    lines.append("# Variables (with their app bindings + current values)")
    for v in binding.variables:
        vid = v.get("var_id")
        label = v.get("label", vid)
        editable = v.get("editable", True)
        val = values.get(vid, v.get("value"))
        lines.append(f"\n## Variable: {label} [{vid}] (editable={editable}, current_value={val!r})")
        lines.append("Bindings:")
        for b in v.get("bindings") or []:
            lines.append(f"  - {b.get('app','?')}.{b.get('entity_id','?')}.{b.get('field','?')} "
                         f"(operator: {b.get('operator','?')})")
    if binding.dependencies:
        lines.append("\n# Dependencies (effect propagation)")
        for d in binding.dependencies:
            de = d.to_dict() if hasattr(d, "to_dict") else d
            te = de.get("to_entity", {})
            lines.append(f"  - {de.get('from_var')} → {te.get('app')}.{te.get('entity_id')} "
                         f"({de.get('relation','tracks')})")
    lines.append("\n# Available apps (for the read-only zone, show each app's projected state)")
    apps = sorted({b.get("app") for v in binding.variables for b in (v.get("bindings") or [])})
    for a in apps:
        lines.append(f"  - {a}")
    lines.append("\nEmit the A2UI v0.9 message stream now (JSONL, one message per line).")
    return "\n".join(lines)


def decode_genui(binding: TaskBinding, values: dict[str, Any], *,
                 model: str | None = None, cost_model: CostModel | None = None,
                 max_tokens: int = 3072) -> dict:
    """Call the GenUI decoder model. Returns a dict with:
      ``ok``: bool, ``messages``: list[dict] (parsed A2UI messages), ``raw``: str,
      ``error``: str | None, ``validation``: dict (schema-validation result).

    The caller (renderer) passes ``messages`` to ``render_a2ui_to_html``.

    Task6 (E10 rework): uses ``a2ui_schema_manager.generate_system_prompt`` (the
    FORMAL JSON Schema + catalog injected, per the official A2UI agent SDK
    pattern) instead of the hand-transcribed ``A2UI_V09_SPEC`` prose. After
    parsing, validates the messages against the formal schema + does ONE repair
    retry if validation fails (mirrors ``model_client.complete_json``'s
    repair_retries)."""
    from taskvm.benchmark.a2ui_schema_manager import (generate_system_prompt,
                                                       validate_a2ui_messages,
                                                       repair_prompt)
    from taskvm.benchmark.a2ui_spec import genui_decoder_directive_only
    # Task6: formal schema + catalog + directive (replaces hand-transcribed spec)
    sys_prompt = generate_system_prompt(directive=genui_decoder_directive_only())
    user_prompt = _build_user_prompt(binding, values)
    parsed, raw, resp = model_client.complete_json(
        sys_prompt, user_prompt, max_tokens=max_tokens,
        temperature=None, model=model, repair_retries=2)
    if resp is not None and cost_model is not None:
        model_client.record_usage(resp, cost_model, tool="genui_decoder",
                                  role=MODEL_ROLE,
                                  model=model or model_client.TASKVM_DEFAULT_MODEL)
    # The model emits a JSONL stream (one A2UI message per line: createSurface,
    # updateComponents, updateDataModel). ``complete_json`` only returns the
    # FIRST balanced object, so parse the raw text as JSONL to get ALL messages.
    messages: list[dict] = _parse_jsonl(raw)
    if not messages:
        # fallback: maybe it was a single object or an array
        if isinstance(parsed, list):
            messages = [m for m in parsed if isinstance(m, dict)]
        elif isinstance(parsed, dict):
            if "messages" in parsed and isinstance(parsed["messages"], list):
                messages = [m for m in parsed["messages"] if isinstance(m, dict)]
            else:
                messages = [parsed]
    # Task6 (c): runtime schema validation + ONE repair retry on failure
    valid, errors = validate_a2ui_messages(messages)
    validation = {"valid": valid, "errors": errors, "repaired": False}
    if not valid and messages:
        logger.warning(f"[genui_decoder] schema validation failed ({len(errors)} "
                       f"errors); attempting one repair retry")
        repair_user = repair_prompt(messages, errors) + "\n\n" + user_prompt
        parsed2, raw2, resp2 = model_client.complete_json(
            sys_prompt, repair_user, max_tokens=max_tokens,
            temperature=None, model=model, repair_retries=1)
        if resp2 is not None and cost_model is not None:
            model_client.record_usage(resp2, cost_model, tool="genui_decoder_repair",
                                      role=MODEL_ROLE,
                                      model=model or model_client.TASKVM_DEFAULT_MODEL)
        msgs2 = _parse_jsonl(raw2)
        if msgs2:
            valid2, errs2 = validate_a2ui_messages(msgs2)
            if valid2:
                messages = msgs2
                raw = raw2
                validation = {"valid": True, "errors": [], "repaired": True,
                              "prior_errors": errors}
                logger.info("[genui_decoder] repair retry succeeded")
            else:
                validation = {"valid": False, "errors": errs2, "repaired": False,
                              "prior_errors": errors}
    out = {"ok": bool(messages), "messages": messages, "raw": raw,
           "error": None if messages else "no_a2ui_messages_parsed",
           "model": model or model_client.TASKVM_DEFAULT_MODEL,
           "validation": validation}
    if not messages:
        logger.warning(f"[genui_decoder] no messages parsed; raw[:200]={raw[:200]!r}")
    return out


# ── thin A2UI → HTML renderer ────────────────────────────────────────────────
# The renderer maps A2UI v0.9 component types to HTML. It does NOT decide which
# control to use for which field — that's the model's job. The renderer only:
#   - walks the component tree (flat list with id-references)
#   - emits HTML for each component type (Card/Text/TextField/ChoicePicker/Button/...)
#   - applies the editable flag → read-only vs read-write styling
# This is the "薄渲染层" (handoff §5.1) — no hardcoded field→control logic.

_CSS = """\
<style>
.genui-surface { font-family: -apple-system, "PingFang SC", sans-serif; max-width: 1100px;
                 margin: 18px auto; background: #fff; border-radius: 10px; padding: 18px 22px;
                 box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.genui-zone-ro { background: #f5f5f7; border-radius: 8px; padding: 12px; margin-bottom: 14px; }
.genui-zone-rw { background: #e8f0fe; border-radius: 8px; padding: 12px; }
.genui-card { background: #fff; border: 1px solid #e5e5e7; border-radius: 8px; padding: 10px 12px;
              margin-bottom: 8px; }
.genui-card h3 { margin: 0 0 6px; font-size: 15px; }
.genui-row { display: flex; gap: 12px; padding: 3px 0; font-size: 13px; }
.genui-row .k { color: #6e6e73; min-width: 90px; }
.genui-text { font-size: 14px; padding: 4px 0; }
.genui-field { margin-bottom: 10px; }
.genui-field label { display: block; font-size: 12px; color: #6e6e73; margin-bottom: 2px; }
.genui-field input, .genui-field select { padding: 4px 6px; border: 1px solid #d2d2d7;
                                          border-radius: 6px; font-size: 14px; min-width: 200px; }
.genui-readonly { font-size: 14px; color: #6e6e73; padding: 4px 0; }
.genui-button { display: inline-block; padding: 6px 14px; border-radius: 6px; background: #1a73e8;
                color: #fff; border: 0; cursor: pointer; margin-right: 8px; font-size: 14px; }
.genui-button.secondary { background: #e8f0fe; color: #1a73e8; }
.genui-pill { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px;
              background: #e8f0fe; color: #1a73e8; }
.genui-divider { border-top: 1px solid #e5e5e7; margin: 8px 0; }
.genui-col { display: flex; flex-direction: column; gap: 4px; }
</style>
"""


def _components_by_id(messages: list[dict]) -> dict[str, dict]:
    """Flatten the updateComponents messages into {component_id: component}."""
    comps: dict[str, dict] = {}
    for m in messages:
        uc = m.get("updateComponents")
        if not uc:
            continue
        for c in uc.get("components") or []:
            cid = c.get("id")
            if cid:
                comps[cid] = c
    return comps


def _data_model(messages: list[dict]) -> dict:
    """Extract the data model (var_id → value) from updateDataModel messages."""
    dm: dict = {}
    for m in messages:
        udm = m.get("updateDataModel")
        if not udm:
            continue
        val = udm.get("value")
        if isinstance(val, dict):
            dm.update(val)
    return dm


def _render_component(cid: str, comps: dict[str, dict], data: dict,
                      zone: str) -> str:
    """Recursively render one component to HTML. ``zone`` = 'ro' | 'rw' | ''."""
    c = comps.get(cid)
    if c is None:
        return f'<div class="genui-text">(?missing {cid})</div>'
    ctype = c.get("component", "Text")
    children = c.get("children") or []
    # detect zone by id convention (the model names them ro_zone / rw_zone)
    if cid == "ro_zone" or "ro_zone" in cid:
        zone = "ro"
    elif cid == "rw_zone" or "rw_zone" in cid:
        zone = "rw"
    editable = c.get("editable", c.get("dataBinding") is not None)
    if ctype == "Column":
        inner = "".join(_render_component(ch, comps, data, zone) for ch in children)
        return f'<div class="genui-col">{inner}</div>'
    if ctype == "Row":
        inner = "".join(_render_component(ch, comps, data, zone) for ch in children)
        return f'<div class="genui-row">{inner}</div>'
    if ctype == "Card":
        title = c.get("title") or cid
        inner = "".join(_render_component(ch, comps, data, zone) for ch in children)
        return f'<div class="genui-card"><h3>{_esc(title)}</h3>{inner}</div>'
    if ctype == "Text":
        text = c.get("text") or c.get("content") or ""
        # data binding: if text is a {path}, resolve from data
        if isinstance(text, dict) and "path" in text:
            text = data.get(str(text["path"]).lstrip("/"), "")
        return f'<div class="genui-text">{_esc(text)}</div>'
    if ctype == "TextField":
        label = c.get("label") or c.get("placeholder") or ""
        binding = c.get("dataBinding") or c.get("varId") or c.get("path")
        val = data.get(binding, "") if isinstance(binding, str) else ""
        if zone == "ro" or editable is False:
            return (f'<div class="genui-field"><label>{_esc(label)}</label>'
                    f'<div class="genui-readonly">{_esc(val)}</div></div>')
        return (f'<div class="genui-field"><label>{_esc(label)}</label>'
                f'<input type="text" value="{_esc(val)}" data-var="{_esc(binding)}"></div>')
    if ctype == "DateTimeInput":
        label = c.get("label") or ""
        binding = c.get("dataBinding") or c.get("path")
        val = data.get(binding, "") if isinstance(binding, str) else ""
        if zone == "ro" or editable is False:
            return (f'<div class="genui-field"><label>{_esc(label)}</label>'
                    f'<div class="genui-readonly">{_esc(val)}</div></div>')
        return (f'<div class="genui-field"><label>{_esc(label)}</label>'
                f'<input type="date" value="{_esc(val)}" data-var="{_esc(binding)}"></div>')
    if ctype == "ChoicePicker":
        label = c.get("label") or ""
        binding = c.get("dataBinding") or c.get("path")
        val = data.get(binding, "") if isinstance(binding, str) else ""
        opts = c.get("options") or c.get("choices") or []
        if zone == "ro" or editable is False:
            return (f'<div class="genui-field"><label>{_esc(label)}</label>'
                    f'<div class="genui-readonly">{_esc(val)}</div></div>')
        opt_html = "".join(f'<option value="{_esc(o)}" '
                           f'{"selected" if str(o)==str(val) else ""}>{_esc(o)}</option>'
                           for o in opts)
        return (f'<div class="genui-field"><label>{_esc(label)}</label>'
                f'<select data-var="{_esc(binding)}">{opt_html}</select></div>')
    if ctype == "Button":
        label = c.get("label") or c.get("text") or "button"
        cls = "secondary" if "undo" in str(label).lower() or "checkpoint" in str(label).lower() else ""
        return f'<button class="genui-button {cls}">{_esc(label)}</button>'
    if ctype == "Divider":
        return '<hr class="genui-divider">'
    if ctype == "List":
        items = c.get("items") or children
        inner = "".join(_render_component(ch if isinstance(ch, str) else ch.get("id",""),
                                          comps, data, zone)
                        for ch in items)
        return f'<div class="genui-col">{inner}</div>'
    # fallback: render as text with the component type (honest — no silent skip)
    return f'<div class="genui-text">({ctype}: {cid})</div>'


def _esc(s: Any) -> str:
    return (str(s) if s is not None else "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def render_a2ui_to_html(messages: list[dict]) -> str:
    """Translate A2UI v0.9 messages to a full HTML page (the THIN renderer).
    Walks the component tree from 'root', maps each component type to HTML.
    No hardcoded field→control logic — the model decided the tree; this only
    renders it."""
    comps = _components_by_id(messages)
    data = _data_model(messages)
    root = comps.get("root")
    if root is None:
        return "<!DOCTYPE html><html><body><p>(no root component)</p></body></html>"
    body = _render_component("root", comps, data, "")
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
            "<title>TaskVM · GenUI decoder surface</title>" + _CSS +
            "</head><body><div class='genui-surface'>" + body +
            "</div></body></html>")
