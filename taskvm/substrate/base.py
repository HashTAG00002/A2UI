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
    # E10 rework (P2): when True, mutate drives a real browser via the GUI
    # executor (grounding model + Playwright) instead of requests.post to the
    # app's internal Flask API. Set via make_adapter(executor='gui_agent').
    use_gui_executor: bool = False
    gui_screenshot_dir: str | None = None   # if set, gui_executor saves step PNGs here

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

    def _mutate_via_gui(self, sid: str, entity_id: str, operator: str,
                        value: Any, *, field: str,
                        undo: bool = False) -> dict:
        """P2 GUI-executor write path (non-invasive: real browser gestures via
        ``gui_executor.gui_write``, NOT ``requests.post`` to the internal API).

        No-leak before-value: ``old`` is captured from ``read_canonical`` (the
        real visible app state) BEFORE the gesture — never from fixtures. After
        the gesture, re-read ``read_canonical`` to capture ``new`` + verify the
        change landed. GUI agents are not 100% reliable (e.g. sometimes click
        Cancel instead of Confirm), so on verification failure we RETRY up to
        ``GUI_WRITE_RETRIES`` times with an attempt-aware instruction before
        raising honestly.

        Raises ``GuiExecutorFailure`` on honest irreversibility (model outputs
        ``fail``) → ``undo_saga`` catches → ``partial_failure=True``."""
        from taskvm.execution.gui_executor import gui_write, GuiExecutorFailure
        GUI_WRITE_RETRIES = 2   # 1 initial + 2 retries = 3 attempts max
        # capture the before-value from the real app state (no GT leak)
        before_state = self.read_canonical(sid)
        ent = (before_state.get("entities") or {}).get(entity_id) or {}
        old_value = ent.get(field)
        last_resp = None
        last_err = None
        prev_screenshot = None   # Task2 (E12): retry carries the stuck screenshot
        resume_url = None        # Task2 (E12): retry resumes from the edit form
        for attempt in range(1, GUI_WRITE_RETRIES + 2):   # 1..3
            try:
                resp = gui_write(
                    app=self.app, sid=sid, entity_id=entity_id, operator=operator,
                    value=value, field=field, entity_kind=self.entity_kind,
                    base_url=self.base_url, old_value=old_value,
                    screenshot_dir=self.gui_screenshot_dir, undo=undo,
                    attempt=attempt,
                    prev_screenshot=prev_screenshot, resume_url=resume_url,
                    backend_name=getattr(self, "grounding_backend", "gpt56sol"))
            except GuiExecutorFailure:
                raise   # honest irreversibility — don't retry
            last_resp = resp
            # Task2: capture the stuck screenshot + edit-form URL for the retry.
            # On the NEXT attempt the executor resumes from the edit form (not
            # the list page) + the model sees where this attempt got stuck, so
            # it doesn't re-walk View→Edit→…→Confirm from scratch (E12 measured
            # 16 calls/op avg because retries re-walked the whole form).
            trace = resp.get("trace") or {}
            prev_screenshot = trace.get("last_screenshot")
            resume_url = self._edit_form_url(sid, entity_id)
            # verify the change landed in the real app state (honest check)
            after_state = self.read_canonical(sid)
            ent_after = (after_state.get("entities") or {}).get(entity_id) or {}
            new_value = ent_after.get(field)
            if not undo:
                ok = str(new_value).strip().lower() == str(value).strip().lower()
            else:
                ok = str(new_value).strip().lower() == str(old_value).strip().lower()
            if ok:
                resp["new"] = new_value
                resp["old"] = old_value
                resp["attempts"] = attempt
                return resp
            last_err = (f"GUI write did not land: {self.app}.{entity_id}.{field} "
                        f"expected {value if not undo else old_value!r}, "
                        f"got {new_value!r} after {resp.get('trace',{}).get('steps',0)} steps (attempt {attempt})")
            logger.warning(f"[gui_mutate] {last_err}; {'retrying' if attempt <= GUI_WRITE_RETRIES else 'giving up'}")
        raise RuntimeError(last_err or "GUI write failed")

    # ── edit-form URL (Task2: retry resume point, per-app kind) ───────────────
    _EDIT_PATH_KIND = {   # app → URL path segment for the edit form
        "calendar": "event", "taskboard": "task", "drive": "file",
        "mail": "message", "outlook_cal": "appointment",
    }

    def _edit_form_url(self, sid: str, entity_id: str) -> str | None:
        """The edit-form URL for this app+entity (the retry resume point).
        Each app's P1 GUI exposes ``/<sid>/<kind>/<eid>/edit``. Returns None if
        the app has no known edit-form path (the retry then falls back to the
        list URL, as before)."""
        kind = self._EDIT_PATH_KIND.get(self.app)
        if not kind:
            return None
        return f"{self.base_url}/{sid}/{kind}/{entity_id}/edit"

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
        if self.use_gui_executor:
            # P2 (E10 rework): drive the real browser through the edit form
            # (list → View → Edit → date input → Review → Confirm) via the
            # grounding model. The Flask /api/event/<sid>/move route stays as
            # the app's backend (the form posts to it when the GUI clicks
            # Confirm), but mutate no longer calls it directly.
            resp = self._mutate_via_gui(sid, entity_id, operator, value,
                                        field="date")
            # match the legacy response shape (old_date/new_date) so any caller
            # reading those keys keeps working
            resp["old_date"] = resp.get("old")
            resp["new_date"] = resp.get("new")
            resp["eid"] = entity_id
            return resp
        # legacy API path (requests.post to the internal Flask route)
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
    # operator → field (mirrors the app's _FIELD_MAP + OPERATOR_REGISTRY)
    _OP_FIELD = {"set_deadline": "deadline", "set_status": "status",
                 "set_assignee": "assignee"}

    def mutate(self, sid: str, entity_id: str, operator: str, value: Any) -> dict:
        if operator not in ("set_deadline", "set_status", "set_assignee"):
            raise ValueError(f"taskboard operator must be set_deadline/set_status/"
                             f"set_assignee, got {operator}")
        if self.use_gui_executor:
            # P2 (E10 rework): drive the real browser through the task edit form
            # (list → View → Edit → change the operator's field → Review → ✓ Confirm)
            resp = self._mutate_via_gui(sid, entity_id, operator, value,
                                        field=self._OP_FIELD[operator])
            resp["tid"] = entity_id
            return resp
        r = requests.post(f"{self.base_url}/api/task/{sid}/{entity_id}",
                          json={"operator": operator, "value": value},
                          timeout=self.timeout)
        r.raise_for_status()
        return r.json()


