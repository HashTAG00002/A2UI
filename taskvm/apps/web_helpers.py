"""Session-state helpers shared by the TaskVM builtin apps.

Contract shape (in-memory ``user_sessions`` dict keyed by a fresh random sid
per episode, + a FIFO cap, + a summary-only state payload) adapted from
SenseAct's ``senseact/web_helpers.py`` — but only the generic session-state
machinery TaskVM actually uses.

Load-bearing: ``session_state_payload`` returns ONLY a summary (counts/status)
— it must NEVER include oracle answers / canonical task graph / expected
diff. Ground-truth state is evaluation-plane-only (see the substrate
evaluation adapters).
"""
from __future__ import annotations

MAX_SESSIONS = 256  # reap oldest sessions beyond this to bound long-eval memory


def reap_sessions(user_sessions: dict, max_sessions: int = MAX_SESSIONS) -> int:
    """Cap the in-memory session dict (FIFO eviction). Each episode uses a new
    random sid, so evicted sids are never revisited — FIFO == safe. The
    just-injected sid is at the tail, so it is never evicted. Returns the
    number evicted."""
    excess = len(user_sessions) - max_sessions
    if excess <= 0:
        return 0
    for k in list(user_sessions.keys())[:excess]:
        user_sessions.pop(k, None)
    return excess


def session_state_payload(site: str, sess: dict, has_task: bool,
                          summary: dict) -> dict:
    """Build the canonical ``/api/session_state`` JSON. Every app returns the
    same shape so the harness can aggregate uniformly; ``summary`` is the
    app-specific part (counts/status only — NEVER oracle/canonical GT)."""
    return {
        "site": site,
        "exists": bool(sess),
        "has_task": has_task,
        "summary": summary or {},
    }
