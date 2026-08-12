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
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect, render_template_string,
                   request, stream_with_context)

from taskvm.execution.action_dispatcher import dispatch
from taskvm.execution.patch_compiler import compile_patch
from taskvm.execution.rollback import RollbackLog, SagaResult
from taskvm.harness import replay_engine as replay
from taskvm.harness.state_adapter import StateAdapter, make_adapters
from taskvm.task_state.entity_binding import TaskBinding
from taskvm.workspace_ui.editable_components import (
    checkpoint_button_html, conflict_row_html, editable_field_html,
    milestone_suggest_html, readonly_card_html, saga_undo_timeline_html,
    undo_button_html)
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
# FF.5 §6.3: per-sid workflow_progress pubsub. The /<sid>/edit route (workflow
# path) pushes node-status events here as the WorkflowExecutor walks the plan;
# the /<sid>/poll SSE stream drains them + pushes to the client. workflow_anim.js
# renders the Sequential/Parallel/Loop viz from these events. A list of queues
# (one per subscriber) so multiple poll streams can listen.
_workflow_progress_queues: dict[str, "list[queue.Queue]"] = {}


def subscribe_workflow_progress(sid: str) -> "queue.Queue":
    """Register a new SSE subscriber for workflow_progress events on ``sid``.
    The /<sid>/poll stream calls this once + drains the returned queue."""
    import queue as _queue
    q: "queue.Queue" = _queue.Queue()
    _workflow_progress_queues.setdefault(sid, []).append(q)
    return q


def unsubscribe_workflow_progress(sid: str, q: "queue.Queue") -> None:
    subs = _workflow_progress_queues.get(sid)
    if subs and q in subs:
        subs.remove(q)
        if not subs:
            _workflow_progress_queues.pop(sid, None)


