"""taskvm.substrate.port — the L1 Substrate Port (Agent B, contract frozen
2026-08-14 in ``docs/contracts/substrate.md``).

This module is the ONLY part of ``taskvm.substrate`` upper layers may import
(``from taskvm.substrate import SubstrateSession, GuiAction, ...``). It is
stdlib-only and defines the platform-neutral protocol every substrate
implements and every upper layer consumes:

    SubstrateRegistry.create_session(name, config)  -> SubstrateSession   (runtime)
    EvaluationRegistry.create(name, config)         -> EvaluationEnvironment (eval plane)

Two physically separate capability sets (handoff 00 §3.3, task brief §二):

  * ``SubstrateSession``  — what a real human could do on the same device:
    observe (screenshot + scrubbed visible text/a11y), act (real GUI
    gestures only), capture. No reset, no seed, no oracle read.
  * ``EvaluationEnvironment`` — exam-room powers: reset / seed / hidden
    oracle read / summary. Never handed to the runtime decision chain.

Content legality (layered ownership protocol): every Observation this port's
implementations construct is legal BY CONSTRUCTION — no hidden DB ids, no
``data-*-id`` attributes, no internal object ids, no deep-link URLs. The
kernel and upper layers do not re-check substrate details.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "SurfaceInfo", "SurfaceHandle", "GuiAction", "Observation",
    "ActionReceipt", "VisualArtifact",
    "SubstrateSession", "SubstrateProvider",
    "EvaluationEnvironment", "EvaluationProvider",
    "SubstrateRegistry", "EvaluationRegistry",
    "SubstrateUnavailable", "IrreversibleAction",
    "scrub_hidden_ids", "VISIBLE_ID_PATTERNS",
]

# ── exceptions ──────────────────────────────────────────────────────────────


class SubstrateUnavailable(RuntimeError):
    """The named substrate cannot be reached/started (e.g. no OSWorld VM,
    no MobileGym sim, no browser install). Honest failure — never a
    fallback to a non-GUI write path."""


class IrreversibleAction(RuntimeError):
    """The substrate has no real-UI way to perform/undo this action.
    Honest irreversibility: callers surface it, they do not restore state
    through a hidden snapshot."""


# ── runtime port value objects ──────────────────────────────────────────────


@dataclass(frozen=True)
class SurfaceInfo:
    """One addressable surface (window/tab/app screen) offered by a session.
    ``surface_id`` is an opaque, substrate-local, EPHEMERAL token — it is
    never an app database primary key."""
    surface_id: str
    display_name: str            # user-visible name (e.g. window title)
    surface_kind: str = "screen"  # screen | window | tab | app …
    revision: int = 0


@dataclass(frozen=True)
class SurfaceHandle:
    """A TaskVM-owned ephemeral cache entry binding a semantic anchor to a
    visible structure (contract §3). Created from Observations; invalidated
    when the structural fingerprint changes; NEVER an app DB primary key.

    The visible anchor is what a real user would use to find the thing:
    role ("button"), visible text, nearby labels, on-screen position."""
    handle_id: str
    surface_id: str
    anchor_role: str                      # button | link | input | row | …
    anchor_text: str = ""                 # visible label/caption
    context_text: str = ""                # nearby visible text (disambiguation)
    bbox_norm: tuple[float, float, float, float] | None = None  # x0,y0,x1,y1 in [0,1000]
    fingerprint: str = ""                 # visible-structure fingerprint
    last_seen_revision: int = 0


#: DOM/JSON attributes that can never enter an Observation — they encode the
#: app's internal database identity (E16/E21 leaks; contract §6).
VISIBLE_ID_PATTERNS: tuple[str, ...] = (
    "data-*-id", "data-event-*", "data-task-*", "data-file-*",
    "data-post-id", "data-chat-id", "data-transaction-id",
    "data-appointment-id", "data-mail-id", "data-action-params",
)

_HIDDEN_ID_RE = re.compile(
    r"data-(?:[a-z0-9]+-)*?(?:event|task|file|post|chat|transaction|"
    r"appointment|mail|action(?:-params)?)[a-z0-9-]*"
    # B-3: consume the attribute VALUE too — the value IS the internal
    # id (E16/E21 leak class); redacting only the attribute name kept
    # the primary key itself in the observation.
    r"(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?",
    re.IGNORECASE,
)


def scrub_hidden_ids(text: str) -> str:
    """Remove hidden identity markers (``data-*-id=...`` style substrings)
    from a serialised DOM/observation snippet. Load-bearing for the
    zero-exposure judgement: 'can a real user SEE this string on the
    rendered screen?' — data attributes are not rendered."""
    return _HIDDEN_ID_RE.sub("data-[redacted]", text)


