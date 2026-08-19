"""mobilegym.evaluation — the MobileGym EvaluationEnvironment.

Exam-room powers for the MobileGym substrate (contract §4): ``reset`` /
``seed`` (``env.set_state`` IS the documented setup API — MobileGym
runtime-api.md L276 — so this is the legitimate seed path, setup-only) /
``oracle_state`` (flattened entity maps over the sim store) /
``session_state`` / the generic ``app_state`` + ``os_state`` oracle reads.

Oracle reads, generic vs semantic projection:
  * ``app_state(sid, app_id)`` — the RAW zustand store slice of ANY app in
    the catalog (27 apps; storeless apps like calculator honestly return
    an empty state — their state IS the screen). App-agnostic: no
    per-app projection table, no id-field map.
  * ``os_state(sid)`` — the OS runtime slice (tasks, activeAppId,
    settings, notifications, home_screen): the part of the phone world
    that belongs to no app.
  * ``oracle_state(sid)`` — the semantic projection for the three
    table-backed apps (wechat chats / alipay transactions / x posts).
    Kept byte-stable for existing consumers; new code reads ``app_state``.

Physical separation: this object shares nothing with
``MobileGymSubstrateSession``. The runtime can never reach ``set_state``
through its session; the exam room can never be smuggled into the model
prompt chain from here — that is the verifier/benchmark's own
responsibility boundary.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


class MobileGymEvaluationEnvironment:
    """HTTP client over the bridge's setup/oracle routes (app per env)."""

    def __init__(self, app: str, sid: str, bridge_url: str,
                 timeout: float = 10.0):
        self.app = app                     # any app_id in the app catalog
        self.sid = sid
        self._bridge = bridge_url.rstrip("/")
        self.timeout = timeout

    # semantic projection tables (wechat / alipay / x only — the
    # three table-backed apps). Kept for byte-stable backward
    # compatibility; the generic oracle reads (app_state / os_state)
    # carry no per-app knowledge. New consumers should prefer
    # ``app_state``.
    _RESOURCE = {"wechat": "wechat_chats", "alipay": "alipay_transactions",
                 "x": "x_posts"}
    _ID_FIELD = {"wechat": "id", "alipay": "id", "x": "id"}
    _ENTITY_KIND = {"wechat": "chat", "alipay": "transaction", "x": "post"}

    # ── exam-room capabilities ─────────────────────────────────────────────
    def reset(self, sid: str | None = None) -> dict:
        s = sid or self.sid
        r = requests.post(f"{self._bridge}/api/reset/{s}",
                          timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def seed(self, sid: str, *, task_id: str | None, goal: str,
             seed_state: dict) -> dict:
        payload = {"task_id": task_id, "goal": goal, "seed_state": seed_state}
        r = requests.post(f"{self._bridge}/api/inject_task/{sid}",
                          json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def oracle_state(self, sid: str | None = None) -> dict:
        """Flattened entities for THIS env's app (verifier/benchmark only).

        LEGACY semantic projection: only the three historical apps
        (wechat/alipay/x) have a flattening table. For any other app this
        raises KeyError — the honest signal that the caller should use the
        generic ``app_state()`` read instead (raw store slice, any catalog
        app). No silent fallback: a caller that asked for a semantic
        projection of an app that has none gets an explicit error, not a
        made-up shape."""
        s = sid or self.sid
        resource = self._RESOURCE[self.app]
        r = requests.get(f"{self._bridge}/api/{resource}/{s}",
                         timeout=self.timeout)
        r.raise_for_status()
        rows = r.json().get(resource) or []
        idf = self._ID_FIELD[self.app]
        return {"entities": {row[idf]: dict(row) for row in rows}}

    def app_state(self, sid: str | None = None,
                  app_id: str | None = None) -> dict:
        """Generic oracle: the RAW zustand store slice of any catalog app
        (verifier/benchmark only). App-agnostic — the bridge validates the
        app_id against the catalog (404 for unknown apps) and returns the
        store slice verbatim; storeless apps (calculator, theme_store)
        honestly return an empty state. For the three table-backed apps
        (wechat/alipay/x) callers that want the flattened semantic
        projection should keep using ``oracle_state()``; this method
        returns the raw store dict either way."""
        s = sid or self.sid
        a = app_id or self.app
        r = requests.get(f"{self._bridge}/api/app_state/{s}/{a}",
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def os_state(self, sid: str | None = None) -> dict:
        """OS runtime state oracle (tasks, activeAppId, settings,
        notifications, home_screen) — the part of the phone world that
        belongs to no app."""
        s = sid or self.sid
        r = requests.get(f"{self._bridge}/api/os_state/{s}",
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def x_state(self, sid: str | None = None) -> dict:
        """X toggle lists (verifier read for the X evaluation scenarios)."""
        s = sid or self.sid
        r = requests.get(f"{self._bridge}/api/x_state/{s}",
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def session_state(self, sid: str | None = None) -> dict:
        s = sid or self.sid
        r = requests.get(f"{self._bridge}/api/session_state/{s}",
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def health(self) -> dict:
        r = requests.get(f"{self._bridge}/health", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        return None

    def __repr__(self):
        return (f"<MobileGymEvaluationEnvironment {self.app} "
                f"@ {self._bridge}>")


#: default ports (env-overridable)
DEFAULT_BRIDGE_PORT = 3019


def make_mobilegym_environments(
        apps: list[str], sid: str, host: str = "localhost",
        port: int | None = None, base_url: str | None = None,
        timeout: float = 10.0) -> dict[str, MobileGymEvaluationEnvironment]:
    import os
    if base_url is None:
        p = port
        if p is None:
            env = os.environ.get("TASKVM_MOBILEGYM_PORT")
            p = int(env) if env and env.isdigit() else DEFAULT_BRIDGE_PORT
        base_url = f"http://{host}:{p}"
    return {a: MobileGymEvaluationEnvironment(a, sid, base_url, timeout)
            for a in apps}
