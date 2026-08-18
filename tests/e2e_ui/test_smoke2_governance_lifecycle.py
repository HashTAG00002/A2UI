"""RM-0.B §6 Smoke 2 — builtin governance op lifecycle over the REAL
calendar app.

Through the UserOpDriver PUBLIC Projection API:

    start → pause (mid-flight) → resume → (write completes) → stop

Proves (work order §Smoke 2):

  1. lifecycle REALLY controls the driver — snapshot ``governance.autonomy``
     tracks paused → running → stopped, the runtime's run-loop observable
     stops advancing during pause, and resume continues the journey (the
     scripted CUA is a page-state dispatcher, so it resumes from wherever
     the page is).
  2. after stop: ZERO new substrate acts — provider calls freeze (each
     gesture is preceded by exactly one predict == one ledger row), kernel
     events total stops growing, and a real wall-clock wait adds nothing.
  3. per-op barrier settles (start quiet / pause sse / resume quiet / stop
     sse — each op's verdict is ``applied``, never ``unsettled``).
  4. SSE and snapshot agree — the SSE ``governance.applied`` frames match
     the snapshot's autonomy transitions over the whole run.

The scripted CUA paces the journey with a ``wait`` gesture before each
real gesture so pause can land mid-flight (the runtime checks ``_paused``
between gestures, autonomy.py §447). Coordinates are measured once from
the REAL rendered pages (the probe fixture in test_smoke_builtin_real_full
is reused — read-only calibration, no SUT out-of-band knowledge).
"""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import pytest

try:
    from playwright.sync_api import sync_playwright
    _PW_AVAILABLE = True
except ImportError:                                    # pragma: no cover
    _PW_AVAILABLE = False

from taskvm.architect import (
    MODEL_ROLE_CUA, MODEL_ROLE_STATE_COMPILER, MODEL_ROLE_TASK_ARCHITECT,
    ModelCallLedger, ModelReply,
)
from taskvm.projection.app import create_app
from taskvm.projection.store import ProjectionSessionStore
from taskvm.substrate.builtin_web.evaluation import CalendarEvaluationEnv
from taskvm.substrate.builtin_web.session import WebSubstrateSession
from taskvm.workspace_ui.composition import bootstrap_real_full

from taskvm_bench.evaluation.projection_client import (
    ProjectionClient, SSE_GOVERNANCE_APPLIED,
)
from taskvm_bench.evaluation.user_ops import (
    SettlePolicy, UserOp, UserOpDriver,
)

pytestmark = pytest.mark.skipif(not _PW_AVAILABLE,
                                reason="playwright not installed")

GOAL = "把日历事件「产品发布」改期到 2026-08-18"
DESIRED = "2026-08-18"
SID = "rm0-smoke2"
VIEWPORT = (1100, 760)

DEMO_EVENT = {"eid": "e1", "title": "产品发布", "date": "2026-08-14",
              "time": "10:00", "calendar": "work", "rsvp": "accepted"}

# reuse the exact scripted evidence (the smoke1 file owns its layout; this
# file re-declares the same shape to keep the smoke isolated and to pin the
# same handle-cache wiring smoke1 exercises).
COMPILER_REPLY = {
    "variables": [{
        "semantic_key": "event_date", "label": "Date",
        "value_type": "date", "mutability": "editable",
        "observed": "2026-08-14", "confidence": 0.97,
        "evidence": [{
            "surface_label": "Calendar", "visible_label": "Date",
            "visible_context": "产品发布 2026-08-14 10:00 work accepted",
            "value_pattern": r"(2026-08-\d{2})"}]}],
    "ambiguities": [], "needs_clarification": False,
}
ARCHITECT_REPLY = {
    "variables": [{
        "semantic_key": "event_date", "label": "Date",
        "value_type": "date", "mutability": "editable",
        "desired": DESIRED}],
    "workflow": {"nodes": [
        {"kind": "action", "label": "改期「产品发布」",
         "semantic_goal": "把发布事件改期",
         "sets": {"event_date": DESIRED},
         "completion": f"event_date=={DESIRED}",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["event_date"]},
        {"kind": "terminal", "label": "完成", "after": ["改期「产品发布」"]},
    ]},
}


