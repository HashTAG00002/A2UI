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
    record_verification. Only ACTION nodes produce CUA work handles, and
    verification requires a FINISHED handle of the CURRENT epoch.
  - VERIFY (control): record_verification directly from READY — the
    runtime observes independently and reports; no action handle exists.
    VERIFY is the ONLY kind that may transition READY → FAILED.
  - BARRIER / CHECKPOINT / TERMINAL (control): advance_control from
    READY. CHECKPOINT additionally writes a CheckpointRecord (the node is
    logically committed FIRST, so it belongs to its own boundary);
    TERMINAL commit means the plan is complete.

GOALPATCH TWO-PHASE CLOSURE (fixed — Wave-A.2 audit G3):
  apply_goal_patch: bump epoch + update intent + INVALIDATE the
  uncommitted future + block execution. It NEVER half-installs anything.
  recompose(new_variables, new_graph, new_schema): the ONLY re-closure
  entry — one atomic install of the complete new composition, then
  execution resumes.

Enforced invariants (handoff 02 §Kernel 服务 + Wave-A reviews):
  1. revisions are store-assigned and strictly monotonic;
  2. projection schema/data revisions are independent counters;
  3. GoalPatch/recompose can never silently rewrite or drop committed
     nodes (ephemeral loop-body commits are NOT committed history);
  4. action results from a stale epoch are discarded without touching
     TaskState (finish_action returns False, ActionDiscarded emitted);
  5. checkpoints pin an exact event-log index + state revision + epoch,
     and are only taken at a STABLE action boundary (nothing in flight);
  6. compensation is derived from the kernel's own COMMITTED ACTION
     HISTORY (before/after recorded at action time) — never from a
     snapshot-diff of the world; IRREVERSIBLE work is reported as
     uncompensatable, never fake-reverted; a reported compensation is
     accepted only when freshly observed values match the plan's targets;
  7. every read returns an immutable snapshot / defensive deep copy, and
     every WRITE boundary deep-copies its input (bidirectional);
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
 13. composition boundaries (set_plan / recompose) reject architect
     output whose projection bindings or contract keys reference unknown
     task variables, AND whose final contract targets disagree with the
     variables' desired plane (the split-brain guard);
 14. set_plan and init_task_state are one-shot; after a GoalPatch,
     execution stays blocked until recompose closes the composition.
