"""replanner — incremental re-plan on partial failure / patch impact (W2+ stub).

Interface reserved for the PromptPatch direction (handoff §10): when a user
edit invalidates part of an in-flight plan, replan only the affected subtree
and preserve valid progress. W1 dispatch is a single shot (no in-flight plan
to replan). Not built in W1.
"""
from __future__ import annotations


def replan(*args, **kwargs):
    raise NotImplementedError("replanner is W2+ (PromptPatch interface reservation).")
