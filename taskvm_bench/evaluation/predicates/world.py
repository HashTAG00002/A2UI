"""taskvm_bench.evaluation.predicates.world — the always-on base group.

Three conjunctive predicate families over the frozen ``TaskSpec`` (the
same semantics the builtin-world ``Oracle`` freezes, restated over the
EvidenceBundle so the grader is substrate-independent):

1. **terminal state** — every ``(surface, key, value)`` in ``success``
   holds in the final oracle state;
2. **non-interference** — every protected ``(surface, key)`` is
   unchanged between the seed state and the final state;
3. **witness** — every witness triple APPEARED on the oracle timeline at
   some point (proves the work happened even when the final state moved
   on — the no-op loophole stays closed for rollback tasks whose final
   state equals the seed).
"""
from __future__ import annotations

from taskvm_bench.benchmark.schema import TaskSpec
from taskvm_bench.evaluation.evidence import EvidenceBundle, _norm_state
from taskvm_bench.evaluation.predicates import CheckResult

__all__ = ["checks", "missing_writes", "interference_violations",
           "missing_witness"]


def _get(state: dict, surface: str, key: str):
    return (state.get(surface) or {}).get(key)


def missing_writes(spec: TaskSpec, final: dict) -> list[tuple[str, str, str]]:
    """Required ``(surface, key, value)`` triples not holding in ``final``."""
    out = []
    for surf, kv in spec.success.items():
        for key, val in kv.items():
            if _get(final, surf, key) != val:
                out.append((surf, key, val))
    return out


def interference_violations(spec: TaskSpec, seed: dict,
                            final: dict) -> list[tuple[str, str, str, str]]:
    """Protected ``(surface, key)`` pairs whose value changed seed→final."""
    out = []
    for surf, key in spec.protected:
        a, b = _get(seed, surf, key), _get(final, surf, key)
        if a != b:
            out.append((surf, key, str(a), str(b)))
    return out


def missing_witness(spec: TaskSpec,
                    bundle: EvidenceBundle) -> list[tuple[str, str, str]]:
    """Witness triples that never appeared on the oracle timeline.

    The timeline is the seed state, every intervention's after-state and
    the final state — the honest observable sweep. A witness value that
    shows up NOWHERE means the system never performed the work it is
    being credited for (e.g. a rollback task that never wrote the
    forward values at all)."""
    want = list(spec.witness)
    if not want:
        return []
    for _label, state in bundle.oracle_timeline():
        want = [t for t in want
                if _get(state, t[0], t[1]) != t[2]]
        if not want:
            return []
    return want


def checks(spec: TaskSpec, bundle: EvidenceBundle) -> list[CheckResult]:
    final = _norm_state(bundle.oracle_final)
    seed = _norm_state(bundle.oracle_seed)
    out: list[CheckResult] = []

    miss = missing_writes(spec, final)
    out.append(CheckResult(
        "WORLD_REQUIRED_WRITE_MISSING", not miss,
        f"{len(miss)} required write(s) not in place"
        if miss else "all required writes hold at trial end"))

    inter = interference_violations(spec, seed, final)
    out.append(CheckResult(
        "WORLD_PROTECTED_CHANGED", not inter,
        f"{len(inter)} protected field(s) modified"
        if inter else "all protected fields unchanged"))

    wit = missing_witness(spec, bundle)
    out.append(CheckResult(
        "WORLD_WITNESS_MISSING", not wit,
        f"{len(wit)} witness value(s) never appeared on the timeline"
        if wit else "every witness value appeared on the timeline"))
    return out
