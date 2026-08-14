"""Rollback fidelity verification — independently check a saga undo restored the
real app state (W3, handoff §6 item 1 + §5 invariants 1-3).

Reads ONLY the real canonical state (``read_canonical``) before the original
dispatch, after the dispatch (the changed state), and after ``undo_saga`` —
then judges, with NO model self-evaluation (handoff §7 item 2):

  1. **Rollback Fidelity** (VM property 5, the gate): every entity that the saga
     TOUCHED is byte-identical between the pre-dispatch snapshot and the
     post-undo snapshot (the real app state was truly restored by compensation,
     not an internal model cheap-rollback — handoff §7 item 16).
  2. **Non-interference-on-rollback**: every entity NOT touched by the saga is
     unchanged between pre-dispatch and post-undo (the undo didn't clobber
     neighbors — same hard-door as W1 ``non_interference`` but applied to the
     rollback step).

No-leak: the "touched" set is derived from the RollbackLog's saga records
(visible app-state writes the app returned ``old``/``new`` for), NEVER from
``fixtures.expected_diff`` or ``user_edit.old``. The verifier compares real
snapshots only.

Score = AOHP-weighted (0.5·fidelity + 0.3·non-interference + 0.2·saga-reported)
mirroring ``round_trip_checks``. Non-interference-on-rollback is a HARD gate:
if violated (the undo clobbered an unrelated entity), score clamps to ≤0.3.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from taskvm.execution.rollback import SagaResult
from taskvm.verifier.canonical_state import (entity_record, entity_unchanged)
from taskvm.substrate import EvaluationEnvironment   # port type only (Agent B)


@dataclass
class RollbackFidelityResult:
    score: float
    fidelity: float                 # fraction of touched entities byte-restored
    non_interference_on_rollback: bool   # hard gate: untouched entities unchanged
    hard_fail: bool
    n_touched: int
    n_restored: int
    not_restored: list[dict]        # [{app, entity_id, field, pre, post_undo}]
    clobbered: list[dict]           # untouched entities that changed (hard-fail)
    info: dict


def _touched_set(saga_records) -> set[tuple[str, str]]:
    """The (app, entity_id) pairs the saga wrote to — from the RollbackLog's own
    records (visible app-state writes), never GT. Used to split 'touched' (must
    be restored) from 'untouched' (must stay unchanged)."""
    return {(r.app, r.entity_id) for r in saga_records}


def check_rollback_fidelity(
        pre_dispatch: dict, post_undo: dict,
        adapters: dict[str, EvaluationEnvironment], sid: str,
        saga_result: SagaResult) -> RollbackFidelityResult:
    """Verify the saga undo restored the real app state.

    ``pre_dispatch`` = canonical snapshot BEFORE the original dispatch (the state
    the user wants to return to). ``post_undo`` = canonical snapshot AFTER
    ``undo_saga``. ``saga_result`` = the SagaResult returned by ``undo_saga``
    (carries the touched records + per-step success).

    Re-reads ``post_undo`` fresh from the apps if the caller passed a stale dict
    — but callers should pass a fresh ``canonical_snapshot(adapters, sid)`` taken
    after the undo. We trust the caller's snapshot is fresh (the gate script
    takes it immediately after undo_saga returns).
    """
    touched = _touched_set(saga_result.steps)
    # ── fidelity: every touched entity byte-restored to pre_dispatch ─────────
    not_restored = []
    for app, eid in touched:
        pre_rec = entity_record(pre_dispatch, app, eid)
        post_rec = entity_record(post_undo, app, eid)
        if pre_rec != post_rec:
            # find the first differing field for the diagnostic
            diff_field = None
            if pre_rec is not None and post_rec is not None:
                for f in set(pre_rec) | set(post_rec):
                    if pre_rec.get(f) != post_rec.get(f):
                        diff_field = f; break
            not_restored.append({"app": app, "entity_id": eid,
                                 "field": diff_field,
                                 "pre": pre_rec, "post_undo": post_rec})
    n_touched = len(touched)
    n_restored = n_touched - len(not_restored)
    fidelity = n_restored / n_touched if n_touched else 1.0

    # ── non-interference-on-rollback: untouched entities unchanged ───────────
    clobbered = []
    all_entities = set()
    for app, snap in post_undo.items():
        for eid in ((snap or {}).get("entities") or {}):
            all_entities.add((app, eid))
    for app, eid in all_entities - touched:
        if not entity_unchanged(pre_dispatch, post_undo, app, eid):
            clobbered.append({"app": app, "entity_id": eid,
                              "pre": entity_record(pre_dispatch, app, eid),
                              "post_undo": entity_record(post_undo, app, eid)})
    non_interference_ok = len(clobbered) == 0

    # ── score: AOHP-weighted, non-interference is a hard gate ────────────────
    saga_reported_fully = 1.0 if saga_result.fully_reverted else 0.0
    score = 0.5 * fidelity + 0.3 * (1.0 if non_interference_ok else 0.0) + \
        0.2 * saga_reported_fully
    hard_fail = not non_interference_ok
    if hard_fail:
        score = min(score, 0.3)

    return RollbackFidelityResult(
        score=round(score, 4), fidelity=round(fidelity, 4),
        non_interference_on_rollback=non_interference_ok, hard_fail=hard_fail,
        n_touched=n_touched, n_restored=n_restored,
        not_restored=not_restored, clobbered=clobbered,
        info={"saga_id": saga_result.saga_id,
              "n_targets": saga_result.n_targets,
              "n_reverted_reported": saga_result.n_reverted,
              "partial_failure_reported": saga_result.partial_failure,
              "saga_errors": saga_result.errors,
              "weights": {"fidelity": 0.5, "non_interference": 0.3,
                          "saga_reported": 0.2}})


def check_non_interference_on_rollback(
        pre_dispatch: dict, post_undo: dict,
        touched: list[tuple[str, str]]) -> tuple[bool, list[dict]]:
    """Standalone non-interference-on-rollback check (usable without a SagaResult).
    ``touched`` = [(app, entity_id), ...] the undo was allowed to affect. Every
    OTHER entity must be byte-identical pre-dispatch vs post-undo. Returns
    (passed, clobbered_list)."""
    touched_set = set(touched)
    clobbered = []
    all_entities = set()
    for app, snap in post_undo.items():
        for eid in ((snap or {}).get("entities") or {}):
            all_entities.add((app, eid))
    for app, eid in all_entities - touched_set:
        if not entity_unchanged(pre_dispatch, post_undo, app, eid):
            clobbered.append({"app": app, "entity_id": eid})
    return (len(clobbered) == 0, clobbered)
