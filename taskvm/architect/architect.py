"""TaskArchitect / Projection Composer — ONE call, ONE coherent architecture.

The SECOND high-level model role (architect contract §1). A single
``compose`` call jointly produces everything the old role-zoo produced
inconsistently (Milestone Suggester / rule-based Workflow Classifier /
GenUI structural decoder):

- milestones / checkpoints          → CHECKPOINT nodes (a committed one
                                       becomes a kernel CheckpointRecord)
- workflow topology                 → Sequence / Fan-out–Barrier–Fan-in /
                                       Bounded Loop (the only three
                                       primitives; domain-validated shapes)
- projection schema                 → semantic component tree (data deltas
                                       never re-compose it)
- semantic action contracts         → ActionContract (desired_state,
                                       completion, reversibility, risk)
- verification intent               → VERIFY nodes
- desired plane                     → variable targets

Production vs validation (layered ownership): assembly maps the model JSON
into ``taskvm.domain`` constructors; STATIC coherence (shape / key ⊆
variables / binding ⊆ variables / split-brain / orphan / task-level
governance handle) is proven by the domain's ``TaskArchitecture``
validating constructor — ONE owner. A failed construction triggers a
bounded repair (the ValidationError is fed back to the model); after
``max_repairs`` (default 3 — RFC-A01) the failure is honest and final.
There is NO fallback to a fixture/GT plan — ever.

Sequence semantics (RFC-A01 / W0.2): the system prompt promises "a
sequence's steps run in your listed order" — the LISTED order is the
model's ordering intent, so the assembler completes each sequence's
intra-container chain in listed order whenever the model's explicit
intra-sequence 'after' edges are CONSISTENT with that order (including
when there are none, or when an edge points at a node outside the
container — the phantom-fork shape). An explicit edge that CONTRADICTS
the listed order is an honest assembly rejection with a specific repair
guidance. Genuinely parallel steps belong in fan-out lanes + a barrier;
the domain's single-chain rule for sequences is untouched (kernel
scheduling primitive, frozen).

``recompose_future`` is the GoalPatch path: the kernel has already applied
``apply_goal_patch`` (history preserved, uncommitted future invalidated,
execution blocked). The architect re-organises ONLY the remaining future:
committed history is carried VERBATIM (same ids, same definitions — kernel
``replace_future`` enforces it), the model sees a committed-work summary and
the new goal, and the deterministic stitcher connects the fresh future to
the carried frontier. Nothing is re-executed from scratch.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace
from typing import Iterable

from taskvm.architect.compiler import CompilerResult
from taskvm.architect.noleak import (
    LEAK_REPAIR_GUIDANCE, assert_prompt_clean, repair_guidance,
    scan_json_values,
)
from taskvm.architect.port import (
    MODEL_ROLE_TASK_ARCHITECT, ModelCallLedger, ModelPort,
)
from taskvm.domain.architecture import TaskArchitecture
from taskvm.domain.contract import ActionContract, Reversibility
from taskvm.domain.errors import ValidationError
from taskvm.domain.intent import TaskIntent
from taskvm.domain.projection import ProjectionComponent, ProjectionSchema
from taskvm.domain.state import (
    MUTABILITY_EDITABLE, MUTABILITY_LOCKED, MUTABILITY_READONLY,
    SurfaceEvidence, SurfaceHandle, TaskVariable,
)
from taskvm.domain.workflow import (
    NodeKind, NodeStatus, WorkflowGraph, WorkflowNode,
)
from taskvm.kernel import WorkflowSnapshot
from taskvm.skills.loader import inject_skill

_REVERSIBILITY = {r.value: r for r in Reversibility}
_NODE_KINDS = {k.value: k for k in NodeKind}
_MUTABILITY = {MUTABILITY_EDITABLE, MUTABILITY_READONLY, MUTABILITY_LOCKED}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are the Task Architect of a Task Virtual Machine. Given the human goal \
and the current task state (semantic variables with their OBSERVED values), \
produce ONE coherent task architecture as JSON with EXACTLY this shape:
{"variables": [{"semantic_key": "<one of the given keys, or a new \
snake_case business key>", "label": "...", "value_type": \
"string|date|number|status|boolean", "mutability": "editable|readonly|locked",
  "desired": <target value for a written variable, else the observed value>}],
 "workflow": {"nodes": [
   {"kind": "sequence", "label": "<unique label>"},
   {"kind": "fan_out", "label": "<unique label>"},
   {"kind": "bounded_loop", "label": "...", "termination": "<state-driven \
termination predicate in words>", "max_iterations": 3},
   {"kind": "action", "label": "...", "container": "<container label or null>",
    "after": ["<labels this node waits for>"],
    "semantic_goal": "<what must become true, in business words>",
    "sets": {"<semantic_key>": "<target value>"} (or {} for a navigation/
     observation/trigger step that writes no variable),
    "completion": "<how a user recognises completion on the visible screen>",
    "reversibility": "reversible|partially_reversible|irreversible",
    "risk": "<one short risk note, or empty>",
    "target_evidence": ["<a visible label a user could read on screen>"]},
   {"kind": "verify", "label": "...", "container": "...", "after": [],
    "condition": "<semantic verification condition>"},
   {"kind": "checkpoint", "label": "...", "after": []},
   {"kind": "barrier", "label": "...", "after": ["<the fan-out lanes, or the \
fan-out container>"]},
   {"kind": "terminal", "label": "<unique label>"}]},
 "projection": {"root": "<component label>", "components": [
   {"label": "<unique label>", "type": "card|field|list|progress|note",
    "binds": "<semantic_key or null>", "editable": false,
    "children": ["<child component labels>"]}]}}
Topology rules: use ONLY sequence / fan-out+barrier / bounded-loop shapes; \
a sequence's steps run in your listed order — list them in execution order \
and give each step 'after' the previous step of the same sequence (a step \
may also wait on nodes outside the sequence); steps that should run in \
parallel belong in fan-out lanes, which are independent and re-join at \
exactly one barrier; a bounded loop has BOTH a termination \
predicate and max_iterations and its body is only action/verify nodes; there \
is EXACTLY ONE terminal and it is the final sink; place checkpoint nodes at \
boundaries worth pausing or rolling back to; every variable whose desired \
value differs from observed must be finally written by some action to that \
exact value; at least one action in the whole task must write a variable \
(navigation/observation/trigger steps may keep 'sets' empty); \
address targets ONLY by visible labels and business keys, \
never by internal ids or operator names.
Output ONLY the JSON object."""


