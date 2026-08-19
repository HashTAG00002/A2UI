"""TaskArchitecture — one validated composition: task variables + workflow
graph + projection schema.

Layered ownership (docs/contracts/layered_ownership_protocol.md §1/§5):
the STATIC coherence of a composition is CONTENT, owned here and proven
exactly once at construction:

- projection ``binding_key`` references ⊆ declared variables;
- ``ActionContract.desired_state`` keys ⊆ declared variables;
- split-brain guard: for every written key, the FINAL writer (in
  downstream order) must target the variable's ``desired`` value, and two
  unordered final writers may never disagree;
- no orphan work: every NON-EXEMPT node must be able to reach the
  TERMINAL (exempt frozen history may legitimately be a dead-end record);
- task-level governance handle: when the graph has ACTION nodes, at
  least one of them must write a task variable (non-empty
  ``desired_state``) — an observation / navigation / trigger action
  with an empty ``desired_state`` is legal, but a plan with actions
  where NONE ever writes leaves governance with nothing to manage,
  verify, or roll back (an action-free pure-verify probe stays legal);
- variables are unique within the composition.

The workflow's own static shape (three primitives, single sink TERMINAL)
is proven by ``WorkflowGraph`` itself; the projection tree by
``ProjectionSchema`` itself. This type owns only the CROSS-OBJECT
coherence. The kernel installs validated compositions and never
re-interprets their static shape.

This is deliberately NOT the Task Architect (Agent C): no model call, no
planner, no runtime state — a pure, immutable domain type.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from taskvm.domain.errors import ValidationError
from taskvm.domain.projection import ProjectionSchema
from taskvm.domain.state import TaskVariable
from taskvm.domain.workflow import NodeKind, WorkflowGraph


@dataclass(frozen=True)
class TaskArchitecture:
    """A statically coherent composition, validated at construction.

    ``exempt_node_ids`` carries the kernel's TEMPORAL knowledge of which
    nodes are frozen history (committed/compensated): their contracts are
    records of already-verified work and are exempt from coherence checks
    (they may even reference variables the new composition dropped).
    """

    variables: tuple[TaskVariable, ...] = ()
    graph: WorkflowGraph | None = None
    schema: ProjectionSchema | None = None
    exempt_node_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "variables", tuple(self.variables))
        object.__setattr__(self, "exempt_node_ids",
                           frozenset(self.exempt_node_ids))
        keys = [v.semantic_key for v in self.variables]
        if len(set(keys)) != len(keys):
            raise ValidationError(
                f"duplicate semantic_key in TaskArchitecture: {keys}")
        self.validate()

    def validate(self) -> None:
        desired: dict[str, Any] = {v.semantic_key: v.desired
                                   for v in self.variables}
        if self.schema is not None:
            missing = sorted({c.binding_key for c in self.schema.components
                              if c.binding_key is not None} - set(desired))
            if missing:
                raise ValidationError(
                    f"ProjectionSchema binds unknown task variables "
                    f"{missing}")
        if self.graph is None:
            return
        self._check_task_level_governance_handle()
        self._check_no_orphan_work()
        writers: dict[str, list[tuple[str, Any]]] = {}
        bad: dict[str, list[str]] = {}
        for n in self.graph.nodes:
            if n.contract is None or n.node_id in self.exempt_node_ids:
                continue
            for key, val in n.contract.desired_state.items():
                if key not in desired:
                    bad.setdefault(n.node_id, []).append(key)
                else:
                    writers.setdefault(key, []).append((n.node_id, val))
        if bad:
            raise ValidationError(
                "ActionContract desired_state references unknown task "
                f"variables: {bad}")
        for key, ws in writers.items():
            finals = [(nid, v) for nid, v in ws
                      if not any(other != nid
                                 and other in self.graph.downstream(nid)
                                 for other, _ in ws)]
            targets: list[Any] = []
            for _, v in finals:
                if v not in targets:
                    targets.append(v)
            if len(targets) > 1:
                raise ValidationError(
                    f"composition incoherent: {key!r} has multiple final "
                    f"writers with different targets {targets}")
            if targets and desired[key] != targets[0]:
                raise ValidationError(
                    f"composition incoherent (split-brain guard): variable "
                    f"{key!r} desired={desired[key]!r} but the plan's "
                    f"final writer targets {targets[0]!r}")

    def _check_task_level_governance_handle(self) -> None:
        """The WHOLE task needs at least one writing action (RFC-A01 /
        W0.2): a plan may freely mix observation / navigation / trigger
        actions whose ``desired_state`` is empty, but if the plan HAS
        actions and none ever writes a variable there is no governance
        handle — nothing the kernel could verify, patch, or compensate.
        Frozen history counts: a committed writer is still a writer (its
        contract is a record of verified work, exempt from coherence
        but not from existence). An ACTION-FREE plan (a pure verify /
        checkpoint probe) stays legal — it was legal before the rule and
        has no actions whose handles could go missing."""
        assert self.graph is not None  # called only from validate()'s graph branch
        actions = [n for n in self.graph.nodes
                   if n.kind is NodeKind.ACTION]
        if not actions:
            return
        if not any(n.contract is not None and n.contract.desired_state
                   for n in actions):
            raise ValidationError(
                "task-level governance handle missing: at least one action "
                "must write a task variable (non-empty 'sets'); observation "
                "or trigger actions with empty 'sets' are legal but cannot "
                "be the whole plan")

    def _check_no_orphan_work(self) -> None:
        """Every NON-EXEMPT node must be able to REACH the TERMINAL — a
        node whose result nobody can consume is work the plan can never
        honestly finish. Exempt (frozen-history) nodes may be dead-end
        records. Backwards reachability from the terminal over run-before
        edges: predecessors of a node are its ``depends_on``, its children
        (child completes before its container), and its parent (a
        container starts before its lanes)."""
        assert self.graph is not None  # called only from validate()'s graph branch
        graph = self.graph
        by_id = {n.node_id: n for n in graph.nodes}
        tid = graph.terminal_nodes()[0].node_id
        children: dict[str, set[str]] = {}
        for n in graph.nodes:
            if n.parent_id is not None:
                children.setdefault(n.parent_id, set()).add(n.node_id)
        reached = {tid}
        frontier = [tid]
        while frontier:
            cur = frontier.pop()
            preds = set(by_id[cur].depends_on) | children.get(cur, set())
            parent = by_id[cur].parent_id
            if parent is not None:
                preds.add(parent)
            for p in preds - reached:
                reached.add(p)
                frontier.append(p)
        # containers are skipped: their reachability is derivative of
        # their children's, and a container housing only historical or
        # invalidated children MUST still exist (the children's parent_id
        # references it) — its orphanhood would be a false positive. A
        # container with FUTURE work that leads nowhere is still caught,
        # because that work (a leaf) is itself unreachable.
        orphans = sorted(
            n.node_id for n in graph.nodes
            if n.node_id not in reached
            and n.node_id not in self.exempt_node_ids
            and n.kind not in (NodeKind.SEQUENCE, NodeKind.FAN_OUT,
                               NodeKind.BOUNDED_LOOP))
        if orphans:
            raise ValidationError(
                f"workflow node(s) {orphans} can never reach the TERMINAL "
                f"{tid!r} (orphan work the plan can never finish)")
