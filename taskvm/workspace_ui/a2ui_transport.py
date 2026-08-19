"""taskvm.workspace_ui.a2ui_transport — the A5 A2UI server→client
transport (composition seam, workspace_ui-owned).

This module owns the SERVER half of the React island's real message
stream:

    GET  /a2ui                      → the island's host page (built assets)
    GET  /api/app/a2ui/bootstrap    → createSurface + latest components
                                      + latest data model (ordered replay)
    GET  /api/app/a2ui/sse?after=N  → ordered A2UI tail + progress events
    POST /api/app/a2ui/action       → renderer action → ActionRouter
                                      validation → the PUBLIC
                                      governance local_patch

Design invariants (workplan §3/§5 + A9.0 latency audit):

- **Server owns facts, model generates structure.** ``createSurface`` /
  ``updateDataModel`` are produced deterministically from the projection
  snapshot (``TaskSurfaceContextBuilder`` + ``TaskDataModelProjector``,
  both pure); ``updateComponents`` comes from the component factory —
  the A4 generic baseline (``taskvm/genui/baseline.py``) by default,
  overridable at exactly one seam (``components_factory``) when the A4
  decoder product is wired into a surface. The factory output passes the
  SAME two-layer gate a decoder tree must pass; a validation failure
  fails honestly (``a2ui_failed`` progress event + no half-created
  surface), never a silent fallback tree.
- **Value updates are SMALL updateDataModel frames with ZERO model
  calls.** ``A2uiDataPoller`` watches the public snapshot; when (and only
  when) the projected data model deep-changes it appends one
  ``updateDataModel`` message to the per-session ``SurfaceStore``. The
  component tree (``generation``) is never regenerated on this path —
  the A5 acceptance invariant, and the direct countermeasure to the
  2.66 MB-frame starvation found by the A9.0 audit: no screenshot bytes,
  no artifact refs, no big payloads ever enter this stream.
- **Progress events ride the SAME SSE connection** as named SSE events
  (``event: progress``) — the §20.1 progressive-plane signals (T0 goal
  acceptance, T1 variable labels from the compiler product, T2 DAG from
  the kernel). They are transient UI morph hints, deliberately NOT
  replayed on reconnect beyond the small ring bound: reconnect recovery
  is the ordered A2UI tail (``after=N``) + goal-status polling, which
  are authoritative.
- **Actions come back through the ONLY write path.** The renderer's
  ``A2uiClientAction`` is posted here and routed through the genui
  ``ActionRouter`` — the C2S validation half, running the SAME ground
  truth the S2C policy layer uses (allowlist / mutability / value
  type / bindings-arrive-resolved) — into one structured
  ``LocalPatchIntent`` that lands as a kernel ``local_patch`` via the
  session's public governance port, the identical command the fixed
  shell's governance routes run. No state is mutated outside the
  governance port; nothing bypasses the real GUI gesture (the button
  the user pressed IS the rendered A2UI Button component).

Layering: this module imports the genui public API + projection public
view models only — both read-only for this layer. It never imports
substrate drivers, never calls models, and never touches the frozen
projection package's routes.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from taskvm.genui import (
    ActionRouteError, ActionRouter, SurfaceStore, SurfaceStoreRegistry,
    TaskDataModelProjector, TaskSurfaceContextBuilder,
    baseline_components, validate_components,
)
from taskvm.projection.view_models import snapshot_view, workflow_view

#: How often the data-model poller re-projects the public snapshot.
POLL_INTERVAL_S = 1.0

#: Named-SSE progress events retained per session (they are transient
#: morph hints; the ring bound is a leak guard, not a replay window).
_PROGRESS_RING = 128


#: The single swap seam for the component tree: the A4 generic baseline
#: today; a composition root may override it with the decoder product
#: (same signature: context → components) — the store only accepts trees
#: that pass the two-layer gate either way.
surface_components_factory: Callable[[Any], list[dict[str, Any]]] = \
    baseline_components


# ── §20.1 progress payloads (screen-visible fields only) ───────────────────

_KIND_TO_CHIP = {
    "sequence": "step", "fan-out": "step", "bounded loop": "step",
    "verify barrier": "step",
    "step": "step", "verification": "verification",
    "checkpoint": "checkpoint", "goal": "goal",
}
_STATUS_TO_CHIP = {
    "waiting": "waiting", "ready": "waiting", "executing": "executing",
    "verified": "verified", "failed": "failed",
    "invalidated": "waiting", "rolled_back": "waiting",
}


def compiler_stage_payload(compiler_result) -> dict[str, Any]:
    """T1 payload — the compiler product's variable labels (values are
    NOT included: the skeleton shows labels + pending marks only)."""
    return {"variables": [
        {"label": v.label or v.semantic_key}
        for v in compiler_result.variables
    ]}


def kernel_stage_payload(kernel) -> dict[str, Any]:
    """T2 payload — the kernel's real DAG as progressive-plane chips."""
    wf = workflow_view(kernel.workflow(), kernel.events())
    nodes = [
        {"label": n["label"],
         "kind": _KIND_TO_CHIP.get(n["kind_label"], "step"),
         "status": _STATUS_TO_CHIP.get(n["status_label"], "waiting")}
        for n in wf.get("nodes", [])
    ]
    return {"nodes": nodes}


