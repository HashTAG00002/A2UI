"""GovernanceService — the unified governance entry.

One ``handle(event)`` routes the six governance events onto the kernel
facade (and, for GoalPatch ONLY, one Task Architect recomposition). The
service is a ROUTER, not a validator: content legality belongs to the
domain constructors and the kernel's own patch state machine — this layer
never re-proves it.

Model-call contract per event class (architect contract §5, test-pinned):

- LocalPatchRequested       → 0 compiler / 0 architect calls
- GoalPatchRequested        → 0 compiler / 1 architect call
  (apply_goal_patch invalidates the future and BLOCKS execution; the
  architect recomposes the affected future; ``recompose`` atomically
  closes the transition and unblocks. Committed history is carried
  verbatim by the kernel — never re-executed, never re-seeded.)
- RollbackRequested         → 0 / 0 — the CompensationPlan derives from the
  kernel's own committed action history; NO model invents reversal copy.
- Pause/Resume/Conflict     → 0 / 0.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from taskvm.architect.architect import RecomposeProposal, TaskArchitect
from taskvm.architect.compiler import CompilerResult, StateCompiler
from taskvm.architect.observation import CompilerObservationView
from taskvm.architect.port import ModelCallLedger
from taskvm.domain.errors import ValidationError
from taskvm.domain.intent import TaskIntent
from taskvm.domain.patch import (
    CompensationPatch, CompensationPlan, GoalPatch, LocalPatch,
    VariableUpdate,
)
from taskvm.domain.state import TaskState
from taskvm.kernel import TaskVMKernel

from taskvm.governance.events import (
    ConflictResolutionRequested, GoalPatchRequested, GovernanceEvent,
    LocalPatchRequested, PauseRequested, ResumeRequested,
    RollbackRequested,
)


class GoalRecomposeFailed(ValidationError):
    """The architect could not close the GoalPatch; execution stays BLOCKED.

    The kernel's ``pending_recompose`` remains set — the failure is honest
    and inspectable; a later :meth:`GovernanceService.retry_goal_recompose`
    (or a fresh GoalPatch) can close the transition."""


@dataclass(frozen=True)
class GovernanceOutcome:
    """What happened, at the governance grain (UI/audit facing)."""

    handled: str                      # the event class name
    epoch: int
    detail: dict[str, Any] = field(default_factory=dict)
    compensation_plan: CompensationPlan | None = None


@dataclass(frozen=True)
class BootstrapResult:
    """The initialization artifact pair: compiled world + architecture."""

    compiled: CompilerResult
    architecture: Any                 # taskvm.domain.TaskArchitecture


class GovernanceService:
    """One task session's governance router (kernel + optional architect)."""

    def __init__(self, kernel: TaskVMKernel, *,
                 architect: TaskArchitect | None = None,
                 compiler: StateCompiler | None = None,
                 ledger: ModelCallLedger | None = None) -> None:
        self._kernel = kernel
        self._architect = architect
        self._compiler = compiler
        self._ledger = ledger
        self._seq = 0

    # ── initialization (composition root may also drive kernel directly) ─
    def initialize(self, intent: TaskIntent,
                   compiled: CompilerResult,
                   architecture: Any) -> BootstrapResult:
        """Install the initial composition: ONE compiler call + ONE
        architect call must have produced ``compiled``/``architecture``
        (drive them via :meth:`bootstrap`); this lands them one-shot."""
        self._kernel.init_task_state(architecture.variables)
        if architecture.graph is not None:
            self._kernel.set_plan(architecture.graph, architecture.schema)
        return BootstrapResult(compiled=compiled, architecture=architecture)

    def bootstrap(self, view: CompilerObservationView) -> BootstrapResult:
        """Initial goal → compiled world → coherent architecture → kernel.

        Exactly two high-level model calls (one State Compiler, one Task
        Architect — each with its own bounded repair), then a one-shot
        install. This is the ONLY production initialization path; there is
        no fixture/task_id variant."""
        if self._compiler is None or self._architect is None:
            raise ValidationError(
                "bootstrap requires a StateCompiler and a TaskArchitect")
        intent = self._kernel.task_state().intent
        compiled = self._compiler.compile(
            view, intent, revision=view.revision,
            purpose="initial_compile")
        architecture = self._architect.compose(intent, compiled)
        return self.initialize(intent, compiled, architecture)

    # ── the unified entry ───────────────────────────────────────────────
    def handle(self, event: GovernanceEvent) -> GovernanceOutcome:
        if isinstance(event, PauseRequested):
            return self._pause(event)
        if isinstance(event, ResumeRequested):
            return self._resume(event)
        if isinstance(event, LocalPatchRequested):
            return self._local_patch(event)
        if isinstance(event, GoalPatchRequested):
            return self._goal_patch(event)
        if isinstance(event, RollbackRequested):
            return self._rollback(event)
        if isinstance(event, ConflictResolutionRequested):
            return self._conflict(event)
        raise ValidationError(f"unknown governance event {event!r}")

    # ── event handlers ──────────────────────────────────────────────────
    def _pause(self, event: PauseRequested) -> GovernanceOutcome:
        self._kernel.request_governance(
            "pause", event.rationale, correlation_id=event.correlation_id)
        return GovernanceOutcome(handled="PauseRequested",
                                 epoch=self._kernel.epoch,
                                 detail={"paused": True})

    def _resume(self, event: ResumeRequested) -> GovernanceOutcome:
        self._kernel.request_governance(
            "resume", event.rationale, correlation_id=event.correlation_id)
        return GovernanceOutcome(handled="ResumeRequested",
                                 epoch=self._kernel.epoch,
                                 detail={"resumed": True})

    def _local_patch(self, event: LocalPatchRequested) -> GovernanceOutcome:
        """0 compiler / 0 architect calls — the kernel retargets the
        uncommitted contracts deterministically and bumps the epoch."""
        patch = LocalPatch(
            patch_id=self._next_id("lp"),
            variable_updates=tuple(
                VariableUpdate(semantic_key=k, new_value=v)
                for k, v in event.updates.items()),
            rationale=event.rationale,
            correlation_id=event.correlation_id)
        result = self._kernel.apply_local_patch(patch)
        return GovernanceOutcome(
            handled="LocalPatchRequested", epoch=result["epoch"],
            detail={"retargeted_nodes": result["retargeted_nodes"],
                    "requires_replan": False,
                    "architect_calls": 0, "compiler_calls": 0})

    def _goal_patch(self, event: GoalPatchRequested) -> GovernanceOutcome:
        """apply_goal_patch (invalidate + block) → ONE architect recompose
        → recompose (atomic close + unblock). Committed history survives."""
        patch = GoalPatch(patch_id=self._next_id("gp"),
                          new_intent=event.new_intent,
                          rationale=event.rationale,
                          correlation_id=event.correlation_id)
        applied = self._kernel.apply_goal_patch(patch)
        proposal = self._recompose_proposal(event)
        state = self._kernel.recompose(
            proposal.variables, reason=proposal.reason,
            new_graph=proposal.graph, new_schema=proposal.schema,
            correlation_id=event.correlation_id)
        return GovernanceOutcome(
            handled="GoalPatchRequested", epoch=self._kernel.epoch,
            detail={"invalidated_node_ids": applied["invalidated_node_ids"],
                    "carried_history_nodes": len(proposal.carried_node_ids),
                    "state_revision": state.revision,
                    "architect_calls": 1, "compiler_calls": 0})

    def _rollback(self, event: RollbackRequested) -> GovernanceOutcome:
        """History-driven compensation: the kernel builds the reversion
        plan from its OWN committed action history. No model call, no
        invented reversal copy."""
        patch = CompensationPatch(
            patch_id=self._next_id("cp"),
            target_checkpoint_id=event.target_checkpoint_id,
            rationale=event.rationale,
            correlation_id=event.correlation_id)
        plan = self._kernel.request_compensation(patch)
        return GovernanceOutcome(
            handled="RollbackRequested", epoch=self._kernel.epoch,
            detail={"entries": len(plan.entries),
                    "uncompensatable": len(plan.uncompensatable),
                    "requires_recompose": plan.requires_recompose,
                    "architect_calls": 0, "compiler_calls": 0},
            compensation_plan=plan)

    def _conflict(self,
                  event: ConflictResolutionRequested) -> GovernanceOutcome:
        cid = self._kernel.record_conflict(
            event.description, event.semantic_keys,
            correlation_id=event.correlation_id)
        self._kernel.resolve_conflict(
            event.resolution, correlation_id=cid)
        return GovernanceOutcome(handled="ConflictResolutionRequested",
                                 epoch=self._kernel.epoch,
                                 detail={"conflict_id": cid})

    # ── GoalPatch closure helpers ───────────────────────────────────────
    def _recompose_proposal(self, event: GovernanceEvent,
                            ) -> RecomposeProposal:
        if self._architect is None:
            raise ValidationError(
                "GoalPatch requires a Task Architect (execution stays "
                f"blocked; kernel.pending_recompose="
                f"{self._kernel.pending_recompose!r})")
        try:
            return self._architect.recompose_future(
                intent=(self._kernel.task_state().intent),
                variables=self._kernel.task_state().variables,
                snapshot=self._kernel.workflow(),
                reason=(event.rationale or "goal patch"))
        except ValidationError as e:
            raise GoalRecomposeFailed(
                f"architect could not recompose the affected future ({e}); "
                f"execution stays BLOCKED until a successful "
                f"retry_goal_recompose() — no fallback, no silent reseed"
            ) from e

    def retry_goal_recompose(self, *, reason: str = "goal retry") -> TaskState:
        """Close a still-pending GoalPatch transition (e.g. after the model
        endpoint recovered). Fails honestly if nothing is pending."""
        if self._kernel.pending_recompose is None:
            raise ValidationError("no pending recompose to retry")
        proposal = self._recompose_proposal_for_reason(reason)
        return self._kernel.recompose(
            proposal.variables, reason=proposal.reason,
            new_graph=proposal.graph, new_schema=proposal.schema)

    def _recompose_proposal_for_reason(self, reason: str) -> RecomposeProposal:
        # shared by retry_goal_recompose (event-less closure)
        if self._architect is None:
            raise ValidationError(
                "closing a GoalPatch requires a Task Architect")
        return self._architect.recompose_future(
            intent=self._kernel.task_state().intent,
            variables=self._kernel.task_state().variables,
            snapshot=self._kernel.workflow(), reason=reason)

    # ── structural drift (slow-path recompile; legitimate re-invoke) ────
    def recompile_structure(self, view: CompilerObservationView, *,
                            reason: str) -> GovernanceOutcome:
        """Structural binding failure → incremental State Compiler call;
        if the variable STRUCTURE changed, ONE architect recomposition
        follows (kernel recompose keeps committed history). Value-only
        changes never reach this method — they are ``apply_observation``."""
        if self._compiler is None:
            raise ValidationError("recompile_structure requires a compiler")
        intent = self._kernel.task_state().intent
        compiled = self._compiler.compile(
            view, intent, revision=view.revision,
            prior_state=self._kernel.task_state(),
            purpose="incremental_recompile")
        struct_changed = not self._same_structure(self._kernel.task_state(),
                                                  compiled)
        architect_calls = 0
        if struct_changed:
            if self._architect is None:
                raise ValidationError(
                    "structure drift changed the variable set; closing it "
                    "requires a Task Architect")
            proposal = self._architect.recompose_future(
                intent=intent, variables=compiled,
                snapshot=self._kernel.workflow(),
                reason=f"structure drift: {reason}",
                purpose="drift_recompose")
            self._kernel.recompose(
                proposal.variables, reason=f"structure drift: {reason}",
                new_graph=proposal.graph, new_schema=proposal.schema)
            architect_calls = 1
        return GovernanceOutcome(
            handled="StructuralDrift", epoch=self._kernel.epoch,
            detail={"structure_changed": struct_changed,
                    "compiler_calls": 1, "architect_calls": architect_calls})

    @staticmethod
    def _same_structure(state: TaskState,
                        compiled: CompilerResult) -> bool:
        cur = {v.semantic_key: (v.label, v.value_type, v.mutability)
               for v in state.variables}
        new = {v.semantic_key: (v.label, v.value_type, v.mutability)
               for v in compiled.variables}
        return cur == new

    # ── internals ───────────────────────────────────────────────────────
    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}:{self._seq:05d}"
