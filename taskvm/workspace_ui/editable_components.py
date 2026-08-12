"""editable_components — HTML widgets for the two-zone governance surface.

W2 scope (handoff §6 item 2 + §10): plain HTML forms (NOT A2UI messages — this
is a hardcoded rendering placeholder, not a real GenUI decoder; see
``.mrules`` E10 and open-doc §5/§7/§8 P4 for the honest current-state audit and
the task package that replaces this with a genuine GenUI model call). The
two-zone split is structural:

  - **read-only zone** (``readonly_card_html``): projected app state as text
    cards. NO forms, NO inputs → no mutate operator is reachable from it. This
    is the "只读区" (app state projection).
  - **read-write zone** (``editable_field_html`` / ``undo_button_html`` /
    ``checkpoint_button_html``): editable inputs + checkpoint + undo buttons.
    This is the "可读可写区" (governance: progress / rollback / checkpoint).

The widgets are pure HTML-string builders (no Flask, no state) so the W2 gate
script can call them directly for JSON assertions, and the Flask server wraps
them for the §10 visual acceptance.
"""
from __future__ import annotations

from typing import Any


def _esc(s: Any) -> str:
    return (str(s) if s is not None else ""
            ).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def readonly_card_html(app: str, entities: dict[str, dict[str, Any]],
                       card_index: int = 0,
                       changed: set[tuple[str, str, str]] | None = None) -> str:
    """One read-only card per app: lists its entities + fields as TEXT only.
    No forms/inputs → no mutate operator reachable (the read-only zone).

    FF.1 §2.2 A: ``card_index`` injects ``style="--card-index: N"`` so the
    staggered card-enter animation (style.css ``@keyframes card-enter``) plays
    each card 40ms after the previous.
    FF.1 §2.2 B: each field value is wrapped in a ``<span class="ro-value"
    data-app data-entity data-field>`` so the value-flash animation can target
    it. When ``(app, entity_id, field)`` is in ``changed``, the span also gets
    the ``changed`` class → ``@keyframes value-flash`` plays on render. The
    ``readonly_partial`` route (server.py) computes the changed set from
    ``sess.last_changed`` / projection diff and passes it here."""
    style = f' style="--card-index:{int(card_index)}"' if card_index else ""
    if not entities:
        return (f'<div class="card ro"{style}><h3>{_esc(app)}</h3>'
                f'<p class="muted">no entities</p></div>')
    rows = []
    for eid, fields in entities.items():
        parts = []
        for k, v in fields.items():
            is_changed = changed is not None and (app, eid, k) in changed
            cls = "ro-value changed" if is_changed else "ro-value"
            parts.append(
                f'{_esc(k)}=<span class="{cls}" data-app="{_esc(app)}" '
                f'data-entity="{_esc(eid)}" data-field="{_esc(k)}">'
                f'{_esc(v)}</span>')
        fv = " ".join(parts)
        rows.append(f'<div class="ro-row"><code>{_esc(eid)}</code> {fv}</div>')
    return f'<div class="card ro"{style}><h3>{_esc(app)}</h3>{"".join(rows)}</div>'


def editable_field_html(var_id: str, label: str, value: Any,
                        app: str | None) -> str:
    """One editable input in the read-write zone. The form posts to
    ``/<sid>/edit`` with {var_id, new_value}; the server compiles a patch +
    dispatches (with rollback_log) + re-renders."""
    return (
        f'<form class="rw-field" method="post" action="edit">'
        f'  <label>{_esc(label)} <span class="muted">[{_esc(var_id)}]'
        f'{" · " + _esc(app) if app else ""}</span></label>'
        f'  <input type="text" name="new_value" value="{_esc(value)}">'
        f'  <input type="hidden" name="var_id" value="{_esc(var_id)}">'
        f'  <button type="submit">apply</button>'
        f'</form>'
    )


def undo_button_html(app: str) -> str:
    """Per-app undo button in the read-write zone. Posts to ``/<sid>/undo/<app>``;
    the server calls ``RollbackLog.undo_saga(latest_saga_id_for_app(app), ...)``
    (E9.2 — cross-app saga undo, produces a ``SagaResult`` with
    ``partial_failure`` + per-step revert/lock outcomes). When the adapter is in
    GUI-executor mode (E10 rework, P2), the compensation mutate drives a real
    browser gesture sequence to set the field back — NOT an API call (the
    non-invasive write/rollback boundary, ``.mrules`` E7)."""
    return (f'<form class="rw-undo" method="post" action="undo/{_esc(app)}">'
            f'  <button type="submit" class="undo">↶ undo last { _esc(app) } write</button>'
            f'</form>')