def push_workflow_progress(sid: str, event: dict) -> None:
    """Push a workflow_progress event to ALL subscribers on ``sid`` (the /poll
    streams). Best-effort: a full queue (slow client) drops the event rather
    than blocking the executor — the next event refreshes the viz anyway."""
    subs = _workflow_progress_queues.get(sid, [])
    for q in list(subs):
        try:
            q.put_nowait(event)
        except Exception:
            pass   # full queue → drop (best-effort; the viz refreshes on next)


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
    # FF.1 §2.2 B: the (app, entity_id, field) tuples touched by the most recent
    # edit/resolve/undo. Consumed (read + cleared) by render_two_zone_html AND
    # the GET /<sid>/readonly_partial route to inject the `.changed` class on
    # matching read-only value spans → the value-flash animation plays once per
    # edit. None = no flash pending (subsequent renders don't re-flash).
    last_changed: list[tuple[str, str, str]] | None = None
    # FF.1 §2.2 D / FF.6: the task's predefined milestones (Checkpoint.id +
    # .description), set at seed time. The /<sid>/checkpoint route names a
    # milestone for the celebrate badge by mapping the checkpoint count to
    # this list (C1/C2/…). Empty for tasks with no predefined milestones.
    task_milestones: list = field(default_factory=list)
    # FF.3 §4: LLM-suggested milestones at seed time (governance start point).
    # Each is {id, name, description}. The user ADOPTS zero or more via the
    # /<sid>/adopt_milestone route → id moves to adopted_milestones. Honest
    # degrade: if the LLM call fails (429/timeout), this stays [] (the rw-zone
    # just doesn't show the suggestion block — session works normally).
    suggested_milestones: list[dict] = field(default_factory=list)
    adopted_milestones: list[str] = field(default_factory=list)


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
    <section class="workflow-viz empty" id="workflow-viz">
      <!-- FF.5 §6.1: workflow visualization region. Rendered by workflow_anim.js
           from SSE `workflow_progress` events pushed by the /<sid>/poll stream
           as the WorkflowExecutor walks the plan (Sequential/Parallel/Loop). -->
    </section>
    <div class="zones">
      <section class="zone ro">
        <h2>只读区 · projected app state (read-only)</h2>
        {{ ro_html | safe }}
        <p class="meta">projected from read_canonical via the binding — no inputs here.</p>
      </section>
      <section class="zone rw">
        <h2>可读可写区 · governance (edit / undo / checkpoint)</h2>
        {{ milestone_html | safe }}
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
  <script src="/static/confetti.min.js" defer></script>
  <script src="/static/workflow_anim.js" defer></script>
  <script src="/static/timeline.js" defer></script>
  <script>
    // EE.8: SSE-based dynamic reconciliation polling (§0 property 1 — live
    // reprojection on world-state change, no user trigger needed). When an
    // external concurrent change creates a conflict, the server pushes an
    // `event: conflict` → the page reloads → the read-only zone AMBER-marks it.
    (function () {
      if (typeof EventSource === "undefined") return;  // browser without SSE
      var path = window.location.pathname;
      // only poll on a session view page (/<sid>, not /health or /seed)
      if (path === "/health" || path === "/seed" || path === "/") return;
      var es = new EventSource(path + "/poll");
      es.addEventListener("conflict", function (e) {
        // a concurrent external change happened — reload to show the AMBER mark
        try { var d = JSON.parse(e.data); console.log("[TaskVM SSE] conflict:", d); }
        catch (err) {}
        window.location.reload();
      });
      es.addEventListener("error", function (e) {
        // SSE errors are expected on disconnect/timeout — close + let the next
        // page load re-open. Don't spam the console.
        es.close();
      });
    })();
  </script>
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
    sid = sess.sid   # EE.7: thread sid so the model-decoded controls are form-wired
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
    full_html = render_a2ui_to_html(result["messages"], sid=sid)
    # EE.7: the model-decoded components are now form-wired (sid passed), so the
    # rendered surface IS the live rw-zone — its TextField/DateTimeInput/
    # ChoicePicker post to /<sid>/edit, its undo/checkpoint Buttons post to
    # /<sid>/undo / /<sid>/checkpoint. Use it directly as the rw-zone HTML.
    # Keep a per-var f-string fallback block too (governance-form-contract
    # safety: if the model omitted a binding the user expects, the f-string
    # forms still let them edit it; + a reviewer can see the model's full output).
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
    fallback = "".join(forms) or '<p class="meta">no editable variables</p>'
    rw_html = full_html + (f'<details class="meta"><summary>f-string fallback '
                f'edit forms (if the model omitted a binding)</summary>'
                f'{fallback}</details>')
    result["cost"] = cm.summary()
    return (rw_html, result)


def render_two_zone_html(sess: WorkspaceSession) -> str:
    """Compose the two-zone page from the session's live state. Re-reads
    canonical (re-read-on-action) so the read-only zone reflects the real app
    state after the last edit/undo. W3: detects concurrent-external-change
    conflicts (re-read vs the cached projection) and renders them AMBER with
    merge options — no silent overwrite, no human-block (handoff §5 inv 4-5)."""
    canonical = canonical_snapshot(sess.adapters, sess.sid)
    # FF.1 §2.2 A: card_index → staggered card-enter animation (style.css).
    # FF.1 §2.2 B: changed set (from sess.last_changed, set by edit/resolve/
    #   undo) → .changed class on matching ro-value spans → value-flash anim.
    #   Consumed here (flash fires once per edit; subsequent renders don't).
    changed_set = set(sess.last_changed) if sess.last_changed else None
    ro_cards = [readonly_card_html(name, (snap or {}).get("entities") or {},
                                   card_index=i, changed=changed_set)
                for i, (name, snap) in enumerate(canonical.items())]
    ro_html = "".join(ro_cards)
    sess.last_changed = None   # consume — value-flash fires once per edit

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
    # FF.3 §4: render the LLM-suggested milestones at the rw-zone top. Empty
    # string when there are no suggestions (graceful degrade — the block just
    # doesn't appear; session works normally).
    milestone_html = milestone_suggest_html(
        sess.suggested_milestones, sess.adopted_milestones)
    return render_template_string(
        _PAGE_TPL, sid=sess.sid, task_id=sess.task_id, ro_html=ro_html,
        rw_fields_html=rw_fields_html, undo_html=undo_html,
        checkpoint_html=checkpoint_html, n_log=len(sess.rollback_log.records),
        n_ckpt=len(sess.checkpoints), notice_html=notice_html,
        saga_html=saga_html, milestone_html=milestone_html, sim_url=SIM_URL)


