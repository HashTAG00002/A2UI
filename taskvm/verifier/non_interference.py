"""Non-interference check — the HARD constraint.

Every entity in the fixture's ``non_interference_set`` must be byte-identical
between the pre- and post-edit snapshots. If ANY protected entity changed, the
run FAILS regardless of the overall score (a changed-happened success that
also touched an unrelated entity is a false positive — the dispatcher
over-applied). This is the load-bearing guard against verifier leniency.

The hard-gate-then-numeric-score pattern conceptually echoes SenseAct's
structural-gate reward (a hard gate that zeroes the score on a structural
failure), but the implementation is TaskVM-native (entity-record byte-compare
on canonical state, not a SenseAct submit-mode oracle).
"""
from __future__ import annotations

from dataclasses import dataclass

from taskvm.verifier.canonical_state import entity_unchanged


@dataclass
class NonInterferenceResult:
    fraction: float
    passed: bool               # True iff fraction == 1.0 (hard constraint)
    violated: list[tuple[str, str]]   # [(app, entity_id), ...] that changed
    info: dict


def check_non_interference(pre: dict, post: dict,
                           non_interference_set: list[tuple[str, str]]) -> NonInterferenceResult:
    """``non_interference_set`` = [(app, entity_id), ...] that must NOT change."""
    if not non_interference_set:
        return NonInterferenceResult(1.0, True, [], {"n_protected": 0, "violated": []})
    violated = []
    for app, eid in non_interference_set:
        if not entity_unchanged(pre, post, app, eid):
            violated.append((app, eid))
    n = len(non_interference_set)
    n_ok = n - len(violated)
    fraction = n_ok / n
    passed = (len(violated) == 0)
    return NonInterferenceResult(
        fraction=fraction, passed=passed, violated=violated,
        info={"n_protected": n, "n_violated": len(violated), "violated": violated})
