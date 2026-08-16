"""Baseline 5 — rule + critic (cheap rule produces a candidate, frontier model
refines it). The "cheap rule + light model" hybrid (handoff §6 item 3).

Flow: ``rule_type_match`` produces a candidate task_binding (deterministic,
cheap). A frontier-model critic then reviews the candidate against the
observations + tool schema and may ADD missing bindings, REMOVE spurious ones,
or MERGE/SPLIT var_ids per the granularity heuristic. Cheaper than full
compiler generation (the model edits, doesn't generate from scratch) — tests
whether a rule seed + light model edit beats pure rule or pure model.

No-leak: rule seed from observations + OPERATOR_REGISTRY; critic sees only the
trace + observed ids + candidate (never fixtures).
"""
from __future__ import annotations

import json
from typing import Any

from taskvm_bench.baselines.base import _ok, register
from taskvm_bench.baselines.rule_type_match import discover as rule_discover
from taskvm_bench.benchmark import model_client
from taskvm_bench.benchmark.cost_model import CostModel
from taskvm_bench.harness.observations import TraceFixture
from taskvm_bench.task_state.entity_binding import build_tool_schema

_BASELINE_ROLE = "rule_plus_critic"


def _critic_prompt(trace: TraceFixture, observed_entity_ids: dict[str, set[str]],
                   candidate: dict) -> tuple[str, str]:
    sys = ("You are a binding critic. You are given a candidate task_binding "
           "(from a rule/type-match heuristic) + the observed app states + the "
           "tool schema. Edit the candidate to fix errors: add MISSING bindings "
           "(entities the rule missed), REMOVE spurious ones (entities not "
           "relevant to the task goal), and MERGE/SPLIT var_ids per this rule: "
           "one shared var_id when one operator+value applies to multiple "
           "entities that track the same quantity; separate var_ids when values "
           "differ. Output ONLY the edited task_binding JSON "
           '{"task_id","variables":[{"var_id","label","value","editable","bindings":'
           '[{"app","entity_id","field","operator"}]}],"dependencies":[]}.')
    lines = [f"# Task", f"task_id: {trace.task_id}", f"goal: {trace.goal}", ""]
    lines.append("# Observed application states")
    for app, obs in trace.final_obs.items():
        lines.append(f"\n## App: {app}")
        lines.append(obs.a11y_text)
    lines.append("")
    lines.append(build_tool_schema(list(trace.final_obs.keys())))
    lines.append("")
    lines.append("# Valid entity ids")
    for app, ids in observed_entity_ids.items():
        lines.append(f"- {app}: {sorted(ids)}")
    lines.append("")
    lines.append("# Candidate task_binding (from rule/type-match — EDIT this):")
    lines.append(json.dumps(candidate, ensure_ascii=False))
    return sys, "\n".join(lines)


def discover(trace: TraceFixture, observed_entity_ids: dict[str, set[str]],
             *, model: str | None = None, cost_model: CostModel | None = None,
             temperature: float | None = None, **kw) -> dict:
    """Rule seed + frontier critic. The rule produces a candidate; the model
    edits it (add/remove/merge-split). Falls back to the rule candidate if the
    critic fails to return valid JSON."""
    cand = rule_discover(trace, observed_entity_ids)
    candidate = cand.get("task_binding")
    if candidate is None:
        return cand   # rule itself failed (no observable date/location/state)
    sys_prompt, user_prompt = _critic_prompt(trace, observed_entity_ids, candidate)
    parsed, raw, resp = model_client.complete_json(
        sys_prompt, user_prompt, max_tokens=3072, temperature=temperature, model=model)
    if cost_model is not None and resp is not None:
        model_client.record_usage(resp, cost_model, tool="baseline_rule_plus_critic",
                                  role=_BASELINE_ROLE,
                                  model=model or model_client.TASKVM_DEFAULT_MODEL)
    tb = parsed if isinstance(parsed, dict) and "variables" in parsed else None
    if tb is None:
        # critic failed — honest fallback to the rule candidate
        return _ok(raw=raw or "", tb=candidate,
                   error="critic_returned_no_valid_json (fell back to rule candidate)")
    tb.setdefault("task_id", trace.task_id)
    tb.setdefault("dependencies", [])
    return _ok(raw=raw or "", tb=tb)


register("rule_plus_critic", discover, role="hybrid", uses_model=True,
         description="rule/type-match seed + frontier-model critic that "
                    "adds/removes/merges-splits — cheap rule + light model edit")