def _suggest_milestones(goal: str, n: int = 3) -> list[dict]:
    """FF.3 §4.1: call the LLM ONCE at seed time to suggest N governance
    milestones for the task goal. Returns a list of {id, name, description}.
    Honest degrade: ANY failure (429/timeout/parse) → [] (the rw-zone simply
    doesn't render the suggestion block; the session works normally).

    Deviation from §4.1's ``temperature=0.3``: gpt-5.6-sol (the default model,
    大纲附录 B.2) is a reasoning model that rejects non-default temperature
    (same lesson as W1/EE — memory taskvm-env-and-proxy-gotchas). Passing 0.3
    would 400 → graceful-degrade to [] every time. We pass ``temperature=None``
    (the reasoning-model-safe default) so the suggestion actually returns. The
    prompt + repair_retries=2 are otherwise exactly §4.1."""
    from taskvm.benchmark.model_client import complete_json
    system = (
        "你是 TaskVM 的 governance 规划助手。给定一个跨多个应用的任务目标，"
        "建议 2-3 个有意义的里程碑 checkpoint，每个 checkpoint 代表任务推进到某个"
        "可以暂停/回退的中间状态。格式：JSON 数组，每项包含 id(C1/C2/C3)、"
        "name(简短中文名)、description(一句话描述达到这个 checkpoint 时的状态)。"
        "不要超过 3 个 checkpoint。不要解释，直接输出 JSON。"
    )
    user = f"任务目标：{goal}"
    try:
        parsed, raw, _resp = complete_json(
            system=system, user=user, max_tokens=400, temperature=None,
            model=None, repair_retries=2)
    except Exception as e:
        logger.warning("[suggest_milestones] LLM call failed (%s); "
                       "graceful-degrading to []", e)
        return []
    # complete_json returns the FIRST balanced {...} object as `parsed` (here a
    # single milestone dict, NOT the array). The model emits a JSON ARRAY of
    # milestones → parse the raw text directly to get all of them (same pattern
    # as genui_decoder._parse_jsonl handling a JSONL stream).
    import json as _json
    arr = None
    try:
        arr = _json.loads(raw) if raw else None
    except Exception:
        arr = None
    if not isinstance(arr, list):
        arr = parsed if isinstance(parsed, list) else (
            parsed.get("checkpoints") if (isinstance(parsed, dict)
                and isinstance(parsed.get("checkpoints"), list)) else None)
    if not isinstance(arr, list):
        return []
    out = []
    for i, m in enumerate(arr[:n]):
        if isinstance(m, dict):
            mid = m.get("id") or f"C{i+1}"
            out.append({"id": str(mid),
                        "name": str(m.get("name") or m.get("description") or mid),
                        "description": str(m.get("description") or "")})
    return out


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
    # FF.1 §2.2 D / FF.6: surface the task's predefined milestones so the
    # /<sid>/checkpoint route can name a celebrate badge (C1/C2/…).
    sess.task_milestones = list(getattr(fixture, "checkpoints", []) or [])
    user_sessions[sid] = sess
    logger.info(f"[workspace_ui] seeded sid={sid} task={fixture.task_id}")
    return sess


