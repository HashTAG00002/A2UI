"""B-04 — ``ProjectionClient``: the bench plane's ONLY handle on a running
TaskVM session, over the projection PUBLIC HTTP API exclusively.

Hard rules this module exists to enforce (RM-0 work order §B-04):

* every interaction is a documented route from ``taskvm/projection/app.py``
  (read: snapshot/governance/variables/workflow/checkpoints/surfaces/
  conflicts/events/sse; write: governance start|pause|resume|stop|
  local_patch|goal_patch|checkpoint|rollback);
* NO handle on Kernel / Runtime / CUAModel / GovernanceService internals —
  those objects are never importable from here, only HTTP speaks;
* op correlation is kept CLIENT-side: every request is appended to
  ``request_log`` so a caller can pin ``op_id ↔ request/response/SSE
  window`` without any prototype-only test API;
* SSE envelopes are consumed from the public ``/sse`` stream — the settle
  barrier (see ``user_ops.py``) never asks the prototype for a hidden
  ``/test/accepted`` endpoint.

SSE type strings below are the FROZEN vocabulary of
``taskvm/projection/events.py`` (D-F3: the totality-asserted union) —
they are mirrored here as plain strings because the bench plane talks
HTTP, not Python objects.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Optional

import requests

# frozen SSE vocabulary (projection/events.py) — mirrored, not imported:
# the bench speaks HTTP to the SUT, never reaches into its objects.
SSE_GOVERNANCE_APPLIED = "governance.applied"
SSE_SNAPSHOT = "snapshot"
SSE_ACTION_OBSERVED = "action.observed"
SSE_ACTION_LANDED = "action.landed"
#: runtime/kernel frames that indicate world/structure movement — used by
#: the quiet-window settle policy (any of these resets the quiet clock).
SSE_PROGRESS_TYPES = frozenset({
    SSE_ACTION_OBSERVED, SSE_ACTION_LANDED,
    "observation.received", "state.updated", "compensation.requested",
})


class ProjectionClient:
    """HTTP client for ONE projection session (``sid``)."""

    def __init__(self, base_url: str, sid: str, *,
                 http: Optional[Any] = None,
                 timeout_s: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.sid = sid
        #: duck-typed HTTP session (``requests.Session`` in production; a
        #: scripted fake in bench tests — only .get/.post are required)
        self._http = http if http is not None else requests.Session()
        self.timeout_s = timeout_s
        #: client-side op correlation: (ts, method, path, status_or_None)
        self.request_log: list[dict] = []

    # ── internals ───────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/sessions/{self.sid}{path}"

    def _get(self, path: str) -> dict:
        url = self._url(path)
        ts = time.time()
        resp = self._http.get(url, timeout=self.timeout_s)
        self.request_log.append(dict(ts=ts, method="GET", path=path,
                                     status=resp.status_code))
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: Optional[dict] = None):
        url = self._url(path)
        ts = time.time()
        resp = self._http.post(url, json=body or {}, timeout=self.timeout_s)
        self.request_log.append(dict(ts=ts, method="POST", path=path,
                                     status=resp.status_code))
        try:
            parsed = resp.json()
        except ValueError:
            parsed = {"_raw": resp.text[:500]}
        return resp.status_code, parsed

    # ── public read routes ──────────────────────────────────────────────
    def snapshot(self) -> dict:
        return self._get("/snapshot")

    def governance(self) -> dict:
        return self._get("/governance")

    def variables(self) -> dict:
        return self._get("/variables")

    def workflow(self) -> dict:
        return self._get("/workflow")

    def checkpoints(self) -> dict:
        return self._get("/checkpoints")

    def surfaces(self) -> dict:
        return self._get("/surfaces")

    def conflicts(self) -> dict:
        return self._get("/conflicts")

    def events(self, offset: int = 0, limit: int = 200) -> dict:
        url = self._url(f"/events?offset={offset}&limit={limit}")
        resp = self._http.get(url, timeout=self.timeout_s)
        resp.raise_for_status()
        return resp.json()

    def event_count(self) -> int:
        """Total kernel events (the public ``total`` of the events page) —
        the barrier's fallback settle signal when SSE is unavailable."""
        return int(self.events(offset=0, limit=1).get("total", 0))

    # ── public governance (write) routes ────────────────────────────────
    def start(self) -> tuple:
        return self._post("/governance/start")

    def pause(self, rationale: str = "") -> tuple:
        return self._post("/governance/pause", {"rationale": rationale})

    def resume(self, rationale: str = "") -> tuple:
        return self._post("/governance/resume", {"rationale": rationale})

    def stop(self, rationale: str = "") -> tuple:
        return self._post("/governance/stop", {"rationale": rationale})

    def local_patch(self, updates: dict, rationale: str = "") -> tuple:
        return self._post("/governance/local_patch",
                          {"updates": updates, "rationale": rationale})

    def goal_patch(self, goal: str, *, constraints=(), scope=(),
                   success_criteria=(), rationale: str = "") -> tuple:
        return self._post("/governance/goal_patch", {
            "goal": goal, "constraints": list(constraints),
            "scope": list(scope),
            "success_criteria": list(success_criteria),
            "rationale": rationale})

    def checkpoint(self, label: str) -> tuple:
        return self._post("/governance/checkpoint", {"label": label})

    def rollback(self, target_checkpoint_id: str,
                 rationale: str = "") -> tuple:
        return self._post("/governance/rollback", {
            "target_checkpoint_id": target_checkpoint_id,
            "rationale": rationale})

    # ── SSE consumption (public stream) ─────────────────────────────────
    def open_sse_window(self) -> "SSEWindow":
        """Start collecting the public SSE stream; returns the window the
        barrier reads. One window at a time per client (a second open
        closes the first — bench tests are sequential per session)."""
        window = SSEWindow(self.base_url, self.sid, self._http,
                           timeout_s=self.timeout_s)
        window.start()
        return window


class SSEWindow:
    """A live collector over ``GET /api/sessions/<sid>/sse``.

    Envelopes are appended to ``events`` (list of dicts as delivered); the
    settle barrier polls ``events`` without touching the prototype. The
    reader thread is a daemon and stops on ``close()``.
    """

    def __init__(self, base_url: str, sid: str,
                 http: requests.Session, *, timeout_s: float) -> None:
        self._url = f"{base_url.rstrip('/')}/api/sessions/{sid}/sse"
        self._http = http
        self._timeout_s = timeout_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self.error: Optional[str] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            with self._http.get(self._url, stream=True,
                                timeout=(self._timeout_s, None)) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines(decode_unicode=True):
                    if self._stop.is_set():
                        return
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    try:
                        envelope = json.loads(payload)
                    except ValueError:
                        continue
                    if isinstance(envelope, dict):
                        with self._lock:
                            self.events.append(envelope)
        except Exception as e:  # connection closed / server stopped — honest
            if not self._stop.is_set():
                self.error = str(e)

    def snapshot_events(self) -> list:
        with self._lock:
            return list(self.events)

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            # daemon reader: closing means "stop collecting", not "wait
            # for the socket to drain" — the thread ends on its own.
            thread.join(timeout=0.3)
