"""Baseline registry + shared result shape."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from taskvm.harness.observations import TraceFixture


# A baseline discover function: (trace, observed_ids, **kwargs) -> result dict
# (same shape as task_state.compiler.compile_binding's output).
DiscoverFn = Callable[..., dict]


@dataclass
class BaselineResult:
    """Metadata for one baseline (name + its discover fn + a one-line role)."""
    name: str
    discover: DiscoverFn
    role: str          # "floor" | "ablation" | "reference" | "upper_bound" | "hybrid"
    uses_model: bool
    description: str


BASELINES: dict[str, BaselineResult] = {}


def register(name: str, discover: DiscoverFn, *, role: str, uses_model: bool,
             description: str) -> BaselineResult:
    res = BaselineResult(name=name, discover=discover, role=role,
                         uses_model=uses_model, description=description)
    BASELINES[name] = res
    return res


def get_baseline(name: str) -> BaselineResult:
    if name not in BASELINES:
        raise KeyError(f"unknown baseline {name!r}; known: {list(BASELINES)}")
    return BASELINES[name]


def list_baselines() -> list[BaselineResult]:
    return list(BASELINES.values())


def _empty_binding_dict(trace: TraceFixture) -> dict:
    """A structurally-valid empty task_binding (ok=True, zero variables)."""
    return {"task_id": trace.task_id, "variables": [], "dependencies": []}


def _ok(raw: str, tb: dict, error: str | None = None) -> dict:
    """Build the standard result dict (mirrors compiler.compile_binding output)."""
    return {"raw": raw, "parsed": {"task_binding": tb} if tb else None,
            "ok": tb is not None, "text_response": None, "a2ui": [],
            "task_binding": tb, "error": error}
