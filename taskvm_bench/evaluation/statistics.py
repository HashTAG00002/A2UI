"""taskvm_bench.evaluation.statistics — honest aggregates over trials.

Handoff 07 §统计与报告: report confidence intervals, never a single best
sample; mean/majority gates, not max (project meta-rule 4). Everything
here is deterministic pure math over already-collected trial records —
no trial is dropped, evaluation errors are counted separately and never
silently folded into success or failure.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

__all__ = ["mean", "wilson_ci", "percentile", "safe_div"]


def safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def mean(xs: Iterable[float]) -> float:
    vals = list(xs)
    return sum(vals) / len(vals) if vals else 0.0


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Chosen over the normal approximation because it behaves honestly at
    small n and at 0/n or n/n (never a zero-width interval pretending
    certainty; never a bound outside [0, 1]).
    """
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (centre - margin) / denom),
            min(1.0, (centre + margin) / denom))


def percentile(xs: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (deterministic, no interpolation surprises).

    ``p`` in [0, 100]; empty input → 0.0.
    """
    if not xs:
        return 0.0
    s = sorted(xs)
    rank = max(1, math.ceil(p / 100.0 * len(s)))
    return s[rank - 1]
