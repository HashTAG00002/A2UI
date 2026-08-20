"""A7 motion e2e — the island's motion layer in a REAL browser, driven
by REAL server truth (no mock timelines anywhere).

Every frame the island consumes travels the REAL pipes:

* §20.1 progress stages via ``transport.push_stage`` — the same signals
  the APP shell's app_open wiring would push as the kernel's DAG view
  updates (the A5 fixture discipline, hand-driven because the hand-built
  kernel has no runtime);
* governance landings via ``transport.push_governance`` — the FROZEN A7
  island contract (``GOVERNANCE_SSE_KINDS``: labels only, never ids),
* the A6 IntentConsole POSTs against the REAL intent endpoint, whose
  honest 501 (no parser wired in this fixture) must surface verbatim.

Acceptance locks (MASTER_HANDOVER L167, item by item):

1. verified snake progress — the trajectory only crosses verified
   milestones; an executing node pokes the head WITHOUT crossing;
2. checkpoint small reward — ``checkpoint_reached`` fires ONE small
   confetti burst (a canvas appears) + the chip flips reached; NO
   final banner;
3. final celebration gate — ONLY ``final_pass`` renders the 🎉 banner +
   the full confetti; ``final_fail`` / ``node_failed`` fire NOTHING
   (negative case locked in the same real browser);
4. rollback reverse playback — the trajectory plays BACKWARDS to the
   target checkpoint node, then settles on the server's rolled-back
   statuses;
5. pause honesty — while paused the trajectory freezes while server
   truth keeps arriving; resume catches up;
6. prefers-reduced-motion — a reduced-motion browser context gets NO
   confetti at all and instant state switches (the contrast pair with
   test 3's full celebration is the A7 comparison evidence);
7. the IntentConsole's honest failure surface against the real 501.

Screenshots + raw DOM evidence land in eval_results/a7_motion_20260820/.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import types

import pytest

try:
    from playwright.sync_api import sync_playwright
    _PW_AVAILABLE = True
except ImportError:
    _PW_AVAILABLE = False

from taskvm.domain import (
    ActionContract, NodeKind, TaskIntent, TaskVariable,
    WorkflowGraph, WorkflowNode,
)
from taskvm.kernel import TaskVMKernel
from taskvm.projection.store import ProjectionSessionStore
from taskvm.workspace_ui import serve
from taskvm.workspace_ui.a2ui_transport import (
    A2uiTransport, register_a2ui_routes,
)

pytestmark = pytest.mark.skipif(not _PW_AVAILABLE,
                                reason="playwright not installed")

_SHOT_DIR = os.path.join("eval_results", "a7_motion_20260820")

GOAL_TEXT = "把发布会日期改到 8 月底并通知所有参会人"

#: the A7 DAG the hand-driven t2 payloads carry (label, kind) — the
#: shapes the kernel's workflow_view projects, statuses hand-advanced
#: exactly like the runtime would report them.
A7_NODES = [
    ("修改发布日期", "step"),
    ("校验日期合法", "verification"),
    ("日期确认点", "checkpoint"),
    ("通知参会人", "step"),
    ("校验通知名单", "verification"),
    ("更新预算表", "step"),
    ("终验", "verification"),
]
ALL_VERIFIED = ("verified",) * len(A7_NODES)


def _t2(*statuses: str) -> dict:
    """A §20.1 t2 payload — the kernel DAG chips with hand-advanced
    statuses (the same frames app_open pushes as the run progresses)."""
    return {"nodes": [
        {"label": label, "kind": kind,
         "status": statuses[i] if i < len(statuses) else "waiting"}
        for i, (label, kind) in enumerate(A7_NODES)
    ]}


def _make_kernel(sid: str) -> TaskVMKernel:
    intent = TaskIntent(goal=GOAL_TEXT)
    kernel = TaskVMKernel(sid, intent)
    kernel.init_task_state([
        TaskVariable(semantic_key="release_date", label="发布日期",
                     observed="2026-08-01", desired="2026-08-30",
                     value_type="date"),
    ])
    kernel.set_plan(WorkflowGraph(nodes=(
        WorkflowNode(node_id="seq1", kind=NodeKind.SEQUENCE, label="发布流程"),
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="修改发布日期",
                     parent_id="seq1",
                     contract=ActionContract(
                         contract_id="c1", semantic_goal="set release_date",
                         desired_state={"release_date": "2026-08-30"},
                         completion_condition="release_date shows 2026-08-30")),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a1",)),
    )))
    return kernel


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def island():
    """The REAL island stack (stock projection app + static island +
    A2UI transport), module-scoped; each test gets a PRISTINE session
    (fresh sid → fresh progress/governance rings → no cross-test
    replay)."""
    store = ProjectionSessionStore()
    transport = A2uiTransport(session_lookup=store.get)
    state = types.SimpleNamespace(sid="boot")
    app = serve(store)
    register_a2ui_routes(app, transport, store, state)

    port = _free_port()

    def _run():
        app.run(host="127.0.0.1", port=port, threaded=True,
                debug=False, use_reloader=False)

    threading.Thread(target=_run, daemon=True).start()
    import requests
    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            requests.get(base, timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        pytest.fail("island server did not start")
    yield types.SimpleNamespace(base=base, store=store,
                                transport=transport, state=state,
                                counter=iter(range(1, 10_000)))


@pytest.fixture()
def session(island):
    """A pristine per-test session: register → compile stages → attach
    (surface minted + ready pushed) — the honest cold-start sequence."""
    sid = f"a7-{next(island.counter)}"
    kernel = _make_kernel(sid)
    island.store.register(sid, kernel)
    island.state.sid = sid
    island.transport.push_stage(sid, "goal", {"goal": GOAL_TEXT})
    island.transport.push_stage(sid, "t1", {"variables": [
        {"label": "发布日期"}, {"label": "通知名单"}, {"label": "预算"},
    ]})
    island.transport.push_stage(sid, "t2", _t2())
    island.transport.attach_session(sid, island.store.get(sid))
    return types.SimpleNamespace(sid=sid, kernel=kernel)


def _can_launch_browser():
    if not _PW_AVAILABLE:
        return False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def browser():
    if not _can_launch_browser():
        pytest.skip("playwright browser not installed")
    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=True)
    yield b
    b.close()
    pw.stop()


def _shot(page, name: str) -> None:
    os.makedirs(_SHOT_DIR, exist_ok=True)
    page.screenshot(path=os.path.join(_SHOT_DIR, name), full_page=True)


def _dump_evidence(name: str, payload: dict) -> None:
    os.makedirs(_SHOT_DIR, exist_ok=True)
    path = os.path.join(_SHOT_DIR, name)
    existing = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
    existing.update(payload)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def _attr(page, selector: str, attr: str) -> str:
    return page.get_attribute(selector, attr) or ""


def _wait_attr(page, selector: str, attr: str, value: str,
               timeout: int = 10_000) -> None:
    page.wait_for_function(
        "([sel, attr, val]) => document.querySelector(sel)?."
        f"getAttribute(attr) === val",
        arg=[selector, attr, value], timeout=timeout)


SNAKE = '[data-testid="snake-progress"]'


def _open(island, browser):
    page = browser.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{island.base}/a2ui")
    page.wait_for_selector('[data-testid="plane-live"]', timeout=15_000)
    page.wait_for_selector(SNAKE, timeout=10_000)
    return page, errors


class TestA7Snake:
    def test_snake_advances_on_real_sse(self, island, session, browser):
        """1 — the trajectory crosses ONLY verified milestones (real
        server t2 frames); an executing node pokes without crossing."""
        page, errors = _open(island, browser)
        assert _attr(page, SNAKE, "data-crossed") == "0"
        assert _attr(page, SNAKE, "data-poking") == "false"

        # first milestone verified, second EXECUTING — poke, no crossing
        island.transport.push_stage(session.sid, "t2",
                                     _t2("verified", "executing"))
        _wait_attr(page, SNAKE, "data-crossed", "1")
        _wait_attr(page, SNAKE, "data-poking", "true")
        assert _attr(page, '[data-index="1"]', "data-crossed") == "false"
        _shot(page, "a7_01_snake_head_poking.png")

        # second verified → the head crosses onto milestone 2
        island.transport.push_stage(session.sid, "t2",
                                     _t2("verified", "verified", "executing"))
        _wait_attr(page, SNAKE, "data-crossed", "2")
        _wait_attr(page, SNAKE, "data-poking", "true")
        _shot(page, "a7_02_snake_crossed_two.png")

        _dump_evidence("evidence_snake_verified_only.json", {
            "claim": "the snake trajectory advances only onto VERIFIED "
                     "milestones through the REAL SSE pipe; an executing "
                     "node pokes the head without crossing",
            "crossed_after_first_verified": "1",
            "node1_crossed_while_executing": "false",
            "crossed_after_second_verified": "2",
            "transport": "real push_stage → event: progress SSE",
        })
        assert errors == []
        page.close()


class TestA7CheckpointReward:
    def test_checkpoint_reached_fires_the_small_reward(self, island,
                                                       session, browser):
        """2 — checkpoint_reached flips the chip + fires ONE small burst;
        a checkpoint is NOT the finale (no banner)."""
        page, errors = _open(island, browser)
        # the REAL GUI gesture: the user starts autonomy first — a pause
        # is only honest against a running task
        page.click('[data-governance-action="start"]')
        island.transport.push_stage(session.sid, "t2",
                                     _t2("verified", "verified", "verified"))
        island.transport.push_governance(session.sid, "checkpoint_added",
                                         label="日期确认点", rev=1)
        island.transport.push_governance(session.sid, "checkpoint_reached",
                                         label="日期确认点", rev=2)
        # the chip flips reached (the small-reward state)
        page.wait_for_function(
            "() => document.querySelector("
            "'[data-testid=\"checkpoint-strip\"] [data-reached=\"true\"]')"
            " !== null", timeout=10_000)
        # the small confetti burst fired — a canvas element exists
        page.wait_for_function(
            "() => document.querySelectorAll('canvas').length > 0",
            timeout=10_000)
        # let the burst spread from its origin before shooting
        page.wait_for_timeout(400)
        _shot(page, "a7_03_checkpoint_small_reward.png")
        # NO final banner — checkpoints never get the finale
        assert page.locator('[data-testid="final-celebration"]').count() == 0
        # the trajectory settles on the verified prefix (DOM lock)
        _wait_attr(page, SNAKE, "data-crossed", "3")

        _dump_evidence("evidence_checkpoint_reward.json", {
            "claim": "checkpoint_reached flips the chip reached + fires "
                     "ONE small confetti burst; no final banner",
            "chip_reached": True,
            "confetti_canvas_count": "> 0",
            "final_banner": "absent",
            "transport": "real push_governance → event: governance SSE",
        })
        assert errors == []
        page.close()


class TestA7FinalCelebration:
    def test_final_pass_celebrates(self, island, session, browser):
        """3 — ONLY final_pass renders the 🎉 banner + full confetti."""
        page, errors = _open(island, browser)
        island.transport.push_stage(session.sid, "t2", _t2(*ALL_VERIFIED))
        island.transport.push_governance(session.sid, "final_pass",
                                         label="全部节点已验证", rev=9)
        page.wait_for_selector('[data-testid="final-celebration"]',
                               timeout=10_000)
        page.wait_for_function(
            "() => document.querySelectorAll('canvas').length > 0",
            timeout=10_000)
        # let the volleys spread, then shoot at the peak — the confetti
        # fades in ~2s; the DOM locks below outlive it
        page.wait_for_timeout(400)
        _shot(page, "a7_04_final_pass_full_celebration.png")
        # a terminal verdict is NOT a freeze — the trajectory honestly
        # settles onto the fully-verified final state
        _wait_attr(page, SNAKE, "data-crossed", str(len(A7_NODES)))
        # the server's terminal verdict wins on the status pill
        assert "已完成" in page.inner_text('[data-testid="task-status-pill"]')

        _dump_evidence("evidence_final_celebration.json", {
            "claim": "final_pass (and only final_pass) renders the full-"
                     "screen 🎉 banner + the multi-volley confetti",
            "banner": "present",
            "confetti_canvas_count": "> 0",
            "status_pill": "已完成 (completed)",
            "transport": "real push_governance → event: governance SSE",
        })
        assert errors == []
        page.close()

    def test_final_fail_never_celebrates(self, island, session, browser):
        """3-negative — node_failed + final_fail produce the honest
        failure surface: ✕ node, failed pill, NO banner, NO confetti."""
        page, errors = _open(island, browser)
        island.transport.push_stage(session.sid, "t2",
                                     _t2("verified", "executing"))
        island.transport.push_governance(session.sid, "node_failed",
                                         label="校验日期合法", rev=1)
        island.transport.push_governance(session.sid, "final_fail",
                                         label="校验日期合法 未通过", rev=2)
        page.wait_for_function(
            "() => document.querySelector("
            "'[data-testid=\"task-status-pill\"]')?.textContent"
            ".includes('失败')", timeout=10_000)
        # the failed node shows the ✕ mark
        assert _attr(page, '[data-index="1"]', "data-status") == "failed"
        # the negative gate: NO celebration of any kind — the banner is
        # absent AND no confetti canvas was ever created (the pill flip
        # and the celebration run in the same event turn, so by the time
        # the pill shows 失败 the gate has already decided)
        assert page.locator('[data-testid="final-celebration"]').count() == 0
        assert page.locator("canvas").count() == 0
        _shot(page, "a7_05_final_fail_no_celebration.png")

        _dump_evidence("evidence_final_fail_negative_gate.json", {
            "claim": "node_failed + final_fail NEVER celebrate: no banner, "
                     "no confetti canvas, the failed node shows ✕ and the "
                     "pill flips to 失败",
            "final_banner": "absent",
            "confetti_canvas_count": 0,
            "failed_node_status": "failed",
            "status_pill": "失败",
            "transport": "real push_governance → event: governance SSE",
        })
        assert errors == []
        page.close()


class TestA7Rollback:
    def test_rollback_plays_backwards_to_the_checkpoint(self, island,
                                                        session, browser):
        """4 — the trajectory plays BACKWARDS milestone by milestone to
        the target checkpoint node, then settles on the server's
        rolled-back statuses."""
        page, errors = _open(island, browser)
        # six milestones verified, the seventh executing
        six = ("verified",) * 6 + ("executing",)
        island.transport.push_stage(session.sid, "t2", _t2(*six))
        island.transport.push_governance(session.sid, "checkpoint_added",
                                         label="日期确认点", rev=1)
        island.transport.push_governance(session.sid, "checkpoint_reached",
                                         label="日期确认点", rev=2)
        _wait_attr(page, SNAKE, "data-crossed", "6")
        # the governance rollback → reverse playback toward node index 2
        island.transport.push_governance(session.sid, "rollback",
                                         label="日期确认点", rev=3)
        _wait_attr(page, SNAKE, "data-rollback-active", "true")
        assert "日期确认点" in page.inner_text(
            '[data-testid="snake-rollback-caption"]')
        _shot(page, "a7_06_rollback_playback_mid.png")
        # the playback lands on the target checkpoint (index 2)
        _wait_attr(page, SNAKE, "data-crossed", "2", timeout=15_000)
        _wait_attr(page, SNAKE, "data-rollback-active", "false")
        # the server's rolled-back statuses settle the trajectory there
        island.transport.push_stage(session.sid, "t2",
                                     _t2("verified", "verified"))
        page.wait_for_timeout(1_500)   # two tick periods — nothing rebounds
        assert _attr(page, SNAKE, "data-crossed") == "2"
        _shot(page, "a7_07_rollback_settled.png")

        _dump_evidence("evidence_rollback_reverse_playback.json", {
            "claim": "a governance rollback plays the snake trajectory "
                     "BACKWARDS to the target checkpoint node (index 2), "
                     "then settles on the server's rolled-back statuses",
            "crossed_before": "6",
            "target_label": "日期确认点 (node index 2)",
            "crossed_after_playback": "2",
            "crossed_after_server_settle": "2 (no rebound)",
            "transport": "real push_governance → event: governance SSE",
        })
        assert errors == []
        page.close()


class TestA7PauseHonesty:
    def test_pause_freezes_progress_honestly(self, island, session, browser):
        """5 — while paused the trajectory freezes even though server
        truth keeps arriving; resume catches up."""
        page, errors = _open(island, browser)
        # the REAL GUI gesture: start autonomy (a pause is only honest
        # against a running task)
        page.click('[data-governance-action="start"]')
        four = ("verified",) * 4 + ("executing",)
        island.transport.push_stage(session.sid, "t2", _t2(*four))
        _wait_attr(page, SNAKE, "data-crossed", "4")

        island.transport.push_governance(session.sid, "pause", rev=1)
        _wait_attr(page, SNAKE, "data-frozen", "true")
        assert page.locator('[data-testid="snake-paused-caption"]').count() == 1
        # server truth keeps arriving while paused — the snake MUST NOT
        # move (two full tick periods of evidence)
        island.transport.push_stage(session.sid, "t2",
                                     _t2("verified", "verified", "verified",
                                         "verified", "verified", "executing"))
        page.wait_for_timeout(1_500)
        assert _attr(page, SNAKE, "data-crossed") == "4"
        assert "已暂停" in page.inner_text(
            '[data-testid="task-status-pill"]')
        _shot(page, "a7_08_paused_frozen.png")

        # resume → the trajectory catches up to the verified prefix
        island.transport.push_governance(session.sid, "resume", rev=2)
        _wait_attr(page, SNAKE, "data-crossed", "5", timeout=15_000)
        _shot(page, "a7_09_resumed_caught_up.png")

        _dump_evidence("evidence_pause_honesty.json", {
            "claim": "pause freezes the trajectory (no faked progress) "
                     "while server truth keeps arriving; resume catches "
                     "up honestly",
            "crossed_before_pause": "4",
            "crossed_while_paused_after_new_truth": "4 (frozen)",
            "crossed_after_resume": "5 (caught up)",
            "transport": "real push_stage + push_governance SSE",
        })
        assert errors == []
        page.close()


class TestA7ReducedMotion:
    def test_reduced_motion_no_confetti_instant_states(self, island,
                                                       session, browser):
        """6 — a prefers-reduced-motion browser context: NO confetti at
        all, states land instantly (the contrast pair with the full
        celebration in TestA7FinalCelebration)."""
        ctx = browser.new_context(reduced_motion="reduce")
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{island.base}/a2ui")
        page.wait_for_selector('[data-testid="plane-live"]', timeout=15_000)
        page.wait_for_selector(SNAKE, timeout=10_000)

        island.transport.push_stage(session.sid, "t2", _t2(*ALL_VERIFIED))
        island.transport.push_governance(session.sid, "final_pass",
                                         label="全部节点已验证", rev=9)
        # the banner still lands (state, not decoration)
        page.wait_for_selector('[data-testid="final-celebration"]',
                               timeout=10_000)
        # the trajectory snapped instantly — no tick stepping
        _wait_attr(page, SNAKE, "data-crossed", str(len(A7_NODES)))
        # what the layer's own reduced-motion decision was
        banner_reduced = _attr(page, '[data-testid="final-celebration"]',
                               "data-reduced")
        # diagnostic evidence: what the page itself sees
        diag = page.evaluate(
            "() => ({"
            " mq_bare: matchMedia('(prefers-reduced-motion)').matches,"
            " mq_reduce: matchMedia('(prefers-reduced-motion: reduce)')"
            ".matches,"
            " canvases: [...document.querySelectorAll('canvas')].map("
            "c => ({cls: c.className, z: c.style.zIndex,"
            " pos: c.style.position, w: c.width, h: c.height}))"
            "})")
        # NO confetti canvas was ever created
        canvas_count = page.locator("canvas").count()
        assert canvas_count == 0, (
            f"reduced-motion leaked a canvas: banner data-reduced="
            f"{banner_reduced}, diag={diag}")
        _shot(page, "a7_10_reduced_motion_no_confetti.png")

        _dump_evidence("evidence_reduced_motion.json", {
            "claim": "under prefers-reduced-motion: no confetti canvas is "
                     "ever created; the final banner lands statically and "
                     "the trajectory snaps instantly (contrast: the "
                     "normal context celebrates with the full confetti)",
            "confetti_canvas_count": 0,
            "crossed_snapped_instantly": str(len(A7_NODES)),
            "banner": "present (static)",
            "browser_context": "reduced_motion='reduce'",
            "contrast_screenshot": "a7_04_final_pass_full_celebration.png",
        })
        assert errors == []
        page.close()
        ctx.close()


class TestA6IntentConsole:
    def test_console_against_the_real_honest_501(self, island, session,
                                                 browser):
        """7 — the console POSTs to the REAL endpoint; without a parser
        the endpoint's honest 501 surfaces verbatim in the error card."""
        page, errors = _open(island, browser)
        page.fill('[data-testid="intent-input"]',
                  "把发布日期改到 8 月 30 日")
        page.click('[data-testid="intent-submit"]')
        page.wait_for_selector('[data-testid="intent-error"]',
                               timeout=10_000)
        text = page.inner_text('[data-testid="intent-error"]')
        assert "intent parsing is not configured" in text
        assert "意图解析失败" in text
        _shot(page, "a7_11_intent_console_honest_501.png")

        _dump_evidence("evidence_intent_console_501.json", {
            "claim": "the IntentConsole POSTs to the REAL "
                     "/api/app/a2ui/intent and surfaces the honest 501 "
                     "(no parser wired in this fixture) verbatim",
            "posted_text": "把发布日期改到 8 月 30 日",
            "error_surfaced": text.strip(),
            "transport": "real fetch POST → Flask route → 501",
        })
        assert errors == []
        page.close()
