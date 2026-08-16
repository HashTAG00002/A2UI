"""Task-state representation — the typed graph the compiler emits.

A ``TaskStateGraph`` is a set of task variables (the editable quantities the
user sees + manipulates). Each variable carries its current value (read from
the observed app state) and an ``editable`` flag. The binding (variable → real
app entities + operators) lives in ``entity_binding.py``.

This is the compiler's primary structured output (alongside the A2UI surface).
The first-class object is Task State, not trajectory (handoff §1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskVariable:
    """One task quantity the user can see / manipulate."""
    var_id: str                # "release_date"
    label: str                 # "发布日期"
    value: Any                 # current value, read from observed app state
    editable: bool = True
    kind: str = "string"       # "date" | "string" | "status" | ... (display hint)
    bindings: list = field(default_factory=list)  # list[EntityBinding] (filled by compiler)


@dataclass
class TaskStateGraph:
    """The compiled task state: the variables + the task meta."""
    task_id: str
    goal: str
    variables: list[TaskVariable] = field(default_factory=list)

    def variable(self, var_id: str) -> TaskVariable | None:
        for v in self.variables:
            if v.var_id == var_id:
                return v
        return None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "variables": [
                {"var_id": v.var_id, "label": v.label, "value": v.value,
                 "editable": v.editable, "kind": v.kind,
                 "bindings": [b.to_dict() for b in v.bindings]}
                for v in self.variables
            ],
        }
