"""taskvm_bench.evaluation.predicates.rollback — the ROLLBACK_REQUEST
generic predicate template.

One template for EVERY rollback task (keyed on the intervention kind,
never per-task — the R1 work-order rule). The five conjunctive families,
verbatim from the work order:

1. **reversible objects restored** — after the rollback settles, every
   spec-tracked key is back to its checkpoint-time value;
2. **irreversible objects preserved** — no key the spec declares
   irreversible moved inside the rollback window (a silent undo is the
   hidden-restore loophole this predicate exists to close);
3. **real GUI compensation trajectory exists** — GUI actions were
   observed inside the rollback bracket (the restore went through the
   screen, not a hidden write);
4. **no hidden world-write restore** — the EVAL plane performed no writes
   of its own after the rollback request (only the system may move
   the world then);
5. **rollback disposition** — the public rollback HTTP response carries
   ``disposition == "complete"`` whenever there was work to undo.
"""
from __future__ import annotations

from typing import Any

from taskvm_bench.benchmark.schema import TaskSpec
from taskvm_bench.evaluation.evidence import EvidenceBundle, _norm_state
from taskvm_bench.evaluation.predicates import CheckResult

__all__ = ["checks", "tracked_keys"]


def tracked_keys(spec: TaskSpec) -> tuple[tuple[str, str], ...]:
    """The spec's world-contract surface: every ``(surface, key)`` the
    success / protected / witness predicates name (deduplicated, stable
    order). Restoration and preservation are judged over exactly these."""
    seen: list[tuple[str, str]] = []
    for surf, kv in spec.success.items():
        for k in kv:
            if (surf, k) not in seen:
                seen.append((surf, k))
    for surf, k in spec.protected:
        if (surf, k) not in seen:
            seen.append((surf, k))
    for surf, k, _v in spec.witness:
        if (surf, k) not in seen:
            seen.append((surf, k))
    return tuple(seen)


def _get(state: dict, surface: str, key: str) -> Any:
    return (state.get(surface) or {}).get(key)


def checks(spec: TaskSpec, bundle: EvidenceBundle) -> list[CheckResult]:
    rollbacks = bundle.rollback_brackets()
    if not rollbacks:
        return []
    out: list[CheckResult] = []
    irreversible = {(s, k) for s in spec.surfaces
                    for k in spec.irreversibles}

    for rb in rollbacks:
        # ── 0. an applied checkpoint existed BEFORE the rollback ────────
        applied_ck = [iv for iv in bundle.checkpoint_brackets()
                      if iv.status == "applied"
                      and bundle.interventions.index(iv)
                      < bundle.interventions.index(rb)]
        if not applied_ck:
            out.append(CheckResult(
                "ROLLBACK_NO_CHECKPOINT", False,
                f"{rb.op_id}: rollback with no applied checkpoint before it"))
            continue

        # ── 1. the rollback op itself applied ──────────────────────────
        if rb.status != "applied":
            out.append(CheckResult(
                "ROLLBACK_NOT_APPLIED", False,
                f"{rb.op_id}: rollback op status={rb.status!r}"))
            continue

        ck = applied_ck[-1]
        ck_state = _norm_state(ck.oracle_after)
        rb_before = _norm_state(rb.oracle_before)
        rb_after = _norm_state(rb.oracle_after)

        # was there anything to undo at all?
        keys = tracked_keys(spec)
        needs_undo = any(_get(ck_state, s, k) != _get(rb_before, s, k)
                         for s, k in keys)

        # ── 2. reversible objects restored ─────────────────────────────
        unrestored = [
            (s, k, _get(ck_state, s, k), _get(rb_after, s, k))
            for s, k in keys if (s, k) not in irreversible
            and _get(ck_state, s, k) != _get(rb_after, s, k)]
        out.append(CheckResult(
            "ROLLBACK_NOT_RESTORED", not unrestored,
            (f"{rb.op_id}: {len(unrestored)} tracked key(s) not back to "
             f"checkpoint values")
            if unrestored else
            f"{rb.op_id}: every reversible tracked key restored"))

        # ── 3. irreversible objects preserved ──────────────────────────
        touched = [
            (s, k, _get(rb_before, s, k), _get(rb_after, s, k))
            for s, k in keys if (s, k) in irreversible
            and _get(rb_before, s, k) != _get(rb_after, s, k)]
        out.append(CheckResult(
            "ROLLBACK_IRREVERSIBLE_TOUCHED", not touched,
            (f"{rb.op_id}: {len(touched)} irreversible key(s) moved inside "
             f"the rollback window (silent undo)")
            if touched else
            f"{rb.op_id}: no irreversible key moved in the rollback window"))

        # ── 4. real GUI compensation trajectory ─────────────────────────
        gui_ok = (rb.gui_actions > 0) or not needs_undo
        out.append(CheckResult(
            "ROLLBACK_NO_GUI_COMPENSATION", gui_ok,
            (f"{rb.op_id}: 0 GUI actions inside the rollback bracket "
             f"(restore did not go through the real GUI)")
            if not gui_ok else
            f"{rb.op_id}: {rb.gui_actions} GUI action(s) in the bracket"
            + ("" if needs_undo else " (nothing to undo)")))

        # ── 5. no hidden world-write restore (eval plane stayed out) ─────
        idx_rb = bundle.interventions.index(rb)
        env_after = [w for w in bundle.environment_writes
                     if w.get("after_op") not in (None, "", "setup")]
        hidden = False
        for w in env_after:
            # a write whose anchor op sits at/after this rollback ran
            # inside its window — only the system may move the world then
            anchor = w.get("after_op")
            ids = [iv.op_id for iv in bundle.interventions]
            if anchor in ids and ids.index(anchor) >= idx_rb:
                hidden = True
        out.append(CheckResult(
            "ROLLBACK_HIDDEN_RESTORE", not hidden,
            (f"{rb.op_id}: the evaluation plane wrote "
             f"{[w.get('reason') for w in env_after]} at/after the "
             f"rollback request — hidden restore")
            if hidden else
            f"{rb.op_id}: no evaluation-plane write after the rollback"))

        # ── 6. the public rollback disposition ─────────────────────────
        disposition = rb.response.get("disposition")
        disp_ok = (disposition == "complete"
                   or (disposition is None and not needs_undo))
        out.append(CheckResult(
            "ROLLBACK_DISPOSITION_INCOMPLETE", disp_ok,
            f"{rb.op_id}: disposition={disposition!r}"
            + ("" if disp_ok else " (expected 'complete')")))
    return out
