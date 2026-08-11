"""Outlook Calendar — minimal resettable Flask app (port 3018).

W4 held-out **reskin** of ``apps/calendar``. Same conceptual semantics (move a
meeting to a new date) but a renamed substrate the model has never seen in
training-on-this-bench:
  - entity kind = ``appointment`` (not ``event``)
  - id field / DOM attribute = ``aid`` / ``data-appointment-id`` (not eid)
  - the load-bearing field = ``scheduled_for`` (not ``date``)
  - the operator = ``reschedule_appointment`` (not ``move_event``)
  - the mutate route = ``/api/appointment/<sid>/reschedule`` with body
    ``{aid, new_scheduled_for}`` (not ``/api/event/<sid>/move`` with ``{eid, new_date}``)
  - RSVP renamed to ``response`` (kept the same values)

The other fields (subject, time, calendar) are kept familiar so the reskin is
*moderate* — realistic for "same app, different vendor skin". This is the
substrate-independence probe: does the compiler discover
``release_date → outlook_cal.A1.scheduled_for via reschedule_appointment``
under the new skin, and does the same user operation produce a stable interface
+ consistent task semantics across calendar (Stack A) and outlook_cal (Stack B)?

Routes (reset/seed/read-canonical contract; per-app success judgment lives in
``verifier/round_trip_checks.py``, NOT here):
    GET  /health                         → {"status":"ok","site":"outlook_cal"}
    GET  /<sid>                          → outlook_cal HTML view (data-appointment-id DOM)
    GET  /api/appointments/<sid>         → JSON appointment list (visible app state)
    POST /api/appointment/<sid>/reschedule → reschedule_appointment(aid, new_scheduled_for)
    POST /api/inject_task/<sid>          → seed appointments from seed_state (no-leak entry)
    GET  /api/session_state/<sid>        → summary ONLY (n_appointments, has_task) — never GT
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

SITE = "outlook_cal"
DEFAULT_PORT = 3018

APP_OPERATORS = ("reschedule_appointment",)


def _new_session() -> dict:
    return {
        "appointments": [],   # [{aid, subject, scheduled_for, time, calendar, response}]
        "task_id": None,
        "goal": "",
    }


def _find_appt(sess: dict, aid: str) -> dict | None:
    for a in sess["appointments"]:
        if a["aid"] == aid:
            return a
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
    sid = "demo"
    with _sessions_lock:
        if sid not in user_sessions:
            user_sessions[sid] = _seed_demo_session()
    return redirect(f"/{sid}")


@app.route("/<sid>")
def outlook_cal_view(sid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    return render_template("outlook_cal.html", site=SITE, sid=sid,
                           appointments=sess.get("appointments") or [],
                           task_id=sess.get("task_id"),
                           goal=sess.get("goal") or "")


@app.route("/api/appointments/<sid>")
def api_appointments(sid: str):
    sess = user_sessions.get(sid) or {}
    return jsonify({"site": SITE, "sid": sid,
                    "appointments": sess.get("appointments") or []})


@app.route("/api/appointment/<sid>/reschedule", methods=["POST"])
def api_appointment_reschedule(sid: str):
    """reschedule_appointment(aid, new_scheduled_for): the executable operator on
    the write path. Mutates the appointment's scheduled_for in the real session
    state; the verifier reads the real post-state via ``read_canonical``.

    Returns ``old_scheduled_for`` / ``new_scheduled_for`` (calendar-style
    old_date/new_date keys, adapted) so the rollback skeleton can compensate
    (the before-value is visible app state, never GT)."""
    sess = user_sessions.get(sid)
    if sess is None:
        return jsonify({"error": "session not found", "sid": sid}), 404
    data = request.get_json(silent=True) or {}
    aid = data.get("aid")
    new_scheduled_for = data.get("new_scheduled_for")
    if not aid or not new_scheduled_for:
        return jsonify({"error": "aid and new_scheduled_for required"}), 400
    res = _reschedule_appt(sess, aid, new_scheduled_for)
    if res is None:
        return jsonify({"error": f"appointment {aid} not found"}), 404
    old, appt = res
    # alias old/new keys so the rollback _extract_before_after helper works
    # uniformly (it reads old/new, then falls back to old_date/new_date).
    return jsonify({"status": "ok", "aid": aid,
                    "old": old, "new": new_scheduled_for,
                    "old_scheduled_for": old, "new_scheduled_for": new_scheduled_for,
                    "appointment": appt})


def _reschedule_appt(sess: dict, aid: str, new_scheduled_for: str) -> tuple | None:
    """Shared reschedule logic (the app's own business operation). Called by BOTH
    the JSON API route and the PRG form route. Returns (old, appointment)."""
    with _sessions_lock:
        appt = _find_appt(sess, aid)
        if appt is None:
            return None
        old = appt["scheduled_for"]
        appt["scheduled_for"] = new_scheduled_for
        return old, appt


# ── P1 (E10 rework): real interactive GUI for reschedule_appointment ─────────
@app.route("/<sid>/appointment/<aid>")
def appointment_detail(sid: str, aid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    appt = _find_appt(sess, aid)
    if appt is None:
        return (f"appointment {aid} not found", 404)
    moved = request.args.get("moved")
    return render_template("appointment_detail.html", site=SITE, sid=sid,
                           appointment=appt, task_id=sess.get("task_id"),
                           goal=sess.get("goal") or "",
                           moved=moved, moved_aid=aid if moved else None,
                           moved_value=request.args.get("moved_value", ""))


@app.route("/<sid>/appointment/<aid>/edit")
def appointment_edit(sid: str, aid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    appt = _find_appt(sess, aid)
    if appt is None:
        return (f"appointment {aid} not found", 404)
    return render_template("appointment_edit.html", site=SITE, sid=sid,
                           appointment=appt, task_id=sess.get("task_id"),
                           goal=sess.get("goal") or "")


@app.route("/<sid>/appointment/<aid>/reschedule", methods=["POST"])
def appointment_reschedule_prg(sid: str, aid: str):
    """PRG handler for the edit form's submit. Processes the reschedule via the
    shared ``_reschedule_appt`` helper, then redirects to the detail page."""
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    new_scheduled_for = request.form.get("new_scheduled_for")
    if not new_scheduled_for:
        return ("new_scheduled_for required", 400)
    res = _reschedule_appt(sess, aid, new_scheduled_for)
    if res is None:
        return (f"appointment {aid} not found", 404)
    logger.info(f"[outlook_cal] PRG reschedule {aid}: → {new_scheduled_for} (sid={sid})")
    return redirect(f"/{sid}/appointment/{aid}?moved=1&moved_value={new_scheduled_for}")


@app.route("/api/inject_task/<sid>", methods=["POST"])
def api_inject_task(sid: str):
    """No-leak entry: seed the session's visible app state from ``seed_state``.
    Canonical task graph NEVER passed here — stays in ``benchmark/ood_fixtures.py``."""
    data = request.get_json(silent=True) or {}
    with _sessions_lock:
        sess = _new_session()
        seed = data.get("seed_state") or {}
        sess["appointments"] = copy.deepcopy(seed.get("appointments") or [])
        sess["task_id"] = data.get("task_id")
        sess["goal"] = data.get("goal") or ""
        user_sessions[sid] = sess
        reap_sessions(user_sessions)
    return jsonify({"status": "ok", "sid": sid,
                    "n_appointments": len(sess["appointments"])})


@app.route("/api/session_state/<sid>")
def api_session_state(sid: str):
    sess = user_sessions.get(sid) or {}
    appts = sess.get("appointments") or []
    return jsonify(session_state_payload(
        SITE, sess, has_task=bool(sess.get("task_id")),
        summary={"n_appointments": len(appts),
                 "task_id": sess.get("task_id")}))


@app.route("/api/reset/<sid>", methods=["POST"])
def api_reset(sid: str):
    with _sessions_lock:
        user_sessions.pop(sid, None)
    return jsonify({"status": "ok", "reset": True, "sid": sid})


def _seed_demo_session() -> dict:
    sess = _new_session()
    sess["task_id"] = "demo"
    sess["appointments"] = [
        {"aid": "A1", "subject": "项目发布会议", "scheduled_for": "2026-08-14",
         "time": "14:00-15:00", "calendar": "work", "response": "accepted"},
        {"aid": "A2", "subject": "周会", "scheduled_for": "2026-08-12",
         "time": "10:00-10:30", "calendar": "work", "response": "accepted"},
    ]
    return sess


def main(argv=None):
    parser = argparse.ArgumentParser(description="TaskVM Outlook Calendar app (held-out reskin)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    logger.info(f"Outlook Calendar app on :{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
