"""browser_controller — Playwright live CUA on the WRITE path (W2+ stub).

W1 writes via the apps' own app-level API (reliable; see
``execution/action_dispatcher.py``). Live CUA GUI driving (click/fill/scroll
against the rendered HTML) is the W2 escalation — it makes the write path
faithful to "agent operates the application" in the CUA sense. The read path
is already GUI (compiler reads screenshot/DOM/a11y). Not built in W1.
"""
from __future__ import annotations


class BrowserController:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("browser_controller is W2+; W1 uses app-API execution.")
