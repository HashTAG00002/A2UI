"""taskvm.projection.app — Flask factory for the TaskVM Projection UI
(contract §6: route matrix; §3: 0 model calls on read paths; §5: no
substrate import).

Route matrix (frozen contract §6; see
docs/contracts/projection_rfc_backlog.md for revision history):

    GET  /sessions/<sid>                        SPA page (or JSON snapshot)
    GET  /api/sessions                          list sessions
    GET  /api/sessions/<sid>/snapshot           full snapshot bundle
    GET  /api/sessions/<sid>/governance         governance bar
    GET  /api/sessions/<sid>/variables          task variables
    GET  /api/sessions/<sid>/workflow           workflow map
    GET  /api/sessions/<sid>/checkpoints        checkpoint timeline
    GET  /api/sessions/<sid>/surfaces           surface cards
    GET  /api/sessions/<sid>/conflicts          open conflicts
    GET  /api/sessions/<sid>/events             event log (paginated JSON)
    GET  /api/sessions/<sid>/sse                SSE stream (live deltas)
    GET  /api/sessions/<sid>/artifacts/<ref>    artifact bytes (read-only)
    POST /api/sessions/<sid>/governance/start      200 | 409 pending-recompose
    POST /api/sessions/<sid>/governance/pause      200
    POST /api/sessions/<sid>/governance/resume     200 | 409
    POST /api/sessions/<sid>/governance/stop       200
    POST /api/sessions/<sid>/governance/local_patch   200 | 422 non-editable
    POST /api/sessions/<sid>/governance/goal_patch    202 (async two-phase)
    POST /api/sessions/<sid>/governance/checkpoint    201 | 409 unstable
    POST /api/sessions/<sid>/governance/rollback      202 plan + disposition
    POST /api/sessions/<sid>/governance/resolve_conflict 200

Error semantics (typed, class-based — no string matching):
    UnknownCheckpointError → 404   (unknown checkpoint on rollback)
    PatchSemanticsError    → 422   (non-editable key / wrong patch class)
    ValidationError        → 409   (unstable boundary / pending recompose /
                                    pending compensation of current epoch)
    anything else          → 400   (malformed payload)
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Callable

from flask import Flask, Response, jsonify, request, send_file
from io import BytesIO

from taskvm.domain.errors import (
    PatchSemanticsError, TaskVMError, UnknownCheckpointError, ValidationError,
)
from taskvm.projection.events import (
    TRANSPORT_EVENT_SSE, format_sse, sse_envelope,
)
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
        try:
            frame = format_sse(envelope)  # vocabulary assertion
        except ValueError:
            return  # an unregistered sse_type never reaches the wire
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

    def _push_kernel_events(sid: str, sess: ProjectionSession,
                            since_index: int) -> None:
        """Forward kernel events emitted after ``since_index`` to SSE
        subscribers (typed envelopes; public facade only)."""
        events = sess.kernel.events()
        for ev in events[since_index:]:
            try:
                _notify(sid, sse_envelope(ev))
            except ValueError:
                pass

    def _driver_for(sess: ProjectionSession):
        """Return the session's driver, lazily constructing the default
        ThreadedRuntimeDriver over the registered runtime (the
        HTTP start path must be able to begin autonomy). ``None`` when
        the session has no runtime at all — the route reports honestly."""
        if sess.driver is not None:
            return sess.driver
        if sess.runtime is None:
            return None
        from taskvm.projection.services.driver import ThreadedRuntimeDriver

        def _on_runtime_event(ev: Any) -> None:
            try:
                _notify(sess.sid, sse_envelope(ev))
            except ValueError:
                pass

        sess.driver = ThreadedRuntimeDriver(
            sess.runtime, on_event=_on_runtime_event,
            kernel=sess.kernel)  # heartbeat path forwards kernel events
        return sess.driver

    # ── helpers ──────────────────────────────────────────────────────────

    def _get_session(sid: str) -> ProjectionSession:
        sess = store.get(sid)
        if sess is None:
            from flask import abort
            abort(404, description=f"session {sid!r} not found")
        return sess

    def _gov(sess: ProjectionSession):
        return sess.governance_port()

    def _json_response(data: Any, status: int = 200) -> Response:
        return jsonify(_jsonable(data)), status

    def _jsonable(value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (list, tuple)):
            return [_jsonable(v) for v in value]
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        return str(value)

    def _error_status(e: Exception) -> int:
        """Typed error → HTTP status (contract §6 error semantics)."""
        if isinstance(e, UnknownCheckpointError):
            return 404
        if isinstance(e, PatchSemanticsError):
            return 422
        if isinstance(e, ValidationError):
            return 409
        return 400

    def _error_body(action: str, e: Exception) -> dict[str, Any]:
        return {"ok": False, "action": action, "error": str(e)}

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
                yield format_sse({"sse_type": TRANSPORT_EVENT_SSE["snapshot"],
                                  "detail": snap})
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

    #: per-route success status (201 checkpoint creation, 202
    #: async two-phase goal_patch / plan-accepted rollback, 200 the rest)
    _SUCCESS_STATUS = {
        "local_patch": 200, "goal_patch": 202, "checkpoint": 201,
        "rollback": 202, "resolve_conflict": 200,
    }

    def _gov_cmd(sid: str, method_name: str) -> Response:
        sess = _get_session(sid)
        port = _gov(sess)
        body = request.get_json(silent=True) or {}
        event_index = len(sess.kernel.events())
        try:
            fn = getattr(port, method_name)
            result = fn(**body) if body else fn()
            if not isinstance(result, dict):
                result = {"ok": True, "result": _jsonable(result)}
            # rollback: hand the plan to the driver for execution (§8
            # honest disposition; without a driver it stays "pending")
            if method_name == "rollback":
                plan = result.pop("plan", None)
                if plan is not None:
                    driver = _driver_for(sess)
                    if driver is not None:
                        result["disposition"] = driver.execute_compensation(
                            plan)
            # notify SSE subscribers (registered transport ack type)
            _notify(sid, {"sse_type": TRANSPORT_EVENT_SSE["governance.applied"],
                          "detail": _jsonable(result)})
            # forward any kernel events the command produced (checkpoint.
            # committed / plan.patched / compensation.* …) as live deltas
            _push_kernel_events(sid, sess, event_index)
            status = _SUCCESS_STATUS.get(method_name, 200)
            return _json_response(result, status)
        except TaskVMError as e:
            _notify(sid, {"sse_type": "error",
                          "detail": _error_body(method_name, e)})
            return jsonify(_error_body(method_name, e)), _error_status(e)
        except Exception as e:  # malformed payload / unexpected failure
            return jsonify(_error_body(method_name, e)), 400

    @app.route("/api/sessions/<sid>/governance/pause", methods=["POST"])
    def gov_pause(sid: str) -> Response:
        """Pause routes through the DRIVER (single-owner path):
        driver.pause() → runtime.request_pause() → kernel governance.
        No separate KernelGovernancePort.pause() double-write."""
        sess = _get_session(sid)
        driver = _driver_for(sess)
        if driver is None:
            body = {"ok": False, "action": "pause",
                    "error": "本会话未注册运行时"}
            return jsonify(body), 409
        rationale = (request.get_json(silent=True) or {}).get("rationale", "")
        event_index = len(sess.kernel.events())
        state = driver.pause()
        result = {"ok": True, "action": "paused", "state": state,
                  "reason": rationale}
        _notify(sid, {"sse_type": TRANSPORT_EVENT_SSE["governance.applied"],
                      "detail": _jsonable(result)})
        _push_kernel_events(sid, sess, event_index)
        return _json_response(result)

    @app.route("/api/sessions/<sid>/governance/resume", methods=["POST"])
    def gov_resume(sid: str) -> Response:
        """Resume routes through the DRIVER (single-owner path):
        driver.resume() → runtime.request_resume() → kernel governance.
        Resume from stopped returns 409 (stop is persistent)."""
        sess = _get_session(sid)
        driver = _driver_for(sess)
        if driver is None:
            body = {"ok": False, "action": "resume",
                    "error": "本会话未注册运行时"}
            return jsonify(body), 409
        rationale = (request.get_json(silent=True) or {}).get("rationale", "")
        event_index = len(sess.kernel.events())
        state = driver.resume()
        if state == "stopped":
            body = {"ok": False, "action": "resume",
                    "error": "会话已停止（stop 是持久状态），无法恢复",
                    "state": "stopped"}
            return jsonify(body), 409
        result = {"ok": True, "action": "resumed", "state": state,
                  "reason": rationale}
        _notify(sid, {"sse_type": TRANSPORT_EVENT_SSE["governance.applied"],
                      "detail": _jsonable(result)})
        _push_kernel_events(sid, sess, event_index)
        return _json_response(result)

    @app.route("/api/sessions/<sid>/governance/stop", methods=["POST"])
    def gov_stop(sid: str) -> Response:
        """Stop routes through the DRIVER (single-owner path):
        driver.stop() → runtime.request_stop() → kernel governance.
        Stop is persistent — start() cannot revive a stopped driver."""
        sess = _get_session(sid)
        driver = _driver_for(sess)
        if driver is None:
            body = {"ok": False, "action": "stop",
                    "error": "本会话未注册运行时"}
            return jsonify(body), 409
        rationale = (request.get_json(silent=True) or {}).get("rationale", "")
        event_index = len(sess.kernel.events())
        state = driver.stop()
        result = {"ok": True, "action": "stopped", "state": state,
                  "reason": rationale}
        _notify(sid, {"sse_type": TRANSPORT_EVENT_SSE["governance.applied"],
                      "detail": _jsonable(result)})
        _push_kernel_events(sid, sess, event_index)
        return _json_response(result)

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

    # ── autonomy start (contract §6 row: begin/resume autonomy) ───────────

    @app.route("/api/sessions/<sid>/governance/start", methods=["POST"])
    def gov_start(sid: str) -> Response:
        """The HTTP path that begins autonomy via the runtime
        driver. 409 when the kernel awaits recompose (GoalPatch phase-1
        landed but the architect has not closed the composition) or when
        the session registered no runtime — honest conflicts, never 500."""
        sess = _get_session(sid)
        if getattr(sess.kernel, "pending_recompose", None) is not None:
            body = {"ok": False, "action": "start",
                    "error": "任务目标已变更，等待重新编排（recompose）后才能继续执行"}
            return jsonify(body), 409
        driver = _driver_for(sess)
        if driver is None:
            body = {"ok": False, "action": "start",
                    "error": "本会话未注册运行时（composition 未注入 runtime），无法启动自治"}
            return jsonify(body), 409
        try:
            state = driver.start()
        except Exception as e:
            return jsonify(_error_body("start", e)), 400
        # if the driver is persistently stopped, start() returns
        # "stopped" — the route must report 409 (cannot revive a stopped
        # driver; a fresh composition is required).
        if state == "stopped":
            body = {"ok": False, "action": "start",
                    "error": "会话已停止（stop 是持久状态），需要新的组合才能重新启动",
                    "state": "stopped"}
            return jsonify(body), 409
        result = {"ok": True, "action": "start", "state": state}
        _notify(sid, {"sse_type": TRANSPORT_EVENT_SSE["governance.applied"],
                      "detail": _jsonable(result)})
        return _json_response(result)

    # ── pages ─────────────────────────────────────────────────────────────

    @app.route("/sessions/<sid>")
    def session_page(sid: str) -> Response:
        """Contract §6: the per-session page. Serves the SPA shell when a
        static frontend is wired (the SPA reads the sid from the path);
        otherwise the JSON snapshot (200) — 404 for unknown sid either way."""
        sess = _get_session(sid)
        if app.static_folder is not None:
            try:
                return app.send_static_file("index.html")
            except Exception:
                pass
        return _json_response(snapshot_view(sess))

    @app.route("/")
    def index() -> Response:
        if app.static_folder is not None:
            return app.send_static_file("index.html")
        return jsonify({"service": "taskvm-projection",
                        "sessions": store.sids()})

    return app