class ArchitectOutputError(ValidationError):
    """The model output could not be assembled after bounded repair."""


@dataclass(frozen=True)
class RecomposeProposal:
    """Everything ``kernel.recompose`` needs to close a GoalPatch."""

    variables: tuple[TaskVariable, ...]
    graph: WorkflowGraph
    schema: ProjectionSchema | None
    carried_node_ids: tuple[str, ...]
    reason: str


def historical_node_ids(snapshot: WorkflowSnapshot) -> frozenset[str]:
    """Derive the historical node set from the PUBLIC snapshot.

    COMMITTED/COMPENSATED nodes, minus ephemeral loop-body commits (a
    committed child of a not-yet-committed BOUNDED_LOOP is per-iteration
    scratch, not history — mirrors WorkflowStore's own rule, computed from
    public data only; the store stays authoritative).  TERMINAL nodes are
    structural sentinels, not committed work — they are excluded so the
    recomposed future produces its own fresh terminal (carrying an old
    terminal + adding a new one would violate the exactly-one-TERMINAL
    invariant).
    """
    graph = snapshot.graph
    if graph is None:
        return frozenset()
    out: set[str] = set()
    for nid, st in snapshot.statuses.items():
        if st not in (NodeStatus.COMMITTED, NodeStatus.COMPENSATED):
            continue
        node = graph.node(nid)
        if node is not None and node.kind is NodeKind.TERMINAL:
            continue  # structural sentinel, not history
        if node is not None and node.parent_id is not None:
            parent = graph.node(node.parent_id)
            if (parent is not None
                    and parent.kind is NodeKind.BOUNDED_LOOP
                    and snapshot.statuses.get(parent.node_id) not in (
                        NodeStatus.COMMITTED, NodeStatus.COMPENSATED)):
                continue  # ephemeral per-iteration commit
        out.add(nid)
    return frozenset(out)


