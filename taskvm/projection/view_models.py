"""taskvm.projection.view_models — pure snapshot → JSON-safe view models
(contract §4: kernel/runtime public snapshots only; 0 side effects, 0 model
calls, 0 substrate knowledge).

Every builder here is a pure function of its inputs. The workflow view
expresses EXACTLY the frozen kernel primitives (sequence / fan-out+barrier
/ bounded loop) with business labels — the browser renders, never guesses
topology (contract §9).
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from taskvm.domain.events import Event, EventKind
from taskvm.domain.workflow import NodeKind, NodeStatus
from taskvm.kernel import CheckpointRecord, TaskVMKernel, WorkflowSnapshot

from taskvm.projection.store import ProjectionSession

#: Business-facing node status vocabulary (contract §9). Raw ids stay in
#: inspectable detail, never as the primary label.
_STATUS_LABELS = {
    NodeStatus.PENDING: "waiting",
    NodeStatus.READY: "ready",
    NodeStatus.RUNNING: "executing",
    NodeStatus.COMMITTED: "verified",
    NodeStatus.FAILED: "failed",
    NodeStatus.INVALIDATED: "invalidated",
    NodeStatus.COMPENSATED: "rolled_back",
}

_KIND_LABELS = {
    NodeKind.SEQUENCE: "sequence",
    NodeKind.FAN_OUT: "fan-out",
    NodeKind.BARRIER: "verify barrier",
    NodeKind.BOUNDED_LOOP: "bounded loop",
    NodeKind.ACTION: "step",
    NodeKind.VERIFY: "verification",
    NodeKind.CHECKPOINT: "checkpoint",
    NodeKind.TERMINAL: "goal",
}

_KIND_ORDER = {
    NodeKind.SEQUENCE: 0, NodeKind.ACTION: 1, NodeKind.VERIFY: 2,
    NodeKind.FAN_OUT: 0, NodeKind.BARRIER: 3, NodeKind.BOUNDED_LOOP: 0,
    NodeKind.CHECKPOINT: 4, NodeKind.TERMINAL: 5,
}


def _jsonable(value: Any) -> Any:
    """Best-effort JSON-safe conversion for view payloads."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


# ── governance bar ─────────────────────────────────────────────────────────

def governance_view(sess: ProjectionSession) -> dict[str, Any]:
    """Top bar: goal summary + autonomy state + honest block reasons."""
    kernel = sess.kernel
    intent = kernel.task_state().intent
    pending = kernel.pending_recompose
    if pending is not None:
        autonomy = "replanning" if pending == "goal_patch" else "blocked"
    elif sess.driver is not None:
        autonomy = sess.driver.status()
    else:
        autonomy = "idle"
    view: dict[str, Any] = {
        "goal": intent.goal,
        "constraints": list(intent.constraints),
        "scope": list(intent.scope),
        "success_criteria": list(intent.success_criteria),
        "autonomy": autonomy,
        "epoch": kernel.epoch,
        "pending_recompose": pending,
        "model_calls": (sess.model_call_probe() if sess.model_call_probe
                        else None),
    }
    return _jsonable(view)


# ── task variables (the editable task state projection) ────────────────────

def variables_view(kernel: TaskVMKernel) -> list[dict[str, Any]]:
    state = kernel.task_state()
    out = []
    for v in sorted(state.variables, key=lambda x: x.semantic_key):
        out.append({
            "key": v.semantic_key,
            "label": v.label or v.semantic_key,
            "observed": _jsonable(v.observed),
            "desired": _jsonable(v.desired),
            "diverged": v.diverged,
            "mutability": v.mutability,
            "editable": v.mutability == "editable",
            "confidence": v.confidence,
        })
    return out


# ── projection schema (structure; changes only on recomposition) ───────────

def projection_schema_view(kernel: TaskVMKernel) -> dict[str, Any] | None:
    snap = kernel.projection()
    if snap.schema is None or not snap.schema.components:
        return None
    return {
        "root_id": snap.schema.root_id,
        "revision": snap.schema.revision,
        "components": [
            {
                "component_id": c.component_id,
                "component_type": c.component_type,
                "label": c.label,
                "binding_key": c.binding_key,
                "children": list(c.children),
                "editable": c.editable,
                "props": _jsonable(c.props),
            }
            for c in snap.schema.components
        ],
    }


def projection_data_view(kernel: TaskVMKernel) -> dict[str, Any]:
    data = kernel.projection().data
    return {
        "revision": data.revision,
        "progress": data.progress,
        "values": _jsonable(data.values),
        "node_status": dict(data.node_status),
    }