def _get_fixture_and_adapters(task_id: str, host: str = "localhost",
                              executor: str = "gui_agent"):
    """Resolve a task_id to (fixture, adapters). MobileGym demo tasks route to
    the mobilegym fixture + bridge-backed wechat/alipay adapters; core tasks use
    ``benchmark.fixtures.get_task`` + the core adapters. This keeps the two
    worlds disjoint: a core kill-test never health-checks the bridge, and the
    mobilegym demo never touches the calendar/drive apps.

    EE.1 (§12.16 backdoor fix): ``executor`` defaults to ``gui_agent`` so the
    demo/server write+rollback path drives a real browser (non-invasive real
    gestures) instead of ``requests.post`` to the app's internal Flask API.
    Pass ``executor='api'`` explicitly for the legacy mock/debug path only."""
    from taskvm.benchmark.mobilegym_fixtures import MOBILEGYM_TASKS
    if task_id in MOBILEGYM_TASKS:
        from taskvm.benchmark.mobilegym_fixtures import get_mobilegym_task
        return get_mobilegym_task(task_id), make_adapters(
            apps=["wechat", "alipay"], host=host, executor=executor)
    from taskvm.benchmark.fixtures import get_task
    fixture = get_task(task_id)
    # FF.1 (honest pre-existing-fix): build the adapter set from the TASK's
    # seed_state apps ∪ binding apps, not ``make_adapters()``'s default
    # (calendar+taskboard+drive). The default omits mail/outlook_cal, so
    # launch_full (needs mail) was serving 4/5 apps (n_applied=4, no mail card)
    # — exposed by FF.1's launch_full render evidence, and a blocker for FF.8
    # (the four-step arc serves launch_full). Same union pattern EE.2 applied
    # to run_w1_killtest. For 3-app tasks (release_reschedule/design_review/
    # doc_handoff) the union is unchanged (byte-identical regression).
    apps = sorted(set(fixture.seed_state.keys())
                  | {b.app for b in fixture.bindings})
    return fixture, make_adapters(apps=apps, host=host, executor=executor)


# ── routes ───────────────────────────────────────────────────────────────────
def _wants_json() -> bool:
    """FF.1: the edit/checkpoint/adopt_milestone routes serve TWO clients —
    the human (HTML form → redirect to the re-rendered page) and the UISimDriver
    / programmatic caller (JSON body). A request is JSON when it sends
    ``format=json`` in the form body OR ``Accept: application/json``. Otherwise
    the route redirects (the browser form-submit flow)."""
    if request.form.get("format") == "json":
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


@app.route("/health")
def health():
    return jsonify({"status": "ok", "site": SITE})


@app.route("/seed", methods=["POST"])
def seed_route():
    """Seed a fresh session for the configured task. Body: {"task_id": "..."}.
    MobileGym task ids (e.g. ``top3_expense_to_wechat``) route to the bridge.

    FF.3 §4: when ``suggest_milestones`` is true (default), call the LLM once
    at seed time to suggest N governance milestones → sess.suggested_milestones.
    The rw-zone renders them as "系统建议的里程碑" cards with an 采纳 button
    (POST /<sid>/adopt_milestone). Honest degrade: a 429/timeout → [] (the
    block just doesn't render). JSON callers (Accept: application/json) get
    {sid, task_id, suggested_milestones} back; HTML callers redirect to /<sid>.
    """
    data = request.get_json(silent=True) or {}
    task_id = data.get("task_id") or "doc_handoff"
    host = data.get("host", "localhost")
    executor = data.get("executor", "gui_agent")   # EE.1: default gui_agent
    suggest = data.get("suggest_milestones", True)
    fixture, adapters = _get_fixture_and_adapters(task_id, host, executor=executor)
    sess = seed_session(fixture, adapters, host=host)
    if suggest:
        sess.suggested_milestones = _suggest_milestones(fixture.goal)
        logger.info(f"[workspace_ui] suggested {len(sess.suggested_milestones)} "
                    f"milestone(s) for sid={sess.sid}")
    if _wants_json():
        return jsonify({"ok": True, "sid": sess.sid, "task_id": sess.task_id,
                        "suggested_milestones": sess.suggested_milestones})
    return redirect(f"/{sess.sid}")


@app.route("/<sid>")
def view(sid: str):
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    return render_two_zone_html(sess)


