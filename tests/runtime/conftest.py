"""tests/runtime/ — OWNER: Agent E (autonomy runtime).

Contract tests for the runtime's producer obligations (layered ownership
protocol §1): before/after observations come from fresh pre/post-action
observation of the visible world; irreversibility is reported honestly;
compensation is executed through the SAME real execution path as forward
work; VerificationResult / CompensationResult are constructed truthfully
via the typed domain contracts.

Agent A (kernel) does NOT implement these. The kernel-side landing
semantics (epoch / single-use / coverage / disposition) are pinned in
tests/kernel/test_timeline_governance.py.

The fakes below live ONLY here (tests/fakes/ holds cross-suite ones):
  * FakeSubstrate — a minimal visible world (k=v text); ``act`` mutates it
    through real-gesture GuiActions only; every act lands in ``act_log``.
  * ScriptedCUA — deterministic decision script + an ``on_predict`` hook so
    tests can inject governance events MID-FLIGHT (the hot-governance race).
  * FakeExtractor — Observation → ObservedValue ("k=v" tokens), raises
    StructureInvalidation when the visible anchor is gone.
  * FakeLedger / FakeSerializer — the DI ports.
"""
from __future__ import annotations

import pytest

from taskvm.domain.contract import ActionContract
from taskvm.domain.intent import TaskIntent
from taskvm.domain.state import (
    ObservedValue, SurfaceEvidence, SurfaceHandle, TaskVariable,
)
from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode
from taskvm.kernel import TaskVMKernel
from taskvm.substrate import (
    ActionReceipt, GuiAction, Observation, SurfaceInfo, VisualArtifact,
)
from taskvm.runtime import (
    AutonomyRuntime, CUADecision, CUADecisionKind, RuntimeBudgets,
)
from taskvm.runtime.sync import StructureInvalidation
from taskvm.verifier.visible import VisibleVerifier


