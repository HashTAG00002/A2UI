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
consistent with ``read_canonical(sid)`` (the real session state). Catches the
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

logger = logging.getLogger(__name__)


# ── observation dataclasses live in harness/observations.py (neutral, no GT
#    imports) so the compiler path can import them without transitively pulling
#    in benchmark/fixtures.py via this module. ────────────────────────────────
from taskvm.harness.observations import StepObservation, TraceFixture  # noqa: F401,E402


# ── DOM parsing (faithful read-path-GUI) ─────────────────────────────────────
# data-{kind}-id covers every app's entity row: event (calendar), task
# (taskboard), file (drive), mail (mail — held-out unseen), appointment
# (outlook_cal — held-out reskin), chat (wechat — MobileGym), transaction
# (alipay — MobileGym). Adding a new app's id-attribute here is the only
# DOM-parse change needed for a new substrate. MobileGym's chat/transaction
# kinds are appended so W1/W2/W3/W4 core-app output stays byte-identical
# (those apps emit no data-chat-id/data-transaction-id rows).
_ROW_RE = re.compile(
    r'<tr[^>]*\bdata-(?:event|task|file|mail|appointment|chat|transaction)-id="([^"]+)"[^>]*>(.*?)</tr>',
    re.DOTALL | re.IGNORECASE)
# typed variant: captures the kind (event/task/.../chat/transaction) so a
# combined page (e.g. the MobileGym bridge's html_view serving BOTH wechat
# chats + alipay txs in one HTML) can be split into per-app observations.
_ROW_RE_TYPED = re.compile(
    r'<tr[^>]*\bdata-(event|task|file|mail|appointment|chat|transaction)-id="([^"]+)"[^>]*>(.*?)</tr>',
    re.DOTALL | re.IGNORECASE)
