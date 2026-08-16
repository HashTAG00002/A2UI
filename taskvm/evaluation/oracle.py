"""taskvm.evaluation.oracle — the hidden ground-truth grader.

Reads what ONLY the evaluation plane may read: the world's hidden
canonical state, its write ledger, and the pre-trial snapshot. Produces
the frozen verdict (handoff 07 §统计与报告: the success predicate is
pre-frozen at spec time; the oracle never re-derives it from behaviour).

Grading is conjunctive over three predicate groups:

1. **terminal state** — every ``(surface, key, value)`` in ``success``
   holds in the hidden canonical state at trial end;
2. **non-interference** — every protected ``(surface, key)`` is
   byte-identical between the pre-trial and post-trial snapshots;
3. **witness** — every witness triple appears in the write ledger as an
   ACCEPTED SYSTEM write (the no-op loophole stays closed: environment
   injections never satisfy a witness, and neither does standing still).

An oracle that itself crashes produces :class:`EvaluationError` — the
runner marks that trial ``evaluation_error`` and NEVER lets the failure
change what the system under test did (handoff 07 §权限隔离).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from taskvm.benchmark.schema import TaskSpec
from taskvm.evaluation.world import BenchmarkWorld, SYSTEM

__all__ = ["Oracle", "Verdict", "EvaluationError"]


class EvaluationError(RuntimeError):
    """The evaluation plane itself failed (oracle crash, corrupt world).
    The trial is graded ``evaluation_error`` — never silently dropped,
    never counted as a system failure or success."""


@dataclass(frozen=True)
class Verdict:
    """The frozen per-trial grading artifact (JSON-serializable)."""

    success: bool
    missing_writes: tuple[tuple[str, str, str], ...] = ()
    interference_violations: tuple[tuple[str, str, str, str], ...] = ()
    # (surface, key, pre_value, post_value)
    missing_witness: tuple[tuple[str, str, str], ...] = ()
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "missing_writes": [list(t) for t in self.missing_writes],
            "interference_violations": [
                list(t) for t in self.interference_violations],
            "missing_witness": [list(t) for t in self.missing_witness],
            "detail": self.detail,
        }

    @staticmethod
    def from_json(d: Mapping[str, Any]) -> "Verdict":
        return Verdict(
            success=bool(d["success"]),
            missing_writes=tuple(tuple(t) for t in d.get("missing_writes", ())),
            interference_violations=tuple(
                tuple(t) for t in d.get("interference_violations", ())),
            missing_witness=tuple(
                tuple(t) for t in d.get("missing_witness", ())),
            detail=str(d.get("detail", "")),
        )


class Oracle:
    """The grader. Constructed per trial from the spec; reads the world's
    hidden faces only at :meth:`grade` time."""

    def __init__(self, spec: TaskSpec) -> None:
        self._spec = spec

    def grade(self, world: BenchmarkWorld,
              pre_snapshot: Mapping[str, Mapping[str, str]]) -> Verdict:
        try:
            return self._grade(world, pre_snapshot)
        except Exception as e:            # noqa: BLE001 — honest wrap
            raise EvaluationError(
                f"oracle failure grading {self._spec.task_id}: "
                f"{type(e).__name__}: {e}") from e

    def _grade(self, world: BenchmarkWorld,
               pre_snapshot: Mapping[str, Mapping[str, str]]) -> Verdict:
        post = world.snapshot()
        missing: list[tuple[str, str, str]] = []
        for surf, kv in self._spec.success.items():
            for key, val in kv.items():
                actual = (post.get(surf) or {}).get(key)
                if actual != val:
                    missing.append((surf, key, val))

        inter: list[tuple[str, str, str, str]] = []
        for surf, key in self._spec.protected:
            pre_v = (pre_snapshot.get(surf) or {}).get(key)
            post_v = (post.get(surf) or {}).get(key)
            if pre_v != post_v:
                inter.append((surf, key, pre_v or "", post_v or ""))

        witnessed: set[tuple[str, str, str]] = set()
        for w in world.write_ledger():
            if w.actor == SYSTEM and w.accepted:
                witnessed.add((w.surface, w.key, w.new))
        miss_w: list[tuple[str, str, str]] = [
            t for t in self._spec.witness if t not in witnessed]

        ok = not missing and not inter and not miss_w
        parts: list[str] = []
        if missing:
            parts.append(f"{len(missing)} required write(s) not in place")
        if inter:
            parts.append(f"{len(inter)} protected field(s) modified")
        if miss_w:
            parts.append(f"{len(miss_w)} witness value(s) never written")
        return Verdict(
            success=ok,
            missing_writes=tuple(missing),
            interference_violations=tuple(inter),
            missing_witness=tuple(miss_w),
            detail="; ".join(parts) if parts else "all predicates hold",
        )
