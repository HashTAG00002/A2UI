"""Replay engine — captures the compiler's INPUT observations from the live apps.

W1 scope (SenseAct has no replay engine — built fresh, but minimal): "replay"
means the compiler's INPUT is captured from the deterministic rendered apps
seeded with a hand-authored ``seed_state`` (``benchmark/fixtures.py``), NOT
live-captured from a running CUA. The EXECUTE + VERIFY steps are live.

**Read-path-is-GUI (load-bearing)**: observations are captured from the rendered
HTML (``GET /<sid>``) — the DOM the user/agent sees — and parsed into an entity
map. The compiler NEVER reads ``state_adapter.read_canonical`` or the app's
state API directly; it reads what ``capture_obs`` captured from the GUI.

**Replay/state consistency assert (load-bearing — protects "live state")**:
``assert_obs_matches_state`` verifies the DOM-parsed entity map is field-by-field
consistent with ``oracle_state(sid)`` (the real session state, read through an
EvaluationEnvironment — Agent B: the evaluation plane owns canonical reads;
the runtime decision chain never calls it). Catches the
"chain passes but the compiler is looking at a static image detached from the
current real session" failure. Runs before every compiler call; fails loudly.
"""
from __future__ import annotations

import html as _html
import logging
import re
from dataclasses import dataclass
from typing import Any

import requests

from taskvm.benchmark.fixtures import CanonicalTaskGraph, get_task
# GG: translation layer — entity_id ↔ visible locator. The parser now keys
# entities by their VISIBLE TITLE (screen-visible), never by entity_id. The
# title↔entity_id map (control-plane) lives in harness.locator.
from taskvm.harness.locator import (TITLE_FIELD, ID_FIELD,
                                    build_locator_index,
                                    build_locator_index_strict)

logger = logging.getLogger(__name__)


# ── observation dataclasses live in harness/observations.py (neutral, no GT
#    imports) so the compiler path can import them without transitively pulling
#    in benchmark/fixtures.py via this module. ────────────────────────────────
from taskvm.harness.observations import StepObservation, TraceFixture  # noqa: F401,E402


