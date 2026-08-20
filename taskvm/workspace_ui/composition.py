"""taskvm.workspace_ui.composition — the composition root for the real
runtime (substrate.md §8 T1 leg-1).

This module is the production composition root and the **single
production call site** of ``taskvm.runtime.bootstrap.compose_runtime``
(RFC-001): the
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
  product — routing that here would duplicate the architect's ownership).
- ``verifier``    — ``taskvm.verifier.visible.VisibleVerifier`` (the
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

import hashlib
import re
import threading
import uuid
from dataclasses import replace as _dc_replace, replace as _replace
from typing import Any, Callable, Iterable, Mapping, cast

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
from taskvm.skills.loader import inject_skill
from taskvm.verifier.visible import VisibleVerifier
from taskvm.workspace_ui.verifier_escalation import build_escalating_verifier

from taskvm.domain.state import ObservedValue, SurfaceEvidence, TaskVariable
from taskvm.domain.intent import TaskIntent
from taskvm.governance.events import GoalPatchRequested
from taskvm.governance.service import GovernanceService
from taskvm.projection.services.governance import KernelGovernancePort


# ── the CUA model adapter (composition-owned, per RuntimePorts docstring) ──

#: The action protocol is aligned with the FROZEN GuiAction
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

#: Kinds that REQUIRE a coordinate (a click/tap without a target
#: point is unexecutable — an honest fail, never a guess).
_COORD_REQUIRED = frozenset({"click", "tap"})
#: Required scalar/text field per kind (missing ⇒ honest fail).
_REQUIRED_FIELD_BY_KIND = {
    "type": "text", "key": "key", "open": "target",
}
#: Numeric fields coerced with int() — a non-numeric value is an
#: illegal action (honest fail), not a silent drop.
_NUMERIC_FIELDS = ("magnitude", "duration_ms")
_SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})


class CUAReplySchemaError(RuntimeError):
    """The provider REPLIED but the reply violates the frozen CUA
    decision schema (unparseable JSON, unknown decision kind, unknown
    action kind, missing required fields, malformed values).

    ``HttpCUAModel.predict_action`` lands its ledger row FIRST, then
    raises this — the runtime's §5 invalid-prediction loops (forward
    autonomy AND compensation alike) own the bounded re-ask. A schema
    slip is a malformed REPLY, never a business FAIL decision: the
    GATE-G0 2026-08-20 postmortem shows the old FAIL conversion
    killing a whole trial on ONE slip (the model answered
    ``{"kind":"tap",…}`` — a GUI action kind in the decision slot —
    and the node died as ``cua reported fail: 未知决策类型 'tap'``
    with zero retries, so the forward witnesses never landed). A
    DELIBERATE ``{"kind":"fail","reason":…}`` from the model is a
    business judgment and still lands as a FAIL decision."""


class HttpCUAModel:
    """``CUAModel`` port over the architect ``HttpModelPort``.

    Single-owner ledger: this adapter declares
    ``records_own_ledger = True`` — it mints a unique ``request_id`` per
    REAL provider request and lands EXACTLY ONE ledger row per request
    on every path (success / unparseable reply / transport exception),
    carrying that ``request_id``. Decisions hand the ``request_id`` back
    so the runtime ANNOTATES the row (node/attempt/repair context)
    instead of appending a second row (1 provider request = 1
    ledger row). The pre-flight no-leak check runs BEFORE any provider
    request — a prompt leak issues no request and lands no row (rows
    count provider requests, not harness bugs).

    One ``predict_action`` = one provider request = one ledger record.
    The outgoing prompt passes the no-leak gate; a reply that violates
    the decision schema raises :class:`CUAReplySchemaError` AFTER the
    row lands — the runtime's §5 invalid-prediction loop owns the
    bounded re-ask (a DELIBERATE model ``fail`` decision still lands
    as an honest FAIL decision).
    """

    #: Promise: this adapter owns its ledger rows (see class docstring)
    records_own_ledger = True

    def __init__(self, port: ModelPort | None = None,
                 ledger: ModelCallLedger | None = None) -> None:
        self._port = port or HttpModelPort()
        self._ledger = ledger
        self._requests = 0

    @property
    def request_count(self) -> int:
        """REAL provider requests issued through this adapter (test
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
        # Vision-capable path — a fresh screenshot travels as the
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
            # the skill injection (R2.5) happens BEFORE the gate, so a
            # distilled skill is scanned like any other prompt text
            assert_prompt_clean(
                inject_skill("cua", _CUA_SYSTEM_PROMPT) + "\n" + user,
                what="cua prompt")
        except Exception as e:  # a leak is a harness bug — fail honestly
            # No provider request was issued, so NO ledger row (rows
            # count real provider requests only).
            return CUADecision(kind=CUADecisionKind.FAIL,
                               reason="指令生成内部错误，已安全终止")
        request_id = self._mint_request_id()
        try:
            reply = self._port.complete_json(
                system=inject_skill("cua", _CUA_SYSTEM_PROMPT),
                user=user, model=model,
                image_data_url=image)
        except Exception as e:
            self._record(request_id, ok=False, error=str(e), model=model)
            raise
        parsed = reply.parsed if isinstance(reply.parsed, dict) else None
        if parsed is None:
            self._record(request_id, ok=False, error="unparseable CUA reply",
                         model=model, reply=reply)
            raise CUAReplySchemaError("模型返回无法解析（不是 JSON 对象）")
        try:
            decision = _decision_from_json(parsed)
        except CUAReplySchemaError as e:
            # the row lands BEFORE the raise: one provider request = one
            # row on every path (same discipline as the transport
            # exception path above)
            self._record(request_id, ok=False, error=str(e),
                         model=model, reply=reply)
            raise
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
    """Parse the model's JSON against the FROZEN GuiAction schema.

    Total over ``GUI_ACTION_KINDS`` (click|tap|type|key|scroll|wait|open)
    and every GuiAction field. Missing REQUIRED fields (click/tap without
    a coordinate; type without text; key without key; open without
    target), unknown kinds, or non-numeric numeric fields RAISE
    :class:`CUAReplySchemaError` — a malformed reply is an invalid
    prediction the runtime re-asks within its small §5 ceiling, never
    a business FAIL decision (and never a guess, never a silent field
    drop). Only the model's DELIBERATE ``{"kind":"fail"}`` is a FAIL
    decision."""
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
            raise CUAReplySchemaError("act 决策缺少 action 对象")
        act_kind = str(act.get("kind", ""))
        if act_kind not in GUI_ACTION_KINDS:
            raise CUAReplySchemaError(f"未知操作类型 {act_kind!r}")
        # required-field discipline (honest fail, no guessing)
        coord = act.get("coordinate")
        if isinstance(coord, (list, tuple)) and len(coord) == 2:
            try:
                coord = (float(coord[0]), float(coord[1]))
            except (TypeError, ValueError):
                raise CUAReplySchemaError("coordinate 不是数值对")
        else:
            coord = None
        if act_kind in _COORD_REQUIRED and coord is None:
            raise CUAReplySchemaError(f"{act_kind} 操作缺少 coordinate")
        req = _REQUIRED_FIELD_BY_KIND.get(act_kind)
        if req is not None and not str(act.get(req, "") or ""):
            raise CUAReplySchemaError(
                f"{act_kind} 操作缺少必需字段 {req}")
        numeric: dict[str, int] = {}
        for field in _NUMERIC_FIELDS:
            raw_v = act.get(field)
            if raw_v is None:
                continue
            try:
                numeric[field] = int(raw_v)
            except (TypeError, ValueError):
                raise CUAReplySchemaError(f"{field} 不是整数")
        direction = act.get("direction")
        if direction is not None:
            direction = str(direction)
            if act_kind == "scroll" and direction not in _SCROLL_DIRECTIONS:
                raise CUAReplySchemaError(f"非法滚动方向 {direction!r}")
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
    raise CUAReplySchemaError(f"未知决策类型 {kind!r}")


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


# ── the observation extractor over the architect compile product ───


class HandleCacheExtractor:
    """Runtime extractor OVER the architect compile product.

    ``demo.py`` documents that the full handle-cache compile product
    (architect plane) is the production observation path, while the builtin
    apps render tables / definition lists — ``label: value`` tokens the
    fast-path ``VisibleLabelExtractor`` matches rarely appear there, so the
    OBSERVED plane would freeze at the compiler's initial reading. This
    adapter closes that gap WITHOUT duplicating the architect's logic: each runtime
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


# ── the production multi-surface binding resolver ───────────────────


class AdaptiveExtractor:
    """HandleCache fast path + runtime handle re-minting for variables the
    INITIAL compile could not see (GATE-G0 2026-08-20 r6 postmortem).

    The initial compile runs on the HOME SCREEN at bootstrap; variables
    that only exist INSIDE an app (the X search phrase, per-post like/
    bookmark toggles) carry ``evidence: []`` there — no handle, so the
    frozen HandleCache fast path can NEVER observe them and every
    contract referencing one fails-closed forever (r6: n002's
    ``desired_state: post_search_phrase`` stayed 'unmet' on a screen
    where the CUA had ALREADY typed the phrase into the search box —
    the VALUE was visible, the HANDLE was missing). This adapter closes
    that gap WITHOUT touching the frozen runtime/compiler: a wanted
    variable with no cached handle triggers the architect's OWN
    ``StateCompiler.compile`` over the fresh observation (incremental —
    ``prior_state`` keeps the semantic key names), mints and caches the
    handles; re-reads then use the deterministic
    ``StateCompiler.extract_observed`` fast path. Cost: ONE compiler
    call per NEW screen (fingerprint-keyed mint with a per-screen
    failure cooldown). ``compiler=None`` degrades to the plain
    HandleCache fast path.
    """

    def __init__(self, handles: Iterable[Any], compiler: Any = None,
                 intent: Any = None, prior_variables: Any = ()) -> None:
        self._fast = HandleCacheExtractor(handles)
        self._by_key: dict[str, Any] = {
            getattr(h, "semantic_key", ""): h for h in handles}
        self._compiler = compiler
        self._intent = intent
        self._prior = tuple(prior_variables or ())
        self._minted: dict[str, Any] = {}
        self._mint_failed: set[tuple[str, str]] = set()
        self.calls = 0

    def extract(self, observation: Observation,
                variables: Mapping[str, TaskVariable],
                ) -> tuple[ObservedValue, ...]:
        self.calls += 1
        values = list(self._fast.extract(observation, variables))
        seen = {v.semantic_key for v in values}
        wanted = [k for k in variables
                  if k not in seen and k not in self._by_key]
        if not (wanted and self._compiler is not None
                and self._intent is not None):
            return tuple(values)
        surface = getattr(observation, "surface", None)
        label = (getattr(surface, "display_name", "") or ""
                 or getattr(surface, "surface_id", "") or "surface")
        text = getattr(observation, "visible_text", "") or ""
        fp = str(getattr(observation, "fingerprint", "") or "")
        region = VisibleRegion(surface_label=label, visible_text=text)
        view = CompilerObservationView(
            revision=int(getattr(observation, "revision", 0) or 0),
            regions=(region,))
        self._mint(view, fp, wanted)
        for key in wanted:
            h = self._minted.get(key)
            if h is None:
                continue
            if getattr(h, "surface_label", "") and h.surface_label != label:
                continue   # same surface-binding discipline as the fast path
            ov = StateCompiler.extract_observed(view, h)
            if ov is not None:
                values.append(ov)
        return tuple(values)

    def _mint(self, view: Any, fingerprint: str,
              missing_keys: list[str]) -> None:
        """ONE incremental recompile mints handles for ALL missing keys on
        this screen; the (key, fingerprint) cooldown keeps a screen that
        yielded nothing from re-calling the model."""
        pending = [k for k in missing_keys
                   if k not in self._minted
                   and (k, fingerprint) not in self._mint_failed]
        if not pending:
            return
        try:
            from taskvm.domain.state import TaskState
            prior = (TaskState(intent=self._intent, variables=self._prior)
                     if self._prior else None)
            result = self._compiler.compile(
                view, self._intent, prior_state=prior,
                purpose="runtime_handle_remint")
        except Exception:
            for k in pending:
                self._mint_failed.add((k, fingerprint))
            return
        for h in getattr(result, "handle_evidence", ()) or ():
            k = getattr(h, "semantic_key", "")
            if k in pending and getattr(h, "value_pattern", ""):
                self._minted[k] = h
        for k in pending:
            if k not in self._minted:
                self._mint_failed.add((k, fingerprint))


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
    """The production ``SurfaceBindingResolver`` — composition-owned
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

    Fail-closed: a handle resolves ONLY when the boot-time snapshot
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


# ── screenshot archiving runtime (keep multi-MB data URLs out
#    of the projection plane — composition-owned, opt-in) ─────────────────


class ScreenshotArchivingRuntime:
    """Proxy over a real AutonomyRuntime that keeps screenshot data URLs
    OUT of the projection plane while preserving the bytes as artifacts.

    Why this exists (see eval_results/latency_audit_20260819): every
    ``ACTION_OBSERVED`` / ``ACTION_LANDED`` runtime event carries the full
    ``data:image/png;base64,…`` screenshot (~2.6 MB) in ``artifact_ref``;
    the frozen SSE envelope serialises that field verbatim, so ONE runtime
    tick pushes a 2.6 MB frame down the ordered SSE stream — every byte
    behind it (the 105-byte governance acks included) starves on slow
    links. This proxy is the composition seam's countermeasure:

    * ``runtime_events()`` returns events whose ``artifact_ref`` is a
      compact token (``shot-<n>``) instead of the data URL;
    * the decoded bytes are pushed into the session's ``ArtifactStore``
      under that token, so the FROZEN artifact route serves them unchanged
      and ``surface_cards`` finds a real ``latest_artifact_ref`` for the
      first time;
    * an optional ``on_screenshot`` sink receives ``(token, mime, data)``
      so the APP shell can run its live-phone/thumbnail side channel.

    Opt-in only: constructed exclusively by ``bootstrap_real_full`` when a
    ``screenshot_sink`` is passed (the APP shell does; the bench factory
    passes nothing and gets the unwrapped runtime). Everything
    else is transparently forwarded to the wrapped runtime.
    """

    def __init__(self, inner: Any, artifacts: Any,
                 on_screenshot: Callable[[str, str, bytes], None] | None = None
                 ) -> None:
        self._inner = inner
        self._artifacts = artifacts
        self._on_screenshot = on_screenshot
        self._lock = threading.Lock()
        self._seq = 0
        self._seen = 0            # events already transformed
        self._transformed: list[Any] = []   # token-bearing copies (append-only)

    # ── the transformed read the projection consumes ────────────────────
    def runtime_events(self) -> tuple[Any, ...]:
        with self._lock:
            raw = tuple(self._inner.runtime_events())
            for ev in raw[self._seen:]:
                ref = getattr(ev, "artifact_ref", "") or ""
                if isinstance(ref, str) and ref.startswith("data:image/"):
                    self._seq += 1
                    token = f"shot-{self._seq:04d}"
                    decoded = _decode_image_data_url(ref)
                    if decoded is not None:
                        mime, data = decoded
                        try:
                            self._artifacts.put(token, data, mime=mime)
                        except Exception:
                            self._transformed.append(ev)
                            self._seen += 1
                            continue
                        if self._on_screenshot is not None:
                            try:
                                self._on_screenshot(token, mime, data)
                            except Exception:
                                pass   # the sink is observability, never a
                                       # failure path for the runtime
                        self._transformed.append(
                            _dc_replace(ev, artifact_ref=token))
                    else:
                        self._transformed.append(ev)
                else:
                    self._transformed.append(ev)
                self._seen += 1
            return tuple(self._transformed)

    # ── everything else forwards to the real runtime ────────────────────
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _decode_image_data_url(ref: str) -> tuple[str, bytes] | None:
    """``data:image/…;base64,XXX`` → ``(mime, bytes)``; ``None`` if the
    payload does not decode (the event then passes through untouched)."""
    try:
        import base64
        head, b64 = ref.split(",", 1)
        mime = head[5:].split(";", 1)[0] or "image/png"
        return mime, base64.b64decode(b64)
    except Exception:
        return None


def artifact_fingerprint(data: bytes) -> str:
    """A short content fingerprint for the APP's dedup-by-unchanged-screen
    screenshot route (md5 hex, first 16 chars — dedup only, not security)."""
    return hashlib.md5(data).hexdigest()[:16]


# ── the bundle builder — the ONE compose_runtime call site ────────────────

def build_runtime_ports(*, model_port: ModelPort | None = None,
                        ledger: ModelCallLedger | None = None,
                        cua_model: Any = None,
                        extractor: Any = None,
                        verifier: Any = None) -> RuntimePorts:
    """Assemble the five injected ports. Pass ``ledger`` to SHARE one
    instance with the architect (the unified call report); pass
    ``cua_model``/``extractor`` to override with test fakes.

    The verifier default is the ESCALATION route (owner order
    2026-08-20): deterministic ``VisibleVerifier`` first, then — only on
    a rule-unresolvable mismatch — the R4 ``ModelVerifier`` as the final
    judge (rules never veto the model; the per-app vocabulary route is
    deleted). Env-gated like every real-model path: without
    ``OPENAI_API_KEY`` the default is the plain deterministic verifier.
    Pass ``verifier`` to force either behavior in tests.

    The ``cast`` below documents the seam's duck-typing contract:
    ``architect.ModelCallLedger`` and ``runtime.ports.CallLedger`` declare
    field-for-field identical ``ModelCallRecord``s (two parallel frozen
    dataclasses — a nominal-type checker cannot see that identity, the
    ledger only string-validates ``role`` at runtime)."""
    ledger = ledger if ledger is not None else ModelCallLedger()
    port = model_port or HttpModelPort()
    if verifier is None:
        verifier = build_escalating_verifier(port=port, ledger=ledger)
    return RuntimePorts(
        serializer=ActionContractSerializer(),
        cua_model=cua_model or HttpCUAModel(port=port, ledger=ledger),
        extractor=extractor or VisibleLabelExtractor(),
        verifier=verifier,
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
    ``surface_resolver`` is the composition-owned
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
    "HttpCUAModel", "CUAReplySchemaError", "VisibleLabelExtractor",
    "HandleCacheExtractor", "AdaptiveExtractor",
    "EvidenceSurfaceResolver",
    "ScreenshotArchivingRuntime", "artifact_fingerprint",
    "DEFAULT_ROLE_MODELS", "parse_role_models", "resolve_role_models",
    "build_runtime_ports", "compose_task_runtime",
    "bootstrap_real_full",
    "CompilerObservationView", "VisibleRegion",
]


# ── role→model routing table (workplan §20.2; scaffold — A6 wires the
#    small-model slots, the main chain never degrades) ────────────────────

#: Every model-consuming role TaskVM defines, with its routing default.
#: ``None`` means "the ModelPort default" (gpt-5.6-sol today) — for the
#: main chain (compiler/architect/cua) this is ALSO the policy: those roles
#: are never routed to a cheaper model (bench alignment). ``intent_parser``
#: and ``nl_polisher`` are presentation-layer slots (workplan §20.2): they
#: may be routed to a small fast model when A6 wires them; until then the
#: slots exist so configuration, validation and ledger accounting have a
#: single home. ``genui_decoder`` defaults to the port model and MAY be
#: routed down (A4).
DEFAULT_ROLE_MODELS: dict[str, str | None] = {
    "state_compiler": None,
    "task_architect": None,
    "cua": None,
    "genui_decoder": None,
    "intent_parser": None,
    "nl_polisher": None,
}


def parse_role_models(spec: str) -> dict[str, str]:
    """Parse ``"role=model,role=model"`` into ``{role: model}``.

    Unknown roles raise ``ValueError`` (fail closed — a typo in a routing
    config must never silently no-op). Values may not be empty.
    """
    out: dict[str, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(
                f"role-model entry {part!r} is not 'role=model'")
        role, model = (s.strip() for s in part.split("=", 1))
        if role not in DEFAULT_ROLE_MODELS:
            raise ValueError(
                f"unknown model role {role!r} (known: "
                f"{', '.join(sorted(DEFAULT_ROLE_MODELS))})")
        if not model:
            raise ValueError(f"role {role!r} has an empty model")
        out[role] = model
    return out


def resolve_role_models(
        override: Mapping[str, str] | None = None) -> dict[str, str | None]:
    """The effective routing table: defaults overlaid with ``override``.

    Sources (in priority order, wired by the caller): explicit mapping →
    ``TASKVM_ROLE_MODELS`` env (``role=model,...``) → all-port-default.
    """
    import os
    table = dict(DEFAULT_ROLE_MODELS)
    merged: dict[str, str] = {}
    env_spec = os.environ.get("TASKVM_ROLE_MODELS", "").strip()
    if env_spec:
        merged.update(parse_role_models(env_spec))
    if override:
        unknown = set(override) - set(DEFAULT_ROLE_MODELS)
        if unknown:
            raise ValueError(f"unknown model roles: {sorted(unknown)}")
        merged.update(dict(override))
    table.update(merged)
    return table


def _notify_stage(on_stage: Callable[[str, Any], None] | None,
                  stage: str, product: Any) -> None:
    """Best-effort stage notification (§20.1 progressive-plane signals):
    a watcher failure is observability noise, never a pipeline failure."""
    if on_stage is None:
        return
    try:
        on_stage(stage, product)
    except Exception:
        pass    # see bootstrap_real_full docstring: not a pipeline stage


# ── the genuine real-full composition bootstrap ─────────────────────

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
                         success_criteria: tuple[str, ...] = (),
                         role_models: Mapping[str, str] | None = None,
                         screenshot_sink: Callable[
                             [str, str, bytes], None] | None = None,
                         on_stage: Callable[[str, Any], None] | None = None,
                         budgets: Any = None
                         ) -> dict:
    """Natural-language goal → fresh observation → StateCompiler →
    TaskArchitect → Kernel → shared ledger → AutonomyRuntime → (optional)
    registered projection session — with ZERO hand-built intermediates.

    Every stage is the REAL
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

    ``on_stage(stage, product)`` is an optional observability callback
    fired at honest pipeline boundaries — ``"compiler"`` with the
    CompilerResult and ``"kernel"`` with the initialized TaskVMKernel —
    so the APP shell can push §20.1 progressive-plane signals (T1
    variable labels / T2 DAG) as they actually happen. Best-effort: a
    callback failure NEVER fails the bootstrap (it is not a pipeline
    stage, it watches one).
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
    # mapping (display_name-or-surface_id → surface_id) becomes the
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
    # role→model routing (workplan §20.2): the explicit ``model`` arg wins
    # over everything (backwards compatibility), else the routing table
    # decides per role. The main chain (compiler/architect/cua) defaults to
    # the port model and is never silently degraded; presentation slots
    # (intent_parser / nl_polisher / genui_decoder) read their slot here so
    # A4/A6 wiring has a single resolution point. Every routed model id
    # lands in the ledger's per-call ``model`` field (honest accounting).
    routing = resolve_role_models(role_models)
    compiler_model = model or routing["state_compiler"]
    architect_model = model or routing["task_architect"]
    compiler = StateCompiler(port, ledger, model=compiler_model)
    architect = TaskArchitect(port, ledger, model=architect_model)
    compiler_result = compiler.compile(view, intent)
    _notify_stage(on_stage, "compiler", compiler_result)
    arch = architect.compose(intent, compiler_result)
    kernel = TaskVMKernel(sid, intent)
    kernel.init_task_state(arch.variables)
    if arch.graph is not None:
        kernel.set_plan(arch.graph)
    _notify_stage(on_stage, "kernel", kernel)
    # Observed-plane wiring: the compiler product's handle cache (the
    # deterministic ``value_pattern`` re-reads) IS the production observation
    # path for the runtime — handed to the runtime seam through a
    # composition adapter that reuses ``StateCompiler.extract_observed``.
    # No handle evidence (or an explicit ``extractor``) falls back to the
    # label-token fast path — honest degradation, never a guess.
    if extractor is None:
        # AdaptiveExtractor: the HandleCache fast path PLUS runtime
        # handle re-minting for variables the home-screen compile could
        # not see (GATE-G0 r6: app-interior variables had evidence: []
        # at bootstrap, so contracts referencing them failed-closed
        # forever). prior_variables = the ARCHITECT's variables (what
        # the kernel actually carries — kernel.init_task_state receives
        # arch.variables below in the same flow), so the incremental
        # recompile keeps the semantic key names the plan references.
        extractor = AdaptiveExtractor(
            compiler_result.handle_evidence, compiler=compiler,
            intent=intent, prior_variables=arch.variables)
    # The production multi-surface resolver — compiler handle_id →
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
                                   surface_resolver=surface_resolver,
                                   budgets=budgets)

    # (10) optional projection registration — the PUBLIC lifecycle takes
    # over from here (POST /governance/start → driver → runtime → GUI).
    # GoalPatch wiring: the session is registered with
    # the GovernanceService-backed governance port, so the public
    # goal_patch route runs the FROZEN closure chain (apply_goal_patch →
    # ONE architect.recompose_future → kernel.recompose → unblock)
    # instead of the kernel-only default that left pending_recompose set
    # and execution blocked forever (projection.md §1: recomposition is
    # "architect-owned, injected" — this is the injection).
    governance_service = GovernanceService(
        kernel, architect=architect, compiler=compiler, ledger=ledger)
    if store is not None:
        from taskvm.projection.store import ArtifactStore, SurfaceDecl
        # When the APP shell asks for the screenshot side channel
        # (screenshot_sink), the runtime is wrapped so multi-MB data URLs
        # never enter the projection plane (see ScreenshotArchivingRuntime).
        # The bench factory passes no sink and gets the unwrapped runtime.
        registered_runtime: Any = runtime
        artifacts: Any = None
        if screenshot_sink is not None:
            artifacts = ArtifactStore()
            registered_runtime = ScreenshotArchivingRuntime(
                runtime, artifacts, on_screenshot=screenshot_sink)
        decls = tuple(
            SurfaceDecl(surface_id=info.surface_id,
                        display_name=info.display_name or info.surface_id)
            for info in surface_infos)
        store.register(sid, kernel, runtime=registered_runtime,
                       surfaces=decls,
                       artifacts=artifacts,
                       governance=GovernanceServicePort(
                           governance_service, kernel))

    return dict(sid=sid, intent=intent, kernel=kernel, runtime=runtime,
                substrate=substrate, ledger=ledger,
                model_port=port, compiler=compiler, architect=architect,
                compiler_result=compiler_result,
                architecture=arch, surfaces=surface_infos,
                surface_resolver=surface_resolver,
                governance_service=governance_service,
                role_models=routing)
