"""taskvm.verifier — the verification package.

Two verifiers, one layering (architecture gate: this package imports ONLY
stdlib + ``taskvm.domain`` — everything else is injected):

- ``visible.VisibleVerifier`` — the runtime-visible verifier
  (runtime.md §6): the single owner of verification CONTENT legality for
  workflow nodes, judging completion from FRESH visible observation with
  ZERO model calls (deterministic). In the model-based verification flow
  its rule checks serve as a CHEAP PRE-FILTER only.
- ``model_verifier.ModelVerifier`` — the model-based verifier
  (PURETY-GEN §4.2): a VLM reads the fresh observation + business intent
  and answers the honest THREE-STATE verdict (changed / not_yet /
  cannot_verify). It is the sole FINAL judge; deterministic rules may only
  short-circuit ``not_yet`` (e.g. an unchanged fingerprint) and may never
  veto the model's conclusion. Every real provider request lands one
  ledger row with role ``model_verifier`` (registered in
  ``taskvm.architect.port.MODEL_ROLES``); the port / ledger / no-leak gate
  are all INJECTED (structural protocols — this package never imports the
  architect layer).

Both satisfy their consumer protocols STRUCTURALLY (no runtime/substrate
import) and are wired in by composition/tests.
"""
from taskvm.verifier.model_verifier import (
    MODEL_ROLE_MODEL_VERIFIER,
    VERDICTS,
    VERDICT_CANNOT_VERIFY,
    VERDICT_CHANGED,
    VERDICT_NOT_YET,
    ModelVerifier,
    ModelVerifierCallRecord,
)
from taskvm.verifier.visible import VisibleVerifier

__all__ = [
    "VisibleVerifier",
    "ModelVerifier", "ModelVerifierCallRecord",
    "MODEL_ROLE_MODEL_VERIFIER", "VERDICTS",
    "VERDICT_CHANGED", "VERDICT_NOT_YET", "VERDICT_CANNOT_VERIFY",
]