def _closure(graph: WorkflowGraph,
             seeds: Iterable[str]) -> tuple[WorkflowNode, ...]:
    """Nodes the seeds structurally require: depends_on targets, the parent
    chain, and (for fan-outs / bounded loops, which must have members)
    COMMITTED children only — recursively. Deterministic order = graph
    order.

    Uncommitted siblings (an old-future lane the GoalPatch just
    invalidated) are deliberately NOT carried: they belong to the future
    the architect is about to redesign, and carrying them would either
    re-schedule stale targets or trip the split-brain guard against the
    new writers. A fan-out with a single remaining committed lane is
    still a valid shape (domain rule: ≥1 lane)."""
    by_id = {n.node_id: n for n in graph.nodes}
    committed = frozenset(seeds)
    carried: dict[str, WorkflowNode] = {}
    frontier = [by_id[s] for s in seeds if s in by_id]
    while frontier:
        n = frontier.pop()
        if n.node_id in carried:
            continue
        carried[n.node_id] = n
        for dep in n.depends_on:
            if dep in by_id and dep not in carried:
                frontier.append(by_id[dep])
        if n.parent_id is not None and n.parent_id in by_id:
            frontier.append(by_id[n.parent_id])
        if n.kind in (NodeKind.FAN_OUT, NodeKind.BOUNDED_LOOP):
            for child in graph.children_of(n.node_id):
                if child.node_id in committed:
                    frontier.append(child)
    return tuple(n for n in graph.nodes if n.node_id in carried)


