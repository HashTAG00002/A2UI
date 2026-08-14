"""builtin_web.evaluation — the Web EvaluationEnvironment (Agent B).

Exam-room powers over the builtin Flask apps (contract §4): ``reset`` /
``seed`` / ``oracle_state`` (the successor of the legacy
``StateAdapter.read_canonical``) / ``session_state``. This object is for
the evaluation plane (verifier, benchmark runners, demo seeding). It is a
DIFFERENT object from ``WebSubstrateSession`` on purpose: possession of a
runtime session grants none of these powers and vice versa.

What is intentionally ABSENT from the RUNTIME: any write/mutate reach. The
legacy API mutation executor (``requests.post`` to
``/api/<resource>/<sid>/<eid>`` as a runtime WRITE path) is deleted for
good (task brief §一; handoff 03 §删除 API Executor). The one exception on
this plane is ``force_write`` — an exam-room injection that simulates an
EXTERNAL actor's edit for reconciliation killtests. It is reachable only
through an EvaluationEnvironment, never a SubstrateSession, and the static
gate (``tests/substrate/test_no_api_backdoor.py``) fails any attempt to
bring an app-mutation ``requests.post`` back into the runtime.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class WebEvaluationEnvironment:
    """Evaluation-plane adapter for ONE builtin web app.

    Read shape (unchanged from the legacy contract so the verifier keeps
    working): ``oracle_state(sid) -> {"entities": {id: {field: value}}}``.
    """

    app: str = ""
    base_url: str = ""
    resource: str = ""      # e.g. "events" -> GET /api/events/<sid>
    id_field: str = ""      # "eid" | "tid" | "fid" | "mid" | "aid"
    entity_kind: str = ""   # "event" | "task" | ... (labels only)

    def __init__(self, base_url: str, app: str | None = None,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        if app:
            self.app = app
        self.timeout = timeout

    # ── exam-room capabilities (evaluation plane ONLY) ─────────────────────
    def health(self) -> dict:
        r = requests.get(f"{self.base_url}/health", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def reset(self, sid: str) -> dict:
        r = requests.post(f"{self.base_url}/api/reset/{sid}",
                          timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def seed(self, sid: str, *, task_id: str | None, goal: str,
             seed_state: dict) -> dict:
        """inject_task: seed the visible app state. No canonical GT here."""
        payload = {"task_id": task_id, "goal": goal, "seed_state": seed_state}
        r = requests.post(f"{self.base_url}/api/inject_task/{sid}",
                          json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def session_state(self, sid: str) -> dict:
        """Summary only (counts) — never canonical GT."""
        r = requests.get(f"{self.base_url}/api/session_state/{sid}",
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def oracle_state(self, sid: str) -> dict:
        """Hidden ground-truth read (was ``read_canonical``). Verifier /
        benchmark ONLY — importing or calling this from the runtime
        decision chain is an architecture violation (contract §4)."""
        rows = self._list(sid)
        entities = {row[self.id_field]: dict(row) for row in rows}
        return {"entities": entities}

    def force_write(self, sid: str, entity_id: str, operator: str,
                    value: Any, **payload_extra: Any) -> dict:
        """Exam-room ONLY write: inject a change AS IF an external actor
        (another human / another system) edited the app behind TaskVM's back.
        Used by reconciliation killtests to manufacture the concurrent-modify
        conflict. This is the one sanctioned use of the app's mutation HTTP
        route — it lives on the evaluation plane precisely so the runtime
        can never reach it. The static gate (tests/substrate/
        test_no_api_backdoor.py) allows ``requests.post`` app-mutations ONLY
        in this file."""
        payload = {"operator": operator, "value": value, **payload_extra}
        r = requests.post(
            f"{self.base_url}/api/{self.app}/{sid}/{entity_id}",
            json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def close(self) -> None:      # protocol parity; HTTP envs are stateless
        return None

    # ── internal ───────────────────────────────────────────────────────────
    def _list(self, sid: str) -> list[dict]:
        r = requests.get(f"{self.base_url}/api/{self.resource}/{sid}",
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json().get(self.resource) or []

    def __repr__(self):
        return f"<{self.__class__.__name__} {self.app} @ {self.base_url}>"


# ── per-app tables (migrated from legacy substrate/base.py) ─────────────────

class CalendarEvaluationEnv(WebEvaluationEnvironment):
    app = "calendar"
    resource = "events"
    id_field = "eid"
    entity_kind = "event"


class TaskBoardEvaluationEnv(WebEvaluationEnvironment):
    app = "taskboard"
    resource = "tasks"
    id_field = "tid"
    entity_kind = "task"


class DriveEvaluationEnv(WebEvaluationEnvironment):
    app = "drive"
    resource = "files"
    id_field = "fid"
    entity_kind = "file"


class MailEvaluationEnv(WebEvaluationEnvironment):
    app = "mail"
    resource = "messages"
    id_field = "mid"
    entity_kind = "message"


class OutlookCalEvaluationEnv(WebEvaluationEnvironment):
    app = "outlook_cal"
    resource = "appointments"
    id_field = "aid"
    entity_kind = "appointment"


_EVAL_CLASSES: dict[str, type[WebEvaluationEnvironment]] = {
    "calendar": CalendarEvaluationEnv,
    "taskboard": TaskBoardEvaluationEnv,
    "drive": DriveEvaluationEnv,
    "mail": MailEvaluationEnv,
    "outlook_cal": OutlookCalEvaluationEnv,
}

DEFAULT_PORTS: dict[str, int] = {
    "calendar": 3013, "taskboard": 3014, "drive": 3015,
    "mail": 3017, "outlook_cal": 3018,
}


def make_evaluation_environment(app: str, port: int | None = None,
                                host: str = "localhost",
                                base_url: str | None = None,
                                timeout: float = 10.0
                                ) -> WebEvaluationEnvironment:
    if app not in _EVAL_CLASSES:
        raise ValueError(f"unknown builtin web app {app!r}; "
                         f"known: {list(_EVAL_CLASSES)}")
    if base_url is None:
        p = port or DEFAULT_PORTS[app]
        base_url = f"http://{host}:{p}"
    return _EVAL_CLASSES[app](base_url=base_url, app=app, timeout=timeout)


def make_evaluation_environments(
        apps: list[str] | None = None, **kwargs
        ) -> dict[str, WebEvaluationEnvironment]:
    """``{app: WebEvaluationEnvironment}`` for an app set. This replaces the
    legacy ``make_adapters()`` oracle/seed surface; the runtime write
    surface replaced by it lives in ``taskvm.execution.gui_driver``."""
    return {a: make_evaluation_environment(a, **kwargs) for a in (apps or [])}
