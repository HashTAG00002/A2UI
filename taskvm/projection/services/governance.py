"""taskvm.projection.services.governance — governance command port
(contract §7: governance UX routes).

All commands return a dict with at least ``ok: bool`` and ``reason: str``.
The kernel is the sole mutation owner — this port merely translates HTTP
intent into kernel calls and formats the response. No substrate access,
no model calls (contract §3).
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from taskvm.domain.intent import TaskIntent
from taskvm.domain.patch import GoalPatch, LocalPatch, VariableUpdate
from taskvm.kernel import TaskVMKernel


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


class KernelGovernancePort:
    """Default governance port: thin wrapper over the kernel facade.

    The kernel's real API uses typed patch objects (``LocalPatch`` /
    ``GoalPatch``) — this port constructs them from the HTTP payload.
    """

    def __init__(self, kernel: TaskVMKernel) -> None:
        self._kernel = kernel

    # ── autonomy control (request_governance) ─────────────────────────────

    def pause(self, rationale: str = "") -> dict[str, Any]:
        self._kernel.request_governance("pause", detail=rationale)
        return {"ok": True, "action": "paused", "reason": rationale}

    def resume(self, rationale: str = "") -> dict[str, Any]:
        self._kernel.request_governance("resume", detail=rationale)
        return {"ok": True, "action": "resumed", "reason": rationale}

    def stop(self, rationale: str = "") -> dict[str, Any]:
        self._kernel.request_governance("stop", detail=rationale)
        return {"ok": True, "action": "stopped", "reason": rationale}

    # ── state editing (LocalPatch path) ───────────────────────────────────

    def local_patch(self, updates: dict, rationale: str = "") -> dict[str, Any]:
        """``updates`` is ``{semantic_key: new_desired_value}``."""
        vups = [VariableUpdate(semantic_key=k, new_value=v)
                for k, v in updates.items()]
        patch = LocalPatch(
            patch_id=f"lp-{int(time.time()*1000)}",
            variable_updates=vups,
            rationale=rationale)
        result = self._kernel.apply_local_patch(patch)
        return {"ok": True, "action": "local_patch",
                "result": _jsonable(result)}

    # ── goal recomposition (GoalPatch path) ───────────────────────────────

    def goal_patch(self, *, goal: str, constraints: Iterable[str] = (),
                   scope: Iterable[str] = (),
                   success_criteria: Iterable[str] = (),
                   rationale: str = "") -> dict[str, Any]:
        new_intent = TaskIntent(
            goal=goal,
            constraints=tuple(constraints),
            scope=tuple(scope),
            success_criteria=tuple(success_criteria))
        patch = GoalPatch(
            patch_id=f"gp-{int(time.time()*1000)}",
            new_intent=new_intent,
            rationale=rationale)
        result = self._kernel.apply_goal_patch(patch)
        return {"ok": True, "action": "goal_patch",
                "result": _jsonable(result)}

    # ── checkpoint / rollback ──────────────────────────────────────────────

    def checkpoint(self, label: str) -> dict[str, Any]:
        import uuid
        ckpt_id = uuid.uuid4().hex[:8]
        rec = self._kernel.commit_checkpoint(ckpt_id, label)
        return {"ok": True, "action": "checkpoint",
                "checkpoint_id": rec.checkpoint_id,
                "label": rec.label}

    def rollback(self, target_checkpoint_id: str,
                 rationale: str = "") -> dict[str, Any]:
        """Rollback goes through the compensation path: the kernel builds
        a CompensationPatch referencing the checkpoint; the ROUTE then
        hands the returned plan (under ``"plan"``) to the session driver
        for execution. With no driver registered the plan honestly stays
        pending (§8: never a fake success).

        ``disposition`` starts as "pending"; the route replaces it with
        the runtime's verdict ("complete"/"partial"/"failed") when an
        executor exists. The ``"plan"`` key carries the typed
        CompensationPlan object — internal to the server, popped before
        JSON serialisation."""
        from taskvm.domain.patch import CompensationPatch
        patch = CompensationPatch(
            patch_id=f"rb-{int(time.time()*1000)}",
            target_checkpoint_id=target_checkpoint_id,
            rationale=rationale)
        plan = self._kernel.request_compensation(patch)
        return {"ok": True, "action": "rollback",
                "plan_id": plan.plan_id,
                "entries": len(plan.entries),
                "uncompensatable": len(plan.uncompensatable),
                "disposition": "pending",
                "plan": plan}

    # ── conflict resolution ──────────────────────────────────────────────

    def resolve_conflict(self, conflict_id: str, resolution: str,
                         detail: str = "") -> dict[str, Any]:
        self._kernel.resolve_conflict(
            resolution, correlation_id=conflict_id)
        return {"ok": True, "action": "resolve_conflict",
                "conflict_id": conflict_id}


class GoalRecomposer:
    """High-level convenience wrapper for the goal-patch path.
    The composition root may inject a different implementation that
    routes through Agent C's architect instead of the kernel directly."""

    def __init__(self, port: KernelGovernancePort) -> None:
        self._port = port

    def recompose(self, *, goal: str, constraints: Iterable[str] = (),
                  scope: Iterable[str] = (),
                  success_criteria: Iterable[str] = (),
                  rationale: str = "") -> dict[str, Any]:
        return self._port.goal_patch(
            goal=goal, constraints=constraints, scope=scope,
            success_criteria=success_criteria, rationale=rationale)
