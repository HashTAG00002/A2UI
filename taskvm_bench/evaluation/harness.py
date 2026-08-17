"""taskvm_bench.evaluation.harness — the system conditions (part 1: contracts,
direct-cua, planner-cua; TaskVMHarness lives here too, appended below).

One condition = one harness class driving the SAME world through the
SAME ``WorldSubstrate`` with the SAME deterministic capability model;
what differs is the STRUCTURE around that capability (handoff 07).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from taskvm.architect.architect import TaskArchitect
from taskvm.architect.compiler import StateCompiler
from taskvm.architect.http_port import HttpModelPort
from taskvm.architect.observation import (
    CompilerObservationView, VisibleRegion,
)
from taskvm.architect.port import ModelCallLedger
from taskvm.architect.serializer import ActionContractSerializer
from taskvm_bench.benchmark.registry import Condition
from taskvm.domain.intent import TaskIntent
from taskvm.domain.state import (
    ObservedValue, SurfaceEvidence, SurfaceHandle,
)
from taskvm.governance.events import (
    ConflictResolutionRequested, GoalPatchRequested, LocalPatchRequested,
    RollbackRequested,
)
from taskvm.governance.service import GovernanceService
from taskvm.kernel import TaskVMKernel
from taskvm.runtime import (
    AutonomyRuntime, CUADecision, CUADecisionKind, RuntimeBudgets,
)
from taskvm.runtime.sync import StructureInvalidation
from taskvm.substrate import SubstrateSession
from taskvm.verifier.visible import VisibleVerifier

from taskvm.workspace_ui.composition import (
    HttpCUAModel, bootstrap_real_full,
)
from taskvm_bench.evaluation.actors import (
    TemplateCUA, TemplateModelPort, parse_goal_program, parse_visible_kv,
)
from taskvm_bench.evaluation.world import ExternalEvent
from taskvm_bench.benchmark.schema import InjectionKind

__all__ = [
    "TrialBudget", "HarnessOutcome", "DirectCUAHarness", "PlannerCUAHarness",
    "TaskVMHarness", "make_harness", "WorldExtractor",
    "DirectCUARealHarness", "PlannerCUARealHarness",
    "RealFullHarness", "RealCUAOnlyHarness",
]


@dataclass(frozen=True)
class TrialBudget:
    """The SAME budget object is handed to every condition (fairness)."""

    max_turns: int = 64            # direct/planner loop turns
    max_rounds: int = 24           # taskvm governance rounds
    max_actions_per_contract: int = 8
    max_invalid_predictions: int = 4
    max_repairs_per_contract: int = 1
    max_model_calls_per_task: int = 512

    def runtime_budgets(self) -> RuntimeBudgets:
        return RuntimeBudgets(
            max_actions_per_contract=self.max_actions_per_contract,
            max_invalid_predictions_per_contract=(
                self.max_invalid_predictions),
            max_repairs_per_contract=self.max_repairs_per_contract,
            max_model_calls_per_task=self.max_model_calls_per_task,
            wall_clock_budget=None,
        )


@dataclass
class HarnessOutcome:
    """What one harness reports for one trial (pre-oracle, system-side)."""

    stop_reason: str
    model_calls_by_role: dict[str, int] = field(default_factory=dict)
    #: role -> (prompt_tokens_sum, completion_tokens_sum). Populated only
    #: where a token meter exists (the architect-side fakes report
    #: deterministic estimates; the template CUA layer has no meter and
    #: honestly stays empty).
    model_tokens_by_role: dict[str, tuple[int, int]] = field(
        default_factory=dict)
    gui_actions: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


class _CUACounter:
    """Wraps ANY CUAModel (template or real) counting ACT decisions as
    GUI actions and recording a compact per-call trace (no observation
    bodies — traces stay small and leak-free)."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.gui_actions = 0
        self.trace: list[dict[str, Any]] = []

    @property
    def records_own_ledger(self) -> bool:
        """A-13 transparency: the wrapper must not hide the inner
        adapter's ledger-ownership declaration. When the inner adapter
        owns its rows (``HttpCUAModel``), the runtime's exception path
        checks THIS attribute on the wrapper — without the forwarding
        it would append a duplicate row (1 request = 2 rows) with an
        unpinned model id."""
        return bool(getattr(self.inner, "records_own_ledger", False))

    def predict_action(self, *, goal: str, observation,
                       labels: Mapping[str, str] | None = None,
                       attempt: int = 1,
                       model: str | None = None) -> CUADecision:
        dec = self.inner.predict_action(
            goal=goal, observation=observation, labels=labels,
            attempt=attempt, model=model)
        entry: dict[str, Any] = {"kind": dec.kind.value, "attempt": attempt}
        if dec.kind is CUADecisionKind.ACT and dec.action is not None:
            entry["gesture"] = dec.action.kind
            if dec.action.text:
                entry["text"] = dec.action.text
            self.gui_actions += 1
        elif dec.reason:
            entry["reason"] = dec.reason
        self.trace.append(entry)
        return dec


