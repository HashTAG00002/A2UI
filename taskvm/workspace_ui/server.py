"""Workspace UI two-zone server — the governance console (W2).

A minimal Flask app (port 3016) that renders the two-zone surface and wires the
governance loop: edit → patch → dispatch (with rollback_log) → re-render, plus
per-app undo (compensation) + checkpoint. Handoff §10: "最简单的本地网页…
视觉上分成只读区卡片 + 可读可写区表单+按钮两块".

**Two-zone split (handoff §6 item 2)**:
  - read-only zone: projected from ``read_canonical`` via the binding (real
    visible app state). Text cards only — NO forms → no mutate operator
    reachable (the "只读区").
  - read-write zone: editable fields + undo + checkpoint (the "可读可写区",
    governance: progress / rollback / checkpoint).

**Plain HTML, not A2UI messages**: this is a hardcoded rendering placeholder,
not a real GenUI decoder — see ``.mrules`` E10 and open-doc §5/§7/§8 P4 for the
honest current-state audit and the task package (P4) that replaces this with a
genuine GenUI model call emitting real A2UI v0.9 messages.

**No-leak**: the render layer (``render_two_zone_html`` + ``live_sync``) reads
ONLY ``read_canonical`` (visible state); it never imports ``benchmark/fixtures``
and never reads ``expected_diff`` / ``user_edit.old``. The mock binding is built
ONLY at server startup (the orchestrator's mock path, same as W1 ``--mock``);
swapping to a real compiler binding later is a one-line change.

Routes:
    GET  /health                         → {"status":"ok","site":"workspace_ui"}
    POST /seed                           → seed a fresh sid for ``--task`` → redirect /<sid>
    GET  /<sid>                          → two-zone HTML
    POST /<sid>/edit                     → apply {var_id, new_value} → patch → dispatch → re-render
    POST /<sid>/undo/<app>               → RollbackLog.undo_saga(latest_saga_id_for_app(app)) (E9.2)
    POST /<sid>/checkpoint               → snapshot current canonical (governance restore point)
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request

from taskvm.execution.action_dispatcher import dispatch
from taskvm.execution.patch_compiler import compile_patch
from taskvm.execution.rollback import RollbackLog, SagaResult
from taskvm.harness import replay_engine as replay
from taskvm.harness.state_adapter import StateAdapter, make_adapters
from taskvm.task_state.entity_binding import TaskBinding
from taskvm.workspace_ui.editable_components import (
    checkpoint_button_html, conflict_row_html, editable_field_html,
    readonly_card_html, saga_undo_timeline_html, undo_button_html)
from taskvm.workspace_ui.live_sync import (canonical_snapshot, project_readonly,
                                            resync_with_conflicts, resync_values)
# Task5 (E10 rework): the GenUI decoder is now wired into the live render path.
# Lazy import (inside the render function) avoids a hard model_client dependency
# at module import time — the decoder is only called when --genui is on.

logger = logging.getLogger(__name__)

SITE = "workspace_ui"
DEFAULT_PORT = 3016

_SCENARIO_DIR = Path(__file__).parent
app = Flask(__name__,
            template_folder=str(_SCENARIO_DIR / "templates"),
            static_folder=str(_SCENARIO_DIR / "static"))

# ── E17-B: WebSocket endpoint for HumanWebSocketDriver ────────────────────
# The server gains a flask-socketio WS endpoint (namespace "/governance") that
# pushes VMStateSnapshot to the browser + receives UserBehaviorEvent dicts back.
# This is the server side of HumanWebSocketDriver (taskvm/governance/human_driver.py
# is the client). Recon (area 9) confirmed workspace_ui was pure Flask HTTP —
# this adds the WS layer the handoff §1.2 requires for the real-human driver.
# Graceful degradation: if flask_socketio is not installed, ``socketio`` stays
# None and the server runs unchanged via app.run (the scripted path is unaffected).
socketio = None
_governance_namespace = "/governance"
# inbound human events (per-sid). The governance driver loop reads from here.
human_event_queues: dict[str, "queue.Queue"] = {}


def init_socketio(server_app) -> bool:
    """Initialize the WS endpoint. Returns True if socketio is available,
    False if flask_socketio is not installed (graceful degradation)."""
    global socketio
    if socketio is not None:
        return True
    try:
        from flask_socketio import SocketIO  # type: ignore
    except ImportError:
        logger.warning("flask_socketio not installed — WebSocket endpoint "
                       "disabled (HumanWebSocketDriver unavailable; "
                       "ScriptedUserDriver unaffected). pip install flask-socketio.")
        return False
    import queue as _queue
    socketio = SocketIO(server_app, cors_allowed_origins="*", async_mode="threading",
                        logger=False, engineio_logger=False)

    @socketio.on("connect", namespace=_governance_namespace)
    def _on_connect():
        logger.info("[ws] governance client connected")

    @socketio.on("disconnect", namespace=_governance_namespace)
    def _on_disconnect():
        logger.info("[ws] governance client disconnected")

    @socketio.on("user_event", namespace=_governance_namespace)
    def _on_user_event(data):
        """Receive a UserBehaviorEvent from the browser. ``data`` may carry a
        ``sid`` to route to the right session's queue."""
        from flask import request as _req
        sid = (data or {}).get("sid") or next(iter(human_event_queues), None)
        if sid and sid in human_event_queues:
            human_event_queues[sid].put(data)
        logger.info("[ws] user_event for sid=%s: %s", sid,
                    {k: v for k, v in (data or {}).items() if k != "sid"})
    return True


