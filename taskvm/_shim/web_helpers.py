"""Session-state helpers for the TaskVM apps.

Contract shape (in-memory ``user_sessions`` dict keyed by a fresh random sid
per episode, + a FIFO cap, + a summary-only state payload) adapted from
SenseAct's ``senseact/web_helpers.py`` — but only the generic session-state
machinery TaskVM actually uses. SenseAct's price/review Jinja filters, its
``tick_session`` belief-over-time clock (for time-sensitive cancel windows /
shipping stages), and its submit-mode ``done``/``reward`` fields are
SenseAct-specific and NOT ported (TaskVM has no time-sensitive app state and
no submit-answer scoring — success is judged by ``verifier/round_trip_checks``
reading canonical state).

Load-bearing: ``session_state_payload`` returns ONLY a summary (counts/status) —
it must NEVER include oracle_answer / canonical task graph / expected_diff. The
canonical state is verifier-only (see ``benchmark/fixtures.py`` +
``verifier/canonical_state.py``).
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
