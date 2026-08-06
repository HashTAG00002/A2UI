"""Canonical-state snapshot + compare helpers for the verifier.

Pure functions over the entity-map shape returned by
``StateAdapter.read_canonical``. The verifier uses these to compare the real
post-edit state against ``CanonicalTaskGraph.expected_diff`` (changed-happened)
and the ``non_interference_set`` (non-interference).

No model, no app imports — just dict comparison. Honesty invariant: these
helpers read ONLY the canonical state snapshotted from the real apps; they
never consult the compiler's binding.
"""
from __future__ import annotations

from typing import Any


def snapshot(adapters: dict, sid: str) -> dict:
    """{app_name: read_canonical(sid)} — the pre/post snapshot shape."""
    return {name: ad.read_canonical(sid) for name, ad in adapters.items()}


def entity_value(snap: dict, app: str, entity_id: str, field: str) -> Any:
    """Read one field of one entity from a snapshot. None if missing."""
    entities = (snap.get(app) or {}).get("entities") or {}
    return (entities.get(entity_id) or {}).get(field)


def entity_record(snap: dict, app: str, entity_id: str) -> dict | None:
    entities = (snap.get(app) or {}).get("entities") or {}
    return entities.get(entity_id)


def entity_unchanged(pre: dict, post: dict, app: str, entity_id: str) -> bool:
    """True iff the entity's full record is byte-identical between pre and post.
    Used by non_interference (an entity in the non_interference_set must be
    unchanged)."""
    return entity_record(pre, app, entity_id) == entity_record(post, app, entity_id)


def field_matches(snap: dict, app: str, entity_id: str, field: str,
                  expected: Any) -> bool:
    """True iff the entity's field equals ``expected`` in ``snap``.
    Used by changed-happened (a binding's expected_value_after_edit met)."""
    actual = entity_value(snap, app, entity_id, field)
    return _eq(actual, expected)


def _eq(a: Any, b: Any) -> bool:
    """Tolerant equality: string-trim + case-insensitive for strings."""
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def changed_entities(pre: dict, post: dict, app: str) -> list[str]:
    """Entity ids in ``app`` whose record differs between pre and post
    (diagnostic: which entities actually changed)."""
    pre_e = (pre.get(app) or {}).get("entities") or {}
    post_e = (post.get(app) or {}).get("entities") or {}
    out = []
    for eid in set(pre_e) | set(post_e):
        if pre_e.get(eid) != post_e.get(eid):
            out.append(eid)
    return out
