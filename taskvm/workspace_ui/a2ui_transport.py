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
    POST /api/app/a2ui/intent       → free-text intent → IntentParser
                                      (small model, §20.2) → validated
                                      structured intent → the PUBLIC
                                      governance port (local_patch /
                                      goal_patch / checkpoint / rollback;
                                      clarify executes NOTHING)

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
- **Governance landings ride the SAME SSE connection** as named events
    (``event: governance``) — the frozen agentAPP.7 island contract
    (2026-08-20): user-visible semantics ONLY (checkpoint/node LABELS,
    never ids — repo contract §3). The ``GovernanceEventBridge`` mirrors
    the kernel event log + the runtime event stream onto the ring, so
    every governance landing is announced ONCE regardless of the entry
    it came through (the FIXED shell's routes, the A6 intent endpoint,
    or the driver itself): pause/resume/stop ← GOVERNANCE_REQUESTED,
    checkpoint_added ← CHECKPOINT_COMMITTED, rollback ←
    COMPENSATION_REQUESTED, checkpoint_reached ← COMPENSATION_APPLIED,
    node_verified/node_failed ← runtime ACTION_LANDED
    (verified / verify-failed), final_pass ← terminal NODE_COMMITTED,
    final_fail ← runtime NODE_FAILED (repair budget spent — the task
    cannot advance without governance). Same ring philosophy as progress:
    bounded, best-effort, reconnect recovery is authoritative state.
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

from taskvm.domain.errors import TaskVMError
from taskvm.domain.events import EventKind
from taskvm.genui import (
    ACTION_LOCAL_PATCH, ActionRouteError, ActionRouter, IntentParser,
    SurfaceStore, SurfaceStoreRegistry, TaskDataModelProjector,
    TaskSurfaceContextBuilder, baseline_components, validate_components,
)
from taskvm.projection.view_models import snapshot_view, workflow_view

#: How often the data-model poller re-projects the public snapshot.
POLL_INTERVAL_S = 1.0

#: Named-SSE progress events retained per session (they are transient
#: morph hints; the ring bound is a leak guard, not a replay window).
_PROGRESS_RING = 128

#: The CLOSED ``kind`` vocabulary of ``event: governance`` SSE frames —
#: the contract FROZEN with agentAPP.7 (2026-08-20). Do not extend or
#: reinterpret unilaterally; a mismatch is an issue ticket, never a
#: silent private change.
GOVERNANCE_SSE_KINDS: frozenset[str] = frozenset({
    "checkpoint_added", "checkpoint_reached", "rollback", "pause",
    "resume", "stop", "node_verified", "node_failed",
    "final_pass", "final_fail",
})


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


