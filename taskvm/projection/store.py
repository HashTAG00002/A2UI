"""taskvm.projection.store — the composition seam (contract §5).

Projection cannot create sessions (the architecture gate bans importing
``taskvm.substrate`` from this package). The composition root —
bootstrap / integration / tests — constructs kernel + runtime +
substrate and REGISTERS the bundle here. Everything the UI serves is read
from the registered objects' public facades.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from taskvm.kernel import TaskVMKernel


# ── artifacts (read-only serving; production happens elsewhere) ────────────

@dataclass(frozen=True)
class StoredArtifact:
    """One immutable captured rendering. ``ref`` is the opaque token the
    runtime/composition used (e.g. an ``observation.screenshot_ref``) —
    never an app-internal id."""

    ref: str
    mime: str
    data: bytes
    captured_at: float


class ArtifactStore:
    """ref → bytes, per session. Populated ONLY by registration /
    composition pushes (contract §5: no capture-on-click, no substrate
    side effects on read). Serving is read-only and thread-safe."""

    def __init__(self) -> None:
        self._items: dict[str, StoredArtifact] = {}
        self._lock = threading.Lock()

    def put(self, ref: str, data: bytes, *, mime: str = "image/png",
            captured_at: float | None = None) -> None:
        if not ref:
            raise ValueError("artifact ref must be non-empty")
        with self._lock:
            self._items[ref] = StoredArtifact(
                ref=ref, mime=mime, data=bytes(data),
                captured_at=captured_at if captured_at is not None
                else time.time())

    def get(self, ref: str) -> StoredArtifact | None:
        with self._lock:
            return self._items.get(ref)

    def has(self, ref: str) -> bool:
        with self._lock:
            return ref in self._items

    def latest_ref(self, refs: Iterable[str]) -> str | None:
        """Newest stored artifact among ``refs`` (by captured_at)."""
        with self._lock:
            stored = [self._items[r] for r in refs if r in self._items]
        if not stored:
            return None
        return max(stored, key=lambda a: a.captured_at).ref


# ── surface declarations (composition may pre-declare known surfaces) ──────

@dataclass(frozen=True)
class SurfaceDecl:
    """A user-visible surface the composition declares at registration.
    ``display_name`` is what a person would call it (a window title);
    ``surface_id`` is the opaque ephemeral token runtime events carry."""

    surface_id: str
    display_name: str


# ── the injected ports (structurally-typed; see services/) ─────────────────

class GovernancePortLike(Protocol):
    """Structural port implemented by ``KernelGovernancePort`` (default)
    or a composition adapter around the GovernanceService.

    pause / resume / stop are NO LONGER on this port — they route
    through the ``DriverPortLike`` (driver → runtime → kernel, single owner)."""

    def local_patch(self, updates: dict, rationale: str = "") -> dict: ...
    def goal_patch(self, *, goal: str, constraints: Iterable[str] = (),
                   scope: Iterable[str] = (),
                   success_criteria: Iterable[str] = (),
                   rationale: str = "") -> dict: ...
    def checkpoint(self, label: str) -> dict: ...
    def rollback(self, target_checkpoint_id: str,
                 rationale: str = "") -> dict: ...
    def resolve_conflict(self, conflict_id: str, resolution: str,
                         detail: str = "") -> dict: ...


class DriverPortLike(Protocol):
    """Structural port implemented by ``ThreadedRuntimeDriver`` (default)
    or a composition-provided driver."""

    def start(self) -> str: ...
    def pause(self) -> str: ...
    def resume(self) -> str: ...
    def stop(self) -> str: ...
    def status(self) -> str: ...
    def execute_compensation(self, plan: Any) -> str: ...
    def join(self, timeout: float | None = None) -> None: ...


# ── the session bundle ─────────────────────────────────────────────────────

@dataclass
class ProjectionSession:
    """One registered task session: the kernel (truth) + the optional
    runtime (facts) + the artifacts + the injected ports."""

    sid: str
    kernel: TaskVMKernel
    runtime: Any = None                    # AutonomyRuntime | None (duck)
    governance: GovernancePortLike | None = None
    driver: DriverPortLike | None = None
    surfaces: tuple[SurfaceDecl, ...] = ()
    artifacts: ArtifactStore = field(default_factory=ArtifactStore)
    created_at: float = field(default_factory=time.time)
    #: optional composition hook exposing the unified model-call count for
    #: the governance bar (contract §3 regression is test-pinned at 0).
    model_call_probe: Callable[[], int] | None = None

    def governance_port(self) -> GovernancePortLike:
        if self.governance is None:
            from taskvm.projection.services.governance import (
                KernelGovernancePort,
            )
            self.governance = KernelGovernancePort(self.kernel)
        return self.governance


class ProjectionSessionStore:
    """sid → ProjectionSession. The ONLY way a session enters the UI."""

    def __init__(self) -> None:
        self._sessions: dict[str, ProjectionSession] = {}
        self._lock = threading.Lock()

    def register(self, sid: str, kernel: TaskVMKernel, *, runtime: Any = None,
                 governance: GovernancePortLike | None = None,
                 driver: DriverPortLike | None = None,
                 surfaces: Iterable[SurfaceDecl] = (),
                 artifacts: ArtifactStore | None = None,
                 model_call_probe: Callable[[], int] | None = None,
                 ) -> ProjectionSession:
        if not sid:
            raise ValueError("sid must be non-empty")
        sess = ProjectionSession(
            sid=sid, kernel=kernel, runtime=runtime,
            governance=governance, driver=driver,
            surfaces=tuple(surfaces),
            artifacts=artifacts or ArtifactStore(),
            model_call_probe=model_call_probe)
        with self._lock:
            if sid in self._sessions:
                raise ValueError(f"session {sid!r} already registered")
            self._sessions[sid] = sess
        return sess

    def get(self, sid: str) -> ProjectionSession | None:
        with self._lock:
            return self._sessions.get(sid)

    def drop(self, sid: str) -> bool:
        with self._lock:
            return self._sessions.pop(sid, None) is not None

    def sids(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions)
