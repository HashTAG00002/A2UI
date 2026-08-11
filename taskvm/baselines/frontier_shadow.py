"""Baseline 3 — frontier+shadow (== the main compiler, the system under test).

This is the main TaskVM compiler (``task_state.compiler.compile_binding`` with
the full A2UI spec + TaskVM binding contract, binding_only=True), wrapped as a
baseline so the benchmark can A/B the main method against itself across runs
(variance characterization) AND so a caller can score "the real method" through
the uniform baseline harness. (handoff §6 item 3.)

No-leak: delegates to ``task_state.compiler`` which is already no-leak.
"""
from __future__ import annotations

from taskvm.baselines.base import register
from taskvm.benchmark.cost_model import CostModel
from taskvm.harness.observations import TraceFixture
from taskvm.task_state.compiler import compile_binding

_BASELINE_ROLE = "frontier_shadow"


def discover(trace: TraceFixture, observed_entity_ids: dict[str, set[str]],
             *, model: str | None = None, cost_model: CostModel | None = None,
             temperature: float | None = None, **kw) -> dict:
    """The main compiler (full A2UI spec + TaskVM contract, binding_only)."""
    return compile_binding(trace, observed_entity_ids, model=model,
                           temperature=temperature, cost_model=cost_model,
                           binding_only=True)


register("frontier_shadow", discover, role="reference", uses_model=True,
         description="the main TaskVM compiler (full spec + binding contract) — "
                    "the system under test, wrapped for A/B variance characterization")
