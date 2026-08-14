"""Rollback — compensation/saga for real-app state reverts.

W2 scope (handoff §6 item 3 + §7 item 17): a transactional log that records
"what writes a patch performed" and can undo a **single-app single-step**
operation via **compensation** — re-dispatching the inverse operator through the
app's own write API (``StateAdapter.mutate``), so the real app state is restored.
This is VM reversibility (governance: the user can roll back), NOT an internal
model cheap-rollback.

W3 scope (handoff §6 item 1): extends to a **cross-app saga** — undo ONE user
action that touched MULTIPLE apps, in dependency-aware order, with honest
partial-failure reporting (no hidden failures, no atomic cross-app transaction —
handoff §7 item 16 + §11 limit 6: independent real systems share no txn
protocol; partial failure is honest partial credit). ``undo_saga`` reverts every
record belonging to one user action across all apps; ``verifier/rollback_verify``
then checks each touched app is byte-restored + untouched apps unaffected.

**No-leak invariant (load-bearing)**: the ``before`` value comes from the app's
own mutate response (``old`` / ``old_date`` — visible app state the app already
returned). It is NEVER read from ``benchmark/fixtures.user_edit.old`` or
``expected_diff`` (that is verifier-only GT). Rollback works without any fixture
import.

**Negative-control invariant**: recording is hooked ONLY at the normal-path
dispatch site (``action_dispatcher.dispatch`` line ~102), NEVER on the
``broken="noop"`` / ``broken="wrong_target"`` branches. A neg-control run must
not produce compensation records (else a later ``undo`` would "restore" a value
that was never the user's edit, corrupting the ≤0.3 honesty bound).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from taskvm.execution.patch_compiler import PatchOp
# Agent B (substrate isolation): compensation re-dispatches through the
# execution layer's GUI-only task drivers (mutate = real gestures). The
# deleted StateAdapter had an API write path; these drivers do not.
from taskvm.execution.gui_driver import GUITaskAdapter, MobileGymTaskAdapter

TaskAdapter = GUITaskAdapter | MobileGymTaskAdapter

logger = logging.getLogger(__name__)


@dataclass
class CompensationRecord:
    """One undoable write: the inverse of one dispatched PatchOp.

    ``before`` / ``after`` are the visible app-state values of ``field`` (taken
    from the app's mutate response, never GT). The inverse op is the SAME
    operator with ``value=before`` — every TaskVM operator is a field-setter
    (move_event / set_deadline / move_file / ...), so the inverse is always
    "set the field back to its prior value."

    ``saga_id`` (W3): groups records from ONE user action (one ``dispatch``
    call). ``undo_saga(saga_id)`` reverts them together. None on W2-style
    records (``undo_last`` doesn't use it).
    """
    app: str
    entity_id: str
    field: str
    operator: str
    before: Any
    after: Any
    saga_id: str | None = None

    @property
    def undo_value(self) -> Any:
        return self.before

    def to_dict(self) -> dict:
        return {"app": self.app, "entity_id": self.entity_id, "field": self.field,
                "operator": self.operator, "before": self.before, "after": self.after,
                "saga_id": self.saga_id}


@dataclass
class SagaStepResult:
    """One app's compensation outcome within a saga undo."""
    app: str
    entity_id: str
    field: str
    before: Any
    after: Any
    reverted: bool            # True iff the compensation mutate succeeded
    error: str | None = None


@dataclass
class SagaResult:
    """The outcome of undoing one cross-app saga. Honest: ``n_reverted`` may be
    < ``n_targets`` (partial failure) — reported, never hidden (handoff §11-6)."""
    saga_id: str
    steps: list[SagaStepResult] = field(default_factory=list)
    n_targets: int = 0
    n_reverted: int = 0
    fully_reverted: bool = False
    partial_failure: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"saga_id": self.saga_id, "n_targets": self.n_targets,
                "n_reverted": self.n_reverted, "fully_reverted": self.fully_reverted,
                "partial_failure": self.partial_failure, "errors": self.errors,
                "steps": [{"app": s.app, "entity_id": s.entity_id, "field": s.field,
                           "before": s.before, "after": s.after, "reverted": s.reverted,
                           "error": s.error} for s in self.steps]}


@dataclass
class RollbackLog:
    """Append-only log of compensation records (one per real dispatched write).

    W2: ``undo_last(app)`` pops the most recent record for ONE app and reverts it.
    W3: ``undo_saga(saga_id)`` reverts every record from ONE user action across
    all apps, in dependency-aware order, with honest partial-failure reporting.
    """
    records: list[CompensationRecord] = field(default_factory=list)
    _next_saga: int = 0        # monotonic saga counter (Date.now banned in this env)

    def record(self, rec: CompensationRecord) -> None:
        self.records.append(rec)

    def for_app(self, app: str) -> list[CompensationRecord]:
        return [r for r in self.records if r.app == app]

    def new_saga_id(self) -> str:
        """Allocate a fresh saga_id for a forthcoming dispatch (one user action
        → one saga_id shared by all its compensation records)."""
        self._next_saga += 1
        return f"saga_{self._next_saga}"

    def tag_pending_saga(self, saga_id: str) -> None:
        """Stamp the most-recently-appended untagged records with ``saga_id``.
        Called by ``action_dispatcher.dispatch`` AFTER a normal-path dispatch so
        every write from that one user action shares a saga_id. (We stamp
        post-hoc rather than passing saga_id through dispatch to keep the W2
        ``dispatch`` signature unchanged for regression.)"""
        for rec in reversed(self.records):
            if rec.saga_id is not None:
                break     # already-tagged older record → stop
            rec.saga_id = saga_id

    def undo_last(self, app: str, sid: str,
                  adapters: dict[str, StateAdapter]) -> dict | None:
        """Revert the most recent recorded write for ``app`` via the app's own
        write API (compensation). Returns the app's mutate response, or None if
        there is nothing to undo for ``app``.

        Single-app single-step: reverts exactly one op (the latest for ``app``).
        Does NOT cascade to other apps (W3 saga territory).
        """
        # find + remove the latest record for this app (LIFO within-app)
        idx = None
        for i in range(len(self.records) - 1, -1, -1):
            if self.records[i].app == app:
                idx = i
                break
        if idx is None:
            logger.info(f"[rollback] no record to undo for app={app}")
            return None
        rec = self.records.pop(idx)
        ad = adapters.get(app)
        if ad is None:
            logger.error(f"[rollback] no adapter for app={app}; cannot compensate")
            self.records.insert(idx, rec)   # put it back; undo failed
            return None
        logger.info(f"[rollback] undo {app}.{rec.entity_id}.{rec.field}: "
                    f"{rec.after!r} → {rec.before!r} (operator={rec.operator})")
        resp = ad.mutate(sid, rec.entity_id, rec.operator, rec.undo_value)
        return resp

    def undo_saga(self, saga_id: str, sid: str,
                  adapters: dict[str, StateAdapter]) -> SagaResult:
        """Revert ONE cross-app saga: every record tagged ``saga_id``, in
        **reverse dispatch order** (LIFO globally — undoes dependent writes
        before the write they depended on, e.g. a synced deadline before the
        release date that drove it). Honest partial-failure: if a compensation
        mutate fails, that step is marked ``reverted=False`` and recorded in
        ``errors``; the saga continues to attempt the rest (best-effort), and
        ``partial_failure`` is set. The caller's verifier
        (``rollback_verify.check_rollback_fidelity``) reads the real post-undo
        state to confirm fidelity independently.

        No atomic cross-app transaction (handoff §11-6): independent real apps
        share no commit protocol, so partial failure is reported, never hidden.
        """
        saga_recs = [r for r in self.records if r.saga_id == saga_id]
        result = SagaResult(saga_id=saga_id, n_targets=len(saga_recs))
        if not saga_recs:
            result.fully_reverted = True   # vacuously
            return result
        # reverse-dispatch order: undo the last write first (LIFO across all apps)
        # so dependent writes (a deadline synced to a date) revert before the
        # driving write (the date itself). This is a global LIFO, NOT per-app.
        for rec in reversed(saga_recs):
            ad = adapters.get(rec.app)
            step = SagaStepResult(app=rec.app, entity_id=rec.entity_id,
                                  field=rec.field, before=rec.before,
                                  after=rec.after, reverted=False)
            if ad is None:
                step.error = f"no adapter for app {rec.app}"
                result.errors.append(step.error)
                result.steps.append(step)
                continue
            try:
                logger.info(f"[rollback/saga] undo {rec.app}.{rec.entity_id}."
                            f"{rec.field}: {rec.after!r} → {rec.before!r}")
                ad.mutate(sid, rec.entity_id, rec.operator, rec.undo_value)
                step.reverted = True
                # remove the reverted record from the log (it's been compensated)
                self.records.remove(rec)
            except Exception as e:
                step.error = str(e)
                result.errors.append(f"{rec.app}.{rec.entity_id}: {e}")
                logger.error(f"[rollback/saga] compensation FAILED for "
                             f"{rec.app}.{rec.entity_id}.{rec.field}: {e}")
            result.steps.append(step)
        result.n_reverted = sum(1 for s in result.steps if s.reverted)
        result.fully_reverted = (result.n_reverted == result.n_targets)
        result.partial_failure = not result.fully_reverted
        if result.partial_failure:
            logger.warning(f"[rollback/saga] {saga_id}: PARTIAL — "
                           f"{result.n_reverted}/{result.n_targets} reverted; "
                           f"errors={result.errors}")
        else:
            logger.info(f"[rollback/saga] {saga_id}: fully reverted "
                        f"({result.n_reverted}/{result.n_targets})")
        return result

    def latest_saga_id(self) -> str | None:
        """The saga_id of the most-recently-tagged record (for 'undo last
        action' governance)."""
        for rec in reversed(self.records):
            if rec.saga_id is not None:
                return rec.saga_id
        return None

    def latest_saga_id_for_app(self, app: str) -> str | None:
        """The saga_id of the most-recently-tagged record belonging to ``app``.
        Lets the per-app undo button route through ``undo_saga`` (so the caller
        gets a full ``SagaResult`` — partial_failure + per-step outcomes —
        instead of the W2 single-step ``undo_last`` dict). The saga undoes the
        WHOLE user action that touched ``app`` (cross-app, LIFO), not just the
        one app's latest write — see E9.2: the unit of governance undo is the
        saga (one user action), and the honesty-based rollback UI renders the
        saga's per-step revert/lock state from the returned SagaResult."""
        for rec in reversed(self.records):
            if rec.saga_id is not None and rec.app == app:
                return rec.saga_id
        return None

    def to_list(self) -> list[dict]:
        return [r.to_dict() for r in self.records]


def _extract_before_after(resp: dict) -> tuple[Any, Any]:
    """Pull the before/after values out of an app's mutate response. Apps use
    slightly different keys (calendar: old_date/new_date; taskboard+drive:
    old/new) — both are visible app state, never GT."""
    before = resp.get("old")
    if before is None:
        before = resp.get("old_date")
    after = resp.get("new")
    if after is None:
        after = resp.get("new_date")
    return before, after


def record_from_response(op, resp: dict) -> CompensationRecord | None:
    """Build a CompensationRecord from a PatchOp + its successful mutate
    response. Returns None if the response lacks a before-value (can't
    compensate without knowing what to revert to)."""
    if not isinstance(resp, dict):
        return None
    before, after = _extract_before_after(resp)
    if before is None:
        logger.warning(f"[rollback] no before-value in response for "
                       f"{op.app}.{op.entity_id}.{op.field}; not recording")
        return None
    return CompensationRecord(app=op.app, entity_id=op.entity_id,
                              field=op.field, operator=op.operator,
                              before=before, after=after)
