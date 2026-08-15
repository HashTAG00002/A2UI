"""taskvm.projection.app — Flask factory for the TaskVM Projection UI
(contract §6: route matrix; §3: 0 model calls on read paths; §5: no
substrate import).

Route matrix (frozen contract §6.1):
    GET  /api/sessions                          list sessions
    GET  /api/sessions/<sid>/snapshot           full snapshot bundle
    GET  /api/sessions/<sid>/governance         governance bar
    GET  /api/sessions/<sid>/variables          task variables
    GET  /api/sessions/<sid>/workflow           workflow map
    GET  /api/sessions/<sid>/checkpoints        checkpoint timeline
    GET  /api/sessions/<sid>/surfaces           surface cards
    GET  /api/sessions/<sid>/conflicts          open conflicts
    GET  /api/sessions/<sid>/events              event log (paginated)
    GET  /api/sessions/<sid>/sse                 SSE stream (live deltas)
    GET  /api/sessions/<sid>/artifacts/<ref>    artifact bytes (read-only)
    POST /api/sessions/<sid>/governance/pause    autonomy control
    POST /api/sessions/<sid>/governance/resume
    POST /api/sessions/<sid>/governance/stop
    POST /api/sessions/<sid>/governance/local_patch
    POST /api/sessions/<sid>/governance/goal_patch
    POST /api/sessions/<sid>/governance/checkpoint
    POST /api/sessions/<sid>/governance/rollback
    POST /api/sessions/<sid>/governance/resolve_conflict
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Callable

from flask import Flask, Response, jsonify, request, send_file
from io import BytesIO

from taskvm.domain.events import Event, EventKind
from taskvm.projection.events import format_sse, sse_envelope
from taskvm.projection.store import (
    ArtifactStore, ProjectionSession, ProjectionSessionStore, SurfaceDecl,
)
from taskvm.projection.view_models import (
    checkpoint_view, conflicts_view, governance_view, snapshot_view,
    surface_cards, variables_view, workflow_view,
)


def create_app(store: ProjectionSessionStore,
               *,
               static_folder: str | None = None,
               static_url_path: str = "/static",
               ) -> Flask:
    """Build the Flask app. ``store`` is the composition-registered
    session store. ``static_folder`` optionally serves the frontend
    assets (workspace_ui)."""
    app = Flask(__name__,
                static_folder=static_folder,
                static_url_path=static_url_path)
    app.config["PROJECTION_STORE"] = store

    # ── SSE subscriber registry ──────────────────────────────────────────
    _subscribers: dict[str, list[queue.Queue[dict[str, Any]]]] = {}
    _sub_lock = threading.Lock()

    def _notify(sid: str, envelope: dict[str, Any]) -> None:
        with _sub_lock:
            for q in _subscribers.get(sid, []):
                try:
                    q.put_nowait(envelope)
                except queue.Full:
                    pass  # drop on slow consumer; SSE is best-effort

    def _subscribe(sid: str) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
        with _sub_lock:
            _subscribers.setdefault(sid, []).append(q)
        return q

    def _unsubscribe(sid: str, q: queue.Queue) -> None:
        with _sub_lock:
            if sid in _subscribers:
                _subscribers[sid] = [x for x in _subscribers[sid] if x is not q]
                if not _subscribers[sid]:
                    del _subscribers[sid]

    # ── helpers ──────────────────────────────────────────────────────────

    def _get_session(sid: str) -> ProjectionSession:
        sess = store.get(sid)
        if sess is None:
            from flask import abort
            abort(404, description=f"session {sid!r} not found")
        return sess

    def _gov(sess: ProjectionSession):
        return sess.governance_port()

    def _json_response(data: Any) -> Response:
        return jsonify(_jsonable(data))

    def _jsonable(value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (list, tuple)):
            return [_jsonable(v) for v in value]
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        return str(value)

    # ── read routes ───────────────────────────────────────────────────────

    @app.route("/api/sessions")
    def list_sessions() -> Response:
        return jsonify({"sessions": store.sids()})

    @app.route("/api/sessions/<sid>/snapshot")
    def get_snapshot(sid: str) -> Response:
        sess = _get_session(sid)
        return _json_response(snapshot_view(sess))

    @app.route("/api/sessions/<sid>/governance")
    def get_governance(sid: str) -> Response:
        sess = _get_session(sid)
        return _json_response(governance_view(sess))

    @app.route("/api/sessions/<sid>/variables")
    def get_variables(sid: str) -> Response:
        sess = _get_session(sid)
        return _json_response(variables_view(sess.kernel))

    @app.route("/api/sessions/<sid>/workflow")
    def get_workflow(sid: str) -> Response:
        sess = _get_session(sid)
        events = sess.kernel.events()
        wf = sess.kernel.workflow()
        return _json_response(workflow_view(wf, events))

    @app.route("/api/sessions/<sid>/checkpoints")
    def get_checkpoints(sid: str) -> Response:
        sess = _get_session(sid)
        return _json_response(
            checkpoint_view(sess.kernel.checkpoints()))

    @app.route("/api/sessions/<sid>/surfaces")
    def get_surfaces(sid: str) -> Response:
        sess = _get_session(sid)
        rt_events = (sess.runtime.runtime_events()
                     if sess.runtime is not None else ())
        return _json_response(surface_cards(sess, rt_events))

    @app.route("/api/sessions/<sid>/conflicts")
    def get_conflicts(sid: str) -> Response:
        sess = _get_session(sid)
        return _json_response(conflicts_view(sess.kernel))

    @app.route("/api/sessions/<sid>/events")
    def get_events(sid: str) -> Response:
        sess = _get_session(sid)
        offset = int(request.args.get("offset", 0))
        limit = int(request.args.get("limit", 100))
        all_events = sess.kernel.events()
        chunk = all_events[offset:offset + limit]
        envelopes = [sse_envelope(e) for e in chunk]
        return _json_response({"events": envelopes, "total": len(all_events),
                               "offset": offset, "limit": limit})

    @app.route("/api/sessions/<sid>/artifacts/<path:ref>")
    def get_artifact(sid: str, ref: str) -> Response:
        sess = _get_session(sid)
        art = sess.artifacts.get(ref)
        if art is None:
            from flask import abort
            abort(404, description=f"artifact {ref!r} not found")
        return send_file(BytesIO(art.data),
                         mimetype=art.mime,
                         as_attachment=False,
                         download_name=f"{ref}.png")

    # ── SSE stream ───────────────────────────────────────────────────────

    @app.route("/api/sessions/<sid>/sse")
    def sse_stream(sid: str) -> Response:
        sess = _get_session(sid)

        def _generate():
            q = _subscribe(sid)
            # send an initial snapshot so the client has a starting point
            try:
                snap = snapshot_view(sess)
                yield f"data: {json.dumps({'sse_type': 'snapshot', 'detail': snap}, ensure_ascii=False)}\n\n"
            except Exception:
                pass
            try:
                while True:
                    try:
                        envelope = q.get(timeout=15.0)
                        yield format_sse(envelope)
                    except queue.Empty:
                        # heartbeat
                        yield ": heartbeat\n\n"
            finally:
                _unsubscribe(sid, q)

        return Response(_generate(),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    # ── governance command routes ─────────────────────────────────────────

    def _gov_cmd(sid: str, method_name: str) -> Response:
        sess = _get_session(sid)
        port = _gov(sess)
        body = request.get_json(silent=True) or {}
        try:
            fn = getattr(port, method_name)
            result = fn(**body) if body else fn()
            # notify SSE subscribers
            _notify(sid, {"sse_type": "governance.applied",
                           "detail": _jsonable(result)})
            return _json_response(result)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.route("/api/sessions/<sid>/governance/pause", methods=["POST"])
    def gov_pause(sid: str) -> Response:
        return _gov_cmd(sid, "pause")

    @app.route("/api/sessions/<sid>/governance/resume", methods=["POST"])
    def gov_resume(sid: str) -> Response:
        return _gov_cmd(sid, "resume")

    @app.route("/api/sessions/<sid>/governance/stop", methods=["POST"])
    def gov_stop(sid: str) -> Response:
        return _gov_cmd(sid, "stop")

    @app.route("/api/sessions/<sid>/governance/local_patch", methods=["POST"])
    def gov_local_patch(sid: str) -> Response:
        return _gov_cmd(sid, "local_patch")

    @app.route("/api/sessions/<sid>/governance/goal_patch", methods=["POST"])
    def gov_goal_patch(sid: str) -> Response:
        return _gov_cmd(sid, "goal_patch")

    @app.route("/api/sessions/<sid>/governance/checkpoint", methods=["POST"])
    def gov_checkpoint(sid: str) -> Response:
        return _gov_cmd(sid, "checkpoint")

    @app.route("/api/sessions/<sid>/governance/rollback", methods=["POST"])
    def gov_rollback(sid: str) -> Response:
        return _gov_cmd(sid, "rollback")

    @app.route("/api/sessions/<sid>/governance/resolve_conflict",
               methods=["POST"])
    def gov_resolve(sid: str) -> Response:
        return _gov_cmd(sid, "resolve_conflict")

    # ── index route (serves workspace_ui if configured) ───────────────────

    @app.route("/")
    def index() -> Response:
        if app.static_folder is not None:
            return app.send_static_file("index.html")
        return jsonify({"service": "taskvm-projection",
                        "sessions": store.sids()})

    return app
