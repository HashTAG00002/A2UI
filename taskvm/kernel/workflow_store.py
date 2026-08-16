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
    NodeKind,
    NodeStatus,
    WorkflowGraph,
    WorkflowNode,
)


@dataclass(frozen=True)
class WorkflowSnapshot:
    """A consistent read: the graph plus each node's current status."""

    graph: WorkflowGraph | None
    statuses: dict[str, NodeStatus]


# legal forward transitions; COMMITTED may only move to COMPENSATED.
# READY → COMMITTED / READY → FAILED exist ONLY for CONTROL nodes (VERIFY
# / BARRIER / CHECKPOINT / TERMINAL): the kernel gates which kinds may use
# them (audit G1 — an ACTION can never skip the action lifecycle).
_TRANSITIONS: dict[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.PENDING: frozenset({NodeStatus.READY, NodeStatus.INVALIDATED}),
    NodeStatus.READY: frozenset({NodeStatus.RUNNING, NodeStatus.COMMITTED,
                                 NodeStatus.FAILED, NodeStatus.INVALIDATED}),
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

    # ── historical semantics (invariant 3 + audit G13d) ──────────────────
    def _is_ephemeral_loop_commit_locked(self, node_id: str) -> bool:
        """A COMMITTED direct child of a NOT-yet-committed bounded loop is
        EPHEMERAL: its per-iteration commit is reset on the next iteration
        and on any epoch interrupt, so it must never be treated as the
        permanent history a GoalPatch has to carry verbatim."""
        if self._graph is None:
            return False
        node = self._graph.node(node_id)
        if node is None or node.parent_id is None:
            return False
        parent = self._graph.node(node.parent_id)
        if parent is None or parent.kind is not NodeKind.BOUNDED_LOOP:
            return False
        return self._statuses.get(parent.node_id) not in (
            NodeStatus.COMMITTED, NodeStatus.COMPENSATED)

    def _historical_locked(self) -> dict[str, NodeStatus]:
        return {nid: st for nid, st in self._statuses.items()
                if st in HISTORICAL_STATUSES
                and not self._is_ephemeral_loop_commit_locked(nid)}

    def historical_node_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._historical_locked())

    # ── install / patch ──────────────────────────────────────────────────
    def install_graph(self, graph: WorkflowGraph, *, epoch: int) -> WorkflowGraph:
        """Install the initial plan (PlanCreated). All nodes start PENDING;
        topologically source-less nodes become READY. The graph is
        deep-copied at the write boundary (audit G10)."""
        with self._lock:
            stamped = replace(copy.deepcopy(graph),
                              revision=self._graph_rev + 1, epoch=epoch)
            self._graph = stamped
            self._graph_rev += 1
            self._statuses = {n.node_id: NodeStatus.PENDING for n in stamped.nodes}
            self._recompute_ready_locked()
            return copy.deepcopy(stamped)

    def validate_replace_future(self, new_graph: WorkflowGraph) -> None:
        """NON-MUTATING pre-flight check for ``replace_future`` (patch
        atomicity: callers validate fully before any state changes).

        Every node currently in a historical status must appear in
        ``new_graph`` with an IDENTICAL definition; otherwise the patch is
        rejected — the caller must route through compensation instead.
        """
        with self._lock:
            if self._graph is None:
                raise ValidationError("replace_future requires an installed graph")
            new_by_id = {n.node_id: n for n in new_graph.nodes}
            historical = self._historical_locked()
            for nid in historical:
                carried = new_by_id.get(nid)
                if carried is None:
                    raise CommittedNodeViolationError(
                        f"GoalPatch drops committed node {nid!r}; committed "
                        "history can only be kept or explicitly compensated")
                if carried != self._graph.node(nid):
                    raise CommittedNodeViolationError(
                        f"GoalPatch silently rewrites committed node {nid!r}")

    def replace_future(self, new_graph: WorkflowGraph, *,
                       epoch: int) -> tuple[WorkflowGraph, list[str]]:
        """Apply the plan half of a GoalPatch/recompose: keep committed
        history, replace the uncommitted future (invariant 3). The caller
        MUST run ``validate_replace_future`` first (patch atomicity:
        validate fully before any state changes); the check is not
        repeated here — one property, one owner, one check.

        Carried-over nodes keep their status; all other new nodes start
        PENDING/READY. Uncommitted nodes absent from the new graph are
        dropped.

        Returns ``(installed_graph, invalidated_node_ids)`` — the kernel
        puts the invalidated ids into the PlanPatched event payload so the
        replaced future is explicit, never silent.
        """
        with self._lock:
            historical = self._historical_locked()
            stamped = replace(copy.deepcopy(new_graph),
                              revision=self._graph_rev + 1, epoch=epoch)
            old_ids = set(self._statuses)
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
            invalidated = sorted(old_ids - set(new_statuses))
            return copy.deepcopy(stamped), invalidated

    def retarget_action_contracts(self, updates: dict) -> list[str]:
        """LocalPatch path (audit G4): DETERMINISTICALLY retarget the
        ``desired_state`` of every NOT-yet-historical ACTION node whose
        contract references an updated semantic key. Only the target VALUE
        changes — topology, evidence, reversibility and risk class are
        structurally untouched. Committed history is never retargeted.

        The ``completion_condition`` (RFC-003 ``key == value`` form) is
        retargeted in lockstep: when the updated key appears as the
        condition's LHS, the RHS value is replaced with the new target.
        A non-conforming or empty condition is left untouched (the
        verifier's fail-closed semantics handle it). Returns the
        retargeted node ids."""
        with self._lock:
            if self._graph is None:
                return []
            retargeted: list[str] = []
            new_nodes = []
            for n in self._graph.nodes:
                st = self._statuses.get(n.node_id, NodeStatus.PENDING)
                if (n.kind is NodeKind.ACTION and n.contract is not None
                        and st not in HISTORICAL_STATUSES
                        and st is not NodeStatus.INVALIDATED
                        and any(k in updates
                                for k in n.contract.desired_state)):
                    new_ds = {k: updates.get(k, v)
                              for k, v in n.contract.desired_state.items()}
                    new_cc = self._retarget_completion(
                        n.contract.completion_condition, updates)
                    n = replace(n, contract=replace(
                        n.contract, desired_state=new_ds,
                        completion_condition=new_cc))
                    retargeted.append(n.node_id)
                new_nodes.append(n)
            if retargeted:
                self._graph = replace(self._graph, nodes=tuple(new_nodes),
                                      revision=self._graph_rev + 1)
                self._graph_rev += 1
            return retargeted

    @staticmethod
    def _retarget_completion(condition: str,
                             updates: dict) -> str:
        """Deterministically update the value side of a RFC-003
        ``key == value`` completion_condition when ``key`` is in
        ``updates``. Non-conforming conditions are returned unchanged."""
        cond = (condition or "").strip()
        if not cond or cond.count("==") != 1:
            return condition
        key, sep, val = cond.partition("==")
        key_s = key.strip()
        if key_s in updates:
            new_val = str(updates[key_s])
            return f"{key_s} == {new_val}"
        return condition

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
        late result belongs to a dead generation (invariant 4). Ephemeral
        loop-body commits of any not-yet-committed loop are reset with the
        generation (audit G13d). Returns the reset node ids."""
        with self._lock:
            reset = [nid for nid, st in self._statuses.items()
                     if st is NodeStatus.RUNNING]
            for nid in reset:
                self._statuses[nid] = NodeStatus.READY
            if self._graph is not None:
                for n in self._graph.nodes:
                    if n.kind is not NodeKind.BOUNDED_LOOP:
                        continue
                    if self._statuses.get(n.node_id) in (
                            NodeStatus.COMMITTED, NodeStatus.COMPENSATED):
                        continue
                    for child in self._graph.children_of(n.node_id):
                        if self._statuses.get(child.node_id) is not (
                                NodeStatus.INVALIDATED):
                            self._statuses[child.node_id] = NodeStatus.PENDING
            return reset

    def _recompute_ready_locked(self) -> None:
        """Propagate READY to a fixpoint (a container becoming READY can in
        turn unblock its child lanes), then auto-commit SEQUENCE/FAN_OUT
        containers whose children have ALL committed (a finished
        fan-out/sequence is itself complete).

        BOUNDED_LOOP is deliberately EXCLUDED from auto-commit: a loop
        commits only when its termination predicate evaluates true via the
        kernel's loop protocol — one fully-committed body pass says
        nothing about termination."""
        if self._graph is None:
            return
        while True:
            changed = False
            for n in self._graph.ready_nodes(self._statuses):
                if self._statuses.get(n.node_id) is NodeStatus.PENDING:
                    self._statuses[n.node_id] = NodeStatus.READY
                    changed = True
            for n in self._graph.nodes:
                if n.kind not in (NodeKind.SEQUENCE, NodeKind.FAN_OUT):
                    continue
                if self._statuses.get(n.node_id) not in (
                        NodeStatus.PENDING, NodeStatus.READY):
                    continue
                children = self._graph.children_of(n.node_id)
                if children and all(
                        self._statuses.get(c.node_id) is NodeStatus.COMMITTED
                        for c in children):
                    self._statuses[n.node_id] = NodeStatus.COMMITTED
                    changed = True
            if not changed:
                return

    def invalidate_future(self) -> list[str]:
        """Mark every non-historical node INVALIDATED (used by GoalPatch to
        void the old future, and when an applied compensation restores a
        pre-GoalPatch intent). Ephemeral loop-body commits are NOT
        historical (audit G13d). Returns the invalidated ids."""
        with self._lock:
            historical = self._historical_locked()
            out = sorted(nid for nid in self._statuses if nid not in historical)
            for nid in out:
                self._statuses[nid] = NodeStatus.INVALIDATED
            return out

    def mark_compensated(self, node_ids) -> None:
        """Cross-intent rollback path: post-checkpoint committed nodes whose
        writes were undone are honestly marked COMPENSATED (they stay
        historical — a following recompose must carry them verbatim)."""
        with self._lock:
            for nid in node_ids:
                if self._statuses.get(nid) is NodeStatus.COMMITTED:
                    self._statuses[nid] = NodeStatus.COMPENSATED

    def rewind_to_boundary(self, committed: frozenset) -> None:
        """Same-intent rollback path (audit G6): restore the
        scheduler-visible frontier to the checkpoint boundary — boundary
        commits stay COMMITTED, everything else returns to PENDING and the
        READY fixpoint re-arms the same path deterministically (no Task
        Architect needed). The undone work's audit trail lives in the
        EventLog (COMPENSATION_APPLIED payload)."""
        with self._lock:
            for nid, st in list(self._statuses.items()):
                if nid in committed:
                    self._statuses[nid] = NodeStatus.COMMITTED
                elif st is NodeStatus.INVALIDATED:
                    continue
                else:
                    self._statuses[nid] = NodeStatus.PENDING
            self._recompute_ready_locked()

    def reset_loop_children(self, loop_id: str) -> None:
        """Bounded-loop protocol: reset the loop's body children for a new
        iteration (their per-iteration COMMITTED/FAILED is ephemeral).
        Internal to the kernel's loop driver — bypasses the normal
        transition table on purpose."""
        with self._lock:
            if self._graph is None or self._graph.node(loop_id) is None:
                raise ValidationError(f"unknown loop node {loop_id!r}")
            for child in self._graph.children_of(loop_id):
                st = self._statuses.get(child.node_id, NodeStatus.PENDING)
                if st is NodeStatus.RUNNING:
                    raise ValidationError(
                        f"loop body node {child.node_id!r} still RUNNING")
                if st is not NodeStatus.INVALIDATED:
                    self._statuses[child.node_id] = NodeStatus.READY

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
