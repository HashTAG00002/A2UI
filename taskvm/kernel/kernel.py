"""TaskVMKernel — the state machine at L3 (master handoff §2).

The kernel owns the ONLY writers for every store. It is deliberately
small and boring: no Flask, no Playwright, no model calls, no substrate,
no benchmark — those live above/below and talk to the kernel through the
public methods here (docs/contracts/kernel.md).

EVENT SEMANTICS (fixed): every public mutating call appends EXACTLY ONE
event whose kind names the semantic operation. An observation folded by
``finish_action`` is part of the action landing (its keys ride in the
ACTION_FINISHED payload), not a separate event.

NODE ADVANCEMENT PROTOCOL (fixed):
  - ACTION (executable): request_action → start_action → finish_action →
    record_verification. Only ACTION nodes produce CUA work handles.
  - VERIFY (control): record_verification directly from READY — the
    runtime observes independently and reports; no action handle exists.
  - BARRIER / CHECKPOINT / TERMINAL (control): advance_control from
    READY. CHECKPOINT additionally writes a CheckpointRecord; TERMINAL
    commit means the plan is complete.

Enforced invariants (handoff 02 §Kernel 服务 + Wave-A review):
  1. revisions are store-assigned and strictly monotonic;
  2. projection schema/data revisions are independent counters;
  3. GoalPatch can never silently rewrite or drop committed nodes;
  4. action results from a stale epoch are discarded without touching
     TaskState (finish_action returns False, ActionDiscarded emitted);
  5. checkpoints pin an exact event-log index + state revision + epoch;
  6. compensation is grounded ONLY in the kernel's own checkpoint
     records, and a reported compensation is accepted only when freshly
     observed values match the plan's targets exactly;
  7. every read returns an immutable snapshot / defensive deep copy;
  8. patches are atomic: full validation before ANY mutation — a
     rejected patch leaves state, epoch, graph, and events untouched;
  9. observed vs desired are separate planes: observations write only
     ``observed``, patches write only ``desired``;
 10. action handles are single-use: REQUESTED → STARTED → FINISHED /
     DISCARDED; at most one ACTIVE handle per (node, epoch); a terminal
     handle can never land again; a result can never rewrite a committed
     node;
 11. compensation plans are epoch-bound and single-use: a stale or
     repeated landing is DISCARDED (its own event kind — never confused
     with an honest execution failure);
 12. a bounded loop commits ONLY via an explicit termination decision
     (begin_loop_iteration / evaluate_loop_termination) — never by
     children-pass auto-commit;
 13. composition boundaries (set_plan / recompose / apply_goal_patch)
     reject architect output whose projection bindings or contract keys
     reference unknown task variables.
"""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import replace
from typing import Any, Iterable

from taskvm.domain.errors import (
    CommittedNodeViolationError,
    PatchSemanticsError,
    ValidationError,
)
from taskvm.domain.events import Event, EventKind
from taskvm.domain.intent import TaskIntent
from taskvm.domain.patch import (
    CompensationEntry,
    CompensationPatch,
    CompensationPlan,
    GoalPatch,
    LocalPatch,
)
from taskvm.domain.projection import ProjectionSchema
from taskvm.domain.state import (
    MUTABILITY_EDITABLE,
    ObservedValue,
    TaskState,
    TaskVariable,
)
from taskvm.domain.workflow import NodeKind, NodeStatus, WorkflowGraph
from taskvm.kernel.checkpoint_store import CheckpointRecord, CheckpointStore
from taskvm.kernel.event_log import EventLog
from taskvm.kernel.projection_store import ProjectionSnapshot, ProjectionStore
from taskvm.kernel.session_store import TaskSessionStore
from taskvm.kernel.workflow_store import WorkflowSnapshot, WorkflowStore

_CONTROL_CONFIRM_KINDS = (NodeKind.BARRIER, NodeKind.CHECKPOINT,
                          NodeKind.TERMINAL)
_PROGRESS_KINDS = (NodeKind.ACTION, NodeKind.VERIFY, NodeKind.BARRIER,
                   NodeKind.CHECKPOINT, NodeKind.TERMINAL)