class _TextGoalMixin:
    """direct/planner 共享的外部事件→goal 文本折叠策略：重定向替换
    goal，其余手势作为用户消息追加（这两个条件没有结构化治理面，
    用户手势只能以自然语言到达——信息等价，处理能力不同）。"""

    _pending: list[ExternalEvent]

    def route_external(self, ev: ExternalEvent) -> None:
        self._pending.append(ev)

    def _fold_externals(self, goal_text: str,
                        trace: list[dict[str, Any]]) -> str:
        while self._pending:
            ev = self._pending.pop(0)
            trace.append({"event": "external", "kind": ev.kind.value})
            if ev.kind.value == "goal_patch" and "goal" in ev.payload:
                goal_text = str(ev.payload["goal"])
            else:
                note = str(ev.payload.get("note", ev.kind.value))
                goal_text = f"{goal_text}\nUser message: {note}"
        return goal_text


class DirectCUAHarness(_TextGoalMixin):
    """Condition ``direct-cua``: the bare CUA loop. No memory, no plan,
    no governance — it executes what it can parse and honestly fails on
    the rest."""

    condition = Condition.DIRECT_CUA

    def __init__(self) -> None:
        self._pending = []

    # ── capability seams (B-06): the real-model twin overrides these ──
    def _new_counter(self) -> _CUACounter:
        return _CUACounter(TemplateCUA())

    def _cua_calls(self, counter: _CUACounter) -> int:
        return len(counter.inner.calls)

    def run(self, substrate: SubstrateSession, goal: str, *,
            budget: TrialBudget) -> HarnessOutcome:
        counter = self._new_counter()
        sid = substrate.list_surfaces()[0].surface_id
        goal_text = goal
        trace: list[dict[str, Any]] = []
        stop = "budget"
        for _turn in range(budget.max_turns):
            goal_text = self._fold_externals(goal_text, trace)
            obs = substrate.observe(sid)
            dec = counter.predict_action(goal=goal_text, observation=obs)
            trace.extend(counter.trace)
            counter.trace.clear()
            if dec.kind is CUADecisionKind.DONE:
                stop = "done"
                break
            if dec.kind is CUADecisionKind.FAIL or dec.action is None:
                stop = "cua_fail"
                break
            receipt = substrate.act(sid, dec.action, epoch="direct-cua")
            trace.append({"event": "act", "status": receipt.status})
            if receipt.status != "ok":
                trace.append({"event": "rejected",
                              "detail": receipt.detail[:120]})
        return HarnessOutcome(
            stop_reason=stop,
            model_calls_by_role={"cua": self._cua_calls(counter)},
            gui_actions=counter.gui_actions, trace=trace,
            detail=f"direct-cua stopped: {stop}")


class PlannerCUAHarness(_TextGoalMixin):
    """Condition ``planner-cua``: a planner model re-emits ONE instruction
    per turn from the whole goal + whole screen; the CUA executes it.
    The planner has the global view (and one model call per turn) but no
    committed-state memory and no rollback topology."""

    condition = Condition.PLANNER_CUA

    def __init__(self) -> None:
        self._pending = []

    # ── capability seams (B-06): the real-model twin overrides these ──
    def _new_counter(self) -> _CUACounter:
        return _CUACounter(TemplateCUA())

    def _cua_calls(self, counter: _CUACounter) -> int:
        return len(counter.inner.calls)

    @staticmethod
    def _next_instruction(prog, current: Mapping[str, str]) -> str | None:
        """One planner decision: the next unmet target as a one-line
        instruction (repeat blocks stay one instruction until the until-
        condition holds). None = planner believes the goal is met."""
        if prog.repeat is not None:
            (gk, gv), (uk, uv) = prog.repeat
            if current.get(uk) != uv:
                return (f"Repeat: Set {gk} to {gv} "
                        f"until {uk} is {uv}.")
        for key, want in prog.sets:
            if current.get(key) != want:
                return f"Set {key} to {want}."
        return None

    def run(self, substrate: SubstrateSession, goal: str, *,
            budget: TrialBudget) -> HarnessOutcome:
        counter = self._new_counter()
        sid = substrate.list_surfaces()[0].surface_id
        goal_text = goal
        trace: list[dict[str, Any]] = []
        planner_calls = 0
        stop = "budget"
        for _turn in range(budget.max_turns):
            goal_text = self._fold_externals(goal_text, trace)
            obs = substrate.observe(sid)
            planner_calls += 1               # one planner model call/turn
            try:
                prog = parse_goal_program(goal_text)
            except ValueError:
                stop = "planner_parse_fail"
                break
            current = parse_visible_kv(obs.visible_text or "")
            instruction = self._next_instruction(prog, current)
            if instruction is None:
                # planner believes the goal holds; confirm via the CUA
                dec = counter.predict_action(goal=goal_text,
                                             observation=obs)
                trace.extend(counter.trace)
                counter.trace.clear()
                stop = ("done" if dec.kind is CUADecisionKind.DONE
                        else "planner_cua_disagree")
                break
            dec = counter.predict_action(goal=instruction, observation=obs)
            trace.extend(counter.trace)
            counter.trace.clear()
            if dec.kind is CUADecisionKind.DONE:
                continue          # already satisfied — next instruction
            if dec.kind is CUADecisionKind.FAIL or dec.action is None:
                stop = "cua_fail"
                break
            receipt = substrate.act(sid, dec.action, epoch="planner-cua")
            trace.append({"event": "act", "status": receipt.status})
            if receipt.status != "ok":
                trace.append({"event": "rejected",
                              "detail": receipt.detail[:120]})
        return HarnessOutcome(
            stop_reason=stop,
            model_calls_by_role={
                "planner": planner_calls,
                "cua": self._cua_calls(counter)},
            gui_actions=counter.gui_actions, trace=trace,
            detail=f"planner-cua stopped: {stop}")


