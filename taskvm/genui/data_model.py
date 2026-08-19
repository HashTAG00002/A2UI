"""data_model — TaskDataModelProjector: TaskSurfaceContext → A2UI data
model, as a PURE function (workplan §7-P2 DoD: same snapshot in, same
data model out; zero model calls, zero side effects).

The data model is the DETERMINISTIC half of the surface: ordinary state
changes (CUA landed, observation folded, verifier pass) only produce a
new ``updateDataModel`` — the component tree (structure) stays untouched
and the GenUI decoder is NOT re-invoked. Only structural changes
(first compose / GoalPatch / affordance set change) trigger a new
``updateComponents``.

Shape (every field is screen-visible; binding paths address it via JSON
Pointer, e.g. ``/variables/release_date/desired``)::

    {
      "task":    {"goal": ..., "status": ...},
      "variables": {"<semantic_key>": {"label", "value_type", "observed",
                                        "desired", "mutability", "status",
                                        "confidence"}},
      "workflow": {"has_plan": bool, "nodes": [{"label", "kind",
                                                 "status", "is_checkpoint"}]},
      "checkpoints": [{"label", "committed_nodes"}],
      "conflicts":   [{"description", "semantic_keys"}]
    }
"""
from __future__ import annotations

from typing import Any, Mapping

from taskvm.genui.context import TaskSurfaceContext

#: Variable status vocabulary (business-visible, not kernel enums).
STATUS_SYNCED = "synced"        # observed == desired
STATUS_DIVERGED = "diverged"    # desired set, reality not yet confirmed
STATUS_PENDING = "pending"      # not yet observed


def variable_status(observed: Any, desired: Any) -> str:
    if observed is None and desired is None:
        return STATUS_PENDING
    if observed != desired:
        return STATUS_DIVERGED
    return STATUS_SYNCED


class TaskDataModelProjector:
    """Deterministic projector — instantiate freely, it holds no state."""

    def project(self, context: TaskSurfaceContext) -> dict[str, Any]:
        """TaskSurfaceContext → the surface's data model value (the
        ``updateDataModel.value`` for path ``/``).

        Determinism: variables are emitted in the context's own (sorted)
        order; every scalar passes through unchanged; no timestamps, no
        counters, no randomness. Two equal contexts produce deeply-equal
        data models (test-pinned).
        """
        variables: dict[str, Any] = {}
        for v in context.variables:
            entry: dict[str, Any] = {
                "label": v.display_label,
                "value_type": v.value_type,
                "observed": v.observed,
                "desired": v.desired,
                "mutability": v.mutability,
                "status": variable_status(v.observed, v.desired),
                "confidence": v.confidence,
            }
            if v.visible_source_label is not None:
                entry["visible_source_label"] = v.visible_source_label
            variables[v.semantic_key] = entry

        return {
            "task": {
                "goal": context.goal,
                "status": context.task_status,
            },
            "variables": variables,
            "workflow": context.workflow.to_payload(),
            "checkpoints": [c.to_payload() for c in context.checkpoints],
            "conflicts": [c.to_payload() for c in context.conflicts],
        }


# ── binding-path whitelist (policy layer's ground truth) ───────────────────

def binding_path_whitelist(data_model: Mapping) -> set[str]:
    """All stable JSON-Pointer paths inside ``data_model``.

    Paths that traverse a LIST are excluded (positions are unstable under
    reorder — a model must never bind ``/workflow/nodes/3/label``); dict
    branches and scalar leaves (``None`` included — a binding may target
    an empty desired plane) are whitelisted.
    """
    out: set[str] = set()
    _walk(data_model, "", out)
    return out


def _walk(node: Any, prefix: str, out: set[str]) -> None:
    if isinstance(node, dict):
        for key in node:
            _walk(node[key], f"{prefix}/{key}", out)
    elif isinstance(node, list):
        return  # positional paths intentionally not whitelisted
    else:
        out.add(prefix)
