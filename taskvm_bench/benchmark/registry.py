"""taskvm_bench.benchmark.registry — suites and system conditions.

The four primary system conditions (Agent F brief §7) answer ONE scientific
question: *under the same base model/CUA capability, the same tasks and a
shared budget object, does the TaskVM harness improve task completion,
controllability, recoverability and open-world behaviour over direct agent
execution?*

Condition fairness contract (handoff 07, reworded by A-08): the six
conditions are **structurally aligned under a shared ``TrialBudget``
object** — same task, same seed, same budget object; only the harness
differs. NOTE: that dataclass mixes axes the conditions consume separately
(direct/planner burn ``max_turns``; taskvm burns ``max_rounds`` plus the
runtime model-call caps — see ``evaluation/harness.py::TrialBudget``).
Unified provider-request / GUI-action / wall-clock hard caps are RM-1.0
freeze work and are NOT claimed today.

The two ablations are deliberately limited to what answers a contribution
claim (brief §8: 不能回答论文核心研究问题的 ablation 不做):

* ``taskvm-no-verifier``   — independent runtime verification OFF (CUA done
  == done). Isolates the value of ``CUA says done != TaskVM verified``.
* ``taskvm-no-replan``     — governance replan routing OFF (GoalPatch blocks
  instead of recomposing). Isolates the value of governed re-planning.

The ``taskvm-no-projection`` ablation is NOT registered: the projection
layer (Agent D) is not landed in the runtime path yet — an honest pending
dependency, not a silent omission.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from taskvm_bench.benchmark.tasks import all_tasks, get_task


class Condition(str, Enum):
    """A system condition = one harness configuration under test."""

    DIRECT_CUA = "direct-cua"
    PLANNER_CUA = "planner-cua"
    TASKVM = "taskvm"
    TASKVM_ORACLE_UPPER_BOUND = "taskvm-oracle-upper-bound"
    # limited ablations
    TASKVM_NO_VERIFIER = "taskvm-no-verifier"
    TASKVM_NO_REPLAN = "taskvm-no-replan"


#: Conditions that may NEVER appear as a main paper result: they consume
#: evaluation-plane secrets (oracle ground truth) and exist only as
#: diagnostic upper bounds. Reports must label them loudly.
DIAGNOSTIC_ONLY_CONDITIONS: frozenset[Condition] = frozenset({
    Condition.TASKVM_ORACLE_UPPER_BOUND,
})

#: The primary paper conditions, in report order.
PRIMARY_CONDITIONS: tuple[Condition, ...] = (
    Condition.DIRECT_CUA,
    Condition.PLANNER_CUA,
    Condition.TASKVM,
    Condition.TASKVM_ORACLE_UPPER_BOUND,
)

#: The limited ablation conditions.
ABLATION_CONDITIONS: tuple[Condition, ...] = (
    Condition.TASKVM_NO_VERIFIER,
    Condition.TASKVM_NO_REPLAN,
)


@dataclass(frozen=True)
class Suite:
    """A named selection of tasks + repeat policy."""

    suite_id: str
    task_ids: tuple[str, ...]
    description: str

    def tasks(self):
        return tuple(get_task(t) for t in self.task_ids)


_SMOKE_TASKS = ("seq-release-sync", "fanout-launch", "rollback-pricing")

SUITES: dict[str, Suite] = {
    "smoke": Suite(
        suite_id="smoke",
        task_ids=_SMOKE_TASKS,
        description="3-task infrastructure smoke: sequence + fan-out + "
                    "rollback. Validates the pipeline end to end; the "
                    "numbers are NOT paper numbers.",
    ),
    "final": Suite(
        suite_id="final",
        task_ids=tuple(t.task_id for t in all_tasks()),
        description="The full final taxonomy: every structural family and "
                    "every open-world split.",
    ),
    "open-world": Suite(
        suite_id="open-world",
        task_ids=("goalpivot-review", "rsvp-confirm", "venues-book",
                  "venues-rsvp"),
        description="All open-world splits: task holdout, operation "
                    "holdout, surface holdout, cross-product.",
    ),
    "governance": Suite(
        suite_id="governance",
        task_ids=("goalpivot-review", "localpatch-shift", "pause-hold",
                  "conflict-budget", "rollback-pricing", "send-announce",
                  "fanout-partial"),
        description="The governance/recovery suite: goal change, local "
                    "patch, hot interruption, conflict, rollback, "
                    "irreversibility, partial failure.",
    ),
}


def get_suite(suite_id: str) -> Suite:
    try:
        return SUITES[suite_id]
    except KeyError:
        raise KeyError(f"unknown suite {suite_id!r}; known: "
                       f"{sorted(SUITES)}") from None


def list_suites() -> tuple[Suite, ...]:
    return tuple(SUITES.values())


def all_conditions() -> tuple[Condition, ...]:
    return PRIMARY_CONDITIONS + ABLATION_CONDITIONS


def condition_of(name: str) -> Condition:
    try:
        return Condition(name)
    except ValueError:
        raise ValueError(
            f"unknown condition {name!r}; known: "
            f"{[c.value for c in all_conditions()]}") from None
