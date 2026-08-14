"""``VMStateSnapshot`` — the L1 state object ``GovernanceInterpreter`` reads (E17-B).

Recon (area 5) confirmed no ``VMStateSnapshot`` class exists in the codebase.
The closest existing aggregate is ``WorkspaceSession`` (workspace_ui/server.py),
a Flask-session dataclass holding binding + rollback_log + checkpoints +
adapters. This module lifts that aggregate into a clean VM-state value object
that lives in the governance layer (not the UI layer), so
``GovernanceInterpreter.interpret(event, vm_state)`` has a typed input.

Composition (grounded in existing types):
  - ``binding``: ``TaskBinding`` (task_state.entity_binding) — the var→entity→op map.
  - ``adapters``: ``dict[str, StateAdapter]`` — needed because ``undo_saga``
    re-dispatches the inverse op through the adapter.
  - ``rollback_log``: ``RollbackLog`` (execution.rollback) — the executed-ops
    log (CompensationRecord list). NOTE (recon): records are POPPED on undo, so
    this log reflects currently-active (not-yet-undone) ops only.
  - ``checkpoints``: the task's defined ``Checkpoint`` list (from the fixture) —
    the milestone graph the driver advances / rolls back along.
  - ``recorded_checkpoints``: runtime snapshots taken at each checkpoint
    (canonical_snapshot dicts, as WorkspaceSession.checkpoints stores them).
  - ``checkpoint_saga_map``: list of ``(checkpoint_id, saga_id)`` pairs — the
    saga that was active when each checkpoint was recorded. This is how
    ``rollback_to C_k`` deterministically resolves to "undo all sagas after
    C_k" without needing an LLM.
  - ``sid``: the session id (passed to adapter.mutate / read_canonical).
  - ``current_values``: optional projected values dict (project_readonly output)
    for the interpreter to read the live UI state.

No-leak boundary: ``current_values`` comes from ``read_canonical`` (visible app
state) — NEVER from fixtures/expected_diff. The interpreter must not receive GT
old/new values through this object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from taskvm.execution.rollback import RollbackLog
    # Agent B (substrate isolation): the adapters field carries the
    # execution layer's GUI-only task drivers (GUITaskAdapter /
    # MobileGymTaskAdapter — duck-typed on ``mutate``), NOT the deleted
    # StateAdapter. Oracle/seed powers live on a separate
    # EvaluationEnvironment the interpreter never receives here.
    from taskvm.execution.gui_driver import (GUITaskAdapter,
                                              MobileGymTaskAdapter)
    from taskvm.benchmark.fixtures import Checkpoint
    from taskvm.task_state.entity_binding import TaskBinding


@dataclass
class VMStateSnapshot:
    """The typed VM-state input to ``GovernanceInterpreter.interpret``."""
    sid: str
    binding: "TaskBinding"
    adapters: dict[str, "GUITaskAdapter | MobileGymTaskAdapter | Any"]
    rollback_log: "RollbackLog"
    checkpoints: list["Checkpoint"] = field(default_factory=list)
    recorded_checkpoints: list[dict] = field(default_factory=list)
    checkpoint_saga_map: list[tuple[str, str]] = field(default_factory=list)
    current_values: dict[str, dict[str, Any]] | None = None
    # Agent B: visible-anchor lookup injected by the composition root
    # (title translation for GG.3 instructions; never an oracle read
    # inside governance — see interpreter._canonical_for_op). Kept AFTER
    # all required fields — dataclasses forbid a default-valued field
    # before a required one (E29: this ordering bug broke the whole
    # taskvm.governance import chain).
    anchor_lookup: "Any | None" = None

    # ── helpers the interpreter uses ──────────────────────────────────────
    def sagas_after_checkpoint(self, target_checkpoint_id: str) -> list[str]:
        """Return the saga_ids that were recorded AFTER the target checkpoint,
        in reverse-dispatch (LIFO) order — the sagas ``rollback_to target``
        must undo. Deterministic: no LLM needed for WHICH sagas.

        If the target is not in the map, returns all recorded sagas (undo
        everything) — the conservative rollback.
        """
        if not self.checkpoint_saga_map:
            # no checkpoint mapping recorded — undo all active sagas (conservative)
            seen: list[str] = []
            for rec in reversed(self.rollback_log.records):
                if rec.saga_id and rec.saga_id not in seen:
                    seen.append(rec.saga_id)
            return seen
        # find the target's position in the recorded order
        ids = [cid for cid, _ in self.checkpoint_saga_map]
        if target_checkpoint_id not in ids:
            # target not recorded — undo everything (conservative)
            seen: list[str] = []
            for rec in reversed(self.rollback_log.records):
                if rec.saga_id and rec.saga_id not in seen:
                    seen.append(rec.saga_id)
            return seen
        idx = ids.index(target_checkpoint_id)
        # the saga that was active WHEN the target checkpoint was taken (None if
        # the checkpoint was taken before any write — C0 case)
        target_saga = self.checkpoint_saga_map[idx][1]
        # GG.5 fix: the map only records sagas at checkpoint moments, so sagas
        # dispatched AFTER the last checkpoint (with no later checkpoint to
        # record them) would be missed by the map-only approach. Use the
        # rollback_log records directly: include every saga dispatched AFTER the
        # target checkpoint's active saga (i.e. sagas whose first record comes
        # after the target_saga's last record, or all sagas if target_saga is
        # None). This is the honest "undo everything that happened after C_k".
        log_sagas: list[str] = []
        seen_sagas: set[str] = set()
        started = (target_saga is None)   # if no saga at C_k, ALL log sagas are after it
        for rec in self.rollback_log.records:
            sid = rec.saga_id
            if not sid:
                continue
            if not started:
                if sid == target_saga:
                    started = True   # past the target's saga; subsequent sagas are "after"
                continue
            if sid not in seen_sagas:
                seen_sagas.add(sid)
                log_sagas.append(sid)
        # LIFO order (reverse dispatch)
        return list(reversed(log_sagas))

    def to_dict(self) -> dict:
        return {
            "sid": self.sid,
            "n_checkpoints": len(self.checkpoints),
            "n_recorded_checkpoints": len(self.recorded_checkpoints),
            "checkpoint_saga_map": self.checkpoint_saga_map,
            "n_active_records": len(self.rollback_log.records),
            "active_sagas": list({r.saga_id for r in self.rollback_log.records
                                  if r.saga_id}),
        }
