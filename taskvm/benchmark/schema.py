"""taskvm.benchmark.schema — the frozen data contract of the final benchmark.

This is NOT the legacy ``CanonicalTaskGraph`` stack (that lives on in
``taskvm/benchmark/fixtures.py`` for the still-supported legacy workspace UI;
its deletion owner is Agent G). This module defines what a FINAL benchmark
task is: a structurally-distinct scenario with a natural-language goal, a
deterministic seed, a frozen success predicate, an optional deterministic
event-injection script, and an explicit open-world split tag.

Design principles (handoff 07 + Agent F brief):

* **Task diversity = structural families**, not parameterized copies. Every
  ``TaskSpec`` must answer *which scientific question* it exists for.
* **Fixture sets the exam room, never hands answers.** ``seed`` /
  ``success`` / ``protected`` are read by the Evaluation plane only; the
  system under test receives ``goal`` + runtime-visible observation.
* **Open-world claims are labelled.** A split tag of ``SURFACE_HOLDOUT``
  means the runtime has never seen that surface in any ID task — adding a
  new operator inside a known app is NOT full open-world generalization.
* **Injections are deterministic.** Each carries the trigger (a stable
  milestone in trial progress) and an explicit revision so repeated runs
  with the same seed reproduce identically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


# ── structural task families ────────────────────────────────────────────────

class Family(str, Enum):
    """The structurally distinct task families of the taxonomy.

    A family is a *control-flow / governance structure*, not an app. Tasks
    in the same family differ in app surface and content; tasks in
    different families differ in the structure the harness must survive.
    """

    SEQUENCE = "sequence"                  # ordered steps with dependencies
    FANOUT_FANIN = "fanout_fanin"          # one intent → independent lanes → barrier
    BOUNDED_LOOP = "bounded_loop"          # repeat with termination predicate
    CROSS_APP = "cross_app"                # one semantic change across ≥2 apps
    GOAL_PATCH = "goal_patch"              # mid-run terminal-goal change
    LOCAL_PATCH = "local_patch"            # mid-run local-target change
    INTERRUPTION = "interruption"          # hot pause mid-run (latency probe)
    CONFLICT = "conflict"                  # external concurrent edit → conflict
    ROLLBACK = "rollback"                  # user asks to return to a checkpoint
    UI_DRIFT = "ui_drift"                  # visible structure changes mid-run
    PARTIAL_FAILURE = "partial_failure"    # one lane/step fails, others must survive
    IRREVERSIBLE = "irreversible"          # an honest unrecoverable boundary


class Split(str, Enum):
    """Open-world split labels (reported separately — never merged).

    * ``ID`` — in-distribution: apps/operations/structure all seen in the
      ID suite.
    * ``TASK_HOLDOUT`` — an unseen *composition* of known apps/operations.
    * ``OPERATION_HOLDOUT`` — an operation (field semantics) absent from
      every ID task.
    * ``SURFACE_HOLDOUT`` — an app/surface absent from every ID task; the
      runtime must not own that app's selector/operator mapping/adapter/GT
      binding (only the evaluator may seed/judge it).
    * ``CROSS_PRODUCT`` — recombination of held-out operations × held-out
      surfaces (the strongest generalization claim).
    """

    ID = "id"
    TASK_HOLDOUT = "task_holdout"
    OPERATION_HOLDOUT = "operation_holdout"
    SURFACE_HOLDOUT = "surface_holdout"
    CROSS_PRODUCT = "cross_product"


# ── deterministic injections (the unified event injector script) ────────────

class InjectionKind(str, Enum):
    """What the environment controller injects mid-trial. All injections
    are EXTERNAL events (other actors / the user), delivered through legal
    surfaces: eval-plane hidden writes for world drift, kernel governance
    patches for user gestures. Production code is never modified."""

    EXTERNAL_FIELD_CHANGE = "external_field_change"  # another actor edits a field
    GOAL_PATCH = "goal_patch"                        # user changes terminal goal
    LOCAL_PATCH = "local_patch"                      # user changes a local target
    ROLLBACK_REQUEST = "rollback_request"            # user returns to a checkpoint
    PAUSE_RESUME = "pause_resume"                    # hot interruption
    UI_DRIFT = "ui_drift"                            # visible relabel/restructure
    LANE_FAILURE = "lane_failure"                    # one branch fails externally


@dataclass(frozen=True)
class Injection:
    """One deterministic external event, fired when the trial reaches
    ``after_writes`` visible state writes (0 = before the system starts).

    ``payload`` carries only what the injection needs: e.g. target surface +
    key + new value for a field change; the new goal text for a goal patch.
    Every payload is JSON-serializable so trials reproduce exactly."""

    kind: InjectionKind
    after_writes: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", dict(self.payload))
        if self.after_writes < 0:
            raise ValueError("Injection.after_writes must be >= 0")


# ── the task specification ──────────────────────────────────────────────────

@dataclass(frozen=True)
class TaskSpec:
    """One final-benchmark task.

    ``goal`` is the ONLY string a system-under-test receives (plus whatever
    the runtime-visible observation shows). ``seed`` / ``success`` /
    ``protected`` are Evaluation-plane secrets — the fixture arranges the
    exam room and grades the paper; it never hands the candidate answers.

    ``success`` is frozen at spec definition time: ``{surface: {key: value}}``
    in the hidden canonical state at trial end. ``protected`` is the field
    level non-interference set: ``((surface, key), ...)`` that must be
    byte-identical between the pre-trial and post-trial snapshots.

    ``witness`` closes the no-op loophole for trajectory tasks (ROLLBACK in
    particular): ``(surface, key, value)`` triples that must APPEAR in the
    trial's write history before the end state is graded. A rollback task
    whose final state equals its seed would otherwise award full marks to a
    system that never touched the world — the witness proves the system
    actually performed the work it later undid. Both predicates must hold.
    """

    task_id: str
    family: Family
    split: Split
    goal: str
    surfaces: tuple[str, ...]                 # surface labels the world offers
    seed: Mapping[str, Mapping[str, str]]     # {surface: {key: initial value}}
    success: Mapping[str, Mapping[str, str]]  # {surface: {key: required value}}
    protected: tuple[tuple[str, str], ...] = ()
    injections: tuple[Injection, ...] = ()
    irreversibles: tuple[str, ...] = ()       # keys whose writes cannot be undone
    loop_gesture: Mapping[str, Any] | None = None  # world-level business button
    witness: tuple[tuple[str, str, str], ...] = ()  # passed-through values
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.task_id or not self.goal.strip():
            raise ValueError("TaskSpec needs task_id and a non-empty goal")
        for surf in self.success:
            if surf not in self.surfaces:
                raise ValueError(
                    f"{self.task_id}: success references unknown surface {surf!r}")
        for surf in self.seed:
            if surf not in self.surfaces:
                raise ValueError(
                    f"{self.task_id}: seed references unknown surface {surf!r}")
        object.__setattr__(self, "seed", {s: dict(v)
                                          for s, v in self.seed.items()})
        object.__setattr__(self, "success", {s: dict(v)
                                             for s, v in self.success.items()})

    # ── convenience views (eval-plane only) ────────────────────────────────
    @property
    def required_writes(self) -> tuple[tuple[str, str, str], ...]:
        """All (surface, key, value) the hidden success predicate requires."""
        out: list[tuple[str, str, str]] = []
        for surf, kv in self.success.items():
            for key, val in kv.items():
                initial = (self.seed.get(surf) or {}).get(key)
                if initial != val:
                    out.append((surf, key, val))
        return tuple(out)

    def n_interference_targets(self) -> int:
        return len(self.protected)
