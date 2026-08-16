"""gui_driver — the runtime's GUI-only operator-write composition (Agent B).

Where the legacy ``taskvm.substrate.base.StateAdapter`` write path lives
now. The substrate layer answers observe/act/capture ONLY; composing a
task-level operator write (``mutate(sid, entity_id, operator, value)`` —
the transitional cross-layer protocol until Agent E's semantic action
contract lands) out of gestures is an EXECUTION concern, so the
orchestration moved UP here:

  1. translate entity → visible locator (``anchor_lookup`` — injected by
     the composition root; the substrate itself never learns entity ids);
  2. build a deterministic instruction (taskvm.architect.serializer —
     visible-locator text, GG.3 + Agent-C: zero internal ids, zero model
     calls);
  3. drive the CUA grounding loop (``gui_executor``) through a
     SubstrateSession port;
  4. verify via the session's own visible observation (NOT an oracle read
     — the evaluation plane's ``oracle_state`` is verifier/benchmark
     only).

There is no API write path, no ``requests.post`` mutation, no fallback:
if the GUI loop cannot land the change, ``mutate`` raises honestly.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from taskvm.substrate import (
    GuiAction,
    SubstrateSession,
    substrate_registry,
)
# TRANSITIONAL DEBT (B-2, Oracle audit 2026-08-15) — this whole file is
# scheduled for DELETION by Agent E's runtime wave (ActionContract → CUA →
# GuiAction → SubstrateSession.act replaces GUITaskAdapter /
# MobileGymTaskAdapter / make_task_adapters and their platform tables).
# Until then the URL knowledge below imports the substrate IMPLEMENTATIONS
# directly (the substrate root facade exports Protocol/DTO/Registry only —
# hiding these imports behind fake port helpers was ruled worse than the
# honest debt). Registered as §6 violation T1 in the Transitional Debt
# Register (docs/contracts/substrate.md §8), mirrored in
# tests/substrate/test_no_api_backdoor.py's TRANSITIONAL_DEBT_REGISTER
# (shrink-only; formal LOCK audit fails while it is non-empty).
from taskvm.substrate.builtin_web.launcher import app_url as _builtin_app_url
from taskvm.substrate.mobilegym.evaluation import (
    DEFAULT_BRIDGE_PORT as _MG_DEFAULT_BRIDGE_PORT,
)

logger = logging.getLogger(__name__)

#: Visible-anchor lookup: (app, entity_id) -> visible title (or None).
#: Provided by the composition root. Until the State Compiler's
#: SurfaceHandle cache owns this (Agent C/E), evaluation/demo assemblies
#: derive it from the evaluation plane; the driver itself never touches
#: an oracle.
AnchorLookup = Callable[[str, str], str | None]


class GUITaskAdapter:
    """Operator-write driver over ONE builtin web app, GUI-only.

    Preserves the legacy ``mutate`` response contract
    (``{status, app, entity_id, operator, old, new, field, trace}``) so
    ``execution.action_dispatcher`` / ``execution.rollback`` /
    workspace_ui keep working unchanged."""

    def __init__(self, app: str, base_url: str,
                 screenshot_dir: str | None = None,
                 grounding_backend: str | None = None,
                 anchor_lookup: AnchorLookup | None = None):
        self.app = app
        self.base_url = base_url.rstrip("/")
        self.gui_screenshot_dir = screenshot_dir
        self.grounding_backend = grounding_backend or "gpt56sol"
        self.anchor_lookup = anchor_lookup

    # ── session cache (one port session per sid) ───────────────────────────
    _sessions: "dict[tuple[str, str], SubstrateSession]" = {}

    def _session_for(self, sid: str) -> SubstrateSession:
        key = (self.app, sid)
        if key not in self._sessions:
            self._sessions[key] = substrate_registry.create_session(
                "builtin_web",
                {"app": self.app, "base_url": self.base_url, "sid": sid,
                 "screenshot_dir": self.gui_screenshot_dir})
        return self._sessions[key]

    def read_canonical(self, sid: str):   # noqa: D401 — transitional name
        """TRANSITIONAL (registered blocker): some legacy callers still ask
        the task adapter for a canonical entity map. The GUI driver has no
        oracle; it honestly refuses. Callers needing ground truth must go
        through the EvaluationEnvironment (builtin_web.evaluation)."""
        raise RuntimeError(
            "GUITaskAdapter has no canonical read (evaluation-plane "
            "power). Use WebEvaluationEnvironment.oracle_state from the "
            "evaluation plane, or the session's visible observe().")

    def mutate(self, sid: str, entity_id: str, operator: str,
               value: Any, *, field: str | None = None,
               undo: bool = False) -> dict:
        """Apply one operator write via REAL GUI gestures (no API path).

        ``field``: the visible field the operator targets (legacy
        ``_OP_FIELD`` tables moved to the app registry below)."""
        from taskvm.execution.gui_executor import gui_write, GuiExecutorFailure  # noqa: F401
        op_field = _OP_FIELD.get(self.app, {}).get(operator)
        if field is None:
            field = op_field
        if field is None:
            raise ValueError(
                f"unknown operator {operator!r} for app {self.app!r}; "
                f"known: {list(_OP_FIELD.get(self.app, {}))}")
        entity_kind = _ENTITY_KIND.get(self.app, self.app)
        # visible anchor (title) — injected lookup; never an oracle read
        # inside the driver
        title = None
        if self.anchor_lookup is not None:
            try:
                title = self.anchor_lookup(self.app, entity_id)
            except Exception as e:      # lookup failure → honest degrade
                logger.warning("[gui_driver] anchor lookup failed: %s", e)
        session = self._session_for(sid)
        return gui_write(
            app=self.app, sid=sid, entity_id=entity_id,
            operator=operator, value=value, field=field,
            entity_kind=entity_kind, session=session,
            base_url=self.base_url, old_value=None,
            screenshot_dir=self.gui_screenshot_dir, undo=undo,
            backend_name=self.grounding_backend,
            visible_anchor_title=title)


class MobileGymTaskAdapter:
    """Operator-write driver for the MobileGym bridge apps (wechat/x).

    The bridge's operator routes wrap a REAL grounding loop inside the
    bridge process (gui_write_async / gui_act_async — injected there via
    ``--cua-loop`` at process assembly). This adapter is the HTTP client
    over those routes; the gestures happen inside the bridge against the
    live sim. Read-only for alipay (no write path — honest)."""

    def __init__(self, app: str, bridge_url: str, timeout: float = 180.0):
        self.app = app
        self.base_url = bridge_url.rstrip("/")
        self.timeout = timeout

    def _post(self, sid: str, entity_id: str, operator: str, value: Any,
              payload_extra: dict | None = None):
        import requests
        payload = {"operator": operator, "value": value}
        if payload_extra:
            payload.update(payload_extra)
        r = requests.post(f"{self.base_url}/api/{self.app}/{sid}/{entity_id}",
                          json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def mutate_raw(self, sid: str, entity_id: str, operator: str,
                   value: Any, payload_extra: dict | None = None
                   ) -> tuple[int, "dict | str"]:
        """Non-raising variant of the bridge write for EVALUATION scripts:
        returns ``(http_status, parsed_json_or_text)`` so evaluation scripts can record
        honest failure bodies (409 irreversible, 500 loop-exhausted…) instead
        of catching exceptions. Same bridge route as ``mutate`` — the bridge's
        operator routes wrap the injected CUA grounding loop (real gestures
        against the live sim), so this IS the GUI write path, transported over
        the bridge's own HTTP protocol."""
        import requests
        payload = {"operator": operator, "value": value}
        if payload_extra:
            payload.update(payload_extra)
        r = requests.post(f"{self.base_url}/api/{self.app}/{sid}/{entity_id}",
                          json=payload, timeout=self.timeout)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text

    def mutate(self, sid: str, entity_id: str, operator: str,
               value: Any, **kwargs) -> dict:
        if self.app == "alipay":
            raise ValueError(
                "alipay is read-only in the Top3 demo (no write path); "
                f"got operator={operator}")
        if self.app == "wechat":
            if operator != "send_message":
                raise ValueError(
                    f"wechat operator must be send_message, got {operator}")
            return self._post(sid, entity_id, operator, value)
        if self.app == "x":
            if operator not in ("toggle_like", "toggle_retweet",
                                "toggle_bookmark"):
                raise ValueError(
                    f"x operator must be toggle_like/retweet/bookmark, "
                    f"got {operator}")
            return self._post(sid, entity_id, operator, value,
                              payload_extra=kwargs.get("payload_extra"))
        raise ValueError(f"unknown mobilegym app {self.app!r}")

    def read_canonical(self, sid: str):
        raise RuntimeError(
            "MobileGymTaskAdapter has no canonical read (evaluation-plane "
            "power); use MobileGymEvaluationEnvironment.oracle_state.")