@app.route("/<sid>/readonly_partial")
def readonly_partial(sid: str):
    """FF.1 §2.2 B + §2.3 item 4: HTMX-style partial refresh — return ONLY the
    read-only zone HTML (app cards) so the frontend can swap it in after an
    edit without a full page reload. Changed fields (from ``sess.last_changed``
    set by /edit, or the projection diff for the SSE-conflict case) get the
    ``.changed`` class so the value-flash animation plays on the swapped-in
    spans. The conflict cards (if any) are rendered above the ro cards.

    Consumes ``sess.last_changed`` (flash fires once per edit). Read-only w.r.t.
    other session state (does not mutate ``last_projection`` — that stays the
    main render's job)."""
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    canonical = canonical_snapshot(sess.adapters, sess.sid)
    changed_set = set(sess.last_changed) if sess.last_changed else None
    ro_cards = [readonly_card_html(name, (snap or {}).get("entities") or {},
                                   card_index=i, changed=changed_set)
                for i, (name, snap) in enumerate(canonical.items())]
    ro_html = "".join(ro_cards)
    sess.last_changed = None   # consume — value-flash fires once per edit
    # surface conflicts (read-only: no sess.last_projection mutation) so a
    # partial refresh after an SSE conflict-push also AMBER-marks.
    if sess.last_projection is not None:
        _updated, recon = resync_with_conflicts(
            sess.binding, sess.last_projection, sess.adapters, sess.sid)
        if recon.has_conflicts:
            conflict_html = "".join(
                conflict_row_html(vid, info.get("label", vid),
                                  info.get("conflict") or {})
                for vid, info in _updated.items() if info.get("conflict"))
            ro_html = (f'<div class="notice resolve">{recon.n_conflicts} '
                       f'conflict(s) detected (underlying changed since your '
                       f'last projection) — pick a merge option.</div>'
                       + conflict_html + ro_html)
    return Response(ro_html, mimetype="text/html")


def _wf_progress_event(plan_type: str, lanes: list[dict], barrier: str) -> dict:
    """FF.5 §6.3: build a workflow_progress SSE event. ``lanes`` = list of
    {idx, app, status} (status: running|done|waiting|locked); ``barrier`` =
    the barrier node's status (waiting|done)."""
    return {"plan_type": plan_type,
            "nodes": [{"idx": i, "type": plan_type, "app": l["app"],
                       "status": l["status"]} for i, l in enumerate(lanes)],
            "barrier_status": barrier}