class WorldExtractor:
    """The runtime's ObservationExtractor over k=v worlds: re-reads every
    known variable by its VISIBLE LABEL (the label is what is on screen;
    the semantic key may survive a relabel). Raises StructureInvalidation
    when a variable whose evidence is bound to THIS surface can no longer
    be recovered — that is the drift signal composition routes to the
    compiler slow path. Variables that simply do not appear on this
    surface (multi-surface distribution) are silently skipped, NOT
    reported as drift (a variable that was never here did not 'drift
    away'). 0 model calls."""

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, observation,
                variables: Mapping[str, Any]) -> tuple[ObservedValue, ...]:
        self.calls += 1
        cur = parse_visible_kv(observation.visible_text or "")
        obs_surface = (observation.surface.surface_id
                       if hasattr(observation, "surface") else None)
        out: list[ObservedValue] = []
        lost_targeted: list[str] = []
        for key, var in variables.items():
            label = getattr(var, "label", None) or key
            if label in cur:
                out.append(ObservedValue(
                    semantic_key=key, value=cur[label],
                    evidence=(SurfaceEvidence(
                        surface=SurfaceHandle(
                            handle_id=obs_surface or "unknown"),
                        visible_label=label,
                        observed_value=cur[label]),),
                    confidence=1.0))
            else:
                # Only flag as structural drift when the variable had
                # evidence bound to THIS surface (it was here and
                # disappeared). A variable that was never on this
                # surface is just multi-surface distribution — not drift.
                had_evidence_here = False
                for ev in getattr(var, "evidence", ()) or ():
                    ev_surf = getattr(ev, "surface", None)
                    ev_handle = getattr(ev_surf, "handle_id", None)
                    if ev_handle == obs_surface:
                        had_evidence_here = True
                        break
                if had_evidence_here:
                    desired = getattr(var, "desired", None)
                    observed = getattr(var, "observed", None)
                    if desired is not None and desired != observed:
                        lost_targeted.append(key)
        if lost_targeted:
            raise StructureInvalidation(
                "targeted variable(s) not visible on surface "
                f"{obs_surface!r}: "
                + ", ".join(sorted(lost_targeted)))
        return tuple(out)


