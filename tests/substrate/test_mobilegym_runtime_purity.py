"""tests/substrate — MobileGym runtime-plane purity + honest irreversibility
(B-1/B-3, Oracle audit 2026-08-15).

Behavioral mirror of the Oracle's probe: a recording fake env is injected
into ``MobileGymBridge`` (``bridge.env``), then the runtime plane is driven
directly. What is locked:

  * a MISMATCHED sid on observe/act/mutate raises the honest 409 with
    ZERO env calls — no reset, no get_state, no set_state (the runtime
    never teleports reality — the "session context switching" defense is
    deleted);
  * the evaluation/setup plane (reset) legitimately activates a sid, and
    the runtime then observes/acts WITHOUT any further state switching;
  * the GENERIC mutate rollback path (undo via the injected CUA loop)
    fails with an honest 409 and NEVER falls back to a set_state restore;
  * the write route without the injected CUA loop / ModelVerifier answers
    the honest 501 — no unverified ok, no fallback.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from aiohttp.web import HTTPConflict

from taskvm.substrate.mobilegym.bridge import MobileGymBridge


# ── fakes ───────────────────────────────────────────────────────────────────

class FakePage:
    async def screenshot(self, type="png", **kw):          # noqa: A002
        return b"\x89PNG-fake"

    async def evaluate(self, _js):
        return "fake visible text"


class FakeEnv:
    """Records every reality-touching call (reset/get_state/set_state/step)."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.page = FakePage()

    async def reset(self, app_ids=None):
        self.calls.append(("reset", tuple(app_ids or ())))

    async def get_state(self, required_apps=None):
        self.calls.append(("get_state", tuple(required_apps or ())))
        return {"apps": {}}

    async def set_state(self, state, deep=False):
        self.calls.append(("set_state", deep))

    async def step(self, action):
        self.calls.append(("step", getattr(action, "kind", str(action))))

    async def open_app(self, app, wait_stable=True):
        self.calls.append(("open_app", app))


class FakeGuiExecutorFailure(Exception):
    def __init__(self, reason="model reported fail: no delete/recall UI"):
        super().__init__(reason)
        self.reason = reason


class FakeCua:
    """CUA-loop double for the GENERIC write path. ``gui_act_async``
    records the composed instruction and returns a trace whose ``done``
    flag the test controls (the grounding loop honestly failing the undo
    = done False)."""

    GuiExecutorFailure = FakeGuiExecutorFailure

    def __init__(self, done: bool = True, steps: int = 3):
        self.done = done
        self.steps = steps
        self.instructions: list[str | None] = []

    async def gui_write_async(self, **kw):
        raise FakeGuiExecutorFailure()

    async def gui_act_async(self, env=None, page=None, instruction=None,
                            navigate=None, wait_ready=None,
                            screenshot_dir=None, max_steps=25):
        self.instructions.append(instruction)
        return {"done": self.done, "steps": self.steps,
                "actions": [{"gesture": f"tap({500 + i},{500 + i})"}
                            for i in range(self.steps)]}


class FakeVerifier:
    """ModelVerifier double: records the business intent it was asked to
    judge and returns a fixed three-state verdict."""

    def __init__(self, verdict: str = "changed"):
        self.verdict = verdict
        self.intents: list[str | None] = []

    async def verify_intent(self, observation=None, intent=None):
        self.intents.append(intent)
        return {"verdict": self.verdict,
                "evidence": "fake screen evidence"}


def _bridge(env: FakeEnv, cua=None, verifier=None) -> MobileGymBridge:
    b = MobileGymBridge(sim_url="http://localhost:3000")
    b.env = env                       # injected (start_env never called)
    b.cua = cua
    b.verifier = verifier
    return b


@pytest.fixture()
def fake_bench_env(monkeypatch):
    """``act_primitive`` imports bench_env lazily; provide a fake module so
    the matching-sid gesture path is testable without the mobilegym env."""
    action_mod = types.ModuleType("bench_env.env.base")

    class FakeAction:
        def __init__(self, kind, payload=None):
            self.kind = kind
            self.payload = payload

        @classmethod
        def click(cls, coord):
            return cls("click", tuple(coord))

        @classmethod
        def type_text(cls, text):
            return cls("type", text)

        @classmethod
        def swipe(cls, a, b):
            return cls("swipe", (tuple(a), tuple(b)))

    class FakeActionType:
        ENTER, BACK, HOME = "enter", "back", "home"

    action_mod.Action = FakeAction
    action_mod.ActionType = FakeActionType
    pkg = types.ModuleType("bench_env")
    pkg_env = types.ModuleType("bench_env.env")
    pkg_env.base = action_mod
    pkg.env = pkg_env
    monkeypatch.setitem(sys.modules, "bench_env", pkg)
    monkeypatch.setitem(sys.modules, "bench_env.env", pkg_env)
    monkeypatch.setitem(sys.modules, "bench_env.env.base", action_mod)
    return action_mod


# ── B-1: mismatched sid = honest error, zero reality switches ──────────────