class DriveAdapter(StateAdapter):
    """W2 third app. ``move_file`` / ``rename`` / ``set_owner`` map to the
    file's ``parent`` / ``name`` / ``owner`` field respectively. The app's mutate
    response returns ``old`` (the before-value of the changed field) so the
    rollback skeleton can compensate without a separate pre-snapshot."""
    app = "drive"
    resource = "files"
    id_field = "fid"
    entity_kind = "file"
    _OP_FIELD = {"move_file": "parent", "rename": "name", "set_owner": "owner",
                 "set_publish_date": "publish_date"}

    def mutate(self, sid: str, entity_id: str, operator: str, value: Any) -> dict:
        if operator not in ("move_file", "rename", "set_owner", "set_publish_date"):
            raise ValueError(f"drive operator must be move_file/rename/set_owner/"
                             f"set_publish_date, got {operator}")
        if self.use_gui_executor:
            # P2 (E10 rework): drive the real browser through the file edit form
            resp = self._mutate_via_gui(sid, entity_id, operator, value,
                                        field=self._OP_FIELD[operator])
            resp["fid"] = entity_id
            return resp
        r = requests.post(f"{self.base_url}/api/file/{sid}/{entity_id}",
                          json={"operator": operator, "value": value},
                          timeout=self.timeout)
        r.raise_for_status()
        return r.json()