# ── the transport state ─────────────────────────────────────────────────────


class A2uiTransportError(RuntimeError):
    """Honest transport rejection (carries an HTTP status)."""

    def _status(self, code: int) -> "A2uiTransportError":
        self.http_status = code
        return self


class A2uiTransport:
    """Per-APP A2UI stream state: SurfaceStore registry + the progress
    ring + the data-model poller lifecycle. One instance per APP shell;
    thread-safe (Flask threads + poller threads + goal threads).

    ``session_lookup`` (optional) maps sid → the projection store's
    CURRENT session object; the poller retires itself when the session
    it was minted for is no longer the registered one — the same
    discipline as the APP shell's screenshot poller.
    """

    def __init__(self, *,
                 components_factory: Callable[
                     [Any], list[dict[str, Any]]] | None = None,
                 session_lookup: Callable[[str], Any] | None = None
                 ) -> None:
        self._registry = SurfaceStoreRegistry()
        self._lock = threading.Lock()
        self._progress: dict[str, deque[tuple[int, dict[str, Any]]]] = {}
        self._progress_seq: dict[str, int] = {}
        self._pollers: dict[str, "A2uiDataPoller"] = {}
        self._factory = components_factory or surface_components_factory
        self._session_lookup = session_lookup

    # ── progress events (named SSE events; transient morph hints) ──────
    def push_stage(self, sid: str, stage: str,
                   payload: dict[str, Any] | None = None) -> None:
        with self._lock:
            ring = self._progress.setdefault(sid, deque(maxlen=_PROGRESS_RING))
            self._progress_seq[sid] = self._progress_seq.get(sid, 0) + 1
            ring.append((self._progress_seq[sid],
                         {"stage": stage, **(payload or {})}))

    def progress_after(self, sid: str, after: int
                       ) -> list[tuple[int, dict[str, Any]]]:
        with self._lock:
            ring = self._progress.get(sid)
            if not ring:
                return []
            return [(s, ev) for s, ev in ring if s > after]

    # ── surface lifecycle ───────────────────────────────────────────────
    def store(self, sid: str) -> SurfaceStore | None:
        return self._registry.get(sid)

    def _build_context(self, sess) -> Any:
        return TaskSurfaceContextBuilder().build(snapshot_view(sess))

    def attach_session(self, sid: str, sess) -> dict[str, Any]:
        """Goal bootstrap finished → mint the A2UI stream: one
        createSurface + one updateComponents (structural, validated) +
        one updateDataModel. Starts the value poller. Returns the
        transport's bookkeeping view (for logs/evidence).

        Raises ``A2uiTransportError`` on a validation failure — the
        surface is NOT created and the reason rides an ``a2ui_failed``
        progress event (never a silent fallback tree, never a
        half-created stream).
        """
        context = self._build_context(sess)
        data_model = TaskDataModelProjector().project(context)
        components = self._factory(context)
        errors = validate_components(components, context, data_model,
                                     surface_id=f"taskvm-task-{sid}")
        if errors:
            self.push_stage(sid, "a2ui_failed", {"errors": errors[:8]})
            raise A2uiTransportError(
                "surface components failed validation: "
                + "; ".join(errors[:8]))
        s = self._registry.get_or_create(sid)
        s.ensure_surface()
        s.set_components(components)
        s.set_data_model(data_model)
        self.push_stage(sid, "ready", {"surfaceId": s.surface_id})
        self._start_poller(sid, sess)
        return {"surfaceId": s.surface_id, "generation": s.generation,
                "dataRevision": s.data_revision,
                "componentCount": len(components)}

    def refresh_data_model(self, sid: str, sess) -> bool:
        """Re-project the public snapshot; append ONE updateDataModel
        frame iff the data model deep-changed. This is the ordinary
        value-update path: zero model calls, zero component regeneration
        (the store bumps ``data_revision`` only — ``generation`` is the
        GenUI-call marker and must stay frozen here)."""
        s = self._registry.get(sid)
        if s is None:
            return False
        data_model = TaskDataModelProjector().project(
            self._build_context(sess))
        if data_model == s.latest_data_model():
            return False
        s.set_data_model(data_model)
        return True

    def _start_poller(self, sid: str, sess) -> None:
        with self._lock:
            old = self._pollers.get(sid)
            if old is not None:
                old.stop()
            poller = A2uiDataPoller(self, sid, sess)
            self._pollers[sid] = poller
        poller.start()

    def drop_session(self, sid: str) -> None:
        """Retire the poller (session replaced/dropped by a new goal)."""
        with self._lock:
            poller = self._pollers.pop(sid, None)
        if poller is not None:
            poller.stop()

    # ── the action path (the ONLY write path through this transport) ───
    def apply_action(self, sid: str, sess, *, name: str,
                     context: dict[str, Any]) -> dict[str, Any]:
        """Renderer action → ActionRouter validation → ONE kernel
        local_patch via the session's public governance port.

        The genui ``ActionRouter`` owns the C2S validation half (the
        SAME ground truth the S2C policy layer uses: allowlist /
        mutability / value type / bindings-arrive-resolved) and mints a
        structured ``LocalPatchIntent``; this method is only the
        execution half — the intent's updates go to the session's
        governance port verbatim, no middle-model translation
        (workplan §20.2).

        Raises ``A2uiTransportError`` with an ``http_status`` attribute
        for every honest rejection (unknown action 400, governance-owned
        403, readonly 403, bad/missing value 400, unresolved binding
        400). No best-effort guessing."""
        try:
            intent = ActionRouter(self._build_context(sess)).route(
                name, context)
        except ActionRouteError as e:
            raise A2uiTransportError(str(e))._status(
                e.http_status) from e
        result = sess.governance_port().local_patch(
            intent.updates, rationale=intent.rationale)
        # The value path: the poller will observe the new desired value
        # and append the updateDataModel frame — zero model calls.
        return result