class TaskVMHarness:
    """Conditions ``taskvm`` / ``taskvm-oracle-upper-bound`` /
    ``taskvm-no-verifier`` / ``taskvm-no-replan``.

    The full governance stack, driven in bounded rounds so external
    events interleave with forward autonomy exactly like a real
    composition would:

        runtime.run(small step budget) → drain governance events →
        heartbeat the inactive surfaces → execute any pending
        compensation plan → repeat.

    * GoalPatch       → GovernanceService (apply → recompose → unblock)
    * RollbackRequest → most recent committed checkpoint → compensation
      plan → real-GUI compensation through the SAME CUA/substrate
    * Conflict        → the rational-user resolution: accept the
      underlying reality and locally re-target the uncommitted future
    * StructureInvalidated → compiler slow path (fresh variables) →
      GoalPatch-shaped recompose of the uncommitted future

    Ablations: ``no_verifier`` swaps the verifier for one that equates
    CUA-done with verified; ``no_replan`` lets a GoalPatch leave the
    kernel blocked (the honest cost of ungoverned redirection).
    """

    def __init__(self, *, condition: Condition = Condition.TASKVM,
                 no_verifier: bool = False,
                 no_replan: bool = False,
                 oracle_spec=None) -> None:
        self.condition = condition
        self._no_verifier = no_verifier
        self._no_replan = no_replan
        self._oracle_spec = oracle_spec
        self._pending: list[ExternalEvent] = []
        self._comp_plan = None
        self._stop_note = ""
        self._runtime = None            # hot-interruption face

    def route_external(self, ev: ExternalEvent) -> None:
        """The world's event tap. PAUSE/RESUME are HOT: they apply the
        moment they arrive (a real interruption does not wait for a
        round boundary — that is what makes "actions after pause" a
        meaningful metric). Everything else queues for round-boundary
        governance routing."""
        if ev.kind is InjectionKind.PAUSE_RESUME and self._runtime is not None:
            self._apply_hot(ev)
            return
        self._pending.append(ev)

    def _apply_hot(self, ev: ExternalEvent) -> None:
        assert self._runtime is not None
        phase = str(ev.payload.get("phase", "pause"))
        if phase == "resume":
            self._runtime.request_resume()
        else:
            self._runtime.request_pause()
        self._hot_events.append({"event": "external",
                                 "kind": ev.kind.value,
                                 "phase": phase})

    # ── composition ────────────────────────────────────────────────────
    def _new_ledger(self) -> ModelCallLedger:
        """Ledger seam (B-06): the template default mints a fresh ledger
        per composition; the real-model twins return the ONE ledger the
        harness pinned at construction (single-owner accounting across
        compiler/architect/CUA rows)."""
        return ModelCallLedger()

    def _new_cua(self, ledger) -> _CUACounter:
        """Capability seam (B-06): the template default; the real-cua
        diagnostic twin swaps in the real ``HttpCUAModel`` over the SAME
        ledger (1 provider request = 1 ledger row holds everywhere)."""
        return _CUACounter(TemplateCUA())

    def _compose(self, substrate: SubstrateSession,
                 goal: str) -> tuple:
        """Assemble (ledger, port, compiler, architect, kernel, gov,
        runtime, cua-counter). Structurally duck-typed on purpose: the
        real-model twins (B-06) return HttpModelPort-backed objects from
        the SAME seam — only the port's identity differs."""
        ledger = self._new_ledger()
        port = TemplateModelPort()
        compiler = StateCompiler(port, ledger)
        architect = TaskArchitect(port, ledger)
        kernel = TaskVMKernel(session_id="benchmark-trial",
                              intent=TaskIntent(goal=goal))
        gov = GovernanceService(kernel, architect=architect,
                                compiler=compiler, ledger=ledger)
        sid = substrate.list_surfaces()[0].surface_id
        obs0 = substrate.observe(sid)
        view = CompilerObservationView(
            revision=1,
            regions=(VisibleRegion(
                surface_label=sid,
                visible_text=obs0.visible_text,
                structure_fingerprint=obs0.fingerprint),))
        gov.bootstrap(view)
        counter = self._new_cua(ledger)
        verifier = _NoVerifier() if self._no_verifier else (
            _OracleVerifier(self._oracle_spec)
            if self._oracle_spec is not None else VisibleVerifier())
        # ports.py: the architect ModelCallLedger is duck-typed compatible
        # with the runtime CallLedger protocol BY DESIGN (one unified call
        # report); the static checker cannot see across the twin
        # ModelCallRecord definitions, hence the explicit cast.
        from typing import cast as _cast
        from taskvm.runtime import CallLedger as _CallLedger
        runtime = AutonomyRuntime(
            kernel, substrate,
            cua_model=counter, serializer=ActionContractSerializer(),
            extractor=WorldExtractor(), verifier=verifier,
            ledger=_cast(_CallLedger, ledger),
            budgets=None)
        return ledger, port, compiler, architect, kernel, gov, runtime, counter

    # ── the trial ──────────────────────────────────────────────────────
    def run(self, substrate: SubstrateSession, goal: str, *,
            budget: TrialBudget) -> HarnessOutcome:
        (ledger, port, compiler, architect, kernel, gov, runtime,
         counter) = self._compose(substrate, goal)
        self._runtime = runtime
        self._hot_events: list[dict[str, Any]] = []
        runtime._budgets = budget.runtime_budgets()
        runtime._comp._budgets = budget.runtime_budgets()
        trace: list[dict[str, Any]] = list(getattr(self, "_hot_events", []))
        stop = "budget"
        heartbeats = 0
        model_heartbeats = 0
        consumed_events = len(runtime.runtime_events())
        for _round in range(budget.max_rounds):
            stop = runtime.run(step_budget=1)
            self._drain(gov, runtime, kernel, substrate, trace)
            # Active-surface signals (STRUCTURE_INVALIDATED / 
            # SURFACE_CONFLICT published by the runtime itself) are
            # consumed INCREMENTALLY from the runtime event log — the
            # same routing ``poll_inactive_surfaces`` gives the inactive
            # faces; without this, an active-surface drift lands as a bare
            # node failure and forward autonomy dies with no_ready_work.
            evs = runtime.runtime_events()
            for rdev in evs[consumed_events:]:
                if rdev.kind.value in ("structure_invalidated",
                                       "surface_conflict"):
                    self._on_runtime_event(rdev, gov, kernel, substrate,
                                           trace)
            consumed_events = len(evs)
            # sync-cost accounting: every inactive-surface heartbeat is a
            # 0-model-call fast path by construction (WorldExtractor);
            # the ledger differential proves it with numbers (handoff
            # §同步成本: “大多数 heartbeat 是否无需模型？”)
            heartbeats += 1
            calls_before = sum(ledger.counts_by_role().values())
            for rdev in runtime.poll_inactive_surfaces():
                self._on_runtime_event(rdev, gov, kernel, substrate,
                                       trace)
            consumed_events = len(runtime.runtime_events())
            calls_after = sum(ledger.counts_by_role().values())
            if calls_after > calls_before:
                model_heartbeats += 1
            if self._comp_plan is not None:
                plan, self._comp_plan = self._comp_plan, None
                disp = runtime.execute_compensation(plan)
                trace.append({"event": "compensation",
                              "disposition": disp})
                continue
            if stop == "done":
                break
            if stop in ("blocked", "pending_recompose", "paused",
                        "budget_exhausted", "escalated", "no_plan",
                        "no_ready_work"):
                # one grace round: events may have unblocked us; if the
                # next round hits the same wall we stop honestly
                if self._grace_round(kernel, stop, trace):
                    continue
                break
        runtime_events = [
            {"event": e.kind.value, "detail": e.detail[:120]}
            for e in runtime.runtime_events()]
        trace.extend(runtime_events)
        return HarnessOutcome(
            stop_reason=stop,
            model_calls_by_role=dict(ledger.counts_by_role()),
            model_tokens_by_role=dict(ledger.tokens_by_role()),
            gui_actions=counter.gui_actions, trace=trace,
            detail=(f"taskvm stopped: {stop}"
                    + (f"; {self._stop_note}" if self._stop_note else "")),
            extras={"kernel_epoch": kernel.epoch,
                    "heartbeats": heartbeats,
                    "model_heartbeats": model_heartbeats,
                    "observed_plane": self._observed_plane(kernel),
                    "goalpatch_reuse": getattr(self, "_gp_reuse", None)})

    @staticmethod
    def _observed_plane(kernel) -> dict[str, str]:
        """The runtime's maintained observed plane keyed by VISIBLE label
        (system-side export only — the evaluation plane diffs it against
        its own hidden snapshot for the round-trip projection metric)."""
        out: dict[str, str] = {}
        try:
            state = kernel.task_state()
            if state is not None:
                for var in state.variables:
                    if var.observed is not None:
                        out[var.label] = str(var.observed)
        except Exception:                      # noqa: BLE001 — best effort
            pass
        return out

    def _grace_round(self, kernel, stop: str,
                     trace: list[dict[str, Any]]) -> bool:
        """Decide whether a non-done stop deserves one more round (an
        event we just processed may have unblocked forward autonomy)."""
        if self._pending or self._comp_plan is not None:
            return True
        if stop == "blocked" and kernel.pending_recompose is None:
            # a pending compensation we already executed, or a genuine
            # kernel gate: the next run() would spin identically
            self._stop_note = "kernel-blocked forward autonomy"
            return False
        self._stop_note = f"runtime stop: {stop}"
        return False

    # ── governance routing ─────────────────────────────────────────────
    def _drain(self, gov, runtime, kernel, substrate,
               trace: list[dict[str, Any]]) -> None:
        while self._pending:
            ev = self._pending.pop(0)
            trace.append({"event": "external", "kind": ev.kind.value})
            try:
                if ev.kind.value == "goal_patch":
                    if self._no_replan:
                        self._stop_note = ("goal patch left the kernel "
                                           "blocked (no-replan ablation)")
                        continue
                    new_goal = str(ev.payload.get("goal", ""))
                    gov.handle(GoalPatchRequested(
                        new_intent=TaskIntent(goal=new_goal)
                        if new_goal else None))
                    # GoalPatch reuse sampling (handoff §治理与恢复:
                    # 已完成工作复用率 / plan size) — counted from the
                    # kernel's workflow snapshot right after the recompose
                    try:
                        snap = kernel.workflow()
                        stats = list(snap.statuses.values())
                        committed = sum(
                            1 for s in stats
                            if getattr(s, "name", str(s)) == "COMMITTED")
                        self._gp_reuse = {"committed": committed,
                                          "total": len(stats)}
                    except Exception:              # noqa: BLE001
                        self._gp_reuse = None
                elif ev.kind.value == "rollback_request":
                    self._rollback(gov, kernel, trace)
                elif ev.kind.value == "local_patch":
                    updates = dict(ev.payload.get("updates", {}))
                    if updates:
                        gov.handle(LocalPatchRequested(updates=updates))
                elif ev.kind.value == "pause_resume":
                    # pre-compose arrivals land here (the hot path above
                    # handled everything after composition)
                    phase = str(ev.payload.get("phase", "pause"))
                    if phase == "resume":
                        runtime.request_resume()
                    else:
                        runtime.request_pause()
            except Exception as e:            # noqa: BLE001
                trace.append({"event": "governance_error",
                              "error": f"{type(e).__name__}: {e}"[:160]})
                self._stop_note = f"governance routing failed: {e}"[:160]

    def _rollback(self, gov, kernel, trace: list[dict[str, Any]]) -> None:
        cps = kernel.checkpoints()
        if not cps:
            self._stop_note = "rollback requested but no committed checkpoint"
            trace.append({"event": "rollback", "outcome": "no_checkpoint"})
            return
        target = cps[-1]
        outcome = gov.handle(RollbackRequested(
            target_checkpoint_id=target.checkpoint_id))
        trace.append({"event": "rollback",
                      "outcome": outcome.handled,
                      "checkpoint": target.checkpoint_id})
        if outcome.compensation_plan is not None:
            self._comp_plan = outcome.compensation_plan

    def _on_runtime_event(self, ev, gov, kernel, substrate,
                          trace: list[dict[str, Any]]) -> None:
        if ev.kind.value == "surface_conflict":
            keys = list(ev.payload.get("keys", []))
            # rational user: the external change is authoritative —
            # accept the underlying reality, re-target the future
            obs = substrate.observe(ev.surface_id)
            cur = parse_visible_kv(obs.visible_text or "")
            updates = {k: cur[k] for k in keys if k in cur}
            # Cascade: when a conflict changes a variable's target, any
            # OTHER variable whose desired value MATCHED the old desired
            # value of a changed variable should follow reality too —
            # the user's goal linked them (e.g. "set budget to 120"
            # implies the note should match the authoritative budget).
            state = kernel.task_state()
            if state is not None and updates:
                for var in state.variables:
                    if var.semantic_key in updates:
                        continue
                    old_desired = var.desired
                    if old_desired is None:
                        continue
                    for changed_key in updates:
                        changed_var = state.variable(changed_key)
                        if changed_var is not None:
                            old_changed_desired = changed_var.desired
                            if (old_changed_desired is not None
                                    and old_changed_desired == old_desired
                                    and var.semantic_key not in updates):
                                updates[var.semantic_key] = updates[changed_key]
                                break
            trace.append({"event": "conflict_resolution",
                          "resolution": "accept_underlying",
                          "keys": list(updates.keys())})
            try:
                gov.handle(ConflictResolutionRequested(
                    description="inactive surface drifted",
                    semantic_keys=tuple(keys),
                    resolution="accept_underlying"))
                if updates:
                    gov.handle(LocalPatchRequested(updates=updates))
            except Exception as e:            # noqa: BLE001
                trace.append({"event": "governance_error",
                              "error": f"{type(e).__name__}: {e}"[:160]})
        elif ev.kind.value == "structure_invalidated":
            # Active-surface drift recovery through the PUBLIC governance
            # seam: recompile_structure = incremental State Compiler call
            # (prior_state handed in so semantic keys survive a relabel)
            # +, if the variable STRUCTURE changed, ONE architect
            # recomposition of the uncommitted future (kernel.recompose
            # keeps committed history). No private attribute reaches, no
            # oracle knowledge — the runtime-visible world only.
            trace.append({"event": "structure_recovery",
                          "via": "recompile_structure"})
            try:
                sid = substrate.list_surfaces()[0].surface_id
                obs = substrate.observe(sid)
                view = CompilerObservationView(
                    revision=(kernel.task_state().revision + 1),
                    regions=(VisibleRegion(
                        surface_label=sid,
                        visible_text=obs.visible_text,
                        structure_fingerprint=obs.fingerprint),))
                outcome = gov.recompile_structure(view, reason=(
                    getattr(ev, "detail", "")
                    or "runtime reported structure_invalidated")[:120])
                trace.append({"event": "structure_recovery",
                              "outcome": str(outcome.handled),
                              "detail": str(outcome.detail)[:160]})
            except Exception as e:            # noqa: BLE001
                trace.append({"event": "governance_error",
                              "error": f"{type(e).__name__}: {e}"[:160]})


