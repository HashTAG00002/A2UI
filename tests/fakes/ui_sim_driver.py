"""``UISimDriver`` — the GenUI-surface-driven L4 implementation (FF.2 §3).

Unlike ``ScriptedUserDriver`` (which emits ``edit_field`` directly, bypassing
the GenUI surface), the UISimDriver REALLY goes through the rendered GenUI
HTML: it GETs the page, parses the editable ``<form>`` controls with
BeautifulSoup, finds the form bound to the target ``var_id``, POSTs the edit
through the server's ``/<sid>/edit`` route (exactly as a human would by filling
the form), reads the ``changed_vars`` from the JSON response (FF.1), and ONLY
THEN emits a ``UserBehaviorEvent(edit_field, {..., from_ui: True})``.

This proves the load-bearing bidirectional claim (handoff §1.2 / VM property 2):
the GenUI-rendered surface's form controls are correctly wired to the binding
→ a submit reaches ``compile_patch`` → ``dispatch`` → the real app write path
→ the verifier sees the change. ``from_ui=True`` tags the event as one that
really traversed the UI (vs. ``ScriptedUserDriver``'s direct intent).

Transport: the driver is HTTP-shaped (GET /<sid>, POST /<sid>/edit) but
duck-typed on ``client.get(path)`` / ``client.post(path, data=...)`` so it works
with a Flask ``test_client`` (in-process, fast — the test path) OR a real
``requests.Session`` against a live server (swap the transport, same driver).
"""
from __future__ import annotations

import logging
from typing import Any

from taskvm.benchmark.fixtures import CanonicalTaskGraph
from tests.fakes.user_behavior_driver import (UserBehaviorDriver,
                                                     UserBehaviorEvent)
from taskvm.governance.vm_state import VMStateSnapshot

logger = logging.getLogger(__name__)


def _parse_edit_forms(html: str) -> dict[str, dict[str, Any]]:
    """Parse every ``<form>`` that posts to ``/<sid>/edit`` and extract its
    ``var_id`` (hidden input) + whether it has a ``new_value`` input + its
    action. Returns ``{var_id: {...}}``. Tolerates both the f-string
    ``rw-field`` forms (``action="edit"``) and the GenUI decoder's
    ``genui-field`` forms (``action="/<sid>/edit"``) — both carry the same
    ``name="var_id"`` + ``name="new_value"`` contract (EE.7)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:   # pragma: no cover — bs4 is a hard dep for FF.2
        raise RuntimeError("UISimDriver needs beautifulsoup4 (bs4)") from e
    soup = BeautifulSoup(html or "", "html.parser")
    forms: dict[str, dict[str, Any]] = {}
    for form in soup.find_all("form"):
        action = form.get("action", "") or ""
        if "edit" not in action:
            continue
        vid_in = form.find("input", {"name": "var_id"})
        if vid_in is None:
            continue
        vid = vid_in.get("value", "") or ""
        val_in = form.find("input", {"name": "new_value"})
        forms[vid] = {
            "var_id": vid,
            "has_value_input": val_in is not None,
            "action": action,
        }
    return forms


class UISimDriver(UserBehaviorDriver):
    """L4 driver that drives the governance loop THROUGH the GenUI surface.

    Construct with a task (for the target ``user_edit`` var_id + new_value), an
    HTTP-shaped ``client`` (Flask test_client or requests.Session), and the
    seeded ``sid``. ``next_event`` does one edit round (GET → parse → POST →
    read changed_vars → emit event) then returns None on the next call.
    """

    def __init__(self, task: CanonicalTaskGraph, client: Any, sid: str) -> None:
        self.task = task
        self.client = client
        self.sid = sid
        self._done = False
        self.last_response: dict[str, Any] = {
            "ui_parse_ok": False, "form_submit_ok": False,
            "found_var_ids": [], "target_var_id": None,
            "changed_vars": [], "n_ops": 0, "n_applied": 0,
            "http_status_edit": None, "error": None,
        }

    # ── UserBehaviorDriver interface ──────────────────────────────────────
    def next_event(self) -> UserBehaviorEvent | None:
        if self._done:
            return None
        self._done = True
        target_vid = self.task.user_edit.get("var_id", "")
        new_value = str(self.task.user_edit.get("new", ""))
        self.last_response["target_var_id"] = target_vid
        try:
            ev = self._do_edit_round(target_vid, new_value)
            return ev
        except Exception as e:   # never let a transport error crash the loop
            self.last_response["error"] = f"{type(e).__name__}: {e}"
            logger.warning("[UISimDriver] edit round failed: %s", e)
            return None

    def on_state_update(self, vm_state: VMStateSnapshot) -> None:
        # the UI-sim driver does not react to mid-loop state (one edit round)
        pass

    # ── the edit round: GET → parse → POST → read changed_vars ───────────
    def _do_edit_round(self, target_vid: str, new_value: str) -> UserBehaviorEvent:
        # 1. GET /<sid> → the rendered GenUI HTML
        r_get = self.client.get(f"/{self.sid}")
        html = r_get.get_data(as_text=True) if hasattr(r_get, "get_data") \
            else getattr(r_get, "text", "")
        # 2. parse the editable forms
        forms = _parse_edit_forms(html)
        found_var_ids = list(forms.keys())
        self.last_response["found_var_ids"] = found_var_ids
        ui_parse_ok = target_vid in forms
        self.last_response["ui_parse_ok"] = ui_parse_ok
        # 3-4. POST /<sid>/edit {var_id, new_value, format=json} — the server
        # compiles the patch + dispatches (via sess.adapters, executor=api or
        # gui_agent) + returns changed_vars (FF.1). format=json so we read the
        # structured response (the HTML form flow would 302-redirect instead).
        r_post = self.client.post(f"/{self.sid}/edit", data={
            "var_id": target_vid, "new_value": new_value, "format": "json"})
        self.last_response["http_status_edit"] = getattr(r_post, "status_code", None)
        data = {}
        try:
            data = r_post.get_json() or {}
        except Exception:
            data = {}
        changed_vars = data.get("changed_vars", []) or []
        n_ops = int(data.get("n_ops", 0))
        n_applied = int(data.get("n_applied", 0))
        self.last_response["changed_vars"] = changed_vars
        self.last_response["n_ops"] = n_ops
        self.last_response["n_applied"] = n_applied
        self.last_response["form_submit_ok"] = bool(data.get("ok")) and bool(changed_vars)
        # 6. emit a UserBehaviorEvent marking this came through the UI surface
        return UserBehaviorEvent("edit_field", {
            "var_id": target_vid, "new_value": new_value, "from_ui": True,
            "ui_parse_ok": ui_parse_ok,
            "changed_vars": changed_vars,
        })