# ── DOM parsing (faithful read-path-GUI, GG: visible-title-keyed) ────────────
# GG red-line §0: entity_id (E1/T1/.../wxid_*) is a database primary key — it is
# NEVER rendered on a real software screen, so it MUST NOT enter any model
# input. The DOM no longer carries ``data-{kind}-id`` row attrs (entity_id) nor
# a visible ID column. Instead each entity row is keyed by its VISIBLE TITLE
# (the screen-visible identity column: title / name / subject / peer_name /
# counterpartyName — see harness.locator.TITLE_FIELD).
#
# ``data-field`` is kept on cells as the parser's field-key contract. It is an
# HTML attribute (NOT rendered on screen) and the raw DOM HTML is NO LONGER fed
# to the compiler (compiler.build_user_prompt dropped the DOM section in GG.2),
# so ``data-field`` never enters any model input. Keeping it (vs pure
# header-position parsing) preserves the exact field-key mapping that the
# verifier's field-by-field compare relies on (e.g. <th>Publish</th> ↔ field
# ``publish_date`` — a position parse would mismatch). This is a deliberate,
# red-line-compliant robustness choice (see commit msg + .mrules E22).
_ROW_RE = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
_CELL_RE = re.compile(
    r'<td[^>]*\bdata-field="([^"]+)"[^>]*>(.*?)</td>',
    re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

# The set of screen-visible title field names — a row is keyed by whichever of
# these its cells carry. (calendar/taskboard=title, drive=name, mail/outlook_cal
# =subject, wechat=peer_name, alipay=counterpartyName.)
_TITLE_FIELD_VALUES = set(TITLE_FIELD.values())

# Reverse of _KIND_MAP (below) keyed by title-field → kind, for the typed
# variant on combined pages (MobileGym bridge: peer_name→chat,
# counterpartyName→transaction). "title"/"subject" each map to two apps, but
# combined pages only ever mix wechat+alipay (unambiguous title fields), and
# single-app pages don't use the typed variant, so the ambiguity is benign.
_TITLE_FIELD_TO_KIND: dict[str, str] = {}  # filled after _KIND_MAP is defined


def _strip_tags(s: str) -> str:
    s = _html.unescape(_TAG_RE.sub("", s or ""))
    return " ".join(s.split()).strip()


def _row_fields(row_html: str) -> dict[str, Any]:
    """Extract ``{data-field: visible-text}`` from one ``<tr>``'s ``<td>`` cells."""
    fields: dict[str, Any] = {}
    for cm in _CELL_RE.finditer(row_html or ""):
        fields[cm.group(1)] = _strip_tags(cm.group(2))
    return fields


def _row_title_key(fields: dict[str, Any]) -> str | None:
    """The visible-title value for a row — the value of whichever cell's
    ``data-field`` is a known title field. None if the row has none (e.g. a
    header ``<th>`` row, or a non-entity row)."""
    for tf in _TITLE_FIELD_VALUES:
        v = fields.get(tf)
        if v:
            return v
    return None


def parse_dom_entities(dom_html: str) -> dict[str, dict[str, Any]]:
    """Parse the rendered DOM into ``{visible_title: {field: value}}``.

    GG: entities are keyed by their **visible title** (screen-visible), NOT by
    ``entity_id``. The title↔entity_id translation is the harness control
    plane (``harness.locator.build_locator_index``); this function's
    output is the model-visible form. ``data-field`` cells supply exact field
    keys (control-plane parser contract; not fed to the model).

    NOTE: on a combined page (e.g. the MobileGym bridge serves wechat chats +
    alipay txs in ONE html), this returns ALL entities mixed — use
    ``parse_dom_entities_typed`` + ``split_entities_by_app`` to separate.
    """
    entities: dict[str, dict[str, Any]] = {}
    for m in _ROW_RE.finditer(dom_html or ""):
        fields = _row_fields(m.group(1))
        if not fields:
            continue  # header <th> row or empty
        key = _row_title_key(fields)
        if key is None:
            continue  # row with no title cell — not an entity row
        entities[key] = fields
    return entities


def parse_dom_entities_typed(dom_html: str) -> dict[str, dict[str, Any]]:
    """Like ``parse_dom_entities`` but tags each entity with its row's KIND
    (``event``/``task``/``file``/``mail``/``appointment``/``chat``/
    ``transaction``) under the ``_kind`` key. The kind is inferred from which
    title field the row carries (peer_name→chat, counterpartyName→transaction,
    …) — GG removed the ``data-{kind}-id`` attr that used to carry it. Needed
    for a combined page (MobileGym bridge html_view serves BOTH wechat chats +
    alipay txs): the caller splits by ``_kind`` into per-app observations
    (``split_entities_by_app``). W1/W2 don't need this (each core app is its
    own service with a single-kind page)."""
    entities: dict[str, dict[str, Any]] = {}
    for m in _ROW_RE.finditer(dom_html or ""):
        fields = _row_fields(m.group(1))
        if not fields:
            continue
        key = _row_title_key(fields)
        if key is None:
            continue
        # find the kind from the title field that matched
        kind = None
        for tf, k in _TITLE_FIELD_TO_KIND.items():
            if fields.get(tf):
                kind = k
                break
        entities[key] = {"_kind": kind, **fields}
    return entities


# Per-app entity-kind label for the synthesized a11y (so a third app does not
# silently fall into the binary calendar/task branch). Add an entry per app.
# Held-out apps (mail, outlook_cal) use genuinely-new kind labels so the OOD
# probe tests whether the compiler generalizes past the seen kinds.
# MobileGym apps (wechat/alipay) added for task-4 binding discovery —
# appended at the end so W1/W2 core-app a11y stays byte-identical.
_KIND_MAP = {"calendar": "event", "taskboard": "task", "drive": "file",
             "mail": "message", "outlook_cal": "appointment",
             "wechat": "chat", "alipay": "transaction"}

# GG: fill the title-field → kind reverse map used by parse_dom_entities_typed
# to infer an entity's kind from its visible title field (no more data-{kind}-id
# attr). "title"→"event" then overwritten by "task" (calendar/taskboard share
# title), "subject"→"message" then "appointment" (mail/outlook_cal share
# subject) — benign: combined pages only mix wechat(peer_name→chat) +
# alipay(counterpartyName→transaction), and single-app pages don't use the
# typed variant.
for _app, _kind in _KIND_MAP.items():
    _TITLE_FIELD_TO_KIND[TITLE_FIELD[_app]] = _kind

# Field whitelist for the a11y text. Kept as an explicit tuple (NOT derived from
# the entity's keys) so the calendar/taskboard a11y output stays byte-stable.
# GG: ``peer_wxid`` removed (wechat internal id — not screen-visible → must not
# be in the model input). ``peer_name`` kept (the visible contact name).
# New apps add their visible fields here.
_A11Y_FIELDS = ("title", "date", "time", "calendar", "rsvp",
                "status", "assignee", "deadline", "depends_on",
                "name", "content", "parent", "owner", "modified", "type",
                "subject", "from_addr", "to_addr", "state", "received",
                "priority", "scheduled_for",
                "publish_date", "send_date",
                "peer_name", "n_messages", "last_message",
                "messages", "counterpartyName", "delta", "timestamp",
                "category", "note", "description")


def synthesize_a11y(app: str, entities: dict[str, dict[str, Any]]) -> str:
    """Build a text accessibility-tree-like representation from the parsed DOM
    entities. This is the compiler's primary text input (faithful to the GUI).

    GG: entities are keyed by their **visible title** (the dict key passed in is
    the screen-visible title, not entity_id). The ``[bid={eid}]`` prefix is
    GONE (it leaked the entity_id into the model input). Each entity line is
    introduced by its visible title value naturally (``title=项目发布会议``),
    with no internal id + no operator jargon."""
    kind = _KIND_MAP.get(app, app)
    lines = [f"[{app}] {kind}s:"]
    for _title, fields in entities.items():
        # entity line: lead with the visible title field, then the rest
        parts: list[str] = []
        for fname in _A11Y_FIELDS:
            if fname in fields:
                parts.append(f"{fname}={fields[fname]}")
        lines.append("  " + " ".join(parts))
    return "\n".join(lines)


# Reverse of _KIND_MAP (kind → app). Used by ``split_entities_by_app`` to route
# parsed entities of each kind back to their owning app's observation.
_KIND_TO_APP = {kind: app for app, kind in _KIND_MAP.items()}


def split_entities_by_app(typed_entities: dict[str, dict[str, Any]],
                          apps: list[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """Split a typed entity map (from ``parse_dom_entities_typed``) into
    ``{app: {visible_title: {field: value}}}``. Entities whose ``_kind`` maps
    to an app NOT in ``apps`` are dropped (the caller asked for a specific app
    set). MobileGym: a combined wechat+alipay page → ``{wechat: {peer_name:
    {...}}, alipay: {counterpartyName: {...}}}``."""
    out: dict[str, dict[str, dict[str, Any]]] = {a: {} for a in apps}
    for title, fields in typed_entities.items():
        kind = fields.get("_kind")
        app = _KIND_TO_APP.get(kind)
        if app in out:
            out[app][title] = {k: v for k, v in fields.items() if k != "_kind"}
    return out


# ── core API ─────────────────────────────────────────────────────────────────
def load_task(task_id: str) -> CanonicalTaskGraph:
    """Return the canonical task graph (verifier-only GT). The orchestrator uses
    this for seed_state (→ apps) + the canonical graph (→ verifier). The compiler
    NEVER receives this object."""
    return get_task(task_id)


def seed_apps(fixture: CanonicalTaskGraph, envs: dict, sid: str) -> None:
    """Seed each app with the fixture's ``seed_state`` (the visible initial state)
    through the EVALUATION environments (Agent B: seeding is an exam-room
    power — reset/seed live on the evaluation plane, never the runtime port).
    No canonical GT is sent to the apps — only the visible events/tasks."""
    for app, ad in envs.items():
        seed = (fixture.seed_state.get(app) or {})
        ad.seed(sid, task_id=fixture.task_id, goal=fixture.goal, seed_state=seed)
    logger.info(f"[replay] seeded {list(envs)} for sid={sid} task={fixture.task_id}")


def capture_obs(adapters: dict, sid: str, step: int = 0,
                 with_screenshot: bool = False) -> dict[str, StepObservation]:
    """Capture the rendered GUI observations for each app: DOM HTML (GET /<sid>)
    + a11y text (parsed from the DOM) + optional screenshot. This is the
    compiler's INPUT — faithful to the read-path-GUI."""
    obs: dict[str, StepObservation] = {}
    for app, ad in adapters.items():
        url = f"{ad.base_url}/{sid}"
        r = requests.get(url, timeout=ad.timeout)
        r.raise_for_status()
        dom_html = r.text
        entities = parse_dom_entities(dom_html)
        a11y = synthesize_a11y(app, entities)
        shot = _try_screenshot(url) if with_screenshot else None
        obs[app] = StepObservation(app=app, step=step, dom_html=dom_html,
                                   a11y_text=a11y, screenshot_path=shot)
    return obs


def assert_obs_matches_state(envs: dict, sid: str,
                             obs: dict[str, StepObservation]) -> None:
    """Field-by-field assert: the DOM-parsed entity map (what the compiler sees)
    must match ``oracle_state(sid)`` (the real session state, via the
    evaluation environments). Raises
    ``AssertionError`` on mismatch — loudly fails the run (protects "live state").

    GG: the DOM is now keyed by **visible title** and canonical by **entity_id**.
    This control-plane seam re-keys the DOM title→entity_id using a locator
    index built from canonical state (the title IS screen-visible, so reading it
    is not a GT leak). Title collisions (two entities share a title) are
    reported honestly — the assert fails rather than silently picking one."""
    mismatches: list[str] = []
    for app, ad in envs.items():
        canonical = ad.oracle_state(sid)
        canonical_entities = canonical["entities"]
        dom_entities = parse_dom_entities(obs[app].dom_html)  # {title: {field}}
        # build title→entity_id from canonical (strict: detect collisions)
        loc_index, collisions = build_locator_index_strict(canonical_entities, app)
        for title, eids in collisions.items():
            mismatches.append(
                f"{app}: visible title {title!r} is ambiguous — maps to "
                f"{eids}; cannot re-key DOM to entity_id uniquely")
        # re-key DOM title → entity_id
        dom_by_eid: dict[str, dict[str, Any]] = {}
        for title, fields in dom_entities.items():
            eid = loc_index.get(title)
            if eid is None:
                # title on screen but not in canonical (stale DOM, or a row
                # canonical doesn't know) — report by title (no entity_id exists)
                mismatches.append(
                    f"{app}: DOM entity with visible title {title!r} has no "
                    f"matching canonical entity (dom-only)")
                continue
            if eid in dom_by_eid:
                mismatches.append(
                    f"{app}: two DOM rows resolved to entity_id {eid} "
                    f"(titles {dom_by_eid[eid]!r} and {title!r})")
            dom_by_eid[eid] = {**fields, "_dom_title": title}
        dom_ids = set(dom_by_eid)
        canon_ids = set(canonical_entities)
        if dom_ids != canon_ids:
            mismatches.append(
                f"{app}: entity set mismatch. DOM(titles)={sorted(dom_entities)} "
                f"canonical(eids)={sorted(canon_ids)} "
                f"dom-only={sorted(dom_ids - canon_ids)} "
                f"canonical-only={sorted(canon_ids - dom_ids)}")
            continue
        for eid in canon_ids:
            cfields = canonical_entities[eid]
            dfields = dom_by_eid[eid]
            # GG: skip the id_field (entity_id key name) in field-by-field
            # compare — it's the primary key (already matched via the
            # title→entity_id re-key), never a screen-visible field, so the DOM
            # (after GG.1) correctly omits it. Comparing it would always flag
            # DOM=None vs canonical=<eid>. All OTHER visible fields are compared.
            id_field = ID_FIELD.get(app)
            for fname, cval in cfields.items():
                if id_field and fname == id_field:
                    continue
                dval = dfields.get(fname)
                if not _field_eq(cval, dval):
                    mismatches.append(
                        f"{app}.{eid}.{fname}: DOM={dval!r} canonical={cval!r}")
    if mismatches:
        raise AssertionError(
            "replay/state consistency assert FAILED — the compiler's DOM input "
            "does not match the real session state (the 'live state' anchor is "
            "violated):\n  " + "\n  ".join(mismatches))
    logger.info(f"[replay] obs matches state for sid={sid} (all entities+fields)")


def _field_eq(cval: Any, dval: Any) -> bool:
    """Tolerant field comparison: lists compared as comma-joined strings;
    strings trimmed + case-insensitive."""
    if isinstance(cval, list):
        cstr = ", ".join(str(x) for x in cval)
        return _strip_tags(str(dval or "")).lower() == cstr.lower() or \
               _strip_tags(str(dval or "")).lower() == "".join(str(x) for x in cval).lower()
    return _strip_tags(str(cval or "")).lower() == _strip_tags(str(dval or "")).lower()


def _try_screenshot(url: str) -> str | None:
    """Optional: render the page to a PNG via Playwright. Returns a file path or
    None if Playwright/the browser is unavailable. W1 defaults to no screenshot
    (DOM + a11y suffice); enable for visual grounding if the model needs it."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(url, wait_until="networkidle", timeout=15000)
            png = page.screenshot(full_page=True)
            browser.close()
        import tempfile, os
        fd, path = tempfile.mkstemp(suffix=".png", prefix="taskvm_obs_")
        with os.fdopen(fd, "wb") as f:
            f.write(png)
        return path
    except Exception as e:
        logger.warning(f"[replay] screenshot capture failed (non-fatal): {e}")
        return None
