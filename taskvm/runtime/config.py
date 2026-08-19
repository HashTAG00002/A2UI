"""taskvm.runtime.config — the flat autonomy budget (runtime.md §5).

One layer, per-quantity ceilings (no triple-amp retry stacking).
``max_replans_per_task`` is NOT here:
replan is a governance/architect budget, runtime never replans.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeBudgets:
    """Per-task / per-contract ceilings. Hitting a ceiling is a SAFE stop
    (``BudgetExhausted`` runtime event + ``kernel.request_governance("pause")``),
    never a blind keep-running."""

    max_actions_per_contract: int = 12
    # CUA timeout / invalid JSON / unparseable / illegal action — NOT a GUI
    # action, but counts as a model call and is bounded separately (small).
    max_invalid_predictions_per_contract: int = 4
    # verifier-fail → context-preserving repair (carries current observation,
    # executed actions, discrepancy). Default 1: no full-Patch rerun loop.
    max_repairs_per_contract: int = 1
    # task-level CUA provider-call hard cap (sum of ok + invalid + repair).
    max_model_calls_per_task: int | None = 120
    # wall-clock seconds for the whole task (None = unbounded for tests).
    wall_clock_budget: float | None = None
    # inactive-surface heartbeat cadence (seconds). Active surface is driven
    # by CUA action observations, NOT this.
    inactive_heartbeat_seconds: float = 5.0

    def within_model_budget(self, calls_so_far: int) -> bool:
        if self.max_model_calls_per_task is None:
            return True
        return calls_so_far < self.max_model_calls_per_task


DEFAULT_BUDGETS = RuntimeBudgets()
