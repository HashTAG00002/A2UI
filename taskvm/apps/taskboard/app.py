"""TaskBoard — minimal resettable Flask app (port 3014).

W1 state is in-memory. Tasks carry a ``depends_on`` list of task-variable ids
(e.g. ``["release_date"]``) — this is the dependency edge that drives effect
propagation (move release_date → tasks whose deadline depends on it must sync).
The canonical task graph lives in ``benchmark/fixtures.py`` (verifier-only).

Routes (reset/seed/read-canonical contract; per-app success judgment lives in
``verifier/round_trip_checks.py``, NOT in this app):
    GET  /health                         → {"status":"ok","site":"taskboard"}
    GET  /<sid>                          → taskboard HTML view (data-task-id DOM)
    GET  /api/tasks/<sid>                → JSON task list (visible app state)
    POST /api/task/<sid>/<tid>           → set_deadline / set_status / set_assignee
    POST /api/inject_task/<sid>          → seed tasks from seed_state (no-leak entry)
    GET  /api/session_state/<sid>        → summary ONLY (n_tasks, n_done) — never GT
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

SITE = "taskboard"
DEFAULT_PORT = 3014

# operators exposed by this app (mirrors the OPERATOR_REGISTRY in
# task_state/entity_binding.py — compiler-visible signatures only, no var_ids).
APP_OPERATORS = ("set_deadline", "set_status", "set_assignee")


def _new_session() -> dict:
    return {
        "tasks": [],          # [{tid, title, status, assignee, deadline, depends_on: []}]
        "task_id": None,
        "goal": "",
    }


def _find_task(sess: dict, tid: str) -> dict | None:
    for t in sess["tasks"]:
        if t["tid"] == tid:
            return t
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
def taskboard_view(sid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    return render_template("taskboard.html", site=SITE, sid=sid,
                           tasks=sess.get("tasks") or [],
                           task_id=sess.get("task_id"),
                           goal=sess.get("goal") or "")


@app.route("/api/tasks/<sid>")
def api_tasks(sid: str):
    sess = user_sessions.get(sid) or {}
    return jsonify({"site": SITE, "sid": sid,
                    "tasks": sess.get("tasks") or []})


@app.route("/api/task/<sid>/<tid>", methods=["POST"])
def api_task_mutate(sid: str, tid: str):
    """Executable operators on the write path:
      {operator: "set_deadline",  value: "2026-08-18"}
      {operator: "set_status",    value: "done"}
      {operator: "set_assignee",  value: "Alex"}
    Mutates the task in the real session state; the verifier reads the real
    post-state via ``state_adapter.read_canonical`` for round-trip GT."""
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
    field_map = {"set_deadline": "deadline", "set_status": "status",
                 "set_assignee": "assignee"}
    field = field_map[op]
    with _sessions_lock:
        t = _find_task(sess, tid)
        if t is None:
            return jsonify({"error": f"task {tid} not found"}), 404
        old = t[field]
        t[field] = value
    return jsonify({"status": "ok", "tid": tid, "operator": op,
                    "old": old, "new": value, "task": t})


@app.route("/api/inject_task/<sid>", methods=["POST"])
def api_inject_task(sid: str):
    """No-leak entry: seed the session's visible app state from ``seed_state``.
    Canonical task graph NEVER passed here — stays in ``benchmark/fixtures.py``."""
    data = request.get_json(silent=True) or {}
    with _sessions_lock:
        sess = _new_session()
        seed = data.get("seed_state") or {}
        sess["tasks"] = copy.deepcopy(seed.get("tasks") or [])
        sess["task_id"] = data.get("task_id")
        sess["goal"] = data.get("goal") or ""
        user_sessions[sid] = sess
        reap_sessions(user_sessions)
    return jsonify({"status": "ok", "sid": sid, "n_tasks": len(sess["tasks"])})


@app.route("/api/session_state/<sid>")
def api_session_state(sid: str):
    sess = user_sessions.get(sid) or {}
    tasks = sess.get("tasks") or []
    return jsonify(session_state_payload(
        SITE, sess, has_task=bool(sess.get("task_id")),
        summary={"n_tasks": len(tasks),
                 "n_done": sum(1 for t in tasks if t.get("status") == "done"),
                 "task_id": sess.get("task_id")}))


@app.route("/api/reset/<sid>", methods=["POST"])
def api_reset(sid: str):
    with _sessions_lock:
        user_sessions.pop(sid, None)
    return jsonify({"status": "ok", "reset": True, "sid": sid})


def _seed_demo_session() -> dict:
    sess = _new_session()
    sess["task_id"] = "demo"
    sess["tasks"] = [
        {"tid": "T1", "title": "最终检查演示文档", "status": "todo",
         "assignee": "Alex", "deadline": "2026-08-14", "depends_on": ["release_date"]},
        {"tid": "T2", "title": "确认发布公告", "status": "todo",
         "assignee": "Bo", "deadline": "2026-08-14", "depends_on": ["release_date"]},
        {"tid": "T3", "title": "整理会议纪要", "status": "done",
         "assignee": "Cara", "deadline": "2026-08-10", "depends_on": []},
    ]
    return sess


def main(argv=None):
    parser = argparse.ArgumentParser(description="TaskVM TaskBoard app")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    logger.info(f"TaskBoard app on :{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