class MailAdapter(StateAdapter):
    """W4 held-out **truly-unseen** app. ``set_state`` / ``set_priority`` /
    ``set_to`` map to the message's ``state`` / ``priority`` / ``to_addr`` field.
    Mutate response returns ``old`` (before-value) so rollback compensates
    without a pre-snapshot — same contract as Drive/TaskBoard."""
    app = "mail"
    resource = "messages"
    id_field = "mid"
    entity_kind = "message"
    _OP_FIELD = {"set_state": "state", "set_priority": "priority", "set_to": "to_addr",
                 "set_send_date": "send_date"}

    def mutate(self, sid: str, entity_id: str, operator: str, value: Any) -> dict:
        if operator not in ("set_state", "set_priority", "set_to", "set_send_date"):
            raise ValueError(f"mail operator must be set_state/set_priority/"
                             f"set_to/set_send_date, got {operator}")
        if self.use_gui_executor:
            resp = self._mutate_via_gui(sid, entity_id, operator, value,
                                        field=self._OP_FIELD[operator])
            resp["mid"] = entity_id
            return resp
        r = requests.post(f"{self.base_url}/api/mail/{sid}/{entity_id}",
                          json={"operator": operator, "value": value},
                          timeout=self.timeout)
        r.raise_for_status()
        return r.json()


class OutlookCalAdapter(StateAdapter):
    """W4 held-out **reskin** of calendar. Same semantics (move a meeting to a
    new date) but renamed substrate: the entity kind is ``appointment`` (not
    ``event``), the field is ``scheduled_for`` (not ``date``), the operator is
    ``reschedule_appointment`` (not ``move_event``), and the DOM id attribute is
    ``data-appointment-id``. Tests substrate-independence: the same conceptual
    operation must be discovered under a different skin."""
    app = "outlook_cal"
    resource = "appointments"
    id_field = "aid"
    entity_kind = "appointment"

    def mutate(self, sid: str, entity_id: str, operator: str, value: Any) -> dict:
        if operator != "reschedule_appointment":
            raise ValueError(f"outlook_cal only supports reschedule_appointment, "
                             f"got {operator}")
        if self.use_gui_executor:
            # P2 (E10 rework): drive the real browser through the appointment
            # edit form. The grounding model must ground on the RENAMED
            # substrate (data-appointment-id / scheduled_for / reschedule) —
            # the substrate-independence test (handoff §12.11).
            resp = self._mutate_via_gui(sid, entity_id, operator, value,
                                        field="scheduled_for")
            resp["aid"] = entity_id
            resp["old_scheduled_for"] = resp.get("old")
            resp["new_scheduled_for"] = resp.get("new")
            return resp
        # calendar-style route (literal ``/reschedule`` suffix, aid in the body) —
        # NOT the drive-style ``/api/appointment/<sid>/<aid>`` path form.
        r = requests.post(f"{self.base_url}/api/appointment/{sid}/reschedule",
                          json={"aid": entity_id, "new_scheduled_for": value},
                          timeout=self.timeout)
        r.raise_for_status()
        return r.json()


