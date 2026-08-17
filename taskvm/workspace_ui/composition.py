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
from typing import Any, Mapping, cast

from taskvm.architect import (
    ActionContractSerializer, HttpModelPort, ModelCallLedger,
    ModelCallRecord, MODEL_ROLE_CUA, assert_prompt_clean,
)
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
    ledger = ledger or ModelCallLedger()
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
                         budgets: Any = None) -> Any:
    """Build a real ``AutonomyRuntime`` for one task session — the single
    production call of ``compose_runtime`` (substrate.md §8 T1 leg-1).

    ``substrate`` may be injected (tests / alternate providers). By
    default a ``builtin_web`` ``SubstrateSession`` is created through the
    substrate registry from the provider's own URL knowledge.
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
                           budgets=budgets, surfaces=surfaces, model=model)


# re-exported for composition consumers (view building is architect-free)
__all__ = [
    "HttpCUAModel", "VisibleLabelExtractor",
    "build_runtime_ports", "compose_task_runtime",
    "CompilerObservationView", "VisibleRegion",
]
