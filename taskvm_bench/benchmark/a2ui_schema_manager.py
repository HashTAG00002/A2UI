"""a2ui_schema_manager — TaskVM's port of the official A2UI agent SDK pattern
(Task6, E10 rework, handoff task 6).

The official A2UI agent SDK (``docs/A2UI-protocol-spec/v0_9/agent_sdk_reference/
agent_development.md``) is pure in-context prompt engineering (no model
training), but it does THREE things TaskVM's hand-transcribed ``A2UI_V09_SPEC``
system prompt does NOT:

  a) ``A2uiSchemaManager.generate_system_prompt()`` auto-injects the **formal
     JSON Schema** (TaskVM was hand-writing a prose description of the schema).
  b) auto-injects **few-shot examples** (TaskVM had 1 example hardcoded in
     prose; the SDK loads them from a catalog's examples dir).
  c) ``A2uiCatalog.validator.validate()`` does **runtime schema validation +
     auto-repair of simple errors** (TaskVM had zero A2UI-message validation).

This module ports those three to TaskVM, loading the official schemas from
``docs/A2UI-protocol-spec/v0_9/json/`` (downloaded verbatim from
github.com/a2ui-project/a2ui). The GenUI decoder (and optionally the compiler)
use ``A2uiSchemaManager.generate_system_prompt()`` instead of the hand-transcribed
``A2UI_V09_SPEC``; the decoder validates its output via ``validate_a2ui_messages``
with a repair retry (mirroring ``model_client.complete_json``'s repair pattern).

Why this is harness-level (no model change): the official SDK confirms the
route is in-context prompt engineering + runtime validation — exactly TaskVM's
approach, just with the real schema/examples instead of a prose paraphrase.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── locate the official A2UI v0.9 spec files ────────────────────────────────
# docs/A2UI-protocol-spec/v0_9/ (moved from docs/references/ — .gitignore'd there)
_SPEC_ROOT = Path(__file__).resolve().parents[2] / "docs" / "A2UI-protocol-spec" / "v0_9"
_JSON_DIR = _SPEC_ROOT / "json"
_CATALOG_DIR = _SPEC_ROOT / "catalogs_basic"


def _load_json(name: str) -> dict | None:
    p = _JSON_DIR / name
    if not p.exists():
        logger.warning(f"[a2ui_schema_manager] {p} not found (spec files missing?)")
        return None
    return json.loads(p.read_text())


def _load_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text()


# lazy-loaded singletons (load once, reuse)
_MESSAGE_SCHEMA: dict | None = None
_CATALOG: dict | None = None
_SAMPLE: dict | None = None


def get_message_schema() -> dict:
    """The formal A2UI v0.9 server→client message schema (oneOf the 4 message
    types: CreateSurface / UpdateComponents / UpdateDataModel / DeleteSurface)."""
    global _MESSAGE_SCHEMA
    if _MESSAGE_SCHEMA is None:
        _MESSAGE_SCHEMA = _load_json("server_to_client.json") or {}
    return _MESSAGE_SCHEMA


def get_catalog() -> dict:
    """The basic catalog (18 components + functions + $defs)."""
    global _CATALOG
    if _CATALOG is None:
        p = _CATALOG_DIR / "catalog.json"
        if p.exists():
            _CATALOG = json.loads(p.read_text())
        else:
            _CATALOG = {}
    return _CATALOG


def get_sample() -> dict:
    """The built-in sample (a named example with ``messages``) — used as the
    few-shot example (the catalog has no ``examples/`` subdir on disk)."""
    global _SAMPLE
    if _SAMPLE is None:
        _SAMPLE = _load_json("sample.json") or {}
    return _SAMPLE


# ── (a) + (b): system prompt with formal schema + few-shot ──────────────────

def _schema_summary(schema: dict, max_chars: int = 6000) -> str:
    """A compact text rendering of the formal schema for prompt injection. Shows
    the oneOf message types + their required fields + the component $defs, so
    the model has the REAL schema (not a prose paraphrase). Capped to max_chars
    to keep the prompt bounded."""
    lines = ["## Formal A2UI v0.9 Message Schema (from server_to_client.json)"]
    one_of = schema.get("oneOf") or []
    defs = schema.get("$defs") or {}
    for i, branch in enumerate(one_of):
        ref = (branch.get("$ref") or "").split("/")[-1]
        d = defs.get(ref, {})
        lines.append(f"\n### Message type {i+1}: {ref}")
        if d.get("description"):
            lines.append(f"  {d['description']}")
        req = d.get("required") or []
        if req:
            lines.append(f"  required: {req}")
        props = d.get("properties") or {}
        for pname, pspec in list(props.items())[:8]:
            # show const / enum / type / $ref — the version field is a const
            # "v0.9" string, so make that explicit (the model was emitting
            # {"version":{"major":0,"minor":9}} because "type: object" was
            # misleading — there's no type, it's a const string).
            if "const" in pspec:
                ptype = f'const "{pspec["const"]}"'
            elif "enum" in pspec:
                ptype = f"enum {pspec['enum']}"
            elif "type" in pspec:
                ptype = pspec["type"]
            elif "$ref" in pspec:
                ptype = pspec["$ref"].split("/")[-1]
            else:
                ptype = "object"
            lines.append(f"    {pname}: {ptype}")
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n... (schema truncated; see full JSON for details)"
    return out


def _catalog_summary(max_chars: int = 4000) -> str:
    """A compact rendering of the basic catalog's 18 components for the prompt."""
    cat = get_catalog()
    comps = cat.get("components") or {}
    lines = ["## A2UI Basic Catalog (18 components, from catalog.json)"]
    for name, spec in list(comps.items()):
        desc = (spec.get("description") or "")[:120]
        props = list((spec.get("properties") or {}).keys())[:6]
        lines.append(f"- {name}: {desc}" + (f" [props: {props}]" if props else ""))
    out = "\n".join(lines)
    # CRITICAL: show the v0.9 FLAT-DISCRIMINATOR component shape with a concrete
    # example, because the model tends to emit the v1.0 keyed-wrapper form
    # ({"component":{"Column":{...}}}) which fails validation. v0.9 is:
    #   {"id":"...","component":"Column","children":[...], ...props}
    # (component is the VALUE of the "component" property, NOT a keyed wrapper).
    out += (
        "\n\n## CRITICAL — v0.9 component shape (flat discriminator, NOT keyed)\n"
        'Each component is {"id":"...","component":"<TypeName>",...props} — the\n'
        'component type is the STRING VALUE of the "component" property.\n'
        'Example: {"id":"root","component":"Column","children":["title","body"]}\n'
        '         {"id":"title","component":"Text","text":"hello"}\n'
        'DO NOT use the v1.0 keyed form {"component":{"Column":{"children":...}}}.\n'
        'DO NOT use {"children":{"explicitList":[...]}} — children is a plain array.\n'
        'DO NOT use {"text":{"literalString":"..."}} — text is a plain string.\n'
        'dataBinding is a plain string var_id (e.g. "dataBinding":"my_var").')
    return out[:max_chars] + "\n... (catalog truncated)" if len(out) > max_chars else out


