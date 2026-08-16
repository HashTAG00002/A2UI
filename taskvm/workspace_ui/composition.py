"""taskvm.workspace_ui.composition — the D-side composition root for the
real runtime (substrate.md §8 T1 leg-1; D audit rework D-F2, 2026-08-16).

This module REPLACES the legacy ``taskvm.execution.gui_driver`` import in
``workspace_ui/server.py``. It is the **single production call site** of
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
from typing import Any, Mapping

from taskvm.architect import (
    ActionContractSerializer, HttpModelPort, ModelCallLedger,
    ModelCallRecord, MODEL_ROLE_CUA, assert_prompt_clean,
)
from taskvm.architect.observation import CompilerObservationView, VisibleRegion
from taskvm.runtime.bootstrap import RuntimePorts, compose_runtime
from taskvm.runtime.ports import CUADecision, CUADecisionKind
from taskvm.substrate import GuiAction, Observation, substrate_registry
from taskvm.verifier.visible import VisibleVerifier

from taskvm.domain.state import ObservedValue, SurfaceEvidence, TaskVariable


# ── the CUA model adapter (composition-owned, per RuntimePorts docstring) ──

_CUA_SYSTEM_PROMPT = (
    "你是一个图形界面操作代理（CUA）。每一轮你会看到当前屏幕的可见文本"
    "和一个操作目标。你只做一件事：返回恰好一个原子 GUI 操作，或声明完成/"
    "失败。只使用屏幕上可见的信息。输出严格 JSON："
    '{"kind":"act","action":{"kind":"click|type|scroll|key",'
    '"text":"...","coordinate":[x,y]}} 或 {"kind":"done"} 或 '
    '{"kind":"fail","reason":"业务原因"}。不要输出任何其他内容。'
)


class HttpCUAModel:
    """``CUAModel`` port over the architect ``HttpModelPort``.

    One ``predict_action`` = one provider request = one ledger record
    (C-2 discipline). The outgoing prompt passes the no-leak gate; a
    parse failure is an honest ``fail`` decision — the runtime's repair
    loop owns any re-ask.
    """

    def __init__(self, port: HttpModelPort | None = None,
                 ledger: ModelCallLedger | None = None) -> None:
        self._port = port or HttpModelPort()
        self._ledger = ledger

    def predict_action(self, *, goal: str, observation: Observation,
                       labels: Mapping[str, str] | None = None,
                       attempt: int = 1, model: str | None = None,
                       ) -> CUADecision:
        visible = getattr(observation, "visible_text", "") or ""
        retry = (f"\n（第 {attempt} 次重试：上一次操作没有完成任务，请确认修改"
                 "确实生效后再报告完成。）") if attempt > 1 else ""
        user = (f"## 操作目标\n{goal}{retry}\n\n## 屏幕可见文本\n{visible}")
        try:
            assert_prompt_clean(_CUA_SYSTEM_PROMPT + "\n" + user,
                                what="cua prompt")
        except Exception as e:  # a leak is a harness bug — fail honestly
            self._record(ok=False, error=f"prompt leak: {e}", model=model)
            return CUADecision(kind=CUADecisionKind.FAIL,
                               reason="指令生成内部错误，已安全终止")
        try:
            reply = self._port.complete_json(system=_CUA_SYSTEM_PROMPT,
                                             user=user, model=model)
        except Exception as e:
            self._record(ok=False, error=str(e), model=model)
            raise
        parsed = reply.parsed if isinstance(reply.parsed, dict) else None
        if parsed is None:
            self._record(ok=False, error="unparseable CUA reply",
                         model=model, reply=reply)
            return CUADecision(kind=CUADecisionKind.FAIL,
                               reason="模型返回无法解析",
                               raw=reply.raw[:200])
        decision = _decision_from_json(parsed)
        self._record(ok=decision.kind is not CUADecisionKind.FAIL,
                     model=model, reply=reply)
        return decision

    # ── ledger ───────────────────────────────────────────────────────────
    def _record(self, *, ok: bool, model: str | None, error: str = "",
                reply: Any = None) -> None:
        if self._ledger is None:
            return
        self._ledger.record(ModelCallRecord(
            role=MODEL_ROLE_CUA, purpose="cua.predict_action",
            model=model or self._port.default_model, ok=ok,
            prompt_tokens=getattr(reply, "prompt_tokens", None),
            completion_tokens=getattr(reply, "completion_tokens", None),
            error=error))


def _decision_from_json(parsed: Mapping[str, Any]) -> CUADecision:
    kind = str(parsed.get("kind", "")).lower()
    if kind == "done":
        return CUADecision(kind=CUADecisionKind.DONE,
                           raw=str(parsed)[:200])
    if kind == "fail":
        return CUADecision(kind=CUADecisionKind.FAIL,
                           reason=str(parsed.get("reason", ""))[:300],
                           raw=str(parsed)[:200])
    if kind == "act":
        act = parsed.get("action") or {}
        act_kind = str(act.get("kind", ""))
        if act_kind not in ("click", "type", "scroll", "key"):
            return CUADecision(kind=CUADecisionKind.FAIL,
                               reason=f"未知操作类型 {act_kind!r}",
                               raw=str(parsed)[:200])
        coord = act.get("coordinate")
        if isinstance(coord, (list, tuple)) and len(coord) == 2:
            coord = (int(coord[0]), int(coord[1]))
        else:
            coord = None
        action = GuiAction(kind=act_kind, text=str(act.get("text", "") or ""),
                           coordinate=coord)
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

def build_runtime_ports(*, model_port: HttpModelPort | None = None,
                        ledger: ModelCallLedger | None = None,
                        cua_model: Any = None,
                        extractor: Any = None,
                        ) -> RuntimePorts:
    """Assemble the five injected ports. Pass ``ledger`` to SHARE one
    instance with the architect (the unified call report); pass
    ``cua_model``/``extractor`` to override with test fakes."""
    ledger = ledger or ModelCallLedger()
    return RuntimePorts(
        serializer=ActionContractSerializer(),
        cua_model=cua_model or HttpCUAModel(
            port=model_port or HttpModelPort(), ledger=ledger),
        extractor=extractor or VisibleLabelExtractor(),
        verifier=VisibleVerifier(),
        ledger=ledger)


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
