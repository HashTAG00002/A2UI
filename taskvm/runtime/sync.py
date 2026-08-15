"""taskvm.runtime.sync — observation-driven synchronization (runtime.md §8).

Two regimes:

- **Active surface**: the surface the CUA is operating. Its observations come
  from the CUA's own actions (act → re-observe) — that IS the main sync signal.
  The heartbeat must NOT re-poll it (duplicate observation, runtime.md §8).
- **Inactive surfaces**: everything else. A low-frequency heartbeat fills in
  the world changes the CUA is not looking at. Fingerprint-unchanged ⇒ 0 model
  calls and 0 compiler calls; known-handle value change ⇒ a deterministic
  observation delta; unrecoverable binding ⇒ a ``StructureInvalidated`` event
  the runtime publishes WITHOUT calling the State Compiler itself (E executes,
  C understands — the event is routed to C's slow path by composition).
"""
from __future__ import annotations

from dataclasses import dataclass

from taskvm.domain.state import ObservedValue, TaskVariable
from taskvm.substrate import Observation

from taskvm.runtime.ports import (
    ObservationExtractor, RuntimeEvent, RuntimeEventKind,
)


class StructureInvalidation(Exception):
    """The extractor could not recover a binding from the visible structure.

    Raised by the injected ``ObservationExtractor`` when an anchor disappeared
    / a binding cannot be recovered / the UI structure drifted materially.
    The runtime catches this and publishes a ``StructureInvalidated`` runtime
    event; it does NOT call the State Compiler itself (runtime.md §8).
    """


@dataclass
class _SurfaceState:
    surface_id: str
    last_fingerprint: str = ""


class SurfaceSync:
    """Tracks per-surface fingerprints and routes active vs inactive
    observation. Holds no authoritative state — the kernel is the truth; this
    only decides what to observe and what to publish."""

    def __init__(self, kernel, substrate: object,
                 extractor: ObservationExtractor,
                 surfaces: list[str]) -> None:
        self._kernel = kernel
        self._substrate = substrate
        self._extractor = extractor
        self._states: dict[str, _SurfaceState] = {
            s: _SurfaceState(s) for s in surfaces}
        self._active: str | None = None
        if surfaces:
            self._active = surfaces[0]

    def set_active(self, surface_id: str) -> None:
        self._active = surface_id

    @property
    def active_surface(self) -> str | None:
        return self._active

    @property
    def surfaces(self) -> tuple[str, ...]:
        return tuple(self._states)

    def observe_active(self, surface_id: str | None = None) -> Observation:
        """Observe the active surface (driven by the CUA loop, not heartbeat).
        Updates the fingerprint so the inactive poll can skip this surface."""
        sid = surface_id or self._active
        if sid is None:
            raise ValueError("no active surface set")
        obs = self._substrate.observe(sid)
        st = self._states.get(sid)
        if st is not None:
            st.last_fingerprint = obs.fingerprint
        return obs

    def poll_inactive(self) -> list[RuntimeEvent]:
        """Low-frequency heartbeat for surfaces the CUA is NOT driving.

        Fingerprint unchanged ⇒ no model call, no compiler call (runtime.md
        §8 fast path). Recoverable value change ⇒ deterministic delta →
        ``apply_observation``. Unrecoverable binding ⇒
        ``StructureInvalidated`` (the runtime does not call the compiler).
        External drift vs pending desired ⇒ ``record_conflict`` (no silent
        overwrite, only the affected surface pauses).
        """
        events: list[RuntimeEvent] = []
        epoch = self._kernel.epoch
        variables = {v.semantic_key: v
                     for v in self._kernel.task_state().variables}
        for sid, st in self._states.items():
            if sid == self._active:
                continue  # the active surface is driven by CUA observations
            obs = self._substrate.observe(
                sid, previous_fingerprint=st.last_fingerprint)
            if (obs.previous_fingerprint_matched is True
                    or (st.last_fingerprint and obs.fingerprint == st.last_fingerprint)):
                continue  # 0 model calls, 0 compiler calls
            try:
                values = self._extractor.extract(obs, variables)
            except StructureInvalidation as e:
                events.append(RuntimeEvent(
                    kind=RuntimeEventKind.STRUCTURE_INVALIDATED,
                    epoch=epoch, surface_id=sid, detail=str(e)))
                continue
            st.last_fingerprint = obs.fingerprint
            desired = self._kernel.task_state().desired_values()
            diverged = [ov.semantic_key for ov in values
                        if (ov.semantic_key in desired
                            and ov.value != desired[ov.semantic_key])]
            if diverged:
                # external drift on a surface we are not driving → conflict,
                # NOT a silent overwrite. Only the affected surface pauses.
                cid = self._kernel.record_conflict(
                    f"inactive surface {sid!r} drifted from desired",
                    diverged)
                events.append(RuntimeEvent(
                    kind=RuntimeEventKind.SURFACE_CONFLICT, epoch=epoch,
                    surface_id=sid, detail=f"conflict {cid}",
                    payload={"keys": list(diverged)}))
            elif values:
                self._kernel.apply_observation(values)
        return events

    def fold_action_observation(self, values: list[ObservedValue]) -> None:
        """Apply the active surface's post-action observation delta into the
        kernel's OBSERVED plane. Called by the autonomy loop as part of
        ``finish_action`` — the active surface's own action is never a
        'conflict' (it is self-caused)."""
        if values:
            self._kernel.apply_observation(values)