def _few_shot_block() -> str:
    """The few-shot example (from sample.json) — a concrete A2UI message stream
    the model can imitate. The official SDK loads these from a catalog's
    examples dir; TaskVM uses the built-in sample (the catalog has no examples/
    subdir on disk)."""
    sample = get_sample()
    if not sample:
        return ""
    # sample.json is itself a schema describing a sample shape; the actual
    # example messages would be in a separate file. Since the catalog has no
    # examples/ dir, fall back to the GENUI_DECODER_DIRECTIVE's built-in
    # example (already in a2ui_spec.py). Return a note that no dynamic example
    # was found — honest, not faking one.
    return ("## Few-shot example\n(no dynamic examples/ dir on disk in the basic "
            "catalog; the built-in example in the directive below serves as the "
            "few-shot. Fetching catalogs/basic/examples/ from the official repo "
            "is a follow-on.)")


def generate_system_prompt(directive: str = "") -> str:
    """Build the GenUI decoder system prompt with the FORMAL schema + catalog +
    few-shot injected (replaces the hand-transcribed A2UI_V09_SPEC prose).

    ``directive``: the TaskVM-specific GenUI decoder directive (two-zone
    governance, output shape) — appended after the formal schema/catalog."""
    schema = get_message_schema()
    parts = []
    if schema:
        parts.append(_schema_summary(schema))
    cat = get_catalog()
    if cat:
        parts.append(_catalog_summary())
    fs = _few_shot_block()
    if fs:
        parts.append(fs)
    if directive:
        parts.append(directive)
    return "\n\n".join(parts) if parts else directive


# ── (c): runtime schema validation + repair ────────────────────────────────