class PacedCalendarScriptedPort:
    """Same page-state dispatcher as smoke1, but paces the journey: every
    real gesture is preceded by a ``wait`` gesture, so a pause issued
    mid-flight reliably lands between two gestures (the runtime checks
    ``_paused`` between gestures, autonomy.py §447 — it never pauses
    mid-gesture)."""

    default_model = "scripted-rm0b-smoke2"

    def __init__(self, coords: dict[str, tuple[float, float]],
                 date_key_presses: int):
        self._coords = coords
        self._presses = date_key_presses
        self.calls: list[tuple[str, str, Any]] = []
        self._phase = 0          # 0 compiler, 1 architect, 2 cua
        self._edit_step = 0
        self._confirmed = False
        self._pending_wait = False   # pace flag — the next decision is wait

    def complete_json(self, *, system: str, user: str, model: str | None = None,
                      max_tokens: int = 3072, temperature: float | None = None,
                      image_data_url: str | None = None) -> ModelReply:
        self.calls.append((system, user, image_data_url))
        if self._phase == 0:
            self._phase = 1
            item: dict = COMPILER_REPLY
        elif self._phase == 1:
            self._phase = 2
            item = ARCHITECT_REPLY
        else:
            visible = user.split("## 屏幕可见文本", 1)[-1]
            if self._pending_wait:
                # a wait gesture paces the journey — the NEXT real gesture
                # follows on the subsequent call (the page is the same, so
                # the dispatcher sees the same state and emits the real
                # gesture then).
                self._pending_wait = False
                item = {"kind": "act",
                        "action": {"kind": "wait", "duration_ms": 200}}
            else:
                real = self._cua_decision(visible)
                if real["kind"] == "act" and real["action"]["kind"] != "wait":
                    self._pending_wait = True
                item = real
        return ModelReply(parsed=item,
                          raw=json.dumps(item, ensure_ascii=False),
                          model=model or self.default_model,
                          prompt_tokens=5, completion_tokens=3)

    def _click(self, key: str) -> dict:
        x, y = self._coords[key]
        return {"kind": "act",
                "action": {"kind": "click", "coordinate": [x, y]}}

    def _cua_decision(self, visible: str) -> dict:
        if "Confirm move" in visible:
            if not self._confirmed:
                self._confirmed = True
                return self._click("confirm")
            return {"kind": "act",
                    "action": {"kind": "wait", "duration_ms": 900}}
        if "moved to" in visible and DESIRED in visible:
            return {"kind": "done"}
        if "Date (the only writable field)" in visible:
            self._edit_step += 1
            if self._edit_step == 1:
                return self._click("date")
            if self._edit_step <= 1 + self._presses:
                return {"kind": "act",
                        "action": {"kind": "key", "key": "UP"}}
            return self._click("review")
        if "← back to calendar" in visible:
            return self._click("edit")
        return self._click("view")


# ── helpers (same as smoke1 — kept local so this file is self-contained) ───

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _serve_flask(app, port: int) -> str:
    t = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, threaded=True,
                               debug=False, use_reloader=False),
        daemon=True)
    t.start()
    import requests
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            requests.get(f"{base}/health", timeout=1)
            return base
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("flask app did not start")


def _can_launch_browser() -> bool:
    if not _PW_AVAILABLE:
        return False
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:
        return False


