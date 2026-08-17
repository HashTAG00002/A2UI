"""taskvm.runtime.compensation — real-GUI rollback execution (runtime.md §7).

Rollback is NOT a DB snapshot restore. The kernel built the
``CompensationPlan`` from its OWN committed action history (before/after
recorded at action time); the runtime's job is to land each reversible entry
through the SAME real execution path as forward work — CUA predicts one
atomic GUI action, the substrate acts, a fresh observation is taken, and E
judges whether reality returned to ``entry.to_observed``.

Ownership: the runtime NEVER generates a ``CompensationPatch``/plan (that is
governance + kernel). It never touches ``plan.uncompensatable`` (IRREVERSIBLE
work the kernel honestly excluded). It builds ``CompensationResult.for_plan``
— outcomes can only name real plan entries — and hands the disposition back
to ``kernel.record_compensation_result``. A stale plan (epoch bumped by
governance mid-execution) lands as ``"discarded"``, never a fake success.

Call accounting (runtime.md §5): compensation CUA calls are recorded with
``is_repair=False`` (they are compensation work, not verifier-fail repairs)
and count against the SAME task-level ``max_model_calls_per_task`` budget as
forward work — the executor is handed the runtime's call count and stops
honestly when the ceiling is reached.
"""
from __future__ import annotations

from typing import Any

from taskvm.domain.patch import CompensationPlan
from taskvm.domain.results import CompensationEntryResult, CompensationResult
from taskvm.domain.state import TaskVariable
from taskvm.substrate import IrreversibleAction

from taskvm.runtime.config import RuntimeBudgets
from taskvm.runtime.ports import (
    CallLedger, CUADecisionKind, CUAGoalSerializer, CUAModel, ModelCallRecord,
    MODEL_ROLE_CUA, ObservationExtractor, RuntimeEvent, RuntimeEventKind,
)
from taskvm.runtime.sync import StructureInvalidation, SurfaceSync


