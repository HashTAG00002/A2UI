"""taskvm_bench.evaluation.predicates.local_patch — the LOCAL_PATCH
generic predicate template.

A local patch is ONE user governance gesture re-targeting specific
semantic keys mid-run. The template (two conjunctive families):

1. the patch op itself APPLIED through the public governance route;
2. every patched key actually landed at its patched value in the
   bracket's after-state (a patch the UI accepted but the world never
   absorbed is a governance lie).
"""
from __future__ import annotations

from taskvm_bench.benchmark.schema import TaskSpec
from taskvm_bench.evaluation.evidence import EvidenceBundle, _norm_state
from taskvm_bench.evaluation.predicates import CheckResult

__all__ = ["checks"]


def checks(spec: TaskSpec, bundle: EvidenceBundle) -> list[CheckResult]:
    patches = [iv for iv in bundle.interventions
               if iv.kind == "local_patch"]
    if not patches:
        return []
    out: list[CheckResult] = []
    for p in patches:
        if p.status != "applied":
            out.append(CheckResult(
                "LOCAL_PATCH_NOT_APPLIED", False,
                f"{p.op_id}: local patch status={p.status!r}"))
            continue
        after = _norm_state(p.oracle_after)
        updates = (p.response.get("updates")
                   or p.response.get("result", {}).get("updates") or {})
        missing = []
        for key, val in (updates or {}).items():
            landed = any(
                key in kv and kv[key] == val
                for kv in after.values())
            if not landed:
                missing.append((key, val))
        out.append(CheckResult(
            "LOCAL_PATCH_KEY_MISSING", not missing,
            (f"{p.op_id}: {len(missing)} patched key(s) did not land "
             f"({missing[:4]})")
            if missing else
            f"{p.op_id}: every patched key landed at its patched value"))
    return out