# ── actions (real-world gestures only) ─────────────────────────────────────

#: The complete action vocabulary. Adding a non-gesture verb here (set /
#: mutate / restore / assign) is a contract violation — the gate test
#: ``test_gui_action_vocabulary_is_gestures`` locks this set.
GUI_ACTION_KINDS: tuple[str, ...] = (
    "click", "tap", "type", "key", "scroll", "wait", "open",
)


@dataclass(frozen=True)
class GuiAction:
    """One real-world input event. Coordinates are NORMALIZED to [0,1000]
    (UITARS convention — same as the legacy browser controller)."""
    kind: str                                  # one of GUI_ACTION_KINDS
    coordinate: tuple[float, float] | None = None   # normalized [0,1000]
    text: str | None = None                    # for kind="type"
    key: str | None = None                     # for kind="key" (e.g. "Enter")
    direction: str | None = None               # scroll: up | down | left | right
    magnitude: int | None = None               # scroll step size
    duration_ms: int | None = None             # wait duration
    target: str | None = None                  # "open": surface_id or visible app name
    description: str | None = None             # provenance note (never an internal id)

    def __post_init__(self) -> None:
        if self.kind not in GUI_ACTION_KINDS:
            raise ValueError(
                f"GuiAction.kind must be one of {GUI_ACTION_KINDS} (real-world "
                f"gestures only); got {self.kind!r}. Setting/mutating/restoring "
                "state through the port is a contract violation "
                "(docs/contracts/substrate.md §2).")


@dataclass(frozen=True)
class Observation:
    """What a real user could see on one surface at one moment (contract §2).

    Content-legality invariants (producer-owned):
      * ``visible_text`` carries only rendered content;
      * ``handle_candidates`` carry TaskVM-owned handles;
      * nothing here may contain app-internal identity."""
    surface: SurfaceInfo
    revision: int
    timestamp: float
    screenshot_ref: str | None = None          # path / data-url / artifact id
    visible_text: str = ""                     # scrubbed rendered text
    accessibility: Any = None                  # platform a11y snapshot (scrubbed)
    handle_candidates: tuple[SurfaceHandle, ...] = ()
    fingerprint: str = ""                      # visible-structure fingerprint
    previous_fingerprint_matched: bool | None = None


@dataclass(frozen=True)
class ActionReceipt:
    """Honest record of one performed gesture (or its failure)."""
    action: GuiAction
    status: str                                # "ok" | "failed" | "unavailable"
    surface_id: str
    epoch: str
    detail: str = ""
    artifact_ref: str | None = None            # e.g. post-action screenshot


@dataclass(frozen=True)
class VisualArtifact:
    """A captured rendering (screenshot) of one surface."""
    surface_id: str
    mime: str = "image/png"
    data: bytes | None = None                  # inline bytes (small artifacts)
    ref: str | None = None                     # path/URL when not inline
    captured_at: float = 0.0


# ── the two protocols (physically separate capability sets) ────────────────


@runtime_checkable
class SubstrateSession(Protocol):
    """Runtime capabilities ONLY (contract §2): observe / act / capture.

    There is deliberately no reset, no seed, no oracle read, no snapshot
    restore on this object. Those live on EvaluationEnvironment and are
    never reachable from here."""
    def list_surfaces(self) -> list[SurfaceInfo]: ...
    def observe(self, surface: str | SurfaceInfo,
                previous_fingerprint: str | None = None) -> Observation: ...
    def act(self, surface: str | SurfaceInfo, action: GuiAction, *,
            epoch: str) -> ActionReceipt: ...
    def capture(self, surface: str | SurfaceInfo) -> VisualArtifact: ...
    def close(self) -> None: ...


@runtime_checkable
class SubstrateProvider(Protocol):
    name: str
    def create_session(self, config: dict[str, Any] | None = None
                       ) -> SubstrateSession: ...