class WechatAdapter(StateAdapter):
    """MobileGym demo app. The substrate is a Playwright-driven React sim
    served by ``harness/mobilegym_bridge.py`` (one resident HTTP server on
    :3019 holding a ``MobileGymEnv``). This adapter is a thin sync ``requests``
    client over that bridge — so ``rollback.py`` / ``verifier/`` are unchanged.

    Write surface: ``send_message`` appends a text message to ``chats[<eid>]``
    via REAL GUI gestures (open_app → deep-link → focus → type_text → ENTER →
    the app's own handleSend; NO ``set_state`` on the write path — the
    non-invasive write/rollback boundary, see memory
    taskvm-non-invasive-write-rollback-boundary + .mrules E7). The bridge
    returns ``old="msg:<id>"`` / ``new=<text>``.

    Rollback (Option C, honest irreversibility — NOT a set_state restore):
    MobileGym's wechat has NO delete/recall UI (no long-press handler, no
    deleteMessage store action, messages are append-only), so a real-gesture
    rollback of a sent message is NOT possible. When ``mutate(value="msg:<id>")``
    is called for compensation, the bridge HONESTLY raises ``HTTPConflict``
    (409) — it does NOT fall back to ``set_state`` to fake a byte-exact restore
    (that would undermine the compensation claim). ``undo_saga`` catches the 409
    → ``reverted=False``, ``partial_failure=True``; the verifier independently
    confirms the message is still there (fidelity=0.0). This is the honest-
    rollback reverse-example (E9.2/E9.3), NOT reversible compensation.

    ``read_canonical`` flattens wechat chats keyed by ``id`` (== the peer's
    wxid; cf. contacts which key on ``wxid`` directly)."""
    app = "wechat"
    resource = "wechat_chats"
    id_field = "id"
    entity_kind = "chat"

    def mutate(self, sid: str, entity_id: str, operator: str, value: Any) -> dict:
        if operator != "send_message":
            raise ValueError(f"wechat operator must be send_message, got {operator}")
        # Task3 (E10 rework): the bridge's mutate_wechat now runs gui_write_async
        # (a real grounding loop, ~30-90s for a multi-step model-driven send) instead
        # of the old hardcoded 7-step sequence. The default 10s timeout is far too
        # short — use 180s for the GUI write path. read_canonical uses the normal
        # timeout (it's a fast GET, not a GUI loop).
        gui_timeout = 180.0 if str(value).startswith("msg:") or not str(value).startswith("__fast__") else self.timeout
        r = requests.post(f"{self.base_url}/api/wechat/{sid}/{entity_id}",
                          json={"operator": operator, "value": value},
                          timeout=gui_timeout)
        r.raise_for_status()
        return r.json()


class AlipayAdapter(StateAdapter):
    """MobileGym demo app (read-only substrate for the Top3 task). The binding
    reads ``transferRecords`` (filter ``delta<0``, sort ``|delta|`` desc, top3)
    — this adapter only surfaces that collection via ``read_canonical``. There
    is no write path: the Top3 task never writes alipay. Served by the same
    bridge on :3019 as wechat."""
    app = "alipay"
    resource = "alipay_transactions"
    id_field = "id"
    entity_kind = "transaction"

    def mutate(self, sid: str, entity_id: str, operator: str, value: Any) -> dict:
        raise ValueError(f"alipay is read-only in the Top3 demo (no write path); "
                         f"got operator={operator}")


# ── registry / factories ────────────────────────────────────────────────────
_ADAPTER_CLASSES = {
    "calendar": CalendarAdapter,
    "taskboard": TaskBoardAdapter,
    "drive": DriveAdapter,
    "mail": MailAdapter,            # W4 held-out (truly unseen)
    "outlook_cal": OutlookCalAdapter,  # W4 held-out (calendar reskin)
    "wechat": WechatAdapter,        # MobileGym demo (bridge :3019)
    "alipay": AlipayAdapter,        # MobileGym demo (bridge :3019, read-only)
}

DEFAULT_PORTS = {"calendar": 3013, "taskboard": 3014, "drive": 3015,
                 "mail": 3017, "outlook_cal": 3018,
                 "wechat": 3019, "alipay": 3019}