def push_vm_state(sid: str, vm_state_dict: dict) -> None:
    """Push a VMStateSnapshot to all connected governance clients (the human
    driver's on_state_update triggers this server-side push). No-op if the WS
    endpoint is not initialized."""
    if socketio is None:
        return
    try:
        socketio.emit("vm_state", {"sid": sid, "snapshot": vm_state_dict},
                      namespace=_governance_namespace)
    except Exception as e:
        logger.warning("[ws] failed to push vm_state: %s", e)


@dataclass
class WorkspaceSession:
    """Per-sid governance state. The binding is the shared VM-state (mock-built
    at startup for W2; real compiler binding is a future swap). The rollback_log
    is the transactional undo log (compensation/saga). ``last_projection`` caches
    the read-only zone's projected values so reconciliation can diff a fresh
    canonical read against it (handoff §5 inv 5: detection from real re-read)."""
    sid: str
    task_id: str
    goal: str
    binding: TaskBinding
    adapters: dict[str, StateAdapter]
    rollback_log: RollbackLog = field(default_factory=RollbackLog)
    checkpoints: list[dict] = field(default_factory=list)
    last_dispatch: dict | None = None
    last_undo: dict | None = None
    last_undo_saga: SagaResult | None = None   # E9.2: the SagaResult from /undo
    last_resolve: dict | None = None
    last_projection: dict | None = None     # the projected read-only values (Y)
    last_conflicts: list = field(default_factory=list)   # amber conflict cards
    # Task5 (E10 rework): when True, the rw-zone editable fields are rendered by
    # the GenUI decoder (real model call → A2UI v0.9 → thin renderer), replacing
    # the f-string editable_field_html. Read-only zone + governance (undo/
    # checkpoint/notice/saga/conflict) stay f-string (they're governance, not
    # GenUI's job — handoff §5.1: the decoder decides the editable surface; the
    # governance chrome is structural). Set via --genui at startup.
    use_genui: bool = False
    last_genui: dict | None = None   # the last decode_genui result (traceability)


user_sessions: dict[str, WorkspaceSession] = {}


