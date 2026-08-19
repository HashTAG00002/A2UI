"""A2UI v0.9 vendored protocol mirror — spec integrity tests (A1, workplan §7-P0).

These tests lock the vendored mirror at ``docs/A2UI-protocol-spec/v0_9`` to the
pinned upstream commit recorded in ``SOURCE.txt``:

1. every vendored JSON file parses;
2. every local ``$ref`` resolves to a file that exists in the mirror (recursive);
3. ``sample.json`` completes full schema resolution against a registry built
   from the whole ``json/`` directory (the pre-A1 mirror could NOT do this:
   ``sample.json`` references ``server_to_client_list.json``, which was
   missing);
4. the Basic Catalog invariant from the official living doc holds: the catalog
   JSON Schema ``$id`` and the A2UI ``catalogId`` both exist and are the same
   URI;
5. every vendored file's SHA-256 matches the manifest inside ``SOURCE.txt``
   (content-addressed pin — a drifted file fails even if it still parses);
6. the runtime version discipline: strict v0.9, no v0.9.1 payloads;
7. ``agent_sdk_reference/`` is clearly marked SDK reference, non-normative.

All checks are offline (no network): the SHA-256 manifest in SOURCE.txt is the
pinned source of truth.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPEC_ROOT = Path(__file__).resolve().parents[2] / "docs" / "A2UI-protocol-spec"
V0_9 = SPEC_ROOT / "v0_9"
JSON_DIR = V0_9 / "json"

#: The four list/wrapper schemas that were missing before A1.
MISSING_BEFORE_A1 = [
    "client_to_server_list.json",
    "client_to_server_list_wrapper.json",
    "server_to_client_list.json",
    "server_to_client_list_wrapper.json",
]

#: The canonical catalog identity shared by $id and catalogId.
BASIC_CATALOG_URI = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"

JSON_FILES = sorted(JSON_DIR.glob("*.json"))

#: Upstream files that ship WITHOUT $id/$schema headers (verified against
#: github.com/a2ui-project/a2ui @ 1133490 — do not "fix" them locally).
SCHEMAS_WITHOUT_ID = {"client_to_server.json"}

#: The official conformance runner (specification/v0_9/test/run_tests.py in
#: the upstream repo) resolves server_to_client.json's "$ref: catalog.json"
#: by aliasing catalogs/basic/catalog.json under the URI
#: https://a2ui.org/specification/v0_9/catalog.json. We replicate that exact
#: mechanism here (upstream-verified, not a local invention).
CATALOG_ALIAS_URI = "https://a2ui.org/specification/v0_9/catalog.json"
BASIC_CATALOG_PATH = V0_9 / "catalogs_basic" / "catalog.json"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ── 1. all vendored JSON parses ────────────────────────────────────────────

def test_json_directory_is_complete() -> None:
    """The 11 official v0.9 json files are all present, incl. the 4 added by A1."""
    names = {p.name for p in JSON_FILES}
    expected = {
        "client_capabilities.json",
        "client_data_model.json",
        "client_to_server.json",
        "client_to_server_list.json",
        "client_to_server_list_wrapper.json",
        "common_types.json",
        "sample.json",
        "server_capabilities.json",
        "server_to_client.json",
        "server_to_client_list.json",
        "server_to_client_list_wrapper.json",
    }
    assert names == expected


@pytest.mark.parametrize("path", JSON_FILES, ids=lambda p: p.name)
def test_all_json_parse(path: Path) -> None:
    doc = _load(path)
    assert isinstance(doc, dict)
    # every schema that declares $schema declares draft 2020-12
    # (client_to_server.json ships without $id/$schema upstream — verbatim)
    if "$schema" in doc:
        assert doc["$schema"] == (
            "https://json-schema.org/draft/2020-12/schema"), (
            f"{path.name} is not a draft 2020-12 JSON Schema")
    else:
        assert path.name in SCHEMAS_WITHOUT_ID


@pytest.mark.parametrize("name", MISSING_BEFORE_A1)
def test_previously_missing_files_exist(name: str) -> None:
    assert (JSON_DIR / name).is_file()


# ── 2. every local $ref resolves ───────────────────────────────────────────

def _resolve_local_ref(source: Path, ref: str) -> Path | None:
    """Map a file-relative $ref to a concrete mirror path.

    ``catalog.json`` is the official alias for the basic catalog (upstream
    conformance runner behavior); every other relative ref must live in
    ``json/``.
    """
    file_part = ref.split("#", 1)[0]
    if not file_part:
        return source  # pure fragment pointer into the same file
    if file_part == "catalog.json":
        return BASIC_CATALOG_PATH
    return JSON_DIR / file_part


def _collect_local_refs(node: Any, source: Path, seen: set[tuple[Path, str]],
                        out: list[tuple[Path, str]]) -> None:
    """Recursively collect (file, ref) pairs for file-relative $refs."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                ref = value.split("#", 1)[0]  # file part only
                if ref and not re.match(r"^[a-z][a-z0-9+.-]*:", ref):
                    # relative file reference (not a URI with scheme)
                    key_pair = (source, ref)
                    if key_pair not in seen:
                        seen.add(key_pair)
                        out.append(key_pair)
            else:
                _collect_local_refs(value, source, seen, out)
    elif isinstance(node, list):
        for item in node:
            _collect_local_refs(item, source, seen, out)


