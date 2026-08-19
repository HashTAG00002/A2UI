"""Projection — what the human sees (L5's data source, kernel-owned truth).

Schema and data are deliberately separated (contract §3.1 +
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
        if not self.components:
            return  # empty schema (pre-composition) is allowed
        if self.root_id not in set(ids):
            raise ValidationError(
                f"ProjectionSchema.root_id {self.root_id!r} not among components")
        # ── the schema is a TREE, not an arbitrary graph ─────────────
        known = set(ids)
        parents: dict[str, str] = {}
        for c in self.components:
            for ch in c.children:
                if ch not in known:
                    raise ValidationError(
                        f"component {c.component_id!r} references unknown "
                        f"children {[ch]}")
                if ch in parents:
                    raise ValidationError(
                        f"component {ch!r} has multiple parents "
                        f"({parents[ch]!r} and {c.component_id!r})")
                parents[ch] = c.component_id
        if self.root_id in parents:
            raise ValidationError(
                f"root {self.root_id!r} must not have a parent")
        for c in self.components:
            if c.component_id != self.root_id and c.component_id not in parents:
                raise ValidationError(
                    f"component {c.component_id!r} is unreachable from root "
                    f"{self.root_id!r} (every non-root needs exactly one parent)")
        # cycle check: walking parents upward from any node must terminate
        # (single-parent above ⇒ termination implies reaching the root)
        for c in self.components:
            seen: set[str] = set()
            cur = c.component_id
            while cur in parents:
                if cur in seen:
                    raise ValidationError(
                        f"projection tree cycle involving {cur!r}")
                seen.add(cur)
                cur = parents[cur]


@dataclass(frozen=True)
class ProjectionData:
    """The volatile part: current display values, node statuses, progress.

    ``values`` maps a task variable's semantic key to a small display
    record ``{"observed": ..., "desired": ..., "diverged": bool}`` so the
    projection layer can render pending divergence honestly (a user edit
    that reality has not yet confirmed must NOT look done).
    ``node_status`` maps a workflow node id to its business-visible status
    string. Both are REPLACED wholesale by the kernel on every refresh —
    keys removed from the task state or the workflow must disappear here
    (authoritative replace, never a merge that leaves stale keys).
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