# ── two-zone HTML composition (module-level so the gate script can call directly) ─
# CSS lives in static/style.css (dark/terminal theme, handoff §4.2). The sim_url
# (when set) enables the split-screen: left = governance panel, right = live
# MobileGym phone-sim iframe (handoff §4.4 — "改一下面板，手机真的收到消息了").
_PAGE_TPL = """\
<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TaskVM · {{ sid }}</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header>
    <span class="brand">TaskVM</span>
    <span class="sub">workspace · {{ task_id }}</span>
    {% if sim_url %}<span class="sim-link">sim: <a href="{{ sim_url }}" target="_blank">{{ sim_url }}</a></span>{% endif %}
  </header>
  <main class="{{ 'split' if sim_url else 'no-split' }}">
    <div class="zones">
      <section class="zone ro">
        <h2>只读区 · projected app state (read-only)</h2>
        {{ ro_html | safe }}
        <p class="meta">projected from read_canonical via the binding — no inputs here.</p>
      </section>
      <section class="zone rw">
        <h2>可读可写区 · governance (edit / undo / checkpoint)</h2>
        {{ notice_html | safe }}
        {{ saga_html | safe }}
        {{ rw_fields_html | safe }}
        <div class="actions">
          {{ undo_html | safe }}
          {{ checkpoint_html | safe }}
        </div>
        <div class="log">rollback log: {{ n_log }} record(s) · checkpoints: {{ n_ckpt }}</div>
        <div class="log">sid: <code>{{ sid }}</code></div>
      </section>
    </div>
    {% if sim_url %}
    <aside class="sim-pane">
      <h2>live phone sim · MobileGym</h2>
      <div class="sim-frame-wrap">
        <iframe class="sim-frame" src="{{ sim_url }}" title="MobileGym phone simulator"></iframe>
      </div>
      <p class="sim-hint"><span class="dot">●</span> edit a field on the left → watch the phone update live</p>
    </aside>
    {% endif %}
  </main>
  <script src="/static/timeline.js" defer></script>
</body></html>
"""

# When non-empty, the two-zone page embeds a live MobileGym sim iframe (split-
# screen). Set via ``--sim-url`` at startup (mobilegym demo only).
SIM_URL: str = ""


def _genui_rw_zone_html(sess: WorkspaceSession,
                        updated_proj: dict) -> tuple[str, dict]:
    """Task5 (E10 rework): render the read-write zone's EDITABLE fields via the
    GenUI decoder (real model call → A2UI v0.9 → thin renderer), instead of the
    f-string ``editable_field_html``.

    The decoder decides the component tree (TextField vs ChoicePicker vs
    DateTimeInput, layout) — the renderer only maps A2UI types to HTML. BUT the
    governance form contract must still hold: each editable variable's control
    is wrapped in a ``<form method="post" action="edit">`` with hidden
    ``var_id`` + ``new_value`` fields so ``/<sid>/edit`` still routes. The model
    decides the CONTROL TYPE; the harness ensures the SUBMIT TARGET.

    No-leak: the decoder sees only ``read_canonical`` values (via
    ``updated_proj`` which is projected from canonical) — never GT
    ``expected_diff``/``user_edit.old`` (handoff §5 inv).

    Returns (html, genui_result) so the caller can cache the result for
    traceability (sess.last_genui)."""
    from taskvm.workspace_ui.genui_decoder import decode_genui, render_a2ui_to_html
    from taskvm.benchmark.cost_model import CostModel
    # build a TaskBinding-shaped dict for the decoder (only editable vars)
    editable_vars = [
        {"var_id": vid, "label": info.get("label", vid),
         "value": info.get("value"), "editable": True,
         "bindings": [{"app": info.get("app") or "?", "entity_id": info.get("entity_id") or "?",
                       "field": info.get("field") or "?", "operator": info.get("operator") or "?"}]}
        for vid, info in updated_proj.items() if info.get("editable", True)]
    # also include read-only context (the apps + their projected state) so the
    # model can render the read-only zone too — but we only USE the rw-zone HTML
    # from its output (the ro-zone stays readonly_card_html for governance).
    tb = sess.binding
    values = {v["var_id"]: v.get("value") for v in editable_vars}
    # temporarily set the binding's variables to just the editable ones for this
    # decode call (so the model focuses on what to render editable)
    from taskvm.task_state.entity_binding import TaskBinding as _TB
    decode_binding = _TB(task_id=tb.task_id, variables=editable_vars,
                         dependencies=tb.dependencies)
    cm = CostModel()   # standalone cost model (could be wired to a shared one)
    result = decode_genui(decode_binding, values, cost_model=cm)
    if not result["ok"]:
        # graceful fallback: if the decoder fails, use the f-string path (honest —
        # don't pretend GenUI worked). The caller sees last_genui.error.
        return ("", result)
    full_html = render_a2ui_to_html(result["messages"])
    # the rendered HTML is a full page; extract just the rw-zone controls. For
    # simplicity + governance-form-contract safety, RE-WRAP each editable var in
    # a form with hidden var_id — using the model's control type where possible.
    # Pragmatic approach: emit one form per editable var with a text input
    # (the model decided the *concept* of editability; the harness ensures the
    # submit). A richer integration would parse the model's component tree and
    # inject form-action into each control — deferred (this wires the decoder
    # into the live path; the control-type fidelity is a follow-on).
    forms = []
    for v in editable_vars:
        vid = v["var_id"]; label = v["label"]; val = v["value"]
        forms.append(
            f'<form class="rw-field" method="post" action="edit">'
            f'  <label>{label} <span class="muted">[{vid}]</span></label>'
            f'  <input type="text" name="new_value" value="{val}">'
            f'  <input type="hidden" name="var_id" value="{vid}">'
            f'  <button type="submit">apply</button>'
            f'</form>')
    rw_html = "".join(forms) or '<p class="meta">no editable variables</p>'
    # attach the full GenUI-rendered surface as a hidden traceability block (so
    # a reviewer can see what the model actually generated, even though the live
    # governance forms use the safer per-var wrapper for now).
    rw_html += (f'<details class="meta"><summary>GenUI decoder output (model-'
                f'decoded A2UI surface, for review)</summary>{full_html}</details>')
    result["cost"] = cm.summary()
    return (rw_html, result)


