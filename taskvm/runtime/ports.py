"""taskvm.runtime.ports — the dependency-injected Port layer.

Runtime (L2) is stdlib-only Python: it holds PROTOCOL ports for the things
that physically live in ``taskvm.architect`` (L4) and ``taskvm.verifier`` —
the CUA-goal serializer, the model port, the call ledger, the observation
extractor, the verifier. The architecture gate forbids runtime from
importing architect or verifier, so composition injects concrete
implementations; runtime consumes Protocols (see docs/contracts/runtime.md
§1 + runtime_rfc_backlog.md RFC-001).

Structural compatibility is deliberate:

- ``CUAGoalSerializer`` matches ``taskvm.architect.ActionContractSerializer``
  method-for-method (``cua_goal`` / ``compensation_goal``), so the architect
  instance is injected unchanged.
- ``ModelCallRecord`` is field-for-field identical to
  ``taskvm.architect.ModelCallRecord``; architect's ``ModelCallLedger.record``
  only string-checks ``role`` then appends, so the SAME ledger instance can
  serve both architect and runtime — one unified call report, no merge code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from taskvm.domain.contract import ActionContract
from taskvm.domain.patch import CompensationEntry
from taskvm.domain.results import VerificationResult
from taskvm.domain.state import ObservedValue, TaskVariable
from taskvm.domain.workflow import WorkflowNode
from taskvm.substrate import GuiAction, Observation

# ── the CUA's one-step verdict ──────────────────────────────────────────────


class CUADecisionKind(str, Enum):
    """What the CUA decided for this turn. ONE atomic GUI action, or a
    terminal signal. The runtime never accepts a multi-step trajectory as an
    uninterruptible unit (runtime.md §9)."""

    ACT = "act"    # perform exactly one GuiAction, then re-enter the gate
    DONE = "done"  # the contract's visible completion is reached → verify
    FAIL = "fail"  # the CUA cannot proceed on the visible UI → repair/escalate


@dataclass(frozen=True)
class CUADecision:
    """The CUA's prediction for one turn.

    ``kind=ACT`` carries one ``GuiAction``; ``DONE``/``FAIL`` carry none.
    ``raw`` is provenance (the model's reply text / fake script id) for the
    call ledger and trace — never an internal id.
    """

    kind: CUADecisionKind
    action: GuiAction | None = None
    reason: str = ""
    raw: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CUADecisionKind):
            object.__setattr__(self, "kind", CUADecisionKind(self.kind))
        if self.kind is CUADecisionKind.ACT and self.action is None:
            raise ValueError("CUADecision kind=ACT requires a GuiAction")


# ── the injected ports ──────────────────────────────────────────────────────


@runtime_checkable
class CUAGoalSerializer(Protocol):
    """Deterministic ActionContract → CUA goal text (0 model calls). The
    concrete impl is ``taskvm.architect.ActionContractSerializer`` (injected
    unchanged — method signatures match)."""

    def cua_goal(self, contract: ActionContract,
                 labels: Mapping[str, str] | None = None,
                 *, attempt: int = 1) -> str: ...

    def compensation_goal(self, entry: CompensationEntry,
                          labels: Mapping[str, str] | None = None) -> str: ...


@runtime_checkable
class CUAModel(Protocol):
    """Predict ONE atomic GUI action (or a done/fail signal) for the current
    visible world. The concrete impl is a composition-layer adapter over
    ``taskvm.architect.ModelPort``+``_LedgeredPort`` (system-prompt assembly,
    observation→prompt, JSON→CUADecision); tests inject a fake. Runtime never
    calls a provider directly."""

    def predict_action(self, *, goal: str, observation: Observation,
                       labels: Mapping[str, str] | None = None,
                       attempt: int = 1,
                       model: str | None = None) -> CUADecision: ...


@runtime_checkable
class ObservationExtractor(Protocol):
    """Deterministic fast-path: substrate ``Observation`` → ``ObservedValue``s
    (0 high-level model calls). The concrete impl wraps
    ``taskvm.architect.StateCompiler.extract_observed``. On a structural
    failure the extractor raises ``StructureInvalidated`` (sync.py) so the
    runtime can publish the typed event without calling the compiler itself."""

    def extract(self, observation: Observation,
                variables: Mapping[str, TaskVariable]
                ) -> tuple[ObservedValue, ...]: ...


@runtime_checkable
class Verifier(Protocol):
    """The runtime-visible verifier (E's single owner; runtime.md §6).
    Checks completion from FRESH visible observation only — never hidden DB /
    fixture / oracle. Produces the typed ``VerificationResult`` the kernel
    lands with TIME checks only."""

    def verify(self, *, node: WorkflowNode,
               before_observed: Mapping[str, Any],
               after_observed: Mapping[str, Any],
               desired: Mapping[str, Any],
               observation: Observation,
               action_id: str | None,
               epoch: int) -> VerificationResult: ...


# ── call accounting (duck-typed compatible with architect ModelCallLedger) ──

#: The CUA role constant — identical string to architect.MODEL_ROLE_CUA so the
#: shared ledger buckets CUA calls under the same key the benchmark reads.
MODEL_ROLE_CUA = "cua"


@dataclass(frozen=True)
class ModelCallRecord:
    """One landed (or failed) CUA model call — audit raw material. Fields are
    field-for-field identical to ``taskvm.architect.ModelCallRecord`` so the
    SAME ``ModelCallLedger`` instance accepts records from both layers (the
    ledger only string-validates ``role``)."""

    role: str
    purpose: str
    model: str
    ok: bool
    is_repair: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    revision: int = 0
    error: str = ""


@runtime_checkable
class CallLedger(Protocol):
    """Append-only call accounting. The concrete impl is
    ``taskvm.architect.ModelCallLedger`` (injected as the SAME instance given
    to the architect — one unified report)."""

    def record(self, rec: ModelCallRecord) -> Any: ...

    @property
    def records(self) -> tuple[ModelCallRecord, ...]: ...

    def counts_by_role(self) -> dict[str, int]: ...

    def total(self) -> int: ...


# ── runtime-produced events (NOT kernel Events; D consumes these) ───────────


class RuntimeEventKind(str, Enum):
    """Typed signals the runtime publishes for projection/evaluation. These
    are NOT kernel mutations (the kernel owns STATE/TIME/HISTORY); they are
    the runtime's own observation/sync/compensation artifacts (runtime.md §3)."""

    ACTION_LANDED = "action_landed"            # a GUI action + fresh observation
    STRUCTURE_INVALIDATED = "structure_invalidated"  # binding can't be recovered
    SURFACE_CONFLICT = "surface_conflict"      # external drift vs pending desired
    COMPENSATION_ENTRY = "compensation_entry"   # one rollback GUI step + fresh obs
    BUDGET_EXHAUSTED = "budget_exhausted"       # safe stop / escalate
    LOOP_TICK = "loop_tick"                     # one bounded-loop iteration
    NODE_FAILED = "node_failed"                 # honest node-level failure
                                               # (CUA fail / verify fail with
                                               # repair budget spent / no
                                               # surface / kernel-blocked)


@dataclass(frozen=True)
class RuntimeEvent:
    """One runtime-side artifact. ``artifact_ref`` points at a captured
    screenshot / visual artifact (never an internal id); ``detail`` is
    human/audit-facing prose."""

    kind: RuntimeEventKind
    epoch: int
    node_id: str = ""
    surface_id: str = ""
    artifact_ref: str = ""
    detail: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RuntimeEventKind):
            object.__setattr__(self, "kind", RuntimeEventKind(self.kind))
