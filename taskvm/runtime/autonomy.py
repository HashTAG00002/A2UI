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
STOPPED = "stopped"          # A-02: persistent lifecycle stop
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
                 model: str | None = None,
                 surface_resolver=None) -> None:
        self._kernel = kernel
        self._substrate = substrate
        self._cua = cua_model
        self._ser = serializer
        self._ext = extractor
        self._verifier = verifier
        self._ledger = ledger
        self._budgets = budgets or RuntimeBudgets()
        self._model = model
        self._resolver = surface_resolver
        surfaces = surfaces if surfaces is not None else self._discover_surfaces()
        self._sync = SurfaceSync(kernel, substrate, extractor, list(surfaces))
        self._comp = CompensationExecutor(
            kernel, substrate, self._sync, cua_model, serializer,
            extractor, ledger, self._budgets)
        self._events: list[RuntimeEvent] = []
        self._paused = False
        self._stopped = False              # A-02: persistent lifecycle state
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
        # A-02: resume from a paused state only — stop is persistent
        if self._stopped:
            return
        self._paused = False
        self._stop_reason = None
        self._kernel.request_governance("resume", "soft pause cleared")

    def request_stop(self) -> None:
        """A-02: persistent lifecycle stop. Once stopped, the runtime never
        starts a new GUI atomic action. An in-flight primitive may complete
        (it is already inside substrate.act); but the next _pre_tick returns
        STOPPED and the driver thread exits. Stop is irreversible — only
        a fresh composition / driver.start() with a new runtime instance
        can begin again (the frozen contract does not define restart-from-
        stopped on the same runtime object).

        A-02 stop-during-inference: a stop that lands while
        ``predict_action`` is blocked on the provider must make the
        prediction's returned ACT stale on arrival — the ACT branch
        re-enters the lifecycle gate BEFORE start_action/substrate.act
        (0 GUI writes). Stop deliberately does NOT bump the kernel epoch
        (``request_governance`` bumps only for pause; the kernel is frozen
        there) and stays ONE governance event — the single-owner path
        pinned by tests/projection/test_lifecycle_a02.py."""
        self._stopped = True
        self._paused = True  # also pause to break out of action loop
        self._stop_reason = "stopped"
        self._kernel.request_governance("stop", "lifecycle stop requested")

    def execute_compensation(self, plan, *, surface_id: str | None = None,
                             model: str | None = None) -> str:
        """Land a kernel-produced CompensationPlan through the real CUA +
        substrate. Forward autonomy is blocked by the kernel while the plan is
        pending (runtime.md §7).

        A-01: each entry routes to the surface that owns its semantic key's
        binding (resolved from the variable's evidence handle), NOT to a
        default surface. ``surface_id`` (explicit override) still wins — the
        caller may know the surface from context; per-entry resolution then
        only fills the entries it can honestly resolve."""
        variables = {v.semantic_key: v
                     for v in self._kernel.task_state().variables}
        # single-surface sessions are routing-trivial (one candidate — no
        # ambiguity, hence not a surface-0 "fallback"); the resolver chain
        # only becomes LOAD-BEARING in multi-surface sessions (A-01)
        trivial = (self._sync.surfaces[0]
                   if len(self._sync.surfaces) == 1 else None)
        surface_for_entry: dict[str, str] = {}
        for entry in plan.entries:
            sid = surface_id
            if sid is None:
                sid = self._resolve_surface_for_key(entry.semantic_key,
                                                    variables)
            if sid is None:
                sid = trivial
            if sid is None:
                self._publish(RuntimeEventKind.STRUCTURE_INVALIDATED,
                              node_id=entry.node_id,
                              detail=(f"compensation entry "
                                      f"{entry.semantic_key!r} has no "
                                      "resolvable surface binding; honest "
                                      "not-compensated (no surface-0 "
                                      "fallback)"))
                continue
            surface_for_entry[entry.node_id] = sid
        # the resolved surfaces still set the active marker so the CUA's own
        # observations drive the right surface
        first = next(iter(surface_for_entry.values()), None)
        if first is not None:
            self._sync.set_active(first)
        # Re-sync the CUA model reference — tests (and composition) may swap
        # ``self._cua`` between forward autonomy and compensation execution.
        self._comp._cua = self._cua
        disp = self._comp.execute(plan, surface_for_entry=surface_for_entry,
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

    @property
    def budgets(self) -> RuntimeBudgets:
        """The flat budget object (read-only view; runtime.md §5). A-03:
        the production driver reads ``inactive_heartbeat_seconds`` from
        here — the cadence is a runtime-layer budget, never a hardcoded
        projection-layer constant."""
        return self._budgets

    # ── the tick ──────────────────────────────────────────────────────────
    def _pre_tick(self) -> str | None:
        # A-02: persistent stop — checked FIRST, before anything else.
        # Once stop lands, the runtime never resumes on any tick.
        if self._stopped:
            return STOPPED
        if self._paused:
            return self._stop_reason or PAUSED
        if self._kernel.pending_recompose is not None:
            return PENDING_RECOMPOSE
        if self._governance_says_stopped():
            # an EXTERNAL stop (composition called the kernel directly)
            # must be honoured here too — the runtime stays stopped.
            self._stopped = True
            self._paused = True
            self._stop_reason = STOPPED
            return STOPPED
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

    def _governance_says_stopped(self) -> bool:
        """A-02: check whether the last governance event was a stop.
        Handles the case where an external caller wrote stop directly to
        the kernel (not through the driver path)."""
        last = None
        for e in self._kernel.events():
            if e.kind is EventKind.GOVERNANCE_REQUESTED:
                last = e.payload.get("action")
        return last == "stop"

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
        if not self._sync.surfaces:
            # no surface at all = unrecoverable for the runtime: escalate
            # (pause + typed event) so the loop stops instead of spinning on
            # a node it can never advance (honest stop, no fallback path)
            self._escalate(node, "no surface available: cannot drive the "
                                 "CUA loop")
            return True
        repairs = 0
        repair_note = ""
        while True:
            outcome, detail = self._run_contract_once(node, repair_note)
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

    # ── A-01: contract-evidence → surface routing ──────────────────────
    def _resolve_surface_for_contract(self, contract, node) -> str | None:
        """Route an ActionContract to the surface its target evidence was
        grounded on — never a ``surfaces[0]`` default.

        Resolution order: (1) the contract's target-evidence handle via the
        injected resolver; (2) the desired-state keys' variable evidence
        handles; (3) the SINGLE-surface session case, which is routing-
        trivial (one candidate — no ambiguity, hence not a fallback). With
        several surfaces and no resolvable binding the answer is ``None``:
        an honest routing failure (StructureInvalidated), never a guess."""
        for ev in (contract.target_evidence or ()):
            sid = self._resolve_handle(ev.surface.handle_id,
                                       visible_label=ev.visible_label)
            if sid is not None:
                return sid
        variables = self._variables()
        for key in contract.desired_state:
            sid = self._resolve_surface_for_key(key, variables)
            if sid is not None:
                return sid
        if len(self._sync.surfaces) == 1:
            return self._sync.surfaces[0]  # trivially unambiguous, not a guess
        return None

    def _resolve_surface_for_key(self, semantic_key: str, variables) -> str | None:
        var = variables.get(semantic_key)
        if var is None:
            return None
        for ev in (var.evidence or ()):
            sid = self._resolve_handle(ev.surface.handle_id,
                                       visible_label=ev.visible_label)
            if sid is not None:
                return sid
        return None

    def _resolve_handle(self, handle_id: str, *, visible_label: str = ""
                        ) -> str | None:
        if self._resolver is None:
            return None
        try:
            return self._resolver.resolve_surface(
                handle_id, visible_label=visible_label)
        except Exception:
            return None  # a broken resolver is a routing failure, not a crash

    def _run_contract_once(self, node: WorkflowNode, repair_note: str
                           ) -> tuple[str, str]:
        """One request → predict → act → finish → verify pass over the CURRENT
        visible world. Returns ``(outcome, detail)`` — outcome is
        ``"verify_failed"`` when the verdict failed and repair may still fix
        it; anything else ends the pass for this node.

        A-01: the surface is resolved from the CONTRACT's target evidence
        AFTER ``request_action`` hands the contract over — evidence grounded
        on surface B drives surface B. An unresolvable binding in a
        multi-surface session ⇒ honest fail (StructureInvalidated), never a
        surface-0 default.

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
        surface = self._resolve_surface_for_contract(contract, node)
        if surface is None:
            self._publish(RuntimeEventKind.STRUCTURE_INVALIDATED,
                          node_id=node.node_id,
                          detail=("action contract has no resolvable surface "
                                  "binding (multi-surface session); honest "
                                  "fail — no surface-0 fallback (A-01)"))
            self._land_fail(node, action_id, False,
                            "surface binding unresolved for contract "
                            f"{contract.contract_id!r}")
            return "stopped", ""
        self._sync.set_active(surface)
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
            if self._paused or self._stopped:
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
                # A-13: when the adapter owns its ledger (declared via
                # ``records_own_ledger``) the row for THIS request already
                # exists (the adapter records on every path, including the
                # exception path) — appending another would double-count.
                # Fakes without the declaration keep the runtime as their
                # row owner (legacy contract).
                if not getattr(self._cua, "records_own_ledger", False):
                    self._ledger.record(ModelCallRecord(
                        role=MODEL_ROLE_CUA,
                        purpose=f"action_{node.node_id}_invalid{invalid}",
                        model=self._model or "", ok=False,
                        is_repair=is_repair, error=str(e)[:200],
                        revision=self._kernel.task_state().revision,
                        node_id=node.node_id, attempt=invalid))
                if invalid >= self._budgets.max_invalid_predictions_per_contract:
                    self._safe_pause(BUDGET_EXHAUSTED)
                    return "stopped", ""
                continue
            self._record_call(decision, f"action_{node.node_id}_{actions + 1}",
                              is_repair=is_repair, node_id=node.node_id,
                              attempt=actions + invalid + 1)
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
            gesture = decision.action
            if gesture is None:
                # ACT kind guarantees a gesture (CUADecision validation);
                # this belt-and-braces guard keeps the type contract local
                return "stopped", ""
            # A-02 (stop during inference): ``predict_action`` may block on
            # the provider for arbitrarily long. A public stop/pause that
            # landed while the model was thinking makes this prediction
            # stale ON ARRIVAL — re-enter the lifecycle gate (the same
            # flags the loop top checks, applied AFTER the blocking
            # inference) BEFORE start_action/substrate.act. Zero GUI
            # writes; no second epoch/cancellation system is invented —
            # these are the runtime's own lifecycle flags plus the kernel's
            # governance log for an EXTERNAL stop (composition wrote
            # ``request_governance("stop")`` directly; that path bumps no
            # epoch, so the kernel gate alone would not veto the gesture).
            # The provider row was already recorded above (1 request = 1
            # row); execution disposition is NOT a ledger field (C-2). The
            # attempt's lifecycle evidence is the kernel's
            # GOVERNANCE_REQUESTED(stop) event + run() returning STOPPED;
            # the handle honestly stays REQUESTED — never started, never
            # partially executed.
            if (self._paused or self._stopped
                    or self._governance_says_stopped()):
                return "stopped", ""
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
                self._substrate.act(surface, gesture,
                                    epoch=str(self._kernel.epoch))
            except IrreversibleAction:
                self._land_fail(node, action_id, started,
                                "irreversible action unavailable on substrate")
                return "stopped", ""
            acts.append(gesture.description or gesture.kind)
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
        surface = self._verify_surface(node)
        if surface is None:
            # A-01: a VERIFY node whose condition keys carry no resolvable
            # binding (multi-surface session) cannot be honestly judged —
            # land a failed verdict + StructureInvalidated, never verify
            # against a guessed surface-0 world.
            self._publish(RuntimeEventKind.STRUCTURE_INVALIDATED,
                          node_id=node.node_id,
                          detail=("verify node has no resolvable surface "
                                  "binding for its condition keys; honest "
                                  "fail — no surface-0 fallback (A-01)"))
            self._kernel.land_verification(VerificationResult(
                node_id=node.node_id, epoch=self._kernel.epoch,
                passed=False, action_id=None,
                detail="surface binding unresolved for verification"))
            return
        obs = self._sync.observe_active(surface)
        before = self._kernel.task_state().observed_values()
        try:
            values = self._ext.extract(obs, self._variables())
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

    def _verify_surface(self, node: WorkflowNode) -> str | None:
        """A-01: the surface a VERIFY node's condition is grounded on —
        parsed from its ``key == value`` predicate; then the variables'
        evidence handles; then the trivial single-surface case. ``None`` in
        a multi-surface session with no resolvable binding (honest fail)."""
        variables = self._variables()
        pred = (node.verification or "").strip()
        if "==" in pred:
            key = pred.partition("==")[0].strip()
            sid = self._resolve_surface_for_key(key, variables)
            if sid is not None:
                return sid
        for var in variables.values():
            for ev in (var.evidence or ()):
                sid = self._resolve_handle(ev.surface.handle_id,
                                           visible_label=ev.visible_label)
                if sid is not None:
                    return sid
        if len(self._sync.surfaces) == 1:
            return self._sync.surfaces[0]  # trivially unambiguous
        return None

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
        node off READY; leaving it spinning would be a hot retry loop.

        The kernel's protocol is request → start → **finish** → verify: a
        verdict can only land on a FINISHED attempt. A mid-contract failure
        (CUA FAIL / structure invalidated / irreversible unavailable) arrives
        with the handle STARTED — it must be finished here too, else the
        kernel rejects the verdict and the node would hang in RUNNING
        forever (a silently dropped failure is not an honest failure). A
        stale finish (governance superseded the attempt) returns False and
        the verdict is correctly not landed."""
        if not started:
            started = self._kernel.start_action(action_id)
            if not started:
                return    # stale handle — governance already superseded it
        if not self._kernel.finish_action(action_id, observations=()):
            return    # stale discard (epoch bumped / node historical)
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
                     is_repair: bool = False,
                     node_id: str = "", attempt: int = 0) -> None:
        """A-13 single-owner accounting. When the decision carries a
        ``request_id`` the CUA adapter already landed the provider-request
        row — we ANNOTATE it with execution context instead of appending a
        second row (C-2: 1 provider request = 1 ledger row). An adapter
        that declares ``records_own_ledger`` (the production
        ``HttpCUAModel``) owns its row on EVERY path — even a decision
        without a ``request_id`` (a test fake or a variant adapter) is
        then NOT re-recorded here, mirroring the exception path's
        existing ``records_own_ledger`` check. Only adapters without the
        declaration (plain test fakes) keep the runtime as their row
        owner."""
        self._model_calls += 1
        if decision.request_id and callable(
                getattr(self._ledger, "annotate", None)):
            self._ledger.annotate(
                decision.request_id, purpose=purpose, node_id=node_id,
                attempt=attempt, is_repair=is_repair,
                revision=self._kernel.task_state().revision)
            return
        if getattr(self._cua, "records_own_ledger", False):
            return    # the adapter's promise covers this request already
        self._ledger.record(ModelCallRecord(
            role=MODEL_ROLE_CUA, purpose=purpose,
            model=self._model or "",
            ok=decision.kind is not CUADecisionKind.FAIL,
            is_repair=is_repair,
            revision=self._kernel.task_state().revision,
            node_id=node_id, attempt=attempt))

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
