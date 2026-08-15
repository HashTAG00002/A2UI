"""taskvm.runtime.autonomy — the execution clock (runtime.md §0, §3, §4).

A single session has one ``AutonomyRuntime``. It owns NO authoritative epoch
(the kernel does); it drives the kernel's action lifecycle forward while no
governance event blocks it, and discards stale CUA responses through the
kernel's own ``start_action`` gate (hot governance, runtime.md §4).

The loop is single-threaded: logical parallel (fan-out lanes advance
independently) does NOT require physical parallel — on a single device lanes
run serial and the runtime records that honestly (runtime.md §10).

Budget model (runtime.md §5) — one flat layer, per-quantity ceilings:
``max_actions_per_contract`` bounds atomic GUI gestures per contract;
``max_invalid_predictions_per_contract`` bounds CUA timeout / invalid-JSON /
unparseable replies (they are model calls but never GUI actions);
``max_repairs_per_contract`` bounds context-preserving repairs after a
verify failure (each repair REQUEUES the node and continues from the CURRENT
visible world — never a back-to-home full rerun); ``max_model_calls_per_task``
and ``wall_clock_budget`` are task-level hard caps. Hitting a ceiling is a
SAFE stop (``BudgetExhausted`` runtime event + kernel governance pause),
never a blind keep-running.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from taskvm.domain.errors import ValidationError
from taskvm.domain.events import EventKind
from taskvm.domain.results import VerificationResult
from taskvm.domain.workflow import (
    NodeKind, NodeStatus, WorkflowNode,
)
from taskvm.substrate import IrreversibleAction

from taskvm.runtime.compensation import CompensationExecutor
from taskvm.runtime.config import RuntimeBudgets
from taskvm.runtime.ports import (
    CallLedger, CUADecision, CUADecisionKind, CUAGoalSerializer, CUAModel,
    ModelCallRecord, MODEL_ROLE_CUA, ObservationExtractor, RuntimeEvent,
    RuntimeEventKind, Verifier,
)
from taskvm.runtime.sync import StructureInvalidation, SurfaceSync

# reasons run() may return (audit/UI facing)
DONE = "done"
PAUSED = "paused"
BUDGET_EXHAUSTED = "budget_exhausted"
PENDING_RECOMPOSE = "pending_recompose"
NO_PLAN = "no_plan"
NO_READY = "no_ready_work"
STEP_BUDGET = "step_budget"
BLOCKED = "blocked"
ESCALATED = "escalated"


class AutonomyRuntime:
    """The execution clock. Holds injected ports + a kernel facade reference.
    Produces runtime events + visual artifacts for projection; never writes
    the kernel's private stores and never imports a concrete substrate."""

    def __init__(self, kernel, substrate, *,
                 cua_model: CUAModel, serializer: CUAGoalSerializer,
                 extractor: ObservationExtractor, verifier: Verifier,
                 ledger: CallLedger, budgets: RuntimeBudgets | None = None,
                 surfaces: list[str] | None = None,
                 model: str | None = None) -> None:
        self._kernel = kernel
        self._substrate = substrate
        self._cua = cua_model
        self._ser = serializer
        self._ext = extractor
        self._verifier = verifier
        self._ledger = ledger
        self._budgets = budgets or RuntimeBudgets()
        self._model = model
        surfaces = surfaces if surfaces is not None else self._discover_surfaces()
        self._sync = SurfaceSync(kernel, substrate, extractor, list(surfaces))
        self._comp = CompensationExecutor(
            kernel, substrate, self._sync, cua_model, serializer,
            extractor, ledger, self._budgets)
        self._events: list[RuntimeEvent] = []
        self._paused = False
        self._stop_reason: str | None = None   # why we paused (budget/…)
        self._model_calls = 0
        self._t0: float = 0.0

    # ── public facade ─────────────────────────────────────────────────────
    def run(self, step_budget: int | None = None) -> str:
        """Drive ready work forward until a stop condition. With no
        governance event the loop advances multiple ACTION/control nodes
        (autonomy, runtime.md §0)."""
        self._t0 = time.monotonic()
        steps = 0
        while True:
            stop = self._pre_tick()
            if stop is not None:
                return stop
            snap = self._kernel.workflow()
            graph, statuses = snap.graph, snap.statuses
            if graph is None:
                return NO_PLAN
            # 1. close any bounded loop whose body just finished (the
            # kernel's loop gate can refuse while compensation is pending —
            # same safe-stop contract as node advancement)
            try:
                if self._complete_loops(graph, statuses):
                    continue
            except ValidationError:
                self._publish(RuntimeEventKind.NODE_FAILED,
                              detail="forward autonomy blocked by the kernel "
                                     "at loop evaluation; safe stop")
                return BLOCKED
            # 2. pull ready actionable nodes
            ready = [n for n in graph.ready_nodes(statuses)
                     if n.kind in (NodeKind.ACTION, NodeKind.VERIFY,
                                   NodeKind.CHECKPOINT, NodeKind.BARRIER,
                                   NodeKind.TERMINAL, NodeKind.BOUNDED_LOOP)]
            if not ready:
                terms = graph.terminal_nodes()
                if terms and statuses.get(terms[0].node_id) is NodeStatus.COMMITTED:
                    return DONE
                return NO_READY
            node = ready[0]  # serial fan-out: one lane at a time
            may_continue = self._advance(node)
            if not may_continue:
                # forward autonomy is kernel-blocked (e.g. a pending
                # compensation plan of the current epoch). A SAFE stop —
                # never a hot retry loop (runtime.md §4).
                return BLOCKED
            steps += 1
            if step_budget is not None and steps >= step_budget:
                return STEP_BUDGET

    def request_pause(self) -> None:
        """Soft pause: the current atomic action completes, the NEXT action is
        blocked (runtime.md §4)."""
        self._paused = True
        self._kernel.request_governance("pause", "soft pause requested")

    def request_resume(self) -> None:
        self._paused = False
        self._stop_reason = None
        self._kernel.request_governance("resume", "soft pause cleared")

    def execute_compensation(self, plan, *, surface_id: str | None = None,
                             model: str | None = None) -> str:
        """Land a kernel-produced CompensationPlan through the real CUA +
        substrate. Forward autonomy is blocked by the kernel while the plan is
        pending (runtime.md §7)."""
        sid = surface_id or (self._sync.surfaces[0] if self._sync.surfaces else None)
        if sid is None:
            raise ValueError("no surface to compensate on")
        self._sync.set_active(sid)
        # Re-sync the CUA model reference — tests (and composition) may swap
        # ``self._cua`` between forward autonomy and compensation execution.
        self._comp._cua = self._cua
        disp = self._comp.execute(plan, surface_id=sid,
                                  model=model or self._model,
                                  model_calls_base=self._model_calls)
        self._model_calls += self._comp.consume_calls()
        self._events.extend(self._comp.events)
        return disp

    def poll_inactive_surfaces(self) -> list[RuntimeEvent]:
        evs = self._sync.poll_inactive()
        self._events.extend(evs)
        return evs

    def runtime_events(self) -> tuple[RuntimeEvent, ...]:
        return tuple(self._events)

    @property
    def model_calls(self) -> int:
        return self._model_calls

    # ── the tick ──────────────────────────────────────────────────────────
    def _pre_tick(self) -> str | None:
        if self._paused:
            return self._stop_reason or PAUSED
        if self._kernel.pending_recompose is not None:
            return PENDING_RECOMPOSE
        if self._governance_says_paused():
            # an EXTERNAL pause (composition called the kernel directly)
            # must stop the runtime too — the epoch bump already discarded
            # in-flight work; honouring it here closes the loop (§4)
            return PAUSED
        if (self._budgets.wall_clock_budget is not None
                and (time.monotonic() - self._t0) >= self._budgets.wall_clock_budget):
            self._safe_pause(BUDGET_EXHAUSTED)
            return BUDGET_EXHAUSTED
        return None

    def _governance_says_paused(self) -> bool:
        """The LAST governance gesture recorded on the kernel event log.
        The kernel's pause has no standing flag (it works via the epoch),
        so the runtime reads the log — read-only, facade-only."""
        last = None
        for e in self._kernel.events():
            if e.kind is EventKind.GOVERNANCE_REQUESTED:
                last = e.payload.get("action")
        return last == "pause"

    def _advance(self, node: WorkflowNode) -> bool:
        """Advance one node. Returns True if the loop may keep pulling ready
        work; False if forward autonomy is kernel-blocked (safe stop).

        A ``ValidationError`` from the kernel facade during a non-ACTION
        advancement means the kernel's executable gate refused the step
        (e.g. a pending compensation plan of the current epoch — the same
        gate ACTION nodes hit at ``request_action``). The honest runtime
        response is a typed blocked stop, never a crash and never a hot
        retry loop (runtime.md §7)."""
        if node.kind is NodeKind.ACTION:
            return self._advance_action(node)
        try:
            if node.kind is NodeKind.VERIFY:
                self._advance_verify(node)
            elif node.kind in (NodeKind.CHECKPOINT, NodeKind.BARRIER,
                               NodeKind.TERMINAL):
                self._advance_control(node)
            elif node.kind is NodeKind.BOUNDED_LOOP:
                self._advance_loop(node)
        except ValidationError:
            self._blocked(node)
            return False
        return True

    # ── ACTION: the CUA loop with repair + budgets ────────────────────────
    def _advance_action(self, node: WorkflowNode) -> bool:
        surface = self._sync.surfaces[0] if self._sync.surfaces else None
        if surface is None:
            # no surface = unrecoverable for the runtime: escalate (pause +
            # typed event) so the loop stops instead of spinning on a node
            # it can never advance (honest stop, no fallback path)
            self._escalate(node, "no surface available: cannot drive the "
                                 "CUA loop")
            return True
        repairs = 0
        repair_note = ""
        while True:
            outcome, detail = self._run_contract_once(node, surface,
                                                      repair_note)
            if outcome == "blocked":
                return False  # kernel-blocked forward autonomy — run()
                              # returns BLOCKED (no hot retry loop)
            if outcome != "verify_failed":
                return True   # committed / stopped / stale — caller proceeds
            # verification failed: context-preserving repair, or escalate
            if repairs >= self._budgets.max_repairs_per_contract:
                self._escalate(node, detail)
                return True
            repairs += 1
            self._kernel.requeue(node.node_id)   # FAILED → READY, same world
            repair_note = detail

    def _run_contract_once(self, node: WorkflowNode, surface: str,
                           repair_note: str) -> tuple[str, str]:
        """One request → predict → act → finish → verify pass over the CURRENT
        visible world. Returns ``(outcome, detail)`` — outcome is
        ``"verify_failed"`` when the verdict failed and repair may still fix
        it; anything else ends the pass for this node.

        Per runtime.md §8 (active-surface sync), EACH atomic GUI action folds
        a fresh observation into the kernel's OBSERVED plane and emits an
        ``ACTION_OBSERVED`` event + visual artifact — reality is folded back
        into the VM per gesture, not only at the final DONE. Per §3/§6 the
        ``before`` the verifier (and the action history) sees comes from a
        FRESH pre-action observation, not a stale kernel cache."""
        try:
            handle = self._kernel.request_action(node.node_id)
        except ValidationError:
            # forward blocked (pending compensation of the current epoch,
            # node not READY anymore) — safe stop signal to run()
            self._blocked(node)
            return "blocked", ""
        action_id = handle["action_id"]
        request_epoch = handle["epoch"]
        contract = handle["contract"]
        labels = self._labels()
        goal = self._ser.cua_goal(contract, labels)
        if repair_note:
            goal = (f"{goal}\n[repair] a previous attempt was made and "
                    f"verification failed: {repair_note}. Continue from the "
                    "CURRENT visible state; do not restart from scratch.")
        # FRESH-BEFORE (runtime.md §3/§8): observe → extract → fold BEFORE
        # the first gesture, so start_action records the real visible world
        # as the action's `before` (not a stale cache). The same observation
        # feeds the first CUA prediction.
        before, latest_obs, latest_values = self._observe_and_fold(
            node, surface)
        if latest_obs is None:
            # structure invalidated at fresh-before — the visible anchor is
            # already gone; an honest fail (composition routes the event to C)
            self._land_fail(node, action_id, False,
                            "structure invalidated before action")
            return "stopped", ""
        started = False
        actions = 0
        invalid = 0
        acts: list[str] = []          # provenance for the repair context
        is_repair = bool(repair_note)
        while actions < self._budgets.max_actions_per_contract:
            if self._paused:
                return "stopped", ""
            if not self._budgets.within_model_budget(self._model_calls):
                self._safe_pause(BUDGET_EXHAUSTED)
                return "stopped", ""
            # one CUA prediction over the LATEST fresh observation (the
            # post-action world from the previous gesture, or the fresh-before
            # observation for the first turn). Invalid replies (timeout /
            # invalid JSON / unparseable) are model calls but NEVER GUI
            # actions and are bounded by a small per-contract ceiling (§5).
            try:
                decision = self._cua.predict_action(
                    goal=goal, observation=latest_obs, labels=labels,
                    attempt=actions + invalid + 1, model=self._model)
            except Exception as e:            # provider/parse failure
                invalid += 1
                self._model_calls += 1
                self._ledger.record(ModelCallRecord(
                    role=MODEL_ROLE_CUA,
                    purpose=f"action_{node.node_id}_invalid{invalid}",
                    model=self._model or "", ok=False, is_repair=is_repair,
                    error=str(e)[:200],
                    revision=self._kernel.task_state().revision))
                if invalid >= self._budgets.max_invalid_predictions_per_contract:
                    self._safe_pause(BUDGET_EXHAUSTED)
                    return "stopped", ""
                continue
            self._record_call(decision, f"action_{node.node_id}_{actions + 1}",
                              is_repair=is_repair)
            if decision.kind is CUADecisionKind.FAIL:
                self._land_fail(node, action_id, started,
                                f"cua reported fail: {decision.reason}")
                return "stopped", ""
            if decision.kind is CUADecisionKind.DONE:
                detail = self._finish_and_verify(
                    node, action_id, started, before, surface,
                    latest_obs, latest_values)
                if detail:
                    # repair context carries the discrepancy AND the actions
                    # already taken (runtime.md §5)
                    detail = (f"{detail}; actions_taken={acts or 'none'}")
                    return "verify_failed", detail
                return "committed", ""
            # ACT — one atomic GUI gesture through the kernel's gate
            if contract.requires_confirmation and self._kernel.epoch != request_epoch:
                # governance moved between prediction and an irreversible
                # act: the stale response must NOT land (runtime.md §4)
                return "stopped", ""
            if not started:
                started = self._kernel.start_action(action_id)
                if not started:
                    return "stopped", ""  # stale CUA response — no substrate.act
            elif self._kernel.epoch != request_epoch:
                return "stopped", ""      # epoch bumped mid-contract — discard
            try:
                self._substrate.act(surface, decision.action,
                                    epoch=str(self._kernel.epoch))
            except IrreversibleAction:
                self._land_fail(node, action_id, started,
                                "irreversible action unavailable on substrate")
                return "stopped", ""
            acts.append(decision.action.description
                        or decision.action.kind)
            actions += 1
            # PER-GESTURE FOLD (runtime.md §8): act → fresh observe → extract
            # → fold into the OBSERVED plane → runtime event + visual
            # artifact. The next CUA prediction sees this folded world.
            _, latest_obs, latest_values = self._observe_and_fold(
                node, surface, gesture=decision.action)
            if latest_obs is None:
                # structure invalidated mid-contract — not repairable by
                # re-acting; land an honest fail and let composition route C
                self._land_fail(node, action_id, started,
                                "structure invalidated mid-contract")
                return "stopped", ""
        # action budget exhausted without DONE — safe stop, no blind rerun
        self._safe_pause(BUDGET_EXHAUSTED)
        return "stopped", ""

    def _observe_and_fold(self, node: WorkflowNode, surface: str,
                          *, gesture: "object | None" = None
                          ) -> tuple[Any, Any, tuple]:
        """Fresh observe → deterministic extract → fold into the kernel's
        OBSERVED plane (runtime.md §8 active-surface sync — NOT a heartbeat:
        the active surface is driven by the CUA's own act→re-observe).

        Returns ``(observed_values, observation, extracted_values)`` —
        ``observed_values`` is the kernel's observed plane AFTER the fold
        (the truthful current world). Returns ``(None, None, ())`` if the
        extractor raised ``StructureInvalidation`` (the runtime publishes the
        typed event and the caller lands an honest fail).

        With ``gesture`` set this is a post-action fold: it ALSO publishes an
        ``ACTION_OBSERVED`` event carrying the captured screenshot artifact
        (the per-gesture signal D consumes). Without ``gesture`` it is the
        fresh-before fold (silent — the per-gesture events begin with the
        first act)."""
        obs = self._sync.observe_active(surface)
        try:
            values = self._ext.extract(obs, self._variables())
        except StructureInvalidation as e:
            self._publish(RuntimeEventKind.STRUCTURE_INVALIDATED,
                          node_id=node.node_id, surface_id=surface,
                          detail=str(e))
            return None, None, ()
        self._sync.fold_action_observation(list(values))
        if gesture is not None:
            self._publish(RuntimeEventKind.ACTION_OBSERVED,
                          node_id=node.node_id, surface_id=surface,
                          artifact_ref=self._artifact_ref(obs),
                          detail=getattr(gesture, "description", None)
                          or getattr(gesture, "kind", "act"))
        return self._kernel.task_state().observed_values(), obs, values

    @staticmethod
    def _artifact_ref(observation: Any) -> str:
        """The captured screenshot the substrate handed back — the visual
        artifact for an ACTION_OBSERVED event (never an internal id)."""
        return getattr(observation, "screenshot_ref", "") or ""

    def _finish_and_verify(self, node: WorkflowNode, action_id: str,
                           started: bool, before: Mapping[str, Any],
                           surface: str, latest_obs: Any,
                           latest_values: tuple) -> str:
        """finish + verify. Returns '' when committed (or stale-discarded);
        otherwise the verification discrepancy for the repair path.

        The last per-gesture fold already brought the OBSERVED plane current;
        the CUA said DONE over ``latest_obs`` (the post-action world). So the
        verifier checks that same fresh observation — no extra observe, and
        the ``after`` is genuinely the visible world the CUA judged (§6)."""
        if not started:
            started = self._kernel.start_action(action_id)
            if not started:
                return ""    # stale discard — nothing to repair
        after_values = latest_values
        if not self._kernel.finish_action(action_id, observations=after_values):
            return ""    # stale discard
        state = self._kernel.task_state()
        vr = self._verifier.verify(
            node=node, before_observed=before,
            after_observed=state.observed_values(),
            desired=state.desired_values(), observation=latest_obs,
            action_id=action_id, epoch=self._kernel.epoch)
        self._kernel.land_verification(vr)
        self._publish(RuntimeEventKind.ACTION_LANDED, node_id=node.node_id,
                      surface_id=surface, artifact_ref=vr.evidence_ref,
                      detail="verified" if vr.passed else "verify-failed")
        return "" if vr.passed else (vr.detail or "verification failed")

    # ── VERIFY / control / loop nodes ─────────────────────────────────────
    def _advance_verify(self, node: WorkflowNode) -> None:
        surface = self._sync.surfaces[0] if self._sync.surfaces else None
        obs = self._sync.observe_active(surface) if surface else None
        before = self._kernel.task_state().observed_values()
        try:
            values = self._ext.extract(obs, self._variables()) if obs else ()
        except StructureInvalidation as e:
            self._publish(RuntimeEventKind.STRUCTURE_INVALIDATED,
                          node_id=node.node_id, detail=str(e))
            self._kernel.land_verification(VerificationResult(
                node_id=node.node_id, epoch=self._kernel.epoch,
                passed=False, action_id=None,
                detail=f"structure invalidated: {e}"))
            return
        if values:
            self._kernel.apply_observation(values)
        state = self._kernel.task_state()
        vr = self._verifier.verify(
            node=node, before_observed=before,
            after_observed=state.observed_values(),
            desired=state.desired_values(), observation=obs,
            action_id=None, epoch=self._kernel.epoch)
        self._kernel.land_verification(vr)

    def _advance_control(self, node: WorkflowNode) -> None:
        self._kernel.advance_control(node.node_id)

    def _advance_loop(self, node: WorkflowNode) -> None:
        # begin an iteration; body children re-arm READY and are advanced on
        # subsequent ticks. Termination is evaluated in _complete_loops once
        # the body is fully committed (runtime.md §11).
        self._kernel.begin_loop_iteration(node.node_id)

    def _complete_loops(self, graph, statuses) -> bool:
        """Evaluate any RUNNING bounded loop whose body just fully committed.
        Returns True if any evaluation happened (re-pull ready nodes)."""
        evaluated = False
        for node in graph.nodes:
            if node.kind is not NodeKind.BOUNDED_LOOP:
                continue
            if statuses.get(node.node_id) is not NodeStatus.RUNNING:
                continue
            body = graph.children_of(node.node_id)
            if not body:
                continue
            if any(statuses.get(c.node_id) is not NodeStatus.COMMITTED
                   for c in body):
                continue
            observed = self._kernel.task_state().observed_values()
            terminated = self._evaluate_termination(node, observed)
            self._kernel.evaluate_loop_termination(node.node_id, terminated)
            self._publish(RuntimeEventKind.LOOP_TICK, node_id=node.node_id,
                          detail=f"terminated={terminated}")
            evaluated = True
        return evaluated

    def _evaluate_termination(self, node: WorkflowNode,
                              observed: Mapping[str, Any]) -> bool:
        """Deterministic termination check from visible observed state. The
        predicate is a contract field (runtime.md §11); the runtime parses a
        ``key == value`` form. Unparseable / unmatched ⇒ continue (the
        max_iterations guard in the kernel fails the loop honestly)."""
        pred = (node.termination_predicate or "").strip()
        if "==" in pred:
            key, _, val = pred.partition("==")
            key, val = key.strip(), val.strip().strip("'\"")
            got = observed.get(key)
            if got is None:
                return False
            return str(got).strip() == val
        return False

    # ── helpers ───────────────────────────────────────────────────────────
    def _blocked(self, node: WorkflowNode) -> None:
        self._publish(RuntimeEventKind.NODE_FAILED, node_id=node.node_id,
                      detail="forward autonomy blocked by the kernel "
                             "(pending compensation / not READY); safe stop")

    def _escalate(self, node: WorkflowNode, detail: str) -> None:
        """Unrecoverable failure (verification with the repair budget spent,
        no surface): honest escalation — pause + typed event, never a blind
        rerun and never a silent spin."""
        self._paused = True
        self._stop_reason = ESCALATED
        self._kernel.request_governance("pause", detail)
        self._publish(RuntimeEventKind.NODE_FAILED, node_id=node.node_id,
                      detail=detail)

    def _land_fail(self, node: WorkflowNode, action_id: str,
                   started: bool, detail: str) -> None:
        """Land an honest node failure. If no gesture ever started we still
        walk the handle through its full lifecycle (start → finish → failed
        verdict) — a CUA 'cannot proceed' IS an attempt and must move the
        node off READY; leaving it spinning would be a hot retry loop."""
        if not started:
            started = self._kernel.start_action(action_id)
            if not started:
                return    # stale handle — governance already superseded it
            if not self._kernel.finish_action(action_id, observations=()):
                return    # stale discard
        if started:
            vr = VerificationResult(
                node_id=node.node_id, epoch=self._kernel.epoch,
                passed=False, action_id=action_id, detail=detail)
            try:
                self._kernel.land_verification(vr)
            except ValidationError:
                pass  # node already terminal — honest no-op
        self._publish(RuntimeEventKind.NODE_FAILED, node_id=node.node_id,
                      detail=detail)

    def _safe_pause(self, reason: str) -> None:
        self._paused = True
        self._stop_reason = reason
        self._kernel.request_governance("pause", reason)
        self._publish(RuntimeEventKind.BUDGET_EXHAUSTED, detail=reason)

    def _record_call(self, decision: CUADecision, purpose: str, *,
                     is_repair: bool = False) -> None:
        self._model_calls += 1
        self._ledger.record(ModelCallRecord(
            role=MODEL_ROLE_CUA, purpose=purpose,
            model=self._model or "",
            ok=decision.kind is not CUADecisionKind.FAIL,
            is_repair=is_repair,
            revision=self._kernel.task_state().revision))

    def _publish(self, kind: RuntimeEventKind, *, node_id: str = "",
                 surface_id: str = "", artifact_ref: str = "",
                 detail: str = "") -> None:
        self._events.append(RuntimeEvent(
            kind=kind, epoch=self._kernel.epoch, node_id=node_id,
            surface_id=surface_id, artifact_ref=artifact_ref, detail=detail))

    def _variables(self) -> Mapping[str, Any]:
        return {v.semantic_key: v for v in self._kernel.task_state().variables}

    def _labels(self) -> Mapping[str, str]:
        return {v.semantic_key: v.label
                for v in self._kernel.task_state().variables}

    def _discover_surfaces(self) -> list[str]:
        # honest: a broken substrate fails loudly here — no silent []
        return [s.surface_id for s in self._substrate.list_surfaces()]
