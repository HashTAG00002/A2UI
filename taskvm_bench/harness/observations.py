"""Observation dataclasses — the compiler's INPUT types (NOT ground-truth).

Co-located here (not in ``benchmark/fixtures.py``) so the compiler path
(``task_state/``, ``execution/``) can import them WITHOUT transitively pulling
in the verifier-only GT (``fixtures.py``). This keeps the no-leak boundary
enforceable by static analysis: ``task_state/`` and ``execution/`` never import
``benchmark/fixtures.py`` or ``harness/replay_engine`` (which imports fixtures).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StepObservation:
    """One app's rendered state as observed by the compiler."""
    app: str
    step: int
    dom_html: str                       # rendered DOM (GET /<sid>), with data-*-id + data-field
    a11y_text: str                      # synthesized text representation (parsed from DOM)
    screenshot_path: str | None = None  # optional PNG (W1: usually None; DOM+a11y suffice)
    action_taken: str | None = None


@dataclass
class TraceFixture:
    """The compiler's full input: task meta + per-app final observations."""
    task_id: str
    goal: str
    final_obs: dict[str, StepObservation]   # {app_name: StepObservation}