def checkpoint_button_html() -> str:
    """Checkpoint button (governance: mark a restore point). W2 stores the
    snapshot + a count; full checkpoint-restore is W3."""
    return '<form class="rw-ckpt" method="post" action="checkpoint">' \
           '<button type="submit" class="ckpt">⚑ checkpoint</button></form>'


def _short_reason(err: str) -> str:
    """Condense the (long) 409/NotImplementedError message into the one honest
    sentence the user sees on the timeline. Keeps the 'no set_state backdoor'
    honesty anchor visible without dumping a stack-trace-ish blob."""
    e = (err or "").strip()
    if not e:
        return "the app exposes no undo UI for this operation (no set_state backdoor)."
    low = e.lower()
    if "wechat" in low and ("delete" in low or "recall" in low or "irrevers" in low
                            or "conflict" in low or "409" in low):
        return ("微信无消息撤回/删除 UI（无长按菜单、无 deleteMessage/recallMessage store "
                "action，消息只追加不可删）；不用 set_state 后门假装恢复。")
    if "not implemented" in low or "no adapter" in low:
        return "this app exposes no compensation gesture for this operation."
    # fallback: first sentence of the error, capped
    return e.split("。")[0][:160] or "this operation is not compensable via the app's own UI."


def saga_undo_timeline_html(saga: dict) -> str:
    """The honesty-based rollback visualization (E9.2 — '以诚实为本的回退').

    Renders a saga's undo outcome as a **progress-bar / timeline metaphor**: each
    step of the undone user action is a segment along the bar. Steps that
    reverted (``reverted=True``) are GREEN — the bar can be dragged back through
    them (real compensation via the app's own write API). Steps that FAILED
    (``reverted=False``, e.g. MobileGym ``wechat.send_message`` → HTTP 409
    because the app has no recall/delete UI) are RED + 🔒 — the bar **拖不回去
    past this point**. A draggable handle (``static/timeline.js``) moves
    leftward through green segments but snaps to a hard stop at any 🔒 segment,
    embodying the user's 'progress bar you can't drag back' analogy.

    This is the **frontend of ``SagaResult.partial_failure``** — the backend
    dataclass field has existed since W3, but until E9.2 nothing surfaced it to
    the user (a backend boolean the user never sees is not honest rollback).
    ``render_two_zone_html`` calls this after the ``/undo`` route calls
    ``undo_saga`` (NOT the W2 ``undo_last``), so ``partial_failure`` actually
    reaches the UI. The MobileGym ``send_message`` task is the first real case
    (an honest irreversible write — NOT a reversible-compensation success).

    ``saga`` = ``SagaResult.to_dict()``: {saga_id, n_targets, n_reverted,
    fully_reverted, partial_failure, errors, steps:[{app, entity_id, field,
    before, after, reverted, error}]}.
    """
    steps = saga.get("steps") or []
    partial = bool(saga.get("partial_failure"))
    n_rev = int(saga.get("n_reverted", 0) or 0)
    n_tgt = int(saga.get("n_targets", 0) or 0)
    errs = saga.get("errors") or []
    # ``undo_saga`` appends steps in REVERSE dispatch order (LIFO); display in
    # dispatch order (left = first executed, right = last executed) so the
    # progress bar reads like a timeline of the action.
    disp = list(reversed(steps))
    segs = []
    locked = []
    for i, s in enumerate(disp, 1):
        app = s.get("app", "?")
        eid = s.get("entity_id", "?")
        fld = s.get("field", "?")
        tag = f"{_esc(app)}.{_esc(eid)}.{_esc(fld)}"
        if s.get("reverted"):
            segs.append(
                f'<div class="seg ok" data-i="{i}" data-reverted="1" title="{tag}: reverted">'
                f'<span class="seg-no">✓{i}</span><span class="seg-lbl">{tag}</span></div>')
        else:
            locked.append(s)
            err = s.get("error") or (errs[0] if errs else "") or "irreversible"
            segs.append(
                f'<div class="seg lock" data-i="{i}" data-lock="1" data-err="{_esc(err)}" '
                f'title="{_esc(err)}"><span class="seg-no">🔒{i}</span>'
                f'<span class="seg-lbl">✗ {tag}</span></div>')
    bar = "".join(segs) or '<div class="seg none">no saga steps recorded</div>'
    lock_count = len(locked)

    # ── honest one-line message (grep-able: 'partial_failure'/'不可撤销'/'🔒') ──
    if partial and lock_count:
        names = ", ".join(
            f"{_esc(s.get('app', '?'))}.{_esc(s.get('entity_id', '?'))}."
            f"{_esc(s.get('field', '?'))}" for s in locked)
        reason = _short_reason((locked[0].get("error") if locked else "")
                               or (errs[0] if errs else ""))
        msg = (
            f'⚠ 本操作 <strong>部分不可撤销</strong> · <span class="pf-tag">partial_failure</span>：'
            f'{n_rev}/{n_tgt} 步已回退，{lock_count} 步已发生且 <strong>物理不可逆</strong>'
            f'（{_esc(names)}）。进度条 <strong>拖不回</strong> 这一步 —— '
            f'{reason} 这一步之后的操作仍可独立回退。')
        cls = "partial"
    elif partial:
        # partial_failure with no locked step record (e.g. missing adapter) — still honest
        msg = (f'⚠ 本操作 <strong>部分不可撤销</strong> · '
               f'<span class="pf-tag">partial_failure</span>：{n_rev}/{n_tgt} 步回退。'
               + (f' 错误：{_esc("; ".join(errs))}。' if errs else '')
               + ' 不用 set_state 后门假装恢复。')
        cls = "partial"
    else:
        msg = (f'✓ 本操作 {n_rev}/{n_tgt} 步全部回退（compensation via the app\'s own '
               f'write API · no set_state backdoor）。') if n_tgt else '✓ 无待回退步骤。'
        cls = "full"

    saga_id = _esc(saga.get("saga_id", "") or "")
    pf_badge = (' · <span class="saga-pf">partial_failure=True</span>') if partial else ""
    return (
        f'<div class="saga-timeline {cls}" data-partial="{1 if partial else 0}" '
        f'data-n-targets="{n_tgt}" data-n-reverted="{n_rev}">'
        f'  <div class="saga-head">'
        f'    <span class="saga-title">↶ honesty-based rollback · saga {saga_id}</span>'
        f'    <span class="saga-count">{n_rev}/{n_tgt} reverted{pf_badge}</span>'
        f'  </div>'
        f'  <div class="saga-bar" role="slider" aria-label="saga rollback progress">'
        f'    {bar}'
        f'    <div class="handle" tabindex="0" title="向左拖以回退（遇到 🔒 停住）"></div>'
        f'  </div>'
        f'  <div class="saga-legend">'
        f'    <span class="leg ok"><span class="dot"></span>可回退（已通过 app 自身写 API 回退）</span>'
        f'    <span class="leg lock"><span class="dot"></span>不可逆（🔒 拖不回去）</span>'
        f'  </div>'
        f'  <div class="saga-msg">{msg}</div>'
        f'</div>'
    )


