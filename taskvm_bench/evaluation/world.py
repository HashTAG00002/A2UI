"""taskvm_bench.evaluation.world — the deterministic evaluation world.

The exam room (handoff 07 §权限隔离): one :class:`BenchmarkWorld` per
trial, built from a frozen :class:`taskvm_bench.benchmark.schema.TaskSpec`.
It owns the hidden canonical state, the write ledger, the deterministic
injection engine and the loop-gesture business semantics.

Capability separation is PHYSICAL (substrate.md §2/§4):

* the system under test only ever meets the world through
  :class:`WorldSubstrate`, which implements the runtime-side
  ``SubstrateSession`` protocol (observe / act / capture / close — no
  reset, no seed, no oracle read);
* the environment controller (the runner) holds the ``BenchmarkWorld``
  reference and fires injections / takes hidden snapshots;
* the oracle reads the hidden ledger through :class:`.oracle.Oracle`.

Determinism contract: no clocks, no randomness, no dict-ordering hazards
(state rendering is sorted; fingerprints are sha256). Two trials of the
same (task, condition, seed) traverse the same world timeline byte for
byte — that is what the reproducibility test proves.

Write semantics: a write is either ``system`` (a GUI gesture through the
substrate — the ONLY write path a system under test has) or
``environment`` (an injection / eval-plane seeding). The injection
milestone counter counts SYSTEM writes only, so an injected external
change can never advance another injection's trigger.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from taskvm_bench.benchmark.schema import Injection, InjectionKind, TaskSpec
from taskvm.substrate import (
    ActionReceipt, GuiAction, Observation, SurfaceInfo, VisualArtifact,
)

__all__ = [
    "BenchmarkWorld", "WorldSubstrate", "WriteEvent", "ExternalEvent",
    "WriteRejected",
]

#: actors in the write ledger
SYSTEM = "system"
ENVIRONMENT = "environment"


class WriteRejected(RuntimeError):
    """The world refused a system write: unknown surface/key (nothing
    visible to write on) or a poisoned lane (external service failure).
    Honest failure — the caller surfaces it, never routes around it."""


@dataclass(frozen=True)
class WriteEvent:
    """One entry in the world's append-only write ledger."""

    seq: int                 # global write sequence (accepted or not)
    surface: str
    key: str
    old: str
    new: str
    actor: str               # SYSTEM | ENVIRONMENT
    accepted: bool


@dataclass(frozen=True)
class ExternalEvent:
    """An injection the world routed OUT to the environment controller
    (governance-shaped events the harness must deliver to its system)."""

    seq: int
    kind: InjectionKind
    payload: Mapping[str, Any]


ExternalCallback = Callable[[ExternalEvent], None]