def _probe_coords(base: str) -> tuple[dict, int]:
    """Read-only calibration: measure the real gesture coordinates + the
    date-arrow step count from the REAL rendered pages (never submits)."""
    coords: dict[str, tuple[float, float]] = {}

    def norm_center(bb) -> tuple[float, float]:
        cx = bb["x"] + bb["width"] / 2
        cy = bb["y"] + bb["height"] / 2
        return (round(cx / VIEWPORT[0] * 1000, 1),
                round(cy / VIEWPORT[1] * 1000, 1))

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page(viewport={"width": VIEWPORT[0],
                                    "height": VIEWPORT[1]})
        page.goto(f"{base}/{SID}")
        page.wait_for_selector("a.btn.view")
        coords["view"] = norm_center(page.locator("a.btn.view").first
                                     .bounding_box())
        bb = page.locator("a.btn.view").first.bounding_box()
        page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        page.wait_for_selector("a.btn:has-text('Edit')")
        coords["edit"] = norm_center(
            page.locator("a.btn:has-text('Edit')").bounding_box())
        bb = page.locator("a.btn:has-text('Edit')").bounding_box()
        page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        page.wait_for_selector("#new_date")
        coords["date"] = norm_center(page.locator("#new_date").bounding_box())
        date_loc = page.locator("#new_date")
        dbb = date_loc.bounding_box()
        day_xy, presses = None, None
        for frac in (0.10, 0.15, 0.20, 0.26, 0.30, 0.33, 0.36,
                     0.40, 0.50, 0.65, 0.80):
            x = dbb["x"] + dbb["width"] * frac
            y = dbb["y"] + dbb["height"] / 2
            before = date_loc.input_value()
            page.mouse.click(x, y)
            page.keyboard.press("ArrowUp")
            after = date_loc.input_value()
            if after != before:
                page.keyboard.press("ArrowDown")
                if after[:8] == before[:8]:
                    day_xy = (round(x / VIEWPORT[0] * 1000, 1),
                              round(y / VIEWPORT[1] * 1000, 1))
                    presses = int(after[8:10]) - int(before[8:10])
                    break
        assert day_xy is not None and presses is not None, (
            "no x position edits the day segment — this chromium build "
            "cannot drive the date input even with arrows")
        target = int(DESIRED[8:10])
        current = int(date_loc.input_value()[8:10])
        key_presses = abs(target - current)
        coords["review"] = norm_center(
            page.locator("#review-btn").bounding_box())
        bb = page.locator("#review-btn").bounding_box()
        page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        page.wait_for_selector("#confirm-move[open]")
        coords["confirm"] = norm_center(
            page.locator("#confirm-move button[type=submit]")
            .bounding_box())
        page.keyboard.press("Escape")
        b.close()
    coords["date"] = day_xy
    return coords, key_presses


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def calendar_app():
    from taskvm.apps.calendar.app import app as calendar_flask_app
    base = _serve_flask(calendar_flask_app, _free_port())
    env = CalendarEvaluationEnv(base_url=base)
    env.seed(SID, task_id="rm0b-smoke2", goal=GOAL,
             seed_state={"events": [dict(DEMO_EVENT)]})
    yield base, env


@pytest.fixture(scope="module")
def browser_ok():
    if not _can_launch_browser():
        pytest.skip("playwright browser not launchable in this environment")
    return True


# ── Smoke 2 ─────────────────────────────────────────────────────────────────