def test_observe_mismatched_sid_never_touches_reality():
    env = FakeEnv()
    b = _bridge(env)
    with pytest.raises(HTTPConflict) as ei:
        asyncio.run(b.observe("sX"))
    assert "session mismatch" in ei.value.text
    assert env.calls == [], (
        f"runtime observe for an INACTIVE sid touched the env: {env.calls}")


def test_act_mismatched_sid_never_touches_reality():
    env = FakeEnv()
    b = _bridge(env)
    with pytest.raises(HTTPConflict) as ei:
        asyncio.run(b.act_primitive("sX", {"kind": "tap",
                                           "coordinate": [10, 10]}))
    assert "session mismatch" in ei.value.text
    assert env.calls == [], (
        f"runtime act for an INACTIVE sid touched the env: {env.calls}")


def test_mutate_mismatched_sid_never_touches_reality():
    env = FakeEnv()
    b = _bridge(env, cua=FakeCua(), verifier=FakeVerifier())
    with pytest.raises(HTTPConflict) as ei:
        asyncio.run(b.mutate("sX", "wechat", "黄勇", "发送文本消息：hi"))
    assert "session mismatch" in ei.value.text
    assert env.calls == []


def test_mismatch_after_another_sid_is_active_still_refuses():
    """The exact Oracle probe scenario: s1 active, runtime asked for s2."""
    env = FakeEnv()
    b = _bridge(env)
    asyncio.run(b.reset("s1"))          # setup plane activates s1 (allowed)
    env.calls.clear()
    with pytest.raises(HTTPConflict):
        asyncio.run(b.observe("s2"))
    with pytest.raises(HTTPConflict):
        asyncio.run(b.act_primitive("s2", {"kind": "tap",
                                           "coordinate": [1, 1]}))
    assert env.calls == [], (
        "the runtime switched/saved/loaded state underneath the caller "
        f"(the deleted context-switch behavior): {env.calls}")


# ── B-1: setup activates, runtime then works WITHOUT state switching ───────

def test_setup_activates_then_runtime_observe_is_read_only():
    env = FakeEnv()
    b = _bridge(env)
    asyncio.run(b.reset("s1"))
    env.calls.clear()
    obs = asyncio.run(b.observe("s1"))
    assert obs["sid"] == "s1"
    assert obs["screenshot"].startswith("data:image/png;base64,")
    assert env.calls == [], (
        f"observe on the ACTIVE sid must not call reset/get_state/set_state: "
        f"{env.calls}")


def test_matching_sid_act_is_a_real_gesture_only(fake_bench_env):
    env = FakeEnv()
    b = _bridge(env)
    asyncio.run(b.reset("s1"))
    env.calls.clear()
    receipt = asyncio.run(b.act_primitive(
        "s1", {"kind": "tap", "coordinate": [120, 340]}))
    assert receipt["status"] == "ok"
    assert env.calls == [("step", "click")], (
        f"act must translate to exactly one env.step gesture: {env.calls}")


# ── honest irreversibility: 409, never a set_state restore ─────────────────

def test_undo_fails_honestly_with_no_backdoor_restore():
    """The GENERIC undo path (mutate undo=True): the injected grounding
    loop cannot complete an undo through the app's own UI → honest 409
    irreversible. The bridge must NEVER fall back to set_state."""
    env = FakeEnv()
    b = _bridge(env, cua=FakeCua(done=False), verifier=FakeVerifier())
    asyncio.run(b.reset("s1"))
    env.calls.clear()
    with pytest.raises(HTTPConflict) as ei:
        asyncio.run(b.mutate("s1", "wechat", "黄勇",
                             "发送文本消息：今晚开会", undo=True))
    assert "irreversible" in ei.value.text.lower()
    switches = [c for c in env.calls if c[0] in ("set_state", "reset")]
    assert not switches, (
        f"a backdoor state restore fired on the 409 path: {env.calls}")


def test_forward_write_without_cua_is_honest_501_not_fallback():
    """Missing the injected CUA loop (or ModelVerifier) → honest 501
    BEFORE touching the world; never an unverified ok, never set_state."""
    env = FakeEnv()
    b = _bridge(env, cua=None, verifier=FakeVerifier())
    asyncio.run(b.reset("s1"))
    from aiohttp.web import HTTPNotImplemented
    with pytest.raises(HTTPNotImplemented):
        asyncio.run(b.mutate("s1", "wechat", "黄勇", "发送文本消息：hello"))
    assert not [c for c in env.calls if c[0] == "set_state"], (
        "no-cua must degrade to 501, never to a set_state shortcut")


def test_forward_write_without_verifier_is_honest_501_not_fallback():
    """The write contract is "execute via real gestures AND verify via the
    model" — missing the ModelVerifier alone is ALSO a 501 (no unverified
    ok, ever)."""
    env = FakeEnv()
    b = _bridge(env, cua=FakeCua(), verifier=None)
    asyncio.run(b.reset("s1"))
    from aiohttp.web import HTTPNotImplemented
    with pytest.raises(HTTPNotImplemented):
        asyncio.run(b.mutate("s1", "wechat", "黄勇", "发送文本消息：hello"))
    assert not [c for c in env.calls if c[0] == "set_state"], (
        "no-verifier must degrade to 501, never to an unverified ok")
