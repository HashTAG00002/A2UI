"""context — TaskSurfaceContext: the PUBLIC semantic context the GenUI
decoder is allowed to see.

No-leak contract (workplan §4 `context.py`, repo contract §3 GUI-only):

- input is a plain JSON snapshot (the projection layer's public view
  models — ``snapshot_view`` shape), never a Kernel object;
- hidden ground truth, verifier GT, database ids, app-internal primary
  keys, raw file paths and internal counters (epoch / event ids / node
  ids / checkpoint ids) NEVER reach the model-facing payload;
- workflow/checkpoint/conflict entries are reduced to what a real user
  can read on screen: labels, kinds, statuses, counts.

The builder is a pure function of its input dict — same snapshot in,
same context out (test-pinned). Semantic keys are kept: they are the
cross-layer identity a binding path and an action context legitimately
address (they are also the strings the renderer shows next to values).
"""
from __future__ import annotations

from typing import Any, Mapping

from taskvm.genui.protocol import ALLOWED_SURFACE_ACTIONS

_STATUS_FALLBACK = "unknown"


def _scalar(value: Any) -> Any:
    """JSON-safe scalar passthrough (defensive: views are already JSON-safe,
    but a stray object must degrade to a visible string, never leak repr)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_scalar(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _scalar(v) for k, v in value.items()}
    return str(value)


class TaskSurfaceContextBuilder:
    """snapshot dict → TaskSurfaceContext (pure, no taskvm imports)."""

    def build(self, snapshot: Mapping[str, Any],
              *, source_labels: Mapping[str, str] | None = None
              ) -> "TaskSurfaceContext":
        """``snapshot`` follows the projection layer's public view shapes
        (governance / variables / workflow / checkpoints / conflicts).
        ``source_labels`` optionally maps semantic_key → user-visible
        surface label ("在哪个应用里看到的") — supplied by the composition
        root from public surface cards, never from app-internal ids.
        """
        governance = snapshot.get("governance") or {}
        variables_in = snapshot.get("variables") or []
        # sorted by semantic_key — the public view's ordering contract
        # (variables_view), so the context is deterministic regardless of
        # the incoming dict order
        variables_in = sorted(variables_in,
                              key=lambda v: str(v.get("key", "")))
        workflow = snapshot.get("workflow") or {}
        checkpoints = snapshot.get("checkpoints") or []
        conflicts = snapshot.get("conflicts") or []
        labels = source_labels or {}

        variables = [
            SurfaceVariable(
                semantic_key=str(v.get("key", "")),
                display_label=str(v.get("label") or v.get("key", "")),
                value_type=_scalar(v.get("value_type", "string")),
                observed=_scalar(v.get("observed")),
                desired=_scalar(v.get("desired")),
                mutability=str(v.get("mutability", "editable")),
                confidence=float(v.get("confidence", 1.0)),
                visible_source_label=labels.get(str(v.get("key", ""))) or None,
            )
            for v in variables_in
        ]

        nodes = [
            WorkflowNodeView(
                label=str(n.get("label") or n.get("node_id", "")),
                kind=str(n.get("kind_label") or n.get("kind", "")),
                status=str(n.get("status_label") or n.get("status", "")),
                depth=int(n.get("depth", 0)),
                is_checkpoint=bool(n.get("is_checkpoint", False)),
            )
            for n in (workflow.get("nodes") or [])
        ]

        checkpoint_views = [
            CheckpointView(
                label=str(c.get("label") or "checkpoint"),
                committed_nodes=int(c.get("committed_nodes", 0)),
            )
            for c in checkpoints
        ]

        conflict_views = [
            ConflictView(
                description=str(cf.get("description", "")),
                semantic_keys=[str(k) for k in (cf.get("semantic_keys") or [])],
            )
            for cf in conflicts
        ]

        return TaskSurfaceContext(
            goal=str(governance.get("goal", "")),
            task_status=str(governance.get("autonomy") or _STATUS_FALLBACK),
            variables=variables,
            workflow=WorkflowView(
                has_plan=bool(workflow.get("has_plan", False)),
                nodes=nodes,
            ),
            checkpoints=checkpoint_views,
            conflicts=conflict_views,
            allowed_surface_actions=sorted(ALLOWED_SURFACE_ACTIONS),
        )


# ── the context value objects (all screen-visible fields only) ────────────

class SurfaceVariable:
    """One task variable as the model may see it. ``semantic_key`` is the
    binding identity (user-visible next to values); no internal ids."""

    __slots__ = ("semantic_key", "display_label", "value_type", "observed",
                 "desired", "mutability", "confidence", "visible_source_label")

    def __init__(self, semantic_key: str, display_label: str,
                 value_type: Any, observed: Any, desired: Any,
                 mutability: str, confidence: float,
                 visible_source_label: str | None) -> None:
        if not semantic_key:
            raise ValueError("SurfaceVariable.semantic_key must be non-empty")
        if mutability not in ("editable", "readonly", "locked"):
            raise ValueError(f"unknown mutability {mutability!r}")
        self.semantic_key = semantic_key
        self.display_label = display_label
        self.value_type = value_type
        self.observed = observed
        self.desired = desired
        self.mutability = mutability
        self.confidence = confidence
        self.visible_source_label = visible_source_label

    @property
    def editable(self) -> bool:
        return self.mutability == "editable"

    def to_payload(self) -> dict[str, Any]:
        out = {
            "semantic_key": self.semantic_key,
            "display_label": self.display_label,
            "value_type": self.value_type,
            "observed": self.observed,
            "desired": self.desired,
            "mutability": self.mutability,
            "confidence": self.confidence,
        }
        if self.visible_source_label is not None:
            out["visible_source_label"] = self.visible_source_label
        return out


class WorkflowNodeView:
    __slots__ = ("label", "kind", "status", "depth", "is_checkpoint")

    def __init__(self, label: str, kind: str, status: str, depth: int,
                 is_checkpoint: bool) -> None:
        self.label = label
        self.kind = kind
        self.status = status
        self.depth = depth
        self.is_checkpoint = is_checkpoint

    def to_payload(self) -> dict[str, Any]:
        return {"label": self.label, "kind": self.kind, "status": self.status,
                "is_checkpoint": self.is_checkpoint}


class WorkflowView:
    __slots__ = ("has_plan", "nodes")

    def __init__(self, has_plan: bool, nodes: list[WorkflowNodeView]) -> None:
        self.has_plan = has_plan
        self.nodes = nodes

    def to_payload(self) -> dict[str, Any]:
        return {"has_plan": self.has_plan,
                "nodes": [n.to_payload() for n in self.nodes]}


class CheckpointView:
    """Checkpoint minus its id: rollback affordances live in the FIXED
    governance shell, so the dynamic surface never needs the id."""

    __slots__ = ("label", "committed_nodes")

    def __init__(self, label: str, committed_nodes: int) -> None:
        self.label = label
        self.committed_nodes = committed_nodes

    def to_payload(self) -> dict[str, Any]:
        return {"label": self.label, "committed_nodes": self.committed_nodes}


class ConflictView:
    __slots__ = ("description", "semantic_keys")

    def __init__(self, description: str, semantic_keys: list[str]) -> None:
        self.description = description
        self.semantic_keys = list(semantic_keys)

    def to_payload(self) -> dict[str, Any]:
        return {"description": self.description,
                "semantic_keys": list(self.semantic_keys)}


class TaskSurfaceContext:
    """What the GenUI decoder model receives. Everything here is allowed
    to appear on a rendered screen (GUI-only rule, repo contract §3)."""

    __slots__ = ("goal", "task_status", "variables", "workflow",
                 "checkpoints", "conflicts", "allowed_surface_actions")

    def __init__(self, goal: str, task_status: str,
                 variables: list[SurfaceVariable], workflow: WorkflowView,
                 checkpoints: list[CheckpointView],
                 conflicts: list[ConflictView],
                 allowed_surface_actions: list[str]) -> None:
        self.goal = goal
        self.task_status = task_status
        self.variables = variables
        self.workflow = workflow
        self.checkpoints = checkpoints
        self.conflicts = conflicts
        self.allowed_surface_actions = list(allowed_surface_actions)

    def variable(self, semantic_key: str) -> SurfaceVariable | None:
        for v in self.variables:
            if v.semantic_key == semantic_key:
                return v
        return None

    def editable_keys(self) -> list[str]:
        return [v.semantic_key for v in self.variables if v.editable]

    def to_payload(self) -> dict[str, Any]:
        """The exact JSON handed to the decoder model."""
        return {
            "goal": self.goal,
            "task_status": self.task_status,
            "variables": [v.to_payload() for v in self.variables],
            "workflow": self.workflow.to_payload(),
            "checkpoints": [c.to_payload() for c in self.checkpoints],
            "conflicts": [c.to_payload() for c in self.conflicts],
            "allowed_surface_actions": list(self.allowed_surface_actions),
        }