def _build_validator():
    """Build a jsonschema validator with a resolver that resolves external
    ``$ref``s across the A2UI v0.9 spec files (server_to_client.json refs
    common_types.json + catalog.json). jsonschema can't auto-resolve cross-file
    refs without a registry/referencing lib; we merge the $defs into one schema
    + use a referencing.Registry as the fallback resolver."""
    import jsonschema
    schema = get_message_schema()
    if not schema:
        return None
    # load the companion schemas the message schema references
    common = _load_json("common_types.json") or {}
    catalog = get_catalog() or {}
    # build a registry mapping the official $id URIs to their docs, so $refs
    # like "catalog.json#/$defs/anyComponent" + "common_types.json#/$defs/..." resolve.
    try:
        import referencing
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
        base = "https://a2ui.org/specification/v0_9/"
        resources = {}
        for name, doc in [("server_to_client.json", schema),
                          ("common_types.json", common),
                          ("catalog.json", catalog),
                          ("client_to_server.json", _load_json("client_to_server.json") or {}),
                          ("client_data_model.json", _load_json("client_data_model.json") or {}),
                          ("server_capabilities.json", _load_json("server_capabilities.json") or {}),
                          ("client_capabilities.json", _load_json("client_capabilities.json") or {})]:
            # explicit specification (some A2UI schemas lack $schema, so
            # Resource.from_contents can't auto-detect — force Draft 2020-12).
            resources[base + name] = Resource.from_contents(doc, default_specification=DRAFT202012)
        registry = Registry().with_resources(resources.items())
        return jsonschema.Draft202012Validator(schema, registry=registry)
    except ImportError:
        # no `referencing` lib — fall back to a merged-schema approach (slower,
        # may miss some refs, but catches the main oneOf structure)
        merged = dict(schema)
        merged_defs = dict(merged.get("$defs") or {})
        for src in (common, catalog):
            for k, v in (src.get("$defs") or {}).items():
                merged_defs.setdefault(k, v)
        merged["$defs"] = merged_defs
        return jsonschema.Draft202012Validator(merged)


def validate_a2ui_messages(messages: list[dict]) -> tuple[bool, list[str]]:
    """Validate a list of A2UI messages against the formal server_to_client
    schema. Returns (is_valid, errors). Each message must match one of the
    oneOf branches (CreateSurface / UpdateComponents / UpdateDataModel /
    DeleteSurface).

    Uses ``jsonschema`` (4.26.0) + a ``referencing.Registry`` to resolve the
    cross-file $refs (server_to_client → common_types + catalog). A message is
    valid iff it validates against the schema's oneOf (at least one branch
    matches). Errors are collected per-message for the repair prompt."""
    try:
        import jsonschema
    except ImportError:
        return True, ["jsonschema not installed — validation skipped (env issue)"]
    validator = _build_validator()
    if validator is None:
        return True, ["schema not loaded — validation skipped (spec files missing)"]
    errors: list[str] = []
    for i, msg in enumerate(messages):
        try:
            validator.validate(msg)
        except jsonschema.ValidationError as e:
            path = "/".join(str(p) for p in e.absolute_path) or "(root)"
            errors.append(f"message[{i}] ({path}): {e.message}")
    return (len(errors) == 0), errors


def repair_prompt(messages: list[dict], errors: list[str]) -> str:
    """Build a repair prompt: show the model which messages failed validation
    + why, ask it to fix + re-emit the full message stream. Mirrors
    ``model_client.complete_json``'s repair_retries pattern.

    Includes the common failure (version field) explicitly because the model
    sometimes emits the v1.0-style ``{"version":{"major":0,"minor":9}}`` object
    instead of the v0.9-required ``"version":"v0.9"`` string constant."""
    return (
        "Your previous A2UI v0.9 output FAILED runtime schema validation:\n"
        + "\n".join(f"  - {e}" for e in errors)
        + "\n\nCommon fixes:\n"
        '  - "version" MUST be the STRING "v0.9" (NOT an object like '
        '{"major":0,"minor":9} — that is v1.0 syntax, this is v0.9).\n'
        "  - Each message must have EXACTLY ONE of: createSurface / "
        "updateComponents / updateDataModel / deleteSurface (plus version).\n"
        "  - updateComponents.components is a FLAT list; parent-child via id refs.\n"
        "  - Exactly ONE component with id 'root' per surface.\n\n"
        "Re-emit the FULL corrected A2UI v0.9 message stream (JSONL, one message "
        "per line). Output ONLY the JSONL, no prose, no fences.")
