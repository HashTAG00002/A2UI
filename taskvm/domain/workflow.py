"""Workflow — the three research primitives, nothing more (mental-model
doc §5, handoff 02 'Workflow'):

    Sequence          — nodes with depends_on edges, executed in order
    Fan-out / Barrier — parallel lanes + a fan-in verification point
    Bounded loop      — a repeating unit with a termination predicate
                        AND a max-iteration guard (both mandatory)

This is NOT a general DAG programming language. A ``WorkflowGraph`` is a
flat, validated node set: containers (SEQUENCE / FAN_OUT / BOUNDED_LOOP)
group children via ``parent_id``; ordering and fan-in are expressed via
``depends_on``. Node *status* lives in the kernel's WorkflowStore, not in
the graph itself — the graph is the plan; the store is the truth about
execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from taskvm.domain.contract import ActionContract
from taskvm.domain.errors import ValidationError


class NodeKind(str, Enum):
    SEQUENCE = "sequence"        # ordered container
    FAN_OUT = "fan_out"          # parallel-lane container
    BARRIER = "barrier"          # fan-in point after a fan-out
    BOUNDED_LOOP = "bounded_loop"
    ACTION = "action"
    VERIFY = "verify"
    CHECKPOINT = "checkpoint"
    TERMINAL = "terminal"


class NodeStatus(str, Enum):
    PENDING = "pending"          # declared, dependencies unmet
    READY = "ready"              # dependencies committed; may be scheduled
    RUNNING = "running"          # an action of the current epoch is in flight
    COMMITTED = "committed"      # independently verified — immutable history
    FAILED = "failed"            # verification failed; may be re-attempted
    INVALIDATED = "invalidated"  # superseded by a patch (pre-commit only)
    COMPENSATED = "compensated"  # was committed, later undone by compensation


# statuses a GoalPatch must preserve verbatim (invariant 3)
HISTORICAL_STATUSES = frozenset({NodeStatus.COMMITTED, NodeStatus.COMPENSATED})


@dataclass(frozen=True)
class WorkflowNode:
    """One plan node. ``label`` is the business-visible name the user
    sees in the progress topology."""

    node_id: str
    kind: NodeKind
    label: str
    depends_on: tuple[str, ...] = ()
    parent_id: str | None = None
    # ACTION payload
    contract: ActionContract | None = None
    # VERIFY payload: semantic predicate description
    verification: str | None = None
    # BOUNDED_LOOP guards (both mandatory for that kind)
    termination_predicate: str | None = None
    max_iterations: int | None = None

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValidationError("WorkflowNode.node_id must be non-empty")
        if not isinstance(self.kind, NodeKind):
            object.__setattr__(self, "kind", NodeKind(self.kind))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))


@dataclass(frozen=True)
class WorkflowGraph:
    """A validated plan. ``revision`` and ``epoch`` are kernel-assigned;
    constructors outside the kernel should leave them at 0."""

    nodes: tuple[WorkflowNode, ...] = ()
    revision: int = 0
    epoch: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        self.validate()

    # ── validation ──────────────────────────────────────────────────────
    def validate(self) -> None:
        ids = [n.node_id for n in self.nodes]
        if len(set(ids)) != len(ids):
            raise ValidationError(f"duplicate node_id in WorkflowGraph: {ids}")
        known = set(ids)
        kinds = {n.node_id: n.kind for n in self.nodes}
        for n in self.nodes:
            for dep in n.depends_on:
                if dep not in known:
                    raise ValidationError(
                        f"node {n.node_id!r} depends on unknown node {dep!r}")
                if dep == n.node_id:
                    raise ValidationError(f"node {n.node_id!r} depends on itself")
            if n.parent_id is not None:
                if n.parent_id not in known:
                    raise ValidationError(
                        f"node {n.node_id!r} has unknown parent {n.parent_id!r}")
                if kinds[n.parent_id] not in (
                        NodeKind.SEQUENCE, NodeKind.FAN_OUT, NodeKind.BOUNDED_LOOP):
                    raise ValidationError(
                        f"node {n.node_id!r} parent {n.parent_id!r} is not a container")
            if n.kind is NodeKind.BOUNDED_LOOP:
                if not n.termination_predicate:
                    raise ValidationError(
                        f"bounded loop {n.node_id!r} needs a termination predicate")
                if not n.max_iterations or n.max_iterations < 1:
                    raise ValidationError(
                        f"bounded loop {n.node_id!r} needs max_iterations >= 1")
            if n.kind is NodeKind.BARRIER and not n.depends_on:
                raise ValidationError(
                    f"barrier {n.node_id!r} needs fan-in depends_on edges")
            if n.kind is NodeKind.ACTION and n.contract is None:
                raise ValidationError(f"action node {n.node_id!r} needs a contract")
            if n.kind is NodeKind.VERIFY and not n.verification:
                raise ValidationError(
                    f"verify node {n.node_id!r} needs a verification condition")
            if n.kind is NodeKind.TERMINAL and n.contract is not None:
                raise ValidationError("terminal node cannot carry a contract")
        self._check_acyclic()
        fan_outs = [n for n in self.nodes if n.kind is NodeKind.FAN_OUT]
        for fo in fan_outs:
            if not any(n.parent_id == fo.node_id for n in self.nodes):
                raise ValidationError(
                    f"fan-out {fo.node_id!r} must contain at least one lane")

    def _check_acyclic(self) -> None:
        deps = {n.node_id: set(n.depends_on) for n in self.nodes}
        # include container membership as an ordering edge (child runs
        # within the container, never before it is scheduled)
        for n in self.nodes:
            if n.parent_id is not None:
                deps[n.node_id].add(n.parent_id)
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {k: WHITE for k in deps}

        def visit(u: str, stack: tuple[str, ...]) -> None:
            color[u] = GRAY
            for v in deps[u]:
                if color[v] == GRAY:
                    raise ValidationError(
                        f"workflow dependency cycle: {' -> '.join(stack + (v,))}")
                if color[v] == WHITE:
                    visit(v, stack + (v,))
            color[u] = BLACK

        for k in deps:
            if color[k] == WHITE:
                visit(k, (k,))

    # ── queries ─────────────────────────────────────────────────────────
    def node(self, node_id: str) -> WorkflowNode | None:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        return None

    def children_of(self, node_id: str) -> tuple[WorkflowNode, ...]:
        return tuple(n for n in self.nodes if n.parent_id == node_id)

    def terminal_nodes(self) -> tuple[WorkflowNode, ...]:
        return tuple(n for n in self.nodes if n.kind is NodeKind.TERMINAL)

    def ready_nodes(self, statuses: dict[str, NodeStatus]) -> tuple[WorkflowNode, ...]:
        """Nodes whose dependencies are all committed and which are not yet
        started. ``statuses`` maps node_id → NodeStatus (kernel store data)."""
        out = []
        for n in self.nodes:
            st = statuses.get(n.node_id, NodeStatus.PENDING)
            if st not in (NodeStatus.PENDING, NodeStatus.READY):
                continue
            if n.parent_id is not None and statuses.get(
                    n.parent_id, NodeStatus.PENDING) not in (
                    NodeStatus.READY, NodeStatus.RUNNING, NodeStatus.COMMITTED):
                continue
            if all(statuses.get(d, NodeStatus.PENDING) is NodeStatus.COMMITTED
                   for d in n.depends_on):
                out.append(n)
        return tuple(out)
