"""Calendar — minimal resettable Flask app (port 3013).

The ecosystem's shared time axis. W1 state is in-memory (``user_sessions``);
the canonical task graph / expected_diff live in ``benchmark/fixtures.py``
(verifier-only) and are NEVER sent to this app — the app holds only the visible
app state (events). The compiler reads the rendered GUI (DOM/a11y/screenshot);
the executor writes via ``POST /api/event/<sid>/move``.

Routes (reset/seed/read-canonical contract; per-app success judgment lives in
``verifier/round_trip_checks.py``, NOT in this app):
    GET  /health                         → {"status":"ok","site":"calendar"}
    GET  /<sid>                          → calendar HTML view (data-event-id DOM)
    GET  /api/events/<sid>               → JSON event list (visible app state)
    POST /api/event/<sid>/move           → move_event(eid, new_date) — mutate
    POST /api/inject_task/<sid>          → seed events from seed_state (no-leak entry)
    GET  /api/session_state/<sid>        → summary ONLY (n_events, has_task) — never GT
    POST /api/reset/<sid>                → drop session
"""
from __future__ import annotations

import argparse
import copy
import logging
import threading
from pathlib import Path

from flask import (Flask, jsonify, redirect, render_template, request,
                   url_for)

from taskvm._shim.web_helpers import reap_sessions, session_state_payload

_SCENARIO_DIR = Path(__file__).parent
logger = logging.getLogger(__name__)

app = Flask(__name__,
            template_folder=str(_SCENARIO_DIR / "templates"),
            static_folder=str(_SCENARIO_DIR / "static"))

user_sessions: dict = {}
_sessions_lock = threading.RLock()

SITE = "calendar"
DEFAULT_PORT = 3013


def _new_session() -> dict:
    """A fresh empty session. ``events`` is the visible app state (the only
    thing the verifier reads via ``state_adapter.read_canonical``). No canonical
    GT ever lives here — that's in ``benchmark/fixtures.py``."""
    return {
        "events": [],          # [{eid, title, date, time, calendar, rsvp}]
        "task_id": None,
        "goal": "",
    }


def _find_event(sess: dict, eid: str) -> dict | None:
    for e in sess["events"]:
        if e["eid"] == eid:
            return e
    return None


# ── routes ───────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "site": SITE})


@app.route("/")
def _root():
    return redirect(url_for("_demo"))


@app.route("/demo")
def _demo():
    """A demo sid so a human can open the app in a browser without the harness."""
    with _sessions_lock:
        sid = "demo"
        if sid not in user_sessions:
            user_sessions[sid] = _seed_demo_session()
    return redirect(f"/{sid}")


