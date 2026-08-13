"""WorkflowStore — single source of truth for the plan and its execution
statuses.

Invariant 3 lives here: a GoalPatch installs a new graph, but every node
already in a historical status (COMMITTED / COMPENSATED) must be carried
over VERBATIM — same id, same definition. A patch that silently rewrites
or drops committed history raises ``CommittedNodeViolationError``; the
only honest ways to undo committed work are compensation flows.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, replace

from taskvm.domain.errors import CommittedNodeViolationError, ValidationError
from taskvm.domain.workflow import (
    HISTORICAL_STATUSES,
    NodeStatus,
    WorkflowGraph,
    WorkflowNode,
)


@dataclass(frozen=True)
class WorkflowSnapshot:
    """A consistent read: the graph plus each node's current status."""

    graph: WorkflowGraph | None
    statuses: dict[str, NodeStatus]


# legal forward transitions; COMMITTED may only move to COMPENSATED
_TRANSITIONS: dict[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.PENDING: frozenset({NodeStatus.READY, NodeStatus.INVALIDATED}),
    NodeStatus.READY: frozenset({NodeStatus.RUNNING, NodeStatus.INVALIDATED}),
    NodeStatus.RUNNING: frozenset({NodeStatus.READY, NodeStatus.FAILED,
                                   NodeStatus.COMMITTED, NodeStatus.INVALIDATED}),
    NodeStatus.FAILED: frozenset({NodeStatus.READY, NodeStatus.INVALIDATED}),
    NodeStatus.COMMITTED: frozenset({NodeStatus.COMPENSATED}),
    NodeStatus.COMPENSATED: frozenset(),
    NodeStatus.INVALIDATED: frozenset(),
}


class WorkflowStore:
    """Holds the current WorkflowGraph + per-node status for one session."""

    def __init__(self) -> None:
        self._graph: WorkflowGraph | None = None
        self._statuses: dict[str, NodeStatus] = {}
        self._graph_rev = 0
        self._lock = threading.RLock()

    # ── install / patch ──────────────────────────────────────────────────
    def install_graph(self, graph: WorkflowGraph, *, epoch: int) -> WorkflowGraph:
        """Install the initial plan (PlanCreated). All nodes start PENDING;
        topologically source-less nodes become READY."""
        with self._lock:
            stamped = replace(graph, revision=self._graph_rev + 1, epoch=epoch)
            self._graph = stamped
            self._graph_rev += 1
            self._statuses = {n.node_id: NodeStatus.PENDING for n in stamped.nodes}
            self._recompute_ready_locked()
            return copy.deepcopy(stamped)

    def replace_future(self, new_graph: WorkflowGraph, *, epoch: int) -> WorkflowGraph:
        """Apply the plan half of a GoalPatch: keep committed history,
        replace the uncommitted future (invariant 3).

        Every node currently in a historical status must appear in
        ``new_graph`` with an IDENTICAL definition; otherwise the patch is
        rejected — the caller must route through compensation instead.
        Carried-over nodes keep their status; all other new nodes start
        PENDING/READY. Uncommitted nodes absent from the new graph are
        dropped (they were INVALIDATED by the patch — visible in the event
        payload emitted by the kernel).
        """
        with self._lock:
            if self._graph is None:
                raise ValidationError("replace_future requires an installed graph")
            new_by_id = {n.node_id: n for n in new_graph.nodes}
            historical = {nid: st for nid, st in self._statuses.items()
                          if st in HISTORICAL_STATUSES}
            for nid, st in historical.items():
                carried = new_by_id.get(nid)
                if carried is None:
                    raise CommittedNodeViolationError(
                        f"GoalPatch drops committed node {nid!r}; committed "
                        "history can only be kept or explicitly compensated")
                if carried != self._graph.node(nid):
                    raise CommittedNodeViolationError(
                        f"GoalPatch silently rewrites committed node {nid!r}")
            stamped = replace(new_graph, revision=self._graph_rev + 1, epoch=epoch)
            self._graph = stamped
            self._graph_rev += 1
            new_statuses: dict[str, NodeStatus] = {}
            for n in stamped.nodes:
                if n.node_id in historical:
                    new_statuses[n.node_id] = historical[n.node_id]
                else:
                    new_statuses[n.node_id] = NodeStatus.PENDING
            self._statuses = new_statuses
            self._recompute_ready_locked()
            return copy.deepcopy(stamped)

    def override_contract(self, node_id: str, contract) -> None:
        """LocalPatch path: swap the contract of a NOT-yet-committed action
        node in place. Topology (ids, kinds, edges) is untouched."""
        with self._lock:
            if self._graph is None or self._graph.node(node_id) is None:
                raise ValidationError(f"unknown node {node_id!r}")
            st = self._statuses.get(node_id, NodeStatus.PENDING)
            if st in HISTORICAL_STATUSES:
                raise CommittedNodeViolationError(
                    f"cannot override contract of committed node {node_id!r}")
            node = self._graph.node(node_id)
            new_node = replace(node, contract=contract)
            new_nodes = tuple(new_node if n.node_id == node_id else n
                              for n in self._graph.nodes)
            self._graph = replace(self._graph, nodes=new_nodes,
                                  revision=self._graph_rev + 1)
            self._graph_rev += 1

    # ── status transitions ───────────────────────────────────────────────
    def set_status(self, node_id: str, status: NodeStatus) -> None:
        with self._lock:
            if self._graph is None or self._graph.node(node_id) is None:
                raise ValidationError(f"unknown node {node_id!r}")
            cur = self._statuses.get(node_id, NodeStatus.PENDING)
            if status not in _TRANSITIONS[cur]:
                raise ValidationError(
                    f"illegal node transition {cur.value} -> {status.value} "
                    f"for {node_id!r}")
            self._statuses[node_id] = status
            # a commit may unblock dependents (transitively: containers then
            # their lanes) — recompute to a fixpoint
            if status is NodeStatus.COMMITTED:
                self._recompute_ready_locked()

    def mark_running_stale_reset(self) -> list[str]:
        """Epoch bump: every in-flight (RUNNING) node returns to READY; its
        late result belongs to a dead generation (invariant 4). Returns the
        reset node ids (for the event payload)."""
        with self._lock:
            reset = [nid for nid, st in self._statuses.items()
                     if st is NodeStatus.RUNNING]
            for nid in reset:
                self._statuses[nid] = NodeStatus.READY
            return reset

    def _recompute_ready_locked(self) -> None:
        """Propagate READY to a fixpoint (a container becoming READY can in
        turn unblock its child lanes)."""
        if self._graph is None:
            return
        while True:
            newly = [n.node_id for n in self._graph.ready_nodes(self._statuses)
                     if self._statuses.get(n.node_id) is NodeStatus.PENDING]
            if not newly:
                return
            for nid in newly:
                self._statuses[nid] = NodeStatus.READY

    def committed_node_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(nid for nid, st in self._statuses.items()
                         if st is NodeStatus.COMMITTED)

    # ── reads ────────────────────────────────────────────────────────────
    def snapshot(self) -> WorkflowSnapshot:
        with self._lock:
            return WorkflowSnapshot(copy.deepcopy(self._graph),
                                    dict(self._statuses))

    def node(self, node_id: str) -> WorkflowNode | None:
        with self._lock:
            if self._graph is None:
                return None
            return copy.deepcopy(self._graph.node(node_id))