@runtime_checkable
class EvaluationEnvironment(Protocol):
    """Exam-room capabilities (contract §4): reset / seed / hidden oracle
    read / non-GT summary. Implemented per substrate in
    ``taskvm/substrate/<name>/evaluation.py``. Consumed by verifier +
    benchmark runners; MUST NOT be handed into the runtime decision chain
    (model prompts, patch compilation, rollback planning)."""
    def reset(self, sid: str) -> dict: ...
    def seed(self, sid: str, *, task_id: str | None, goal: str,
             seed_state: dict) -> dict: ...
    def oracle_state(self, sid: str) -> dict: ...
    def session_state(self, sid: str) -> dict: ...
    def close(self) -> None: ...


@runtime_checkable
class EvaluationProvider(Protocol):
    name: str
    def create(self, config: dict[str, Any] | None = None
               ) -> EvaluationEnvironment: ...


# ── registries (the ONLY place a substrate gets chosen) ────────────────────


class _BaseRegistry:
    """Name -> lazily-imported provider factory. Selection happens once at
    composition; afterwards callers hold a Protocol and never learn which
    subtree answered (contract §1)."""

    def __init__(self, entrypoints: dict[str, str]):
        # entrypoints: name -> "module.attr" of the provider class
        self._entrypoints = dict(entrypoints)
        self._cache: dict[str, Any] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._entrypoints))

    def _load(self, name: str):
        if name in self._cache:
            return self._cache[name]
        spec = self._entrypoints.get(name)
        if spec is None:
            # B-3 (Oracle audit): an unknown substrate is honest
            # unavailability — the port's own exception contract — never a
            # raw KeyError leaking to upper layers.
            raise SubstrateUnavailable(
                f"unknown substrate {name!r}; registered: {self.names}")
        module_name, _, attr = spec.partition(":")
        import importlib
        try:
            mod = importlib.import_module(module_name)
            provider = getattr(mod, attr)
        except (ImportError, AttributeError) as e:
            raise SubstrateUnavailable(
                f"substrate {name!r} ({spec}) is not importable in this "
                f"environment: {e}") from e
        self._cache[name] = provider
        return provider

    def register(self, name: str, spec: str) -> None:
        """Register an additional provider as "module.path:ClassName"."""
        self._entrypoints[name] = spec
        self._cache.pop(name, None)


class SubstrateRegistry(_BaseRegistry):
    """Runtime sessions. ``create_session(name, config)`` is the single
    composition point through which upper layers obtain a SubstrateSession."""

    def __init__(self,
                 entrypoints: dict[str, str] | None = None):
        super().__init__(entrypoints or {})

    def create_session(self, name: str,
                       config: dict[str, Any] | None = None) -> SubstrateSession:
        provider = self._load(name)
        try:
            return provider().create_session(config)
        except SubstrateUnavailable:
            raise
        except Exception as e:  # provider blew up constructing the env
            raise SubstrateUnavailable(
                f"substrate {name!r} failed to create a session: {e}") from e


class EvaluationRegistry(_BaseRegistry):
    """Evaluation environments (reset/seed/oracle). A separate registry on
    purpose: possession of one must not grant the other."""

    def __init__(self,
                 entrypoints: dict[str, str] | None = None):
        super().__init__(entrypoints or {})

    def create(self, name: str,
               config: dict[str, Any] | None = None) -> EvaluationEnvironment:
        provider = self._load(name)
        try:
            return provider().create(config)
        except SubstrateUnavailable:
            raise
        except Exception as e:
            raise SubstrateUnavailable(
                f"evaluation environment {name!r} failed to initialize: {e}"
            ) from e


# ── default registries (composition roots use these) ───────────────────────

#: Runtime sessions. Keys are substrate names; values are dotted provider
#: entrypoints, resolved lazily so importing the port stays side-effect-free.
DEFAULT_SUBSTRATES: dict[str, str] = {
    "builtin_web": "taskvm.substrate.builtin_web.provider:BuiltinWebProvider",
    "mobilegym": "taskvm.substrate.mobilegym.provider:MobileGymProvider",
    "osworld": "taskvm.substrate.osworld.provider:OSWorldProvider",
}

#: Evaluation environments. Same names, DIFFERENT objects/protocols.
DEFAULT_EVALUATION: dict[str, str] = {
    "builtin_web": "taskvm.substrate.builtin_web.provider:BuiltinWebEvaluationProvider",
    "mobilegym": "taskvm.substrate.mobilegym.provider:MobileGymEvaluationProvider",
    "osworld": "taskvm.substrate.osworld.provider:OSWorldEvaluationProvider",
}

substrate_registry = SubstrateRegistry(DEFAULT_SUBSTRATES)
evaluation_registry = EvaluationRegistry(DEFAULT_EVALUATION)