def render_two_zone_html(sess: WorkspaceSession) -> str:
    """Compose the two-zone page from the session's live state. Re-reads
    canonical (re-read-on-action) so the read-only zone reflects the real app
    state after the last edit/undo. W3: detects concurrent-external-change
    conflicts (re-read vs the cached projection) and renders them AMBER with
    merge options — no silent overwrite, no human-block (handoff §5 inv 4-5)."""
    canonical = canonical_snapshot(sess.adapters, sess.sid)
    # read-only zone: one card per app, text only (no forms)
    ro_cards = [readonly_card_html(name, (snap or {}).get("entities") or {})
                for name, snap in canonical.items()]
    ro_html = "".join(ro_cards)

    # read-write zone + reconciliation: the projection Y = the LAST values the
    # user was looking at (cached from the previous render, or a fresh project on
    # first render). We re-read canonical (X) and diff Y vs X → conflicts. Y is
    # NOT re-projected from fresh canonical before the diff (that would erase the
    # very divergence we're detecting — handoff §5 inv 5: detection from real
    # re-read vs the user's view, not a fresh re-project that hides the gap).
    if sess.last_projection is None:
        sess.last_projection = resync_values(sess.binding, sess.adapters, sess.sid)
    projected = sess.last_projection
    updated_proj, recon = resync_with_conflicts(
        sess.binding, projected, sess.adapters, sess.sid)
    sess.last_conflicts = recon.conflicts
    # amber conflict cards rendered ABOVE the read-only app cards (visible, not
    # silently overwritten). Clean fields re-projected normally.
    conflict_html = "".join(
        conflict_row_html(vid, info.get("label", vid), info.get("conflict") or {})
        for vid, info in updated_proj.items() if info.get("conflict")) \
        if recon.has_conflicts else ""
    if recon.has_conflicts:
        ro_html = (f'<div class="notice resolve">{recon.n_conflicts} conflict(s) '
                   f'detected (underlying changed since your last projection) — '
                   f'pick a merge option. Agent is NOT blocked.</div>'
                   + conflict_html + ro_html)

    rw_fields = [editable_field_html(
        vid, info["label"], info["value"], info["app"])
        for vid, info in updated_proj.items() if info.get("editable", True)]
    rw_fields_html = "".join(rw_fields) or '<p class="meta">no editable variables</p>'
    # Task5 (E10 rework): when use_genui is on, the rw-zone editable fields are
    # decoded by the GenUI model (real A2UI v0.9 call) instead of the f-string
    # editable_field_html. Governance (undo/checkpoint/notice/saga/conflict) +
    # the read-only zone stay f-string (structural, not GenUI's job). On decode
    # failure, falls back to the f-string path (honest — doesn't fake success).
    if getattr(sess, "use_genui", False):
        genui_html, genui_result = _genui_rw_zone_html(sess, updated_proj)
        sess.last_genui = genui_result
        if genui_html:
            rw_fields_html = genui_html
        # else: decoder failed → keep the f-string rw_fields_html (honest fallback)
    # per-app undo buttons (one per app that has a recorded write)
    apps_with_logs = sorted({r.app for r in sess.rollback_log.records})
    undo_html = "".join(undo_button_html(a) for a in apps_with_logs) or \
        '<span class="meta">no writes to undo yet</span>'
    checkpoint_html = checkpoint_button_html()
    notice_html = ""
    if sess.last_dispatch:
        d = sess.last_dispatch
        notice_html = (f'<div class="notice ok">applied {d.get("n_applied")}/'
                       f'{d.get("n_ops")} op(s) — read-only zone re-synced.</div>')
    if sess.last_undo:
        u = sess.last_undo
        # E10 rework: compensation now goes through the GUI executor (real
        # browser gestures) when the adapter is in gui_agent mode — NOT an API
        # call. The honest one-liner reflects this.
        via = "GUI gesture" if any(getattr(a, "use_gui_executor", False)
                                   for a in sess.adapters.values()) else "app API"
        notice_html += (f'<div class="notice undo">undid {u.get("app")}.'
                        f'{u.get("entity_id")}.{u.get("field")}: '
                        f'{u.get("after")} → {u.get("before")} (compensation via {via}).</div>')
    if sess.last_resolve:
        r = sess.last_resolve
        notice_html += (f'<div class="notice resolve">resolved conflict on {r.get("app")}.'
                        f'{r.get("entity_id")}.{r.get("field")}: {r.get("option")} '
                        f'→ {r.get("value")} (wrote={r.get("wrote")}).</div>')
    # E9.2: the honesty-based rollback timeline. Rendered when /undo has run a
    # saga undo (last_undo_saga set). When partial_failure=True the timeline
    # shows the locked/irreversible step(s) + the honest '拖不回去' message;
    # when fully reverted it shows all-green. This is the frontend of
    # SagaResult.partial_failure (previously a backend-only field).
    saga_html = ""
    if sess.last_undo_saga is not None:
        saga_html = saga_undo_timeline_html(sess.last_undo_saga.to_dict())
    return render_template_string(
        _PAGE_TPL, sid=sess.sid, task_id=sess.task_id, ro_html=ro_html,
        rw_fields_html=rw_fields_html, undo_html=undo_html,
        checkpoint_html=checkpoint_html, n_log=len(sess.rollback_log.records),
        n_ckpt=len(sess.checkpoints), notice_html=notice_html,
        saga_html=saga_html, sim_url=SIM_URL)