class _NoVerifier:
    """Ablation ``taskvm-no-verifier``: CUA done == verified. Satisfies
    the runtime Verifier protocol structurally."""

    def verify(self, *, node, before_observed, after_observed, desired,
               observation, action_id, epoch):
        from taskvm.domain.results import VerificationResult
        return VerificationResult(
            node_id=node.node_id, epoch=epoch, passed=True,
            action_id=action_id,
            detail="cua-done accepted without independent check "
                   "(no-verifier ablation)")


class _OracleVerifier:
    """DIAGNOSTIC ONLY (``taskvm-oracle-upper-bound``): the runtime
    verifier bound to the evaluation plane's frozen ground truth — a
    node passes when the FROZEN success values hold, regardless of what
    the architect's desired plane claims (planning errors vanish; this
    is an upper bound, never a main result)."""

    def __init__(self, spec) -> None:
        self._gt: dict[str, str] = {}
        for _surf, kv in spec.success.items():
            for k, v in kv.items():
                self._gt[k] = v

    def verify(self, *, node, before_observed, after_observed, desired,
               observation, action_id, epoch):
        from taskvm.domain.results import VerificationResult
        # Upper-bound semantics: judge ONLY the node's own contract keys,
        # and accept EITHER the contract's desired value OR the frozen
        # ground-truth value (a planning error that still lands the
        # eventual truth is not held against execution). Execution
        # failures stay failures; keys outside this node are not judged
        # (their nodes have not run yet — judging them here was what made
        # this ablation escalate instead of bounding).
        contract = dict(node.contract.desired_state) if node.contract else {}
        passed = True
        for k, want in contract.items():
            got = after_observed.get(k)
            gt = self._gt.get(k)
            if got == want or (gt is not None and got == gt):
                continue
            passed = False
        return VerificationResult(
            node_id=node.node_id, epoch=epoch, passed=passed,
            action_id=action_id,
            detail="oracle-bound verification (diagnostic upper bound)")


