"""live_sync — live re-sync of the surface to app state (W2 stub).

W1 re-renders from the post-state canonical graph inside the verifier
(``check_interface_resynced``). Live push-based sync (surface updates as the
app state changes) is W2. Not built in W1.
"""
from __future__ import annotations
