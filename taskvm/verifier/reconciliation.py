"""reconciliation — re-read-on-action + conflict marking (W4+ stub).

Handoff §15-E-Q11: re-read-on-action (user edit / heartbeat triggers re-read)
+ conflict → mark red, never silently overwrite ("底层已变 / 你的编辑 / 合并选项").
W4 will introduce a mechanism for the benchmark to inject a concurrent external
state change (e.g. a colleague moves a Jira deadline to 8/20 while the user set
8/18) to test conflict marking — this is SaC's "frontend state synchronisation"
future-work gap. W1-W3 reconciliation relies on historical heartbeat re-read
only. Not built in W1.
"""
from __future__ import annotations


def reconcile(*args, **kwargs):
    raise NotImplementedError("reconciliation is W4+.")
