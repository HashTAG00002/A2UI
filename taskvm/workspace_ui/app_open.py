"""python -m taskvm.workspace_ui.app_open — the TaskVM APP (Codex-like shell).

The user-visible APP a real human opens in a browser. Unlike ``demo_open``
(goal fixed on the launch line, bootstrap runs BEFORE the server), the APP
boots EMPTY:

    launch → empty store → UI hero "How can I help you today?"
    user submits a natural-language goal
      → POST /api/app/goals           (this module's route)
      → background: stop old driver → drop old session →
        bootstrap_real_full(goal …)   (the ONE composition, real model
        calls: StateCompiler + TaskArchitect via HttpModelPort) → session
        registered in the projection store
      → frontend polls /api/app/goals/<gid> (stage timeline + live timer)
        until ready, then drives the PUBLIC projection routes (select sid
        → SSE → the user EXPLICITLY presses Start — no autostart)

Layering (why this file is legal):
* ``taskvm/projection/`` is FROZEN — this module does NOT touch it. It
  builds the stock app via ``workspace_ui.serve`` (composition seam) and
  then adds APP-shell routes ON THE FLASK OBJECT from here
  (``workspace_ui`` is the ACTIVE layer; session creation is composition
  glue — exactly the place the contracts let wiring live).
* autonomy is never started from here — the ONLY start path is the frozen
  ``POST /api/sessions/<sid>/governance/start`` pressed by the user.
* the APP has NO reset/seed power (contract §4): the world is activated
  ONCE, up-front, by the launch script's setup phase
  (``scripts/app_mobilegym.sh`` → ``POST /api/reset/<sid>`` on the
  bridge). Every goal runs on the SAME activated sid — one phone, one
  live world; new goals see the world as the previous goal left it
  (bottom-up projection, principle 1).
* app/surface selection is a RUNTIME capability, not a task-UI semantic
  (workplan §2): the goal API accepts ``{"goal": …}`` only; the initial
  foreground surface comes from server config (``TASKVM_INITIAL_APP`` /
  ``--initial-app``, default: the substrate provider's own default), and
  the world's surfaces are DISCOVERED through the substrate port
  (``list_surfaces``), never hardcoded here.

Single-session honesty (bridge contract: ONE active sid): the store keeps ONE
session per bridge sid; a new goal stops the previous driver and
re-registers under the same sid.

Screenshot side-channel (workspace_ui-owned, read-only): the composition
registers the runtime wrapped in ``ScreenshotArchivingRuntime`` — runtime
events reaching the projection plane carry compact artifact tokens while
the decoded bytes land in the session's frozen ArtifactStore and in this
module's live-shot cache. ``GET /api/app/screenshot`` serves the live
phone with a content-hash dedup param (unchanged screen ⇒ zero-body 200)
and an optional server-side thumbnail (≤240 px JPEG, Pillow), so a slow
remote link never has to move a 2 MB PNG per poll.

Usage
-----
    OPENAI_API_KEY=… python -m taskvm.workspace_ui.app_open \\
        --host 0.0.0.0 --port 3016 --start-bridge
    # (scripts/app_mobilegym.sh wraps this: sim + bridge + world reset
    #  + this APP, all long-lived, pids under .run/)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from typing import Any

from taskvm.architect import ModelCallLedger
from taskvm.architect.http_port import HttpModelPort
from taskvm.projection.store import ProjectionSessionStore
from taskvm.domain.errors import TaskVMError
from taskvm.projection.view_models import snapshot_view
from taskvm.substrate import substrate_registry
from taskvm.workspace_ui import serve
from taskvm.workspace_ui.call_archive import maybe_recording_port
from taskvm.workspace_ui.composition import (
    artifact_fingerprint, bootstrap_real_full,
)
from taskvm.workspace_ui.a2ui_transport import (
    A2uiTransport, A2uiTransportError, compiler_stage_payload,
    kernel_stage_payload, register_a2ui_routes,
)
from taskvm.workspace_ui.a2ui_transport import (
    _taskvm_error_status,
)
from taskvm.workspace_ui.demo_open import (
    _ensure_mobilegym_bridge,          # closed-whitelist bridge glue
    _probe_observe,                    # one read-only observe() before serving
)

_GOAL_BOOTSTRAPPING = "bootstrapping"
_GOAL_READY = "ready"
_GOAL_FAILED = "failed"

_THUMB_CACHE_MAX = 64


class _ScreenshotPoller(threading.Thread):
    """Per-session daemon that keeps the screenshot side channel current.

    ``ScreenshotArchivingRuntime`` is pull-based: its transform (and the
    ``on_screenshot`` sink wired into ``AppState.push_screenshot``) fires
    when ``runtime_events()`` is read. The autonomy driver reads it every
    tick; this poller keeps a 1s heartbeat read so the side channel also
    advances while the driver is paused/idle-with-events. Read-only; exits
    when its session is no longer the registered one."""

    _POLL_S = 1.0

    def __init__(self, app_state: "AppState", sid: str, sess: Any) -> None:
        super().__init__(daemon=True, name=f"shot-{sid}")
        self._app = app_state
        self._sid = sid
        self._sess = sess

    def run(self) -> None:
        while True:
            time.sleep(self._POLL_S)
            if self._app.store.get(self._sid) is not self._sess:
                return          # dropped / replaced — stop watching
            rt = getattr(self._sess, "runtime", None)
            events_fn = getattr(rt, "runtime_events", None)
            if not callable(events_fn):
                return
            try:
                events_fn()     # drives the transform + sink
            except Exception:
                continue


def _thumbnail_bytes(data: bytes, max_px: int) -> bytes | None:
    """Server-side downscale to a JPEG thumbnail (≤ max_px on the long
    side). ``None`` when Pillow is unavailable or the decode fails — the
    caller then serves the original bytes (honest degradation, never an
    error to the client)."""
    try:
        import io

        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=75)
        return buf.getvalue()
    except Exception:
        return None


class AppState:
    """The APP shell's own mutable state (goals + live shots + thumbs)."""

    def __init__(self, store: ProjectionSessionStore, *,
                 sid: str, bridge_url: str, sim_url: str,
                 model: str | None, surfaces: tuple[dict[str, str], ...],
                 initial_app: str, offline: bool,
                 a2ui: A2uiTransport | None = None) -> None:
        self.store = store
        self.sid = sid
        self.bridge_url = bridge_url
        self.sim_url = sim_url
        self.model = model
        self.surfaces = surfaces          # discovered via the substrate port
        self.initial_app = initial_app    # "" ⇒ the provider's own default
        self.offline = offline
        self.a2ui = a2ui                  # None ⇒ APP runs without island
        self._lock = threading.Lock()
        self._goals: list[dict[str, Any]] = []
        self._seq = 0
        self._shots: dict[str, tuple[str, bytes, float, str]] = {}
        self._shot_seq: dict[str, int] = {}
        self._ledgers: dict[str, ModelCallLedger] = {}
        self._thumbs: dict[tuple[str, int], bytes] = {}
        self._boot_lock = threading.Lock()   # one bootstrap at a time

    # ── screenshots (live phone side channel) ──────────────────────────
    def push_screenshot(self, sid: str, mime: str, data: bytes) -> None:
        with self._lock:
            self._shots[sid] = (mime, data, time.time(),
                                artifact_fingerprint(data))
            self._shot_seq[sid] = self._shot_seq.get(sid, 0) + 1

    def screenshot(self, sid: str) -> tuple[str, bytes, int, str] | None:
        with self._lock:
            shot = self._shots.get(sid)
            if shot is None:
                return None
            mime, data, _, fp = shot
            return mime, data, self._shot_seq.get(sid, 0), fp

    def thumbnail(self, data: bytes, max_px: int) -> bytes | None:
        """Cached thumbnail for immutable bytes (keyed by content hash)."""
        fp = artifact_fingerprint(data)
        key = (fp, max_px)
        with self._lock:
            hit = self._thumbs.get(key)
        if hit is not None:
            return hit
        thumb = _thumbnail_bytes(data, max_px)
        if thumb is None:
            return None
        with self._lock:
            if len(self._thumbs) >= _THUMB_CACHE_MAX:
                self._thumbs.pop(next(iter(self._thumbs)))
            self._thumbs[key] = thumb
        return thumb

    # ── goals ──────────────────────────────────────────────────────────
    def new_goal(self, goal: str) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            gid = f"goal-{self._seq}"
            rec = {"goal_id": gid, "sid": self.sid, "goal": goal,
                   "status": _GOAL_BOOTSTRAPPING,
                   "error": "", "model_calls": 0,
                   "created_at": time.time(),
                   "stages": {}}
            self._goals.append(rec)
            return rec

    def attach_ledger(self, gid: str, ledger: ModelCallLedger) -> None:
        with self._lock:
            self._ledgers[gid] = ledger

    def goal(self, gid: str) -> dict[str, Any] | None:
        with self._lock:
            for g in self._goals:
                if g["goal_id"] == gid:
                    return dict(g)
        return None

    def goals(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(g) for g in self._goals]

    def goal_detail(self, gid: str) -> dict[str, Any] | None:
        """The goal record + a live ledger summary (per-role call counts).

        Stage timestamps are stamped idempotently on first observation of
        each role transition — poll-driven granularity (the poller drives
        this route), honest, never fabricated."""
        rec = self.goal(gid)
        if rec is None:
            return None
        with self._lock:
            ledger = self._ledgers.get(gid)
            counts = {"state_compiler": 0, "task_architect": 0, "cua": 0}
            if ledger is not None:
                for r in ledger.records:
                    counts[r.role] = counts.get(r.role, 0) + 1
            total = sum(counts.values())
            stages = dict(rec["stages"])
            now = time.time()
            if counts["state_compiler"] >= 1 and \
                    "compiler_done_at" not in stages:
                stages["compiler_done_at"] = now
            if counts["task_architect"] >= 1 and \
                    "architect_done_at" not in stages:
                stages["architect_done_at"] = now
            for g in self._goals:
                if g["goal_id"] == gid:
                    g["stages"] = stages
                    g["model_calls"] = total
        out = dict(rec)
        out["stages"] = stages
        out["model_calls"] = total
        out["ledger_counts"] = counts
        return out

    def finish(self, gid: str, *, ok: bool, error: str = "",
               model_calls: int = 0) -> None:
        with self._lock:
            for g in self._goals:
                if g["goal_id"] == gid:
                    g["status"] = _GOAL_READY if ok else _GOAL_FAILED
                    g["error"] = error
                    g["model_calls"] = model_calls
                    g["finished_at"] = time.time()

    # ── the A2UI island's §20.1 progressive-plane signals ──────────
    def a2ui_stage(self, stage: str, product: Any) -> None:
        """composition ``on_stage`` → named SSE progress events (T1
        variable labels from the compiler product, T2 DAG from the
        kernel). Maps the two honest stage names onto the transport's
        payload builders; never fabricates a third stage."""
        if self.a2ui is None:
            return
        try:
            if stage == "compiler":
                self.a2ui.push_stage(self.sid, "t1",
                                     compiler_stage_payload(product))
            elif stage == "kernel":
                self.a2ui.push_stage(self.sid, "t2",
                                     kernel_stage_payload(product))
        except Exception:
            pass    # observability only — composition also guards this

    # ── the goal runner (one at a time; single world, single sid) ──────
    def run_goal(self, rec: dict[str, Any]) -> None:
        """Background: stop the old driver → drop the old session →
        bootstrap the new goal on the SAME activated sid → expose the
        model-call probe → start the screenshot poller."""
        gid, goal = rec["goal_id"], rec["goal"]
        ledger = ModelCallLedger()   # visible in the except path too —
        #                            failures still report what they cost
        self.attach_ledger(gid, ledger)
        if self.a2ui is not None:
            # T0 signal: the goal text the user actually submitted — the
            # island morphs to its compile skeleton off THIS, not a guess
            self.a2ui.push_stage(self.sid, "goal", {"goal": goal})
        try:
            with self._boot_lock:
                # (a) retire the previous session honestly: stop its
                # autonomy loop BEFORE dropping it from the store.
                old = self.store.get(self.sid)
                if old is not None:
                    driver = getattr(old, "driver", None)
                    if driver is not None:
                        try:
                            driver.stop()
                            driver.join(timeout=5.0)
                        except Exception:
                            pass        # best-effort; bootstrap proceeds
                    self.store.drop(self.sid)
                    if self.a2ui is not None:
                        self.a2ui.drop_session(self.sid)   # retire poller
                # (b) the ONE composition: real substrate session →
                # bootstrap_real_full (compiler + architect REAL calls).
                cua_model = None
                if self.offline:
                    from taskvm.workspace_ui.demo_open import _OfflineCUA
                    cua_model = _OfflineCUA()
                cfg: dict[str, Any] = {"sid": self.sid,
                                       "bridge_url": self.bridge_url}
                if self.initial_app:
                    cfg["app"] = self.initial_app   # runtime capability, not
                    #                                  a task-UI semantic
                substrate = substrate_registry.create_session(
                    "mobilegym", cfg)
                # full-fidelity model-call archiving (opt-in): when
                # TASKVM_CALL_ARCHIVE_DIR is set, EVERY provider request
                # (compiler / architect / CUA) lands one verbatim txt +
                # image files in that dir; without the var this is a
                # pass-through and behavior is unchanged.
                port = maybe_recording_port(HttpModelPort())
                _archive_session_note(gid, goal, self)

                def _sink(token: str, mime: str, data: bytes) -> None:
                    self.push_screenshot(self.sid, mime, data)

                bundle = bootstrap_real_full(
                    goal=goal, sid=self.sid, substrate=substrate,
                    ledger=ledger, store=self.store, model=self.model,
                    model_port=port, cua_model=cua_model,
                    screenshot_sink=_sink, on_stage=self.a2ui_stage)
                # (c) governance-bar probe: unified compiler+architect+CUA
                # call count (read-only closure over the shared ledger).
                sess = self.store.get(self.sid)
                if sess is not None:
                    sess.model_call_probe = ledger.total
                    _ScreenshotPoller(self, self.sid, sess).start()
                    if self.a2ui is not None:
                        # A6: re-wire the intent parser onto THIS goal's
                        # shared ledger + port (rows for an old goal
                        # never land in the new one) and the §20.2
                        # routing slot (the small fast model when
                        # TASKVM_ROLE_MODELS/TASKVM_INTENT_PARSER_MODEL
                        # sets one; the port default otherwise).
                        from taskvm.genui import IntentParser
                        from taskvm.workspace_ui.composition import (
                            resolve_role_models,
                        )
                        self.a2ui.set_intent_parser(IntentParser(
                            port, ledger,
                            model=resolve_role_models().get(
                                "intent_parser")))
                        try:
                            self.a2ui.attach_session(self.sid, sess)
                        except A2uiTransportError:
                            # the honest reason already rides an
                            # a2ui_failed progress event; the goal itself
                            # (kernel + runtime + fixed shell) is healthy
                            pass
                _ = bundle          # (bundle kept for debugging; the
                #                      projection store is the UI's truth)
                self.finish(gid, ok=True, model_calls=ledger.total())
        except Exception as e:      # honest failure — surfaced in the UI,
            #                       with the real cost of the failed attempt
            if self.a2ui is not None:
                self.a2ui.push_stage(self.sid, "goal_failed",
                                     {"error": f"{type(e).__name__}: {e}"})
            self.finish(gid, ok=False,
                        error=f"{type(e).__name__}: {e}",
                        model_calls=ledger.total())


