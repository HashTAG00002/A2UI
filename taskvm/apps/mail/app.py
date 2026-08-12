"""Mail app — minimal resettable Flask app (port 3017).

W4 held-out **truly-unseen** app. A minimal mailbox: messages carry a ``mid``,
``subject``, ``from_addr``, ``to_addr``, ``state`` (draft/sent/scheduled),
``received`` date, and ``priority`` (high/normal/low). The ``set_state`` /
``set_priority`` / ``set_to`` operators are the executable write surface; the
canonical "send the scheduled announcement" OOD task edits ``state``.

**Why this is a held-out OOD app (not a reskin)**: the entity shape (a message
with a lifecycle *state* field), the operator semantics (``set_state`` mutates a
finite-state machine, not a scalar like a date/folder), and the DOM id attribute
(``data-mail-id``) are all genuinely new. The A2UI compiler system prompt
(``benchmark/a2ui_spec.py``) names ONLY calendar/taskboard/drive id attributes —
mail is never mentioned — so the model must generalize from the supplied
tool-schema + valid-ids + DOM, exactly the OOD generalization the W4 kill-test
measures.

Structurally a twin of ``apps/drive`` (in-memory ``user_sessions`` dict; the
canonical task graph / expected_diff live in ``benchmark/ood_fixtures.py``
verifier-only and are NEVER sent to this app). Per-app success judgment lives in
``verifier/round_trip_checks.py``, NOT here.

Routes (reset/seed/read-canonical contract):
    GET  /health                         → {"status":"ok","site":"mail"}
    GET  /<sid>                          → mail HTML view (data-mail-id DOM)
    GET  /api/messages/<sid>             → JSON message list (visible app state)
    POST /api/mail/<sid>/<mid>           → set_state / set_priority / set_to
    POST /api/inject_task/<sid>          → seed messages from seed_state (no-leak entry)
    GET  /api/session_state/<sid>        → summary ONLY (n_messages, n_by_state) — never GT
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

SITE = "mail"
DEFAULT_PORT = 3017

# operators exposed by this app (mirrors the OPERATOR_REGISTRY in
# task_state/entity_binding.py — compiler-visible signatures only, no var_ids).
APP_OPERATORS = ("set_state", "set_priority", "set_to", "set_send_date")


def _new_session() -> dict:
    return {
        "messages": [],      # [{mid, subject, from_addr, to_addr, state, received, priority}]
        "task_id": None,
        "goal": "",
    }


def _find_msg(sess: dict, mid: str) -> dict | None:
    for m in sess["messages"]:
        if m["mid"] == mid:
            return m
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
def mail_view(sid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    return render_template("mail.html", site=SITE, sid=sid,
                           messages=sess.get("messages") or [],
                           task_id=sess.get("task_id"),
                           goal=sess.get("goal") or "")


@app.route("/api/messages/<sid>")
def api_messages(sid: str):
    sess = user_sessions.get(sid) or {}
    return jsonify({"site": SITE, "sid": sid,
                    "messages": sess.get("messages") or []})


@app.route("/api/mail/<sid>/<mid>", methods=["POST"])
def api_mail_mutate(sid: str, mid: str):
    """Executable operators on the write path:
      {operator: "set_state",     value: "sent"}      → message.state = "sent"
      {operator: "set_priority",  value: "high"}      → message.priority = "high"
      {operator: "set_to",        value: "team@x.com"}→ message.to_addr = "team@x.com"
    Mutates the message in the real session state; the verifier reads the real
    post-state via ``state_adapter.read_canonical`` for round-trip GT.

    Returns ``old`` (the before-value of the changed field) so the rollback
    skeleton can compensate without a separate pre-snapshot (the before-value is
    visible app state, never GT)."""
    sess = user_sessions.get(sid)
    if sess is None:
        return jsonify({"error": "session not found", "sid": sid}), 404
    data = request.get_json(silent=True) or {}
    op = data.get("operator")
    value = data.get("value")
    if op not in APP_OPERATORS:
        return jsonify({"error": f"operator must be one of {APP_OPERATORS}"}), 400
    if value is None:
        return jsonify({"error": "value required"}), 400
    res = _mutate_message(sess, mid, op, value)
    if res is None:
        return jsonify({"error": f"message {mid} not found"}), 404
    old, m = res
    return jsonify({"status": "ok", "mid": mid, "operator": op,
                    "old": old, "new": value, "message": m})


_FIELD_MAP = {"set_state": "state", "set_priority": "priority", "set_to": "to_addr",
              "set_send_date": "send_date"}


def _mutate_message(sess: dict, mid: str, op: str, value) -> tuple | None:
    """Shared mutation logic (the app's own business operation). Called by BOTH
    the JSON API route and the PRG form route. Returns (old_value, message)."""
    if op not in APP_OPERATORS:
        return None
    field = _FIELD_MAP[op]
    with _sessions_lock:
        m = _find_msg(sess, mid)
        if m is None:
            return None
        old = m[field]
        m[field] = value
        return old, m


# ── P1 (E10 rework): real interactive GUI for set_state/set_priority/set_to ──
@app.route("/<sid>/message/<mid>")
def message_detail(sid: str, mid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    m = _find_msg(sess, mid)
    if m is None:
        return (f"message {mid} not found", 404)
    moved = request.args.get("moved")
    return render_template("message_detail.html", site=SITE, sid=sid, message=m,
                           task_id=sess.get("task_id"), goal=sess.get("goal") or "",
                           moved=moved, moved_mid=mid if moved else None,
                           moved_op=request.args.get("moved_op", ""),
                           moved_value=request.args.get("moved_value", ""))


@app.route("/<sid>/message/<mid>/edit")
def message_edit(sid: str, mid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    m = _find_msg(sess, mid)
    if m is None:
        return (f"message {mid} not found", 404)
    return render_template("message_edit.html", site=SITE, sid=sid, message=m,
                           task_id=sess.get("task_id"), goal=sess.get("goal") or "")


@app.route("/<sid>/message/<mid>/mutate", methods=["POST"])
def message_mutate_prg(sid: str, mid: str):
    """PRG handler for the edit form's submit. Applies only the CHANGED operator
    fields via ``_mutate_message``, then redirects to the detail page."""
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    m = _find_msg(sess, mid)
    if m is None:
        return (f"message {mid} not found", 404)
    applied = []
    for op, field in _FIELD_MAP.items():
        new_val = request.form.get(op)
        if new_val is None:
            continue
        # EE.2: m.get(field) (not m[field]) so send_date — which may be None on
        # messages with no scheduled send — doesn't KeyError. Empty==None (unset)
        # so an unchanged empty field isn't written as "".
        cur = m.get(field)
        if str(new_val).strip() == str(cur or "").strip():
            continue
        res = _mutate_message(sess, mid, op, new_val)
        if res is not None:
            applied.append((op, new_val))
    if not applied:
        return redirect(f"/{sid}/message/{mid}?moved=0")
    last_op, last_val = applied[-1]
    logger.info(f"[mail] PRG mutate {mid}: applied {applied}")
    return redirect(f"/{sid}/message/{mid}?moved=1&moved_op={last_op}&moved_value={last_val}")


@app.route("/api/inject_task/<sid>", methods=["POST"])
def api_inject_task(sid: str):
    """No-leak entry: seed the session's visible app state from ``seed_state``.
    Canonical task graph NEVER passed here — stays in ``benchmark/ood_fixtures.py``."""
    data = request.get_json(silent=True) or {}
    with _sessions_lock:
        sess = _new_session()
        seed = data.get("seed_state") or {}
        sess["messages"] = copy.deepcopy(seed.get("messages") or [])
        sess["task_id"] = data.get("task_id")
        sess["goal"] = data.get("goal") or ""
        user_sessions[sid] = sess
        reap_sessions(user_sessions)
    return jsonify({"status": "ok", "sid": sid, "n_messages": len(sess["messages"])})


@app.route("/api/session_state/<sid>")
def api_session_state(sid: str):
    sess = user_sessions.get(sid) or {}
    msgs = sess.get("messages") or []
    n_by_state: dict[str, int] = {}
    for m in msgs:
        s = m.get("state") or "(none)"
        n_by_state[s] = n_by_state.get(s, 0) + 1
    return jsonify(session_state_payload(
        SITE, sess, has_task=bool(sess.get("task_id")),
        summary={"n_messages": len(msgs),
                 "n_by_state": n_by_state,
                 "task_id": sess.get("task_id")}))


@app.route("/api/reset/<sid>", methods=["POST"])
def api_reset(sid: str):
    with _sessions_lock:
        user_sessions.pop(sid, None)
    return jsonify({"status": "ok", "reset": True, "sid": sid})


def _seed_demo_session() -> dict:
    sess = _new_session()
    sess["task_id"] = "demo"
    sess["messages"] = [
        {"mid": "M1", "subject": "项目发布公告", "from_addr": "pm@x.com",
         "to_addr": "team@x.com", "state": "scheduled", "received": "2026-08-12",
         "priority": "high"},
        {"mid": "M2", "subject": "周报", "from_addr": "bo@x.com",
         "to_addr": "team@x.com", "state": "draft", "received": "2026-08-11",
         "priority": "normal"},
    ]
    return sess


def main(argv=None):
    parser = argparse.ArgumentParser(description="TaskVM Mail app (held-out)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    logger.info(f"Mail app on :{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