def _dispatch_edit_workflow(sess: WorkspaceSession, ops, sid: str) -> dict:
    """FF.5 §5.5: when the edit's ops span ≥2 apps, auto-upgrade to a PARALLEL
    workflow (WorkflowExecutor) + push workflow_progress SSE events (lanes
    running → per-lane done → barrier converged). Single-app edits use the
    existing sequential ``dispatch`` (no workflow overhead). Returns the
    dispatch report dict (n_ops, n_applied, + workflow trace for JSON callers).
    """
    apps_in_ops = {op.app for op in ops}
    if len(apps_in_ops) < 2 or len(ops) < 2:
        # single-app / single-op → sequential (no PARALLEL upgrade)
        rep = dispatch(ops, sess.adapters, sid, broken=None,
                       rollback_log=sess.rollback_log)
        return rep.to_dict()
    from taskvm.execution.workflow_executor import WorkflowExecutor
    from taskvm.governance.subgoal import (SubgoalInstruction, WorkflowNode,
                                            WorkflowNodeType, WorkflowPlan)
    subgoals = [SubgoalInstruction(
        natural_language=f"set {op.app}.{op.entity_id}.{op.field} → {op.value}",
        patch_ops=[op]) for op in ops]
    plan = WorkflowPlan(
        task_id=sess.task_id,
        nodes=[WorkflowNode(node_type=WorkflowNodeType.PARALLEL, subgoals=subgoals,
                            display_name="parallel fanout", barrier_label="verifier")],
        workflow_type="parallel")
    lanes = [{"app": op.app, "status": "running"} for op in ops]
    # 1. push "lanes running, barrier waiting"
    push_workflow_progress(sid, _wf_progress_event("parallel", lanes, "waiting"))
    done_idx = []
    def _on_subgoal(r):
        # mark this lane done — find its index by subgoal identity
        for i, sg in enumerate(subgoals):
            if r.subgoal is sg:
                done_idx.append(i); break
        for i in range(len(lanes)):
            lanes[i]["status"] = "done" if i in done_idx else "running"
        push_workflow_progress(sid, _wf_progress_event("parallel", lanes, "waiting"))
    wexec = WorkflowExecutor()
    wres = wexec.execute(plan, sess.adapters, sid, rollback_log=sess.rollback_log,
                         on_subgoal_complete=_on_subgoal)
    # 2. push "lanes done, barrier converged"
    for l in lanes: l["status"] = "done"
    push_workflow_progress(sid, _wf_progress_event("parallel", lanes, "done"))
    n_applied = (sum(r.n_applied for r in wres.nodes[0].subgoal_results)
                 if wres.nodes else 0)
    return {"n_ops": len(ops), "n_applied": n_applied,
            "n_applied_ops": n_applied,
            "workflow": {"type": "parallel", "overall_pass": wres.overall_pass,
                          "nodes": wres.to_dict()["nodes"]}}


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
    # FF.5 §5.5: multi-app edit → PARALLEL workflow (WorkflowExecutor + SSE
    # workflow_progress). Single-app → sequential dispatch (unchanged).
    rep_dict = _dispatch_edit_workflow(sess, ops, sid)
    sess.last_dispatch = rep_dict
    sess.last_undo = None
    sess.last_undo_saga = None
    sess.last_resolve = None
    # FF.1 §2.2 B: record the (app, entity_id, field) tuples this edit touched
    # so the read-only zone flashes them on the next render / readonly_partial.
    # Also exposed as `changed_vars` in the JSON response (FF.2 UISimDriver reads
    # this to verify the GenUI form submit actually reached the binding).
    changed_tuples = [(op.app, op.entity_id, op.field) for op in ops]
    sess.last_changed = changed_tuples or None
    # the user's own action reconciles → refresh the projection cache so the next
    # render's Y = the new post-edit state (only EXTERNAL changes then conflict)
    sess.last_projection = resync_values(sess.binding, sess.adapters, sid)
    n_applied = rep_dict.get("n_applied", rep_dict.get("n_applied_ops", 0))
    logger.info(f"[workspace_ui] edit {var_id}={new_value!r} → "
                f"{n_applied}/{len(ops)} applied")
    if _wants_json():
        return jsonify({
            "ok": True, "sid": sid, "var_id": var_id, "new_value": new_value,
            "n_ops": len(ops), "n_applied": n_applied,
            "changed_vars": [{"app": a, "entity_id": e, "field": f}
                             for a, e, f in changed_tuples],
        })
    return redirect(f"/{sid}")