class TaskArchitect:
    """Goal + observed state → one validated TaskArchitecture."""

    def __init__(self, port: ModelPort, ledger: ModelCallLedger | None = None,
                 *, model: str | None = None, max_repairs: int = 3) -> None:
        self._port = port
        self._ledger = ledger
        self._model = model
        if max_repairs < 0:
            raise ValidationError("max_repairs must be >= 0")
        self._max_repairs = max_repairs

    # ── initial composition ─────────────────────────────────────────────
    def compose(self, intent: TaskIntent,
                variables: Iterable[TaskVariable] | CompilerResult, *,
                purpose: str = "initial_compose") -> TaskArchitecture:
        """ONE model call → the complete coherent artifact.

        ``variables``: a :class:`CompilerResult` or an iterable of
        TaskVariables carrying the OBSERVED plane (from the State Compiler).
        The merge is one-way: observed / evidence / confidence come only
        from observation; the architect contributes desired / mutability /
        label / new variables. It can never rewrite an observed value.
        """
        base_vars = self._base_variables(variables)
        user = self._build_user_prompt(intent, base_vars,
                                       committed_summary=None)
        return self._compose_with_repair(intent, base_vars, user,
                                         carried=(), id_offset=0,
                                         exempt_ids=frozenset(),
                                         purpose=purpose)

    # ── GoalPatch recomposition (affected future only) ──────────────────
    def recompose_future(self, intent: TaskIntent,
                         variables: Iterable[TaskVariable] | CompilerResult,
                         snapshot: WorkflowSnapshot, *,
                         reason: str = "goal patch",
                         purpose: str = "goal_recompose") -> RecomposeProposal:
        """Re-organise ONLY the uncommitted future after a GoalPatch.

        Committed history (see :func:`historical_node_ids`) is carried
        verbatim — same ids, same definitions — plus its structural closure
        (dependencies / parents / required members), so the installed graph
        stays well-formed. The model only designs the remaining future
        against the new goal; committed work is never re-executed.
        """
        base_vars = self._base_variables(variables)
        graph = snapshot.graph
        if graph is None:
            raise ValidationError(
                "recompose_future requires an installed workflow graph")
        historical = historical_node_ids(snapshot)
        carried = _closure(graph, historical)
        summary = self._committed_summary(graph, historical)
        user = self._build_user_prompt(intent, base_vars,
                                       committed_summary=summary)
        arch = self._compose_with_repair(
            intent, base_vars, user, carried=carried,
            id_offset=self._max_numeric_suffix(carried) + 1,
            exempt_ids=frozenset(historical),
            purpose=purpose)
        return RecomposeProposal(
            variables=arch.variables, graph=arch.graph,
            schema=arch.schema,
            carried_node_ids=tuple(n.node_id for n in carried),
            reason=reason)

    # ── model-call loop with bounded repair ─────────────────────────────
    def _compose_with_repair(self, intent: TaskIntent,
                             base_vars: tuple[TaskVariable, ...],
                             user: str, *, carried: tuple[WorkflowNode, ...],
                             id_offset: int,
                             exempt_ids: frozenset[str],
                             purpose: str) -> TaskArchitecture:
        repair_note = ""
        last_err: Exception | None = None
        for attempt in range(1 + self._max_repairs):
            is_repair = attempt > 0
            exact_user = user + repair_note
            # EVERY message actually sent — the initial
            # one AND each repair round — passes the no-leak gate on the
            # exact text about to go out. The leak repair note is token-free
            # (LEAK_REPAIR_GUIDANCE); any other note that unexpectedly
            # carries internal vocabulary fails honestly at this line.
            # The skill injection (R2.5) happens BEFORE the gate, so a
            # distilled skill is scanned like any other prompt text.
            assert_prompt_clean(
                inject_skill("architect", _SYSTEM_PROMPT) + "\n" + exact_user,
                what="task-architect prompt")
            reply = self._call_model(exact_user, purpose=purpose,
                                     is_repair=is_repair)
            parsed = reply.parsed
            if not isinstance(parsed, dict) or "workflow" not in parsed:
                last_err = ArchitectOutputError(
                    "architect output is not a JSON object with 'workflow'")
                repair_note = self._repair_note(last_err)
                continue
            leaks = scan_json_values(parsed)
            if leaks:
                # the offending tokens go into the error (honest failure
                # detail) but NEVER back into the model prompt (no-leak contract).
                last_err = ArchitectOutputError(
                    "architect output echoes internal vocabulary: "
                    f"{sorted(set(leaks))}")
                repair_note = LEAK_REPAIR_GUIDANCE
                continue
            try:
                return self._assemble(parsed, base_vars, carried, id_offset,
                                      exempt_ids)
            except ValidationError as e:
                last_err = e
                repair_note = self._repair_note(e)
        raise ArchitectOutputError(
            f"task architect failed after {1 + self._max_repairs} attempt(s); "
            f"last error: {last_err}")

    def _call_model(self, user: str, *, purpose: str, is_repair: bool):
        system = inject_skill("architect", _SYSTEM_PROMPT)
        if self._ledger is None:
            return self._port.complete_json(
                system=system, user=user, model=self._model)
        from taskvm.architect.port import ModelCallRecord
        t0 = time.monotonic()
        reply = None
        try:
            reply = self._port.complete_json(
                system=system, user=user, model=self._model)
            return reply
        finally:
            self._ledger.record(ModelCallRecord(
                role=MODEL_ROLE_TASK_ARCHITECT, purpose=purpose,
                model=(reply.model if reply else (self._model or "")),
                ok=reply is not None and reply.parsed is not None,
                is_repair=is_repair,
                prompt_tokens=(reply.prompt_tokens if reply else None),
                completion_tokens=(reply.completion_tokens if reply else None),
                latency_ms=int((time.monotonic() - t0) * 1000)))

    # ── prompt building ─────────────────────────────────────────────────
    @staticmethod
    def _base_variables(variables) -> tuple[TaskVariable, ...]:
        if isinstance(variables, CompilerResult):
            return tuple(variables.variables)
        return tuple(variables)

    @staticmethod
    def _build_user_prompt(intent: TaskIntent,
                           base_vars: tuple[TaskVariable, ...],
                           committed_summary: str | None) -> str:
        parts = ["# Task goal", intent.goal]
        if intent.constraints:
            parts.append("Constraints: " + "; ".join(intent.constraints))
        if intent.success_criteria:
            parts.append("Success criteria: "
                         + "; ".join(intent.success_criteria))
        parts += ["", "# Current task state (observed plane — from the "
                      "State Compiler; you set ONLY the desired plane)"]
        for v in base_vars:
            parts.append(f"- {v.semantic_key} ({v.label}, type="
                         f"{v.value_type}, mutability={v.mutability}, "
                         f"observed={v.observed!r})")
        if committed_summary:
            parts += ["", "# Already COMMITTED history (labels are frozen; "
                          "design ONLY the remaining future; new nodes may "
                          "wait 'after' these labels)",
                      committed_summary]
        parts += ["", "Produce the task architecture now. Output ONLY the "
                      "JSON object."]
        return "\n".join(parts)

    @staticmethod
    def _committed_summary(graph: WorkflowGraph,
                           historical: frozenset[str]) -> str:
        lines = []
        for n in graph.nodes:
            if n.node_id not in historical:
                continue
            extra = f" — did: {n.contract.semantic_goal}" if (
                n.contract is not None) else ""
            lines.append(f"- [committed] {n.label} ({n.kind.value}){extra}")
        return "\n".join(lines) if lines else "(no committed work yet)"

    @staticmethod
    def _repair_note(err: Exception) -> str:
        # the raw error text may quote internal
        # ids minted by the assembly (n001/c001/…) — only the business-level
        # category goes back to the model; the detail stays in the log and
        # in the exception for the honest-failure path.
        logger.debug("task-architect repair: %s: %s", type(err).__name__, err)
        return ("\n\nYour previous output was rejected: "
                + repair_guidance(err)
                + " Fix that and output the corrected JSON object only.")

    @staticmethod
    def _max_numeric_suffix(nodes: tuple[WorkflowNode, ...]) -> int:
        best = 0
        for n in nodes:
            m = re.search(r"(\d+)$", n.node_id)
            if m:
                best = max(best, int(m.group(1)))
        return best

    # ── deterministic JSON → domain assembly ────────────────────────────
    def _assemble(self, parsed: dict, base_vars: tuple[TaskVariable, ...],
                  carried: tuple[WorkflowNode, ...], id_offset: int,
                  exempt_ids: frozenset[str]) -> TaskArchitecture:
        variables = self._merge_variables(parsed, base_vars)
        graph = self._assemble_graph(parsed, variables, carried, id_offset)
        schema = self._assemble_schema(parsed, variables, id_offset)
        # exempt_ids = frozen history (committed/compensated): their
        # contracts are records of verified work, exempt from coherence —
        # the SAME exemption kernel.recompose re-proves at install time
        # (workflow_store.historical_node_ids).
        return TaskArchitecture(variables=variables, graph=graph,
                                schema=schema, exempt_node_ids=exempt_ids)

    @staticmethod
    def _merge_variables(parsed: dict,
                         base_vars: tuple[TaskVariable, ...],
                         ) -> tuple[TaskVariable, ...]:
        """One-way merge: observation owns ``observed``/``evidence``/
        ``confidence``; the architect contributes ``desired`` /
        ``mutability`` / ``label`` / new variables — never an observed
        value."""
        raw = parsed.get("variables")
        if not isinstance(raw, list) or not raw:
            raise ArchitectOutputError(
                "'variables' must be a non-empty array")
        by_key = {v.semantic_key: v for v in base_vars}
        out: list[TaskVariable] = []
        seen: set[str] = set()
        for i, rv in enumerate(raw):
            if not isinstance(rv, dict):
                raise ArchitectOutputError(f"variable #{i} is not an object")
            key = str(rv.get("semantic_key") or "").strip()
            if not _KEY_RE.match(key):
                raise ArchitectOutputError(
                    f"variable #{i} semantic_key {key!r} is not lower "
                    f"snake_case")
            if key in seen:
                raise ArchitectOutputError(f"duplicate semantic_key {key!r}")
            seen.add(key)
            base = by_key.get(key)
            label = str(rv.get("label") or (base.label if base else key))
            value_type = str(rv.get("value_type")
                             or (base.value_type if base else "string"))
            mutability = str(rv.get("mutability")
                             or (base.mutability if base
                                 else MUTABILITY_EDITABLE))
            if mutability not in _MUTABILITY:
                raise ArchitectOutputError(
                    f"variable {key!r} mutability {mutability!r} unknown")
            desired = rv.get("desired")
            if desired is None:
                desired = base.observed if base is not None else None
            if base is not None:
                out.append(replace(base, label=label, value_type=value_type,
                                   mutability=mutability, desired=desired))
            else:
                out.append(TaskVariable(
                    semantic_key=key, label=label, observed=None,
                    desired=desired, value_type=value_type,
                    mutability=mutability))
        return tuple(out)

    def _assemble_graph(self, parsed: dict,
                        variables: tuple[TaskVariable, ...],
                        carried: tuple[WorkflowNode, ...],
                        id_offset: int) -> WorkflowGraph:
        wf = parsed.get("workflow")
        raw_nodes = wf.get("nodes") if isinstance(wf, dict) else None
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ArchitectOutputError("'workflow.nodes' must be non-empty")

        # label → id map: carried nodes first (their labels are frozen),
        # then new nodes in listed order.
        label_to_id: dict[str, str] = {n.label: n.node_id for n in carried}
        seq = id_offset

        def next_id() -> str:
            nonlocal seq
            seq += 1
            return f"n{seq:03d}"

        parsed_nodes: list[dict] = []
        for i, rn in enumerate(raw_nodes):
            if not isinstance(rn, dict):
                raise ArchitectOutputError(f"workflow node #{i} not an object")
            kind = str(rn.get("kind") or "")
            if kind not in _NODE_KINDS:
                raise ArchitectOutputError(
                    f"workflow node #{i} kind {kind!r} unknown")
            label = str(rn.get("label") or "").strip()
            if not label:
                raise ArchitectOutputError(
                    f"workflow node #{i} needs a non-empty label")
            if label in label_to_id:
                raise ArchitectOutputError(
                    f"duplicate workflow label {label!r} (a committed "
                    f"history label must not be reused)")
            label_to_id[label] = next_id()
            parsed_nodes.append(rn)

        # build the compiler-minted handle index
        # from the observed variables' evidence — a Task Architect action
        # targeting a label the State Compiler already grounded MUST reuse
        # that same handle (same opaque id), so the surface binding chain
        # compiler → contract → runtime resolver stays unbroken. New labels
        # the compiler never saw get fresh architect handles (ha…) — those
        # carry no surface provenance and rely on the runtime's unambiguous
        # single-surface case or fail honestly in multi-surface sessions.
        handle_by_label: dict[str, SurfaceHandle] = {}
        for v in variables:
            for ev in (v.evidence or ()):
                if ev.visible_label and ev.visible_label not in handle_by_label:
                    handle_by_label[ev.visible_label] = ev.surface

        def _action_handle(label_str: str, seq: int) -> SurfaceHandle:
            reused = handle_by_label.get(label_str)
            return reused if reused is not None else SurfaceHandle(
                handle_id=f"ha{seq:03d}")

        def ids_of(labels, owner: str) -> tuple[str, ...]:
            out = []
            for lb in labels or []:
                lb = str(lb)
                if lb not in label_to_id:
                    raise ArchitectOutputError(
                        f"{owner} references unknown node label {lb!r}")
                out.append(label_to_id[lb])
            return tuple(out)

        new_nodes: list[WorkflowNode] = []
        for rn in parsed_nodes:
            nid = label_to_id[str(rn["label"])]
            kind = _NODE_KINDS[str(rn["kind"])]
            parent_label = rn.get("container") or rn.get("parent")
            parent_id = None
            if parent_label:
                if parent_label not in label_to_id:
                    raise ArchitectOutputError(
                        f"node {rn['label']!r} container {parent_label!r} "
                        f"unknown")
                parent_id = label_to_id[parent_label]
            depends_on = ids_of(rn.get("after"), f"node {rn['label']!r}")
            contract = None
            termination = None
            max_iters = None
            verification = None
            if kind is NodeKind.ACTION:
                # RFC-A01 / W0.2: an action may be a navigation /
                # observation / trigger step — ``sets: {}`` is legal at
                # NODE level. The governance guarantee moved to TASK level
                # (TaskArchitecture: at least one action writes a
                # variable), so the contract keeps its handle without
                # misclassifying semantically-correct model output.
                sets = rn.get("sets")
                if sets is None:
                    sets = {}
                if not isinstance(sets, dict):
                    raise ArchitectOutputError(
                        f"action {rn['label']!r} 'sets' must be an object "
                        f"mapping semantic keys to target values (it may "
                        f"be empty for a navigation/observation/trigger "
                        f"step)")
                rev = str(rn.get("reversibility") or "reversible")
                if rev not in _REVERSIBILITY:
                    raise ArchitectOutputError(
                        f"action {rn['label']!r} reversibility {rev!r} "
                        f"unknown")
                ev = rn.get("target_evidence") or []
                if not isinstance(ev, list):
                    raise ArchitectOutputError(
                        f"action {rn['label']!r} target_evidence must be a "
                        f"list of visible labels")
                evidence = tuple(
                    SurfaceEvidence(
                        surface=_action_handle(
                            str(s).strip(), e_i + id_offset),
                        visible_label=str(s))
                    for e_i, s in enumerate(ev, start=1)
                    if str(s).strip())
                contract = ActionContract(
                    contract_id=f"c{label_to_id[str(rn['label'])][1:]}",
                    semantic_goal=str(rn.get("semantic_goal")
                                      or rn["label"]),
                    desired_state=dict(sets),
                    completion_condition=str(rn.get("completion") or ""),
                    target_evidence=evidence,
                    reversibility=_REVERSIBILITY[rev],
                    risk_note=str(rn.get("risk") or ""))
            elif kind is NodeKind.VERIFY:
                verification = str(rn.get("condition") or "").strip()
                if not verification:
                    raise ArchitectOutputError(
                        f"verify node {rn['label']!r} needs a 'condition'")
            elif kind is NodeKind.BOUNDED_LOOP:
                termination = str(rn.get("termination") or "").strip()
                if not termination:
                    raise ArchitectOutputError(
                        f"bounded loop {rn['label']!r} needs a termination "
                        f"predicate")
                try:
                    max_iters = int(rn.get("max_iterations"))
                except (TypeError, ValueError):
                    max_iters = 0
                if max_iters is None or max_iters < 1:
                    raise ArchitectOutputError(
                        f"bounded loop {rn['label']!r} needs "
                        f"max_iterations >= 1")
            new_nodes.append(WorkflowNode(
                node_id=nid, kind=kind, label=str(rn["label"]),
                depends_on=depends_on, parent_id=parent_id,
                contract=contract, verification=verification,
                termination_predicate=termination,
                max_iterations=max_iters))

        nodes = list(carried) + new_nodes
        nodes = self._chain_fill(nodes, new_nodes, carried)
        return WorkflowGraph(nodes=tuple(nodes))

    @staticmethod
    def _chain_fill(nodes: list[WorkflowNode],
                    new_nodes: list[WorkflowNode],
                    carried: tuple[WorkflowNode, ...]) -> list[WorkflowNode]:
        """Deterministic order-fill + history stitching.

        1. SEQUENCE containers: the listed order IS the model's ordering
           intent (the system prompt promises "steps run in your listed
           order"), so complete each sequence's intra-container chain in
           listed order — adding the missing consecutive edges — whenever
           the model's explicit intra-sequence edges are CONSISTENT with
           that order. This covers both the no-explicit-edges case and the
           partial-edges case (including edges that point at nodes OUTSIDE
           the container — the phantom-fork shape where a step's only
           dependency is an external checkpoint). An explicit edge that
           CONTRADICTS the listed order is rejected honestly with a
           specific message (business labels only, no internal ids).
        2. Top level with no intra-top edges → listed order (unchanged).
        3. Stitch the carried frontier: the terminal also depends on every
           carried TOP-LEVEL node it cannot already reach — committed
           history must remain on the path to the end. This can never
           create a cycle: carried nodes never depend on new nodes.
        """
        by_id = {n.node_id: n for n in nodes}
        top_new_ids = {n.node_id for n in new_nodes if n.parent_id is None}

        # 1. sequence containers: complete the chain in listed order
        #    (consistency-checked — see docstring)
        for cont in [n for n in new_nodes
                     if n.kind is NodeKind.SEQUENCE]:
            child_ids = [c.node_id for c in nodes
                         if c.parent_id == cont.node_id]
            if len(child_ids) <= 1:
                continue
            child_set = set(child_ids)
            pos = {cid: i for i, cid in enumerate(child_ids)}
            for cid in child_ids:
                for dep in by_id[cid].depends_on:
                    if dep in child_set and pos[dep] > pos[cid]:
                        raise ArchitectOutputError(
                            f"sequence {cont.label!r}: the listed order "
                            f"puts step {by_id[cid].label!r} before step "
                            f"{by_id[dep].label!r}, but the former waits "
                            f"'after' the latter — list the steps in "
                            f"execution order or drop that 'after' edge")
            for prev_nid, next_nid in zip(child_ids, child_ids[1:]):
                node = by_id[next_nid]
                if prev_nid not in node.depends_on:
                    by_id[next_nid] = replace(
                        node,
                        depends_on=node.depends_on + (prev_nid,))
        # 2. top level with no intra-top edges → listed order
        def has_explicit_edges(scope_ids: set[str]) -> bool:
            return any(set(by_id[nid].depends_on)
                       & (scope_ids - {nid})
                       for nid in scope_ids if nid in by_id)

        if len(top_new_ids) > 1 and not has_explicit_edges(top_new_ids):
            chain = [n.node_id for n in nodes if n.node_id in top_new_ids]
            for prev_nid, next_nid in zip(chain, chain[1:]):
                node = by_id[next_nid]
                if prev_nid not in node.depends_on:
                    by_id[next_nid] = replace(
                        node, depends_on=node.depends_on + (prev_nid,))
        # refresh to the CURRENT edge set before reachability
        nodes = [by_id[n.node_id] for n in nodes]
        # 2. stitch carried frontier to the terminal
        terminals = [n for n in nodes if n.kind is NodeKind.TERMINAL]
        if terminals and carried:
            term = terminals[0]
            succ: dict[str, set[str]] = {}
            for n in nodes:
                for d in n.depends_on:
                    succ.setdefault(d, set()).add(n.node_id)
                if n.parent_id is not None:
                    succ.setdefault(n.node_id, set()).add(n.parent_id)

            def reaches(start: str, target: str) -> bool:
                seen = {start}
                stack = [start]
                while stack:
                    cur = stack.pop()
                    if cur == target:
                        return True
                    for s in succ.get(cur, ()):
                        if s not in seen:
                            seen.add(s)
                            stack.append(s)
                return False

            carried_tops = [c for c in carried if c.parent_id is None]
            extra = tuple(c.node_id for c in carried_tops
                          if not reaches(c.node_id, term.node_id))
            if extra:
                by_id[term.node_id] = replace(
                    term, depends_on=term.depends_on + extra)
                nodes = [by_id[n.node_id] for n in nodes]
        return nodes

    @staticmethod
    def _assemble_schema(parsed: dict,
                         variables: tuple[TaskVariable, ...],
                         id_offset: int) -> ProjectionSchema | None:
        proj = parsed.get("projection")
        if not isinstance(proj, dict):
            return None
        raw = proj.get("components")
        if not isinstance(raw, list) or not raw:
            return None
        keys = {v.semantic_key for v in variables}
        editable_by_key = {v.semantic_key: v.mutability == MUTABILITY_EDITABLE
                           for v in variables}
        label_to_id: dict[str, str] = {}
        comps: list[dict] = []
        for i, rc in enumerate(raw):
            if not isinstance(rc, dict):
                raise ArchitectOutputError(
                    f"projection component #{i} is not an object")
            label = str(rc.get("label") or "").strip()
            if not label:
                raise ArchitectOutputError(
                    f"projection component #{i} needs a label")
            if label in label_to_id:
                raise ArchitectOutputError(
                    f"duplicate projection label {label!r}")
            cid = f"p{id_offset + i + 1:03d}"
            label_to_id[label] = cid
            comps.append(rc)
        components: list[ProjectionComponent] = []
        for rc in comps:
            binds = rc.get("binds")
            binding_key = (str(binds).strip() or None) if binds else None
            if binding_key is not None and binding_key not in keys:
                raise ArchitectOutputError(
                    f"projection component {rc['label']!r} binds unknown "
                    f"variable {binding_key!r}")
            children = []
            for ch in rc.get("children") or []:
                ch = str(ch)
                if ch not in label_to_id:
                    raise ArchitectOutputError(
                        f"projection component {rc['label']!r} references "
                        f"unknown child {ch!r}")
                children.append(label_to_id[ch])
            editable = bool(rc.get("editable"))
            if binding_key is not None:
                editable = editable and editable_by_key[binding_key]
            components.append(ProjectionComponent(
                component_id=label_to_id[str(rc["label"])],
                component_type=str(rc.get("type") or "field"),
                label=str(rc["label"]), binding_key=binding_key,
                children=tuple(children), editable=editable))
        root = str(proj.get("root") or "").strip()
        if not root:
            raise ArchitectOutputError("projection needs a 'root' label")
        if root not in label_to_id:
            raise ArchitectOutputError(
                f"projection root {root!r} is not a component label")
        return ProjectionSchema(root_id=label_to_id[root],
                                components=tuple(components))
