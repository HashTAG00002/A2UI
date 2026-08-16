"""taskvm.harness.locator — canonical-row ↔ visible-title helpers.

Bench-plane companion of ``replay_engine`` (this module migrates with the
bench split). It holds the per-app visible "title" column map and the
``{visible_title: entity_id}`` index builders the DOM parser uses to key
entities by their SCREEN-VISIBLE identity.

GG red-line §0 lineage: ``entity_id`` is the app's database primary key —
a real user never sees it on screen, so it must never enter any model
input. The title field IS rendered on screen (the user sees "项目发布会议"),
so reading it from canonical state to build the title↔entity_id map is
reading a screen-visible value, not hidden ground truth.

(The former production home ``taskvm/governance/translate.py`` was deleted
by the Wave-3 cluster deletion — the runtime plane targets surfaces via
Observation → State Compiler → SurfaceHandle; only this bench-side parser
still needs the canonical-row indexing helpers.)
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── per-app visible "title" field (the screen-visible identity column) ─────────
# Confirmed by reading each app's <th> headers + _new_session row schema:
# calendar=title, taskboard=title, drive=name, mail=subject, outlook_cal=subject,
# wechat=peer_name, alipay=counterpartyName. Mirrors _KIND_MAP
# (replay_engine.py) — per-app constants.
TITLE_FIELD: dict[str, str] = {
    "calendar": "title", "taskboard": "title", "drive": "name",
    "mail": "subject", "outlook_cal": "subject",
    "wechat": "peer_name", "alipay": "counterpartyName",
}

# ── per-app internal ID field (canonical-row primary key, control-plane) ──────
ID_FIELD: dict[str, str] = {
    "calendar": "eid", "taskboard": "tid", "drive": "fid",
    "mail": "mid", "outlook_cal": "aid", "wechat": "id", "alipay": "id",
}


def _visible_title(app: str, row: dict | None) -> str | None:
    """Return the visible title value for one canonical entity row, or None."""
    f = TITLE_FIELD.get(app)
    if not f or not isinstance(row, dict):
        return None
    v = row.get(f)
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def build_locator_index(canonical_entities: dict[str, dict[str, Any]],
                        app: str) -> dict[str, str]:
    """Build ``{visible_title: entity_id}`` from canonical state.

    ``canonical_entities``: ``{entity_id: {field: value, ...}}``. The title
    field is screen-visible, so reading it is not a GT leak.

    On title collision (two entities share a title), the LAST entity wins in
    the returned dict BUT the collision is logged — callers that need
    unambiguous resolution should use :func:`build_locator_index_strict`
    and fail honestly on collisions rather than silently picking one.
    """
    index: dict[str, str] = {}
    for eid, row in (canonical_entities or {}).items():
        title = _visible_title(app, row)
        if title is None:
            continue
        if title in index:
            logger.warning(
                f"[locator] {app}: title {title!r} collision "
                f"({index[title]} vs {eid}) — last wins; use strict mode to fail")
        index[title] = eid
    return index


def build_locator_index_strict(
        canonical_entities: dict[str, dict[str, Any]],
        app: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Strict variant: returns ``(index, collisions)``.

    ``index`` holds only unambiguous titles (``{title: entity_id}``).
    ``collisions`` holds ``{title: [eid, ...]}`` for titles shared by >1
    entity. Callers SHOULD fail honestly when ``collisions`` is non-empty
    (a visible title alone cannot uniquely identify the entity).
    """
    multi: dict[str, list[str]] = {}
    index: dict[str, str] = {}
    for eid, row in (canonical_entities or {}).items():
        title = _visible_title(app, row)
        if title is None:
            continue
        if title in index or title in multi:
            # promote to multi
            existing = multi.setdefault(title, [])
            if index.get(title) and index[title] not in existing:
                existing.append(index.pop(title))
            if eid not in existing:
                existing.append(eid)
        else:
            index[title] = eid
    return index, multi