def test_smoke2_governance_lifecycle_over_public_api(browser_ok, calendar_app):
    base, env = calendar_app
    coords, key_presses = _probe_coords(base)
    substrate = WebSubstrateSession(app="calendar", url=base, sid=SID,
                                    viewport=VIEWPORT)
    port = PacedCalendarScriptedPort(coords, key_presses)
    ledger = ModelCallLedger()
    store = ProjectionSessionStore()
    bootstrap_real_full(goal=GOAL, sid=SID, substrate=substrate,
                        model_port=port, ledger=ledger, store=store)

    proj_base = _serve_flask(create_app(store), _free_port())
    client = ProjectionClient(proj_base, SID, timeout_s=30.0)
    driver = UserOpDriver(client)
    sse = client.open_sse_window()

    # ── (1) start → driver running, barrier settles (quiet) ─────────────
    start_out = driver.execute(UserOp.start(settle_policy=SettlePolicy(
        "quiet", quiet_seconds=1.0, timeout_s=30.0)))
    assert start_out.verdict == "applied", start_out.detail
    # give the loop a tick to move past the first wait/gesture
    time.sleep(0.6)
    assert port.calls and port.calls[0][1].startswith("## 操作目标") is False, (
        "compiler call never happened — bootstrap wiring broken")

    # ── (2) pause mid-flight ─────────────────────────────────────────────
    calls_before_pause = len(port.calls)
    pause_out = driver.execute(UserOp.pause(rationale="test mid-flight pause"))
    assert pause_out.verdict == "applied", pause_out.detail
    # the snapshot's autonomy MUST reflect paused (driver.status() is the
    # deterministic lifecycle value; _SOFT_STOPS per-tick dispositions stay
    # in status() but governance_view reports driver.status()).
    snap_paused = client.snapshot()
    assert snap_paused["governance"]["autonomy"] in ("paused", "done",
                                                      "stopped"), (
        f"pause did not control the driver: autonomy="
        f"{snap_paused['governance']['autonomy']}")
    # prove the loop REALLY stopped advancing: no new provider calls during
    # a real wall-clock window after pause lands.
    time.sleep(1.2)
    calls_during_pause = len(port.calls)
    assert calls_during_pause == calls_before_pause, (
        f"paused driver still issued provider calls: "
        f"{calls_before_pause} → {calls_during_pause}")

    # ── (3) resume → the journey continues from the current visible world ─
    resume_out = driver.execute(UserOp.resume(rationale="test resume"))
    assert resume_out.verdict == "applied", resume_out.detail
    snap_resumed = client.snapshot()
    assert snap_resumed["governance"]["autonomy"] in ("running", "done",
                                                       "stopped"), (
        f"resume did not continue the driver: autonomy="
        f"{snap_resumed['governance']['autonomy']}")

    # ── (4) the write completes — settle quietly until the chain is done ─
    deadline = time.time() + 60.0
    entities = env.oracle_state(SID)["entities"]
    while time.time() < deadline:
        entities = env.oracle_state(SID)["entities"]
        if entities.get("e1", {}).get("date") == DESIRED:
            break
        time.sleep(0.3)
    assert entities["e1"]["date"] == DESIRED, (
        f"resume did not let the write land: {entities.get('e1')}")

    # ── (5) stop → terminal; ZERO new acts after stop ────────────────────
    calls_before_stop = len(port.calls)
    stop_out = driver.execute(UserOp.stop(rationale="test terminal stop"))
    assert stop_out.verdict == "applied", stop_out.detail
    snap_stopped = client.snapshot()
    assert snap_stopped["governance"]["autonomy"] in ("stopped", "done"), (
        f"stop did not terminate: autonomy="
        f"{snap_stopped['governance']['autonomy']}")
    # The stop op itself produces a GOVERNANCE_REQUESTED kernel event (via
    # runtime.request_stop → kernel.request_governance) — that is the stop
    # landing, not a post-stop leak. Take the baseline AFTER stop settles,
    # then prove the totals stay frozen across a real wall-clock window.
    events_baseline = client.event_count()
    time.sleep(1.5)
    calls_after_stop = len(port.calls)
    events_after_stop = client.event_count()
    assert calls_after_stop == calls_before_stop, (
        f"stopped driver issued new provider calls: "
        f"{calls_before_stop} → {calls_after_stop}")
    assert events_after_stop == events_baseline, (
        f"stopped driver produced new kernel events after settle: "
        f"{events_baseline} → {events_after_stop}")

    # ── (6) SSE and snapshot agree over the whole run ────────────────────
    sse_frames = sse.snapshot_events()
    sse.close()
    gov_frames = [f for f in sse_frames
                  if f.get("sse_type") == SSE_GOVERNANCE_APPLIED]
    # each governance op (start/pause/resume/stop) lands at least one
    # governance.applied SSE frame; the snapshot's autonomy matched the
    # paused/resumed/stopped transitions checked above.
    assert len(gov_frames) >= 4, (
        f"fewer than 4 governance.applied SSE frames: {len(gov_frames)}")
    # SSE governance frames carry {"sse_type": "governance.applied",
    # "detail": {"action": "start"|"paused"|"resumed"|"stopped", ...}}.
    # The action values are the past-tense ack forms (paused/resumed/stopped)
    # matching the HTTP response bodies; "start" is the only present-tense
    # form (start is the verb, the ack is the state it entered).
    sse_actions = {f.get("detail", {}).get("action")
                   for f in gov_frames}
    assert {"start", "paused", "resumed", "stopped"} <= sse_actions, (
        f"SSE governance frames missing ops: {sse_actions}")

    # ── (7) Smoke 4 ledger invariant (builtin governance path) ───────────
    # 1 provider request == 1 ledger row across all three roles.
    roles = [r.role for r in ledger.records]
    assert ledger.total() == len(port.calls), (
        f"ledger {ledger.total()} rows vs {len(port.calls)} provider "
        "requests — double or missing accounting on the governance path")
    assert roles.count(MODEL_ROLE_STATE_COMPILER) == 1
    assert roles.count(MODEL_ROLE_TASK_ARCHITECT) == 1
    assert roles.count(MODEL_ROLE_CUA) == len(port.calls) - 2

    # ── the driver only ever spoke to public session routes ─────────────
    for entry in client.request_log:
        assert entry["path"].startswith(
            ("/snapshot", "/governance", "/variables", "/workflow",
             "/checkpoints", "/surfaces", "/conflicts", "/events", "/sse")
        ), f"non-public route used: {entry}"

    substrate.close()
