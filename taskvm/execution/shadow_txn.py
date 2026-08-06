"""shadow_txn — copy-on-write shadow execution for reversible previews (W2+ stub).

Interface reserved for the Beyond-Submit direction (handoff §10): a user edit
is previewed against a shadow copy of app state; only on "commit" does it write
back for real and get verified. W1 commits directly (no preview). The
round-trip verifier already provides the honesty guarantee; shadow_txn adds
reversibility. Not built in W1.
"""
from __future__ import annotations


class ShadowTxn:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("shadow_txn is W2+ (Beyond-Submit interface reservation).")
