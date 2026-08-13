"""TaskVMKernel — the state machine at L3 (master handoff §2).

The kernel owns the ONLY writers for every store and turns each accepted
mutation into exactly one event. It is deliberately small and boring:
no Flask, no Playwright, no model calls, no substrate, no benchmark —
those live above/below and talk to the kernel through the public methods
here (docs/contracts/kernel.md).

Enforced invariants (handoff 02 §Kernel 服务):
  1. revisions are store-assigned and strictly monotonic;
  2. projection schema/data revisions are independent counters;
  3. GoalPatch can never silently rewrite or drop committed nodes
     (WorkflowStore.replace_future raises CommittedNodeViolationError);
  4. action results from a stale epoch are discarded without touching
     TaskState (finish_action returns False, ActionDiscarded is emitted);
  5. checkpoints pin an exact event-log index + state revision + epoch;
  6. CompensationPatch is validated against what the kernel itself
     recorded at the checkpoint — never against an external oracle;
  7. every read returns an immutable snapshot / defensive deep copy.
"""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import replace
from typing import Any, Iterable

from taskvm.domain.contract import ActionContract
from taskvm.domain.errors import (
    CompensationMismatchError,
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
from taskvm.domain.projection import ProjectionData, ProjectionSchema
from taskvm.domain.state import SurfaceEvidence, TaskState, TaskVariable
from taskvm.domain.workflow import NodeKind, NodeStatus, WorkflowGraph
from taskvm.kernel.checkpoint_store import CheckpointRecord, CheckpointStore
from taskvm.kernel.event_log import EventLog
from taskvm.kernel.projection_store import ProjectionSnapshot, ProjectionStore
from taskvm.kernel.session_store import TaskSessionStore
from taskvm.kernel.workflow_store import WorkflowSnapshot, WorkflowStore


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

    # ══ initial composition (Task Architect output) ═════════════════════
    def init_task_state(self, variables: Iterable[TaskVariable],
                        *, correlation_id: str = "") -> TaskState:
        """Install the compiled variables (State Compiler output)."""
        with self._lock:
            state = self._sessions.set_task_state(
                TaskState(intent=self._sessions.task_state().intent,
                          variables=tuple(variables)))
            self._emit(EventKind.STATE_UPDATED,
                       {"source": "initial_composition",
                        "keys": [v.semantic_key for v in state.variables]},
                       correlation_id)
            self._refresh_projection_data()
            return state

    def set_plan(self, graph: WorkflowGraph,
                 schema: ProjectionSchema | None = None,
                 *, correlation_id: str = "") -> WorkflowGraph:
        """Install the initial workflow plan (+ optionally the projection
        schema) produced by the Task Architect. Emits PlanCreated."""
        with self._lock:
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
    def apply_observation(
            self, values: dict[str, Any],
            evidence: Iterable[SurfaceEvidence] = (),
            *, correlation_id: str = "") -> TaskState:
        """Fold a fresh observation into the task state. Unknown semantic
        keys are rejected: discovering NEW variables is a structural change
        and belongs to the State Compiler, not to a value sync."""
        with self._lock:
            self._emit(EventKind.OBSERVATION_RECEIVED,
                       {"keys": sorted(values),
                        "n_evidence": len(tuple(evidence))}, correlation_id)
            state = self._sessions.task_state()
            unknown = [k for k in values if state.variable(k) is None]
            if unknown:
                raise ValidationError(
                    f"observation carries unknown semantic keys {unknown}; "
                    "structural discovery must go through re-composition")
            new_vars = tuple(
                v.with_value(values[v.semantic_key])
                if v.semantic_key in values else v
                for v in state.variables)
            state = self._sessions.set_task_state(replace(state, variables=new_vars))
            self._emit(EventKind.STATE_UPDATED,
                       {"source": "observation", "keys": sorted(values)},
                       correlation_id)
            self._refresh_projection_data()
            return state

    # ══ action lifecycle (epoch-stamped — invariant 4) ══════════════════
    def request_action(self, node_id: str, *,
                       correlation_id: str = "") -> dict[str, Any]:
        """Register an action request for a READY ACTION or VERIFY node.
        Returns the handle the runtime must present back:
        {action_id, node_id, epoch, contract, verification}."""
        with self._lock:
            node = self._workflows.node(node_id)
            if node is None or node.kind not in (NodeKind.ACTION,
                                                 NodeKind.VERIFY):
                raise ValidationError(
                    f"node {node_id!r} is not an action/verify node")
            st = self._workflows.snapshot().statuses.get(node_id)
            if st is not NodeStatus.READY:
                raise ValidationError(
                    f"action node {node_id!r} not READY (status={st})")
            self._action_seq += 1
            action_id = f"act_{self._action_seq:05d}"
            handle = {"action_id": action_id, "node_id": node_id,
                      "epoch": self.epoch,
                      "contract": copy.deepcopy(node.contract),
                      "verification": node.verification}
            self._actions[action_id] = handle
            self._emit(EventKind.ACTION_REQUESTED,
                       {"action_id": action_id, "node_id": node_id},
                       correlation_id or action_id)
            return dict(handle)

    def start_action(self, action_id: str) -> None:
        with self._lock:
            handle = self._require_action(action_id)
            self._workflows.set_status(handle["node_id"], NodeStatus.RUNNING)
            self._emit(EventKind.ACTION_STARTED,
                       {"action_id": action_id, "node_id": handle["node_id"]},
                       action_id)
            self._refresh_projection_data()

    def finish_action(self, action_id: str, *,
                      observed_values: dict[str, Any] | None = None,
                      evidence: Iterable[SurfaceEvidence] = ()) -> bool:
        """Land an action result. A result carrying a stale epoch is
        DISCARDED: no state change, ActionDiscarded emitted, False
        returned (invariant 4)."""
        with self._lock:
            handle = self._require_action(action_id)
            if handle["epoch"] != self.epoch:
                self._reset_running_node(handle["node_id"])
                self._emit(EventKind.ACTION_DISCARDED,
                           {"action_id": action_id, "node_id": handle["node_id"],
                            "action_epoch": handle["epoch"],
                            "current_epoch": self.epoch}, action_id)
                self._refresh_projection_data()
                return False
            if observed_values:
                self.apply_observation(observed_values, evidence,
                                       correlation_id=action_id)
            self._emit(EventKind.ACTION_FINISHED,
                       {"action_id": action_id, "node_id": handle["node_id"]},
                       action_id)
            return True

    # ══ verification (independent — mental-model §3.5) ══════════════════
    def record_verification(self, node_id: str, passed: bool, *,
                            detail: str = "",
                            correlation_id: str = "") -> None:
        """Commit (or fail) a RUNNING node based on independent evidence."""
        with self._lock:
            st = self._workflows.snapshot().statuses.get(node_id)
            if st is not NodeStatus.RUNNING:
                raise ValidationError(
                    f"node {node_id!r} not RUNNING (status={st})")
            self._workflows.set_status(
                node_id, NodeStatus.COMMITTED if passed else NodeStatus.FAILED)
            self._emit(EventKind.VERIFICATION_PASSED if passed
                       else EventKind.VERIFICATION_FAILED,
                       {"node_id": node_id, "detail": detail}, correlation_id)
            self._refresh_projection_data()

    def requeue(self, node_id: str, *, correlation_id: str = "") -> None:
        """Retry path: FAILED → READY."""
        with self._lock:
            self._workflows.set_status(node_id, NodeStatus.READY)
            self._emit(EventKind.ACTION_REQUESTED,
                       {"node_id": node_id, "requeue": True}, correlation_id)
            self._refresh_projection_data()

    # ══ checkpoints (invariant 5) ════════════════════════════════════════
    def commit_checkpoint(self, checkpoint_id: str, label: str, *,
                          correlation_id: str = "") -> CheckpointRecord:
        with self._lock:
            state = self._sessions.task_state()
            rec = CheckpointRecord(
                checkpoint_id=checkpoint_id,
                label=label,
                state_revision=state.revision,
                event_index=len(self._events),  # exclusive boundary
                epoch=self.epoch,
                variables=state.values(),
                committed_nodes=self._workflows.committed_node_ids(),
                created_at=time.time())
            rec = self._checkpoints.add(rec)
            self._emit(EventKind.CHECKPOINT_COMMITTED,
                       {"checkpoint_id": checkpoint_id, "label": label,
                        "state_revision": rec.state_revision,
                        "event_index": rec.event_index},
                       correlation_id or checkpoint_id)
            return rec

    # ══ governance patches ═══════════════════════════════════════════════
    def apply_local_patch(self, patch: LocalPatch) -> dict[str, Any]:
        """Local adjustment: variables + not-yet-committed node contracts
        only. Topology and terminal intent are structurally out of reach
        here. Bumps the epoch: in-flight work predates the adjustment."""
        if not isinstance(patch, LocalPatch):
            raise PatchSemanticsError(
                "apply_local_patch accepts LocalPatch only")
        with self._lock:
            state = self._sessions.task_state()
            unknown = [u.semantic_key for u in patch.variable_updates
                       if state.variable(u.semantic_key) is None]
            if unknown:
                raise PatchSemanticsError(
                    f"LocalPatch introduces unknown variables {unknown}; "
                    "adding variables is a scope change — use GoalPatch")
            if patch.variable_updates:
                new_vars = tuple(
                    v.with_value(u.new_value)
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

        ALWAYS bumps the epoch and reports requires_replan=True — with no
        ``new_graph`` the Task Architect MUST re-plan the uncommitted
        future before execution continues; with one, committed nodes are
        carried verbatim (invariant 3) and only the future is replaced.
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
            graph_revision = None
            if new_graph is not None:
                installed = self._workflows.replace_future(new_graph, epoch=epoch)
                graph_revision = installed.revision
            if new_schema is not None:
                self._projections.set_schema(new_schema)
            self._emit(EventKind.PLAN_PATCHED,
                       {"patch_id": patch.patch_id, "patch_class": "goal",
                        "intent_changed": intent_changed,
                        "graph_revision": graph_revision,
                        "requires_replan": True, "epoch": epoch,
                        "rationale": patch.rationale},
                       patch.correlation_id or patch.patch_id)
            self._refresh_projection_data()
            return {"epoch": epoch, "requires_replan": True,
                    "intent_changed": intent_changed,
                    "graph_revision": graph_revision}

    def request_compensation(self, patch: CompensationPatch) -> CompensationPlan:
        """Validate a CompensationPatch against the kernel's OWN recorded
        checkpoint history (invariant 6) and produce the reversion plan
        the runtime must execute through the real action path."""
        if not isinstance(patch, CompensationPatch):
            raise PatchSemanticsError(
                "request_compensation accepts CompensationPatch only")
        with self._lock:
            rec = self._checkpoints.get(patch.target_checkpoint_id)
            fabricated = {k: v for k, v in patch.observed_before.items()
                          if k not in rec.variables or rec.variables[k] != v}
            if fabricated:
                raise CompensationMismatchError(
                    f"CompensationPatch 'before' values not grounded in the "
                    f"recorded checkpoint {rec.checkpoint_id!r}: {sorted(fabricated)}")
            current = self._sessions.task_state().values()
            entries = tuple(
                CompensationEntry(semantic_key=k, from_value=current.get(k),
                                  to_value=target)
                for k, target in rec.variables.items()
                if current.get(k) != target)
            epoch = self._bump_epoch_locked()
            self._comp_seq += 1
            plan = CompensationPlan(
                plan_id=f"comp_{self._comp_seq:05d}",
                target_checkpoint_id=rec.checkpoint_id,
                entries=entries, epoch=epoch, created_at=time.time())
            self._comp_plans[plan.plan_id] = plan
            self._emit(EventKind.COMPENSATION_REQUESTED,
                       {"patch_id": patch.patch_id, "plan_id": plan.plan_id,
                        "target_checkpoint_id": rec.checkpoint_id,
                        "entries": [{"semantic_key": e.semantic_key,
                                     "from": e.from_value, "to": e.to_value}
                                    for e in entries],
                        "epoch": epoch},
                       patch.correlation_id or patch.patch_id)
            return plan

    def record_compensation_result(
            self, plan_id: str, applied: bool, *,
            observed_values: dict[str, Any] | None = None,
            detail: str = "") -> None:
        """Land the outcome of a compensation execution.

        ``applied=True`` REQUIRES freshly observed values — the kernel
        records what reality shows after the compensation actions, never
        an assumed echo of the plan (mental-model §3.5: independent
        verification, honest reversibility). Nodes committed after the
        target checkpoint are marked COMPENSATED.
        """
        with self._lock:
            plan = self._comp_plans.get(plan_id)
            if plan is None:
                raise ValidationError(f"unknown compensation plan {plan_id!r}")
            if applied:
                if observed_values is None:
                    raise ValidationError(
                        "applied compensation requires freshly observed values")
                self.apply_observation(
                    {k: observed_values[k] for k in observed_values
                     if self._sessions.task_state().variable(k) is not None},
                    correlation_id=plan_id)
                rec = self._checkpoints.get(plan.target_checkpoint_id)
                kept = set(rec.committed_nodes)
                for nid in self._workflows.committed_node_ids():
                    if nid not in kept:
                        self._workflows.set_status(nid, NodeStatus.COMPENSATED)
                self._emit(EventKind.COMPENSATION_APPLIED,
                           {"plan_id": plan_id, "detail": detail,
                            "observed": dict(observed_values)}, plan_id)
            else:
                self._emit(EventKind.COMPENSATION_FAILED,
                           {"plan_id": plan_id, "detail": detail}, plan_id)
            self._refresh_projection_data()

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
        """Projection data always mirrors the stores (single truth)."""
        state = self._sessions.task_state()
        wf = self._workflows.snapshot()
        node_status = {nid: st.value for nid, st in wf.statuses.items()}
        countable = [] if wf.graph is None else [
            n for n in wf.graph.nodes
            if n.kind in (NodeKind.ACTION, NodeKind.VERIFY, NodeKind.CHECKPOINT)]
        done = [n for n in countable
                if wf.statuses.get(n.node_id) is NodeStatus.COMMITTED]
        progress = (len(done) / len(countable)) if countable else 0.0
        self._projections.update_data(values=state.values(),
                                      node_status=node_status,
                                      progress=progress)