def test_all_local_refs_resolve() -> None:
    failures: list[str] = []
    for path in JSON_FILES:
        doc = _load(path)
        refs: list[tuple[Path, str]] = []
        _collect_local_refs(doc, path, set(), refs)
        for _src, ref in refs:
            target = _resolve_local_ref(_src, ref)
            if target is None or not target.is_file():
                failures.append(f"{_src.name} -> {ref}: target file missing")
    assert not failures, "unresolvable local $refs:\n" + "\n".join(failures)


# ── 3. sample.json completes full schema resolution ────────────────────────

def _build_registry() -> Registry:
    """Register every vendored schema under its canonical URI.

    Two upstream conventions are replicated here (both verified against the
    official conformance runner in the upstream repo):
    - client_to_server.json ships without $id, so it is registered under its
      directory-derived canonical URI;
    - the basic catalog is additionally aliased under
      https://a2ui.org/specification/v0_9/catalog.json so that
      server_to_client.json's "$ref: catalog.json#..." resolves.
    """
    registry: Registry = Registry()
    for path in JSON_FILES:
        doc = _load(path)
        if "$id" in doc:
            uri = doc["$id"]
        else:
            uri = (f"https://a2ui.org/specification/v0_9/{path.name}")
        resource = Resource.from_contents(doc, default_specification=DRAFT202012)
        registry = registry.with_resource(uri, resource)

    # official catalog alias (see CATALOG_ALIAS_URI docstring)
    catalog = _load(BASIC_CATALOG_PATH)
    aliased = dict(catalog)
    aliased["$id"] = CATALOG_ALIAS_URI
    registry = registry.with_resource(
        CATALOG_ALIAS_URI,
        Resource.from_contents(aliased, default_specification=DRAFT202012))
    return registry


def test_sample_json_full_resolution() -> None:
    """sample.json's $ref chain resolves to the end through the registry.

    Chain: sample.json -> server_to_client_list.json -> server_to_client.json
    -> common_types.json / catalog.json#/$defs/... (all local). Each $ref is
    resolved with the resolver carried by its parent Resolved object, which is
    how relative file refs AND pure fragment refs (#/$defs/...) both work.
    """
    registry = _build_registry()
    sample_doc = _load(JSON_DIR / "sample.json")
    root = Resource.from_contents(sample_doc, default_specification=DRAFT202012)
    resolver = registry.resolver_with_root(root)

    resolved: set[str] = set()
    visited: set[int] = set()

    def _walk(node: Any, current_resolver: Any, depth: int = 0) -> None:
        assert depth < 512, "$ref resolution runaway"
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                try:
                    hit = current_resolver.lookup(ref)
                    resolved.add(ref)
                    # visited-key on the identity of the resolved contents:
                    # the registry caches resources, so the same document
                    # fragment is the same object — recursive catalog $defs
                    # (anyComponent -> Row -> anyComponent...) are legal
                    # schema design and must not recurse forever
                    if id(hit.contents) in visited:
                        return
                    visited.add(id(hit.contents))
                    # continue walking inside the resolved document with the
                    # resolver rooted there (handles both relative file refs
                    # and pure fragment pointers correctly)
                    _walk(hit.contents, hit.resolver, depth + 1)
                except Exception as exc:  # noqa: BLE001 — fail with context
                    pytest.fail(f"sample.json cannot resolve $ref {ref!r}: {exc}")
            for key_, value in node.items():
                if key_ != "$ref":
                    _walk(value, current_resolver, depth + 1)
        elif isinstance(node, list):
            for item in node:
                _walk(item, current_resolver, depth + 1)

    _walk(sample_doc, resolver)

    # the chain MUST have reached the file whose absence motivated A1
    assert any("server_to_client_list" in r for r in resolved), (
        "sample.json resolution never reached server_to_client_list.json "
        "(the schema that was missing before A1)")
    # and the leaf message schema
    assert any(r == "server_to_client.json" for r in resolved)


