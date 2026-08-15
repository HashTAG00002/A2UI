"""taskvm.verifier.visible — the RUNTIME-visible verifier (Agent E; runtime.md §6).

The single owner of verification CONTENT legality (layered ownership protocol
§1): ``VerificationResult.passed`` reflects an independent visible-world
check — ``completion_condition`` / desired_state evaluated against FRESH
post-action observation — never hidden DB / fixture / oracle. The kernel
lands the typed verdict with TIME checks only (identity / epoch / lifecycle).

This concrete class satisfies ``taskvm.runtime.ports.Verifier`` STRUCTURALLY
(Python Protocol — matching method signature, no import of taskvm.runtime
needed), so the verifier package stays a pure domain+stdlib leaf and the
runtime gate's "runtime may not import verifier" rule holds: composition /
tests inject a ``VisibleVerifier`` instance; runtime never imports it.

Default verification is deterministic and costs ZERO model calls:
- ACTION node: ``passed`` iff every ``contract.desired_state`` key's freshly
  observed value equals its target (the load-bearing ``CUA done ≠ verified``
  check — the CUA may report done while the screen still shows the old value).
- VERIFY node: ``passed`` iff the variables referenced by the node's
  ``verification`` predicate are observed at their desired values (or, when
  no variable is referenced, iff nothing is diverged).

The ``evidence_ref`` points at the captured screenshot the runtime passed in
(never an internal id). Composition may inject a model-augmented verifier,
but there is only ONE verifier (E) — the kernel never re-runs it.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from taskvm.domain.results import VerificationResult
from taskvm.domain.workflow import NodeKind, WorkflowNode

# A couple of generic English tokens that should never be treated as a
# variable semantic_key when parsed out of a free-text verification predicate.
_STOPWORDS = frozenset({"value", "the", "a", "an", "is", "are", "to", "of",
                        "and", "or", "not", "matches", "equals", "shows",
                        "displays", "reads", "set", "when", "then", "true",
                        "false", "none", "empty", "done", "complete"})


class VisibleVerifier:
    """The runtime-visible verifier (deterministic, 0 model calls).

    Satisfies the ``taskvm.runtime.ports.Verifier`` Protocol structurally.
    """

    def verify(self, *, node: WorkflowNode,
               before_observed: Mapping[str, Any],
               after_observed: Mapping[str, Any],
               desired: Mapping[str, Any],
               observation: Any,
               action_id: str | None,
               epoch: int) -> VerificationResult:
        evidence_ref = self._evidence_ref(observation)
        if node.kind is NodeKind.ACTION:
            return self._verify_action(node, after_observed, evidence_ref,
                                       action_id, epoch)
        return self._verify_control(node, after_observed, desired,
                                    evidence_ref, action_id, epoch)

    # ── ACTION: desired_state must be met in the fresh observation ────────
    @staticmethod
    def _verify_action(node: WorkflowNode,
                       after_observed: Mapping[str, Any],
                       evidence_ref: str, action_id: str | None,
                       epoch: int) -> VerificationResult:
        targets = node.contract.desired_state or {}
        mismatched = [k for k in targets
                      if after_observed.get(k) != targets[k]]
        passed = not mismatched
        return VerificationResult(
            node_id=node.node_id, epoch=epoch, passed=passed,
            action_id=action_id, evidence_ref=evidence_ref,
            detail="ok" if passed else f"unmet desired_state: {mismatched}")

    # ── VERIFY: referenced variables must be at their desired values ──────
    @staticmethod
    def _verify_control(node: WorkflowNode,
                        after_observed: Mapping[str, Any],
                        desired: Mapping[str, Any], evidence_ref: str,
                        action_id: str | None, epoch: int) -> VerificationResult:
        pred = node.verification or ""
        referenced = VisibleVerifier._referenced_keys(pred, set(after_observed))
        if referenced:
            mismatched = [k for k in referenced
                          if after_observed.get(k) != desired.get(k)]
        else:
            mismatched = [k for k in desired
                          if desired[k] != after_observed.get(k)]
        passed = not mismatched
        return VerificationResult(
            node_id=node.node_id, epoch=epoch, passed=passed,
            action_id=action_id, evidence_ref=evidence_ref,
            detail="ok" if passed else f"verify unmet: {mismatched}")

    @staticmethod
    def _referenced_keys(predicate: str, known: set[str]) -> list[str]:
        """Deterministic parse: identifiers in the predicate that name known
        variables (so a free-text predicate still grounds in real observed
        quantities)."""
        seen: list[str] = []
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", predicate):
            if tok in known and tok not in _STOPWORDS and tok not in seen:
                seen.append(tok)
        return seen

    @staticmethod
    def _evidence_ref(observation: Any) -> str:
        """The captured screenshot the runtime passed in (an opaque ref the
        kernel stores but never interprets). Never an internal id."""
        return getattr(observation, "screenshot_ref", "") or ""