# ── the value-update poller ─────────────────────────────────────────────────


class A2uiDataPoller(threading.Thread):
    """Daemon that keeps the surface's data model in lockstep with the
    public projection snapshot — the ONLY thing it does is re-project
    and, on deep change, append one small ``updateDataModel`` message.
    Zero model calls by construction (read-only snapshot → pure
    projector → store append). Retires itself when its session is no
    longer the registered one (the injected ``session_lookup``) or when
    explicitly stopped (``drop_session``)."""

    def __init__(self, transport: A2uiTransport, sid: str, sess) -> None:
        super().__init__(daemon=True, name=f"a2ui-poll-{sid}")
        self._transport = transport
        self._sid = sid
        self._sess = sess
        # NB: NOT ``self._stop`` — threading.Thread owns that name for
        # its internal teardown; shadowing it crashes on thread exit.
        self._stop_evt = threading.Event()

    def run(self) -> None:
        while not self._stop_evt.wait(POLL_INTERVAL_S):
            try:
                lookup = self._transport._session_lookup
                if lookup is not None and lookup(self._sid) is not self._sess:
                    return    # replaced/dropped — stop watching
                self._transport.refresh_data_model(self._sid, self._sess)
            except Exception:
                continue    # read-only; a transient kernel lock/visibility
                #            hiccup must never kill the side channel

    def stop(self) -> None:
        self._stop_evt.set()


