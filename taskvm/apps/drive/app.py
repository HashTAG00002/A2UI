"""Drive app — minimal resettable Flask app (port 3015).

W2 third app. A minimal file/document store: files carry a ``parent`` folder,
``owner``, ``name``, ``content``, ``modified`` date, and ``type``. The
``move_file`` / ``rename`` / ``set_owner`` operators are the executable write
surface; ``parent`` is the field the canonical "move doc to shared folder" task
edits (single-app single-step → the W2 rollback gate's undo target).

Structurally a twin of ``apps/taskboard`` (in-memory ``user_sessions`` dict; the
canonical task graph / expected_diff live in ``benchmark/fixtures.py``
verifier-only and are NEVER sent to this app). Per-app success judgment lives in
``verifier/round_trip_checks.py``, NOT here.

Routes (reset/seed/read-canonical contract):
    GET  /health                         → {"status":"ok","site":"drive"}
    GET  /<sid>                          → drive HTML view (visible title-keyed rows)
    GET  /api/files/<sid>                → JSON file list (visible app state)
    POST /api/file/<sid>/<fid>           → move_file / rename / set_owner
    POST /api/inject_task/<sid>          → seed files from seed_state (no-leak entry)
    GET  /api/session_state/<sid>        → summary ONLY (n_files, n_by_folder) — never GT
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

SITE = "drive"
DEFAULT_PORT = 3015

# operators exposed by this app (mirrors the OPERATOR_REGISTRY in
# task_state/entity_binding.py — compiler-visible signatures only, no var_ids).
APP_OPERATORS = ("move_file", "rename", "set_owner", "set_publish_date")


def _new_session() -> dict:
    return {
        "files": [],          # [{fid, name, content, parent, owner, modified, type}]
        "task_id": None,
        "goal": "",
    }


def _find_file(sess: dict, fid: str) -> dict | None:
    for f in sess["files"]:
        if f["fid"] == fid:
            return f
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
def drive_view(sid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    return render_template("drive.html", site=SITE, sid=sid,
                           files=sess.get("files") or [],
                           task_id=sess.get("task_id"),
                           goal=sess.get("goal") or "")


@app.route("/api/files/<sid>")
def api_files(sid: str):
    sess = user_sessions.get(sid) or {}
    return jsonify({"site": SITE, "sid": sid,
                    "files": sess.get("files") or []})


@app.route("/api/file/<sid>/<fid>", methods=["POST"])
def api_file_mutate(sid: str, fid: str):
    """Executable operators on the write path (JSON API — the app's backend):
      {operator: "move_file",  value: "shared"}    → file.parent = "shared"
      {operator: "rename",     value: "new.doc"}   → file.name = "new.doc"
      {operator: "set_owner",  value: "Bo"}        → file.owner = "Bo"
    Mutates the file in the real session state; the verifier reads the real
    post-state via ``state_adapter.read_canonical`` for round-trip GT. The P1
    edit form posts here via its PRG sibling; the GUI agent (P2) drives the
    browser through the form, NOT through this route directly.

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
    res = _mutate_file(sess, fid, op, value)
    if res is None:
        return jsonify({"error": f"file {fid} not found"}), 404
    old, f = res
    return jsonify({"status": "ok", "fid": fid, "operator": op,
                    "old": old, "new": value, "file": f})


_FIELD_MAP = {"move_file": "parent", "rename": "name", "set_owner": "owner",
              "set_publish_date": "publish_date"}


def _mutate_file(sess: dict, fid: str, op: str, value) -> tuple | None:
    """Shared mutation logic (the app's own business operation). Called by BOTH
    the JSON API route and the PRG form route. Returns (old_value, file) or
    None if the file wasn't found. A rename bumps ``modified`` for realism."""
    if op not in APP_OPERATORS:
        return None
    field = _FIELD_MAP[op]
    with _sessions_lock:
        f = _find_file(sess, fid)
        if f is None:
            return None
        old = f[field]
        f[field] = value
        if op == "rename":
            f["modified"] = "2026-08-14"   # bump modified for realism (visible state)
        return old, f


# ── P1 (E10 rework): real interactive GUI for move_file/rename/set_owner ─────
@app.route("/<sid>/file/<fid>")
def file_detail(sid: str, fid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    f = _find_file(sess, fid)
    if f is None:
        return (f"file {fid} not found", 404)
    moved = request.args.get("moved")
    return render_template("file_detail.html", site=SITE, sid=sid, file=f,
                           task_id=sess.get("task_id"), goal=sess.get("goal") or "",
                           moved=moved, moved_fid=fid if moved else None,
                           moved_op=request.args.get("moved_op", ""),
                           moved_value=request.args.get("moved_value", ""))


@app.route("/<sid>/file/<fid>/edit")
def file_edit(sid: str, fid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    f = _find_file(sess, fid)
    if f is None:
        return (f"file {fid} not found", 404)
    return render_template("file_edit.html", site=SITE, sid=sid, file=f,
                           task_id=sess.get("task_id"), goal=sess.get("goal") or "")


@app.route("/<sid>/file/<fid>/mutate", methods=["POST"])
def file_mutate_prg(sid: str, fid: str):
    """PRG handler for the edit form's submit. The form posts all three operator
    fields (move_file / rename / set_owner); the server applies only the CHANGED
    ones via the shared ``_mutate_file`` helper, then redirects to the detail
    page with a toast. The GUI agent clicks '✓ Confirm edit' in the <dialog>."""
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    f = _find_file(sess, fid)
    if f is None:
        return (f"file {fid} not found", 404)
    applied = []
    for op, field in _FIELD_MAP.items():
        new_val = request.form.get(op)
        if new_val is None:
            continue
        # EE.2: f.get(field) (not f[field]) so publish_date — which may be None
        # on files with no publish date — doesn't KeyError. Treat empty string
        # and None as equivalent (unset) so an unchanged empty field isn't written
        # as "". Backward-compatible for parent/name/owner (always non-empty:
        # str(x or "") == str(x)).
        cur = f.get(field)
        if str(new_val).strip() == str(cur or "").strip():
            continue
        res = _mutate_file(sess, fid, op, new_val)
        if res is not None:
            applied.append((op, new_val))
    if not applied:
        return redirect(f"/{sid}/file/{fid}?moved=0")
    last_op, last_val = applied[-1]
    logger.info(f"[drive] PRG mutate {fid}: applied {applied}")
    return redirect(f"/{sid}/file/{fid}?moved=1&moved_op={last_op}&moved_value={last_val}")


@app.route("/api/inject_task/<sid>", methods=["POST"])
def api_inject_task(sid: str):
    """No-leak entry: seed the session's visible app state from ``seed_state``.
    Canonical task graph NEVER passed here — stays in ``benchmark/fixtures.py``."""
    data = request.get_json(silent=True) or {}
    with _sessions_lock:
        sess = _new_session()
        seed = data.get("seed_state") or {}
        sess["files"] = copy.deepcopy(seed.get("files") or [])
        sess["task_id"] = data.get("task_id")
        sess["goal"] = data.get("goal") or ""
        user_sessions[sid] = sess
        reap_sessions(user_sessions)
    return jsonify({"status": "ok", "sid": sid, "n_files": len(sess["files"])})


@app.route("/api/session_state/<sid>")
def api_session_state(sid: str):
    sess = user_sessions.get(sid) or {}
    files = sess.get("files") or []
    n_by_folder: dict[str, int] = {}
    for f in files:
        p = f.get("parent") or "(none)"
        n_by_folder[p] = n_by_folder.get(p, 0) + 1
    return jsonify(session_state_payload(
        SITE, sess, has_task=bool(sess.get("task_id")),
        summary={"n_files": len(files),
                 "n_by_folder": n_by_folder,
                 "task_id": sess.get("task_id")}))


@app.route("/api/reset/<sid>", methods=["POST"])
def api_reset(sid: str):
    with _sessions_lock:
        user_sessions.pop(sid, None)
    return jsonify({"status": "ok", "reset": True, "sid": sid})


def _seed_demo_session() -> dict:
    sess = _new_session()
    sess["task_id"] = "demo"
    sess["files"] = [
        {"fid": "F1", "name": "发布公告.doc", "content": "v1", "parent": "personal",
         "owner": "Alex", "modified": "2026-08-12", "type": "doc"},
        {"fid": "F2", "name": "设计稿.png", "content": "", "parent": "shared",
         "owner": "Bo", "modified": "2026-08-10", "type": "image"},
        {"fid": "F3", "name": "会议纪要.doc", "content": "draft", "parent": "shared",
         "owner": "Cara", "modified": "2026-08-11", "type": "doc"},
    ]
    return sess


def main(argv=None):
    parser = argparse.ArgumentParser(description="TaskVM Drive app")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    logger.info(f"Drive app on :{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