class BenchmarkWorld:
    """The hidden canonical world + the deterministic event injector."""

    def __init__(self, spec: TaskSpec, *,
                 on_external: ExternalCallback | None = None) -> None:
        self._spec = spec
        self._state: dict[str, dict[str, str]] = {
            surf: dict(vals) for surf, vals in spec.seed.items()}
        self._poisoned: set[tuple[str, str]] = set()
        self._renamed: dict[tuple[str, str], str] = {}   # old key -> new key
        self._system_writes = 0
        self._write_seq = 0
        self._write_log: list[WriteEvent] = []
        self._notices: list[str] = []          # external-change notices
        self._external_seq = 0
        #: (surface, key) that a SYSTEM write has already landed on —
        #: the one-way door. TaskSpec.irreversibles names keys whose value
        #: cannot be changed again once the system has written it (the
        #: send that cannot be unsent). The compensation executor meets
        #: this door as an honest rejected write, never a hidden undo.
        self._irreversible: set[tuple[str, str]] = {
            (surf, k) for surf in spec.surfaces
            for k in spec.irreversibles}
        self._system_written: set[tuple[str, str]] = set()
        self._pending: list[Injection] = sorted(
            spec.injections, key=lambda i: i.after_writes)
        self._fired: list[Injection] = []
        self._on_external = on_external
        self._observe_seq = 0

    # ── environment-controller API (NEVER reachable from the substrate) ──
    @property
    def spec(self) -> TaskSpec:
        return self._spec

    def snapshot(self) -> dict[str, dict[str, str]]:
        """Hidden canonical snapshot (oracle / pre-trial baseline)."""
        return {s: dict(v) for s, v in self._state.items()}

    def restore(self, snap: dict[str, dict[str, str]]) -> None:
        """Eval-plane only: deterministic reset between trials. The runtime
        has no access to this method — systems that must undo work do it
        through real GUI compensation, never through a hidden restore."""
        self._state = {s: dict(v) for s, v in snap.items()}

    def write_ledger(self) -> tuple[WriteEvent, ...]:
        return tuple(self._write_log)

    def system_writes(self) -> int:
        return self._system_writes

    def fired_injections(self) -> tuple[Injection, ...]:
        return tuple(self._fired)

    # ── the write path ──────────────────────────────────────────────────
    def apply_write(self, surface: str, key: str, value: str, *,
                    actor: str) -> bool:
        """Apply one write. Returns True if accepted. A rejected SYSTEM
        write still lands in the ledger (honest failure accounting) but
        does NOT advance the injection milestone counter."""
        if surface not in self._state:
            raise WriteRejected(f"unknown surface {surface!r}")
        table = self._state[surface]
        old = table.get(key)
        if key not in table:
            if actor == ENVIRONMENT:
                # injections reference spec keys; a missing key is a
                # fixture bug — fail loudly at the eval plane
                raise WriteRejected(
                    f"injection targets unknown key {surface}.{key}")
            raise WriteRejected(
                f"nothing visible at {surface}.{key} — the field is not "
                f"on screen (renamed or absent)")
        if actor == SYSTEM and (surface, key) in self._poisoned:
            self._write_seq += 1
            self._write_log.append(WriteEvent(
                seq=self._write_seq, surface=surface, key=key,
                old=old or "", new=value, actor=actor, accepted=False))
            return False
        if (actor == SYSTEM and (surface, key) in self._irreversible
                and (surface, key) in self._system_written):
            # the one-way door: the system already wrote this key once —
            # a second system write (a compensation trying to unsend) is
            # rejected. Environment writes are exempt (eval-plane
            # seeding owns reality, not the system).
            self._write_seq += 1
            self._write_log.append(WriteEvent(
                seq=self._write_seq, surface=surface, key=key,
                old=old or "", new=value, actor=actor, accepted=False))
            return False
        self._write_seq += 1
        self._write_log.append(WriteEvent(
            seq=self._write_seq, surface=surface, key=key,
            old=old or "", new=value, actor=actor, accepted=True))
        table[key] = value
        if actor == SYSTEM:
            self._system_writes += 1
            self._system_written.add((surface, key))
            self._apply_gesture_side_effects(surface, key, value)
            self._fire_due_injections()
        return True

    def _apply_gesture_side_effects(self, surface: str, key: str,
                                    value: str) -> None:
        """The loop-gesture business button: writing the gesture key with
        the gesture value atomically moves one unit from ``decrement`` to
        ``increment`` (state-driven loop termination)."""
        g = self._spec.loop_gesture
        if not g or surface != self._spec.surfaces[0]:
            return
        gkey: str = str(g.get("key") or "")
        gval: str = str(g.get("value") or "")
        dec: str = str(g.get("decrement") or "")
        inc: str = str(g.get("increment") or "")
        if key != gkey or value != gval:
            return
        table = self._state[surface]
        if dec in table and table[dec].isdigit() and int(table[dec]) > 0:
            moved = table[dec]
            table[dec] = str(int(table[dec]) - 1)
            if inc in table and table[inc].isdigit():
                table[inc] = str(int(table[inc]) + 1)
            self._write_seq += 1
            self._write_log.append(WriteEvent(
                seq=self._write_seq, surface=surface, key=dec,
                old=moved, new=table[dec], actor=SYSTEM, accepted=True))

    # ── the deterministic injection engine ─────────────────────────────
    def begin_trial(self) -> None:
        """Environment controller ONLY: fire every injection whose milestone
        is ``after_writes=0`` — i.e. events that must land BEFORE the
        system under test performs its first gesture (a pre-launch UI
        rename, a lane poisoned from the start). Called once per trial,
        before the system starts; without it a 0-milestone injection
        would never fire (system writes alone never reach it)."""
        self._fire_due_injections()

    def _fire_due_injections(self) -> None:
        while (self._pending
               and self._pending[0].after_writes <= self._system_writes):
            inj = self._pending.pop(0)
            self._fired.append(inj)
            self._execute_injection(inj)

    def _execute_injection(self, inj: Injection) -> None:
        p = dict(inj.payload)
        if inj.kind is InjectionKind.EXTERNAL_FIELD_CHANGE:
            self.apply_write(p["surface"], p["key"], p["value"],
                             actor=ENVIRONMENT)
            self._notice(
                f"external change: {p['surface']} {p['key']} is now "
                f"{p['value']}")
        elif inj.kind is InjectionKind.UI_DRIFT:
            surface, old, new = p["surface"], p["old_key"], p["new_key"]
            table = self._state.get(surface, {})
            if old in table:
                table[new] = table.pop(old)
                self._renamed[(surface, old)] = new
                self._notice(
                    f"app update: the visible field {old} on {surface} "
                    f"is now labelled {new}")
        elif inj.kind is InjectionKind.LANE_FAILURE:
            self._poisoned.add((p["surface"], p["key"]))
            self._notice(
                f"service notice: writes to {p['key']} on {p['surface']} "
                f"are being rejected")
        else:
            # governance-shaped events route OUT to the harness
            self._external_seq += 1
            if self._on_external is not None:
                self._on_external(ExternalEvent(
                    seq=self._external_seq, kind=inj.kind, payload=p))

    def _notice(self, text: str) -> None:
        """External-change notices render into every subsequent
        observation (same information for every condition — the fairness
        contract). Worded so they contain no ``k=v`` token: parsing them
        as a field write must be impossible by construction."""
        self._notices.append(text)

    # ── visible rendering (the ONLY thing a system under test sees) ────
    def visible_text(self, surface: str) -> str:
        table = self._state.get(surface, {})
        rows = sorted(f"{k}={v}" for k, v in table.items())
        return " ".join(rows)

    def notices_text(self) -> str:
        return "\n".join(self._notices)

    def fingerprint(self, surface: str) -> str:
        """Structure fingerprint over THIS surface's rows only — external
        notices must not perturb structural-change detection."""
        digest = hashlib.sha256(
            self.visible_text(surface).encode("utf-8")).hexdigest()
        return f"w_{digest[:16]}"

    def surface_labels(self) -> tuple[str, ...]:
        return tuple(self._state.keys())


