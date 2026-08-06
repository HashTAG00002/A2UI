"""Benchmark layer: fixtures (hidden canonical task graphs), model client
(frontier-API), cost model (real-token accounting), A2UI v0.8 spec.

No-leak boundary: ``fixtures.py`` is verifier-only GT. It MUST NOT be imported
by the compiler path (``task_state/``, ``execution/``). See the code-review
checkpoint in the W1 plan (Verification step 6).
"""
