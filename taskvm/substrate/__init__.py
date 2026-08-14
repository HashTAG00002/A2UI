"""taskvm.substrate — L1 Substrate Port (Agent B, contract frozen
2026-08-14 in ``docs/contracts/substrate.md``).

The package ROOT is the port and nothing else: upper layers import
``taskvm.substrate`` and receive protocol types + registries. Concrete
implementations (``builtin_web`` / ``mobilegym`` / ``osworld``) are
banned imports outside composition roots — enforced by
``tests/architecture/test_import_boundaries.py`` and
``tests/substrate/test_no_api_backdoor.py``.

What changed vs. the legacy package (E17-C → Agent B rework):
  * ``substrate/base.py`` (StateAdapter + 7 app adapters + executor
    factory) is DELETED. Its oracle/seed surface moved to
    ``builtin_web/evaluation.py`` / ``mobilegym/evaluation.py``
    (EvaluationEnvironment — a physically separate object); its runtime
    write surface was already GUI-only and now lives above the port in
    ``taskvm.execution.gui_driver`` (operator→gesture composition is an
    execution-layer concern; the substrate answers observe/act/capture).
  * ``harness/state_adapter.py``, ``harness/mobilegym_bridge.py`` and
    ``harness/browser_controller.py`` shims/files are DELETED (Web and
    MobileGym specifics live in their substrate subdirectories).
"""
from taskvm.substrate.port import (
    ActionReceipt,
    EvaluationEnvironment,
    EvaluationProvider,
    EvaluationRegistry,
    GUI_ACTION_KINDS,
    GuiAction,
    IrreversibleAction,
    Observation,
    SubstrateProvider,
    SubstrateRegistry,
    SubstrateSession,
    SubstrateUnavailable,
    SurfaceHandle,
    SurfaceInfo,
    VisualArtifact,
    evaluation_registry,
    scrub_hidden_ids,
    substrate_registry,
)

__all__ = [
    "ActionReceipt",
    "EvaluationEnvironment",
    "EvaluationProvider",
    "EvaluationRegistry",
    "GUI_ACTION_KINDS",
    "GuiAction",
    "IrreversibleAction",
    "Observation",
    "SubstrateProvider",
    "SubstrateRegistry",
    "SubstrateSession",
    "SubstrateUnavailable",
    "SurfaceHandle",
    "SurfaceInfo",
    "VisualArtifact",
    "evaluation_registry",
    "scrub_hidden_ids",
    "substrate_registry",
]