# ── Flask route registration (composition seam, APP-shell family) ──────────


def register_a2ui_routes(app, transport: A2uiTransport, store,
                         state) -> None:
    """Add the A2UI transport routes to the APP Flask object.

    All routes live under ``/api/app/a2ui/*`` (single-sid world, same
    family as the existing APP-shell routes) plus the ``/a2ui`` host
    page. The frozen projection route matrix is untouched."""

    # ── GET /a2ui — the React island's host page ───────────────────────
    @app.route("/a2ui")
    def a2ui_page():
        from flask import jsonify, send_from_directory
        index = Path(app.static_folder) / "a2ui" / "index.html"
        if not index.is_file():
            return jsonify({
                "ok": False,
                "error": "the A2UI island is not built yet — run "
                         "npm run build in taskvm/workspace_ui/a2ui_client "
                         "(output mounts at static/a2ui/)",
            }), 404
        return send_from_directory(app.static_folder, "a2ui/index.html")

    # ── GET /api/app/a2ui/bootstrap — ordered message replay ───────────
    @app.route("/api/app/a2ui/bootstrap")
    def a2ui_bootstrap():
        from flask import jsonify
        s = transport.store(state.sid)
        if s is None:
            return jsonify({"ok": False,
                            "error": "no A2UI surface yet"}), 404
        return jsonify({
            "ok": True,
            "surfaceId": s.surface_id,
            "seq": s.seq,
            "generation": s.generation,
            "dataRevision": s.data_revision,
            "messages": s.bootstrap_messages(),
        })

    # ── GET /api/app/a2ui/sse — ordered A2UI tail + progress events ────
    @app.route("/api/app/a2ui/sse")
    def a2ui_sse():
        import json as _json
        from flask import Response, request
        try:
            after = int(request.args.get("after", 0) or 0)
        except ValueError:
            after = 0
        sid = state.sid

        def _gen():
            cursor = after
            progress_cursor = 0
            idle = 0.0
            while True:
                s = transport.store(sid)
                frames: list[str] = []
                if s is not None:
                    msgs = s.events_after(cursor)
                    if msgs:
                        # SurfaceStore seqs are gapless (+1 per append),
                        # so message i of this batch carries cursor+i.
                        for i, msg in enumerate(msgs, start=1):
                            env = {"type": "a2ui", "seq": cursor + i,
                                   "message": msg}
                            frames.append(
                                f"data: {_json.dumps(env, ensure_ascii=False)}"
                                "\n\n")
                        cursor += len(msgs)
                for pseq, ev in transport.progress_after(sid,
                                                         progress_cursor):
                    progress_cursor = pseq
                    frames.append(
                        "event: progress\ndata: "
                        + _json.dumps(ev, ensure_ascii=False) + "\n\n")
                if frames:
                    idle = 0.0
                    yield "".join(frames)
                else:
                    time.sleep(0.4)
                    idle += 0.4
                    if idle >= 15.0:
                        idle = 0.0
                        yield ": heartbeat\n\n"

        return Response(_gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    # ── POST /api/app/a2ui/action — the renderer's write path ─────────
    @app.route("/api/app/a2ui/action", methods=["POST"])
    def a2ui_action():
        from flask import jsonify, request
        body = request.get_json(silent=True) or {}
        name = str(body.get("name", "") or "")
        context = body.get("context") or {}
        if not isinstance(context, dict):
            return jsonify({"ok": False,
                            "error": "context must be an object"}), 400
        sess = store.get(state.sid)
        if sess is None:
            return jsonify({"ok": False,
                            "error": "no active session"}), 404
        if transport.store(state.sid) is None:
            return jsonify({"ok": False,
                            "error": "no A2UI surface yet"}), 404
        try:
            result = transport.apply_action(
                state.sid, sess, name=name, context=context)
        except A2uiTransportError as e:
            return jsonify({"ok": False, "error": str(e)}), \
                getattr(e, "http_status", 400)
        return jsonify({"ok": True, "action": name, "result": result})