class WorldSubstrate:
    """The runtime-side view of the world: a real ``SubstrateSession``
    (observe / act / capture / close — nothing else). Writes happen ONLY
    through GUI gestures (``type`` with ``key=value`` text); every other
    gesture is an honest no-op click/scroll/wait (recorded, stateless).

    ``epoch`` on receipts is the caller's correlation token echoed back —
    this substrate keeps no authority of its own."""

    def __init__(self, world: BenchmarkWorld) -> None:
        self._world = world

    # convenience for the eval plane only (harness composition); the
    # runtime consumes this object ONLY through the protocol methods.
    @property
    def world(self) -> BenchmarkWorld:
        return self._world

    def list_surfaces(self) -> list[SurfaceInfo]:
        return [SurfaceInfo(surface_id=sid, display_name=sid)
                for sid in self._world.surface_labels()]

    def observe(self, surface: str | SurfaceInfo,
                previous_fingerprint: str | None = None) -> Observation:
        sid = surface if isinstance(surface, str) else surface.surface_id
        body = self._world.visible_text(sid)
        notices = self._world.notices_text()
        text = body + ("\n" + notices if notices else "")
        self._world._observe_seq += 1
        fp = self._world.fingerprint(sid)
        return Observation(
            surface=SurfaceInfo(surface_id=sid, display_name=sid),
            revision=self._world._observe_seq,
            timestamp=0.0,                      # determinism: no wall clock
            screenshot_ref=None,
            visible_text=text,
            fingerprint=fp,
            previous_fingerprint_matched=(
                previous_fingerprint == fp
                if previous_fingerprint is not None else None),
        )

    def act(self, surface: str | SurfaceInfo, action: GuiAction, *,
            epoch: str) -> ActionReceipt:
        sid = surface if isinstance(surface, str) else surface.surface_id
        if action.kind == "type" and action.text and "=" in action.text:
            key, _, val = action.text.partition("=")
            try:
                ok = self._world.apply_write(sid, key, val, actor=SYSTEM)
            except WriteRejected as e:
                return ActionReceipt(
                    action=action, status="failed", surface_id=sid,
                    epoch=epoch, detail=str(e))
            if not ok:
                return ActionReceipt(
                    action=action, status="failed", surface_id=sid,
                    epoch=epoch,
                    detail=f"write to {key} rejected by the service")
            return ActionReceipt(action=action, status="ok",
                                 surface_id=sid, epoch=epoch)
        # click / scroll / wait / key / open: honest stateless gestures
        return ActionReceipt(action=action, status="ok", surface_id=sid,
                             epoch=epoch)

    def capture(self, surface: str | SurfaceInfo) -> VisualArtifact:
        sid = surface if isinstance(surface, str) else surface.surface_id
        return VisualArtifact(surface_id=sid, ref=f"world://{sid}")

    def close(self) -> None:
        return None
