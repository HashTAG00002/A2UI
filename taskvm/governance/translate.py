"""GG translation layer (§2 ④a): entity_id ↔ visible locator.

**GG red-line §0 (control-plane / model-visible / translation)**: ``entity_id``
(``E1``/``T1``/``F1``/``M1``/``A1``/``wxid_*``) is the app's database primary
key. A real user holding a real device never sees it on screen, so it MUST NOT
enter any model input (observation / instruction / prompt / context). This
module is the harness-internal bridge that lets ``entity_id`` live server-side
(canonical state, fixtures/GT, saga, verifier — all control-plane) while the
model + the user see only **visible locators** (the screen-visible title column:
``title`` / ``name`` / ``subject`` / ``peer_name`` / ``counterpartyName``).

Why this is not a GT leak: the title field IS rendered on screen (the user sees
"项目发布会议"), so reading it from canonical state to build the locator↔entity_id
map is reading a screen-visible value, not reading hidden ground-truth. The
hidden GT (``expected_diff`` / ``non_interference_set``) stays in
``benchmark/fixtures.py`` and is never imported here.

Two directions:
  - **resolve_locator** (model → control): the compiler emits ``locator`` (a
    visible title); this injects the resolved ``entity_id`` into the binding so
    downstream ``compile_patch`` / ``dispatch`` / ``adapter.mutate`` / verifier
    — which all speak ``entity_id`` — work unchanged.
  - **entity_id_to_locator** (control → model): given ``(app, entity_id)``,
    produce a human-readable visible-locator description ("标题为'项目发布会议'的
    会议") for SubgoalGenerator instruction text. Returns None on miss → the
    caller emits an honest "cannot locate" fail, NEVER a fallback to entity_id.

No function in this module ever puts ``entity_id`` into a model-facing string.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── per-app visible "title" field (the screen-visible identity column) ─────────
# Confirmed by reading each app's <th> headers + _new_session row schema:
# calendar=title, taskboard=title, drive=name, mail=subject, outlook_cal=subject,
# wechat=peer_name, alipay=counterpartyName. This mirrors _KIND_MAP
# (replay_engine.py) + _EDIT_PATH_KIND (substrate/base.py) — per-app constants.
TITLE_FIELD: dict[str, str] = {
    "calendar": "title", "taskboard": "title", "drive": "name",
    "mail": "subject", "outlook_cal": "subject",
    "wechat": "peer_name", "alipay": "counterpartyName",
}

# ── internal-ID regex (GG §0 推论1) ───────────────────────────────────────────
# entity_id patterns that must NEVER appear in a model input. Used by the GG.3
# no-leak unit assertions + the GG.6 static gate. A locator or subgoal NL that
# matches this is an automatic FAIL (a leak).
INTERNAL_ID_RE = re.compile(
    r"\b(?:E\d+|T\d+|F\d+|M\d+|A\d+|wxid_\w+|p_\d+|chat_id\d*|tx_\w+)\b")

# Operator jargon (GG §1.3): hardcoded operator names must not appear in NL
# subgoal instructions (the model must not parrot the operator vocabulary).
OPERATOR_JARGON_RE = re.compile(
    r"\b(?:move_event|set_deadline|set_status|set_assignee|move_file|rename|"
    r"set_owner|set_publish_date|set_state|set_priority|set_to|set_send_date|"
    r"reschedule_appointment|send_message|toggle_like)\b")

# Field-display names (user-facing Chinese for the writable fields) — used by
# entity_id_to_locator + SubgoalGenerator to render field names a user sees, not
# the internal field key. Falls back to the raw field key if not listed.
FIELD_DISPLAY: dict[str, str] = {
    "date": "日期", "deadline": "截止日期", "status": "状态", "assignee": "负责人",
    "parent": "所在文件夹", "name": "文件名", "owner": "所有者", "publish_date": "发布日期",
    "state": "状态", "priority": "优先级", "to_addr": "收件人", "send_date": "发送日期",
    "subject": "主题", "scheduled_for": "日期", "messages": "消息",
}

# Kind display (user-facing noun for the entity kind, per app).
KIND_DISPLAY: dict[str, str] = {
    "calendar": "会议", "taskboard": "任务", "drive": "文件", "mail": "邮件",
    "outlook_cal": "日程", "wechat": "聊天", "alipay": "交易",
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

    ``canonical_entities``: ``{entity_id: {field: value, ...}}`` (the shape
    ``StateAdapter.read_canonical(sid)["entities"]`` returns). The title field
    is screen-visible, so reading it is not a GT leak.

    On title collision (two entities share a title), the LAST entity wins in the
    returned dict BUT the collision is logged — callers that need unambiguous
    resolution should use :func:`build_locator_index_strict` and fail honestly
    on collisions rather than silently picking one.
    """
    index: dict[str, str] = {}
    for eid, row in (canonical_entities or {}).items():
        title = _visible_title(app, row)
        if title is None:
            continue
        if title in index:
            logger.warning(
                f"[translate] {app}: title {title!r} collision "
                f"({index[title]} vs {eid}) — last wins; use strict mode to fail")
        index[title] = eid
    return index