def _archive_session_note(gid: str, goal: str,
                          state: "AppState") -> None:
    """When call archiving is on, drop a session header file into the
    archive dir (goal, sid, model, time) so the folder is self-describing."""
    d = os.environ.get("TASKVM_CALL_ARCHIVE_DIR", "").strip()
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "00_SESSION.txt"), "a",
                  encoding="utf-8") as f:
            f.write("═" * 78 + "\n")
            f.write(f"goal_id : {gid}\n")
            f.write(f"goal    : {goal}\n")
            f.write(f"substrate: mobilegym\n")
            f.write(f"sid     : {state.sid}\n")
            f.write(f"model   : {state.model or 'TASKVM_MODEL env or default'}\n")
            f.write(f"time    : {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("═" * 78 + "\n")
    except Exception:
        pass  # archiving is observability, never a failure path


def _shot_response(data: bytes, mime: str, *, fp: str,
                   cacheable: bool):
    from flask import Response
    headers = {"X-Shot-Hash": fp}
    headers["Cache-Control"] = ("public, max-age=300" if cacheable
                                else "no-cache")
    return Response(data, mimetype=mime, headers=headers)


#: The CLOSED command vocabulary of the island's governance proxy route
#: (A9.1: optimistic first-response). These map 1:1 onto the FROZEN
#: projection routes' driver/governance semantics — this proxy is the
#: single-sid composition seam so the React island never has to know the
#: session id (GUI-only, repo contract §3).
_APP_GOV_COMMANDS = frozenset({
    "start", "pause", "resume", "stop", "checkpoint", "rollback",
})


def _lazy_driver(sess):
    """The frozen app's lazy ThreadedRuntimeDriver construction, reused
    verbatim by the island's governance proxy (one driver per session,
    single-owner path). ``None`` when the session registered no runtime."""
    if getattr(sess, "driver", None) is not None:
        return sess.driver
    if getattr(sess, "runtime", None) is None:
        return None
    from taskvm.projection.services.driver import ThreadedRuntimeDriver
    sess.driver = ThreadedRuntimeDriver(sess.runtime, kernel=sess.kernel)
    return sess.driver


def _surface_shot_entries(state: "AppState") -> list[dict[str, Any]]:
    """The screenshot wall's card list (A9.2): one entry per world
    surface, each carrying ONLY user-visible fields + ready-to-render
    URLs (the client never assembles internal ids into URLs).

    Foreground surface → the live-shot side channel (fresh every poller
    tick). Background surfaces (multi-substrate worlds, and the A-03
    heartbeat fresh-observe channel when one is registered) extend this
    list with ``role: "background"`` — same card shape, lower cadence.
    """
    entries: list[dict[str, Any]] = []
    shot = state.screenshot(state.sid)
    fg_name = (state.surfaces[0]["name"] if state.surfaces
               else "MobileGym")
    if shot is not None:
        _mime, _data, seq, fp = shot
        entries.append({
            "name": fg_name,
            "role": "foreground",
            "hash": fp,
            "seq": seq,
            "thumbUrl": f"/api/app/screenshot?thumb=1&w=240&h={fp}",
            "fullUrl": f"/api/app/screenshot?h={fp}",
        })
    return entries


def register_app_routes(app, store: ProjectionSessionStore,
                        state: AppState) -> None:
    """Add the APP-shell routes to the stock projection Flask app.

    These are composition-seam routes (session creation + screenshot
    side-channel); everything else stays on the frozen projection route
    matrix."""

    # ── GET /api/app/status — world + config for the empty-state hero ──
    @app.route("/api/app/status")
    def app_status():
        shot = state.screenshot(state.sid)
        return {
            "ok": True, "app": "taskvm-app",
            "world": {
                "substrate": "mobilegym",
                "sid": state.sid,
                "bridge_url": state.bridge_url,
                "sim_url": state.sim_url,
                "surfaces": [dict(s) for s in state.surfaces],
                "model": (state.model
                          or os.environ.get("TASKVM_MODEL", "gpt-5.6-sol")),
                "offline": state.offline,
                "openai_configured": bool(os.environ.get("OPENAI_API_KEY")),
            },
            "live_screenshot_seq": (shot[2] if shot else 0),
            "goals": state.goals(),
        }

    # ── POST /api/app/goals — the first instruction creates the session ─
    #     Body: {"goal": "…"}. The app/surface a goal runs on is a runtime
    #     capability (server config), never a user-facing task parameter.
    @app.route("/api/app/goals", methods=["POST"])
    def app_create_goal():
        from flask import jsonify, request
        body = request.get_json(silent=True) or {}
        goal = str(body.get("goal", "") or "").strip()
        if not goal:
            return jsonify({"ok": False, "error": "goal 不能为空"}), 400
        if not state.offline and not os.environ.get("OPENAI_API_KEY"):
            return jsonify({"ok": False,
                            "error": "OPENAI_API_KEY 未设置——编排阶段"
                                     "（compiler+architect）需要真实模型"
                                     "调用，没有手写兜底计划"}), 400
        if any(g["status"] == _GOAL_BOOTSTRAPPING for g in state.goals()):
            return jsonify({"ok": False,
                            "error": "上一个任务还在编排中（同一时刻只"
                                     "有一个活动世界）——请稍候"}), 409
        rec = state.new_goal(goal)
        threading.Thread(target=state.run_goal, args=(rec,), daemon=True,
                         name=f"goal-{rec['goal_id']}").start()
        return jsonify({"ok": True, "goal": rec}), 202

    # ── GET /api/app/goals/<gid> — bootstrap status polling ────────────
    @app.route("/api/app/goals/<gid>")
    def app_goal_status(gid: str):
        from flask import jsonify
        rec = state.goal_detail(gid)
        if rec is None:
            return jsonify({"ok": False, "error": "unknown goal"}), 404
        return jsonify({"ok": True, "goal": rec})

    # ── GET /api/app/screenshot — live phone / artifact serving ────────
    #     Query:
    #       sid   — which session (default: the APP's sid)
    #       ref   — an artifact token (serves that stored artifact)
    #       h     — the client's last X-Shot-Hash: same content ⇒ a
    #               zero-body 200 (unchanged screen ⇒ zero transfer)
    #       thumb=1&w=240 — a server-side JPEG thumbnail (≤240px default)
    @app.route("/api/app/screenshot")
    def app_screenshot():
        from flask import Response, jsonify, request
        sid = request.args.get("sid") or state.sid
        want_thumb = request.args.get("thumb") == "1"
        try:
            max_px = min(640, max(64, int(request.args.get("w", 240) or 240)))
        except ValueError:
            max_px = 240
        ref = request.args.get("ref") or ""
        client_hash = request.args.get("h") or ""

        if ref:
            sess = store.get(sid)
            art = sess.artifacts.get(ref) if sess is not None else None
            if art is None:
                return jsonify({"ok": False,
                                "error": f"unknown artifact {ref!r}"}), 404
            data, mime = art.data, art.mime
            if want_thumb:
                thumb = state.thumbnail(data, max_px)
                if thumb is not None:
                    data, mime = thumb, "image/jpeg"
            return _shot_response(data, mime, fp=artifact_fingerprint(art.data),
                                  cacheable=True)

        shot = state.screenshot(sid)
        if shot is None:
            return jsonify({"ok": False, "error": "no screenshot yet"}), 404
        mime, data, _, fp = shot
        if client_hash and client_hash == fp:
            # unchanged screen — zero-body 200 carries only the hash header
            return Response(b"", mimetype=mime,
                            headers={"X-Shot-Hash": fp,
                                     "X-Shot-Same": "1",
                                     "Cache-Control": "no-cache"})
        if want_thumb:
            thumb = state.thumbnail(data, max_px)
            if thumb is not None:
                data, mime = thumb, "image/jpeg"
        return _shot_response(data, mime, fp=fp, cacheable=False)

    # ── GET /api/app/surface_shots — the A9.2 screenshot wall's feed ───
    #     One card per world surface; URLs are pre-assembled server-side
    #     so the client never touches internal ids (repo contract §3).
    @app.route("/api/app/surface_shots")
    def app_surface_shots():
        from flask import jsonify
        return jsonify({
            "ok": True,
            "entries": _surface_shot_entries(state),
            "surfaces": [{"name": s["name"],
                          "role": ("foreground" if i == 0 else "background")}
                         for i, s in enumerate(state.surfaces)],
        })

    # ── POST /api/app/governance/<command> — the island's optimistic ───
    #     first-response proxy (A9.1). Zero model calls by construction;
    #     the command set + statuses mirror the FROZEN projection routes
    #     (single-owner driver path), so the island's Start button is no
    #     longer local-only theater. "No reaction to Start" is now a
    #     real HTTP round trip measured in single-digit ms.
    @app.route("/api/app/governance/<command>", methods=["POST"])
    def app_governance(command: str):
        from flask import jsonify, request
        if command not in _APP_GOV_COMMANDS:
            return jsonify({"ok": False, "action": command,
                            "error": f"unknown governance command "
                                     f"{command!r}"}), 400
        sess = store.get(state.sid)
        if sess is None:
            return jsonify({"ok": False, "action": command,
                            "error": "任务世界尚未就绪（还没有会话）"}), 409
        body = request.get_json(silent=True) or {}
        label = str(body.get("label", "") or "").strip()
        driver = _lazy_driver(sess)
        try:
            if command in ("start", "pause", "resume", "stop"):
                if command == "start":
                    if getattr(sess.kernel, "pending_recompose",
                               None) is not None:
                        return jsonify({
                            "ok": False, "action": "start",
                            "error": "任务目标已变更，等待重新编排后才能继续"
                                     "执行"}), 409
                if driver is None:
                    return jsonify({
                        "ok": False, "action": command,
                        "error": "本会话未注册运行时，无法执行"}), 409
                driver_state = {
                    "start": driver.start, "pause": driver.pause,
                    "resume": driver.resume,
                    "stop": driver.stop}[command]()
                if driver_state == "stopped" and command in (
                        "start", "resume"):
                    return jsonify({
                        "ok": False, "action": command,
                        "state": driver_state,
                        "error": "会话已停止（stop 是持久状态），需要新的"
                                 "任务"}), 409
                return jsonify({"ok": True, "action": command,
                                "state": driver_state})
            if command == "checkpoint":
                if not label:
                    _n = len(sess.kernel.checkpoints() or ()) + 1
                    label = f"检查点 {_n}"
                result = sess.governance_port().checkpoint(label)
                return jsonify({"ok": True, "action": "checkpoint",
                                "result": result}), 201
            # rollback: the user-visible checkpoint LABEL (the island
            # never sees checkpoint ids — repo contract §3)
            if not label:
                return jsonify({"ok": False, "action": "rollback",
                                "error": "缺少检查点标签"}), 400
            latest = None
            for c in (snapshot_view(sess).get("checkpoints") or []):
                if c.get("label") == label:
                    latest = c.get("checkpoint_id")
            if not latest:
                return jsonify({"ok": False, "action": "rollback",
                                "error": f"没有名为 {label!r} 的检查点"}), 404
            result = sess.governance_port().rollback(
                latest, rationale=str(body.get("rationale", "") or ""))
            plan = result.pop("plan", None)
            if plan is not None and driver is not None:
                result["disposition"] = driver.execute_compensation(plan)
            return jsonify({"ok": True, "action": "rollback",
                            "result": result})
        except TaskVMError as e:
            return jsonify({"ok": False, "action": command,
                            "error": f"{type(e).__name__}: {e}"}), \
                _taskvm_error_status(e)


def _discover_surfaces(session: Any) -> tuple[dict[str, str], ...]:
    """The world's surfaces, discovered through the substrate port (the
    provider's own naming — never a hardcoded app list)."""
    try:
        infos = session.list_surfaces()
    except Exception:
        return ()
    return tuple({"id": i.surface_id, "name": (i.display_name
                                               or i.surface_id)}
                 for i in infos)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m taskvm.workspace_ui.app_open",
        description="TaskVM APP — empty-state Codex-like shell; the first "
                    "user instruction (POST /api/app/goals) bootstraps the "
                    "real pipeline on the MobileGym substrate")
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind host (default 0.0.0.0 — reachable from "
                         "your laptop via port forwarding)")
    ap.add_argument("--port", type=int, default=3016,
                    help="APP UI port (default 3016)")
    ap.add_argument("--sid", default="app",
                    help="the bridge-activated session id the APP runs on "
                         "(default: app; scripts/app_mobilegym.sh resets "
                         "this sid once, up-front)")
    ap.add_argument("--bridge-url", default=None,
                    help="bridge base URL (default http://127.0.0.1:--bridge-port)")
    ap.add_argument("--bridge-port", type=int, default=3019,
                    help="bridge port (default 3019)")
    ap.add_argument("--sim-url", default="http://localhost:3000",
                    help="Vite sim URL (default http://localhost:3000)")
    ap.add_argument("--start-bridge", action="store_true",
                    help="spawn the bridge subprocess (closed whitelist) "
                         "if no healthy bridge is found; the APP "
                         "owns and kills only the bridge IT spawned")
    ap.add_argument("--model", default=None,
                    help="model override (default TASKVM_MODEL env or the "
                         "port default gpt-5.6-sol)")
    ap.add_argument("--initial-app", default="",
                    help="runtime capability: the substrate's initial "
                         "foreground surface for goal sessions (default "
                         "TASKVM_INITIAL_APP env, else the substrate "
                         "provider's own default). This is environment "
                         "config, NOT a task-UI semantic (workplan §2).")
    ap.add_argument("--offline", action="store_true",
                    help="honest-FAIL placeholder CUA (compiler/architect "
                         "still need OPENAI_API_KEY)")
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    bridge_url = (args.bridge_url
                  or f"http://127.0.0.1:{args.bridge_port}").rstrip("/")
    initial_app = (args.initial_app
                   or os.environ.get("TASKVM_INITIAL_APP", "")).strip()

    spawned_bridge = None
    try:
        # fail-closed bridge glue — identical semantics to demo_open
        spawned_bridge = _ensure_mobilegym_bridge(args)

        # one read-only observe() BEFORE serving: names what is missing
        # (sim down / sid not activated) instead of a broken empty APP.
        # The session is created WITHOUT an app override — the substrate
        # provider's own default is the environment's choice.
        probe_cfg: dict[str, Any] = {"sid": args.sid,
                                     "bridge_url": bridge_url}
        if initial_app:
            probe_cfg["app"] = initial_app
        session = substrate_registry.create_session(
            "mobilegym", probe_cfg)
        _probe_observe(session, args.sid, bridge_url)
        surfaces = _discover_surfaces(session)
        # seed the live phone with the world as it is RIGHT NOW (bottom-up
        # projection, principle 1) — one observe at startup, its screenshot
        # becomes the first frame of the side channel.
        try:
            obs = session.observe(session.list_surfaces()[0])
            ref = getattr(obs, "screenshot_ref", None)
            if isinstance(ref, str) and ref.startswith("data:image/"):
                head, b64 = ref.split(",", 1)
                import base64
                mime = head[5:].split(";", 1)[0] or "image/png"
                state_shot = (mime, base64.b64decode(b64))
            else:
                state_shot = None
        except Exception:
            state_shot = None

        store = ProjectionSessionStore()          # EMPTY — the hero state
        a2ui = A2uiTransport(session_lookup=store.get)
        state = AppState(store, sid=args.sid, bridge_url=bridge_url,
                         sim_url=args.sim_url, model=args.model,
                         surfaces=surfaces, initial_app=initial_app,
                         offline=args.offline, a2ui=a2ui)
        if state_shot is not None:
            state.push_screenshot(args.sid, state_shot[0], state_shot[1])
        app = serve(store)
        register_app_routes(app, store, state)
        register_a2ui_routes(app, a2ui, store, state)

        print("=" * 62)
        print("  TaskVM APP (empty state — no goal yet)")
        print(f"  →  http://{args.host}:{args.port}")
        print(f"  world: mobilegym sid '{args.sid}' · bridge {bridge_url}")
        print(f"  sim (watch the phone): {args.sim_url}")
        print(f"  surfaces discovered: "
              f"{', '.join(s['id'] for s in surfaces) or '(none)'}")
        print(f"  model: {args.model or os.environ.get('TASKVM_MODEL', 'gpt-5.6-sol')}"
              f"{' (OFFLINE CUA)' if args.offline else ''}")
        print("  the first instruction you type in the browser runs the")
        print("  REAL pipeline (StateCompiler → TaskArchitect → kernel →")
        print("  AutonomyRuntime) — same bootstrap_real_full as the bench.")
        print("  compiled to Ready; autonomy starts ONLY on the user's")
        print("  explicit Start (frozen POST /governance/start).")
        print("  A2UI island (real stream): http://"
              f"{(args.host if args.host != '0.0.0.0' else 'localhost')}"
              f":{args.port}/a2ui")
        if spawned_bridge is not None:
            print("  bridge: SPAWNED by this APP (killed on exit)")
        print("=" * 62)

        app.run(host=args.host, port=args.port, threaded=True,
                debug=False, use_reloader=False)
    finally:
        if spawned_bridge is not None:
            spawned_bridge.terminate()
            try:
                spawned_bridge.wait(timeout=10)
            except subprocess.TimeoutExpired:
                spawned_bridge.kill()
                spawned_bridge.wait(timeout=5)


if __name__ == "__main__":
    main()
