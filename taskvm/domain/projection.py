"""Projection — what the human sees (L5's data source, kernel-owned truth).

Schema and data are deliberately separated (master handoff §3.1 +
§Definition-of-Done): ordinary value/progress changes only bump the DATA
revision and must never force a re-composition of the UI structure
(schema revision). The kernel enforces that the two revision counters are
independent (invariant 2).

A projection is substrate-neutral: components describe semantic UI
structure (card / field / list / ...), never HTML, never a platform
widget class.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from taskvm.domain.errors import ValidationError


@dataclass(frozen=True)
class ProjectionComponent:
    """One stable node of the projection tree.

    ``binding_key`` links the component to a task variable by its
    semantic key (None for pure layout containers). ``props`` carries
    component-type-specific, substrate-neutral options.
    """

    component_id: str
    component_type: str  # "card" | "field" | "list" | "progress" | ... (semantic, not HTML)
    label: str = ""
    binding_key: str | None = None
    children: tuple[str, ...] = ()
    editable: bool = False
    props: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.component_id:
            raise ValidationError("ProjectionComponent.component_id must be non-empty")
        if not self.component_type:
            raise ValidationError("ProjectionComponent.component_type must be non-empty")
        object.__setattr__(self, "children", tuple(self.children))


@dataclass(frozen=True)
class ProjectionSchema:
    """The stable component tree. Changes only on (re)composition by the
    Task Architect — never on a value update."""

    root_id: str
    components: tuple[ProjectionComponent, ...] = ()
    revision: int = 0  # kernel-assigned, monotonic per session

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", tuple(self.components))
        ids = [c.component_id for c in self.components]
        if len(set(ids)) != len(ids):
            raise ValidationError("duplicate component_id in ProjectionSchema")
        if self.components and self.root_id not in set(ids):
            raise ValidationError(
                f"ProjectionSchema.root_id {self.root_id!r} not among components")
        known = set(ids)
        for c in self.components:
            missing = [ch for ch in c.children if ch not in known]
            if missing:
                raise ValidationError(
                    f"component {c.component_id!r} references unknown children {missing}")


@dataclass(frozen=True)
class ProjectionData:
    """The volatile part: current display values, node statuses, progress.

    ``values`` maps a task variable's semantic key to its display value;
    ``node_status`` maps a workflow node id to its business-visible status
    string. Both are replaced wholesale per update (small by design).
    """

    values: dict[str, Any] = field(default_factory=dict)
    node_status: dict[str, str] = field(default_factory=dict)
    progress: float = 0.0
    revision: int = 0  # kernel-assigned, monotonic per session

    def __post_init__(self) -> None:
        if not 0.0 <= self.progress <= 1.0:
            raise ValidationError("ProjectionData.progress must be in [0, 1]")


@dataclass(frozen=True)
class ProjectionRevision:
    """The pair of independent counters a client uses for cache/delta
    logic (e.g. SSE reconnection: refetch schema only if schema_revision
    advanced)."""

    schema_revision: int
    data_revision: int
