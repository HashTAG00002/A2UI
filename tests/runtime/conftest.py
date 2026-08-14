"""tests/runtime/ — OWNER: Agent E (autonomy runtime).

Contract tests for the runtime's producer obligations (layered ownership
protocol §1): before/after observations come from fresh pre/post-action
observation of the visible world; irreversibility is reported honestly;
compensation is executed through the SAME real execution path as forward
work; VerificationResult / CompensationResult are constructed truthfully
via the typed domain contracts.

Agent A (kernel) does NOT implement these. The kernel-side landing
semantics (epoch / single-use / coverage / disposition) are pinned in
tests/kernel/test_timeline_governance.py.
"""