# ── B-06: the real-model conditions ──────────────────────────────────────
#
# Model pinning (B-06 hard rule): each harness constructs ONE
# ``HttpModelPort`` at ``__init__`` and reuses it for the whole trial —
# compiler, architect and CUA share that one port (and its pinned
# ``default_model``). There is NO failure-triggered re-construction, NO
# model fallback path: a provider failure propagates as an honest stop /
# raised error with the SAME port and SAME model id. A fallback would
# have to be a different condition id (explicitly recorded).

class _RealPortMixin:
    """Shared construction: one pinned port + one shared ledger, injected
    once; ``model_port`` exists so CONTRACT-WIRING tests can script the
    provider (a scripted port pass is never a real-model claim)."""

    _rm_port: HttpModelPort
    _rm_ledger: ModelCallLedger

    def _init_real(self, *, model: str | None, model_port, ledger) -> None:
        self._rm_ledger = ledger if ledger is not None else ModelCallLedger()
        self._rm_port = model_port if model_port is not None else \
            HttpModelPort(default_model=model)

    @property
    def pinned_model(self) -> str:
        """The model id this condition is pinned to (report metadata)."""
        return getattr(self._rm_port, "default_model", "") or ""


class DirectCUARealHarness(_RealPortMixin, DirectCUAHarness):
    """Condition ``direct-cua-real`` (B-06 baseline): the bare CUA loop
    over the REAL ``HttpCUAModel`` — identical structure to
    ``direct-cua``, real capability. Every provider request lands in the
    shared ledger (1 request = 1 row, A-13)."""

    condition = Condition.DIRECT_CUA_REAL

    def __init__(self, *, model: str | None = None,
                 model_port=None, ledger=None) -> None:
        super().__init__()
        self._init_real(model=model, model_port=model_port, ledger=ledger)

    def _new_counter(self) -> _CUACounter:
        return _CUACounter(
            HttpCUAModel(port=self._rm_port, ledger=self._rm_ledger))

    def _cua_calls(self, counter: _CUACounter) -> int:
        return int(getattr(counter.inner, "request_count", 0))