def _taskvm_error_status(e: Exception) -> int:
    """The frozen projection route-matrix error semantics, reused
    verbatim so the intent path answers with the SAME honest statuses
    the fixed shell's governance routes serve (never a 500):
    UnknownCheckpointError → 404, PatchSemanticsError → 422,
    ValidationError → 409 (unstable boundary / pending recompose / …),
    anything else → 400."""
    from taskvm.domain.errors import (
        PatchSemanticsError, UnknownCheckpointError, ValidationError,
    )
    if isinstance(e, UnknownCheckpointError):
        return 404
    if isinstance(e, PatchSemanticsError):
        return 422
    if isinstance(e, ValidationError):
        return 409
    return 400


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
                 session_lookup: Callable[[str], Any] | None = None,
                 intent_parser: "IntentParser | None" = None
                 ) -> None:
        self._registry = SurfaceStoreRegistry()
        self._lock = threading.Lock()
        self._progress: dict[str, deque[tuple[int, dict[str, Any]]]] = {}
        self._progress_seq: dict[str, int] = {}
        self._governance: dict[str, deque[tuple[int, dict[str, Any]]]] = {}
        self._governance_seq: dict[str, int] = {}
        self._pollers: dict[str, "A2uiDataPoller"] = {}
        self._gov_bridges: dict[str, "GovernanceEventBridge"] = {}
        self._factory = components_factory or surface_components_factory
        self._session_lookup = session_lookup
        # The A6 intent parser (free text → structured governance
        # intent). Optional by design: a transport without one serves
        # the intent endpoint with an HONEST 501, never a silent
        # no-op. The composition root re-wires it per goal so its
        # ledger rows land in the CURRENT goal's shared ledger (the
        # same lifecycle discipline as attach_session).
        self._intent_parser = intent_parser

    def set_intent_parser(self, parser: "IntentParser | None") -> None:
        """(Re-)wire the intent parser — called by the composition root
        when a new goal's ledger exists (one ledger per goal; rows for
        an old goal must never land in the new one)."""
        self._intent_parser = parser

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

    # ── governance named SSE events (the frozen A7 island contract) ────
    def push_governance(self, sid: str, kind: str, *, label: str = "",
                        rev: int = 0, detail: dict[str, Any] | None = None,
                        ts_ms: int | None = None) -> None:
        """Append ONE ``event: governance`` frame to the session's ring.

        ``kind`` MUST be in :data:`GOVERNANCE_SSE_KINDS` (the frozen
        contract) — an unknown kind raises honestly instead of being
        silently dropped or renamed. ``label`` carries user-visible
        text ONLY (a checkpoint label / a node label); internal ids
        never ride this frame (repo contract §3)."""
        if kind not in GOVERNANCE_SSE_KINDS:
            raise ValueError(
                f"governance SSE kind {kind!r} is not in the frozen "
                f"contract vocabulary {sorted(GOVERNANCE_SSE_KINDS)}")
        ev = {"type": "governance", "kind": kind, "label": label,
              "rev": int(rev),
              "ts": int(ts_ms if ts_ms is not None
                        else time.time() * 1000),
              "detail": dict(detail or {})}
        with self._lock:
            ring = self._governance.setdefault(
                sid, deque(maxlen=_PROGRESS_RING))
            self._governance_seq[sid] = \
                self._governance_seq.get(sid, 0) + 1
            ring.append((self._governance_seq[sid], ev))

    def governance_after(self, sid: str, after: int
                         ) -> list[tuple[int, dict[str, Any]]]:
        with self._lock:
            ring = self._governance.get(sid)
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
        self._start_gov_bridge(sid, sess)
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

    def _start_gov_bridge(self, sid: str, sess) -> None:
        """Start (or replace) the governance event bridge for this
        session. The bridge starts at the CURRENT end of the event logs:
        history that predates the surface is state the island reads from
        the data model, not events to replay."""
        with self._lock:
            old = self._gov_bridges.get(sid)
            if old is not None:
                old.stop()
            bridge = GovernanceEventBridge(
                self, sid, sess, session_lookup=self._session_lookup)
            self._gov_bridges[sid] = bridge
        bridge.start()

    def drop_session(self, sid: str) -> None:
        """Retire the poller + the governance bridge (session replaced /
        dropped by a new goal)."""
        with self._lock:
            poller = self._pollers.pop(sid, None)
            bridge = self._gov_bridges.pop(sid, None)
        if poller is not None:
            poller.stop()
        if bridge is not None:
            bridge.stop()

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

    # ── the free-text intent path (A6: NL → structured → governance) ───
    def apply_intent(self, sid: str, sess, *, text: str) -> dict[str, Any]:
        """Free text → IntentParser (small model, §20.2) → ONE validated
        structured intent → the session's PUBLIC governance port.

        Every executable kind maps onto the SAME governance entry the
        fixed shell's routes run (no parallel write path): local_patch
        re-validates each (key, value) through the ActionRouter before
        the single atomic governance write; goal_patch / checkpoint /
        rollback go straight to the port (rollback resolves the
        user-visible checkpoint LABEL to the kernel's checkpoint id —
        the model never sees ids, repo contract §3). A ``clarify``
        intent executes NOTHING — it is a question back to the user.

        Raises ``A2uiTransportError`` (http_status) for honest
        rejections: no parser configured 501, unknown checkpoint 404,
        ActionRouter re-validation failures 400/403."""
        if self._intent_parser is None:
            raise A2uiTransportError(
                "intent parsing is not configured on this transport "
                "(no model port wired by the composition root)"
            )._status(501)
        context = self._build_context(sess)
        parsed = self._intent_parser.parse(text, context)
        if parsed.is_clarify:
            return {"ok": True, "kind": "clarify",
                    "question": parsed.question,
                    "intent": parsed.to_payload()}

        if parsed.kind == "local_patch":
            # The LAST enforcement point: the parser validated the
            # pairs, the router re-validates them against the SAME
            # rule set (defense in depth — nothing reaches the kernel
            # unvalidated), all-or-nothing (kernel local_patch is
            # atomic; a mixed valid/invalid intent writes NOTHING).
            # A router rejection rides the SAME honest statuses the
            # fixed action path serves (400 malformed / 403 ownership)
            # — never a 500.
            router = ActionRouter(context)
            try:
                for key, value in parsed.updates.items():
                    router.route(ACTION_LOCAL_PATCH,
                                 {"semanticKey": key, "value": value,
                                  "rationale": parsed.rationale})
            except ActionRouteError as e:
                raise A2uiTransportError(str(e))._status(
                    e.http_status) from e
            result = self._port_call(
                sess.governance_port().local_patch, parsed.updates,
                rationale=parsed.rationale or text.strip()[:200])
        elif parsed.kind == "goal_patch":
            result = self._port_call(
                sess.governance_port().goal_patch,
                goal=parsed.goal, constraints=parsed.constraints,
                scope=parsed.scope,
                success_criteria=parsed.success_criteria,
                rationale=parsed.rationale)
        elif parsed.kind == "checkpoint":
            result = self._port_call(
                sess.governance_port().checkpoint,
                parsed.checkpoint_label)
        elif parsed.kind == "rollback":
            target = self._resolve_checkpoint_id(sess,
                                                 parsed.checkpoint_label)
            result = self._port_call(
                sess.governance_port().rollback, target,
                rationale=parsed.rationale)
            # the frozen projection route's discipline: hand the
            # compensation plan to the session's driver when one is
            # registered; without a driver the disposition honestly
            # stays "pending" (§8 — never a fake success)
            plan = result.pop("plan", None)
            if plan is not None:
                driver = getattr(sess, "driver", None)
                if driver is not None:
                    result["disposition"] = driver.execute_compensation(
                        plan)
        else:  # pragma: no cover — INTENT_KINDS is closed
            raise A2uiTransportError(
                f"unsupported intent kind {parsed.kind!r}")._status(400)
        return {"ok": True, "kind": parsed.kind, "result": result,
                "intent": parsed.to_payload()}

    @staticmethod
    def _port_call(fn, *args, **kwargs):
        """Call the public governance port; a kernel-level rejection
        (unstable boundary / pending recompose / unknown checkpoint / …)
        rides the frozen route-matrix statuses — the intent path NEVER
        answers a 500 for an honest governance refusal."""
        try:
            return fn(*args, **kwargs)
        except TaskVMError as e:
            raise A2uiTransportError(
                f"{type(e).__name__}: {e}")._status(
                _taskvm_error_status(e)) from e

    @staticmethod
    def _resolve_checkpoint_id(sess, label: str) -> str:
        """User-visible checkpoint LABEL → the kernel's checkpoint id
        (the model only ever sees labels — repo contract §3; the
        composition root resolves ids). Duplicate labels resolve to
        the LATEST checkpoint; an unknown label is an honest 404."""
        latest = None
        for c in (snapshot_view(sess).get("checkpoints") or []):
            if c.get("label") == label:
                latest = c.get("checkpoint_id")
        if not latest:
            raise A2uiTransportError(
                f"no checkpoint labelled {label!r}")._status(404)
        return latest


