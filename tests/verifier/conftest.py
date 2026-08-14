"""tests/verifier/ — OWNER: Agent E (independent visible verifier).

Contract tests for the verifier's CONTENT obligations (layered ownership
protocol §1/§3): VerificationResult.passed reflects an independent
visible-world check (completion_condition evaluated, observed vs desired
judged by the verifier — never by the kernel); CompensationEntryResult.
compensated=True is only reported when a fresh observation confirms the
plan entry's target; evidence_ref points at real captured evidence.

Agent A (kernel) does NOT implement these: the kernel consumes the typed
verdicts and checks only TIME (identity / epoch / lifecycle / coverage).
"""