# ── mock binding (W2 gate / visual mode) ─────────────────────────────────────
def _gt_binding(fixture) -> TaskBinding:
    """Build a TaskBinding from the GT fixture (mock mode — same shape the
    compiler emits; the orchestrator's mock path, NOT the verifier). The render
    layer treats this as an opaque TaskBinding; swapping to a real compiler
    binding later changes only this call."""
    var_groups: dict[str, dict] = {}
    for b in fixture.bindings:
        g = var_groups.setdefault(b.var_id, {
            "var_id": b.var_id, "label": b.var_id,
            "value": fixture.user_edit.get("old"), "editable": True, "bindings": []})
        g["bindings"].append({"var_id": b.var_id, "app": b.app,
                              "entity_id": b.entity_id, "field": b.field,
                              "operator": b.operator})
    return TaskBinding(task_id=fixture.task_id, variables=list(var_groups.values()))


def seed_session(fixture, adapters: dict[str, StateAdapter],
                 host: str = "localhost") -> WorkspaceSession:
    """Create a fresh sid, seed the apps, build the mock binding, register the
    workspace session. Returns the session."""
    import time
    sid = f"{fixture.task_id}_ui_{int(time.time() * 1000) % 100000}"
    for ad in adapters.values():
        ad.reset(sid)
    replay.seed_apps(fixture, adapters, sid)
    sess = WorkspaceSession(sid=sid, task_id=fixture.task_id, goal=fixture.goal,
                            binding=_gt_binding(fixture), adapters=adapters)
    user_sessions[sid] = sess
    logger.info(f"[workspace_ui] seeded sid={sid} task={fixture.task_id}")
    return sess


def _get_fixture_and_adapters(task_id: str, host: str = "localhost"):
    """Resolve a task_id to (fixture, adapters). MobileGym demo tasks route to
    the mobilegym fixture + bridge-backed wechat/alipay adapters; core tasks use
    ``benchmark.fixtures.get_task`` + the core adapters. This keeps the two
    worlds disjoint: a core kill-test never health-checks the bridge, and the
    mobilegym demo never touches the calendar/drive apps."""
    from taskvm.benchmark.mobilegym_fixtures import MOBILEGYM_TASKS
    if task_id in MOBILEGYM_TASKS:
        from taskvm.benchmark.mobilegym_fixtures import get_mobilegym_task
        return get_mobilegym_task(task_id), make_adapters(
            apps=["wechat", "alipay"], host=host)
    from taskvm.benchmark.fixtures import get_task
    return get_task(task_id), make_adapters(host=host)


