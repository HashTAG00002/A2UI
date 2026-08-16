"""mobilegym.evaluation — the MobileGym EvaluationEnvironment (Agent B).

Exam-room powers for the MobileGym substrate (contract §4): ``reset`` /
``seed`` (``env.set_state`` IS the documented setup API — MobileGym
runtime-api.md L276 — so this is the legitimate seed path, setup-only) /
``oracle_state`` (flattened entity maps over the sim store) /
``session_state``.

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
        self.app = app                     # wechat | alipay | x
        self.sid = sid
        self._bridge = bridge_url.rstrip("/")
        self.timeout = timeout

    # resource route per app (mirror of the legacy adapters' tables)
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
        """Flattened entities for THIS env's app (verifier/benchmark only)."""
        s = sid or self.sid
        resource = self._RESOURCE[self.app]
        r = requests.get(f"{self._bridge}/api/{resource}/{s}",
                         timeout=self.timeout)
        r.raise_for_status()
        rows = r.json().get(resource) or []
        idf = self._ID_FIELD[self.app]
        return {"entities": {row[idf]: dict(row) for row in rows}}

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


#: legacy ports (kept for deployment continuity; env-overridable)
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
