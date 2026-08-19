"""taskvm_bench.evaluation.predicates — deterministic grading predicates.

One file PER INTERVENTION TYPE (the R1 work-order rule: predicates are
templates keyed by intervention kind, never per-task evaluators — new
intervention families plug in by adding a module, not by editing the
grader). The grader (:mod:`taskvm_bench.evaluation.grader`) dispatches
here; nothing in this package imports the prototype — predicates are
pure functions of ``(TaskSpec, EvidenceBundle)``.

Modules:

* :mod:`.world`      — the base group, ALWAYS evaluated: frozen terminal
  state (required writes), field-level non-interference (protected), and
  the write witness (values that must have APPEARED on the timeline);
* :mod:`.rollback`   — the ROLLBACK_REQUEST template (reversible objects
  restored + irreversible objects preserved + real GUI compensation
  trajectory + no hidden world-write restore);
* :mod:`.local_patch` — the LOCAL_PATCH template (patched keys landed
  through governance, nothing else moved).
"""
from __future__ import annotations

from dataclasses import dataclass

from taskvm_bench.benchmark.schema import TaskSpec
from taskvm_bench.evaluation.evidence import EvidenceBundle

__all__ = [
    "CheckResult", "FAILURE_CODES", "run_predicates", "predicate_modules",
]


@dataclass(frozen=True)
class CheckResult:
    """One predicate outcome: a CLOSED failure code + pass flag + detail.

    ``code`` is always a member of :data:`FAILURE_CODES` — codes are never
    invented per call-site, so downstream aggregation stays a closed set."""

    code: str
    passed: bool
    detail: str = ""


#: The CLOSED failure-code vocabulary (the grader's whole taxonomy —
#: reported verbatim in ``ContractVerdict.failure_codes``).
#:
#: world group (always evaluated):
#: * ``WORLD_REQUIRED_WRITE_MISSING``  — the frozen success state does not
#:   hold at trial end (a required value is wrong/absent);
#: * ``WORLD_PROTECTED_CHANGED``       — a protected field differs between
#:   seed and final (non-interference violated);
#: * ``WORLD_WITNESS_MISSING``         — a witness value never appeared on
#:   the oracle timeline (the no-op loophole stayed open: the system may
#:   have reached the final state without doing the work);
#:
#: rollback group (ROLLBACK_REQUEST interventions only):
#: * ``ROLLBACK_NO_CHECKPOINT``        — rollback ran with no applied
#:   checkpoint before it;
#: * ``ROLLBACK_NOT_APPLIED``          — the rollback op itself was
#:   rejected/unsettled/errored;
#: * ``ROLLBACK_NOT_RESTORED``         — the post-rollback oracle state
#:   does not equal the checkpoint-time state for the spec's targets;
#: * ``ROLLBACK_IRREVERSIBLE_TOUCHED`` — a key the spec declares
#:   irreversible was silently reverted (a hidden undo);
#: * ``ROLLBACK_NO_GUI_COMPENSATION``  — no GUI action inside the rollback
#:   bracket (the restore did NOT go through the real GUI);
#: * ``ROLLBACK_HIDDEN_RESTORE``       — the EVAL plane performed writes
#:   after the rollback request (only the system may move the world then);
#: * ``ROLLBACK_DISPOSITION_INCOMPLETE`` — the rollback HTTP disposition
#:   is not ``complete``;
#:
#: local-patch group (LOCAL_PATCH interventions only):
#: * ``LOCAL_PATCH_NOT_APPLIED``       — the patch op was
#:   rejected/unsettled/errored;
#: * ``LOCAL_PATCH_KEY_MISSING``       — a patched key did not land at the
#:   patched value in the bracket's oracle after-state;
#:
#: governance group (any user-op bundle):
#: * ``GOVERNANCE_OP_REJECTED``        — some user op was honestly rejected;
#: * ``GOVERNANCE_OP_UNSETTLED``       — some user op never settled;
#: * ``LEDGER_INTEGRITY_BROKEN``       — a role shows more ledger rows than
#:   provider requests (double counting) or the ledger is absent;
#:
#: projection group:
#: * ``PROJECTION_MISMATCH``           — a public projection snapshot
#:   claims a value the hidden world nowhere holds;
#: * ``PROJECTION_UNAVAILABLE``        — no projection evidence was
#:   collected (an unverified dimension is NOT a passed dimension);
#:
#: progress group:
#: * ``PROGRESS_INCOMPLETE``           — not every user op in the program
#:   applied (task did not traverse its governance program).
FAILURE_CODES = (
    "WORLD_REQUIRED_WRITE_MISSING",
    "WORLD_PROTECTED_CHANGED",
    "WORLD_WITNESS_MISSING",
    "ROLLBACK_NO_CHECKPOINT",
    "ROLLBACK_NOT_APPLIED",
    "ROLLBACK_NOT_RESTORED",
    "ROLLBACK_IRREVERSIBLE_TOUCHED",
    "ROLLBACK_NO_GUI_COMPENSATION",
    "ROLLBACK_HIDDEN_RESTORE",
    "ROLLBACK_DISPOSITION_INCOMPLETE",
    "LOCAL_PATCH_NOT_APPLIED",
    "LOCAL_PATCH_KEY_MISSING",
    "GOVERNANCE_OP_REJECTED",
    "GOVERNANCE_OP_UNSETTLED",
    "LEDGER_INTEGRITY_BROKEN",
    "PROJECTION_MISMATCH",
    "PROJECTION_UNAVAILABLE",
    "PROGRESS_INCOMPLETE",
)


def predicate_modules() -> dict[str, str]:
    """Intervention kind → predicate module name (the dispatch table).
    ``world`` is the always-on base group; the rest key on the
    intervention kinds the bundle actually carries."""
    return {
        "world": "taskvm_bench.evaluation.predicates.world",
        "rollback": "taskvm_bench.evaluation.predicates.rollback",
        "rollback_request": "taskvm_bench.evaluation.predicates.rollback",
        "local_patch": "taskvm_bench.evaluation.predicates.local_patch",
    }


def run_predicates(spec: TaskSpec,
                   bundle: EvidenceBundle) -> list[CheckResult]:
    """Evaluate every predicate group the bundle calls for, in stable
    order: the world base group first, then one group per intervention
    kind present (each kind evaluated ONCE, not once per intervention)."""
    from importlib import import_module

    results: list[CheckResult] = []
    table = predicate_modules()
    # base group first (frozen contract)
    world = import_module(table["world"])
    results.extend(world.checks(spec, bundle))
    # one evaluation per DISTINCT intervention kind present
    seen: set[str] = set()
    for iv in bundle.interventions:
        key = iv.kind if iv.kind in table else iv.kind.replace(
            "_request", "")
        module_name = table.get(key)
        if module_name is None or key in seen:
            continue
        seen.add(key)
        mod = import_module(module_name)
        if hasattr(mod, "checks"):
            results.extend(mod.checks(spec, bundle))
    return results
