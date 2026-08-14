"""TaskVMKernel — the TaskVM control kernel (L3).

Owns exactly STATE / TIME / HISTORY / TRANSITION
(docs/contracts/layered_ownership_protocol.md; public contract:
docs/contracts/kernel.md). CONTENT validity is proven ONCE by the domain
constructors / producers (WorkflowGraph, ProjectionSchema,
TaskArchitecture, ObservationBatch, typed VerificationResult /
CompensationResult) — the kernel receives validated typed objects and
decides whether they may land on the CURRENT timeline; it never
re-proves content, and it is not a hostile-caller firewall.

Fixed semantics (locked by tests/kernel): exactly one event per accepted
mutation; ACTION rides request → start → finish → land_verification of
the current epoch's FINISHED attempt; GoalPatch is a closed two-phase
transition; a pending compensation plan blocks all forward autonomy;
compensation derives only from the kernel's own committed action
history; checkpoints pin stable boundaries; COMPLETE rollback truncates
active future checkpoints, PARTIAL marks undone work COMPENSATED and
waits for governance.
"""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import replace
from typing import Any, Iterable

from taskvm.domain.architecture import TaskArchitecture
from taskvm.domain.contract import Reversibility
from taskvm.domain.errors import PatchSemanticsError, ValidationError
from taskvm.domain.events import Event, EventKind
from taskvm.domain.intent import TaskIntent
from taskvm.domain.patch import (
    CompensationEntry, CompensationPatch, CompensationPlan, GoalPatch,
    LocalPatch, UncompensatableAction,
)
from taskvm.domain.projection import ProjectionSchema
from taskvm.domain.results import CompensationResult, VerificationResult
from taskvm.domain.state import (
    MUTABILITY_EDITABLE, ObservationBatch, ObservedValue, TaskState,
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
        self._comp_status: dict[str, str] = {}  # pending/complete/partial/failed/discarded
        self._loop_iters: dict[str, int] = {}   # loop node → current iteration
        # GoalPatch/rollback closure: while set, execution is blocked until
        # recompose() atomically installs the new composition
        self._pending_recompose: str | None = None
        # committed action history — the ONLY source of compensation;
        # appended at verification time (before/after recorded at action time)
        self._action_history: list[dict[str, Any]] = []

    # ── introspection (immutable snapshots) ─────────────────────────────
    @property
    def session_id(self) -> str:
        return self._sessions.session_id

    @property
    def epoch(self) -> int:
        return self._sessions.epoch

    @property
    def pending_recompose(self) -> str | None:
        """Non-None while execution is blocked awaiting recompose()."""
        return self._pending_recompose

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

    # ── event plumbing + the forward-autonomy gate ──────────────────────
    def _emit(self, kind: EventKind, payload: dict[str, Any],
              correlation_id: str = "") -> Event:
        ev = Event(event_id=f"evt:{len(self._events) + 1:05d}",
                   session_id=self.session_id, kind=kind,
                   revision=self._sessions.task_state().revision,
                   epoch=self._sessions.epoch, timestamp=time.time(),
                   correlation_id=correlation_id, payload=payload)
        self._events.append(ev)
        return ev

    def _require_executable_locked(self) -> None:
        """Forward autonomy is blocked (a) after a GoalPatch / rollback
        until recompose() closes the composition, and (b) while a
        compensation plan of the CURRENT epoch is pending."""
        if self._pending_recompose is not None:
            raise ValidationError(
                "execution blocked: recompose() required before execution "
                f"continues ({self._pending_recompose})")
        for pid, status in self._comp_status.items():
            if status == "pending" and self._comp_plans[pid].epoch == self.epoch:
                raise ValidationError(
                    f"execution blocked: compensation plan {pid} is pending; "
                    "forward autonomy resumes once it lands "
                    "(complete/partial/failed) or is superseded by governance")

    # ── composition (State Compiler / Task Architect output) ────────────
    def init_task_state(self, variables: Iterable[TaskVariable],
                        *, correlation_id: str = "") -> TaskState:
        """Install the initial variables. ONE-SHOT via an explicit
        initialized flag — an EMPTY initial composition is legal."""
        with self._lock:
            if self._sessions.initialized:
                raise ValidationError(
                    "init_task_state is one-shot; use recompose()")
            state = self._sessions.set_task_state(TaskState(
                intent=self._sessions.task_state().intent,
                variables=tuple(variables)))
            self._sessions.mark_initialized()
            self._emit(EventKind.STATE_UPDATED,
                       {"source": "initial_composition",
                        "keys": [v.semantic_key for v in state.variables]},
                       correlation_id)
            self._refresh_projection_data()
            return state

    def recompose(self, variables: Iterable[TaskVariable], *, reason: str,
                  new_graph: WorkflowGraph | None = None,
                  new_schema: ProjectionSchema | None = None,
                  correlation_id: str = "") -> TaskState:
        """The ONLY re-closure entry after a GoalPatch (and the legitimate
        entry for structure drift): ONE atomic install of the complete
        new composition; after a GoalPatch ``new_graph`` is MANDATORY.
        Static coherence is proven by the TaskArchitecture constructor
        (the EFFECTIVE composition, supplied or retained); history-carry
        by the WorkflowStore. Full validation before any mutation."""
        if not reason:
            raise ValidationError("recompose requires a reason")
        with self._lock:
            if not self._sessions.initialized:
                raise ValidationError(
                    "recompose requires an initialised state")
            if self._pending_recompose is not None and new_graph is None:
                raise ValidationError(
                    "recompose after a GoalPatch requires new_graph: the "
                    "old future was invalidated and cannot be retained")
            new_variables = tuple(variables)
            current_graph = self._workflows.snapshot().graph
            if new_graph is not None and current_graph is not None:
                self._workflows.validate_replace_future(new_graph)
            TaskArchitecture(
                variables=new_variables,
                graph=new_graph if new_graph is not None else current_graph,
                schema=(new_schema if new_schema is not None
                        else self._projections.snapshot().schema),
                exempt_node_ids=self._workflows.historical_node_ids())
            # ── all validation passed; mutate ──
            old = self._sessions.task_state()
            epoch = self._bump_epoch_locked()
            state = self._sessions.set_task_state(
                TaskState(intent=old.intent, variables=new_variables))
            graph_revision, invalidated = None, []
            if new_graph is not None:
                if current_graph is None:
                    installed = self._workflows.install_graph(new_graph, epoch=epoch)
                else:
                    installed, invalidated = self._workflows.replace_future(
                        new_graph, epoch=epoch)
                graph_revision = installed.revision
            if new_schema is not None:
                self._projections.set_schema(new_schema)
            closure = self._pending_recompose
            self._pending_recompose = None
            old_keys = {v.semantic_key for v in old.variables}
            new_keys = {v.semantic_key for v in state.variables}
            self._emit(EventKind.STATE_UPDATED,
                       {"source": "recomposition", "reason": reason,
                        "added": sorted(new_keys - old_keys),
                        "removed": sorted(old_keys - new_keys),
                        "graph_revision": graph_revision, "epoch": epoch,
                        "invalidated_node_ids": invalidated,
                        "closed_pending_recompose": closure}, correlation_id)
            self._refresh_projection_data()
            return state

    def set_plan(self, graph: WorkflowGraph,
                 schema: ProjectionSchema | None = None,
                 *, correlation_id: str = "") -> WorkflowGraph:
        """Install the initial plan (+ optional schema). ONE-SHOT: future
        replacement goes through apply_goal_patch → recompose ONLY."""
        with self._lock:
            if self._workflows.snapshot().graph is not None:
                raise ValidationError(
                    "set_plan is one-shot; use apply_goal_patch → recompose")
            TaskArchitecture(
                variables=self._sessions.task_state().variables,
                graph=graph, schema=schema)
            installed = self._workflows.install_graph(graph, epoch=self.epoch)
            if schema is not None:
                self._projections.set_schema(schema)
            self._emit(EventKind.PLAN_CREATED,
                       {"graph_revision": installed.revision,
                        "node_ids": [n.node_id for n in installed.nodes],
                        "schema_installed": schema is not None}, correlation_id)
            self._refresh_projection_data()
            return installed

    # ── observation (bottom-up; OBSERVED plane only) ────────────────────
    def apply_observation(self, observations: Iterable[ObservedValue],
                          *, correlation_id: str = "") -> TaskState:
        """Fold a batch into the OBSERVED plane. Duplicate keys in one
        batch are rejected by the ObservationBatch constructor (content);
        unknown keys against the CURRENT state (structural discovery
        belongs to recompose)."""
        with self._lock:
            batch = ObservationBatch(tuple(observations))
            updated, with_evidence = self._fold_observations_locked(batch.observations)
            state = self._sessions.task_state()
            self._emit(EventKind.OBSERVATION_RECEIVED,
                       {"keys": updated, "keys_with_evidence": with_evidence},
                       correlation_id)
            self._refresh_projection_data()
            return state

    # ── action lifecycle (ACTION nodes only; epoch-stamped) ─────────────
    def request_action(self, node_id: str, *,
                       correlation_id: str = "") -> dict[str, Any]:
        """Register a request for a READY ACTION node; at most one ACTIVE
        handle per (node, epoch). The contract is an immutable domain
        object — no defensive copy needed at this boundary."""
        with self._lock:
            self._require_executable_locked()
            node = self._workflows.node(node_id)
            if node is None or node.kind is not NodeKind.ACTION:
                raise ValidationError(
                    f"node {node_id!r} is not an ACTION node")
            st = self._workflows.snapshot().statuses.get(node_id)
            if st is not NodeStatus.READY:
                raise ValidationError(
                    f"action node {node_id!r} not READY (status={st})")
            for h in self._actions.values():
                if (h["node_id"] == node_id and h["epoch"] == self.epoch
                        and h["status"] in ("requested", "started")):
                    raise ValidationError(
                        f"node {node_id!r} already has active action "
                        f"{h['action_id']} in epoch {self.epoch}")
            self._action_seq += 1
            action_id = f"action:{self._action_seq:05d}"
            handle = {"action_id": action_id, "node_id": node_id,
                      "epoch": self.epoch, "status": "requested",
                      "contract": node.contract}
            self._actions[action_id] = handle
            self._emit(EventKind.ACTION_REQUESTED,
                       {"action_id": action_id, "node_id": node_id},
                       correlation_id or action_id)
            return copy.deepcopy(handle)

    def start_action(self, action_id: str) -> bool:
        """REQUESTED → STARTED (stale epoch ⇒ DISCARDED). Records the
        BEFORE observation — the compensation-history raw material."""
        with self._lock:
            handle = self._require_action(action_id)
            self._reject_terminal_handle(handle)
            if handle["epoch"] != self.epoch:
                self._discard_action_locked(
                    handle, {"phase": "start", "action_epoch": handle["epoch"],
                             "current_epoch": self.epoch})
                return False
            if handle["status"] != "requested":
                raise ValidationError(f"action {action_id} already started")
            state = self._sessions.task_state()
            handle["before_observed"] = {
                key: (v.observed if (v := state.variable(key)) is not None else None)
                for key in handle["contract"].desired_state}
            self._workflows.set_status(handle["node_id"], NodeStatus.RUNNING)
            handle["status"] = "started"
            self._emit(EventKind.ACTION_STARTED,
                       {"action_id": action_id, "node_id": handle["node_id"]},
                       action_id)
            self._refresh_projection_data()
            return True

    def finish_action(self, action_id: str, *,
                      observations: Iterable[ObservedValue] = ()) -> bool:
        """Land a result — exactly once, only from STARTED, only in its own
        epoch; a result for an already-historical node is DISCARDED.
        Observations fold into the OBSERVED plane as part of the landing;
        the AFTER observation feeds the compensation history."""
        with self._lock:
            handle = self._require_action(action_id)
            self._reject_terminal_handle(handle)
            if handle["epoch"] != self.epoch:
                self._reset_running_node(handle["node_id"])
                self._discard_action_locked(
                    handle, {"action_epoch": handle["epoch"],
                             "current_epoch": self.epoch})
                return False
            if handle["status"] != "started":
                raise ValidationError(
                    f"finish_action requires a STARTED handle "
                    f"(action {action_id} is {handle['status']!r})")
            node_status = self._workflows.snapshot().statuses.get(handle["node_id"])
            if node_status in (NodeStatus.COMMITTED, NodeStatus.COMPENSATED):
                self._discard_action_locked(
                    handle, {"reason": "node_already_historical",
                             "node_status": node_status.value})
                return False
            batch = ObservationBatch(tuple(observations))
            updated, with_evidence = self._fold_observations_locked(batch.observations)
            state = self._sessions.task_state()
            handle["after_observed"] = {
                key: (v.observed if (v := state.variable(key)) is not None else None)
                for key in handle["contract"].desired_state}
            handle["status"] = "finished"
            self._emit(EventKind.ACTION_FINISHED,
                       {"action_id": action_id, "node_id": handle["node_id"],
                        "keys": updated, "keys_with_evidence": with_evidence},
                       action_id)
            self._refresh_projection_data()
            return True

    # ── verification & control-node advancement ─────────────────────────
    def land_verification(self, result: VerificationResult) -> None:
        """Land the verifier's typed verdict. TIME checks only (node
        status / current epoch / current FINISHED attempt identity);
        content is the verifier's (E), never re-judged here. VERIFY
        confirms from READY and is the only kind allowed READY → FAILED.
        A committed ACTION enters the compensation history."""
        if not isinstance(result, VerificationResult):
            raise ValidationError("land_verification accepts VerificationResult")
        with self._lock:
            self._require_executable_locked()
            node = self._workflows.node(result.node_id)
            if node is None:
                raise ValidationError(f"unknown node {result.node_id!r}")
            if result.epoch != self.epoch:
                raise ValidationError(
                    f"verification for {result.node_id!r} arrived from "
                    f"stale epoch {result.epoch} (current {self.epoch})")
            st = self._workflows.snapshot().statuses.get(result.node_id)
            handle = None
            if node.kind is NodeKind.ACTION:
                if result.action_id is None:
                    raise ValidationError(
                        "ACTION verification must name the finished "
                        "attempt (action_id)")
                handle = self._actions.get(result.action_id)
                if (handle is None or handle["node_id"] != result.node_id
                        or handle["epoch"] != self.epoch
                        or handle["status"] != "finished"):
                    raise ValidationError(
                        f"ACTION node {result.node_id!r} has no FINISHED "
                        f"attempt {result.action_id!r} in epoch "
                        f"{self.epoch}; the protocol is request → start "
                        "→ finish → verify")
                if st is not NodeStatus.RUNNING:
                    raise ValidationError(
                        f"ACTION node {result.node_id!r} not RUNNING (status={st})")
            elif node.kind is NodeKind.VERIFY:
                if result.action_id is not None:
                    raise ValidationError(
                        "VERIFY nodes have no action attempt")
                if st is not NodeStatus.READY:
                    raise ValidationError(
                        f"VERIFY node {result.node_id!r} not READY (status={st})")
            else:
                raise ValidationError(
                    f"node {result.node_id!r} is {node.kind.value}; control "
                    "nodes advance via advance_control")
            self._workflows.set_status(
                result.node_id,
                NodeStatus.COMMITTED if result.passed else NodeStatus.FAILED)
            self._emit(EventKind.VERIFICATION_PASSED if result.passed
                       else EventKind.VERIFICATION_FAILED,
                       {"node_id": result.node_id, "kind": node.kind.value,
                        "detail": result.detail,
                        "evidence_ref": result.evidence_ref},
                       result.action_id or result.node_id)
            if result.passed and handle is not None:
                self._action_history.append({
                    "node_id": result.node_id, "epoch": handle["epoch"],
                    "event_index": len(self._events),
                    "before": dict(handle.get("before_observed") or {}),
                    "after": dict(handle.get("after_observed") or {}),
                    "reversibility": handle["contract"].reversibility})
            self._refresh_projection_data()

    def advance_control(self, node_id: str, *,
                        correlation_id: str = "") -> CheckpointRecord | None:
        """Advance a READY control node (BARRIER/CHECKPOINT/TERMINAL) to
        COMMITTED. A CHECKPOINT node is logically committed FIRST, so it
        belongs to its own boundary record."""
        with self._lock:
            self._require_executable_locked()
            node = self._workflows.node(node_id)
            if node is None or node.kind not in _CONTROL_CONFIRM_KINDS:
                raise ValidationError(
                    f"node {node_id!r} is not a BARRIER/CHECKPOINT/TERMINAL")
            st = self._workflows.snapshot().statuses.get(node_id)
            if st is not NodeStatus.READY:
                raise ValidationError(
                    f"control node {node_id!r} not READY (status={st})")
            rec = None
            if node.kind is NodeKind.CHECKPOINT:
                self._workflows.set_status(node_id, NodeStatus.COMMITTED)
                rec = self._commit_checkpoint_locked(
                    f"ckpt:{node.node_id}", node.label)
            else:
                self._workflows.set_status(node_id, NodeStatus.COMMITTED)
                self._emit(EventKind.NODE_COMMITTED,
                           {"node_id": node_id, "kind": node.kind.value},
                           correlation_id or node_id)
            self._refresh_projection_data()
            return rec

    def requeue(self, node_id: str, *, correlation_id: str = "") -> None:
        """FAILED → READY, gated to ACTION/VERIFY — a maxed-out loop needs
        governance / a new plan, never a silent retry."""
        with self._lock:
            self._require_executable_locked()
            node = self._workflows.node(node_id)
            if node is None:
                raise ValidationError(f"unknown node {node_id!r}")
            if node.kind not in (NodeKind.ACTION, NodeKind.VERIFY):
                raise ValidationError(
                    f"requeue is only defined for ACTION/VERIFY; "
                    f"{node_id!r} is {node.kind.value}")
            self._workflows.set_status(node_id, NodeStatus.READY)
            self._emit(EventKind.ACTION_REQUEUED, {"node_id": node_id},
                       correlation_id or node_id)
            self._refresh_projection_data()

    # ── bounded loop protocol ───────────────────────────────────────────
    def begin_loop_iteration(self, node_id: str, *,
                             correlation_id: str = "") -> int:
        """Start the next iteration of a READY bounded loop (1-based); the
        body children are (re)armed READY — their per-iteration commits
        are ephemeral until the termination decision lands."""
        with self._lock:
            self._require_executable_locked()
            node = self._workflows.node(node_id)
            if node is None or node.kind is not NodeKind.BOUNDED_LOOP:
                raise ValidationError(f"node {node_id!r} is not a BOUNDED_LOOP")
            st = self._workflows.snapshot().statuses.get(node_id)
            if st is not NodeStatus.READY:
                raise ValidationError(f"loop {node_id!r} not READY (status={st})")
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
        """Report the termination decision for the iteration whose body has
        FULLY committed. Only ``terminated=True`` commits the loop; False
        re-arms it, or FAILS it at the max guard with an escalation payload."""
        with self._lock:
            self._require_executable_locked()
            node = self._workflows.node(node_id)
            if node is None or node.kind is not NodeKind.BOUNDED_LOOP:
                raise ValidationError(f"node {node_id!r} is not a BOUNDED_LOOP")
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
                    f"loop {node_id!r} body not fully committed: {incomplete}")
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
                outcome = {"outcome": "continue", "iteration": iteration,
                           "next_iteration": iteration + 1}
            self._emit(EventKind.LOOP_ITERATION_EVALUATED,
                       {"node_id": node_id, "terminated": terminated,
                        "detail": detail, **outcome}, correlation_id or node_id)
            self._refresh_projection_data()
            return outcome

    # ── checkpoints (stable boundaries) ─────────────────────────────────
    def commit_checkpoint(self, checkpoint_id: str, label: str, *,
                          correlation_id: str = "") -> CheckpointRecord:
        """Governance-driven checkpoint. The record id is namespaced by
        construction (``ckpt:<name>``) so a caller-chosen name can never
        collide with a workflow node / action / plan id."""
        with self._lock:
            self._require_executable_locked()
            return self._commit_checkpoint_locked(
                f"ckpt:{checkpoint_id}", label, correlation_id=correlation_id)

    def _commit_checkpoint_locked(self, record_id: str, label: str,
                                  *, correlation_id: str = "") -> CheckpointRecord:
        # stability: nothing may be in flight — the recorded world would be
        # neither the before nor the after of an in-flight write
        snap = self._workflows.snapshot()
        running = sorted(nid for nid, st in snap.statuses.items()
                         if st is NodeStatus.RUNNING)
        active = sorted(h["action_id"] for h in self._actions.values()
                        if h["status"] in ("requested", "started")
                        and h["epoch"] == self.epoch)
        if running or active:
            raise ValidationError(
                f"checkpoint {record_id!r} requires a stable action "
                f"boundary; in-flight nodes={running} actions={active}")
        state = self._sessions.task_state()
        rec = self._checkpoints.add(CheckpointRecord(
            checkpoint_id=record_id, label=label,
            state_revision=state.revision,
            event_index=len(self._events),  # exclusive boundary
            epoch=self.epoch, intent=state.intent,
            structure={v.semantic_key: {"label": v.label,
                                        "value_type": v.value_type,
                                        "mutability": v.mutability}
                       for v in state.variables},
            observed=state.observed_values(),
            desired=state.desired_values(),
            committed_nodes=self._workflows.committed_node_ids(),
            created_at=time.time()))
        self._emit(EventKind.CHECKPOINT_COMMITTED,
                   {"checkpoint_id": record_id, "label": label,
                    "state_revision": rec.state_revision,
                    "event_index": rec.event_index},
                   correlation_id or record_id)
        return rec

    # ── governance patches (atomic: validate fully, then mutate) ────────
    def apply_local_patch(self, patch: LocalPatch) -> dict[str, Any]:
        """Local adjustment: DESIRED values only (single source of truth).
        Unknown / readonly / locked targets are rejected against the
        CURRENT state (Kernel STATE); duplicate keys were already rejected
        by the LocalPatch constructor (content). Uncommitted ACTION
        contracts referencing an updated key are deterministically
        retargeted; bumps the epoch on success."""
        if not isinstance(patch, LocalPatch):
            raise PatchSemanticsError("apply_local_patch accepts LocalPatch only")
        with self._lock:
            self._require_executable_locked()
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
                    "is readonly/locked — they change only via recompose")
            updates = {u.semantic_key: u.new_value
                       for u in patch.variable_updates}
            new_vars = tuple(
                v.with_desired(updates[v.semantic_key])
                if v.semantic_key in updates else v
                for v in state.variables)
            state = self._sessions.set_task_state(replace(state, variables=new_vars))
            retargeted = self._workflows.retarget_action_contracts(updates)
            epoch = self._bump_epoch_locked()
            self._emit(EventKind.PLAN_PATCHED,
                       {"patch_id": patch.patch_id, "patch_class": "local",
                        "updated_variables": sorted(updates),
                        "retargeted_nodes": retargeted,
                        "requires_replan": False, "epoch": epoch,
                        "rationale": patch.rationale},
                       patch.correlation_id or patch.patch_id)
            self._refresh_projection_data()
            return {"epoch": epoch, "requires_replan": False,
                    "retargeted_nodes": retargeted}

    def apply_goal_patch(self, patch: GoalPatch) -> dict[str, Any]:
        """Terminal change — PHASE ONE of the closed two-phase transition:
        bump epoch, update intent, INVALIDATE the uncommitted future,
        block execution. NEVER installs a graph or schema: the only
        re-closure is ``recompose``."""
        if not isinstance(patch, GoalPatch):
            raise PatchSemanticsError("apply_goal_patch accepts GoalPatch only")
        with self._lock:
            epoch = self._bump_epoch_locked()
            intent_changed = False
            if patch.new_intent is not None:
                old = self._sessions.task_state().intent
                intent_changed = not patch.new_intent.describes_same_terminal(old)
                self._sessions.set_intent(patch.new_intent)
            invalidated = self._workflows.invalidate_future()
            self._pending_recompose = (
                f"GoalPatch {patch.patch_id}"
                + (f": {patch.rationale}" if patch.rationale else ""))
            self._emit(EventKind.PLAN_PATCHED,
                       {"patch_id": patch.patch_id, "patch_class": "goal",
                        "intent_changed": intent_changed,
                        "invalidated_node_ids": invalidated,
                        "requires_replan": True, "epoch": epoch,
                        "pending_recompose": True,
                        "rationale": patch.rationale},
                       patch.correlation_id or patch.patch_id)
            self._refresh_projection_data()
            return {"epoch": epoch, "requires_replan": True,
                    "intent_changed": intent_changed,
                    "invalidated_node_ids": invalidated}

    # ── compensation (derived from the committed action history) ────────
    def request_compensation(self, patch: CompensationPatch) -> CompensationPlan:
        """Build the reversion plan from the kernel's OWN committed action
        history since the target checkpoint (LIFO): a variable introduced
        later still has its true pre-action 'before'; reality that moved
        WITHOUT a TaskVM action produces NO entry; IRREVERSIBLE actions go
        to ``uncompensatable``, never disguised as revertible."""
        if not isinstance(patch, CompensationPatch):
            raise PatchSemanticsError(
                "request_compensation accepts CompensationPatch only")
        with self._lock:
            rec = self._checkpoints.get(patch.target_checkpoint_id)
            post = [r for r in self._action_history
                    if r["event_index"] > rec.event_index]
            entries: list[CompensationEntry] = []
            blocked: list[UncompensatableAction] = []
            for r in reversed(post):   # LIFO: undo the latest write first
                if r["reversibility"] is Reversibility.IRREVERSIBLE:
                    blocked.append(UncompensatableAction(
                        node_id=r["node_id"], semantic_keys=tuple(r["after"]),
                        reversibility=r["reversibility"],
                        reason="irreversible action: honest non-compensation"))
                    continue
                for key, after_v in r["after"].items():
                    before_v = r["before"].get(key)
                    if before_v == after_v:
                        continue
                    entries.append(CompensationEntry(
                        node_id=r["node_id"], semantic_key=key,
                        from_observed=after_v, to_observed=before_v,
                        to_desired=rec.desired.get(key, before_v),
                        reversibility=r["reversibility"]))
            state = self._sessions.task_state()
            requires_recompose = bool(
                (rec.intent is not None
                 and not rec.intent.describes_same_terminal(state.intent))
                or self._structure_differs(rec, state))
            epoch = self._bump_epoch_locked()
            self._comp_seq += 1
            plan = CompensationPlan(
                plan_id=f"comp:{self._comp_seq:05d}",
                target_checkpoint_id=rec.checkpoint_id,
                entries=tuple(entries), epoch=epoch, created_at=time.time(),
                uncompensatable=tuple(blocked),
                requires_recompose=requires_recompose)
            self._comp_plans[plan.plan_id] = plan
            self._comp_status[plan.plan_id] = "pending"
            self._emit(EventKind.COMPENSATION_REQUESTED,
                       {"patch_id": patch.patch_id, "plan_id": plan.plan_id,
                        "target_checkpoint_id": rec.checkpoint_id,
                        "entries": [{"node_id": e.node_id,
                                     "semantic_key": e.semantic_key,
                                     "from": e.from_observed,
                                     "to": e.to_observed} for e in entries],
                        "uncompensatable_nodes": sorted({b.node_id for b in blocked}),
                        "requires_recompose": requires_recompose,
                        "epoch": epoch},
                       patch.correlation_id or patch.patch_id)
            return plan

    def record_compensation_result(self, plan_id: str,
                                   result: CompensationResult) -> str:
        """Land the runtime's typed CompensationResult; returns the
        timeline disposition "complete" | "partial" | "failed" |
        "discarded" (docs/contracts/kernel.md §3). TIME checks only;
        the per-entry verdicts are E's content judgment."""
        if not isinstance(result, CompensationResult):
            raise ValidationError(
                "record_compensation_result accepts a typed CompensationResult")
        with self._lock:
            plan = self._comp_plans.get(plan_id)
            if plan is None:
                raise ValidationError(f"unknown compensation plan {plan_id!r}")
            if result.plan_id != plan_id:
                raise ValidationError(
                    f"result names plan {result.plan_id!r}, not {plan_id!r}")
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
                return "discarded"
            if result.epoch != plan.epoch:
                raise ValidationError(
                    f"result epoch {result.epoch} does not match the "
                    f"plan's epoch {plan.epoch}")
            reported = {(r.node_id, r.semantic_key): r
                        for r in result.entry_results}
            known = {(e.node_id, e.semantic_key) for e in plan.entries}
            foreign = sorted(k for k in reported if k not in known)
            if foreign:
                raise ValidationError(
                    f"result entries {foreign} are not part of plan {plan_id}")
            undone_keys = {k for k in known
                           if k in reported and reported[k].compensated}
            if plan.entries and not undone_keys:
                self._comp_status[plan_id] = "failed"
                self._emit(EventKind.COMPENSATION_FAILED,
                           {"plan_id": plan_id, "disposition": "failed",
                            "detail": result.detail}, plan_id)
                self._refresh_projection_data()
                return "failed"
            # §4.11: COMPLETE means the timeline is fully back at the
            # boundary — every reversible entry landed compensated AND no
            # uncompensatable standing work remains (an IRREVERSIBLE
            # commit that stays is honest PARTIAL, never a fake COMPLETE)
            complete = (undone_keys == known and not plan.uncompensatable)
            undone_nodes = self._undone_nodes(plan, undone_keys)
            rec = self._checkpoints.get(plan.target_checkpoint_id)
            audit = self._restore_governance_locked(
                plan, rec, reported, complete)
            blocked_ids = {b.node_id for b in plan.uncompensatable}
            boundary = set(rec.committed_nodes)
            compensated_nodes = sorted(
                nid for nid in self._workflows.committed_node_ids()
                if nid not in boundary and nid not in blocked_ids
                and (complete or nid in undone_nodes))
            invalidated: list[str] = []
            if complete and not plan.requires_recompose:
                # deterministic frontier rewind to the boundary
                self._workflows.rewind_to_boundary(
                    frozenset(boundary | blocked_ids))
                # §4.11: the checkpoint's desired plane is authoritative
                # for the re-armed future — deterministically retarget the
                # surviving future contracts (the same channel as
                # LocalPatch; committed history is never touched), so the
                # runtime chases the restored targets, never the abandoned
                # ones
                self._workflows.retarget_action_contracts(dict(rec.desired))
            else:
                # cross-boundary, or PARTIAL: undone work is honestly
                # marked COMPENSATED; the remaining future is void and the
                # timeline waits for governance (recompose)
                self._workflows.mark_compensated(compensated_nodes)
                invalidated = self._workflows.invalidate_future()
                self._pending_recompose = (
                    f"rollback to {rec.checkpoint_id} "
                    + ("crossed an intent/structure boundary"
                       if plan.requires_recompose else "is PARTIAL"))
            self._consume_history(rec, undone_nodes, complete)
            # §4.11: loops rewound or voided by the rollback lose their
            # pre-rollback progress and restart from iteration 1 — only
            # loops still COMMITTED keep their counter (checkpoints pin
            # stable boundaries, so no loop is mid-flight here)
            still_committed = {
                nid for nid, st in self._workflows.snapshot().statuses.items()
                if st is NodeStatus.COMMITTED}
            self._loop_iters = {
                nid: it for nid, it in self._loop_iters.items()
                if nid in still_committed}
            payload = {"plan_id": plan_id, "detail": result.detail,
                       "restored": audit["restored"],
                       "compensated_nodes": compensated_nodes,
                       "uncompensatable_nodes": sorted(blocked_ids),
                       "intent_restored": audit["intent_restored"],
                       "restored_structure_keys": audit["restored_structure"],
                       "metadata_restored": audit["metadata_restored"],
                       "requires_recompose": plan.requires_recompose,
                       "invalidated_node_ids": invalidated}
            if complete:
                truncated = self._checkpoints.truncate_after(rec.checkpoint_id)
                self._comp_status[plan_id] = "complete"
                self._emit(EventKind.COMPENSATION_APPLIED,
                           {**payload, "disposition": "complete",
                            "truncated_checkpoint_ids": truncated}, plan_id)
            else:
                self._comp_status[plan_id] = "partial"
                self._emit(EventKind.COMPENSATION_PARTIAL,
                           {**payload, "disposition": "partial",
                            "uncompensated": [
                                {"node_id": k[0], "semantic_key": k[1]}
                                for k in sorted(known - undone_keys)]},
                           plan_id)
            self._refresh_projection_data()
            return "complete" if complete else "partial"

    # ── governance events & conflicts ───────────────────────────────────
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
            cid = correlation_id or f"conflict:{len(self._events) + 1:05d}"
            self._emit(EventKind.CONFLICT_DETECTED,
                       {"description": description,
                        "semantic_keys": list(semantic_keys)}, cid)
            return cid

    def resolve_conflict(self, resolution: str, *,
                         correlation_id: str = "") -> None:
        with self._lock:
            self._emit(EventKind.CONFLICT_RESOLVED,
                       {"resolution": resolution}, correlation_id)

    # ── internals ───────────────────────────────────────────────────────
    def _require_action(self, action_id: str) -> dict[str, Any]:
        handle = self._actions.get(action_id)
        if handle is None:
            raise ValidationError(f"unknown action {action_id!r}")
        return handle

    @staticmethod
    def _reject_terminal_handle(handle: dict[str, Any]) -> None:
        """A FINISHED/DISCARDED handle can never land again."""
        if handle["status"] in ("finished", "discarded"):
            raise ValidationError(
                f"action handle {handle['action_id']} is terminal "
                f"({handle['status']}); a result lands exactly once")

    def _discard_action_locked(self, handle: dict[str, Any],
                               payload: dict[str, Any]) -> None:
        handle["status"] = "discarded"
        self._emit(EventKind.ACTION_DISCARDED,
                   {"action_id": handle["action_id"],
                    "node_id": handle["node_id"], **payload},
                   handle["action_id"])
        self._refresh_projection_data()

    @staticmethod
    def _structure_differs(rec: CheckpointRecord, state: TaskState) -> bool:
        """Metadata-aware structure comparison: keys AND label/type/
        mutability — a same-key widget change IS a structure change."""
        current = {v.semantic_key: {"label": v.label,
                                    "value_type": v.value_type,
                                    "mutability": v.mutability}
                   for v in state.variables}
        return rec.structure != current

    def _fold_observations_locked(
            self, observations: Iterable[ObservedValue]
    ) -> tuple[list[str], list[str]]:
        """Fold a validated batch into the OBSERVED plane with evidence;
        returns (updated keys, keys that carried evidence). Unknown keys
        are rejected against the CURRENT state. No event — the caller owns
        the single event."""
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

    # ── compensation landing helpers (HISTORY / TRANSITION) ─────────────
    @staticmethod
    def _undone_nodes(plan: CompensationPlan, undone_keys: set) -> set[str]:
        """A node's write is undone iff EVERY plan entry it owns landed
        compensated (a partially-undone action still stands)."""
        ok: dict[str, bool] = {}
        for e in plan.entries:
            ok[e.node_id] = ok.get(e.node_id, True) and (
                (e.node_id, e.semantic_key) in undone_keys)
        return {nid for nid, good in ok.items() if good}

    def _restore_governance_locked(
            self, plan: CompensationPlan, rec: CheckpointRecord,
            reported: dict, complete: bool) -> dict[str, Any]:
        """Fold the reported fresh observations (plan order ⇒ the
        earliest action's value is the resting one), then restore the
        checkpoint's desired plane + structure metadata + intent. Never
        deletes later-appeared variables; never restores stale evidence.
        On COMPLETE the desired plane is restored for EVERY surviving
        variable (a LocalPatch-only drift owns no physical entry); on
        PARTIAL only the physically undone entries rewind their desired
        — standing writes keep their standing targets."""
        by_key: dict[str, Any] = {}
        for e in plan.entries:
            r = reported.get((e.node_id, e.semantic_key))
            if r is not None:
                by_key[e.semantic_key] = r.final_observed
        if by_key:
            self._fold_observations_locked(
                [ObservedValue(semantic_key=k, value=v)
                 for k, v in by_key.items()])
        state = self._sessions.task_state()
        final_entry = {e.semantic_key: e for e in plan.entries}
        metadata_restored: list[str] = []
        new_vars: list[TaskVariable] = []
        for v in state.variables:
            nv = v
            if v.semantic_key in final_entry:
                nv = nv.with_desired(final_entry[v.semantic_key].to_desired)
            elif complete and v.semantic_key in rec.desired:
                # §4.11: a COMPLETE rollback rewinds governance to the
                # boundary for every surviving variable — not only those
                # with a physical compensation entry
                nv = nv.with_desired(rec.desired[v.semantic_key])
            meta = rec.structure.get(v.semantic_key)
            if meta and (nv.label, nv.value_type, nv.mutability) != (
                    meta["label"], meta["value_type"], meta["mutability"]):
                nv = replace(nv, label=meta["label"],
                             value_type=meta["value_type"],
                             mutability=meta["mutability"])
                metadata_restored.append(v.semantic_key)
            new_vars.append(nv)
        existing = {v.semantic_key for v in new_vars}
        restored_structure: list[str] = []
        for key, meta in rec.structure.items():
            if key not in existing:
                new_vars.append(TaskVariable(
                    semantic_key=key, label=meta["label"],
                    value_type=meta["value_type"], mutability=meta["mutability"],
                    observed=None,
                    desired=rec.desired.get(key)))
                restored_structure.append(key)
        intent_restored = (rec.intent is not None
                           and not rec.intent.describes_same_terminal(state.intent))
        self._sessions.set_task_state(TaskState(
            intent=(rec.intent if (intent_restored and rec.intent is not None)
                    else state.intent),
            variables=tuple(new_vars)))
        return {"restored": by_key,
                "metadata_restored": sorted(metadata_restored),
                "restored_structure": sorted(restored_structure),
                "intent_restored": intent_restored}

    def _consume_history(self, rec: CheckpointRecord,
                         undone_nodes: set[str], complete: bool) -> None:
        """Consume the compensated slice of the action history. COMPLETE
        consumes every reversible post-boundary record; PARTIAL consumes
        only the undone nodes' records — standing writes keep their
        history for a later, earlier-checkpoint rollback. IRREVERSIBLE
        (blocked) work was NOT undone and always persists."""
        self._action_history = [
            r for r in self._action_history
            if r["event_index"] <= rec.event_index
            or r["reversibility"] is Reversibility.IRREVERSIBLE
            or (not complete and r["node_id"] not in undone_nodes)]

    def _bump_epoch_locked(self) -> int:
        epoch = self._sessions.bump_epoch()
        self._workflows.mark_running_stale_reset()
        return epoch

    def _reset_running_node(self, node_id: str) -> None:
        snap = self._workflows.snapshot()
        if snap.statuses.get(node_id) is NodeStatus.RUNNING:
            self._workflows.set_status(node_id, NodeStatus.READY)

    def _refresh_projection_data(self) -> None:
        """Projection data mirrors the stores via an AUTHORITATIVE replace
        (no stale keys)."""
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