def build_locator_index_strict(
        canonical_entities: dict[str, dict[str, Any]],
        app: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Strict variant: returns ``(index, collisions)``.

    ``index`` holds only unambiguous titles (``{title: entity_id}``).
    ``collisions`` holds ``{title: [eid, ...]}`` for titles shared by >1 entity.
    Callers SHOULD fail honestly when ``collisions`` is non-empty (a visible
    title alone cannot uniquely identify the entity — the model must
    disambiguate, or the harness surfaces both candidates).
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


def _strip_locator_prefix(locator: str, app: str) -> str:
    """A model may emit ``"title:项目发布会议"`` (field:value) or a bare title
    ``"项目发布会议"``. Normalize to the bare title value for lookup."""
    if not isinstance(locator, str):
        return ""
    loc = locator.strip()
    field = TITLE_FIELD.get(app)
    if field and loc.lower().startswith(field.lower() + ":"):
        loc = loc[len(field) + 1:].strip()
    return loc


def resolve_locator(binding: dict, locator_index: dict[str, dict[str, str]],
                    ) -> tuple[dict, list[str]]:
    """Inject ``entity_id`` into every binding + dependency from its ``locator``.

    ``locator_index``: ``{app: {visible_title: entity_id}}`` (from
    :func:`build_locator_index`). Mutates and returns ``binding``; also returns a
    list of resolution errors (empty iff every locator resolved uniquely).

    A binding whose ``locator`` does not resolve gets ``entity_id = None`` — the
    caller's downstream validate/render_check should treat None entity_id as a
    binding miss (honest). This function does NOT raise; it records errors so the
    orchestrator can report them in the eval JSON (GG §6 honesty).
    """
    errors: list[str] = []
    if not isinstance(binding, dict):
        return binding, ["binding is not a dict"]
    for vi, v in enumerate(binding.get("variables") or []):
        if not isinstance(v, dict):
            continue
        for bi, b in enumerate(v.get("bindings") or []):
            if not isinstance(b, dict):
                continue
            app = b.get("app")
            locator = b.get("locator")
            # if entity_id already present (e.g. mock/GT path), keep it
            if b.get("entity_id"):
                continue
            if not app:
                errors.append(f"variables[{vi}].bindings[{bi}]: missing app")
                continue
            if not locator:
                errors.append(f"variables[{vi}].bindings[{bi}] (app={app}): "
                              f"missing locator")
                continue
            title = _strip_locator_prefix(locator, app)
            app_index = locator_index.get(app, {})
            eid = app_index.get(title)
            if eid is None:
                errors.append(f"variables[{vi}].bindings[{bi}] (app={app}): "
                              f"locator {locator!r} (title {title!r}) not found "
                              f"among visible {app} entities "
                              f"{sorted(app_index.keys())}")
                b["entity_id"] = None
            else:
                b["entity_id"] = eid
    # dependencies: to_entity also needs resolution
    for di, d in enumerate(binding.get("dependencies") or []):
        if not isinstance(d, dict):
            continue
        to_entity = d.get("to_entity")
        if isinstance(to_entity, dict) and not to_entity.get("entity_id"):
            app = to_entity.get("app")
            locator = to_entity.get("locator")
            if app and locator:
                title = _strip_locator_prefix(locator, app)
                eid = locator_index.get(app, {}).get(title)
                if eid is None:
                    errors.append(f"dependencies[{di}]: to_entity locator "
                                  f"{locator!r} (app={app}) not found")
                to_entity["entity_id"] = eid
    return binding, errors


def entity_id_to_locator(app: str, entity_id: str,
                         canonical_entities: dict[str, dict[str, Any]],
                         *, field: str | None = None) -> str | None:
    """Reverse direction (control → model): produce a human-readable visible
    locator description for instruction generation.

    Returns e.g. ``'标题为"项目发布会议"的会议'`` (and the field, if given:
    ``'标题为"项目发布会议"的会议的日期'``). Returns None if the entity or its
    visible title is not found in canonical state — the caller MUST emit an
    honest "cannot locate" fail and MUST NOT fall back to拼接 entity_id.
    """
    row = (canonical_entities or {}).get(entity_id)
    title = _visible_title(app, row)
    if title is None:
        logger.warning(
            f"[translate] entity_id_to_locator({app},{entity_id}): no visible "
            f"title in canonical → honest None (caller must fail, not fabricate)")
        return None
    kind = KIND_DISPLAY.get(app, app)
    desc = f'标题为"{title}"的{kind}'
    if field:
        fd = FIELD_DISPLAY.get(field, field)
        desc += f'的{fd}'
    return desc


def visible_entity_titles(canonical_entities: dict[str, dict[str, Any]],
                          app: str) -> list[str]:
    """The list of visible titles for an app (screen-visible). Used to tell the
    compiler which locators are valid — replaces the old entity_id whitelist
    (GG.2: the whitelist leaked entity_id; titles are screen-visible, safe)."""
    titles = []
    for row in (canonical_entities or {}).values():
        t = _visible_title(app, row)
        if t is not None:
            titles.append(t)
    return titles


def assert_no_internal_id(text: str | None, *, source: str = "") -> list[str]:
    """GG.3/GG.6 no-leak check: scan a model-facing string for internal IDs.
    Returns the list of matched internal-ID substrings (empty = clean). Used by
    the SubgoalGenerator no-leak unit test + the GG.6 static gate."""
    if not text:
        return []
    return INTERNAL_ID_RE.findall(text)


def assert_no_operator_jargon(text: str | None) -> list[str]:
    """GG.3 no-leak check: scan a subgoal NL for operator vocabulary."""
    if not text:
        return []
    return OPERATOR_JARGON_RE.findall(text)


def eid_to_title_in_seed(seed_state: dict, app: str, entity_id: str) -> str | None:
    """Translate a GT entity_id → its visible title using the fixture's
    ``seed_state`` (the VISIBLE app state used to seed, not hidden GT). Used by
    the mock/_gt_task_binding path so the mock binding emits ``locator`` (like a
    real model) instead of ``entity_id``.

    ``seed_state[app]`` is either ``{resource_key: [rows]}`` (the fixture shape:
    calendar→events, taskboard→tasks, drive→files, mail→messages, outlook_cal→
    appointments) or a flat ``[rows]`` list (fallback). Each row carries the
    id_field + the title field."""
    raw = (seed_state or {}).get(app)
    rows: list = []
    if isinstance(raw, dict):
        # find the first list-valued resource key
        for v in raw.values():
            if isinstance(v, list):
                rows = v
                break
    elif isinstance(raw, list):
        rows = raw
    id_field = ID_FIELD.get(app, "id")
    for r in rows:
        if isinstance(r, dict) and str(r.get(id_field)) == str(entity_id):
            return _visible_title(app, r)
    return None


# per-app id_field (the canonical primary-key column). Mirrors
# StateAdapter.id_field (substrate/base.py) — duplicated here so this module
# stays dependency-free (no substrate import on the compiler path). This is the
# entity_id key name; it is NEVER a screen-visible field, so the DOM (after GG.1)
# does not carry it — assert_obs_matches_state skips it in field-by-field compare
# (it's already matched via the title→entity_id re-key, not as a field).
ID_FIELD: dict[str, str] = {
    "calendar": "eid", "taskboard": "tid", "drive": "fid",
    "mail": "mid", "outlook_cal": "aid", "wechat": "id", "alipay": "id",
}
_ID_FIELD = ID_FIELD   # backward-compat alias (was private before GG)