# ── operator tables (migrated from legacy substrate/base.py) ────────────────

_OP_FIELD: dict[str, dict[str, str]] = {
    "calendar": {"move_event": "date", "update_rsvp": "rsvp"},
    "taskboard": {"set_deadline": "deadline", "set_status": "status",
                  "set_assignee": "assignee"},
    "drive": {"move_file": "parent", "rename": "name", "set_owner": "owner",
              "set_publish_date": "publish_date"},
    "mail": {"set_state": "state", "set_priority": "priority",
             "set_to": "to_addr", "set_send_date": "send_date"},
    "outlook_cal": {"reschedule_appointment": "scheduled_for"},
}

_ENTITY_KIND: dict[str, str] = {
    "calendar": "event", "taskboard": "task", "drive": "file",
    "mail": "message", "outlook_cal": "appointment",
}

_WEB_APPS = ("calendar", "taskboard", "drive", "mail", "outlook_cal")
_MOBILEGYM_APPS = ("wechat", "alipay", "x")


def mobilegym_bridge_url(host: str = "localhost") -> str:
    """TRANSITIONAL (B-2): bridge URL for the legacy task adapters. Lives
    HERE (not in the substrate root facade) because this file is Agent E's
    deletion target; the env override mirrors the mobilegym provider's
    config precedence. Deleted together with make_task_adapters."""
    import os
    env = os.environ.get("TASKVM_MOBILEGYM_PORT")
    port = int(env) if env and env.isdigit() else _MG_DEFAULT_BRIDGE_PORT
    return f"http://{host}:{port}"

def make_task_adapters(apps: list[str] | None = None, *,
                       host: str = "localhost",
                       screenshot_dir: str | None = None,
                       grounding_backend: str | None = None,
                       anchor_lookup: AnchorLookup | None = None,
                       mobilegym_url: str | None = None,
                       **kwargs) -> dict:
    """GUI-only successor of the legacy ``make_adapters``. There is no
    ``executor`` parameter: the API write path is deleted from the runtime
    (task brief §一). Every adapter writes through real GUI gestures."""
    if apps is None:
        apps = list(_WEB_APPS)
    out: dict[str, Any] = {}
    for a in apps:
        if a in _WEB_APPS:
            out[a] = GUITaskAdapter(
                app=a, base_url=_builtin_app_url(a, host=host),
                screenshot_dir=screenshot_dir,
                grounding_backend=grounding_backend,
                anchor_lookup=anchor_lookup)
        elif a in _MOBILEGYM_APPS:
            url = mobilegym_url or mobilegym_bridge_url(host)
            out[a] = MobileGymTaskAdapter(app=a, bridge_url=url)
        else:
            raise ValueError(f"unknown app {a!r}; known: web={_WEB_APPS} "
                             f"mobilegym={_MOBILEGYM_APPS}")
    return out