def make_adapter(app: str, port: int | None = None, host: str = "localhost",
                 base_url: str | None = None, *,
                 executor: str = "api",
                 gui_screenshot_dir: str | None = None,
                 grounding_backend: str | None = None) -> StateAdapter:
    """Factory. ``base_url`` overrides host/port if given (e.g. docker service name).

    ``executor`` (E10 rework P2): ``'api'`` (default, legacy requests.post to the
    app's Flask API) or ``'gui_agent'`` (drive a real browser via the GUI
    executor — non-invasive write/rollback boundary, ``.mrules`` E7/E10). When
    ``gui_agent``, the adapter's ``use_gui_executor`` flag is set + a
    ``gui_screenshot_dir`` for step evidence. Read/seed/reset always use the
    Flask app (the read path is allowed to use the internal API — only the
    WRITE/ROLLBACK path must go through the browser).

    ``grounding_backend`` (EE.6): when ``executor='gui_agent'``, name the
    hot-swappable vision model the GUI executor uses ('gpt56sol' default,
    'glm5v', 'uitars'). The executor singleton is keyed by this name so a
    model-ablation run can swap backends. None → 'gpt56sol' (pre-EE.6 baseline).
    Resolved lazily in ``gui_executor.get_executor`` at first write."""
    if app not in _ADAPTER_CLASSES:
        raise ValueError(f"unknown app {app!r}; known: {list(_ADAPTER_CLASSES)}")
    if base_url is None:
        p = port or DEFAULT_PORTS[app]
        base_url = f"http://{host}:{p}"
    ad = _ADAPTER_CLASSES[app](base_url=base_url, app=app)
    if executor == "gui_agent":
        ad.use_gui_executor = True
        ad.gui_screenshot_dir = gui_screenshot_dir
        # EE.6: record the requested backend name so _mutate_via_gui's
        # get_executor() call builds the right backend. Stored on the adapter;
        # gui_executor reads it via the module-level get_executor(backend_name=).
        ad.grounding_backend = grounding_backend or "gpt56sol"
    elif executor != "api":
        raise ValueError(f"unknown executor {executor!r}; expected 'api' or 'gui_agent'")
    return ad


# apps that are always-on for the W1/W2 core kill-tests. Held-out apps
# (mail, outlook_cal) are W4-only — they do NOT auto-mount into a W1/W2 run
# (those apps are not running in that context). Request them explicitly via
# ``make_adapters(apps=[...])`` or ``make_adapters(include_heldout=True)``.
_CORE_APPS = ("calendar", "taskboard", "drive")
_HELDOUT_APPS = ("mail", "outlook_cal")
_MOBILEGYM_APPS = ("wechat", "alipay")   # MobileGym demo (bridge :3019)


def make_adapters(apps: list[str] | None = None, *, include_heldout: bool = False,
                  include_mobilegym: bool = False, executor: str = "api",
                  gui_screenshot_dir: str | None = None,
                  **kwargs) -> dict[str, StateAdapter]:
    """Build a {app_name: adapter} dict for the app set.

    Default = the W1/W2 core apps (calendar/taskboard/drive) — held-out apps are
    excluded unless ``include_heldout=True`` or an explicit ``apps`` list names
    them. This keeps W1/W2 kill-tests unchanged (they don't health-check apps
    that aren't running) while letting the W4 OOD recon opt in explicitly.
    MobileGym apps (wechat/alipay) require ``include_mobilegym=True`` or an
    explicit ``apps`` list — they need the bridge server running, so they must
    never silently enter a core kill-test's app set.

    ``executor`` (E10 rework P2): pass ``'gui_agent'`` to wire all built
    adapters through the GUI executor (real browser gestures). Default
    ``'api'`` preserves the legacy requests.post path for backward compat."""
    if apps is None:
        apps = list(_CORE_APPS)
        if include_heldout:
            apps += list(_HELDOUT_APPS)
        if include_mobilegym:
            apps += list(_MOBILEGYM_APPS)
    return {a: make_adapter(a, executor=executor,
                            gui_screenshot_dir=gui_screenshot_dir, **kwargs)
            for a in apps}
