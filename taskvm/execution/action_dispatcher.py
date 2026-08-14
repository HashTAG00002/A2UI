"""Action dispatcher — apply a patch's PatchOps to the real apps via app-API.

The WRITE path (load-bearing: write-path-is-API in W1). Calls each app's
``StateAdapter.mutate`` with the operator + value → real state change. The
verifier then reads the real post-state via ``read_canonical``.

**Negative-control (honesty safeguard)**: ``broken=True`` makes the dispatcher
a no-op (or apply to a wrong target) — the run MUST then score ≤0.3. This is
the defense against verifier false-positives: if a broken dispatcher still
scores high, the verifier is broken. ``broken="wrong_target"`` applies each op
to a *different* entity (so non-interference should fail too — a stronger
negative control than a pure no-op, which trivially passes non-interference).

W1 keeps no atomic cross-app transaction (handoff §14-6: no atomic txn across
independent real systems) — partial failure is honest partial credit via the
AOHP-weighted verifier score, never hidden.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from taskvm.execution.patch_compiler import PatchOp
from taskvm.execution.rollback import RollbackLog, record_from_response
# Agent B (substrate isolation): the dispatch surface is the execution
# layer's GUI-only task drivers (mutate via REAL gestures). The deleted
# StateAdapter's API write path no longer exists anywhere in the runtime.
from taskvm.execution.gui_driver import GUITaskAdapter, MobileGymTaskAdapter

TaskAdapter = GUITaskAdapter | MobileGymTaskAdapter

logger = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    """Per-op outcome. ``applied`` True iff the app-API call succeeded."""
    op: PatchOp
    applied: bool
    response: dict | None = None
    error: str | None = None


@dataclass
class DispatchReport:
    ops: list[DispatchResult] = field(default_factory=list)

    @property
    def n_applied(self) -> int:
        return sum(1 for r in self.ops if r.applied)

    @property
    def all_applied(self) -> bool:
        return bool(self.ops) and all(r.applied for r in self.ops)

    def to_dict(self) -> dict:
        return {"n_ops": len(self.ops), "n_applied": self.n_applied,
                "all_applied": self.all_applied,
                "ops": [{"app": r.op.app, "entity_id": r.op.entity_id,
                         "operator": r.op.operator, "value": r.op.value,
                         "applied": r.applied, "error": r.error}
                        for r in self.ops]}


def dispatch(ops: list[PatchOp], adapters: dict[str, TaskAdapter], sid: str,
             *, broken: str | None = None,
             rollback_log: RollbackLog | None = None,
             saga_id: str | None = None,
             oracle: dict | None = None) -> DispatchReport:
    """Apply each PatchOp to its app via the adapter's ``mutate`` (GUI-only;
     there is no API executor anymore — Agent B deleted it).

    ``oracle`` (neg-control ONLY): an optional ``{app: EvaluationEnvironment}``
    dict used exclusively by the ``broken="wrong_target"`` kill-test branch
    to enumerate alternative entity ids. It is evaluation-plane power,
    deliberately explicit at the call site, and NEVER consulted on the
    normal write path.

    ``broken``:
      - None: normal dispatch (real state change).
      - "noop": apply nothing (pure no-op; non-interference trivially passes,
        changed-happened fails → verifier should score ≤0.3).
      - "wrong_target": apply each op to a *different* entity in the same app
        (changed-happened fails AND non-interference likely fails — a stronger
        negative control that checks the verifier catches wrong-target writes).

    ``rollback_log``: optional. When given AND ``broken is None``, each
    successful normal-path write is recorded as a CompensationRecord (so
    ``undo_last`` / ``undo_saga`` can later revert it). Recording is SKIPPED on
    the noop / wrong_target neg-control branches — a neg-control run must
    produce no compensation records (else undo would "restore" a value that was
    never the user's edit, corrupting the ≤0.3 honesty bound).

    ``saga_id`` (W3): optional. When given AND ``broken is None``, all
    compensation records from THIS dispatch are tagged with ``saga_id`` so
    ``undo_saga(saga_id)`` can revert the whole user action cross-app. If
    omitted, the log auto-allocates a saga_id for any records this dispatch
    creates (so every record is saga-tagged for W3 even when the caller doesn't
    pass one — ``undo_last`` still works on untagged-era W2 logs).
    """
    report = DispatchReport()
    # allocate a saga_id up-front for normal-path records (one user action → one saga)
    active_saga = saga_id
    if rollback_log is not None and active_saga is None and broken is None:
        active_saga = rollback_log.new_saga_id()
    for op in ops:
        ad = adapters.get(op.app)
        if ad is None:
            report.ops.append(DispatchResult(op, applied=False,
                                             error=f"no adapter for app {op.app}"))
            continue
        if broken == "noop":
            report.ops.append(DispatchResult(op, applied=False,
                                             error="neg-control: noop"))
            logger.info(f"[dispatch] neg-control noop: skipped {op.app}.{op.entity_id}")
            continue
        if broken == "wrong_target":
            # pick a different entity in the same app to mutate instead.
            # Requires the explicit evaluation-plane oracle (Agent B:
            # runtime adapters no longer expose canonical reads).
            if oracle is None or oracle.get(op.app) is None:
                report.ops.append(DispatchResult(
                    op, applied=False,
                    error=("neg-control wrong_target requires the explicit "
                           "evaluation oracle (dispatch(..., oracle=...))")))
                continue
            canon = oracle[op.app].oracle_state(sid)
            ids = [i for i in canon["entities"] if i != op.entity_id]
            if not ids:
                report.ops.append(DispatchResult(op, applied=False,
                                                 error="neg-control: no alt target"))
                continue
            wrong = ids[0]
            logger.info(f"[dispatch] neg-control wrong_target: {op.app}.{op.entity_id} "
                        f"→ {wrong}")
            try:
                resp = ad.mutate(sid, wrong, op.operator, op.value)
                # NOT recorded: neg-control write, never the user's edit
                report.ops.append(DispatchResult(op, applied=True, response=resp))
            except Exception as e:
                report.ops.append(DispatchResult(op, applied=False, error=str(e)))
            continue
        # normal
        try:
            resp = ad.mutate(sid, op.entity_id, op.operator, op.value)
            report.ops.append(DispatchResult(op, applied=True, response=resp))
            if rollback_log is not None:
                rec = record_from_response(op, resp)
                if rec is not None:
                    rec.saga_id = active_saga     # tag for undo_saga
                    rollback_log.record(rec)
        except Exception as e:
            report.ops.append(DispatchResult(op, applied=False, error=str(e)))
    logger.info(f"[dispatch] {report.n_applied}/{len(ops)} ops applied "
                f"(broken={broken}, saga={active_saga})")
    return report