"""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import replace
from typing import Any, Iterable

from taskvm.domain.contract import Reversibility
from taskvm.domain.errors import (
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
    UncompensatableAction,
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
        # GoalPatch/rollback closure state: while set, execution is blocked
        # until recompose() atomically installs the new composition (G3)
        self._pending_recompose: str | None = None
        # committed action history — the ONLY source of compensation (G5).
        # Each entry: {node_id, epoch, event_index, before, after,
        # reversibility}; appended at verification time (committed only).
        self._action_history: list[dict[str, Any]] = []

    # ══ introspection (snapshots — invariant 7) ═════════════════════════
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

    def _require_executable_locked(self) -> None:
        """Execution gate (invariant 14): after a GoalPatch or a
        cross-boundary rollback, NOTHING advances until recompose() has
        atomically closed the new composition."""
        if self._pending_recompose is not None:
            raise ValidationError(
                "execution blocked: recompose() required before execution "
                f"continues ({self._pending_recompose})")

    # ══ composition (State Compiler / Task Architect output) ════════════
    def init_task_state(self, variables: Iterable[TaskVariable],
                        *, correlation_id: str = "") -> TaskState:
        """Install the initially compiled variables. ONE-SHOT (keyed on an
        explicit initialized flag — an EMPTY initial composition is legal
        and still counts; audit G13a). Structural changes MUST go through
        ``recompose``."""
        with self._lock:
            if self._sessions.initialized:
                raise ValidationError(
                    "init_task_state is one-shot; use recompose() for "
                    "structural updates")
            state = self._sessions.set_task_state(
                TaskState(intent=self._sessions.task_state().intent,
                          variables=tuple(variables)))
            self._sessions.mark_initialized()
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
        """The ONLY re-closure entry after a GoalPatch — and the legitimate
        entry for structure drift. ONE atomic install of the complete new
        composition (variables + graph + schema).

        Rules (audit G3):
        - after a GoalPatch, ``new_graph`` is MANDATORY (the old future
          was invalidated; there is nothing to retain);
        - without a pending GoalPatch, omitting graph/schema retains the
          current ones — but the RETAINED composition is validated against
          the new variables exactly like a supplied one (no dangling
          contract keys, no desired split-brain);
        - atomic: full validation before any mutation; on success the
          execution gate opens again.
        """
        if not reason:
            raise ValidationError("recompose requires a reason")
        with self._lock:
            if not self._sessions.initialized:
                raise ValidationError(
                    "recompose requires an initialised state; use "
                    "init_task_state first")
            if self._pending_recompose is not None and new_graph is None:
                raise ValidationError(
                    "recompose after a GoalPatch requires new_graph: the "
                    "old future was invalidated and cannot be retained")
            new_variables = tuple(variables)
            desired_map = {v.semantic_key: v.desired for v in new_variables}
            current_graph = self._workflows.snapshot().graph
            if new_graph is not None and current_graph is not None:
                self._workflows.validate_replace_future(new_graph)
            effective_graph = new_graph if new_graph is not None else current_graph
            effective_schema = (new_schema if new_schema is not None
                                else self._projections.snapshot().schema)
            self._validate_composition_locked(
                graph=effective_graph, schema=effective_schema,
                variables=desired_map)
            # ── all validation passed; mutate ──
            old = self._sessions.task_state()
            epoch = self._bump_epoch_locked()
            state = self._sessions.set_task_state(
                TaskState(intent=old.intent, variables=new_variables))
            graph_revision = None
            invalidated: list[str] = []
            if new_graph is not None:
                if current_graph is None:
                    installed = self._workflows.install_graph(
                        new_graph, epoch=epoch)
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
                        "kept": sorted(old_keys & new_keys),
                        "graph_revision": graph_revision, "epoch": epoch,
                        "invalidated_node_ids": invalidated,
                        "closed_pending_recompose": closure},
                       correlation_id)
            self._refresh_projection_data()
            return state

    def set_plan(self, graph: WorkflowGraph,
                 schema: ProjectionSchema | None = None,
                 *, correlation_id: str = "") -> WorkflowGraph:
        """Install the initial workflow plan (+ optionally the projection
        schema). ONE-SHOT (audit G8): a second call could wipe execution
        history behind the GoalPatch invariant — future replacement goes
        through apply_goal_patch → recompose ONLY. Composition-validated
        (invariant 13)."""
        with self._lock:
            if self._workflows.snapshot().graph is not None:
                raise ValidationError(
                    "set_plan is one-shot; future replacement goes through "
                    "apply_goal_patch → recompose")
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

        Unknown semantic keys are rejected (structural discovery belongs
        to ``recompose``); a duplicate key inside ONE batch is rejected
        (audit G13b — a silent last-write-wins would eat real conflicts).
        """
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
        present back: {action_id, node_id, epoch, status, contract}."""
        with self._lock:
            self._require_executable_locked()
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
            return copy.deepcopy(handle)

    def start_action(self, action_id: str) -> bool:
        """REQUESTED → STARTED. A stale-epoch start is DISCARDED (False);
        a terminal or already-started handle is a contract error. The
        action's BEFORE observation is recorded here — this is what makes
        compensation action-history-based (invariant 6)."""
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
            state = self._sessions.task_state()
            handle["before_observed"] = {
                key: (v.observed if (v := state.variable(key)) is not None
                      else None)
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
        """Land an action result — exactly once, only from STARTED, only
        in the action's own epoch (invariants 4 + 10). The AFTER
        observation is recorded onto the handle for the compensation
        history. Observations fold into the OBSERVED plane as part of the
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
            state = self._sessions.task_state()
            handle["after_observed"] = {
                key: (v.observed if (v := state.variable(key)) is not None
                      else None)
                for key in handle["contract"].desired_state}
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

        ACTION nodes require a FINISHED handle of the CURRENT epoch
        (audit G2: request → start → finish → verify is the only path —
        start → verify can never commit). VERIFY nodes confirm directly
        from READY and are the ONLY kind allowed READY → FAILED (audit
        G1). A committed ACTION enters the compensation history."""
        with self._lock:
            self._require_executable_locked()
            node = self._workflows.node(node_id)
            if node is None:
                raise ValidationError(f"unknown node {node_id!r}")
            st = self._workflows.snapshot().statuses.get(node_id)
            handle = None
            if node.kind is NodeKind.ACTION:
                handle = self._finished_handle_locked(node_id)
                if handle is None:
                    raise ValidationError(
                        f"ACTION node {node_id!r} has no FINISHED action in "
                        f"epoch {self.epoch}; the protocol is request → "
                        "start → finish → verify")
                if st is not NodeStatus.RUNNING:
                    raise ValidationError(
                        f"ACTION node {node_id!r} not RUNNING (status={st})")
            elif node.kind is NodeKind.VERIFY:
                if st is not NodeStatus.READY:
                    raise ValidationError(
                        f"VERIFY node {node_id!r} not READY (status={st})")
            else:
                raise ValidationError(
                    f"node {node_id!r} is {node.kind.value}; control nodes "
                    "advance via advance_control")
            self._workflows.set_status(
                node_id, NodeStatus.COMMITTED if passed else NodeStatus.FAILED)
            self._emit(EventKind.VERIFICATION_PASSED if passed
                       else EventKind.VERIFICATION_FAILED,
                       {"node_id": node_id, "kind": node.kind.value,
                        "detail": detail}, correlation_id)
            if passed and node.kind is NodeKind.ACTION and handle is not None:
                # only VERIFIED work becomes compensable history (inv. 6)
                self._action_history.append({
                    "node_id": node_id,
                    "epoch": handle["epoch"],
                    "event_index": len(self._events),
                    "before": dict(handle.get("before_observed") or {}),
                    "after": dict(handle.get("after_observed") or {}),
                    "reversibility": handle["contract"].reversibility,
                })
            self._refresh_projection_data()

    def advance_control(self, node_id: str, *,
                        correlation_id: str = "") -> CheckpointRecord | None:
        """Advance a READY control node (BARRIER / CHECKPOINT / TERMINAL)
        to COMMITTED. A CHECKPOINT node additionally writes a
        CheckpointRecord (committed FIRST, so it is part of its own
        boundary — audit G7). A TERMINAL commit means the plan is
        complete. Returns the CheckpointRecord for checkpoints."""
        with self._lock:
            self._require_executable_locked()
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
        """Retry path: FAILED → READY, gated to ACTION/VERIFY (audit
        G13c): a maxed-out BOUNDED_LOOP can never be 'requeued' into a
        state that can run — it needs governance / a new plan."""
        with self._lock:
            self._require_executable_locked()
            node = self._workflows.node(node_id)
            if node is None:
                raise ValidationError(f"unknown node {node_id!r}")
            if node.kind not in (NodeKind.ACTION, NodeKind.VERIFY):
                raise ValidationError(
                    f"requeue is only defined for ACTION/VERIFY nodes; "
                    f"{node_id!r} is {node.kind.value}")
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
            self._require_executable_locked()
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
            self._require_executable_locked()
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

    # ══ checkpoints (invariant 5: stable boundaries) ════════════════════
    def commit_checkpoint(self, checkpoint_id: str, label: str, *,
                          correlation_id: str = "") -> CheckpointRecord:
        """Governance-driven checkpoint (the user's 'mark this as a
        checkpoint' gesture). Workflow CHECKPOINT nodes use
        ``advance_control`` instead — or this method with the node's id
        when it is READY (equivalent)."""
        with self._lock:
            self._require_executable_locked()
            return self._commit_checkpoint_locked(
                checkpoint_id, label, correlation_id=correlation_id)

    def _commit_checkpoint_locked(self, checkpoint_id: str, label: str,
                                  *, correlation_id: str = ""
                                  ) -> CheckpointRecord:
        # stability (audit G9): a checkpoint is a STABLE action boundary —
        # nothing may be in flight (the recorded world would be neither
        # the before nor the after of the in-flight write)
        snap = self._workflows.snapshot()
        running = sorted(nid for nid, st in snap.statuses.items()
                         if st is NodeStatus.RUNNING)
        active = sorted(h["action_id"] for h in self._actions.values()
                        if h["status"] in ("requested", "started")
                        and h["epoch"] == self.epoch)
        if running or active:
            raise ValidationError(
                f"checkpoint {checkpoint_id!r} requires a stable action "
                f"boundary; in-flight nodes={running} actions={active}")
        # id collision (audit G9): an id matching a workflow node is only
        # legal for a READY CHECKPOINT node — and then it IS that node's
        # advancement
        wf_node = snap.graph.node(checkpoint_id) if snap.graph else None
        if wf_node is not None:
            st = snap.statuses.get(checkpoint_id)
            if wf_node.kind is not NodeKind.CHECKPOINT or (
                    st is not NodeStatus.READY):
                raise ValidationError(
                    f"checkpoint id {checkpoint_id!r} collides with "
                    f"workflow node (kind={wf_node.kind.value}, "
                    f"status={st})")
            # logical commit FIRST, so the node belongs to its own
            # boundary record (audit G7)
            self._workflows.set_status(checkpoint_id, NodeStatus.COMMITTED)
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
        self._emit(EventKind.CHECKPOINT_COMMITTED,
                   {"checkpoint_id": checkpoint_id, "label": label,
                    "state_revision": rec.state_revision,
                    "event_index": rec.event_index},
                   correlation_id or checkpoint_id)
        return rec

    # ══ governance patches (atomic — invariant 8) ═══════════════════════
    def apply_local_patch(self, patch: LocalPatch) -> dict[str, Any]:
        """Local adjustment: DESIRED values only — the single source of
        truth (audit G4). The kernel deterministically retargets every
        not-yet-historical ACTION contract referencing an updated key, so
        the runtime can never receive a stale target. Topology, intent,
        evidence, reversibility and risk class are structurally out of
        reach. Atomic; bumps the epoch on success (in-flight work predates
        the adjustment)."""
        if not isinstance(patch, LocalPatch):
            raise PatchSemanticsError(
                "apply_local_patch accepts LocalPatch only")
        with self._lock:
            self._require_executable_locked()
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
            # ── all validation passed; mutate ──
            updates = {u.semantic_key: u.new_value
                       for u in patch.variable_updates}
            new_vars = tuple(
                v.with_desired(updates[v.semantic_key])
                if v.semantic_key in updates else v
                for v in state.variables)
            state = self._sessions.set_task_state(
                replace(state, variables=new_vars))
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
        """Terminal change — PHASE ONE of the closed two-phase transition
        (audit G3): bump epoch, update the intent, INVALIDATE the whole
        uncommitted future, and block execution. This method NEVER
        installs a graph or schema: the only re-closure is ``recompose``.
        """
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

    # ══ compensation (action-history-based — invariant 6) ═══════════════
    def request_compensation(self, patch: CompensationPatch) -> CompensationPlan:
        """Build the reversion plan from the kernel's OWN committed action
        history since the target checkpoint (audit G5): every verified
        action recorded its before/after at execution time, so:

        - a variable introduced after the checkpoint still has its true
          pre-action 'before' (no snapshot-diff blind spot);
        - reality that moved WITHOUT a TaskVM action produces NO entry
          (external drift is not TaskVM's to undo);
        - IRREVERSIBLE actions are reported in ``uncompensatable``,
          never disguised as revertible value changes.
        """
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
                        node_id=r["node_id"],
                        semantic_keys=tuple(r["after"]),
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
            intent_differs = (rec.intent is not None
                              and not rec.intent.describes_same_terminal(
                                  state.intent))
            structure_differs = self._structure_differs(rec, state)
            requires_recompose = bool(intent_differs or structure_differs)
            epoch = self._bump_epoch_locked()
            self._comp_seq += 1
            plan = CompensationPlan(
                plan_id=f"comp_{self._comp_seq:05d}",
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
                                     "to": e.to_observed}
                                    for e in entries],
                        "uncompensatable_nodes": sorted(
                            {b.node_id for b in blocked}),
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

        ``applied=True`` is NEVER taken on faith (invariant 6): every plan
        entry's final target must match the freshly observed values. On
        full match the kernel: folds the fresh observations; restores the
        desired plane + structure metadata (label/value_type/mutability —
        never stale evidence) for checkpoint variables; re-adds checkpoint
        variables that structurally vanished; NEVER deletes variables that
        only appeared later (no logical state deletion); restores the
        checkpoint intent; and rewinds the workflow — same intent ⇒ the
        frontier returns to the boundary and the same path is re-armed
        deterministically (audit G6), crossed intent/structure ⇒
        post-checkpoint commits are honestly marked COMPENSATED and the
        remaining future is INVALIDATED pending recompose.
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
            # final target per key: entries are LIFO, so the LAST write in
            # this loop is the EARLIEST action — the resting value
            expected: dict[str, Any] = {}
            final_entry: dict[str, CompensationEntry] = {}
            for e in plan.entries:
                expected[e.semantic_key] = e.to_observed
                final_entry[e.semantic_key] = e
            mismatches: dict[str, dict[str, Any]] = {}
            if applied and observed_values is not None:
                for key, target in expected.items():
                    got = observed_values.get(key, _MISSING)
                    if got is _MISSING or got != target:
                        mismatches[key] = {
                            "expected": target,
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
            # 1. fold the fresh observations (observed plane)
            self._fold_observations_locked(
                [ObservedValue(semantic_key=k, value=v)
                 for k, v in (observed_values or {}).items()])
            # 2. desired plane + structure metadata for checkpoint variables
            state = self._sessions.task_state()
            metadata_restored: list[str] = []
            new_vars: list[TaskVariable] = []
            for v in state.variables:
                nv = v
                if v.semantic_key in final_entry:
                    nv = nv.with_desired(final_entry[v.semantic_key].to_desired)
                meta = rec.structure.get(v.semantic_key)
                if meta and (nv.label, nv.value_type, nv.mutability) != (
                        meta["label"], meta["value_type"], meta["mutability"]):
                    nv = replace(nv, label=meta["label"],
                                 value_type=meta["value_type"],
                                 mutability=meta["mutability"])
                    metadata_restored.append(v.semantic_key)
                new_vars.append(nv)
            # 3. re-add checkpoint variables that structurally vanished
            existing = {v.semantic_key for v in new_vars}
            restored_structure: list[str] = []
            for key, meta in rec.structure.items():
                if key not in existing:
                    new_vars.append(TaskVariable(
                        semantic_key=key,
                        label=meta["label"], value_type=meta["value_type"],
                        mutability=meta["mutability"],
                        observed=rec.observed.get(key),
                        desired=rec.desired.get(key)))
                    restored_structure.append(key)
            # 4. intent
            intent_restored = (rec.intent is not None
                               and not rec.intent.describes_same_terminal(
                                   state.intent))
            self._sessions.set_task_state(TaskState(
                intent=(rec.intent
                        if (intent_restored and rec.intent is not None)
                        else state.intent),
                variables=tuple(new_vars)))
            # 5. workflow rewind
            blocked_ids = {b.node_id for b in plan.uncompensatable}
            boundary = set(rec.committed_nodes)
            compensated_nodes = sorted(
                nid for nid in self._workflows.committed_node_ids()
                if nid not in boundary and nid not in blocked_ids)
            invalidated: list[str] = []
            if plan.requires_recompose:
                self._workflows.mark_compensated(compensated_nodes)
                invalidated = self._workflows.invalidate_future()
                self._pending_recompose = (
                    f"rollback to {rec.checkpoint_id} crossed an "
                    "intent/structure boundary")
            else:
                self._workflows.rewind_to_boundary(
                    frozenset(boundary | blocked_ids))
            # 6. consume the compensated history (blocked actions persist —
            #    they were NOT undone and stay candidates for an earlier
            #    checkpoint's rollback)
            self._action_history = [
                r for r in self._action_history
                if r["event_index"] <= rec.event_index
                or r["reversibility"] is Reversibility.IRREVERSIBLE]
            self._comp_status[plan_id] = "applied"
            self._emit(EventKind.COMPENSATION_APPLIED,
                       {"plan_id": plan_id, "detail": detail,
                        "restored": {k: e.to_observed
                                     for k, e in final_entry.items()},
                        "compensated_nodes": compensated_nodes,
                        "uncompensatable_nodes": sorted(blocked_ids),
                        "intent_restored": intent_restored,
                        "restored_structure_keys": sorted(restored_structure),
                        "metadata_restored": sorted(metadata_restored),
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

    def _finished_handle_locked(self, node_id: str) -> dict[str, Any] | None:
        """The (single) FINISHED handle for this node in the CURRENT epoch,
        or None. This is what makes 'verify without finish' impossible."""
        for h in self._actions.values():
            if (h["node_id"] == node_id and h["epoch"] == self.epoch
                    and h["status"] == "finished"):
                return h
        return None

    @staticmethod
    def _structure_differs(rec: CheckpointRecord, state: TaskState) -> bool:
        """Metadata-aware structure comparison (audit G12): keys AND each
        variable's label/value_type/mutability — a same-key widget change
        IS a structure change."""
        current = {v.semantic_key: {"label": v.label,
                                    "value_type": v.value_type,
                                    "mutability": v.mutability}
                   for v in state.variables}
        return rec.structure != current

    def _fold_observations_locked(
            self, observations: Iterable[ObservedValue]
    ) -> tuple[list[str], list[str]]:
        """Validate then fold observations into the OBSERVED plane with
        their evidence. Returns (updated keys, keys that carried
        evidence). No event is emitted here — the calling public method
        owns the single event."""
        obs = tuple(observations)
        keys = [o.semantic_key for o in obs]
        if len(set(keys)) != len(keys):
            dups = sorted({k for k in keys if keys.count(k) > 1})
            raise ValidationError(
                f"duplicate semantic keys in one observation batch: {dups}; "
                "aggregate or resolve the conflict upstream first")
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
            variables: dict[str, Any] | None = None) -> None:
        """Composition boundary check (invariant 13). Applies to the
        EFFECTIVE composition (supplied OR retained). Two rejections:

        - unknown references: schema binding keys / non-historical
          contract keys must exist among the variables;
        - split-brain guard: for every key written by non-historical
          ACTION contracts, the FINAL writer's target (in downstream
          order) must equal the variable's desired value, and two
          unordered final writers may never disagree.

        Historical (committed/compensated) nodes are exempt: their
        contracts are frozen records of work already verified.
        """
        desired_map = (variables if variables is not None else
                       {v.semantic_key: v.desired
                        for v in self._sessions.task_state().variables})
        exempt = set(self._workflows.historical_node_ids())
        if schema is not None:
            missing = sorted({c.binding_key for c in schema.components
                              if c.binding_key is not None}
                             - set(desired_map))
            if missing:
                raise ValidationError(
                    f"ProjectionSchema binds unknown task variables "
                    f"{missing}; architect output rejected at the kernel "
                    "boundary")
        if graph is None:
            return
        writers: dict[str, list[tuple[str, Any]]] = {}
        bad: dict[str, list[str]] = {}
        for n in graph.nodes:
            if n.contract is None or n.node_id in exempt:
                continue
            for key, val in n.contract.desired_state.items():
                if key not in desired_map:
                    bad.setdefault(n.node_id, []).append(key)
                else:
                    writers.setdefault(key, []).append((n.node_id, val))
        if bad:
            raise ValidationError(
                "ActionContract desired_state references unknown task "
                f"variables: {bad}; architect output rejected at the "
                "kernel boundary")
        for key, ws in writers.items():
            finals = [(nid, v) for nid, v in ws
                      if not any(other != nid
                                 and other in graph.downstream(nid)
                                 for other, _ in ws)]
            targets: list[Any] = []
            for _, v in finals:
                if v not in targets:
                    targets.append(v)
            if len(targets) > 1:
                raise ValidationError(
                    f"composition incoherent: {key!r} has multiple final "
                    f"writers with different targets {targets}")
            if targets and desired_map[key] != targets[0]:
                raise ValidationError(
                    f"composition incoherent (split-brain guard): variable "
                    f"{key!r} desired={desired_map[key]!r} but the plan's "
                    f"final writer targets {targets[0]!r}")

    def _bump_epoch_locked(self) -> int:
        epoch = self._sessions.bump_epoch()
        self._workflows.mark_running_stale_reset()
        return epoch

    def _reset_running_node(self, node_id: str) -> None:
        snap = self._workflows.snapshot()
        if snap.statuses.get(node_id) is NodeStatus.RUNNING:
            self._workflows.set_status(node_id, NodeStatus.READY)

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