_CELL_RE = re.compile(
    r'<td[^>]*\bdata-field="([^"]+)"[^>]*>(.*?)</td>',
    re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    s = _html.unescape(_TAG_RE.sub("", s or ""))
    return " ".join(s.split()).strip()


def parse_dom_entities(dom_html: str) -> dict[str, dict[str, Any]]:
    """Parse the rendered DOM into {entity_id: {field: value}}.

    Reads ``data-event-id`` / ``data-task-id`` / ``data-file-id`` /
    ``data-chat-id`` / ``data-transaction-id`` rows + ``data-field`` cells —
    the stable read-path-GUI surface. Returns the entity map the GUI actually
    shows. NOTE: on a combined page (e.g. the MobileGym bridge serves wechat
    chats + alipay txs in ONE html), this returns ALL entities mixed — use
    ``parse_dom_entities_typed`` + ``split_entities_by_app`` to separate.
    """
    entities: dict[str, dict[str, Any]] = {}
    for m in _ROW_RE.finditer(dom_html or ""):
        eid = m.group(1)
        row_html = m.group(2)
        fields: dict[str, Any] = {}
        for cm in _CELL_RE.finditer(row_html):
            fname = cm.group(1)
            fval = _strip_tags(cm.group(2))
            fields[fname] = fval
        entities[eid] = fields
    return entities


def parse_dom_entities_typed(dom_html: str) -> dict[str, dict[str, Any]]:
    """Like ``parse_dom_entities`` but tags each entity with its row's
    id-attribute KIND (``event``/``task``/``file``/``mail``/``appointment``/
    ``chat``/``transaction``) under the ``_kind`` key. Needed for a combined
    page (MobileGym bridge html_view serves BOTH wechat chats + alipay txs):
    the caller splits the parsed entities by ``_kind`` into per-app
    observations (``split_entities_by_app``). W1/W2 don't need this (each core
    app is its own service with a single-kind page)."""
    entities: dict[str, dict[str, Any]] = {}
    for m in _ROW_RE_TYPED.finditer(dom_html or ""):
        kind = m.group(1)
        eid = m.group(2)
        row_html = m.group(3)
        fields: dict[str, Any] = {"_kind": kind}
        for cm in _CELL_RE.finditer(row_html):
            fname = cm.group(1)
            fval = _strip_tags(cm.group(2))
            fields[fname] = fval
        entities[eid] = fields
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

# Field whitelist for the a11y text. Kept as an explicit tuple (NOT derived from
# the entity's keys) so the W1 calendar/taskboard a11y output stays byte-stable
# (regression guard — W1 PASS must reproduce). New apps add their fields here.
# Mail fields (subject/from_addr/to_addr/state/received/priority) and
# outlook_cal's renamed field (scheduled_for) are appended at the end so the
# original 3 apps' a11y output is byte-identical (regression guard).
# MobileGym wechat (peer_name/messages/n_messages/last_message/peer_wxid) +
# alipay (counterpartyName/delta/timestamp/category/kind/note/description)
# fields appended for task-4 binding discovery.
_A11Y_FIELDS = ("title", "date", "time", "calendar", "rsvp",
                "status", "assignee", "deadline", "depends_on",
                "name", "content", "parent", "owner", "modified", "type",
                "subject", "from_addr", "to_addr", "state", "received",
                "priority", "scheduled_for",
                "publish_date", "send_date",
                "peer_name", "peer_wxid", "n_messages", "last_message",
                "messages", "counterpartyName", "delta", "timestamp",
                "category", "note", "description")


def synthesize_a11y(app: str, entities: dict[str, dict[str, Any]]) -> str:
    """Build a text accessibility-tree-like representation from the parsed DOM
    entities. This is the compiler's primary text input (faithful to the GUI)."""
    kind = _KIND_MAP.get(app, app)
    lines = [f"[{app}] {kind}s:"]
    for eid, fields in entities.items():
        parts = [f'[bid={eid}] {kind}']
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
    ``{app: {entity_id: {field: value}}}``. Entities whose ``_kind`` maps to an
    app NOT in ``apps`` are dropped (the caller asked for a specific app set).
    MobileGym: a combined wechat+alipay page → ``{wechat: {chat_id: {...}},
    alipay: {tx_id: {...}}}``."""
    out: dict[str, dict[str, dict[str, Any]]] = {a: {} for a in apps}
    for eid, fields in typed_entities.items():
        kind = fields.get("_kind")
        app = _KIND_TO_APP.get(kind)
        if app in out:
            out[app][eid] = {k: v for k, v in fields.items() if k != "_kind"}
    return out


# ── core API ─────────────────────────────────────────────────────────────────
def load_task(task_id: str) -> CanonicalTaskGraph:
    """Return the canonical task graph (verifier-only GT). The orchestrator uses
    this for seed_state (→ apps) + the canonical graph (→ verifier). The compiler
    NEVER receives this object."""
    return get_task(task_id)


def seed_apps(fixture: CanonicalTaskGraph, adapters: dict, sid: str) -> None:
    """Seed each app with the fixture's ``seed_state`` (the visible initial state).
    No canonical GT is sent to the apps — only the visible events/tasks."""
    for app, ad in adapters.items():
        seed = (fixture.seed_state.get(app) or {})
        ad.seed(sid, task_id=fixture.task_id, goal=fixture.goal, seed_state=seed)
    logger.info(f"[replay] seeded {list(adapters)} for sid={sid} task={fixture.task_id}")


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


def assert_obs_matches_state(adapters: dict, sid: str,
                             obs: dict[str, StepObservation]) -> None:
    """Field-by-field assert: the DOM-parsed entity map (what the compiler sees)
    must match ``read_canonical(sid)`` (the real session state). Raises
    ``AssertionError`` on mismatch — loudly fails the run (protects "live state")."""
    mismatches: list[str] = []
    for app, ad in adapters.items():
        canonical = ad.read_canonical(sid)
        canonical_entities = canonical["entities"]
        dom_entities = parse_dom_entities(obs[app].dom_html)
        dom_ids = set(dom_entities)
        canon_ids = set(canonical_entities)
        if dom_ids != canon_ids:
            mismatches.append(
                f"{app}: entity-id set mismatch. DOM={sorted(dom_ids)} "
                f"canonical={sorted(canon_ids)} "
                f"dom-only={sorted(dom_ids - canon_ids)} "
                f"canonical-only={sorted(canon_ids - dom_ids)}")
            continue
        for eid in canon_ids:
            cfields = canonical_entities[eid]
            dfields = dom_entities[eid]
            for fname, cval in cfields.items():
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