@app.route("/<sid>/undo", methods=["POST"])
def undo_latest(sid: str):
    """EE.7: generic cross-app undo — undoes the LATEST saga (one user action
    across all apps), no app specified. The GenUI decoder's 'undo' Button posts
    here (it doesn't carry an app). Routes through ``undo_saga`` (SagaResult with
    partial_failure) just like the per-app ``/<sid>/undo/<app>`` route."""
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    saga_id = sess.rollback_log.latest_saga_id()
    if saga_id is None:
        empty = SagaResult(saga_id="(none)", n_targets=0, n_reverted=0,
                          fully_reverted=True, partial_failure=False)
        sess.last_undo_saga = empty
        sess.last_dispatch = None
        sess.last_resolve = None
        sess.last_projection = resync_values(sess.binding, sess.adapters, sid)
        logger.info(f"[workspace_ui] undo (latest): no saga records")
        return redirect(f"/{sid}")
    sres = sess.rollback_log.undo_saga(saga_id, sid, sess.adapters)
    sess.last_undo_saga = sres
    # FF.1 §2.2 B: the reverted fields flash on the next render (value-flash).
    sess.last_changed = [(s.app, s.entity_id, s.field)
                         for s in sres.steps] or None
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
    logger.info(f"[workspace_ui] undo latest saga {saga_id} → "
                f"{sres.n_reverted}/{sres.n_targets} reverted, "
                f"partial_failure={sres.partial_failure}")
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
    # FF.1 §2.2 B: the reverted fields flash on the next render (value-flash).
    sess.last_changed = [(s.app, s.entity_id, s.field)
                         for s in sres.steps] or None
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
    """FF.1 §2.2 D + FF.6 §7.2: record a checkpoint snapshot (governance
    restore point) AND fire the celebration. The response carries
    ``checkpoint_reached`` (FF.1) + ``milestone_reached`` (FF.6 — the {id, name}
    the celebrate badge shows). For the HTML form-submit flow, the redirect
    carries ``?celebrate=<name>`` so timeline.js's ``celebrateCheckpoint`` pops
    the confetti + badge (timeline.js reads the query on DOMContentLoaded)."""
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    snap = canonical_snapshot(sess.adapters, sid)
    sess.checkpoints.append(snap)
    n = len(sess.checkpoints)
    # name the milestone: map the checkpoint count to the task's predefined
    # milestones (C1/C2/…); else a generic "checkpoint N".
    ms = sess.task_milestones[n - 1] if 0 < n <= len(sess.task_milestones) else None
    ms_id = getattr(ms, "id", None) or f"C{n}"
    ms_name = getattr(ms, "description", None) or f"checkpoint {n}"
    milestone_reached = {"id": ms_id, "name": ms_name}
    logger.info(f"[workspace_ui] checkpoint #{n} for sid={sid} (milestone={ms_id})")
    if _wants_json():
        return jsonify({
            "ok": True, "sid": sid, "checkpoint_index": n,
            "checkpoint_reached": True, "milestone_reached": milestone_reached,
        })
    from urllib.parse import quote
    return redirect(f"/{sid}?celebrate={quote(ms_name)}")


@app.route("/<sid>/adopt_milestone", methods=["POST"])
def adopt_milestone(sid: str):
    """FF.3 §4.1: adopt one LLM-suggested milestone → move its id to
    ``sess.adopted_milestones`` (the rw-zone card flips to ✓ adopted). The
    milestone is a governance INTENT marker (the user intends to reach this
    state) — NOT a verifier criterion (the LLM doesn't produce expected_diff;
    §13.3: "milestone 初始化 LLM 调用是建议，不是决定"). FF.6: the response
    carries ``milestone_reached`` so the celebrate badge fires on adoption too.
    """
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)
    # accept milestone_id from JSON body OR form data
    if request.is_json:
        milestone_id = (request.get_json(silent=True) or {}).get("milestone_id")
    else:
        milestone_id = request.form.get("milestone_id")
    if not milestone_id:
        return ("milestone_id required", 400)
    found = next((m for m in sess.suggested_milestones
                 if m.get("id") == milestone_id), None)
    if found is None:
        return (f"no suggested milestone with id={milestone_id}", 404)
    if milestone_id not in sess.adopted_milestones:
        sess.adopted_milestones.append(milestone_id)
    ms_name = found.get("name") or milestone_id
    logger.info(f"[workspace_ui] adopted milestone {milestone_id} ({ms_name}) "
                f"for sid={sid}")
    if _wants_json():
        return jsonify({
            "ok": True, "sid": sid, "milestone_id": milestone_id,
            "adopted": True, "milestone_reached": {"id": milestone_id, "name": ms_name},
        })
    from urllib.parse import quote
    return redirect(f"/{sid}?celebrate={quote(ms_name)}")


