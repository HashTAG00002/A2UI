"""taskvm.verifier — Agent E's runtime-visible verifier.

``visible.VisibleVerifier`` is the runtime-visible verifier (runtime.md §6):
the single owner of verification CONTENT legality — it judges completion from
FRESH visible observation, never hidden DB / fixture / oracle. The kernel
lands its typed ``VerificationResult`` with TIME checks only. It satisfies the
``taskvm.runtime.ports.Verifier`` Protocol structurally (pure domain+stdlib,
no runtime import), and is injected into the runtime by composition/tests.

The legacy modules below (canonical_state / non_interference /
round_trip_checks / cross_app_checks / reconciliation / rollback_verify) are
EVALUATION-plane oracle verifiers (read hidden canonical sandbox state) —
they belong in ``taskvm/evaluation/`` (Agent F) and remain here only until
their call sites migrate; they are NOT part of the runtime-visible path.
"""
from taskvm.verifier.visible import VisibleVerifier

__all__ = ["VisibleVerifier"]