def conflict_row_html(var_id: str, label: str, conflict: dict) -> str:
    """An AMBER conflict row for the read-only zone (W3 reconciliation, handoff
    §5 inv 4-5). Shows BOTH the projected value (Y, what the user is looking at)
    AND the underlying app state (X, the real world now), with merge-option
    buttons. NO silent overwrite — neither X nor Y is auto-picked. NO human-block
    — these are affordances the user MAY click; the agent is not paused.

    ``conflict`` = {"underlying": X, "projected": Y, "app", "entity_id", "field"}.
    The three merge options post to ``/<sid>/resolve`` with the chosen option
    (and an optional resolved_value for "merge")."""
    underlying = conflict.get("underlying")
    projected = conflict.get("projected")
    app = conflict.get("app")
    eid = conflict.get("entity_id")
    field = conflict.get("field")
    return (
        f'<div class="card conflict">'
        f'  <h3>⚠ {_esc(label)} <span class="muted">[{_esc(var_id)}]</span></h3>'
        f'  <div class="conflict-row">'
        f'    <div>底层已变 (现 <code>{_esc(underlying)}</code>) · 你投影的是 '
        f'      <code>{_esc(projected)}</code>'
        f'      <span class="muted">({_esc(app)}.{_esc(eid)}.{_esc(field)})</span></div>'
        f'    <div class="merge-opts">'
        f'      <form class="inline" method="post" action="resolve">'
        f'        <input type="hidden" name="var_id" value="{_esc(var_id)}">'
        f'        <input type="hidden" name="option" value="accept_underlying">'
        f'        <button type="submit" class="merge">采用底层值</button>'
        f'      </form>'
        f'      <form class="inline" method="post" action="resolve">'
        f'        <input type="hidden" name="var_id" value="{_esc(var_id)}">'
        f'        <input type="hidden" name="option" value="keep_projected">'
        f'        <button type="submit" class="merge">保留我的投影</button>'
        f'      </form>'
        f'      <form class="inline merge-form" method="post" action="resolve">'
        f'        <input type="hidden" name="var_id" value="{_esc(var_id)}">'
        f'        <input type="hidden" name="option" value="merge">'
        f'        <input type="text" name="resolved_value" placeholder="新值 Z">'
        f'        <button type="submit" class="merge">合并</button>'
        f'      </form>'
        f'    </div>'
        f'  </div>'
        f'</div>'
    )
