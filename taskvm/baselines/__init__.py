"""Baselines — 5 reference binding-discovery methods for the W4 benchmark
(handoff §6 item 3 + §7).

Each baseline exposes ``discover_binding(trace, observed_entity_ids, *,
model=None, cost_model=None) -> dict`` returning the SAME shape the compiler
emits (``{"task_binding": {...}, "ok": bool, "error": str|None, "raw": str}``),
so a baseline slots into the binding-evaluation path via the same
``parse_compiler_output`` / ``binding_accuracy`` path. This lets the benchmark
score every baseline on the identical honesty contract (no model self-judge;
binding F1 vs the hidden GT).

Baselines (handoff §6 item 3):
  1. ``rule_type_match`` — deterministic: match entities by field-name/type
     heuristics (no model). The "is this just a dashboard?" floor.
  2. ``prompt_only`` — the frontier model with the A2UI spec but NO TaskVM
     binding contract (does the binding emerge from generic UI-gen alone?).
  3. ``frontier_shadow`` — the frontier model + TaskVM contract (== the main
     compiler, the system under test) — included as the "shadow" reference so
     the benchmark can A/B the main method against itself with variance.
  4. ``human_binding_upper_bound`` — the GT binding itself (the upper bound;
     confirms the scoring pipeline tops out at F1=1.0 and that the gate is
     reachable). NOT a baseline to beat — a calibration anchor.
  5. ``rule_plus_critic`` — rule_type_match produces a candidate, then a
     frontier-model critic refines it (the "cheap rule + light model" hybrid).

No-leak: baselines 1, 4 are deterministic (no fixture import beyond what the
orchestrator passes in). Baselines 2, 3, 5 call ``model_client`` (same proxy)
but NEVER import ``benchmark/fixtures`` — they see only the trace + observed ids,
same as the compiler.
"""
from __future__ import annotations

from taskvm.baselines.base import (BASELINES, BaselineResult, get_baseline,
                                    list_baselines)
from taskvm.baselines.rule_type_match import discover as rule_type_match
from taskvm.baselines.prompt_only import discover as prompt_only
from taskvm.baselines.frontier_shadow import discover as frontier_shadow
from taskvm.baselines.human_upper_bound import discover as human_upper_bound
from taskvm.baselines.rule_plus_critic import discover as rule_plus_critic

__all__ = ["BASELINES", "BaselineResult", "get_baseline", "list_baselines",
           "rule_type_match", "prompt_only", "frontier_shadow",
           "human_upper_bound", "rule_plus_critic"]
