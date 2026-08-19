"""schema — vendored-mirror → official A2UI Agent SDK catalog/validator.

Production port of the bench-only ``taskvm_bench/benchmark/
a2ui_schema_manager.py`` (workplan §7-P1): the official ``a2ui-agent-sdk``
provides the catalog object, the runtime validator and the prompt-facing
schema summaries — we load the FROZEN vendored mirror
(``docs/A2UI-protocol-spec/v0_9``) into it instead of re-implementing
validation against hand-rolled schemas.

⚠ v0.9 binding syntax (verified three ways: official ``a2ui_protocol.md``
example, ``evolution_guide.md`` v0.8→v0.9 migration table, and the SDK
validator rejecting the legacy form): a dynamic property binds via the
``DataBinding`` branch of its ``Dynamic*`` type —

    {"id": "f", "component": "TextField", "label": "Date",
     "value": {"path": "/variables/release_date/desired"}}

There is NO ``dataBinding`` property in v0.9 (that is v0.8 syntax; the
SDK validator rejects it as an unevaluated property).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from taskvm.genui.protocol import CATALOG_ID, PROTOCOL_VERSION

#: Vendored, pinned mirror root (SOURCE.txt carries upstream commit +
#: per-file SHA-256; tests/protocol locks integrity).
SPEC_ROOT = (Path(__file__).resolve().parents[2]
             / "docs" / "A2UI-protocol-spec" / "v0_9")

_catalog_lock = threading.Lock()
_catalog: Any = None          # a2ui.schema.catalog.A2uiCatalog (lazy singleton)


class SchemaAssetError(RuntimeError):
    """The vendored mirror or the a2ui-agent-sdk is unusable — honest hard
    failure, never a silent validation bypass."""


def _load(rel: str) -> dict[str, Any]:
    """``rel`` is relative to SPEC_ROOT (e.g. "json/server_to_client.json"
    or "catalogs_basic/catalog.json")."""
    path = SPEC_ROOT / rel
    if not path.exists():
        raise SchemaAssetError(f"vendored A2UI spec file missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def get_catalog():
    """The (cached) official-SDK catalog built from the vendored mirror.

    Uses ``a2ui.schema.catalog.A2uiCatalog`` — the SDK resolves the
    cross-file ``$ref`` web (server_to_client → common_types + catalog)
    and exposes ``catalog.validator.validate(messages)``.
    """
    global _catalog
    if _catalog is not None:
        return _catalog
    with _catalog_lock:
        if _catalog is not None:
            return _catalog
        try:
            from a2ui.schema.catalog import A2uiCatalog
        except ImportError as e:  # pragma: no cover - env drift guard
            raise SchemaAssetError(
                "a2ui-agent-sdk is not installed; the GenUI layer requires "
                "it (pyproject dependency `a2ui-agent-sdk>=0.4.0`)") from e
        catalog = A2uiCatalog(
            version=PROTOCOL_VERSION.removeprefix("v"),
            name="basic",
            catalog_schema=_load("catalogs_basic/catalog.json"),
            s2c_schema=_load("json/server_to_client.json"),
            common_types_schema=_load("json/common_types.json"),
        )
        if catalog.catalog_id != CATALOG_ID:
            raise SchemaAssetError(
                f"vendored catalog $id {catalog.catalog_id!r} does not "
                f"match protocol.CATALOG_ID {CATALOG_ID!r} — the mirror "
                "and protocol.py have drifted apart")
        _catalog = catalog
        return _catalog


def get_validator():
    """The SDK validator bound to the vendored Basic Catalog."""
    return get_catalog().validator


def validate_protocol_messages(messages: list[dict[str, Any]]
                               ) -> list[str]:
    """Layer-1 validation: raw A2UI protocol/catalog conformance.

    Returns a list of human-readable errors (empty == conformant). The
    SDK raises one joined ``A2uiValidatorError``; we normalise it back to
    per-line strings so callers can surface every problem at once.
    """
    try:
        get_validator().validate(messages)
        return []
    except Exception as exc:  # SDK raises its own error type; normalise
        return [line for line in str(exc).splitlines() if line.strip()]


# ── prompt-facing summaries (for the GenUI decoder system prompt) ──────────

def basic_catalog_names() -> list[str]:
    """The 18 Basic Catalog component type names, in mirror order."""
    cat = _load("catalogs_basic/catalog.json")
    return list((cat.get("components") or {}).keys())


def catalog_prompt_summary(max_chars: int = 4000) -> str:
    """Compact component digest for the decoder prompt — with the CORRECT
    v0.9 binding/action syntax spelled out (the bench's v0.8-era
    'dataBinding' guidance is a known upstream copy-paste bug; do not
    replicate it)."""
    cat = _load("catalogs_basic/catalog.json")
    lines = ["## A2UI Basic Catalog (v0.9, 18 components)"]
    for name, spec in (cat.get("components") or {}).items():
        desc = (spec.get("description") or "").strip().split("\n")[0][:110]
        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    lines += [
        "",
        "## v0.9 component + binding syntax (flat discriminator)",
        '- Each component: {"id":"...","component":"<TypeName>",...props}.',
        '- Exactly ONE component must have id "root".',
        '- Bind dynamic values with {"path": "<json-pointer>"} on Dynamic*',
        '  properties, e.g. {"id":"f","component":"TextField",',
        '  "label":"Date","value":{"path":"/variables/release_date/desired"}}.',
        '- v0.9 has NO "dataBinding" property (that is v0.8 syntax and',
        '  fails validation). Plain literals stay plain: {"text":"hello"}.',
        '- Buttons: {"component":"Button","child":"<text-component-id>",',
        '  "action":{"event":{"name":"taskvm.local_patch",',
        '  "context":{"semanticKey":"<variable key>"}}}}.',
    ]
    out = "\n".join(lines)
    return out[:max_chars] + "\n... (catalog truncated)" if len(out) > max_chars else out
