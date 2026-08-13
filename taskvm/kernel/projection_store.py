"""ProjectionStore — single source of truth for projection schema + data.

Invariant 2 lives here: schema revision and data revision are independent
counters. A value/progress update bumps ONLY the data revision, so the
projection layer can keep rendering the cached schema (no re-composition,
no model call — master handoff §3.1 / §6).
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, replace
from typing import Any

from taskvm.domain.errors import RevisionConflictError, ValidationError
from taskvm.domain.projection import (
    ProjectionData,
    ProjectionRevision,
    ProjectionSchema,
)


@dataclass(frozen=True)
class ProjectionSnapshot:
    """A consistent read of the store at one instant (defensive copies)."""

    schema: ProjectionSchema | None
    data: ProjectionData
    revision: ProjectionRevision


class ProjectionStore:
    """Holds the current projection schema + data for one session."""

    def __init__(self) -> None:
        self._schema: ProjectionSchema | None = None
        self._data = ProjectionData()
        self._schema_rev = 0
        self._data_rev = 0
        self._lock = threading.RLock()

    # ── schema (architect composition only) ──────────────────────────────
    def set_schema(self, schema: ProjectionSchema) -> ProjectionSchema:
        with self._lock:
            stamped = replace(schema, revision=self._schema_rev + 1)
            self._schema = stamped
            self._schema_rev += 1
            return copy.deepcopy(stamped)

    # ── data (ordinary value/progress updates) ───────────────────────────
    def replace_data(self, *, values: dict[str, Any],
                     node_status: dict[str, str],
                     progress: float) -> ProjectionData:
        """AUTHORITATIVE wholesale replace of the volatile projection data.
        This is the kernel's refresh path: keys that no longer exist in
        the task state or workflow must disappear here (no stale merge).
        """
        with self._lock:
            data = ProjectionData(values=dict(values),
                                  node_status=dict(node_status),
                                  progress=progress,
                                  revision=self._data_rev + 1)
            self._data = data
            self._data_rev += 1
            return copy.deepcopy(data)

    def update_data(self, *, values: dict[str, Any] | None = None,
                    node_status: dict[str, str] | None = None,
                    progress: float | None = None) -> ProjectionData:
        with self._lock:
            data = self._data
            if values is not None:
                merged = dict(data.values)
                merged.update(values)
                data = replace(data, values=merged)
            if node_status is not None:
                merged_status = dict(data.node_status)
                merged_status.update(node_status)
                data = replace(data, node_status=merged_status)
            if progress is not None:
                data = replace(data, progress=progress)
            data = replace(data, revision=self._data_rev + 1)
            if not 0.0 <= data.progress <= 1.0:
                raise ValidationError("progress must be in [0, 1]")
            self._data = data
            self._data_rev += 1
            return copy.deepcopy(data)

    # ── reads ────────────────────────────────────────────────────────────
    def snapshot(self) -> ProjectionSnapshot:
        with self._lock:
            return ProjectionSnapshot(
                copy.deepcopy(self._schema),
                copy.deepcopy(self._data),
                ProjectionRevision(schema_revision=self._schema_rev,
                                   data_revision=self._data_rev))

    def check_schema_revision(self, schema: ProjectionSchema) -> None:
        with self._lock:
            if schema.revision <= self._schema_rev:
                raise RevisionConflictError(
                    f"schema revision {schema.revision} not > current "
                    f"{self._schema_rev}")