# ── the governance event bridge (kernel/runtime → governance SSE) ───────────


def _checkpoint_label_map(sess) -> dict[str, str]:
    """Kernel checkpoint id → the USER-VISIBLE label. Used ONLY to
    resolve ids the frozen kernel events carry into the labels the SSE
    contract allows on the wire (repo contract §3 — ids stay here)."""
    out: dict[str, str] = {}
    for rec in (sess.kernel.checkpoints() or []):
        out[rec.checkpoint_id] = rec.label or rec.checkpoint_id
    return out


def _node_label_map(sess) -> dict[str, str]:
    """Node id → the USER-VISIBLE node label (the same label the
    workflow view renders)."""
    graph = getattr(sess.kernel.workflow(), "graph", None)
    if graph is None:
        return {}
    return {n.node_id: (n.label or n.node_id) for n in graph.nodes}


class GovernanceEventBridge(threading.Thread):
    """Daemon mirroring governance landings onto the transport's
    ``event: governance`` SSE ring (the frozen A7 island contract).

    Read-only over the public facades — ``kernel.events()`` and
    ``runtime.runtime_events()`` are non-destructive snapshots, so this
    bridge races nobody (the screenshot poller and the autonomy driver
    read the same streams independently). Zero model calls by
    construction. Because BOTH the fixed shell's routes and the A6
    intent endpoint land their commands on the kernel (through the
    governance port / the driver), ONE bridge covers every entry —
    each landing is announced exactly once, no matter who asked.

    ``scan_once()`` is synchronous and side-effect-accounted (returns
    the number of frames pushed) so tests drive it directly; ``run()``
    just polls it. The bridge retires itself when its session is no
    longer the registered one (the injected ``session_lookup``)."""

    def __init__(self, transport: "A2uiTransport", sid: str, sess, *,
                 session_lookup: Callable[[str], Any] | None = None
                 ) -> None:
        super().__init__(daemon=True, name=f"a2ui-gov-{sid}")
        self._transport = transport
        self._sid = sid
        self._sess = sess
        self._session_lookup = session_lookup
        # start at the CURRENT end: pre-surface history is state (the
        # island reads it from the data model), not events to replay
        self._kernel_cursor = len(sess.kernel.events())
        rt = getattr(sess, "runtime", None)
        events_fn = getattr(rt, "runtime_events", None)
        self._runtime_cursor = (len(tuple(events_fn()))
                                if callable(events_fn) else 0)
        # compensation plan id → the target checkpoint's user-visible
        # label (carried across events: COMPENSATION_APPLIED only names
        # the plan, the checkpoint label was resolved at request time)
        self._plan_labels: dict[str, str] = {}
        self._stop_evt = threading.Event()

    def run(self) -> None:
        while not self._stop_evt.wait(POLL_INTERVAL_S):
            try:
                lookup = self._session_lookup
                if lookup is not None and lookup(self._sid) is not self._sess:
                    return    # replaced/dropped — stop watching
                self.scan_once()
            except Exception:
                continue    # read-only mirroring; a transient kernel lock
                #            hiccup must never kill the side channel

    def stop(self) -> None:
        self._stop_evt.set()

    def scan_once(self) -> int:
        """One read-only pass over the new kernel + runtime events.
        Returns how many governance frames were pushed (0 when nothing
        user-visible landed)."""
        return self._scan_kernel() + self._scan_runtime()

    # ── the kernel event log (every governance entry lands here) ──────
    def _scan_kernel(self) -> int:
        events = self._sess.kernel.events()
        new = events[self._kernel_cursor:]
        if not new:
            return 0
        self._kernel_cursor = len(events)
        node_labels = _node_label_map(self._sess)
        cp_labels = _checkpoint_label_map(self._sess)
        pushed = 0
        for ev in new:
            for frame in self._kernel_frames(ev, cp_labels, node_labels):
                self._transport.push_governance(
                    self._sid, frame.pop("kind"), ts_ms=int(ev.timestamp
                                                             * 1000),
                    **frame)
                pushed += 1
        return pushed

    def _kernel_frames(self, ev, cp_labels: dict[str, str],
                       node_labels: dict[str, str]) -> list[dict[str, Any]]:
        """ONE kernel event → its governance SSE frame(s) (usually 0 or
        1). User-visible semantics only: ids are resolved to labels
        here and never ride the frame (repo contract §3)."""
        kind, p = ev.kind, (ev.payload or {})
        if kind is EventKind.GOVERNANCE_REQUESTED:
            action = str(p.get("action", ""))
            if action in ("pause", "resume", "stop"):
                return [{"kind": action, "label": "",
                         "rev": ev.revision,
                         "detail": {"action": action}}]
            return []      # mode changes etc. carry no island contract
        if kind is EventKind.CHECKPOINT_COMMITTED:
            return [{"kind": "checkpoint_added",
                     "label": str(p.get("label", "") or ""),
                     "rev": ev.revision,
                     "detail": {"epoch": ev.epoch}}]
        if kind is EventKind.COMPENSATION_REQUESTED:
            target = str(p.get("target_checkpoint_id", "") or "")
            label = cp_labels.get(target, target)
            plan_id = str(p.get("plan_id", "") or "")
            if plan_id:
                self._plan_labels[plan_id] = label
            return [{"kind": "rollback", "label": label,
                     "rev": ev.revision,
                     "detail": {
                         "entries": len(p.get("entries") or []),
                         "uncompensatable": len(
                             p.get("uncompensatable_nodes") or [])}}]
        if kind in (EventKind.COMPENSATION_APPLIED,
                    EventKind.COMPENSATION_PARTIAL):
            # COMPLETE rollback = reality is back AT the checkpoint;
            # PARTIAL still reaches it honestly but names what stood
            # (the disposition rides detail — never a fake "complete")
            return [{"kind": "checkpoint_reached",
                     "label": self._plan_labels.get(ev.correlation_id,
                                                     ""),
                     "rev": ev.revision,
                     "detail": {"disposition": str(
                         p.get("disposition", ""))}}]
        if kind is EventKind.NODE_COMMITTED:
            if str(p.get("kind", "")) == "terminal":
                return [{"kind": "final_pass",
                         "label": node_labels.get(
                             str(p.get("node_id", "")), ""),
                         "rev": ev.revision,
                         "detail": {}}]
            return []
        # COMPENSATION_FAILED / DISCARDED, VERIFICATION_*, observations,
        # plan lifecycle … — no island contract kind; the route HTTP
        # responses and the data model already carry those honestly.
        return []

    # ── the runtime event stream (node verdicts) ───────────────────────
    def _scan_runtime(self) -> int:
        rt = getattr(self._sess, "runtime", None)
        events_fn = getattr(rt, "runtime_events", None)
        if not callable(events_fn):
            return 0
        events = tuple(events_fn())
        new = events[self._runtime_cursor:]
        if not new:
            return 0
        self._runtime_cursor = len(events)
        node_labels = _node_label_map(self._sess)
        pushed = 0
        for ev in new:
            frames = self._runtime_frames(ev, node_labels)
            for frame in frames:
                self._transport.push_governance(self._sid, **frame)
                pushed += 1
        return pushed

    def _runtime_frames(self, ev, node_labels: dict[str, str]
                        ) -> list[dict[str, Any]]:
        """ONE runtime event → its governance SSE frame(s). ACTION_LANDED
        carries the node verdict (verified / verify-failed); NODE_FAILED
        is a node's TERMINAL failure (repair budget spent / unexecutable)
        — the task cannot advance without governance, the honest
        final_fail signal."""
        kind = getattr(getattr(ev, "kind", None), "name", "") or \
            str(getattr(ev, "kind", ""))
        detail = str(getattr(ev, "detail", "") or "")
        node_label = node_labels.get(getattr(ev, "node_id", ""), "")
        rev = int(getattr(ev, "epoch", 0) or 0)
        if kind == "ACTION_LANDED":
            if detail == "verified":
                return [{"kind": "node_verified", "label": node_label,
                         "rev": rev, "detail": {}}]
            if detail == "verify-failed":
                return [{"kind": "node_failed", "label": node_label,
                         "rev": rev, "detail": {}}]
            return []
        if kind == "NODE_FAILED":
            return [{"kind": "final_fail", "label": node_label,
                     "rev": rev, "detail": {"reason": detail}}]
        return []


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
            governance_cursor = 0
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
                for gseq, ev in transport.governance_after(
                        sid, governance_cursor):
                    governance_cursor = gseq
                    frames.append(
                        "event: governance\ndata: "
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

    # ── POST /api/app/a2ui/intent — the free-text governance path ────
    @app.route("/api/app/a2ui/intent", methods=["POST"])
    def a2ui_intent():
        """Free text → small-model parse → ONE structured governance
        command through the PUBLIC port (handover A6). A clarify reply
        is a 200 with kind=clarify + the question (the parse itself
        succeeded — it honestly concluded "ask the user")."""
        from flask import jsonify, request
        body = request.get_json(silent=True) or {}
        text = body.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return jsonify({"ok": False,
                            "error": "text must be a non-empty string"}), 400
        sess = store.get(state.sid)
        if sess is None:
            return jsonify({"ok": False,
                            "error": "no active session"}), 404
        try:
            result = transport.apply_intent(state.sid, sess, text=text)
        except A2uiTransportError as e:
            return jsonify({"ok": False, "error": str(e)}), \
                getattr(e, "http_status", 400)
        return jsonify(result)
