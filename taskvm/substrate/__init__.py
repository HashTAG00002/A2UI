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
    "builtin_web_app_url",
    "evaluation_registry",
    "mobilegym_bridge_url",
    "scrub_hidden_ids",
    "substrate_registry",
]


# ── name-routed composition helpers (Agent B) ─────────────────────────────
# The port routes BY NAME so upper layers never import an implementation
# subtree (gui_driver's ``from taskvm.substrate.builtin_web.launcher import
# app_url`` was the last such leak — AC3 of the substrate-isolation brief).
# Both helpers are lazy: the implementation import happens at call time,
# inside THIS package (the substrate may import itself).


def builtin_web_app_url(app: str, host: str = "localhost") -> str:
    """Resolve a builtin web app surface to its URL (ports/env overrides
    live ONLY in the builtin provider config — contract §5)."""
    from taskvm.substrate.builtin_web.launcher import app_url
    return app_url(app, host=host)


def mobilegym_bridge_url(host: str = "localhost") -> str:
    """Resolve the MobileGym bridge base URL (default port/env override
    lives ONLY in the mobilegym substrate config)."""
    from taskvm.substrate.mobilegym.evaluation import DEFAULT_BRIDGE_PORT
    import os
    env = os.environ.get("TASKVM_MOBILEGYM_PORT")
    port = int(env) if env and env.isdigit() else DEFAULT_BRIDGE_PORT
    return f"http://{host}:{port}"
