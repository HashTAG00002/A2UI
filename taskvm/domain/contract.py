"""ActionContract — the cross-layer semantic action contract (master
handoff §5: "跨层协议必须是 substrate-neutral semantic action contract").

This is the ONLY unit of work that flows from the task layer down to the
autonomy runtime. It describes WHAT must become true and how a human
would recognise the target — never HOW a specific platform performs it:

- no app-internal operation names;
- no storage keys / database identifiers;
- no platform selectors (DOM paths, coordinates, node ids) — locating is
  expressed as *visible evidence* (the label/context a user could read);
  the substrate session resolves it privately at execution time.

Serialisation of a contract into a concrete low-level instruction is a
deterministic, runtime-side concern (master handoff §6).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from taskvm.domain.errors import ValidationError
from taskvm.domain.state import SurfaceEvidence


class Reversibility(str, Enum):
    """The honest reversibility boundary (mental-model doc §3.5)."""

    REVERSIBLE = "reversible"
    PARTIALLY_REVERSIBLE = "partially_reversible"
    IRREVERSIBLE = "irreversible"


@dataclass(frozen=True)
class ActionContract:
    """One semantic action the runtime must realise on some substrate.

    ``desired_state`` maps task-variable semantic keys to their target
    values. ``completion_condition`` is a semantic predicate description
    (human-readable, verifiable from visible state). ``target_evidence``
    carries the visible locating evidence available at plan time.
    """

    contract_id: str
    semantic_goal: str
    desired_state: dict[str, Any] = field(default_factory=dict)
    completion_condition: str = ""
    target_evidence: tuple[SurfaceEvidence, ...] = ()
    reversibility: Reversibility = Reversibility.REVERSIBLE
    risk_note: str = ""

    def __post_init__(self) -> None:
        if not self.contract_id:
            raise ValidationError("ActionContract.contract_id must be non-empty")
        if not self.semantic_goal:
            raise ValidationError("ActionContract.semantic_goal must be non-empty")
        object.__setattr__(self, "target_evidence", tuple(self.target_evidence))
        if not isinstance(self.reversibility, Reversibility):
            object.__setattr__(self, "reversibility",
                               Reversibility(self.reversibility))

    @property
    def requires_confirmation(self) -> bool:
        """Irreversible work must be visibly locked + confirmed upstream
        (mental-model doc §3.5, DoD: '不可逆动作在 UI 中可见地锁定')."""
        return self.reversibility is Reversibility.IRREVERSIBLE
