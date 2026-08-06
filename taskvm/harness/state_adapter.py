"""State adapter — the no-leak bridge between the harness and one running app.

The three-step ``reset`` / ``seed`` (inject_task) / ``read_canonical`` contract
is adapted from SenseAct's per-scenario adapter pattern; the entity-map
normalization is TaskVM-native (so the verifier is app-agnostic):

    read_canonical(sid) -> {"entities": {entity_id: {field: value, ...}}}

Load-bearing: ``read_canonical`` is the ONLY read the verifier uses, and it
returns the real app state (events/tasks). The canonical task graph
(expected_diff / bindings / non_interference_set) is NEVER in the app — it
lives in ``benchmark/fixtures.py`` and goes directly to the verifier. The
compiler never calls ``read_canonical`` (it reads rendered GUI observations
captured by ``replay_engine.capture_obs``).
"""
from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class StateAdapter:
    """Base HTTP adapter for one app. Subclasses set ``resource`` (the JSON
    list route), ``id_field`` (eid/tid), and ``entity_kind`` (event/task)."""

    app: str = ""
    base_url: str = ""
    resource: str = ""      # e.g. "events" -> GET /api/events/<sid>
    id_field: str = ""      # "eid" | "tid"
    entity_kind: str = ""   # "event" | "task" (for logs)

    def __init__(self, base_url: str, app: str | None = None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        if app:
            self.app = app
        self.timeout = timeout

    # ── HTTP helpers ────────────────────────────────────────────────────────
    def health(self) -> dict:
        r = requests.get(f"{self.base_url}/health", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def reset(self, sid: str) -> dict:
        r = requests.post(f"{self.base_url}/api/reset/{sid}", timeout=self.timeout)
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
        """Summary only (n_events/n_tasks, has_task) — never canonical GT."""
        r = requests.get(f"{self.base_url}/api/session_state/{sid}",
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _list(self, sid: str) -> list[dict]:
        r = requests.get(f"{self.base_url}/api/{self.resource}/{sid}",
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json().get(self.resource) or []

    def mutate(self, sid: str, entity_id: str, operator: str, value: Any) -> dict:
        """The executable-operator call on the write path (used by
        ``execution/action_dispatcher``). Subclasses route to the right URL."""
        raise NotImplementedError

    # ── canonical read (verifier-only) ──────────────────────────────────────
    def read_canonical(self, sid: str) -> dict:
        """Return the real session state as a normalized entity map. This is
        what the verifier compares against ``expected_diff`` (round-trip GT).

        Returns ``{"entities": {entity_id: {field: value, ...}}}`` — the visible
        app state only. No action log (TaskVM's verifier judges via entity
        fields, not a SenseAct-style state-mode action log)."""
        rows = self._list(sid)
        entities = {row[self.id_field]: dict(row) for row in rows}
        return {"entities": entities}

    def __repr__(self):
        return f"<{self.__class__.__name__} {self.app} @ {self.base_url}>"


class CalendarAdapter(StateAdapter):
    app = "calendar"
    resource = "events"
    id_field = "eid"
    entity_kind = "event"

    def mutate(self, sid: str, entity_id: str, operator: str, value: Any) -> dict:
        if operator != "move_event":
            raise ValueError(f"calendar only supports move_event, got {operator}")
        r = requests.post(f"{self.base_url}/api/event/{sid}/move",
                          json={"eid": entity_id, "new_date": value},
                          timeout=self.timeout)
        r.raise_for_status()
        return r.json()


class TaskBoardAdapter(StateAdapter):
    app = "taskboard"
    resource = "tasks"
    id_field = "tid"
    entity_kind = "task"

    def mutate(self, sid: str, entity_id: str, operator: str, value: Any) -> dict:
        if operator not in ("set_deadline", "set_status", "set_assignee"):
            raise ValueError(f"taskboard operator must be set_deadline/set_status/"
                             f"set_assignee, got {operator}")
        r = requests.post(f"{self.base_url}/api/task/{sid}/{entity_id}",
                          json={"operator": operator, "value": value},
                          timeout=self.timeout)
        r.raise_for_status()
        return r.json()


# ── registry / factories ────────────────────────────────────────────────────
_ADAPTER_CLASSES = {
    "calendar": CalendarAdapter,
    "taskboard": TaskBoardAdapter,
}

DEFAULT_PORTS = {"calendar": 3013, "taskboard": 3014}


def make_adapter(app: str, port: int | None = None, host: str = "localhost",
                 base_url: str | None = None) -> StateAdapter:
    """Factory. ``base_url`` overrides host/port if given (e.g. docker service name)."""
    if app not in _ADAPTER_CLASSES:
        raise ValueError(f"unknown app {app!r}; known: {list(_ADAPTER_CLASSES)}")
    if base_url is None:
        p = port or DEFAULT_PORTS[app]
        base_url = f"http://{host}:{p}"
    return _ADAPTER_CLASSES[app](base_url=base_url, app=app)


def make_adapters(apps: list[str] | None = None, **kwargs) -> dict[str, StateAdapter]:
    """Build a {app_name: adapter} dict for the W1 app set (default: both)."""
    apps = apps or ["calendar", "taskboard"]
    return {a: make_adapter(a, **kwargs) for a in apps}
