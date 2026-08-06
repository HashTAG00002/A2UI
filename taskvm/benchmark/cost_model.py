"""Token-usage bookkeeping for TaskVM.

TaskVM's cost axis is a plain budget/reporting concern: "how many real tokens
did the W1 kill-test spend, so the report can print a cost line" — NOT a
research metric. (Compare SenseAct, where token cost IS a headline research
axis — its core claim is "active perception saves tokens vs. naive
observation," so it needs image-tile accounting, budget-exhaustion cutoffs,
and a tool-call trajectory to prove the savings. TaskVM makes no such claim;
those fields would be dead weight here, so this module does NOT port them.)

Two model roles (UI-gen compiler + compute-use executor) each get their own
``CostModel`` instance if needed — independent calls, no shared context
(handoff §6.4). W1 exercises only the compiler's CostModel live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CallUsage:
    """One LLM call's real token usage (as billed by the provider)."""
    prompt_tokens: int = 0        # input tokens (incl. any images sent)
    completion_tokens: int = 0    # output tokens (incl. reasoning, if any)
    reasoning_tokens: int = 0     # subset of completion_tokens (thinking tokens)
    cached_tokens: int = 0        # input tokens served from prompt cache
    tool: Optional[str] = None    # which call site this came from (e.g. "compile_binding")
    model: Optional[str] = None   # which model was called
    role: Optional[str] = None    # "compiler" | "compute_use" (which model role)


@dataclass
class CostModel:
    """Accumulates real token usage across an eval run (e.g. one kill-test)."""

    calls: list = field(default_factory=list, repr=False)  # list[CallUsage]

    def record_call(self, usage: CallUsage) -> int:
        """Record one LLM call. Returns its billed tokens (input+output)."""
        self.calls.append(usage)
        return usage.prompt_tokens + usage.completion_tokens

    @property
    def total_input_tokens(self) -> int:
        return sum(c.prompt_tokens for c in self.calls)

    @property
    def total_output_tokens(self) -> int:
        return sum(c.completion_tokens for c in self.calls)

    @property
    def total_reasoning_tokens(self) -> int:
        return sum(c.reasoning_tokens for c in self.calls)

    @property
    def total_cached_tokens(self) -> int:
        return sum(c.cached_tokens for c in self.calls)

    @property
    def total_tokens(self) -> int:
        """Headline cost: all billed input + output tokens across the run."""
        return self.total_input_tokens + self.total_output_tokens

    def reset(self) -> None:
        self.calls.clear()

    def summary(self) -> dict:
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_reasoning_tokens": self.total_reasoning_tokens,
            "total_cached_tokens": self.total_cached_tokens,
            "total_tokens": self.total_tokens,
            "n_llm_calls": len(self.calls),
            "calls": [
                {
                    "prompt_tokens": c.prompt_tokens,
                    "completion_tokens": c.completion_tokens,
                    "reasoning_tokens": c.reasoning_tokens,
                    "cached_tokens": c.cached_tokens,
                    "tool": c.tool,
                    "model": c.model,
                    "role": c.role,
                }
                for c in self.calls
            ],
        }