# ── workflow map ───────────────────────────────────────────────────────────

def _loop_iterations(events: Iterable[Event]) -> dict[str, dict[str, Any]]:
    """Bounded-loop live state from the kernel event log: current
    iteration count + last termination verdict per loop node."""
    out: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.kind is EventKind.LOOP_ITERATION_STARTED:
            nid = e.payload.get("node_id", "")
            it = e.payload.get("iteration")
            if nid:
                out.setdefault(nid, {})["iteration"] = it
        elif e.kind is EventKind.LOOP_ITERATION_EVALUATED:
            nid = e.payload.get("node_id", "")
            if nid:
                out.setdefault(nid, {})["last_verdict"] = e.payload.get(
                    "terminated")
    return out


def workflow_view(wf: WorkflowSnapshot,
                  events: Iterable[Event] = ()) -> dict[str, Any]:
    """The full workflow view model: ordered rows, business labels, loop
    live state, checkpoint markers. Server computes; browser renders."""
    if wf.graph is None:
        return {"nodes": [], "has_plan": False}
    graph = wf.graph
    statuses = wf.statuses
    loops = _loop_iterations(events)

    nodes_by_id = {n.node_id: n for n in graph.nodes}

    def depth_of(node: Any) -> int:
        d, cur = 0, node
        while cur.parent_id is not None and cur.parent_id in nodes_by_id:
            cur = nodes_by_id[cur.parent_id]
            d += 1
        return d

    def container_progress(node: Any) -> dict[str, Any] | None:
        if node.kind not in (NodeKind.SEQUENCE, NodeKind.FAN_OUT,
                             NodeKind.BOUNDED_LOOP):
            return None
        children = [c for c in graph.nodes if c.parent_id == node.node_id]
        if not children:
            return None
        committed = sum(1 for c in children
                        if statuses.get(c.node_id) is NodeStatus.COMMITTED)
        return {"committed": committed, "total": len(children)}

    # stable row order: parents before children, then label, then id
    ordered = sorted(graph.nodes,
                     key=lambda n: (depth_of(n), n.parent_id or "",
                                    _KIND_ORDER.get(n.kind, 9), n.label,
                                    n.node_id))

    rows = []
    for node in ordered:
        status = statuses.get(node.node_id, NodeStatus.PENDING)
        row: dict[str, Any] = {
            "node_id": node.node_id,
            "kind": node.kind.value,
            "kind_label": _KIND_LABELS[node.kind],
            "label": node.label or node.node_id,
            "status": status.value,
            "status_label": _STATUS_LABELS.get(status, status.value),
            "depth": depth_of(node),
            "parent_id": node.parent_id,
            "depends_on": list(node.depends_on),
            "is_checkpoint": node.kind is NodeKind.CHECKPOINT,
            "rollback_boundary": node.kind is NodeKind.CHECKPOINT,
        }
        prog = container_progress(node)
        if prog is not None:
            row["progress"] = prog
        if node.kind is NodeKind.BOUNDED_LOOP:
            live = loops.get(node.node_id, {})
            row["loop"] = {
                "iteration": live.get("iteration"),
                "max_iterations": node.max_iterations,
                "termination_predicate": node.termination_predicate,
                "last_verdict": live.get("last_verdict"),
            }
        if node.kind is NodeKind.ACTION and node.contract is not None:
            rev = getattr(node.contract.reversibility, "value",
                          str(node.contract.reversibility))
            row["action"] = {
                "goal": node.contract.semantic_goal,
                "reversibility": rev,
                "irreversible": rev.lower().startswith("irreversible"),
            }
        if node.kind is NodeKind.VERIFY:
            row["verification"] = node.verification
        rows.append(row)

    total = len(rows)
    committed = sum(1 for r in rows if r["status"] == "committed")
    return {
        "has_plan": True,
        "nodes": rows,
        "progress": {"committed": committed, "total": total},
    }


# ── checkpoint timeline ────────────────────────────────────────────────────

def checkpoint_view(checkpoints: Iterable[CheckpointRecord],
                    ) -> list[dict[str, Any]]:
    """Business-language checkpoint timeline with rollback affordances."""
    out = []
    for rec in checkpoints:
        out.append({
            "checkpoint_id": rec.checkpoint_id,
            "label": rec.label or rec.checkpoint_id,
            "state_revision": rec.state_revision,
            "event_index": rec.event_index,
            "epoch": rec.epoch,
            "committed_nodes": len(rec.committed_nodes),
            "created_at": rec.created_at,
            "rollback_available": True,
        })
    return out