class PlannerCUARealHarness(_RealPortMixin, PlannerCUAHarness):
    """Condition ``planner-cua-real`` (B-06 baseline): the planner loop
    with the REAL ``HttpCUAModel`` executing. The planner layer stays the
    deterministic goal-program structure — the compared axis across the
    real series is the CUA capability under each harness STRUCTURE, so
    the planner must remain structure, not a second model variable."""

    condition = Condition.PLANNER_CUA_REAL

    def __init__(self, *, model: str | None = None,
                 model_port=None, ledger=None) -> None:
        super().__init__()
        self._init_real(model=model, model_port=model_port, ledger=ledger)

    def _new_counter(self) -> _CUACounter:
        return _CUACounter(
            HttpCUAModel(port=self._rm_port, ledger=self._rm_ledger))

    def _cua_calls(self, counter: _CUACounter) -> int:
        return int(getattr(counter.inner, "request_count", 0))


class RealCUAOnlyHarness(_RealPortMixin, TaskVMHarness):
    """Condition ``taskvm-real-cua-only`` (B-06 DIAGNOSTIC): compiler /
    architect stay template (deterministic); ONLY the CUA is real. The
    condition id says cua-only and it is registered in
    ``DIAGNOSTIC_ONLY_CONDITIONS`` — it must never be reported as
    real-full. Useful for isolating CUA capability from the
    compiler/architect legs."""

    condition = Condition.TASKVM_REAL_CUA_ONLY

    def __init__(self, *, model: str | None = None,
                 model_port=None, ledger=None) -> None:
        super().__init__(condition=Condition.TASKVM_REAL_CUA_ONLY)
        self._init_real(model=model, model_port=model_port, ledger=ledger)

    def _new_cua(self, ledger) -> _CUACounter:
        # compiler/architect rows land in the ledger minted by the base
        # compose; the REAL CUA port writes into the SAME ledger object
        # the harness pinned (single owner).
        return _CUACounter(
            HttpCUAModel(port=self._rm_port, ledger=self._rm_ledger))

    def _new_ledger(self) -> ModelCallLedger:
        # the pinned single-owner ledger: template compiler/architect
        # rows AND real CUA rows all land here.
        return self._rm_ledger