@app.route("/<sid>/poll")
def poll(sid: str):
    """EE.8: Server-Sent Events — push conflict updates to the client WITHOUT a
    user action. Implements §0 property 1 "随世界状态变化动态重投影" (the
    projection re-projects on world-state change, not on user trigger). The
    client JS (in _PAGE_TPL) opens an EventSource on this route and reloads the
    page when a conflict is pushed, so the read-only zone AMBER-marks external
    concurrent changes as they happen.

    SSE stream: every ``POLL_INTERVAL_S`` seconds, re-read canonical + run
    resync_with_conflicts (diff vs the cached projection). Push ``event:
    conflict`` with the count if any; ``event: ok`` otherwise. The stream is
    infinite (closed on client disconnect — stream_with_context handles that)."""
    import time
    POLL_INTERVAL_S = 5
    sess = user_sessions.get(sid)
    if sess is None:
        return ("session not found", 404)

    def generate():
        # FF.5: subscribe to the per-sid workflow_progress pubsub so events
        # pushed by /edit's WorkflowExecutor (lanes running → done → barrier)
        # are drained + forwarded to the client. Unsubscribe on disconnect.
        wf_q = subscribe_workflow_progress(sid)
        try:
            while True:
                # 1. drain any workflow_progress events (non-blocking) + push them
                drained = False
                while True:
                    try:
                        ev = wf_q.get_nowait()
                        drained = True
                        yield (f"event: workflow_progress\ndata: "
                               f"{json.dumps(ev, ensure_ascii=False)}\n\n")
                    except Exception:
                        break   # queue empty
                # 2. the regular conflict check (EE.8 — §0 property 1)
                try:
                    if sess.last_projection is None:
                        sess.last_projection = resync_values(sess.binding, sess.adapters, sid)
                    _updated, recon = resync_with_conflicts(
                        sess.binding, sess.last_projection, sess.adapters, sid)
                    if recon.has_conflicts:
                        sess.last_conflicts = recon.conflicts
                        yield (f"event: conflict\ndata: "
                               f"{json.dumps({'n_conflicts': recon.n_conflicts})}\n\n")
                    elif not drained:
                        yield "event: ok\ndata: {}\n\n"
                except Exception as e:
                    logger.warning(f"[poll/{sid}] cycle error: {e}")
                    yield f"event: error\ndata: {json.dumps({'error': str(e)[:120]})}\n\n"
                # if we just drained workflow events, poll again promptly (near
                # real-time per-lane progress); else the regular 5s heartbeat.
                time.sleep(0.2 if drained else POLL_INTERVAL_S)
        finally:
            unsubscribe_workflow_progress(sid, wf_q)

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


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
    # FF.1 §2.2 B: the resolved field flashes on the next render.
    if result.get("wrote"):
        sess.last_changed = [(conflict.app, conflict.entity_id, conflict.field)]
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
    parser.add_argument("--executor", default="gui_agent",
                        choices=["api", "gui_agent"],
                        help="EE.1 (§12.16 backdoor fix): write/rollback executor. "
                             "Default 'gui_agent' — non-invasive real browser gestures "
                             "via the GUI executor (the honest write path). Use 'api' "
                             "ONLY for the legacy mock/debug path (requests.post to the "
                             "app's internal Flask API = the backdoor, not for demo).")
    parser.add_argument("--sim-url", default="",
                        help="MobileGym sim URL for the split-screen phone iframe "
                             "(mobilegym demo only; e.g. http://localhost:3000)")
    parser.add_argument("--genui", dest="use_genui", action="store_true",
                        default=True,
                        help="EE.7: render the rw-zone editable fields via the GenUI "
                             "decoder (real model call → A2UI v0.9 → form-wired thin "
                             "renderer). DEFAULT ON — the model-decoded component IS "
                             "the live governance control (bidirectional §1.2). Use "
                             "--no-genui for the legacy f-string path (mock/debug).")
    parser.add_argument("--no-genui", dest="use_genui", action="store_false",
                        help="disable GenUI; use the legacy f-string editable_field_html.")
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
    fixture, adapters = _get_fixture_and_adapters(args.task, args.app_host,
                                                  executor=args.executor)
    # EE.1: log the executor so it's never silently on the backdoor path. When
    # 'api' is chosen, warn loudly (§12.16: API writes are backdoors, not demo).
    if args.executor == "api":
        logger.warning("executor=api — LEGACY BACKDOOR PATH (requests.post to the "
                       "app's internal Flask API). NOT §12.16-compliant. Use only "
                       "for mock/debug. Demo must run with --executor gui_agent.")
    else:
        logger.info("executor=gui_agent — write/rollback drives a real browser "
                    "(non-invasive gestures, §12.16-compliant).")
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
    sess.use_genui = args.use_genui   # EE.7: default on (--no-genui to disable)
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