# ── surface cards (multi-app overview; contract §5/§11) ────────────────────

def surface_cards(sess: ProjectionSession,
                  runtime_events: Iterable[Any] = ()) -> list[dict[str, Any]]:
    """One high-level card per declared/observed surface. Identical
    treatment for every surface — no platform branching (contract §11)."""
    cards: dict[str, dict[str, Any]] = {}

    def _card(surface_id: str, display_name: str) -> dict[str, Any]:
        if surface_id not in cards:
            cards[surface_id] = {
                "surface_id": surface_id,
                "display_name": display_name or surface_id,
                "current_goal": "",
                "last_observed_at": None,
                "latest_artifact_ref": None,
                "artifact_refs": [],
                "status": "unknown",
                "recent_actions": [],
            }
        return cards[surface_id]

    for decl in sess.surfaces:
        _card(decl.surface_id, decl.display_name)

    for ev in runtime_events:
        sid = getattr(ev, "surface_id", "") or ""
        if not sid:
            continue
        card = _card(sid, sid)
        ts = getattr(ev, "epoch", 0)
        if card["last_observed_at"] is None or ts > card["last_observed_at"]:
            card["last_observed_at"] = ts
        ref = getattr(ev, "artifact_ref", "") or ""
        if ref and ref not in card["artifact_refs"]:
            card["artifact_refs"].append(ref)
        node_id = getattr(ev, "node_id", "") or ""
        kind = getattr(ev, "kind", None)
        kind_name = getattr(kind, "value", str(kind))
        if kind_name == "action_landed":
            card["status"] = ("verified" if getattr(ev, "detail", "") == "verified"
                              else "executing")
            if node_id:
                card["current_goal"] = node_id
            card["recent_actions"].append(
                {"kind": kind_name, "node_id": node_id, "epoch": ts})
            card["recent_actions"] = card["recent_actions"][-8:]
        elif kind_name == "surface_conflict":
            card["status"] = "conflict"
        elif kind_name == "budget_exhausted":
            card["status"] = "need_human"

    # latest artifact resolution (read-only; no capture side effects)
    for card in cards.values():
        stored = [r for r in card["artifact_refs"]
                  if sess.artifacts.has(r)]
        card["artifact_refs"] = stored
        card["latest_artifact_ref"] = sess.artifacts.latest_ref(stored)

    return sorted(cards.values(), key=lambda c: c["surface_id"])


# ── conflicts (business-facing resolution state) ───────────────────────────

def conflicts_view(kernel: TaskVMKernel) -> list[dict[str, Any]]:
    """Open conflicts from the kernel event log (detected-not-yet-resolved)."""
    open_conflicts: dict[str, dict[str, Any]] = {}
    for e in kernel.events():
        if e.kind is EventKind.CONFLICT_DETECTED:
            cid = e.correlation_id or f"conflict:{e.event_id}"
            open_conflicts[cid] = {
                "conflict_id": cid,
                "description": e.payload.get("description", ""),
                "semantic_keys": list(e.payload.get("semantic_keys", [])),
                "epoch": e.epoch,
                "resolved": False,
            }
        elif e.kind is EventKind.CONFLICT_RESOLVED:
            cid = e.correlation_id
            if cid in open_conflicts:
                open_conflicts[cid]["resolved"] = True
                open_conflicts[cid]["resolution"] = e.payload.get(
                    "resolution", "")
    return [c for c in open_conflicts.values() if not c["resolved"]]


# ── the full snapshot bundle (one GET; then deltas only) ───────────────────

def snapshot_view(sess: ProjectionSession) -> dict[str, Any]:
    """The complete view-model bundle served once on load (contract §6);
    afterwards the client consumes SSE deltas only."""
    kernel = sess.kernel
    events = kernel.events()
    proj = kernel.projection()
    runtime_events = (sess.runtime.runtime_events()
                      if sess.runtime is not None else ())
    return {
        "sid": sess.sid,
        "governance": governance_view(sess),
        "variables": variables_view(kernel),
        "projection_schema": projection_schema_view(kernel),
        "projection_data": projection_data_view(kernel),
        "workflow": workflow_view(kernel.workflow(), events),
        "checkpoints": checkpoint_view(kernel.checkpoints()),
        "surfaces": surface_cards(sess, runtime_events),
        "conflicts": conflicts_view(kernel),
        "revisions": {
            "state": kernel.task_state().revision,
            "schema": (proj.revision.schema_revision
                       if proj.schema is not None else 0),
            "data": proj.revision.data_revision,
            "events": len(events),
        },
    }