# ── fakes ──────────────────────────────────────────────────────────────────
class FakeLedger:
    """The CallLedger port — records every ModelCallRecord."""

    def __init__(self) -> None:
        self._records = []

    def record(self, rec) -> None:
        self._records.append(rec)

    @property
    def records(self) -> tuple:
        return tuple(self._records)

    def counts_by_role(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self._records:
            out[r.role] = out.get(r.role, 0) + 1
        return out

    def total(self) -> int:
        return len(self._records)

    def of_role(self, role: str = "cua") -> tuple:
        return tuple(r for r in self._records if r.role == role)


class FakeSubstrate:
    """A minimal visible world per surface. ``world[sid]`` maps semantic
    keys to visible values; the visible text is the scrubbed "k=v" tokens a
    real user could read. ``act`` performs REAL gestures only — a ``type``
    with text "k=v" writes the value; everything else is a no-op click/wait.
    """

    def __init__(self, worlds: dict[str, dict[str, str]] | None = None):
        self.world = {sid: dict(vals) for sid, vals in (worlds or {}).items()}
        if not self.world:
            self.world = {"app": {}}
        self.act_log: list[tuple[str, str]] = []
        self.observe_log: list[str] = []

    def list_surfaces(self) -> list[SurfaceInfo]:
        return [SurfaceInfo(surface_id=sid, display_name=sid)
                for sid in self.world]

    def _visible_text(self, sid: str) -> str:
        return " ".join(f"{k}={v}" for k, v in sorted(self.world[sid].items()))

    def _fingerprint(self, sid: str) -> str:
        return f"fp:{hash(self._visible_text(sid)) & 0xFFFFFFFF:x}"

    def observe(self, surface, previous_fingerprint: str | None = None,
                ) -> Observation:
        sid = surface if isinstance(surface, str) else surface.surface_id
        self.observe_log.append(sid)
        return Observation(
            surface=SurfaceInfo(surface_id=sid, display_name=sid),
            revision=len(self.observe_log),
            timestamp=0.0,
            screenshot_ref=f"shot://{sid}/{len(self.observe_log)}",
            visible_text=self._visible_text(sid),
            fingerprint=self._fingerprint(sid),
            previous_fingerprint_matched=(
                previous_fingerprint == self._fingerprint(sid)
                if previous_fingerprint is not None else None),
        )

    def act(self, surface, action: GuiAction, *, epoch: str) -> ActionReceipt:
        sid = surface if isinstance(surface, str) else surface.surface_id
        self.act_log.append((sid, action.kind))
        if action.kind == "type" and action.text and "=" in action.text:
            key, _, val = action.text.partition("=")
            self.world[sid][key] = val
        return ActionReceipt(action=action, status="ok", surface_id=sid,
                             epoch=epoch)

    def capture(self, surface) -> VisualArtifact:
        sid = surface if isinstance(surface, str) else surface.surface_id
        return VisualArtifact(surface_id=sid)

    def close(self) -> None:
        return None


class ScriptedCUA:
    """Deterministic CUA. The script is a list of CUADecisions, Exceptions
    (provider failures) or callables ``(cua, observation) -> CUADecision``.
    ``on_predict`` fires BEFORE each reply is consumed — the hook where
    tests inject mid-flight governance (the epoch race)."""

    def __init__(self, script: list | None = None):
        self.script = list(script or [])
        self.calls: list[dict] = []
        self.on_predict = None

    def predict_action(self, *, goal: str, observation, labels=None,
                       attempt: int = 1, model=None) -> CUADecision:
        self.calls.append({"goal": goal, "observation": observation,
                           "attempt": attempt})
        if self.on_predict is not None:
            hook, self.on_predict = self.on_predict, None
            hook(self)
        if not self.script:
            return CUADecision(kind=CUADecisionKind.FAIL,
                               reason="script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(self, observation)
        return item


class FakeExtractor:
    """Observation → ObservedValues from "k=v" tokens (only keys known to
    the current task state — unknown keys are structure discovery and NOT
    our business). Raises StructureInvalidation when the visible anchor is
    gone ("STRUCTURE-GONE" in the text)."""

    def __init__(self):
        self.calls = 0

    def extract(self, observation, variables):
        self.calls += 1
        text = observation.visible_text or ""
        if "STRUCTURE-GONE" in text:
            raise StructureInvalidation("visible anchor disappeared")
        known = set(variables)
        out = []
        for tok in text.split():
            if "=" in tok:
                key, _, val = tok.partition("=")
                if key in known:
                    out.append(ObservedValue(
                        semantic_key=key, value=val,
                        evidence=(SurfaceEvidence(
                            surface=SurfaceHandle(handle_id="vis"),
                            visible_label=key, observed_value=val),)))
        return tuple(out)


class FakeSerializer:
    """CUAGoalSerializer port — deterministic text, 0 model calls."""

    def cua_goal(self, contract, labels=None, *, attempt: int = 1) -> str:
        return (f"make it true that {contract.semantic_goal} "
                f"(targets: {dict(contract.desired_state)})")

    def compensation_goal(self, entry, labels=None) -> str:
        return (f"restore {entry.semantic_key} back to "
                f"{entry.from_observed} through the visible UI")


# ── script helpers ─────────────────────────────────────────────────────────
def type_kv(key: str, value) -> CUADecision:
    return CUADecision(kind=CUADecisionKind.ACT,
                       action=GuiAction(kind="type", text=f"{key}={value}"))


def type_from_obs(key: str, fn):
    """A callable decision: type key=<fn(current values dict)>."""
    def _decide(cua, obs):
        cur = {}
        for tok in (obs.visible_text or "").split():
            if "=" in tok:
                k, _, v = tok.partition("=")
                cur[k] = v
        return CUADecision(kind=CUADecisionKind.ACT,
                           action=GuiAction(kind="type",
                                            text=f"{key}={fn(cur)}"))
    return _decide


DONE = CUADecision(kind=CUADecisionKind.DONE)
CLICK = CUADecision(kind=CUADecisionKind.ACT,
                    action=GuiAction(kind="click",
                                     coordinate=(500, 500)))


# ── kernel / graph builders ────────────────────────────────────────────────
def action_node(node_id: str, *, goal: str = "", desired: dict | None = None,
                depends_on: tuple[str, ...] = (), parent_id: str | None = None,
                reversibility="reversible", completion: str = "") -> WorkflowNode:
    from taskvm.domain.contract import Reversibility
    return WorkflowNode(
        node_id=node_id, kind=NodeKind.ACTION, label=node_id,
        depends_on=depends_on, parent_id=parent_id,
        contract=ActionContract(
            contract_id=f"c-{node_id}",
            semantic_goal=goal or f"realise {node_id}",
            desired_state=dict(desired or {}),
            completion_condition=completion,
            reversibility=Reversibility(reversibility)))


def var(key: str, observed, desired, *, label: str | None = None,
        mutability: str = "editable") -> TaskVariable:
    return TaskVariable(semantic_key=key, label=label or key,
                        observed=observed, desired=desired,
                        mutability=mutability)


def make_kernel(variables, graph: WorkflowGraph, goal: str = "g"
                ) -> TaskVMKernel:
    k = TaskVMKernel(session_id="rt-test", intent=TaskIntent(goal=goal))
    k.init_task_state(variables)
    k.set_plan(graph)
    return k


def make_runtime(kernel, substrate, cua, *, extractor=None,
                 budgets: RuntimeBudgets | None = None) -> AutonomyRuntime:
    return AutonomyRuntime(
        kernel, substrate,
        cua_model=cua, serializer=FakeSerializer(),
        extractor=extractor or FakeExtractor(),
        verifier=VisibleVerifier(), ledger=FakeLedger(),
        budgets=budgets)


def status_of(kernel, node_id: str):
    return kernel.workflow().statuses.get(node_id)


@pytest.fixture
def ledger() -> FakeLedger:
    return FakeLedger()
