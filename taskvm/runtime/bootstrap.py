"""taskvm.runtime.bootstrap — the composition/bootstrap seam (runtime.md §3).

``AutonomyRuntime`` is stdlib-only and holds PROTOCOL ports; the concrete
implementations (architect ``ActionContractSerializer`` / ``ModelPort`` /
``ModelCallLedger``, the ``StateCompiler.extract_observed`` wrapper, the
``VisibleVerifier``, the real substrate ``SubstrateSession``) live in
``taskvm.architect`` / ``taskvm.substrate`` / ``taskvm.verifier`` and are
INJECTED by composition (runtime_rfc_backlog.md RFC-001). The runtime gate
forbids ``taskvm.runtime`` from importing architect/verifier/concrete-
substrate, so the composition root — NOT the runtime — wires concrete ports.

This module is the **clean public interface** composition (workspace_ui /
integration) calls. The legacy operator-write adapter
(``taskvm.execution.gui_driver.make_task_adapters``, substrate.md §8 T1)
was deleted by the Wave-3 cluster deletion (2026-08-16). It
bundles the injected ports into one typed ``RuntimePorts`` object and exposes
``compose_runtime`` as the single entry point that assembles a real
``AutonomyRuntime`` over a real ``SubstrateSession`` driven by ActionContract
→ CUA → GuiAction (runtime.md §3/§9) — NOT the legacy operator-write
``adapter.mutate`` transport (the platform-table / internal-id API path the
frozen contract bans).

Ownership boundary: this module imports ONLY ``taskvm.runtime`` +
``taskvm.domain`` + ``taskvm.substrate`` (the PORT root) + stdlib. It does
NOT import ``taskvm.architect``, ``taskvm.verifier.visible``, or any concrete
substrate implementation — composition constructs those and hands them in.
It is therefore gate-clean (tests/architecture/test_import_boundaries.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from taskvm.runtime.autonomy import AutonomyRuntime
from taskvm.runtime.config import DEFAULT_BUDGETS, RuntimeBudgets
from taskvm.runtime.ports import (
    CallLedger, CUAGoalSerializer, CUAModel, ObservationExtractor, Verifier,
)

if TYPE_CHECKING:
    from taskvm.substrate import SubstrateSession


@dataclass(frozen=True)
class RuntimePorts:
    """The five injected ports composition assembles (RFC-001) — bundled so
    the bootstrap call site is stable as ports evolve. Composition MUST
    inject:

    - ``serializer``: ``taskvm.architect.ActionContractSerializer`` (the
      deterministic ActionContract→CUA-goal text, 0 model calls).
    - ``cua_model``: a composition adapter over architect ``ModelPort``
      (system-prompt assembly, observation→prompt, JSON→``CUADecision``).
    - ``extractor``: a wrapper over architect ``StateCompiler.extract_observed``
      (deterministic Observation→ObservedValues; raises ``StructureInvalidation``
      on unrecoverable binding).
    - ``verifier``: ``taskvm.verifier.VisibleVerifier`` (E's single verifier).
    - ``ledger``: the SAME ``taskvm.architect.ModelCallLedger`` instance given
      to the architect (one unified cua/compiler/architect call report).

    None of these are imported here (gate); composition constructs them.
    """
    cua_model: CUAModel
    serializer: CUAGoalSerializer
    extractor: ObservationExtractor
    verifier: Verifier
    ledger: CallLedger


def compose_runtime(kernel: Any, substrate: "SubstrateSession",
                    ports: RuntimePorts, *,
                    budgets: RuntimeBudgets | None = None,
                    surfaces: list[str] | None = None,
                    model: str | None = None) -> AutonomyRuntime:
    """The single composition entry point — assemble a real
    ``AutonomyRuntime`` over a real ``SubstrateSession`` with injected ports.

    This is the clean bootstrap seam workspace_ui (D) calls to drive the
    real runtime (ActionContract → CUA → GuiAction → SubstrateSession.act →
    fresh observe → verify) instead of the legacy
    ``gui_driver.make_task_adapters`` operator-write adapters. Composition
    owns building the kernel, the SubstrateSession (via
    ``substrate_registry.create_session``), and the five ports; this function
    only wires them into the runtime facade.
    """
    return AutonomyRuntime(
        kernel, substrate,
        cua_model=ports.cua_model, serializer=ports.serializer,
        extractor=ports.extractor, verifier=ports.verifier,
        ledger=ports.ledger, budgets=budgets or DEFAULT_BUDGETS,
        surfaces=surfaces, model=model)
