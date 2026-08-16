"""taskvm.substrate — L1 Substrate Port (Agent B, contract frozen
2026-08-14 in ``docs/contracts/substrate.md``).

The package ROOT is the port and NOTHING ELSE: Protocol types, DTOs,
registries. Upper layers import ``taskvm.substrate`` and receive exactly
that. Concrete implementations (``builtin_web`` / ``mobilegym`` /
``osworld``) are banned imports outside composition roots — enforced by
``tests/architecture/test_import_boundaries.py`` and
``tests/substrate/test_no_api_backdoor.py`` (B-3, Oracle audit 2026-08-15:
the root previously ALSO exported ``builtin_web_app_url`` /
``mobilegym_bridge_url`` helpers — that hid the import leak behind a
fake port surface while the semantic leak stayed; both are deleted. URL /
port / launch knowledge lives ONLY in the provider config; transitional
consumers (``execution/gui_driver.py``, scheduled for Agent E's deletion)
import the implementations directly and are registered in the Transitional
Debt Register (docs/contracts/substrate.md §8, mirrored shrink-only in
``tests/substrate/test_no_api_backdoor.py``; the formal LOCK audit fails
while the register is non-empty).

What changed vs. the legacy package (E17-C → Agent B rework):
  * ``substrate/base.py`` (StateAdapter + 7 app adapters + executor
    factory) is DELETED. Its oracle/seed surface moved to
    ``builtin_web/evaluation.py`` / ``mobilegym/evaluation.py``
    (EvaluationEnvironment — a physically separate object); its runtime
    write surface lives above the port in the runtime plane
    (``taskvm.runtime`` + composition: operator→gesture composition is an
    upper-layer concern; the substrate answers observe/act/capture).
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
