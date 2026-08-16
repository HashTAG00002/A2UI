"""Projection policy — which task variables are user-visible/editable (rule stub).

Demoted per handoff §5/§15-Q4: SCF is a derivative contribution, not first-class.
``projection_policy`` stays in the architecture but is a simple rule/heuristic in
W1 — NO Pareto-frontier experiment, NO learned policy. The full three-axis
measurement (coverage × round-trip-fidelity × interaction-compression) is
Discussion / Future Work.

W1 rule: every variable flagged ``editable=True`` by the compiler is shown to
the user; non-editable (derived/display-only) variables are shown read-only.
"""
from __future__ import annotations

from taskvm_bench.task_state.representation import TaskVariable


def decide_visible(variables: list[TaskVariable]) -> list[TaskVariable]:
    """W1 rule: all variables are visible; editable ones are manipulable.
    Returns the visible set (caller renders editable vs read-only)."""
    return list(variables)


def compression_ratio(variables: list[TaskVariable], total_app_entities: int) -> float:
    """Diagnostic for Discussion: visible variables / total app entities.
    Not a W1 gate metric — collected for the SCF narrative."""
    if total_app_entities <= 0:
        return 0.0
    return len(variables) / total_app_entities
