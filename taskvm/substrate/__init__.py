"""taskvm.substrate — L1 Substrate Port (contract frozen in
``docs/contracts/substrate.md``).

The package ROOT is the port and NOTHING ELSE: Protocol types, DTOs,
registries. Upper layers import ``taskvm.substrate`` and receive exactly
that. Concrete implementations (``builtin_web`` / ``mobilegym`` /
``osworld``) are banned imports outside composition roots — enforced by
``tests/architecture/test_import_boundaries.py`` and
``tests/substrate/test_no_api_backdoor.py`` (the root must not export
url/bridge helpers of any kind — URL / port / launch knowledge lives
ONLY in the provider config). Transitional consumers
(``execution/gui_driver.py``) import the implementations directly and
are registered in the Transitional
Debt Register (docs/contracts/substrate.md §8, mirrored shrink-only in
``tests/substrate/test_no_api_backdoor.py``; the formal LOCK audit fails
while the register is non-empty).

Responsibility split inside the package:
  * Oracle/seed capabilities live in ``builtin_web/evaluation.py`` /
    ``mobilegym/evaluation.py`` (EvaluationEnvironment — a physically
    separate object, never handed to the runtime decision chain).
  * The runtime write surface lives above the port in the runtime plane
    (``taskvm.runtime`` + composition: operator→gesture composition is an
    upper-layer concern; the substrate answers observe/act/capture).
  * Web and MobileGym specifics live in their substrate subdirectories.
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
