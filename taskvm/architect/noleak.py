"""Prompt no-leak gate — the L4 execution of the GG red line §0.

The one-question test (docs/A2UI_GG阶段开工目标 §0): *\"this string — could
a real user see it on the rendered screen?\"* If not, it must never enter a
model input. This module scans **actually-built messages** (never just
templates) for the internal vocabulary of the legacy stack:

- database-ish ids: ``E1`` / ``T12`` / ``wxid_xxx`` / ``evt:00042`` /
  ``action:00007`` / ``ckpt:C2`` / ``comp:00001`` …
- internal operator jargon: ``move_event`` / ``set_deadline`` /
  ``toggle_like`` / ``send_message`` / ``read_canonical`` / ``set_state`` …
- DOM-internal attributes: ``data-entity-id`` / ``data-field`` …
- kernel-internal namespaces that are not business semantics.

A hit raises :class:`PromptLeakError` — an HONEST failure. The gate never
silently strips the offending text: a prompt that needed stripping was built
from the wrong inputs and must be fixed at the producer.
"""
from __future__ import annotations

import re


class PromptLeakError(Exception):
    """A model-facing message carried internal, non-visible vocabulary."""


# DB-primary-key-shaped tokens: standalone E1 / T2 / wxid_* / C3 used as an
# ADDRESS (not inside prose). Word-boundary anchored so ordinary English
# words survive.
_DB_ID_RE = re.compile(
    r"\b(?:[ETW]\d{1,6})\b"          # E1, T12, W3 …
    r"|\bwxid_[A-Za-z0-9_]+\b"       # wechat internal ids
    r"|\b(?:evt|action|ckpt|comp|plan|node|saga)[:#]\w+\b"  # kernel/log ids
    r"|\bentity_id\b|\bdata-[a-z-]*id\b",
)

# internal operator / API vocabulary of the legacy stack (extendable via
# extra_terms at call sites for task-specific operators)
_OPERATOR_JARGON = (
    "move_event", "set_deadline", "toggle_like", "send_message",
    "read_canonical", "set_state", "get_state", "undo_saga",
    "compile_patch", "interpret_as_workflow", "_gt_binding",
)

_OPERATOR_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _OPERATOR_JARGON) + r")\b")


def scan(text: str, *, extra_terms: tuple[str, ...] = ()) -> list[str]:
    """Return the list of offending snippets found in ``text`` (empty = clean)."""
    if not text:
        return []
    hits: list[str] = []
    hits.extend(m.group(0) for m in _DB_ID_RE.finditer(text))
    hits.extend(m.group(0) for m in _OPERATOR_RE.finditer(text))
    for term in extra_terms:
        if term and re.search(rf"\b{re.escape(term)}\b", text):
            hits.append(term)
    return hits


def assert_prompt_clean(text: str, *, extra_terms: tuple[str, ...] = (),
                        what: str = "model-facing message") -> None:
    """Raise :class:`PromptLeakError` listing every hit (honest, complete)."""
    hits = scan(text, extra_terms=extra_terms)
    if hits:
        raise PromptLeakError(
            f"{what} carries internal (non screen-visible) vocabulary: "
            f"{sorted(set(hits))}; the prompt must be rebuilt from visible "
            f"evidence only — stripping is not allowed")


def scan_json_values(obj, *, extra_terms: tuple[str, ...] = ()) -> list[str]:
    """Scan every string inside a parsed JSON object/list (model OUTPUT side:
    guards against the model echoing internal ids back as semantic keys)."""
    hits: list[str] = []
    if isinstance(obj, str):
        hits.extend(scan(obj, extra_terms=extra_terms))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                hits.extend(scan(k, extra_terms=extra_terms))
            hits.extend(scan_json_values(v, extra_terms=extra_terms))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            hits.extend(scan_json_values(v, extra_terms=extra_terms))
    return hits