def test_every_schema_is_a_valid_draft2020_document() -> None:
    """All 11 vendored schemas pass jsonschema meta-schema checking."""
    for path in JSON_FILES:
        doc = _load(path)
        Draft202012Validator.check_schema(doc)


# ── 4. Basic Catalog $id == catalogId ──────────────────────────────────────

def test_basic_catalog_id_equals_catalog_id() -> None:
    catalog = _load(V0_9 / "catalogs_basic" / "catalog.json")
    schema_id = catalog.get("$id")
    catalog_id = catalog.get("catalogId")
    assert isinstance(schema_id, str) and schema_id, "catalog.json missing $id"
    assert isinstance(catalog_id, str) and catalog_id, "catalog.json missing catalogId"
    assert schema_id == catalog_id == BASIC_CATALOG_URI


def test_protocol_doc_states_id_catalogid_invariant() -> None:
    """The living doc carries the official clarification sentence."""
    text = (V0_9 / "docs" / "a2ui_protocol.md").read_text(encoding="utf-8")
    assert "$id" in text and "catalogId" in text
    assert re.search(
        r"both .\$id. .* and .catalogId. .* should be set to the same URI"
        r"|both fields should be set to the same URI",
        text, re.IGNORECASE), (
        "a2ui_protocol.md lost the catalog $id == catalogId clarification")


def test_protocol_doc_examples_use_canonical_catalog_uri() -> None:
    text = (V0_9 / "docs" / "a2ui_protocol.md").read_text(encoding="utf-8")
    assert BASIC_CATALOG_URI in text


# ── 5. SHA-256 manifest pin ────────────────────────────────────────────────

def _parse_source_manifest() -> dict[str, str]:
    text = (SPEC_ROOT / "SOURCE.txt").read_text(encoding="utf-8")
    manifest: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-f]{64})\s{2}(\S+)$", line.strip())
        if match:
            digest, rel = match.groups()
            manifest[rel] = digest
    assert manifest, "SOURCE.txt contains no SHA-256 manifest lines"
    return manifest


def test_source_txt_pins_upstream_commit() -> None:
    text = (SPEC_ROOT / "SOURCE.txt").read_text(encoding="utf-8")
    assert "https://github.com/a2ui-project/a2ui" in text
    assert re.search(r"Commit SHA:\s+([0-9a-f]{40})", text), (
        "SOURCE.txt must pin the full 40-char upstream commit SHA")
    assert "Fetch date:" in text
    # the 4 A1 files must be recorded as fetched from that commit
    for name in MISSING_BEFORE_A1:
        assert f"v0_9/json/{name}" in text


def test_vendored_files_match_source_manifest() -> None:
    manifest = _parse_source_manifest()
    # every file present under v0_9/ must be in the manifest (no orphans) ...
    on_disk = {
        str(p.relative_to(SPEC_ROOT))
        for p in V0_9.rglob("*") if p.is_file() and "__pycache__" not in str(p)
    }
    assert on_disk == set(manifest.keys()), (
        f"files on disk but not in manifest: {sorted(on_disk - set(manifest))}; "
        f"in manifest but not on disk: {sorted(set(manifest) - on_disk)}")
    # ... and every file's digest must match
    for rel, expected in manifest.items():
        path = SPEC_ROOT / rel
        assert path.is_file(), f"manifest references missing file {rel}"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == expected, (
            f"{rel} drifted from the pinned upstream content "
            f"(expected {expected}, got {actual})")


# ── 6. version discipline: strict v0.9 ─────────────────────────────────────

def test_v0_9_schemas_declare_v09_identities() -> None:
    """No vendored v0_9 schema may carry a v0.9.1 identity."""
    for path in JSON_FILES:
        doc = _load(path)
        uri = str(doc.get("$id", ""))
        if uri:
            assert "v0_9" in uri, (
                f"{path.name} $id does not identify as v0_9: {uri}")
            assert "v0_9_1" not in uri and "0.9.1" not in uri
        # schemas carrying a version const must const to v0.9 (strict freeze)
        version_const = (doc.get("properties") or {}).get("version", {}).get(
            "const")
        if version_const is not None:
            assert version_const == "v0.9", (
                f"{path.name} version const is {version_const!r}, not v0.9")


# ── 7. agent_sdk_reference is marked non-normative ─────────────────────────

def test_agent_sdk_reference_marked_non_normative() -> None:
    readme = (V0_9 / "agent_sdk_reference" / "README.md").read_text(
        encoding="utf-8")
    assert "NOT a normative protocol specification" in readme