class RealFullHarness(_RealPortMixin, TaskVMHarness):
    """Condition ``taskvm-real-full`` (B-06 MAIN): the full TaskVM stack
    over the REAL provider chain — fresh substrate observation → real
    StateCompiler → real TaskArchitect → kernel FROM THE ARCHITECT
    PRODUCT → real ``HttpCUAModel`` over the real GUI substrate →
    VisibleVerifier → ONE shared single-owner ledger.

    The composition path is ``bootstrap_real_full`` (B-07) — the SAME
    single path the projection/demo use; this harness adds NO second
    orchestration. Forbidden by construction: TemplateModelPort /
    TemplateCUA / hand-written TaskVariable / hand-written WorkflowGraph
    / fixture plan fallback (an architect failure honestly stops the
    trial with ``no_plan`` — nothing hand-built is substituted)."""

    condition = Condition.TASKVM_REAL_FULL

    def __init__(self, *, model: str | None = None,
                 model_port=None, ledger=None) -> None:
        super().__init__(condition=Condition.TASKVM_REAL_FULL)
        self._rm_model = model
        self._init_real(model=model, model_port=model_port, ledger=ledger)

    def _compose(self, substrate: SubstrateSession,
                 goal: str) -> tuple:
        # ONE pinned port + ONE shared ledger across compiler, architect
        # AND CUA; the counting wrapper is transparent to ledger rows.
        cua = _CUACounter(
            HttpCUAModel(port=self._rm_port, ledger=self._rm_ledger))
        bundle = bootstrap_real_full(
            goal=goal, sid="benchmark-trial-real-full",
            substrate=substrate, model_port=self._rm_port,
            ledger=self._rm_ledger, model=self._rm_model, cua_model=cua)
        kernel = bundle["kernel"]
        runtime = bundle["runtime"]
        # governance routing for external events reuses the REAL
        # compiler/architect objects the bootstrap itself constructed
        # (no second instance, no second ledger).
        gov = GovernanceService(
            kernel, architect=bundle["architect"],
            compiler=bundle["compiler"], ledger=self._rm_ledger)
        return (self._rm_ledger, self._rm_port, bundle["compiler"],
                bundle["architect"], kernel, gov, runtime, cua)


def make_harness(condition: "Condition | str", *, spec=None,
                 model: str | None = None,
                 model_port=None, ledger=None):
    """The condition factory. ``spec`` is required (and only legal) for
    the diagnostic oracle upper bound.

    B-06: ``model`` pins the model id for the REAL conditions (one
    ``HttpModelPort`` per harness — no failure-driven switch). It is a
    hard error to pass ``model``/``model_port`` to a template condition:
    pinning is meaningless there and silently accepting it would blur
    the condition taxonomy. ``model_port`` exists for contract-wiring
    tests only — a scripted port is never a real-model claim."""
    if not isinstance(condition, Condition):
        from taskvm_bench.benchmark.registry import condition_of
        condition = condition_of(condition)
    real_kwargs = (model, model_port, ledger)
    if condition is Condition.DIRECT_CUA:
        if any(k is not None for k in real_kwargs):
            raise ValueError(
                "model/model_port/ledger only apply to real-model "
                f"conditions; {condition.value!r} is template")
        return DirectCUAHarness()
    if condition is Condition.PLANNER_CUA:
        if any(k is not None for k in real_kwargs):
            raise ValueError(
                "model/model_port/ledger only apply to real-model "
                f"conditions; {condition.value!r} is template")
        return PlannerCUAHarness()
    if condition is Condition.TASKVM:
        if any(k is not None for k in real_kwargs):
            raise ValueError(
                "model/model_port/ledger only apply to real-model "
                f"conditions; {condition.value!r} is template")
        return TaskVMHarness()
    if condition is Condition.TASKVM_TEMPLATE_CONTROL:
        if any(k is not None for k in real_kwargs):
            raise ValueError(
                "model/model_port/ledger only apply to real-model "
                "conditions; the template control is template by design")
        return TaskVMHarness(condition=condition)
    if condition is Condition.DIRECT_CUA_REAL:
        return DirectCUARealHarness(model=model, model_port=model_port,
                                    ledger=ledger)
    if condition is Condition.PLANNER_CUA_REAL:
        return PlannerCUARealHarness(model=model, model_port=model_port,
                                     ledger=ledger)
    if condition is Condition.TASKVM_REAL_CUA_ONLY:
        return RealCUAOnlyHarness(model=model, model_port=model_port,
                                  ledger=ledger)
    if condition is Condition.TASKVM_REAL_FULL:
        return RealFullHarness(model=model, model_port=model_port,
                               ledger=ledger)
    if condition is Condition.TASKVM_ORACLE_UPPER_BOUND:
        if spec is None:
            raise ValueError(
                "taskvm-oracle-upper-bound requires the task spec "
                "(diagnostic ground-truth binding)")
        return TaskVMHarness(condition=condition, oracle_spec=spec)
    if condition is Condition.TASKVM_NO_VERIFIER:
        return TaskVMHarness(condition=condition, no_verifier=True)
    if condition is Condition.TASKVM_NO_REPLAN:
        return TaskVMHarness(condition=condition, no_replan=True)
    raise ValueError(f"unknown condition {condition!r}")
