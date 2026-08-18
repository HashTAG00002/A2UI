"""taskvm.workspace_ui.composition — the D-side composition root for the
real runtime (substrate.md §8 T1 leg-1; D audit rework D-F2, 2026-08-16).

This module is the production composition root (the legacy
``taskvm.execution.gui_driver`` import and the legacy
``workspace_ui/server.py`` write routes were deleted by the Wave-3 cluster
deletion, 2026-08-16). It is the **single production call site** of
Agent E's ``taskvm.runtime.bootstrap.compose_runtime`` (RFC-001): the
five injected ports are assembled HERE, by composition — the runtime gate
forbids ``taskvm.runtime`` from importing architect/verifier/concrete
substrate, and the projection gate forbids ``taskvm.projection`` from
importing substrate at all, so this file (a composition root, allowed to
import concrete substrate implementations) is where the wiring lives.

The five ports (see ``RuntimePorts`` docstring in runtime/bootstrap.py):

- ``serializer``  — ``taskvm.architect.ActionContractSerializer`` (real,
  deterministic, 0 model calls).
- ``cua_model``   — ``HttpCUAModel``: a composition adapter over the
  architect ``HttpModelPort`` (system-prompt assembly, observation→prompt,
  JSON→``CUADecision``, no-leak gated, ledger recorded under the CUA role).
- ``extractor``   — ``VisibleLabelExtractor``: deterministic Observation →
  ObservedValues from ``label: value`` / ``label=value`` visible tokens
  (a label that appears more than once is ambiguous → honestly skipped;
  the full regex handle-cache path belongs to the architect compile
  product — routing that here would duplicate C's ownership).
- ``verifier``    — ``taskvm.verifier.visible.VisibleVerifier`` (E's
  single verifier, real).
- ``ledger``      — ``taskvm.architect.ModelCallLedger``; ONE instance is
  created here per bundle and is meant to be handed to the architect as
  well, so cua + compiler + architect calls land in one unified report.

Layering: this module imports architect + verifier + runtime bootstrap +
the substrate PORT ROOT ONLY (``taskvm.substrate`` — DTOs + registry).
Concrete provider implementations are reached EXCLUSIVELY through
``substrate_registry.create_session`` name routing (substrate.md §6:
upper layers import only the port root; the provider resolves its own
URLs/config defaults). Nothing under ``taskvm/projection`` or
``taskvm/runtime`` imports from here.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import replace as _replace
from typing import Any, Iterable, Mapping, cast

from taskvm.architect import (
    ActionContractSerializer, HttpModelPort, ModelCallLedger,
    ModelCallRecord, MODEL_ROLE_CUA, assert_prompt_clean,
)
from taskvm.architect.compiler import StateCompiler
from taskvm.architect.architect import TaskArchitect
from taskvm.architect.observation import CompilerObservationView, VisibleRegion
from taskvm.architect.port import ModelPort
from taskvm.runtime.bootstrap import RuntimePorts, compose_runtime
from taskvm.runtime.ports import (
    CallLedger, CUADecision, CUADecisionKind,
)
from taskvm.substrate import (
    GUI_ACTION_KINDS, GuiAction, Observation, substrate_registry,
)
from taskvm.verifier.visible import VisibleVerifier

from taskvm.domain.state import ObservedValue, SurfaceEvidence, TaskVariable
from taskvm.domain.intent import TaskIntent
from taskvm.governance.events import GoalPatchRequested
from taskvm.governance.service import GovernanceService
from taskvm.projection.services.governance import KernelGovernancePort


# ── the CUA model adapter (composition-owned, per RuntimePorts docstring) ──

#: B-01: the action protocol is aligned with the FROZEN GuiAction
#: vocabulary (substrate.md §2: GUI_ACTION_KINDS — click|tap|type|key|
#: scroll|wait|open) and its full field set. The model sees ONLY this
#: user-visible vocabulary — never substrate internals.
_CUA_SYSTEM_PROMPT = (
    "你是一个图形界面操作代理（CUA）。每一轮你会看到当前屏幕的截图（若"
    "提供）和清洗后的可见文本，以及一个操作目标。你只做一件事：返回恰"
    "好一个原子 GUI 操作，或声明完成/失败。只使用屏幕上可见的信息。"
    "输出严格 JSON："
    '{"kind":"act","action":{"kind":"click|tap|type|key|scroll|wait|open",'
    '"coordinate":[x,y],"text":"...","key":"...",'
    '"direction":"up|down|left|right","magnitude":0,'
    '"duration_ms":0,"target":"可见的应用名或界面名"}}'
    ' 或 {"kind":"done"} 或 {"kind":"fail","reason":"业务原因"}。'
    "坐标归一化到 [0,1000]。不要输出任何其他内容。"
)

#: B-01: kinds that REQUIRE a coordinate (a click/tap without a target
#: point is unexecutable — an honest fail, never a guess).
_COORD_REQUIRED = frozenset({"click", "tap"})
#: B-01: required scalar/text field per kind (missing ⇒ honest fail).
_REQUIRED_FIELD_BY_KIND = {
    "type": "text", "key": "key", "open": "target",
}
#: B-01: numeric fields coerced with int() — a non-numeric value is an
#: illegal action (honest fail), not a silent drop.
_NUMERIC_FIELDS = ("magnitude", "duration_ms")
_SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})


class HttpCUAModel:
    """``CUAModel`` port over the architect ``HttpModelPort``.

    A-13 single-owner ledger: this adapter declares
    ``records_own_ledger = True`` — it mints a unique ``request_id`` per
    REAL provider request and lands EXACTLY ONE ledger row per request
    on every path (success / unparseable reply / transport exception),
    carrying that ``request_id``. Decisions hand the ``request_id`` back
    so the runtime ANNOTATES the row (node/attempt/repair context)
    instead of appending a second row (C-2: 1 provider request = 1
    ledger row). The pre-flight no-leak check runs BEFORE any provider
    request — a prompt leak issues no request and lands no row (rows
    count provider requests, not harness bugs).

    One ``predict_action`` = one provider request = one ledger record
    (C-2 discipline). The outgoing prompt passes the no-leak gate; a
    parse failure is an honest ``fail`` decision — the runtime's repair
    loop owns any re-ask.
    """

    #: A-13 promise: this adapter owns its ledger rows (see class docstring)
    records_own_ledger = True

    def __init__(self, port: ModelPort | None = None,
                 ledger: ModelCallLedger | None = None) -> None:
        self._port = port or HttpModelPort()
        self._ledger = ledger
        self._requests = 0

    @property
    def request_count(self) -> int:
        """REAL provider requests issued through this adapter (A-13 test
        invariant: this must equal the number of ledger rows the adapter
        owns — and the runtime must add none of its own)."""
        return self._requests

    def predict_action(self, *, goal: str, observation: Observation,
                       labels: Mapping[str, str] | None = None,
                       attempt: int = 1, model: str | None = None,
                       ) -> CUADecision:
        visible = getattr(observation, "visible_text", "") or ""
        retry = (f"\n（第 {attempt} 次重试：上一次操作没有完成任务，请确认修改"
                 "确实生效后再报告完成。）") if attempt > 1 else ""
        user = (f"## 操作目标\n{goal}{retry}\n\n## 屏幕可见文本\n{visible}")
        # B-01: vision-capable path — a fresh screenshot travels as the
        # multimodal image part (HttpModelPort.complete_json's
        # ``image_data_url``), NEVER as prompt text. Only a real data URL
        # ("data:image/…;base64,…) qualifies: an artifact ref / file path /
        # internal locator is NOT sent as an image and NEVER inlined into
        # the prompt (honest text-only degradation, no guessing).
        image = None
        ref = getattr(observation, "screenshot_ref", None)
        if isinstance(ref, str) and ref.startswith("data:image/"):
            image = ref
        try:
            assert_prompt_clean(_CUA_SYSTEM_PROMPT + "\n" + user,
                                what="cua prompt")
        except Exception as e:  # a leak is a harness bug — fail honestly
            # No provider request was issued, so NO ledger row (C-2: rows
            # count real provider requests only — A-13).
            return CUADecision(kind=CUADecisionKind.FAIL,
                               reason="指令生成内部错误，已安全终止")
        request_id = self._mint_request_id()
        try:
            reply = self._port.complete_json(system=_CUA_SYSTEM_PROMPT,
                                             user=user, model=model,
                                             image_data_url=image)
        except Exception as e:
            self._record(request_id, ok=False, error=str(e), model=model)
            raise
        parsed = reply.parsed if isinstance(reply.parsed, dict) else None
        if parsed is None:
            self._record(request_id, ok=False, error="unparseable CUA reply",
                         model=model, reply=reply)
            return CUADecision(kind=CUADecisionKind.FAIL,
                               reason="模型返回无法解析",
                               raw=reply.raw[:200], request_id=request_id)
        decision = _decision_from_json(parsed)
        self._record(request_id, ok=decision.kind is not CUADecisionKind.FAIL,
                     model=model, reply=reply)
        return _replace(decision, request_id=request_id)

    # ── ledger ───────────────────────────────────────────────────────────
    def _mint_request_id(self) -> str:
        self._requests += 1
        return f"cua-{uuid.uuid4().hex[:16]}"

    def _record(self, request_id: str, *, ok: bool, model: str | None,
                error: str = "", reply: Any = None) -> None:
        if self._ledger is None:
            return
        self._ledger.record(ModelCallRecord(
            role=MODEL_ROLE_CUA, purpose="cua.predict_action",
            model=model or str(getattr(self._port, "default_model", "")
                               or ""),
            ok=ok,
            prompt_tokens=getattr(reply, "prompt_tokens", None),
            completion_tokens=getattr(reply, "completion_tokens", None),
            error=error, request_id=request_id))


def _decision_from_json(parsed: Mapping[str, Any]) -> CUADecision:
    """B-01: parse the model's JSON against the FROZEN GuiAction schema.

    Total over ``GUI_ACTION_KINDS`` (click|tap|type|key|scroll|wait|open)
    and every GuiAction field. Missing REQUIRED fields (click/tap without
    a coordinate; type without text; key without key; open without
    target), unknown kinds, or non-numeric numeric fields are HONEST
    FAILs — never a guess, never a silent field drop."""
    kind = str(parsed.get("kind", "")).lower()
    if kind == "done":
        return CUADecision(kind=CUADecisionKind.DONE,
                           raw=str(parsed)[:200])
    if kind == "fail":
        return CUADecision(kind=CUADecisionKind.FAIL,
                           reason=str(parsed.get("reason", ""))[:300],
                           raw=str(parsed)[:200])
    if kind == "act":
        act = parsed.get("action")
        if not isinstance(act, Mapping):
            return CUADecision(kind=CUADecisionKind.FAIL,
                               reason="act 决策缺少 action 对象",
                               raw=str(parsed)[:200])
        act_kind = str(act.get("kind", ""))
        if act_kind not in GUI_ACTION_KINDS:
            return CUADecision(kind=CUADecisionKind.FAIL,
                               reason=f"未知操作类型 {act_kind!r}",
                               raw=str(parsed)[:200])
        # required-field discipline (honest fail, no guessing)
        coord = act.get("coordinate")
        if isinstance(coord, (list, tuple)) and len(coord) == 2:
            try:
                coord = (float(coord[0]), float(coord[1]))
            except (TypeError, ValueError):
                return CUADecision(kind=CUADecisionKind.FAIL,
                                   reason="coordinate 不是数值对",
                                   raw=str(parsed)[:200])
        else:
            coord = None
        if act_kind in _COORD_REQUIRED and coord is None:
            return CUADecision(
                kind=CUADecisionKind.FAIL,
                reason=f"{act_kind} 操作缺少 coordinate",
                raw=str(parsed)[:200])
        req = _REQUIRED_FIELD_BY_KIND.get(act_kind)
        if req is not None and not str(act.get(req, "") or ""):
            return CUADecision(
                kind=CUADecisionKind.FAIL,
                reason=f"{act_kind} 操作缺少必需字段 {req}",
                raw=str(parsed)[:200])
        numeric: dict[str, int] = {}
        for field in _NUMERIC_FIELDS:
            raw_v = act.get(field)
            if raw_v is None:
                continue
            try:
                numeric[field] = int(raw_v)
            except (TypeError, ValueError):
                return CUADecision(
                    kind=CUADecisionKind.FAIL,
                    reason=f"{field} 不是整数",
                    raw=str(parsed)[:200])
        direction = act.get("direction")
        if direction is not None:
            direction = str(direction)
            if act_kind == "scroll" and direction not in _SCROLL_DIRECTIONS:
                return CUADecision(
                    kind=CUADecisionKind.FAIL,
                    reason=f"非法滚动方向 {direction!r}",
                    raw=str(parsed)[:200])
        action = GuiAction(
            kind=act_kind,
            coordinate=coord,
            text=str(act.get("text", "") or "") or None,
            key=str(act.get("key", "") or "") or None,
            direction=direction,
            magnitude=numeric.get("magnitude"),
            duration_ms=numeric.get("duration_ms"),
            target=str(act.get("target", "") or "") or None)
        return CUADecision(kind=CUADecisionKind.ACT, action=action,
                           raw=str(parsed)[:200])
    return CUADecision(kind=CUADecisionKind.FAIL,
                       reason=f"未知决策类型 {kind!r}", raw=str(parsed)[:200])


# ── the observation extractor (deterministic fast path) ───────────────────

_LABEL_VALUE_RE = r"[^|\n]{1,120}?"


class VisibleLabelExtractor:
    """Observation → ``ObservedValue``s from ``label: value`` /
    ``label=value`` visible tokens.

    Deterministic and honest: a variable's label must appear EXACTLY ONCE
    in the visible text followed by a separator — an ambiguous or absent
    label yields NO observation for that variable (never a guess). This
    is the demo/composition fast path; the full ``value_pattern``
    handle-cache path is the architect compile product (C's ownership)
    and is deliberately NOT duplicated here.
    """

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, observation: Observation,
                variables: Mapping[str, TaskVariable],
                ) -> tuple[ObservedValue, ...]:
        self.calls += 1
        text = getattr(observation, "visible_text", "") or ""
        out: list[ObservedValue] = []
        for key, var in variables.items():
            evs = getattr(var, "evidence", ()) or ()
            if not evs:
                continue
            label = getattr(evs[0], "visible_label", "") or ""
            if not label:
                continue
            pattern = (rf"{re.escape(label)}\s*[:：=]\s*"
                       rf"({_LABEL_VALUE_RE})")
            matches = re.findall(pattern, text)
            if len(matches) != 1:
                continue  # absent or ambiguous — honestly skip
            value = matches[0].strip()
            out.append(ObservedValue(
                semantic_key=key, value=value,
                evidence=(SurfaceEvidence(
                    surface=evs[0].surface, visible_label=label,
                    observed_value=value),),
                confidence=1.0))
        return tuple(out)


# ── the observation extractor over the architect compile product (B-07) ───


class HandleCacheExtractor:
    """Runtime extractor OVER the architect compile product.

    ``demo.py`` documents that the full handle-cache compile product
    (architect plane) is the production observation path, while the builtin
    apps render tables / definition lists — ``label: value`` tokens the
    fast-path ``VisibleLabelExtractor`` matches rarely appear there, so the
    OBSERVED plane would freeze at the compiler's initial reading. This
    adapter closes that gap WITHOUT duplicating C's logic: each runtime
    ``Observation`` becomes a one-region ``CompilerObservationView`` and
    every variable re-read goes through ``StateCompiler.extract_observed``
    — the architect's own public static implementation (reuse, not
    re-implementation).

    Honest fail-closed (same discipline as the fast path): a handle whose
    ``surface_label`` does not match the observed surface's display name,
    a handle without a ``value_pattern``, or a pattern that no longer
    matches yields NO observation for that variable — never a guess.
    """

    def __init__(self, handles: Iterable[Any]) -> None:
        self._handles = tuple(handles)
        self.calls = 0

    def extract(self, observation: Observation,
                variables: Mapping[str, TaskVariable],
                ) -> tuple[ObservedValue, ...]:
        self.calls += 1
        surface = getattr(observation, "surface", None)
        label = (getattr(surface, "display_name", "") or ""
                 or getattr(surface, "surface_id", "") or "surface")
        region = VisibleRegion(
            surface_label=label,
            visible_text=getattr(observation, "visible_text", "") or "")
        view = CompilerObservationView(
            revision=int(getattr(observation, "revision", 0) or 0),
            regions=(region,))
        out: list[ObservedValue] = []
        for h in self._handles:
            if getattr(h, "semantic_key", "") not in variables:
                continue
            if not getattr(h, "value_pattern", ""):
                continue
            if getattr(h, "surface_label", "") and h.surface_label != label:
                continue   # the handle is bound to a different surface
            ov = StateCompiler.extract_observed(view, h)
            if ov is not None:
                out.append(ov)
        return tuple(out)


# ── A-01: the production multi-surface binding resolver ───────────────────


class GovernanceServicePort:
    """The production governance port for the projection routes — the
    GoalPatch wiring the frozen contracts mandate (projection.md §1:
    "GoalPatch recomposition (architect-owned, **injected**)").

    Every command EXCEPT goal_patch delegates to the stock
    ``KernelGovernancePort`` (byte-identical kernel-only semantics —
    LocalPatch / checkpoint / rollback / conflict are 0-model-call
    kernel commands, single governance write each). ``goal_patch``
    routes through ``GovernanceService.handle`` so the public route runs
    the FROZEN chain:

        apply_goal_patch (invalidate + block)
        → ONE architect.recompose_future
        → kernel.recompose (atomic close + unblock)

    (architect contract §5 model-call table: GoalPatchRequested →
    0 compiler / 1 architect.) A failed recomposition raises
    ``GoalRecomposeFailed`` (a ValidationError → HTTP 409) with
    ``pending_recompose`` honestly left set for a later
    ``retry_goal_recompose`` — never a fallback plan, never a silent
    reseed.
    """

    def __init__(self, service: GovernanceService,
                 kernel: Any) -> None:
        self._service = service
        self._kernel_port = KernelGovernancePort(kernel)

    # ── kernel-only commands: identical to the default port ────────────
    def local_patch(self, updates: dict[str, Any],
                    rationale: str = "") -> dict[str, Any]:
        return self._kernel_port.local_patch(updates, rationale=rationale)

    def checkpoint(self, label: str) -> dict[str, Any]:
        return self._kernel_port.checkpoint(label)

    def rollback(self, target_checkpoint_id: str,
                 rationale: str = "") -> dict[str, Any]:
        return self._kernel_port.rollback(target_checkpoint_id,
                                          rationale=rationale)

    def resolve_conflict(self, conflict_id: str, resolution: str,
                         detail: str = "") -> dict[str, Any]:
        return self._kernel_port.resolve_conflict(
            conflict_id, resolution, detail=detail)

    # ── the frozen GoalPatch closure chain (architect-injected) ────────
    def goal_patch(self, *, goal: str,
                   constraints: Iterable[str] = (),
                   scope: Iterable[str] = (),
                   success_criteria: Iterable[str] = (),
                   rationale: str = "") -> dict[str, Any]:
        new_intent = TaskIntent(
            goal=goal,
            constraints=tuple(constraints),
            scope=tuple(scope),
            success_criteria=tuple(success_criteria))
        outcome = self._service.handle(GoalPatchRequested(
            new_intent=new_intent, rationale=rationale))
        detail = dict(outcome.detail)
        detail["epoch"] = outcome.epoch
        detail["recompose_closed"] = (            # closed by construction
            self._service._kernel.pending_recompose is None)
        return {"ok": True, "action": "goal_patch", "result": detail}


class EvidenceSurfaceResolver:
    """The production ``SurfaceBindingResolver`` (A-01) — composition-owned
    private glue mapping compiler-minted opaque handle ids to session
    surface ids over the frozen provenance chain:

        compiler handle_id
          → CompilerResult.handle_evidence.surface_label
          → bootstrap's VisibleRegion.surface_label (= SurfaceInfo
            .display_name or surface_id — what a real user would call the
            window)
          → SubstrateSession.list_surfaces().surface_id

    The domain ``SurfaceHandle`` stays frozen (an opaque id only — a real
    surface_id is never written into it); this mapping lives entirely in
    composition glue, exactly where substrate.md §1 says substrate
    selection/binding happens. The architect already reuses the compiler's
    handle for a target_evidence label it has seen (``architect.py
    _action_handle``), so ONE mapping serves both the contract-evidence and
    variable-evidence resolution paths in the runtime.

    Fail-closed (A-01): a handle resolves ONLY when the boot-time snapshot
    still holds — the bound surface_id still exists, still answers to the
    same visible label, and NO other surface claims that label. A surface
    that disappeared, was recreated under a new id, was renamed, a label
    conflict, or an unknown handle all return ``None`` — the runtime then
    lands an honest StructureInvalidated / fail, NEVER a ``surfaces[0]``
    guess. Every check is a read-only ``list_surfaces()`` — 0 model calls.
    """

    def __init__(self, substrate: Any,
                 handle_labels: Mapping[str, str],
                 bound_surfaces: Mapping[str, str]) -> None:
        #: handle_id -> surface_label (from CompilerResult.handle_evidence)
        self._handle_labels = dict(handle_labels)
        #: surface_label -> surface_id (the bootstrap-time snapshot, i.e.
        #: the same labels the VisibleRegions carried to the compiler)
        self._bound = dict(bound_surfaces)
        self._substrate = substrate
        self.asks: list[tuple[str, str]] = []   # audit trail (test-facing)

    def resolve_surface(self, handle_id: str, *, visible_label: str = ""
                        ) -> str | None:
        self.asks.append((handle_id, visible_label))
        label = self._handle_labels.get(handle_id)
        if not label:
            return None          # unknown handle — no provenance, no guess
        bound_sid = self._bound.get(label)
        if not bound_sid:
            return None          # the label never bound a surface at bootstrap
        try:
            infos = self._substrate.list_surfaces()   # fresh, read-only
        except Exception:
            return None          # an unreachable substrate is a routing failure
        claimants = [i.surface_id for i in infos
                     if (i.display_name or i.surface_id) == label]
        if claimants != [bound_sid]:
            return None          # gone / recreated / renamed / label conflict
        return bound_sid


# ── the bundle builder — the ONE compose_runtime call site ────────────────

def build_runtime_ports(*, model_port: ModelPort | None = None,
                        ledger: ModelCallLedger | None = None,
                        cua_model: Any = None,
                        extractor: Any = None,
                        ) -> RuntimePorts:
    """Assemble the five injected ports. Pass ``ledger`` to SHARE one
    instance with the architect (the unified call report); pass
    ``cua_model``/``extractor`` to override with test fakes.

    The ``cast`` below documents the seam's duck-typing contract:
    ``architect.ModelCallLedger`` and ``runtime.ports.CallLedger`` declare
    field-for-field identical ``ModelCallRecord``s (two parallel frozen
    dataclasses — a nominal-type checker cannot see that identity, the
    ledger only string-validates ``role`` at runtime)."""
    ledger = ledger if ledger is not None else ModelCallLedger()
    return RuntimePorts(
        serializer=ActionContractSerializer(),
        cua_model=cua_model or HttpCUAModel(
            port=model_port or HttpModelPort(), ledger=ledger),
        extractor=extractor or VisibleLabelExtractor(),
        verifier=VisibleVerifier(),
        ledger=cast(CallLedger, ledger))


def compose_task_runtime(kernel: Any, *, host: str = "localhost",
                         sid: str = "", app: str | None = None,
                         base_url: str | None = None,
                         substrate: Any = None,
                         ports: RuntimePorts | None = None,
                         surfaces: list[str] | None = None,
                         model: str | None = None,
                         budgets: Any = None,
                         surface_resolver: Any = None) -> Any:
    """Build a real ``AutonomyRuntime`` for one task session — the single
    production call of ``compose_runtime`` (substrate.md §8 T1 leg-1).

    ``substrate`` may be injected (tests / alternate providers). By
    default a ``builtin_web`` ``SubstrateSession`` is created through the
    substrate registry from the provider's own URL knowledge.
    ``surface_resolver`` (A-01) is the composition-owned
    ``SurfaceBindingResolver`` (see ``EvidenceSurfaceResolver``); ``None``
    keeps single-surface sessions working (routing-trivial) while a
    multi-surface session without a resolver fails honestly in the
    runtime instead of guessing.
    """
    if substrate is None:
        # §6-clean: NO concrete provider import — the builtin_web provider
        # resolves its own app URL from (app, host[, port][, base_url]);
        # passing the raw keys through the registry is the whole job of a
        # composition root.
        cfg: dict[str, Any] = {"app": app or "calendar", "host": host,
                               "sid": sid}
        if base_url is not None:
            cfg["base_url"] = base_url
        substrate = substrate_registry.create_session("builtin_web", cfg)
    return compose_runtime(kernel, substrate,
                           ports or build_runtime_ports(),
                           budgets=budgets, surfaces=surfaces, model=model,
                           surface_resolver=surface_resolver)


# re-exported for composition consumers (view building is architect-free)
__all__ = [
    "HttpCUAModel", "VisibleLabelExtractor", "HandleCacheExtractor",
    "EvidenceSurfaceResolver",
    "build_runtime_ports", "compose_task_runtime",
    "bootstrap_real_full",
    "CompilerObservationView", "VisibleRegion",
]


# ── B-07: the genuine real-full composition bootstrap ─────────────────────

def bootstrap_real_full(*, goal: str, sid: str,
                         substrate: Any = None,
                         model_port: Any = None,
                         ledger: ModelCallLedger | None = None,
                         store: Any = None,
                         model: str | None = None,
                         cua_model: Any = None,
                         extractor: Any = None,
                         app: str | None = None, host: str = "localhost",
                         base_url: str | None = None,
                         surfaces: list[str] | None = None,
                         success_criteria: tuple[str, ...] = ()) -> dict:
    """Natural-language goal → fresh observation → StateCompiler →
    TaskArchitect → Kernel → shared ledger → AutonomyRuntime → (optional)
    registered projection session — with ZERO hand-built intermediates.

    This is the RM-0.B real-full path (B-07). Every stage is the REAL
    production object; nothing is hand-assembled:

      1. the NL ``goal`` becomes a ``TaskIntent``;
      2. the substrate's own ``list_surfaces()`` declares the world;
      3. each (selected) surface gets a FRESH ``observe()``;
      4. observations convert to ``CompilerObservationView`` regions
         (visible-only: label/text/fingerprint; a screenshot travels ONLY
         if it is a real ``data:image/…`` URL — paths/ids are dropped,
         never inlined);
      5. ``StateCompiler`` makes its REAL model call #1 (observed plane);
      6. ``TaskArchitect`` makes its REAL model call #2 (variables+
         workflow, validated);
      7. the kernel initializes FROM THE ARCHITECT PRODUCT
         (``init_task_state(arch.variables)`` + ``set_plan(arch.graph)``)
         — never a hand-written TaskVariable/WorkflowGraph;
      8. ONE shared ``ModelCallLedger`` is injected everywhere (compiler +
         architect rows from steps 5-6, CUA rows from the runtime) so
         1 provider request = 1 ledger row across all three roles;
      9. ``compose_task_runtime`` assembles the real AutonomyRuntime
         (same composition root as the demo/production);
     10. with a ``store`` the session registers in the projection — the
         subsequent lifecycle (driver start → CUA → real GUI → verifier →
         SSE) is driven through the PUBLIC governance routes, not here.

    ``model_port`` may inject a scripted port for CONTRACT-WIRING tests;
    with the default ``HttpModelPort`` every call is a real provider
    request (provider availability is the caller's environment concern).
    """
    from taskvm.domain.intent import TaskIntent
    from taskvm.kernel import TaskVMKernel

    intent = TaskIntent(goal=goal, success_criteria=tuple(success_criteria))

    # (2) the substrate declares the world (registry-routed builtin_web by
    # default — the provider resolves its own URL/config, §6-clean)
    if substrate is None:
        cfg: dict[str, Any] = {"app": app or "calendar", "host": host,
                               "sid": sid}
        if base_url is not None:
            cfg["base_url"] = base_url
        substrate = substrate_registry.create_session("builtin_web", cfg)
    surface_infos = substrate.list_surfaces()

    # (3-4) fresh observe → visible-only compiler view. The SAME label
    # mapping (display_name-or-surface_id → surface_id) becomes the A-01
    # binding snapshot the production resolver re-validates live at every
    # resolve — one source of truth for the provenance chain.
    wanted = set(surfaces) if surfaces else None
    regions: list[VisibleRegion] = []
    bound_surfaces: dict[str, str] = {}
    revision = 0
    for info in surface_infos:
        if wanted is not None and info.surface_id not in wanted:
            continue
        label = info.display_name or info.surface_id
        obs = substrate.observe(info)
        revision = max(revision, int(getattr(obs, "revision", 0) or 0))
        shot = getattr(obs, "screenshot_ref", None)
        regions.append(VisibleRegion(
            surface_label=label,
            visible_text=getattr(obs, "visible_text", "") or "",
            structure_fingerprint=getattr(obs, "fingerprint", "") or "",
            screenshot_data_url=(shot if isinstance(shot, str)
                                 and shot.startswith("data:image/") else None)))
        # duplicate labels last-write-win here; the resolver's LIVE
        # uniqueness check is the authority (a duplicated label can never
        # resolve — fail closed, never a guess)
        bound_surfaces[label] = info.surface_id
    view = CompilerObservationView(revision=revision,
                                   regions=tuple(regions))

    # (5-8) real compiler → real architect → kernel FROM THE PRODUCTS,
    # with ONE shared ledger for compiler/architect/CUA rows alike.
    # NOTE: ``is not None`` (not ``or``) — an EMPTY ModelCallLedger is
    # falsy (__len__), and ``or`` would silently mint a second ledger,
    # splitting the single-owner accounting across two objects.
    if ledger is None:
        ledger = ModelCallLedger()
    port = model_port or HttpModelPort()
    compiler = StateCompiler(port, ledger, model=model)
    architect = TaskArchitect(port, ledger, model=model)
    compiler_result = compiler.compile(view, intent)
    arch = architect.compose(intent, compiler_result)
    kernel = TaskVMKernel(sid, intent)
    kernel.init_task_state(arch.variables)
    if arch.graph is not None:
        kernel.set_plan(arch.graph)
    # B-07 observed-plane wiring: the compiler product's handle cache (the
    # deterministic ``value_pattern`` re-reads) IS the production observation
    # path for the runtime — handed to the runtime seam through a
    # composition adapter that reuses ``StateCompiler.extract_observed``.
    # No handle evidence (or an explicit ``extractor``) falls back to the
    # label-token fast path — honest degradation, never a guess.
    if extractor is None and compiler_result.handle_evidence:
        extractor = HandleCacheExtractor(compiler_result.handle_evidence)
    # A-01: the production multi-surface resolver — compiler handle_id →
    # evidence surface_label → the bootstrap binding snapshot → the
    # session's surface ids, re-validated live (read-only) per resolve.
    # Multi-surface sessions route by evidence; single-surface sessions
    # stay routing-trivial either way; unresolvable bindings fail closed.
    surface_resolver = EvidenceSurfaceResolver(
        substrate,
        handle_labels={he.handle.handle_id: he.surface_label
                       for he in compiler_result.handle_evidence},
        bound_surfaces=bound_surfaces)
    ports = build_runtime_ports(model_port=port, ledger=ledger,
                                cua_model=cua_model, extractor=extractor)
    runtime = compose_task_runtime(kernel, substrate=substrate,
                                   ports=ports, model=model,
                                   surface_resolver=surface_resolver)

    # (10) optional projection registration — the PUBLIC lifecycle takes
    # over from here (POST /governance/start → driver → runtime → GUI).
    # GoalPatch wiring (audit 2026-08-19): the session is registered with
    # the GovernanceService-backed governance port, so the public
    # goal_patch route runs the FROZEN closure chain (apply_goal_patch →
    # ONE architect.recompose_future → kernel.recompose → unblock)
    # instead of the kernel-only default that left pending_recompose set
    # and execution blocked forever (projection.md §1: recomposition is
    # "architect-owned, injected" — this is the injection).
    governance_service = GovernanceService(
        kernel, architect=architect, compiler=compiler, ledger=ledger)
    if store is not None:
        from taskvm.projection.store import SurfaceDecl
        decls = tuple(
            SurfaceDecl(surface_id=info.surface_id,
                        display_name=info.display_name or info.surface_id)
            for info in surface_infos)
        store.register(sid, kernel, runtime=runtime, surfaces=decls,
                       governance=GovernanceServicePort(
                           governance_service, kernel))

    return dict(sid=sid, intent=intent, kernel=kernel, runtime=runtime,
                substrate=substrate, ledger=ledger,
                model_port=port, compiler=compiler, architect=architect,
                compiler_result=compiler_result,
                architecture=arch, surfaces=surface_infos,
                surface_resolver=surface_resolver,
                governance_service=governance_service)
