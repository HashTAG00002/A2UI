"""taskvm.verifier.visible — the RUNTIME-visible verifier (runtime.md §6).

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
  observed value equals its target AND ``completion_condition``'s visible
  criterion is satisfied (runtime.md §6 — the load-bearing ``CUA done ≠
  verified`` check: the CUA may report done while the screen still shows the
  old value, OR while an independent completion criterion is unmet). See
  ``_completion_satisfied`` / RFC-003 for the minimal deterministic form.
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

    # -- ACTION: desired_state + completion_condition in the fresh obs -------
    @staticmethod
    def _verify_action(node: WorkflowNode,
                       after_observed: Mapping[str, Any],
                       evidence_ref: str, action_id: str | None,
                       epoch: int) -> VerificationResult:
        """runtime.md §6: passed iff desired_state matches AND
        completion_condition's visible criterion is satisfied. Both are
        checked against the FRESH after_observation; neither is silently
        skipped (a pre-fix version checked only desired_state — a doc-vs-code
        lie the module docstring once made)."""
        targets = node.contract.desired_state or {}
        reasons: list[str] = []
        mismatched = [k for k in targets
                     if after_observed.get(k) != targets[k]]
        if mismatched:
            reasons.append(f"unmet desired_state: {mismatched}")
        cc_ok, cc_detail = VisibleVerifier._completion_satisfied(
            node.contract.completion_condition, after_observed)
        if not cc_ok and cc_detail:
            reasons.append(cc_detail)
        passed = not reasons
        return VerificationResult(
            node_id=node.node_id, epoch=epoch, passed=passed,
            action_id=action_id, evidence_ref=evidence_ref,
            detail="ok" if passed else "; ".join(reasons))

    @staticmethod
    def _completion_satisfied(completion_condition: str,
                              after_observed: Mapping[str, Any]
                              ) -> tuple[bool, str | None]:
        """Evaluate ``completion_condition`` deterministically against the
        fresh after-observation (RFC-003; runtime.md §6). ZERO model calls,
        visible-only.

        - empty / whitespace -> ``(True, None)`` — no extra visible criterion
          (desired_state match is sufficient); NOT a silent skip, the contract
          expressed no additional criterion.
        - ``<key> == <value>`` (single clause, the same minimal parse form
          runtime.md §11 freezes for loop ``termination_predicate``) ->
          ``(str(after_observed[key]) == value, …)``. A key absent from the
          fresh observation cannot be grounded -> fail.
        - non-empty but not the minimal form -> ``(False, …)`` — the verifier
          CANNOT establish the visible criterion, so it fails honestly rather
          than silently passing (runtime.md §6: completion_condition MUST be
          checked; never ignored). RFC-003 defines this minimal mechanism; it
          is NOT a general predicate language / NLP parser.

        Returns ``(satisfied, detail)``; ``detail`` is None when satisfied.
        """
        cond = (completion_condition or "").strip()
        if not cond:
            return True, None
        # strict single-clause minimal form (RFC-003): exactly one '=='. A
        # multi-'==' string (e.g. 'a==b==c') is non-conforming -> fail-closed
        # (never silently satisfied by partitioning on the first '==').
        if cond.count("==") == 1:
            key, _, val = cond.partition("==")
            key = key.strip()
            val = val.strip().strip("'\"")
            if not key:
                return False, (f"completion_condition missing key "
                               f"(RFC-003): {cond!r}")
            got = after_observed.get(key)
            if got is None:
                return False, (f"completion_condition references unobserved "
                               f"key {key!r} (RFC-003)")
            ok = str(got).strip() == val
            return (ok, None if ok
                    else f"completion_condition unmet: {key}={got!r} != {val!r}")
        return False, (f"completion_condition not in the deterministic "
                       f"single-clause 'key == value' form (RFC-003): {cond!r}")

    # -- VERIFY: referenced variables must be at their desired values --------
    @staticmethod
    def _verify_control(node: WorkflowNode,
                        after_observed: Mapping[str, Any], desired: Mapping[str, Any], evidence_ref: str,
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