@app.route("/<sid>")
def calendar_view(sid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    return render_template("calendar.html", site=SITE, sid=sid,
                           events=sess.get("events") or [],
                           task_id=sess.get("task_id"),
                           goal=sess.get("goal") or "")


@app.route("/api/events/<sid>")
def api_events(sid: str):
    sess = user_sessions.get(sid) or {}
    return jsonify({"site": SITE, "sid": sid,
                    "events": sess.get("events") or []})


def _move_event(sess: dict, eid: str, new_date: str) -> tuple[str, dict] | None:
    """The shared move_event logic (the app's own business operation). Called by
    BOTH the JSON API route (``/api/event/<sid>/move``) AND the PRG form route
    (``/<sid>/event/<eid>/move``) so the GUI form and the adapter hit the same
    backend mutation. Returns ``(old_date, event)`` or None if the event wasn't
    found. Holds ``_sessions_lock`` for the dict mutation."""
    with _sessions_lock:
        ev = _find_event(sess, eid)
        if ev is None:
            return None
        old_date = ev["date"]
        ev["date"] = new_date
        return old_date, ev


@app.route("/api/event/<sid>/move", methods=["POST"])
def api_event_move(sid: str):
    """move_event(eid, new_date): the executable operator on the write path
    (JSON API — the app's own backend). The P1 edit form posts here via its PRG
    sibling route; the GUI agent (P2) drives the browser through the form, NOT
    through this route directly. Mutates the event date in the real session
    state; the verifier reads the real post-state via
    ``state_adapter.read_canonical`` for round-trip GT."""
    sess = user_sessions.get(sid)
    if sess is None:
        return jsonify({"error": "session not found", "sid": sid}), 404
    data = request.get_json(silent=True) or {}
    eid = data.get("eid")
    new_date = data.get("new_date")
    if not eid or not new_date:
        return jsonify({"error": "eid and new_date required"}), 400
    res = _move_event(sess, eid, new_date)
    if res is None:
        return jsonify({"error": f"event {eid} not found"}), 404
    old_date, ev = res
    return jsonify({"status": "ok", "eid": eid, "old_date": old_date,
                    "new_date": new_date, "event": ev})


# ── P1 (E10 rework): real interactive GUI for move_event ──────────────────────
# List (/<sid>) → detail (/<sid>/event/<eid>) → edit form (/<sid>/event/<eid>/edit)
# → confirm <dialog> → PRG POST (/<sid>/event/<eid>/move) → redirect to detail
# with a toast. The inline-JS-fetch debug button is gone; the GUI agent (P2)
# drives this real interaction hierarchy through the browser.


@app.route("/<sid>/event/<eid>")
def event_detail(sid: str, eid: str):
    """Detail view for one event — all fields shown, with an Edit affordance
    (link to the edit form). The list row's "View" link lands here."""
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    ev = _find_event(sess, eid)
    if ev is None:
        return (f"event {eid} not found", 404)
    moved = request.args.get("moved")
    moved_date = request.args.get("moved_date", "")
    return render_template("event_detail.html", site=SITE, sid=sid,
                           event=ev, task_id=sess.get("task_id"),
                           goal=sess.get("goal") or "",
                           moved=moved, moved_eid=eid if moved else None,
                           moved_date=moved_date)


@app.route("/<sid>/event/<eid>/edit")
def event_edit(sid: str, eid: str):
    """The edit form — a real <form> with <input type="date"> for the date (the
    only operator-writable field; move_event maps to field 'date'). Other fields
    are read-only context. A "Review changes" button opens a native <dialog>
    confirm modal; "Confirm move" submits the form via the PRG route below."""
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    ev = _find_event(sess, eid)
    if ev is None:
        return (f"event {eid} not found", 404)
    return render_template("event_edit.html", site=SITE, sid=sid, event=ev,
                           task_id=sess.get("task_id"),
                           goal=sess.get("goal") or "")


@app.route("/<sid>/event/<eid>/move", methods=["POST"])
def event_move_prg(sid: str, eid: str):
    """PRG (Post-Redirect-Get) handler for the edit form's submit. Processes the
    move via the shared ``_move_event`` helper (same backend mutation as the
    JSON API), then redirects to the detail page with a toast query-param. This
    is the form's action target — a real form submission, not a JS fetch. The
    GUI agent clicks "Confirm move" in the <dialog>, the browser POSTs here, the
    server mutates state + redirects to the detail view."""
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    new_date = request.form.get("new_date")
    if not new_date:
        return ("new_date required", 400)
    res = _move_event(sess, eid, new_date)
    if res is None:
        return (f"event {eid} not found", 404)
    logger.info(f"[calendar] PRG move {eid}: → {new_date} (sid={sid})")
    return redirect(f"/{sid}/event/{eid}?moved=1&moved_date={new_date}")


@app.route("/api/inject_task/<sid>", methods=["POST"])
def api_inject_task(sid: str):
    """No-leak entry: seed the session's visible app state from ``seed_state``.
    The canonical task graph (expected_diff / bindings / non_interference_set)
    is NEVER passed here — it stays in ``benchmark/fixtures.py`` and goes
    directly to the verifier."""
    data = request.get_json(silent=True) or {}
    with _sessions_lock:
        sess = _new_session()
        seed = data.get("seed_state") or {}
        # deep-copy so the session owns its state (no aliasing the fixture)
        sess["events"] = copy.deepcopy(seed.get("events") or [])
        sess["task_id"] = data.get("task_id")
        sess["goal"] = data.get("goal") or ""
        user_sessions[sid] = sess
        reap_sessions(user_sessions)
    return jsonify({"status": "ok", "sid": sid, "n_events": len(sess["events"])})


@app.route("/api/session_state/<sid>")
def api_session_state(sid: str):
    """Summary ONLY — never the canonical GT. The events themselves are visible
    app state (rendered in the GUI), but no oracle/expected_diff is returned."""
    sess = user_sessions.get(sid) or {}
    return jsonify(session_state_payload(
        SITE, sess, has_task=bool(sess.get("task_id")),
        summary={"n_events": len(sess.get("events") or []),
                 "task_id": sess.get("task_id")}))


@app.route("/api/reset/<sid>", methods=["POST"])
def api_reset(sid: str):
    with _sessions_lock:
        user_sessions.pop(sid, None)
    return jsonify({"status": "ok", "reset": True, "sid": sid})


def _seed_demo_session() -> dict:
    """A small static demo so the app is presentable at /demo without the harness."""
    sess = _new_session()
    sess["task_id"] = "demo"
    sess["events"] = [
        {"eid": "E1", "title": "项目发布会议", "date": "2026-08-14",
         "time": "14:00-15:00", "calendar": "work", "rsvp": "accepted"},
        {"eid": "E2", "title": "周会", "date": "2026-08-12",
         "time": "10:00-10:30", "calendar": "work", "rsvp": "accepted"},
        {"eid": "E7", "title": " dentist", "date": "2026-08-13",
         "time": "09:00-09:30", "calendar": "personal", "rsvp": "accepted"},
    ]
    return sess


def main(argv=None):
    parser = argparse.ArgumentParser(description="TaskVM Calendar app")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    logger.info(f"Calendar app on :{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
