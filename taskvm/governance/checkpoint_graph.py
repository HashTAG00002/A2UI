"""``CheckpointGraph`` — the milestone transition model (E17-B, L3).

Models a task's checkpoints as an ordered sequence (the handoff §1.3 "checkpoint
transition graph" reduced to its linear core — MobileGym tasks are linear
progressions C0 → C1 → C2; the full N×N transition graph would be space O(N²)
with no benefit for the current task set). The graph answers two questions the
interpreter needs:

  - ``direction(src, dst)``: is moving src→dst an ADVANCE (forward, execute
    subgoals) or a ROLLBACK (backward, undo subgoals)?
  - ``checkpoints_between(src, dst)``: the ordered checkpoints crossed.

This is the structural substrate; ``GovernanceInterpreter`` does the subgoal
generation (deterministic for edit_field, undo_saga for rollback_to, optional
LLM for the NL description).

Recon note: the existing ``WorkspaceSession.checkpoints`` is a flat list of raw
canonical_snapshot dicts with no id/description/criterion. The ``Checkpoint``
dataclass (benchmark/fixtures.py, E17-A) carries the criterion. This module
bridges them: it operates on ``Checkpoint`` objects (the task's defined
milestones), separate from the runtime ``recorded_checkpoints`` snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass

from taskvm.benchmark.fixtures import Checkpoint


class CheckpointDirection:
    ADVANCE = "advance"      # src → dst moves forward (execute subgoals)
    ROLLBACK = "rollback"    # src → dst moves backward (undo subgoals)
    NONE = "none"            # src == dst (no transition)


@dataclass
class CheckpointGraph:
    """Ordered checkpoint sequence for a task (C0=initial is implicit)."""
    checkpoints: list[Checkpoint]

    def __post_init__(self) -> None:
        # validate unique ids
        ids = [c.id for c in self.checkpoints]
        if len(set(ids)) != len(ids):
            raise ValueError(f"checkpoint ids must be unique, got {ids}")

    @classmethod
    def from_task(cls, checkpoints: list[Checkpoint]) -> "CheckpointGraph":
        return cls(checkpoints=list(checkpoints))

    @property
    def ids(self) -> list[str]:
        """All checkpoint ids in order, with implicit 'C0' (initial) prepended."""
        return ["C0"] + [c.id for c in self.checkpoints]

    def index_of(self, checkpoint_id: str) -> int:
        """Index into ``ids`` (C0=0, C1=1, ...). -1 if not found."""
        return self.ids.index(checkpoint_id) if checkpoint_id in self.ids else -1

    def direction(self, src: str, dst: str) -> str:
        """ADVANCE if dst is later than src, ROLLBACK if earlier, NONE if equal."""
        si = self.index_of(src)
        di = self.index_of(dst)
        if si < 0 or di < 0:
            raise ValueError(f"unknown checkpoint in transition {src}→{dst}")
        if di > si:
            return CheckpointDirection.ADVANCE
        if di < si:
            return CheckpointDirection.ROLLBACK
        return CheckpointDirection.NONE

    def checkpoints_between(self, src: str, dst: str) -> list[Checkpoint]:
        """The defined checkpoints crossed moving src→dst (exclusive of src,
        inclusive of dst). Empty if same or unknown."""
        si = self.index_of(src)
        di = self.index_of(dst)
        if si < 0 or di < 0 or si == di:
            return []
        lo, hi = min(si, di), max(si, di)
        # ids[0]='C0', ids[1]=checkpoints[0].id, ... so ids[k]→checkpoints[k-1]
        out: list[Checkpoint] = []
        for k in range(lo + 1, hi + 1):
            out.append(self.checkpoints[k - 1])
        # for rollback, return in reverse (LIFO undo order)
        if di < si:
            out = list(reversed(out))
        return out

    def criterion_for(self, checkpoint_id: str) -> dict | None:
        """The verification criterion for a checkpoint (None for C0 / unknown)."""
        if checkpoint_id == "C0":
            return {}  # initial state — empty criterion
        for c in self.checkpoints:
            if c.id == checkpoint_id:
                return c.criterion
        return None