class CompensationExecutor:
    """Executes one ``CompensationPlan`` through the real CUA + substrate.
    Forward autonomy is blocked by the kernel while a plan is pending, so the
    executor is the only thing driving the substrate in this window."""

    def __init__(self, kernel, substrate, sync: SurfaceSync,
                 cua_model: CUAModel, serializer: CUAGoalSerializer,
                 extractor: ObservationExtractor, ledger: CallLedger,
                 budgets: RuntimeBudgets) -> None:
        self._kernel = kernel
        self._substrate = substrate
        self._sync = sync
        self._cua = cua_model
        self._ser = serializer
        self._ext = extractor
        self._ledger = ledger
        self._budgets = budgets
        self._events: list[RuntimeEvent] = []
        self._calls = 0

    @property
    def events(self) -> list[RuntimeEvent]:
        return self._events

    def consume_calls(self) -> int:
        """CUA calls made by the last ``execute`` — the runtime folds them
        into its task-level budget counter."""
        used, self._calls = self._calls, 0
        return used

    def execute(self, plan: CompensationPlan, *,
                surface_for_entry: dict[str, str] | None = None,
                model: str | None = None,
                model_calls_base: int = 0) -> str:
        """Land every reversible entry; return the kernel disposition
        (``complete`` / ``partial`` / ``failed`` / ``discarded``).

        A-01: ``surface_for_entry`` maps ``node_id → surface_id`` — each
        compensation entry lands on the surface that owns its binding (the
        runtime resolved it from the variable's evidence handle). Entries
        WITHOUT a mapping are honestly not-compensated: a routing failure
        must never become a wrong-surface GUI write."""
        self._events = []
        self._calls = 0
        surface_for_entry = surface_for_entry or {}
        variables = {v.semantic_key: v
                     for v in self._kernel.task_state().variables}
        labels = {v.semantic_key: v.label for v in variables.values()}
        outcomes: list[CompensationEntryResult] = []
        for entry in plan.entries:
            sid = surface_for_entry.get(entry.node_id)
            if sid is None:
                outcomes.append(CompensationEntryResult(
                    node_id=entry.node_id, semantic_key=entry.semantic_key,
                    final_observed=entry.from_observed, compensated=False))
                self._events.append(RuntimeEvent(
                    kind=RuntimeEventKind.COMPENSATION_ENTRY,
                    epoch=plan.epoch, node_id=entry.node_id,
                    detail=(f"{entry.semantic_key}=not-compensated "
                            "(no resolvable surface binding; A-01)"),
                    payload={"semantic_key": entry.semantic_key,
                             "final_observed": entry.from_observed}))
                continue
            outcome = self._execute_entry(
                plan, entry, sid, variables, labels, model,
                model_calls_base)
            outcomes.append(outcome)
            self._events.append(RuntimeEvent(
                kind=RuntimeEventKind.COMPENSATION_ENTRY, epoch=plan.epoch,
                node_id=entry.node_id, surface_id=sid,
                detail=f"{entry.semantic_key}="
                       f"{'compensated' if outcome.compensated else 'not-compensated'}",
                payload={"semantic_key": entry.semantic_key,
                         "final_observed": outcome.final_observed}))
        result = CompensationResult.for_plan(
            plan, epoch=plan.epoch, outcomes=outcomes,
            detail=(f"executed {len(outcomes)} entries "
                    f"({sum(1 for s in surface_for_entry.values())} routed"))
        return self._kernel.record_compensation_result(plan.plan_id, result)

    def _execute_entry(self, plan: CompensationPlan, entry,
                       surface_id: str, variables, labels, model,
                       model_calls_base: int) -> CompensationEntryResult:
        goal = self._ser.compensation_goal(entry, labels)
        target = entry.to_observed
        final_observed: Any = entry.from_observed
        invalid = 0
        for attempt in range(1, self._budgets.max_actions_per_contract + 1):
            # stale plan: governance bumped the epoch mid-compensation → stop
            # acting; record_compensation_result will return "discarded".
            if self._kernel.epoch != plan.epoch:
                return CompensationEntryResult(
                    node_id=entry.node_id, semantic_key=entry.semantic_key,
                    final_observed=final_observed, compensated=False)
            if not self._budgets.within_model_budget(
                    model_calls_base + self._calls):
                # task-level ceiling reached: stop honestly, do not fake
                # completion for the remaining entries (they stay absent —
                # partial coverage is the kernel's honest disposition input)
                return CompensationEntryResult(
                    node_id=entry.node_id, semantic_key=entry.semantic_key,
                    final_observed=final_observed, compensated=False)
            obs = self._sync.observe_active(surface_id)
            try:
                decision = self._cua.predict_action(
                    goal=goal, observation=obs, labels=labels,
                    attempt=attempt, model=model)
            except Exception as e:          # provider / parse failure — same
                # honest accounting as the forward loop (runtime.md §5):
                # a model call that produced NO usable prediction, bounded
                # by the small invalid ceiling, never a silent crash.
                # A-13: the adapter may already own the row
                # (``records_own_ledger``) — never double-count.
                invalid += 1
                self._calls += 1
                if not getattr(self._cua, "records_own_ledger", False):
                    self._ledger.record(ModelCallRecord(
                        role=MODEL_ROLE_CUA,
                        purpose=f"compensation_{entry.node_id}_invalid{invalid}",
                        model=model or "", ok=False, is_repair=False,
                        error=str(e)[:200],
                        revision=self._kernel.task_state().revision,
                        node_id=entry.node_id, attempt=invalid))
                if invalid >= self._budgets.max_invalid_predictions_per_contract:
                    break
                continue
            self._record_call(decision, entry.node_id, model)
            if decision.kind is CUADecisionKind.DONE:
                final_observed = self._read_value(obs, entry.semantic_key,
                                                  variables)
                break
            if decision.kind is CUADecisionKind.FAIL:
                break
            # ACT — one real GUI gesture, then re-observe
            if self._kernel.epoch != plan.epoch:
                return CompensationEntryResult(
                    node_id=entry.node_id, semantic_key=entry.semantic_key,
                    final_observed=final_observed, compensated=False)
            try:
                self._substrate.act(surface_id, decision.action,
                                    epoch=str(self._kernel.epoch))
            except IrreversibleAction:
                # honest: the substrate has no real-UI way to undo → not
                # compensated, never a hidden DB write (runtime.md §7).
                return CompensationEntryResult(
                    node_id=entry.node_id, semantic_key=entry.semantic_key,
                    final_observed=final_observed, compensated=False)
            obs_after = self._sync.observe_active(surface_id)
            final_observed = self._read_value(obs_after, entry.semantic_key,
                                              variables)
            if final_observed == target:
                return CompensationEntryResult(
                    node_id=entry.node_id, semantic_key=entry.semantic_key,
                    final_observed=final_observed, compensated=True)
        compensated = (final_observed == target)
        return CompensationEntryResult(
            node_id=entry.node_id, semantic_key=entry.semantic_key,
            final_observed=final_observed, compensated=compensated)

    def _read_value(self, observation, semantic_key: str,
                    variables) -> Any:
        """Deterministic extraction of one variable's visible value, for the
        compensation completion check (observed == to_observed). 0 model
        calls. A ``StructureInvalidation`` ⇒ the value can't be read honestly
        ⇒ not compensated (the runtime does not fabricate a value)."""
        try:
            values = self._ext.extract(observation, variables)
        except StructureInvalidation:
            return None
        for ov in values:
            if ov.semantic_key == semantic_key:
                return ov.value
        return None

    def _record_call(self, decision, node_id: str, model: str | None) -> None:
        ok = decision.kind is not CUADecisionKind.FAIL
        self._calls += 1
        # A-13 single-owner accounting: annotate the adapter's row when the
        # decision carries its request_id; fakes keep the runtime as owner.
        if decision.request_id and callable(
                getattr(self._ledger, "annotate", None)):
            self._ledger.annotate(
                decision.request_id, purpose=f"compensation_{node_id}",
                node_id=node_id, attempt=self._calls, is_repair=False,
                revision=self._kernel.task_state().revision)
            return
        self._ledger.record(ModelCallRecord(
            role=MODEL_ROLE_CUA, purpose=f"compensation_{node_id}",
            model=model or "", ok=ok, is_repair=False,
            revision=self._kernel.task_state().revision,
            node_id=node_id))