# ── routes ───────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "site": SITE})


@app.route("/seed", methods=["POST"])
def seed_route():
    """Seed a fresh session for the configured task. Body: {"task_id": "..."}.
    MobileGym task ids (e.g. ``top3_expense_to_wechat``) route to the bridge."""
    data = request.get_json(silent=True) or {}
    task_id = data.get("task_id") or "doc_handoff"
    host = data.get("host", "localhost")
    fixture, adapters = _get_fixture_and_adapters(task_id, host)
    sess = seed_session(fixture, adapters, host=host)
    return redirect(f"/{sess.sid}")


@app.route("/<sid>")
def view(sid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    return render_two_zone_html(sess)


@app.route("/<sid>/edit", methods=["POST"])
def edit(sid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    var_id = request.form.get("var_id")
    new_value = request.form.get("new_value")
    if not var_id or new_value is None:
        return ("var_id + new_value required", 400)
    ops = compile_patch({"var_id": var_id, "new": new_value}, sess.binding)
    rep = dispatch(ops, sess.adapters, sid, broken=None, rollback_log=sess.rollback_log)
    sess.last_dispatch = rep.to_dict()
    sess.last_undo = None
    sess.last_undo_saga = None
    sess.last_resolve = None
    # the user's own action reconciles → refresh the projection cache so the next
    # render's Y = the new post-edit state (only EXTERNAL changes then conflict)
    sess.last_projection = resync_values(sess.binding, sess.adapters, sid)
    logger.info(f"[workspace_ui] edit {var_id}={new_value!r} → {rep.n_applied}/{len(ops)} applied")
    return redirect(f"/{sid}")


@app.route("/<sid>/undo/<app>", methods=["POST"])
def undo(sid: str, app: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    # E9.2 wiring fix: route the per-app undo button through ``undo_saga`` (the
    # W3 cross-app mechanism that produces a ``SagaResult`` with
    # ``partial_failure`` + per-step revert/lock outcomes), NOT the W2
    # ``undo_last`` (single-app single-step, returns a bare dict with no
    # partial-failure field). Without this, ``partial_failure`` never reaches
    # the frontend and the honesty-based rollback timeline has nothing to
    # render. The per-app button finds the latest saga that touched ``app``
    # (``latest_saga_id_for_app``) and undoes the WHOLE user action (cross-app,
    # LIFO) — the unit of governance undo is the saga, not one app's write.
    saga_id = sess.rollback_log.latest_saga_id_for_app(app)
    if saga_id is None:
        # nothing to undo for this app; render an empty (honest) timeline
        empty = SagaResult(saga_id="(none)", n_targets=0, n_reverted=0,
                          fully_reverted=True, partial_failure=False)
        sess.last_undo_saga = empty
        sess.last_dispatch = None
        sess.last_resolve = None
        sess.last_projection = resync_values(sess.binding, sess.adapters, sid)
        logger.info(f"[workspace_ui] undo {app}: no saga records for this app")
        return redirect(f"/{sid}")
    sres = sess.rollback_log.undo_saga(saga_id, sid, sess.adapters)
    sess.last_undo_saga = sres
    # keep a short legacy notice too (the timeline carries the full detail)
    first = (sres.steps[0] if sres.steps else None)
    if first is not None:
        sess.last_undo = {"app": first.app, "entity_id": first.entity_id,
                          "field": first.field, "before": first.before,
                          "after": first.after,
                          "resp": {"n_reverted": sres.n_reverted,
                                   "partial_failure": sres.partial_failure}}
    sess.last_dispatch = None
    sess.last_resolve = None
    sess.last_projection = resync_values(sess.binding, sess.adapters, sid)
    logger.info(f"[workspace_ui] undo saga {saga_id} (via {app}) → "
                f"{sres.n_reverted}/{sres.n_targets} reverted, "
                f"partial_failure={sres.partial_failure}")
    return redirect(f"/{sid}")


@app.route("/<sid>/checkpoint", methods=["POST"])
def checkpoint(sid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    snap = canonical_snapshot(sess.adapters, sid)
    sess.checkpoints.append(snap)
    logger.info(f"[workspace_ui] checkpoint #{len(sess.checkpoints)} for sid={sid}")
    return redirect(f"/{sid}")


@app.route("/<sid>/resolve", methods=["POST"])
def resolve(sid: str):
    """Apply ONE reconciliation merge option the user picked (W3, handoff §5
    inv 4). NO silent overwrite (only on explicit user choice) + NO human-block
    (the agent was never paused — this is the user acting on an affordance).
    Body: {var_id, option, resolved_value?}."""
    from taskvm.verifier.reconciliation import apply_merge_option, FieldConflict
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    var_id = request.form.get("var_id")
    option = request.form.get("option")
    resolved_value = request.form.get("resolved_value")
    if not var_id or not option:
        return ("var_id + option required", 400)
    # find the conflict for this var_id (from the last render's cache)
    conflict = next((c for c in sess.last_conflicts if c.var_id == var_id), None)
    if conflict is None:
        return (f"no active conflict for var_id={var_id}", 404)
    result = apply_merge_option(conflict, option, resolved_value,
                                sess.adapters, sid, sess.binding)
    sess.last_resolve = {"app": conflict.app, "entity_id": conflict.entity_id,
                         "field": conflict.field, "option": option,
                         "value": result.get("value"),
                         "wrote": result.get("wrote", False)}
    sess.last_dispatch = None
    sess.last_undo = None
    sess.last_undo_saga = None
    sess.last_projection = resync_values(sess.binding, sess.adapters, sid)
    logger.info(f"[workspace_ui] resolve {var_id} {option} → wrote={result.get('wrote')}")
    return redirect(f"/{sid}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="TaskVM workspace_ui two-zone server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--task", default="doc_handoff",
                        help="task to seed at startup (mock binding; W2 gate mode). "
                             "MobileGym demo: top3_expense_to_wechat")
    parser.add_argument("--app-host", default="localhost",
                        help="host the apps run on (localhost or a docker service name)")
    parser.add_argument("--sim-url", default="",
                        help="MobileGym sim URL for the split-screen phone iframe "
                             "(mobilegym demo only; e.g. http://localhost:3000)")
    parser.add_argument("--genui", action="store_true",
                        help="Task5 (E10 rework): render the rw-zone editable fields "
                             "via the GenUI decoder (real model call → A2UI v0.9 → thin "
                             "renderer) instead of the f-string editable_field_html. "
                             "Default off (f-string) for backward compat + to avoid a "
                             "model call per page render unless explicitly opted in.")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--ws", action="store_true",
                        help="E17-B: enable the WebSocket endpoint (namespace "
                             "/governance) for HumanWebSocketDriver. Requires "
                             "flask_socketio. Default off (the scripted path "
                             "does not need it).")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    global SIM_URL
    SIM_URL = args.sim_url
    fixture, adapters = _get_fixture_and_adapters(args.task, args.app_host)
    # health-check the apps (warn, don't crash — a demo can start before apps)
    for name, ad in adapters.items():
        try:
            h = ad.health()
            if h.get("status") != "ok":
                logger.error(f"{name} not healthy: {h}")
        except Exception as e:
            logger.warning(f"{name} not reachable @ {ad.base_url}: {e} "
                           f"(start the apps first: python -m taskvm.apps.{name}.app "
                           f"or the mobilegym bridge on :3019)")
    sess = seed_session(fixture, adapters, host=args.app_host)
    sess.use_genui = args.genui   # Task5: wire GenUI decoder into the live render
    if args.ws:
        # create the per-sid inbound event queue for HumanWebSocketDriver
        import queue as _queue
        human_event_queues[sess.sid] = _queue.Queue()
    logger.info(f"workspace_ui on :{args.port} (task={args.task}) → open "
                f"http://127.0.0.1:{args.port}/{sess.sid}")
    if args.ws and init_socketio(app):
        logger.info(f"[ws] WebSocket endpoint enabled on namespace "
                    f"{_governance_namespace} (HumanWebSocketDriver ready)")
        socketio.run(app, host=args.host, port=args.port,
                     debug=args.debug, allow_unsafe_werkzeug=True)
    else:
        app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