class TaskVMKernel:
    """One task session's kernel: stores + event log + patch state machine."""

    def __init__(self, session_id: str, intent: TaskIntent) -> None:
        self._sessions = TaskSessionStore(session_id, intent)
        self._projections = ProjectionStore()
        self._workflows = WorkflowStore()
        self._checkpoints = CheckpointStore()
        self._events = EventLog()
        self._lock = threading.RLock()
        self._action_seq = 0
        self._actions: dict[str, dict[str, Any]] = {}
        self._comp_seq = 0
        self._comp_plans: dict[str, CompensationPlan] = {}
        self._comp_status: dict[str, str] = {}   # pending/applied/failed/discarded
        self._loop_iters: dict[str, int] = {}    # loop node → current iteration

    # ══ introspection (snapshots — invariant 7) ═════════════════════════
    @property
    def session_id(self) -> str:
        return self._sessions.session_id

    @property
    def epoch(self) -> int:
        return self._sessions.epoch

    def task_state(self) -> TaskState:
        return self._sessions.task_state()

    def projection(self) -> ProjectionSnapshot:
        return self._projections.snapshot()

    def workflow(self) -> WorkflowSnapshot:
        return self._workflows.snapshot()

    def checkpoints(self) -> list[CheckpointRecord]:
        return self._checkpoints.all()

    def events(self) -> list[Event]:
        return self._events.all()

    # ══ event plumbing ═══════════════════════════════════════════════════
    def _emit(self, kind: EventKind, payload: dict[str, Any],
              correlation_id: str = "") -> Event:
        state = self._sessions.task_state()
        ev = Event(
            event_id=f"evt_{len(self._events) + 1:05d}",
            session_id=self.session_id,
            kind=kind,
            revision=state.revision,
            epoch=self._sessions.epoch,
            timestamp=time.time(),
            correlation_id=correlation_id,
            payload=payload,
        )
        self._events.append(ev)
        return ev

    # ══ composition (State Compiler / Task Architect output) ════════════
    def init_task_state(self, variables: Iterable[TaskVariable],
                        *, correlation_id: str = "") -> TaskState:
        """Install the initially compiled variables. ONE-SHOT: once
        variables exist, structural changes MUST go through
        ``recompose`` — this method cannot be (ab)used as a structural
        update channel."""
        with self._lock:
            if self._sessions.task_state().variables:
                raise ValidationError(
                    "init_task_state is one-shot; use recompose() for "
                    "structural updates")
            state = self._sessions.set_task_state(
                TaskState(intent=self._sessions.task_state().intent,
                          variables=tuple(variables)))
            self._emit(EventKind.STATE_UPDATED,
                       {"source": "initial_composition",
                        "keys": [v.semantic_key for v in state.variables]},
                       correlation_id)
            self._refresh_projection_data()
            return state

    def recompose(self, variables: Iterable[TaskVariable], *,
                  reason: str,
                  new_graph: WorkflowGraph | None = None,
                  new_schema: ProjectionSchema | None = None,
                  correlation_id: str = "") -> TaskState:
        """Structure-level recomposition: the legitimate entry for the
        State Compiler to replace/add/remove task variables + evidence
        after a GoalPatch or structure drift.

        Atomic (invariant 8): the graph is fully validated BEFORE any
        mutation. Bumps the epoch — existing surface handles and in-flight
        work predated the recomposition.
        """
        if not reason:
            raise ValidationError("recompose requires a reason")
        with self._lock:
            if not self._sessions.task_state().variables:
                raise ValidationError(
                    "recompose requires an initialised state; use "
                    "init_task_state first")
            new_variables = tuple(variables)
            if new_graph is not None:
                self._workflows.validate_replace_future(new_graph)
            # composition boundary: the architect's graph/schema must only
            # reference the variables being installed (invariant 13)
            self._validate_composition_locked(
                graph=new_graph, schema=new_schema,
                variable_keys={v.semantic_key for v in new_variables})
            # ── all validation passed; mutate ──
            old = self._sessions.task_state()
            epoch = self._bump_epoch_locked()
            state = self._sessions.set_task_state(
                TaskState(intent=old.intent, variables=new_variables))
            graph_revision = None
            invalidated: list[str] = []
            if new_graph is not None:
                installed, invalidated = self._workflows.replace_future(
                    new_graph, epoch=epoch)
                graph_revision = installed.revision
            if new_schema is not None:
                self._projections.set_schema(new_schema)
            old_keys = {v.semantic_key for v in old.variables}
            new_keys = {v.semantic_key for v in state.variables}
            self._emit(EventKind.STATE_UPDATED,
                       {"source": "recomposition", "reason": reason,
                        "added": sorted(new_keys - old_keys),
                        "removed": sorted(old_keys - new_keys),
                        "kept": sorted(old_keys & new_keys),
                        "graph_revision": graph_revision, "epoch": epoch,
                        "invalidated_node_ids": invalidated},
                       correlation_id)
            self._refresh_projection_data()
            return state

    def set_plan(self, graph: WorkflowGraph,
                 schema: ProjectionSchema | None = None,
                 *, correlation_id: str = "") -> WorkflowGraph:
        """Install the initial workflow plan (+ optionally the projection
        schema) produced by the Task Architect. Emits PlanCreated.
        Composition-validated (invariant 13): a plan/schema referencing
        unknown task variables is rejected before anything is installed."""
        with self._lock:
            self._validate_composition_locked(graph=graph, schema=schema)
            installed = self._workflows.install_graph(graph, epoch=self.epoch)
            if schema is not None:
                self._projections.set_schema(schema)
            self._emit(EventKind.PLAN_CREATED,
                       {"graph_revision": installed.revision,
                        "node_ids": [n.node_id for n in installed.nodes],
                        "schema_installed": schema is not None},
                       correlation_id)
            self._refresh_projection_data()
            return installed

    # ══ observation (bottom-up projection — mental-model §3.1) ══════════
    def apply_observation(self, observations: Iterable[ObservedValue],
                          *, correlation_id: str = "") -> TaskState:
        """Fold fresh observations into the task state — OBSERVED plane
        only (invariant 9); ``desired`` is never touched here.

        The observation contract is ``ObservedValue``: value and its
        visible evidence travel together and land on the SAME variable.
        Unknown semantic keys are rejected: discovering NEW variables is
        a structural change and belongs to ``recompose``."""
        with self._lock:
            updated, with_evidence = self._fold_observations_locked(
                observations)
            state = self._sessions.task_state()
            self._emit(EventKind.OBSERVATION_RECEIVED,
                       {"keys": updated, "keys_with_evidence": with_evidence},
                       correlation_id)
            self._refresh_projection_data()
            return state

    # ══ action lifecycle (ACTION nodes only; epoch-stamped — inv. 4) ════
    def request_action(self, node_id: str, *,
                       correlation_id: str = "") -> dict[str, Any]:
        """Register an action request for a READY ACTION node — the only
        kind that produces CUA work. Returns the handle the runtime must
        present back: {action_id, node_id, epoch, contract}."""
        with self._lock:
            node = self._workflows.node(node_id)
            if node is None or node.kind is not NodeKind.ACTION:
                raise ValidationError(
                    f"node {node_id!r} is not an ACTION node; control nodes "
                    "advance via record_verification / advance_control")
            st = self._workflows.snapshot().statuses.get(node_id)
            if st is not NodeStatus.READY:
                raise ValidationError(
                    f"action node {node_id!r} not READY (status={st})")
            # exactly-once: at most one ACTIVE handle per (node, epoch)
            for h in self._actions.values():
                if (h["node_id"] == node_id and h["epoch"] == self.epoch
                        and h["status"] in ("requested", "started")):
                    raise ValidationError(
                        f"node {node_id!r} already has active action "
                        f"{h['action_id']} in epoch {self.epoch}")
            self._action_seq += 1
            action_id = f"act_{self._action_seq:05d}"
            handle = {"action_id": action_id, "node_id": node_id,
                      "epoch": self.epoch, "status": "requested",
                      "contract": copy.deepcopy(node.contract)}
            self._actions[action_id] = handle
            self._emit(EventKind.ACTION_REQUESTED,
                       {"action_id": action_id, "node_id": node_id},
                       correlation_id or action_id)
            return dict(handle)

    def start_action(self, action_id: str) -> bool:
        """REQUESTED → STARTED. A stale-epoch start is DISCARDED (False);
        a terminal or already-started handle is a contract error."""
        with self._lock:
            handle = self._require_action(action_id)
            self._reject_terminal_handle(handle)
            if handle["epoch"] != self.epoch:
                handle["status"] = "discarded"
                self._emit(EventKind.ACTION_DISCARDED,
                           {"action_id": action_id,
                            "node_id": handle["node_id"], "phase": "start",
                            "action_epoch": handle["epoch"],
                            "current_epoch": self.epoch}, action_id)
                return False
            if handle["status"] != "requested":
                raise ValidationError(
                    f"action {action_id} already started")
            self._workflows.set_status(handle["node_id"], NodeStatus.RUNNING)
            handle["status"] = "started"
            self._emit(EventKind.ACTION_STARTED,
                       {"action_id": action_id, "node_id": handle["node_id"]},
                       action_id)
            self._refresh_projection_data()
            return True

    def finish_action(self, action_id: str, *,
                      observations: Iterable[ObservedValue] = ()) -> bool:
        """Land an action result — exactly once, only from STARTED, only
        in the action's own epoch (invariants 4 + 10).

        A stale-epoch result is DISCARDED (no state change, False). A
        result arriving for a node that is already committed/historical
        is DISCARDED too — committed history is never rewritten by a late
        landing. Observations fold into the OBSERVED plane as part of the
        landing (single ACTION_FINISHED event)."""
        with self._lock:
            handle = self._require_action(action_id)
            self._reject_terminal_handle(handle)
            if handle["epoch"] != self.epoch:
                handle["status"] = "discarded"
                self._reset_running_node(handle["node_id"])
                self._emit(EventKind.ACTION_DISCARDED,
                           {"action_id": action_id, "node_id": handle["node_id"],
                            "action_epoch": handle["epoch"],
                            "current_epoch": self.epoch}, action_id)
                self._refresh_projection_data()
                return False
            if handle["status"] != "started":
                raise ValidationError(
                    f"finish_action requires a STARTED handle "
                    f"(action {action_id} is {handle['status']!r})")
            node_status = self._workflows.snapshot().statuses.get(
                handle["node_id"])
            if node_status in (NodeStatus.COMMITTED, NodeStatus.COMPENSATED):
                handle["status"] = "discarded"
                self._emit(EventKind.ACTION_DISCARDED,
                           {"action_id": action_id,
                            "node_id": handle["node_id"],
                            "reason": "node_already_historical",
                            "node_status": node_status.value}, action_id)
                self._refresh_projection_data()
                return False
            updated, with_evidence = self._fold_observations_locked(
                observations)
            handle["status"] = "finished"
            self._emit(EventKind.ACTION_FINISHED,
                       {"action_id": action_id, "node_id": handle["node_id"],
                        "keys": updated, "keys_with_evidence": with_evidence},
                       action_id)
            self._refresh_projection_data()
            return True

    # ══ verification & control-node advancement ═════════════════════════
    def record_verification(self, node_id: str, passed: bool, *,
                            detail: str = "",
                            correlation_id: str = "") -> None:
        """Commit (or fail) a node based on independent evidence.

        ACTION nodes must be RUNNING (their result has landed);
        VERIFY nodes confirm directly from READY (they ARE the
        verification — no action handle exists for them)."""
        with self._lock:
            node = self._workflows.node(node_id)
            if node is None:
                raise ValidationError(f"unknown node {node_id!r}")
            st = self._workflows.snapshot().statuses.get(node_id)
            if node.kind is NodeKind.ACTION and st is not NodeStatus.RUNNING:
                raise ValidationError(
                    f"ACTION node {node_id!r} not RUNNING (status={st})")
            if node.kind is NodeKind.VERIFY and st is not NodeStatus.READY:
                raise ValidationError(
                    f"VERIFY node {node_id!r} not READY (status={st})")
            if node.kind not in (NodeKind.ACTION, NodeKind.VERIFY):
                raise ValidationError(
                    f"node {node_id!r} is {node.kind.value}; control nodes "
                    "advance via advance_control")
            self._workflows.set_status(
                node_id, NodeStatus.COMMITTED if passed else NodeStatus.FAILED)
            self._emit(EventKind.VERIFICATION_PASSED if passed
                       else EventKind.VERIFICATION_FAILED,
                       {"node_id": node_id, "kind": node.kind.value,
                        "detail": detail}, correlation_id)
            self._refresh_projection_data()

    def advance_control(self, node_id: str, *,
                        correlation_id: str = "") -> CheckpointRecord | None:
        """Advance a READY control node (BARRIER / CHECKPOINT / TERMINAL)
        to COMMITTED. A CHECKPOINT node additionally writes a
        CheckpointRecord keyed by its node id (the fan-in point IS the
        verified boundary). A TERMINAL commit means the plan is complete.
        Returns the CheckpointRecord for checkpoints, else None."""
        with self._lock:
            node = self._workflows.node(node_id)
            if node is None or node.kind not in _CONTROL_CONFIRM_KINDS:
                raise ValidationError(
                    f"node {node_id!r} is not a BARRIER/CHECKPOINT/TERMINAL "
                    "control node")
            st = self._workflows.snapshot().statuses.get(node_id)
            if st is not NodeStatus.READY:
                raise ValidationError(
                    f"control node {node_id!r} not READY (status={st})")
            rec = None
            if node.kind is NodeKind.CHECKPOINT:
                rec = self._commit_checkpoint_locked(node.node_id, node.label)
            else:
                self._workflows.set_status(node_id, NodeStatus.COMMITTED)
                self._emit(EventKind.NODE_COMMITTED,
                           {"node_id": node_id, "kind": node.kind.value},
                           correlation_id or node_id)
            self._refresh_projection_data()
            return rec

    def requeue(self, node_id: str, *, correlation_id: str = "") -> None:
        """Retry path: FAILED → READY. Emits ACTION_REQUEUED — a requeue
        is not a fresh action request (no handle exists yet)."""
        with self._lock:
            self._workflows.set_status(node_id, NodeStatus.READY)
            self._emit(EventKind.ACTION_REQUEUED,
                       {"node_id": node_id}, correlation_id or node_id)
            self._refresh_projection_data()

    # ══ bounded loop protocol (invariant 12) ═══════════════════════════
    def begin_loop_iteration(self, node_id: str, *,
                             correlation_id: str = "") -> int:
        """Start the next iteration of a READY bounded loop; returns the
        1-based iteration index. The loop goes RUNNING and its body
        children are (re)armed to READY — their per-iteration commits are
        ephemeral until the termination decision lands."""
        with self._lock:
            node = self._workflows.node(node_id)
            if node is None or node.kind is not NodeKind.BOUNDED_LOOP:
                raise ValidationError(
                    f"node {node_id!r} is not a BOUNDED_LOOP")
            st = self._workflows.snapshot().statuses.get(node_id)
            if st is not NodeStatus.READY:
                raise ValidationError(
                    f"loop {node_id!r} not READY (status={st})")
            iteration = self._loop_iters.get(node_id, 0) + 1
            if iteration > (node.max_iterations or 0):
                raise ValidationError(
                    f"loop {node_id!r} already reached "
                    f"max_iterations={node.max_iterations}")
            self._workflows.set_status(node_id, NodeStatus.RUNNING)
            self._loop_iters[node_id] = iteration
            self._workflows.reset_loop_children(node_id)
            self._emit(EventKind.LOOP_ITERATION_STARTED,
                       {"node_id": node_id, "iteration": iteration,
                        "max_iterations": node.max_iterations},
                       correlation_id or node_id)
            self._refresh_projection_data()
            return iteration

    def evaluate_loop_termination(self, node_id: str, terminated: bool, *,
                                  detail: str = "",
                                  correlation_id: str = "") -> dict[str, Any]:
        """Report the termination decision for the iteration whose body
        has FULLY committed. Only ``terminated=True`` commits the loop;
        False re-arms it (loop → READY for the next iteration) or, at the
        max-iteration guard, FAILS it with an explicit escalation payload."""
        with self._lock:
            node = self._workflows.node(node_id)
            if node is None or node.kind is not NodeKind.BOUNDED_LOOP:
                raise ValidationError(
                    f"node {node_id!r} is not a BOUNDED_LOOP")
            snap = self._workflows.snapshot()
            if snap.statuses.get(node_id) is not NodeStatus.RUNNING:
                raise ValidationError(
                    f"loop {node_id!r} not RUNNING "
                    f"(status={snap.statuses.get(node_id)})")
            incomplete = [
                c.node_id
                for c in (snap.graph.children_of(node_id)
                          if snap.graph is not None else ())
                if snap.statuses.get(c.node_id) is not NodeStatus.COMMITTED]
            if incomplete:
                raise ValidationError(
                    f"loop {node_id!r} body not fully committed this "
                    f"iteration: {incomplete}")
            iteration = self._loop_iters.get(node_id, 0)
            if terminated:
                self._workflows.set_status(node_id, NodeStatus.COMMITTED)
                outcome = {"outcome": "committed", "iteration": iteration}
            elif iteration >= (node.max_iterations or 0):
                self._workflows.set_status(node_id, NodeStatus.FAILED)
                outcome = {"outcome": "failed",
                           "reason": "max_iterations_exceeded",
                           "iteration": iteration}
            else:
                self._workflows.set_status(node_id, NodeStatus.READY)
                outcome = {"outcome": "continue",
                           "iteration": iteration,
                           "next_iteration": iteration + 1}
            self._emit(EventKind.LOOP_ITERATION_EVALUATED,
                       {"node_id": node_id, "terminated": terminated,
                        "detail": detail, **outcome},
                       correlation_id or node_id)
            self._refresh_projection_data()
            return outcome

    # ══ checkpoints (invariant 5) ════════════════════════════════════════
    def commit_checkpoint(self, checkpoint_id: str, label: str, *,
                          correlation_id: str = "") -> CheckpointRecord:
        """Governance-driven checkpoint (the user's 'mark this as a
        checkpoint' gesture). Workflow CHECKPOINT nodes use
        ``advance_control`` instead."""
        with self._lock:
            return self._commit_checkpoint_locked(
                checkpoint_id, label, correlation_id=correlation_id)

    def _commit_checkpoint_locked(self, checkpoint_id: str, label: str,
                                  *, correlation_id: str = ""
                                  ) -> CheckpointRecord:
        state = self._sessions.task_state()
        rec = CheckpointRecord(
            checkpoint_id=checkpoint_id,
            label=label,
            state_revision=state.revision,
            event_index=len(self._events),  # exclusive boundary
            epoch=self.epoch,
            intent=state.intent,
            structure={v.semantic_key: {"label": v.label,
                                        "value_type": v.value_type,
                                        "mutability": v.mutability}
                       for v in state.variables},
            observed=state.observed_values(),
            desired=state.desired_values(),
            committed_nodes=self._workflows.committed_node_ids(),
            created_at=time.time())
        rec = self._checkpoints.add(rec)
        # a workflow CHECKPOINT node being advanced commits itself
        wf_node = self._workflows.node(checkpoint_id)
        if (wf_node is not None and wf_node.kind is NodeKind.CHECKPOINT
                and self._workflows.snapshot().statuses.get(checkpoint_id)
                is NodeStatus.READY):
            self._workflows.set_status(checkpoint_id, NodeStatus.COMMITTED)
        self._emit(EventKind.CHECKPOINT_COMMITTED,
                   {"checkpoint_id": checkpoint_id, "label": label,
                    "state_revision": rec.state_revision,
                    "event_index": rec.event_index},
                   correlation_id or checkpoint_id)
        return rec

    # ══ governance patches (atomic — invariant 8) ═══════════════════════
    def apply_local_patch(self, patch: LocalPatch) -> dict[str, Any]:
        """Local adjustment: DESIRED values + not-yet-committed node
        contracts only. Topology and terminal intent are structurally out
        of reach here. Atomic: every validation runs before any mutation;
        a rejected patch changes nothing. Bumps the epoch on success:
        in-flight work predates the adjustment."""
        if not isinstance(patch, LocalPatch):
            raise PatchSemanticsError(
                "apply_local_patch accepts LocalPatch only")
        with self._lock:
            # ── validate everything first ──
            state = self._sessions.task_state()
            unknown = [u.semantic_key for u in patch.variable_updates
                       if state.variable(u.semantic_key) is None]
            if unknown:
                raise PatchSemanticsError(
                    f"LocalPatch introduces unknown variables {unknown}; "
                    "adding variables is a scope change — use GoalPatch")
            frozen_vars = [
                u.semantic_key for u in patch.variable_updates
                if (v := state.variable(u.semantic_key)) is not None
                and v.mutability != MUTABILITY_EDITABLE]
            if frozen_vars:
                raise PatchSemanticsError(
                    f"LocalPatch cannot retarget {frozen_vars}: mutability "
                    "is readonly/locked — structural or locked variables "
                    "change only via recompose / GoalPatch")
            wf = self._workflows.snapshot()
            for ov in patch.node_overrides:
                node = self._workflows.node(ov.node_id)
                if node is None:
                    raise ValidationError(
                        f"LocalPatch override targets unknown node "
                        f"{ov.node_id!r}")
                if node.kind is not NodeKind.ACTION:
                    raise PatchSemanticsError(
                        f"LocalPatch override targets {node.kind.value} node "
                        f"{ov.node_id!r}; only ACTION contracts can be "
                        "overridden")
                st = wf.statuses.get(ov.node_id)
                if st in (NodeStatus.COMMITTED, NodeStatus.COMPENSATED):
                    raise CommittedNodeViolationError(
                        f"cannot override contract of committed node "
                        f"{ov.node_id!r}")
            # ── all validation passed; mutate ──
            if patch.variable_updates:
                new_vars = tuple(
                    v.with_desired(u.new_value)
                    if (u := self._find_update(patch, v.semantic_key)) else v
                    for v in state.variables)
                state = self._sessions.set_task_state(
                    replace(state, variables=new_vars))
            for ov in patch.node_overrides:
                self._workflows.override_contract(ov.node_id, ov.contract)
            epoch = self._bump_epoch_locked()
            self._emit(EventKind.PLAN_PATCHED,
                       {"patch_id": patch.patch_id, "patch_class": "local",
                        "updated_variables": [u.semantic_key
                                              for u in patch.variable_updates],
                        "overridden_nodes": [o.node_id
                                             for o in patch.node_overrides],
                        "requires_replan": False, "epoch": epoch,
                        "rationale": patch.rationale},
                       patch.correlation_id or patch.patch_id)
            self._refresh_projection_data()
            return {"epoch": epoch, "requires_replan": False}

    def apply_goal_patch(self, patch: GoalPatch,
                         new_graph: WorkflowGraph | None = None,
                         new_schema: ProjectionSchema | None = None,
                         ) -> dict[str, Any]:
        """Terminal change: new intent and/or re-organised future subgraph.

        Atomic: the replacement graph is fully validated BEFORE the epoch
        bump / intent change — a rejected GoalPatch leaves intent, epoch,
        graph, and event log untouched (invariant 8).

        On success ALWAYS bumps the epoch and reports requires_replan=True
        — with no ``new_graph`` the Task Architect MUST re-plan the
        uncommitted future before execution continues; with one, committed
        nodes are carried verbatim (invariant 3) and only the future is
        replaced.
        """
        if not isinstance(patch, GoalPatch):
            raise PatchSemanticsError("apply_goal_patch accepts GoalPatch only")
        with self._lock:
            # ── validate everything first ──
            if new_graph is not None:
                self._workflows.validate_replace_future(new_graph)
            self._validate_composition_locked(graph=new_graph,
                                              schema=new_schema)
            # ── all validation passed; mutate ──
            epoch = self._bump_epoch_locked()
            intent_changed = False
            if patch.new_intent is not None:
                old = self._sessions.task_state().intent
                intent_changed = not patch.new_intent.describes_same_terminal(old)
                self._sessions.set_intent(patch.new_intent)
            graph_revision = None
            invalidated: list[str] = []
            if new_graph is not None:
                installed, invalidated = self._workflows.replace_future(
                    new_graph, epoch=epoch)
                graph_revision = installed.revision
            if new_schema is not None:
                self._projections.set_schema(new_schema)
            self._emit(EventKind.PLAN_PATCHED,
                       {"patch_id": patch.patch_id, "patch_class": "goal",
                        "intent_changed": intent_changed,
                        "graph_revision": graph_revision,
                        "invalidated_node_ids": invalidated,
                        "requires_replan": True, "epoch": epoch,
                        "rationale": patch.rationale},
                       patch.correlation_id or patch.patch_id)
            self._refresh_projection_data()
            return {"epoch": epoch, "requires_replan": True,
                    "intent_changed": intent_changed,
                    "graph_revision": graph_revision}

    def request_compensation(self, patch: CompensationPatch) -> CompensationPlan:
        """Build the reversion plan for a CompensationPatch from the
        kernel's OWN checkpoint record (invariant 6) — the patch carries
        only the target checkpoint id, so there is no caller-supplied
        history to spoof. The runtime must execute the reversions through
        the SAME real action path as forward work."""
        if not isinstance(patch, CompensationPatch):
            raise PatchSemanticsError(
                "request_compensation accepts CompensationPatch only")
        with self._lock:
            rec = self._checkpoints.get(patch.target_checkpoint_id)
            state = self._sessions.task_state()
            current_observed = state.observed_values()
            entries = tuple(
                CompensationEntry(semantic_key=k,
                                  from_observed=current_observed.get(k),
                                  to_observed=target,
                                  to_desired=rec.desired.get(k))
                for k, target in rec.observed.items()
                if current_observed.get(k) != target)
            # cross-GoalPatch / cross-structure rollback signal: if the
            # checkpoint's intent or semantic structure differs from the
            # current one, the remaining future topology was planned for
            # an abandoned goal — the architect MUST recompose (never
            # silently keep the wrong future).
            intent_differs = (rec.intent is not None
                              and not rec.intent.describes_same_terminal(
                                  state.intent))
            checkpoint_keys = set(rec.structure) or set(rec.observed)
            structure_differs = checkpoint_keys != set(current_observed)
            requires_recompose = bool(intent_differs or structure_differs)
            epoch = self._bump_epoch_locked()
            self._comp_seq += 1
            plan = CompensationPlan(
                plan_id=f"comp_{self._comp_seq:05d}",
                target_checkpoint_id=rec.checkpoint_id,
                entries=entries, epoch=epoch, created_at=time.time(),
                requires_recompose=requires_recompose)
            self._comp_plans[plan.plan_id] = plan
            self._comp_status[plan.plan_id] = "pending"
            self._emit(EventKind.COMPENSATION_REQUESTED,
                       {"patch_id": patch.patch_id, "plan_id": plan.plan_id,
                        "target_checkpoint_id": rec.checkpoint_id,
                        "entries": [{"semantic_key": e.semantic_key,
                                     "from": e.from_observed,
                                     "to": e.to_observed}
                                    for e in entries],
                        "requires_recompose": requires_recompose,
                        "epoch": epoch},
                       patch.correlation_id or patch.patch_id)
            return plan

    def record_compensation_result(
            self, plan_id: str, applied: bool, *,
            observed_values: dict[str, Any] | None = None,
            detail: str = "") -> bool:
        """Land the outcome of a compensation execution — exactly once,
        only inside the plan's own epoch (invariant 11).

        A STALE plan (an epoch boundary was crossed after the plan was
        issued) is DISCARDED: COMPENSATION_DISCARDED is emitted, nothing
        changes, False returns — this is never confused with an honest
        execution failure. A terminal plan (applied/failed/discarded)
        can never land again.

        ``applied=True`` is NEVER taken on faith (invariant 6): the
        caller must supply freshly observed values, and EVERY plan
        entry's target must match — a single mismatch (or a missing key)
        turns the outcome into CompensationFailed. Only on full match
        does the kernel restore the checkpoint's logical state — intent,
        semantic structure, and both value planes — and mark
        post-checkpoint commits COMPENSATED. If the rollback crossed a
        GoalPatch boundary (plan.requires_recompose), the remaining
        future topology is INVALIDATED: it was planned for the abandoned
        goal and must not silently keep running.
        """
        with self._lock:
            plan = self._comp_plans.get(plan_id)
            if plan is None:
                raise ValidationError(f"unknown compensation plan {plan_id!r}")
            status = self._comp_status.get(plan_id, "pending")
            if status != "pending":
                raise ValidationError(
                    f"compensation plan {plan_id} is terminal ({status}); "
                    "a plan lands exactly once")
            if plan.epoch != self.epoch:
                self._comp_status[plan_id] = "discarded"
                self._emit(EventKind.COMPENSATION_DISCARDED,
                           {"plan_id": plan_id, "plan_epoch": plan.epoch,
                            "current_epoch": self.epoch}, plan_id)
                self._refresh_projection_data()
                return False
            if applied and observed_values is None:
                raise ValidationError(
                    "applied compensation requires freshly observed values")
            mismatches: dict[str, dict[str, Any]] = {}
            if applied and observed_values is not None:
                for e in plan.entries:
                    got = observed_values.get(e.semantic_key, _MISSING)
                    if got is _MISSING or got != e.to_observed:
                        mismatches[e.semantic_key] = {
                            "expected": e.to_observed,
                            "observed": None if got is _MISSING else got}
            if not applied or mismatches:
                self._comp_status[plan_id] = "failed"
                self._emit(EventKind.COMPENSATION_FAILED,
                           {"plan_id": plan_id, "detail": detail,
                            "mismatches": mismatches,
                            "caller_claimed_applied": applied}, plan_id)
                self._refresh_projection_data()
                return False
            # ── full match confirmed: restore the logical checkpoint ──
            rec = self._checkpoints.get(plan.target_checkpoint_id)
            by_key = {e.semantic_key: e for e in plan.entries}
            state = self._sessions.task_state()
            current = {v.semantic_key: v for v in state.variables}
            structure = rec.structure or {k: {} for k in rec.observed}
            restored_vars: list[TaskVariable] = []
            for key, meta in structure.items():
                observed_v = (by_key[key].to_observed if key in by_key
                              else rec.observed.get(key))
                desired_v = rec.desired.get(key)
                base = current.get(key)
                if base is not None:
                    restored_vars.append(replace(base, observed=observed_v,
                                                 desired=desired_v))
                else:
                    # the variable existed at the checkpoint but was
                    # structurally removed since — restore it whole
                    restored_vars.append(TaskVariable(
                        semantic_key=key,
                        label=meta.get("label") or key,
                        value_type=meta.get("value_type") or "string",
                        mutability=meta.get("mutability")
                        or MUTABILITY_EDITABLE,
                        observed=observed_v, desired=desired_v))
            dropped = sorted(set(current) - set(structure))
            intent_restored = (rec.intent is not None
                               and not rec.intent.describes_same_terminal(
                                   state.intent))
            self._sessions.set_task_state(TaskState(
                intent=rec.intent or state.intent,
                variables=tuple(restored_vars)))
            kept = set(rec.committed_nodes)
            compensated_nodes = []
            for nid in self._workflows.committed_node_ids():
                if nid not in kept:
                    self._workflows.set_status(nid, NodeStatus.COMPENSATED)
                    compensated_nodes.append(nid)
            invalidated: list[str] = []
            if plan.requires_recompose:
                invalidated = self._workflows.invalidate_future()
            self._comp_status[plan_id] = "applied"
            self._emit(EventKind.COMPENSATION_APPLIED,
                       {"plan_id": plan_id, "detail": detail,
                        "restored": {k: e.to_observed
                                     for k, e in by_key.items()},
                        "compensated_nodes": compensated_nodes,
                        "intent_restored": intent_restored,
                        "restored_structure_keys": sorted(
                            set(structure) - set(current)),
                        "dropped_semantic_keys": dropped,
                        "requires_recompose": plan.requires_recompose,
                        "invalidated_node_ids": invalidated}, plan_id)
            self._refresh_projection_data()
            return True

    # ══ governance events & conflicts ════════════════════════════════════
    def request_governance(self, action: str, detail: str = "", *,
                           correlation_id: str = "") -> None:
        """pause / resume / mode change — recorded, epoch-safe."""
        with self._lock:
            if action == "pause":
                self._bump_epoch_locked()
            self._emit(EventKind.GOVERNANCE_REQUESTED,
                       {"action": action, "detail": detail}, correlation_id)

    def record_conflict(self, description: str,
                        semantic_keys: Iterable[str] = (), *,
                        correlation_id: str = "") -> str:
        with self._lock:
            cid = correlation_id or f"conflict_{len(self._events) + 1:05d}"
            self._emit(EventKind.CONFLICT_DETECTED,
                       {"description": description,
                        "semantic_keys": list(semantic_keys)}, cid)
            return cid

    def resolve_conflict(self, resolution: str, *,
                         correlation_id: str = "") -> None:
        with self._lock:
            self._emit(EventKind.CONFLICT_RESOLVED,
                       {"resolution": resolution}, correlation_id)

    # ══ internals ════════════════════════════════════════════════════════
    def _require_action(self, action_id: str) -> dict[str, Any]:
        handle = self._actions.get(action_id)
        if handle is None:
            raise ValidationError(f"unknown action {action_id!r}")
        return handle

    @staticmethod
    def _reject_terminal_handle(handle: dict[str, Any]) -> None:
        """A FINISHED/DISCARDED handle can never land again (invariant 10)."""
        if handle["status"] in ("finished", "discarded"):
            raise ValidationError(
                f"action handle {handle['action_id']} is terminal "
                f"({handle['status']}); an action result lands exactly once")

    def _fold_observations_locked(
            self, observations: Iterable[ObservedValue]
    ) -> tuple[list[str], list[str]]:
        """Validate (all keys known) then fold observations into the
        OBSERVED plane with their evidence. Returns (updated keys,
        keys that carried evidence). No event is emitted here — the
        calling public method owns the single event."""
        obs = tuple(observations)
        state = self._sessions.task_state()
        unknown = [o.semantic_key for o in obs
                   if state.variable(o.semantic_key) is None]
        if unknown:
            raise ValidationError(
                f"observation carries unknown semantic keys {unknown}; "
                "structural discovery must go through recompose()")
        if not obs:
            return [], []
        by_key = {o.semantic_key: o for o in obs}
        new_vars = tuple(
            v.with_observed(by_key[v.semantic_key].value,
                            evidence=by_key[v.semantic_key].evidence,
                            confidence=by_key[v.semantic_key].confidence)
            if v.semantic_key in by_key else v
            for v in state.variables)
        self._sessions.set_task_state(replace(state, variables=new_vars))
        return (sorted(by_key),
                sorted(k for k, o in by_key.items() if o.evidence))

    def _validate_composition_locked(
            self, graph: WorkflowGraph | None = None,
            schema: ProjectionSchema | None = None, *,
            variable_keys: set[str] | None = None) -> None:
        """Composition boundary check (invariant 13): architect output may
        only reference task variables that exist (or are being installed).
        Rejects illegal output BEFORE it enters the kernel."""
        keys = (variable_keys if variable_keys is not None else
                {v.semantic_key
                 for v in self._sessions.task_state().variables})
        if schema is not None:
            missing = sorted({c.binding_key for c in schema.components
                              if c.binding_key is not None} - keys)
            if missing:
                raise ValidationError(
                    f"ProjectionSchema binds unknown task variables "
                    f"{missing}; architect output rejected at the kernel "
                    "boundary")
        if graph is not None:
            bad: dict[str, list[str]] = {}
            for n in graph.nodes:
                if n.contract is not None:
                    miss = sorted(set(n.contract.desired_state) - keys)
                    if miss:
                        bad[n.node_id] = miss
            if bad:
                raise ValidationError(
                    "ActionContract desired_state references unknown task "
                    f"variables: {bad}; architect output rejected at the "
                    "kernel boundary")

    def _bump_epoch_locked(self) -> int:
        epoch = self._sessions.bump_epoch()
        self._workflows.mark_running_stale_reset()
        return epoch

    def _reset_running_node(self, node_id: str) -> None:
        snap = self._workflows.snapshot()
        if snap.statuses.get(node_id) is NodeStatus.RUNNING:
            self._workflows.set_status(node_id, NodeStatus.READY)

    @staticmethod
    def _find_update(patch: LocalPatch, key: str):
        for u in patch.variable_updates:
            if u.semantic_key == key:
                return u
        return None

    def _refresh_projection_data(self) -> None:
        """Projection data always mirrors the stores (single truth) via an
        AUTHORITATIVE replace — nodes/variables removed from the plan or
        the state disappear here too (no stale keys)."""
        state = self._sessions.task_state()
        wf = self._workflows.snapshot()
        values = {v.semantic_key: {"observed": v.observed,
                                   "desired": v.desired,
                                   "diverged": v.diverged}
                  for v in state.variables}
        node_status = {nid: st.value for nid, st in wf.statuses.items()}
        countable = [] if wf.graph is None else [
            n for n in wf.graph.nodes if n.kind in _PROGRESS_KINDS]
        done = [n for n in countable
                if wf.statuses.get(n.node_id) is NodeStatus.COMMITTED]
        progress = (len(done) / len(countable)) if countable else 0.0
        self._projections.replace_data(values=values,
                                       node_status=node_status,
                                       progress=progress)


class _Missing:
    """Sentinel distinguishing 'key absent' from 'value is None'."""

    def __repr__(self) -> str:  # pragma: no cover
        return "<MISSING>"


_MISSING = _Missing()
