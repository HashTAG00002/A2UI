"""tests/substrate — MobileGym bridge full-app coverage (MG-FULL-APPS /
PURETY-GEN).

Fake-env behavioral tests over the catalog-driven bridge:

  * the ``open`` gesture accepts EVERY catalog app (27, storeless
    calculator/theme_store included) and rejects unknown apps;
  * the generic oracle reads — ``app_state`` (raw store slice of any app;
    honest empty state for storeless apps; 404 for unknown) and
    ``os_state`` (the OS runtime slice);
  * the GENERIC mutate write path is app-agnostic: the same code path and
    the same instruction composition for wechat / x / calculator / notes
    — no operator enum, no per-app branch (locked both behaviorally and
    against the method's own source), no internal vocabulary in the
    composed instruction (noleak), and the ModelVerifier three-state
    verdict gates the returned status;
  * the per-app POST routes are GONE (the 302 compat window closed —
    the R3 tail removed them; POST /api/mutate/<sid> is the only write
    route);
  * ``session_state`` / ``html_view`` are catalog-driven generic
    projections (every store app joins by having a store, legacy tables
    stay byte-stable).
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import types

import pytest

from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web import HTTPBadRequest, HTTPNotFound

from taskvm.architect.noleak import scan as noleak_scan
from taskvm.substrate.mobilegym.app_catalog import (
    ALL_APP_IDS,
    DISPLAY_NAMES,
    get_display_name,
)
from taskvm.substrate.mobilegym.bridge import (
    MobileGymBridge,
    build_app,
)


# ── fakes ───────────────────────────────────────────────────────────────────

class FakePage:
    async def screenshot(self, type="png", **kw):          # noqa: A002
        return b"\x89PNG-fake"

    async def evaluate(self, _js):
        return "fake visible text"


class FakeEnv:
    """Recording fake MobileGymEnv: a plausible store slice per app, an OS
    slice, and every reality-touching call recorded."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.page = FakePage()
        self._apps = {
            "wechat": {"chats": [
                {"id": "c1", "user": {"name": "黄勇", "wxid": "wxid_hy"},
                 "messages": [{"type": "text", "content": "在吗"}]}],
                "contacts": [{"wxid": "wxid_hy", "name": "黄勇"}]},
            "alipay": {"transferRecords": [
                {"id": "t1", "counterpartyName": "黄勇", "delta": -100,
                 "timestamp": 1, "category": "transfer", "kind": "expense",
                 "note": "", "description": ""}],
                "balance": {"total": 900}},
            "x": {"user": {"likedPostIds": ["p_1"],
                           "retweetedPostIds": [], "bookmarkedPostIds": []}},
            "notes": {"notes": [{"id": "n1", "title": "购物清单",
                                 "content": "牛奶 面包"}]},
            "calculator2": {"history": [{"expr": "1+1", "value": 2}]},
            # storeless apps: NO state.ts — an honest empty store slice
            "calculator": {},
            "theme_store": {},
        }
        self._os = {"activeAppId": "home", "tasks": {}, "settings": {},
                    "notifications": [], "home_screen": {}}

    async def reset(self, app_ids=None):
        self.calls.append(("reset", tuple(app_ids or ())))

    async def get_state(self, required_apps=None):
        self.calls.append(("get_state", tuple(required_apps or ())))
        return {"os": dict(self._os), "apps": self._apps}

    async def set_state(self, state, deep=False):
        self.calls.append(("set_state", deep))

    async def step(self, action):
        self.calls.append(("step", getattr(action, "kind", str(action))))

    async def open_app(self, app, wait_stable=True):
        self.calls.append(("open_app", app))


class FakeGuiExecutorFailure(Exception):
    pass


class FakeCua:
    """Records the composed instruction; the trace's ``done`` flag is
    test-controlled."""

    GuiExecutorFailure = FakeGuiExecutorFailure

    def __init__(self, done: bool = True):
        self.done = done
        self.instructions: list[str | None] = []

    async def gui_write_async(self, **kw):                 # pragma: no cover
        raise FakeGuiExecutorFailure()

    async def gui_act_async(self, env=None, page=None, instruction=None,
                            navigate=None, wait_ready=None,
                            screenshot_dir=None, max_steps=25):
        self.instructions.append(instruction)
        return {"done": self.done, "steps": 2,
                "actions": [{"gesture": "tap(500,500)"},
                            {"gesture": "tap(510,510)"}]}


class FakeVerifier:
    def __init__(self, verdict: str = "changed"):
        self.verdict = verdict
        self.intents: list[str | None] = []

    async def verify_intent(self, observation=None, intent=None):
        self.intents.append(intent)
        return {"verdict": self.verdict,
                "evidence": "fake screen evidence"}


@pytest.fixture()
def fake_bench_env(monkeypatch):
    """``act_primitive`` imports bench_env lazily; provide a fake module so
    the gesture path is testable without the mobilegym env."""
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


def _bridge(env: FakeEnv, cua=None, verifier=None) -> MobileGymBridge:
    b = MobileGymBridge(sim_url="http://localhost:3000")
    b.env = env
    b.cua = cua
    b.verifier = verifier
    return b


def _activated(env: FakeEnv, cua=None, verifier=None) -> MobileGymBridge:
    b = _bridge(env, cua=cua, verifier=verifier)
    asyncio.run(b.reset("s1"))          # setup plane activates s1
    env.calls.clear()
    return b


# ── open: full catalog whitelist ────────────────────────────────────────────

def test_open_accepts_every_catalog_app(fake_bench_env):
    env = FakeEnv()
    b = _activated(env)
    for app in ALL_APP_IDS:
        env.calls.clear()
        receipt = asyncio.run(b.act_primitive(
            "s1", {"kind": "open", "target": app}))
        assert receipt["status"] == "ok", (
            f"open({app!r}) must be accepted — the catalog is the open "
            f"whitelist, storeless apps included: {receipt}")
        assert env.calls == [("open_app", app)], (
            f"open({app!r}) must translate to exactly one open_app: "
            f"{env.calls}")


def test_open_rejects_unknown_app(fake_bench_env):
    env = FakeEnv()
    b = _activated(env)
    receipt = asyncio.run(b.act_primitive(
        "s1", {"kind": "open", "target": "phone"}))
    assert receipt["status"] == "failed"
    assert "unknown app" in receipt["detail"]
    assert ("open_app", "phone") not in env.calls


# ── generic oracle reads: app_state / os_state ──────────────────────────────

def test_app_state_returns_raw_store_slice_of_any_app():
    env = FakeEnv()
    b = _activated(env)
    for app in ("notes", "calculator2", "wechat"):
        out = asyncio.run(b.app_state("s1", app))
        assert out == {"sid": "s1", "app": app, "state": env._apps[app]}


def test_app_state_storeless_app_is_honest_empty():
    env = FakeEnv()
    b = _activated(env)
    for app in ("calculator", "theme_store"):
        out = asyncio.run(b.app_state("s1", app))
        assert out["state"] == {}, (
            f"{app} has no zustand store — the honest answer is an empty "
            f"state, not an error and not fabricated data")


def test_app_state_unknown_app_is_404():
    env = FakeEnv()
    b = _activated(env)
    with pytest.raises(HTTPNotFound):
        asyncio.run(b.app_state("s1", "phone"))


def test_os_state_returns_the_os_slice():
    env = FakeEnv()
    b = _activated(env)
    out = asyncio.run(b.os_state("s1"))
    assert out == {"sid": "s1", "os": env._os}


# ── generic mutate: app-agnostic write path ─────────────────────────────────

def test_mutate_source_has_zero_per_app_branch():
    """PURETY-GEN structural lock: the generic write path must not branch
    on the app id — no operator enum, no per-app route table, no
    ``if app == ...`` dispatch."""
    src = inspect.getsource(MobileGymBridge.mutate)
    for banned in ("if app ==", 'app == "', "app == '",
                   "send_message", "toggle_like", "toggle_retweet",
                   "toggle_bookmark"):
        assert banned not in src, (
            f"MobileGymBridge.mutate carries per-app knowledge ({banned!r}) "
            "— the write path must be app-agnostic")


def test_mutate_same_composition_for_every_app_including_storeless():
    """wechat / x / a storeless app (calculator) / a system app (notes)
    all take the SAME path: catalog display name + user-visible entity
    label + NL intent, executed via the injected grounding loop, verified
    by the injected ModelVerifier. No set_state anywhere."""
    env = FakeEnv()
    cua, verifier = FakeCua(), FakeVerifier()
    b = _activated(env, cua=cua, verifier=verifier)
    for app in ("wechat", "x", "calculator", "notes"):
        env.calls.clear()
        cua.instructions.clear()
        verifier.intents.clear()
        out = asyncio.run(b.mutate("s1", app, "黄勇", "发送文本消息：hi"))
        assert out["status"] == "ok", f"app={app}: {out}"
        assert out["verify"]["verdict"] == "changed"
        # one instruction, composed from business language ONLY
        assert len(cua.instructions) == 1
        instruction = cua.instructions[0]
        assert instruction is not None
        assert get_display_name(app) in instruction
        assert "黄勇" in instruction and "发送文本消息" in instruction
        # the verifier judged the business intent, not an operator call
        assert len(verifier.intents) == 1 and "黄勇" in verifier.intents[0]
        # real gestures only — open_app navigates, the loop taps; NEVER a
        # store write behind the GUI
        kinds = {c[0] for c in env.calls}
        assert kinds <= {"open_app"}, f"unexpected env calls: {env.calls}"
        assert ("open_app", app) in env.calls
        assert "set_state" not in kinds


def test_mutate_instruction_is_prompt_clean():
    """GUI-only red line: the composed instruction must carry NO internal
    vocabulary — no operator names, no store ids, no kernel jargon."""
    env = FakeEnv()
    cua, verifier = FakeCua(), FakeVerifier()
    b = _activated(env, cua=cua, verifier=verifier)
    asyncio.run(b.mutate("s1", "wechat", "黄勇", "发送文本消息：今晚开会"))
    asyncio.run(b.mutate("s1", "x", "关于年度复盘的帖子", "点赞"))
    for instruction in cua.instructions:
        assert instruction
        hits = noleak_scan(instruction, extra_terms=("send_message",
                                                     "toggle_like",
                                                     "entity_ref"))
        assert hits == [], (
            f"mutate instruction leaked internal vocabulary: {hits} in "
            f"{instruction!r}")


def test_mutate_verdict_gates_status():
    env = FakeEnv()
    for verdict, status in (("changed", "ok"), ("not_yet", "not_yet"),
                            ("cannot_verify", "cannot_verify")):
        cua, verifier = FakeCua(), FakeVerifier(verdict=verdict)
        b = _activated(env, cua=cua, verifier=verifier)
        out = asyncio.run(b.mutate("s1", "notes", "购物清单", "置顶这条笔记"))
        assert out["status"] == status, (
            f"verdict {verdict!r} must map to status {status!r}: {out}")
        assert out["verify"]["verdict"] == verdict


def test_mutate_malformed_verdict_is_500():
    from aiohttp.web import HTTPInternalServerError
    env = FakeEnv()
    cua, verifier = FakeCua(), FakeVerifier(verdict="probably_done")
    b = _activated(env, cua=cua, verifier=verifier)
    with pytest.raises(HTTPInternalServerError):
        asyncio.run(b.mutate("s1", "notes", "购物清单", "置顶这条笔记"))


def test_mutate_unknown_app_is_400():
    env = FakeEnv()
    b = _activated(env, cua=FakeCua(), verifier=FakeVerifier())
    with pytest.raises(HTTPBadRequest) as ei:
        asyncio.run(b.mutate("s1", "phone", "黄勇", "发送消息"))
    assert "unknown app" in ei.value.text


def test_mutate_empty_intent_is_400():
    env = FakeEnv()
    b = _activated(env, cua=FakeCua(), verifier=FakeVerifier())
    with pytest.raises(HTTPBadRequest):
        asyncio.run(b.mutate("s1", "wechat", "黄勇", "   "))


def test_mutate_loop_not_done_is_honest_500_not_ok():
    from aiohttp.web import HTTPInternalServerError
    env = FakeEnv()
    cua, verifier = FakeCua(done=False), FakeVerifier()
    b = _activated(env, cua=cua, verifier=verifier)
    with pytest.raises(HTTPInternalServerError) as ei:
        asyncio.run(b.mutate("s1", "wechat", "黄勇", "发送文本消息：hi"))
    assert "could not complete" in ei.value.text


# ── legacy per-app routes: removed after the announced R3 window ─────────────

def test_legacy_mutate_routes_are_gone():
    """The per-app operator routes (wechat / x) were 302 compat aliases
    for the generic POST /api/mutate/<sid> route; the removal announced
    in the R3 report has landed — the old spellings answer 404 now, so
    no scripted caller can reach a per-app mutate route anymore."""
    env = FakeEnv()
    b = _bridge(env)

    async def _probe(url: str):
        app = build_app(b)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(url, json={"op": "send_message",
                                                "text": "hi"})
            return resp.status

    for legacy in ("/api/wechat/s1/chat1", "/api/x/s1/p1"):
        assert asyncio.run(_probe(legacy)) == 404, legacy


def test_generic_mutate_route_serves_the_api():
    """The generic route end-to-end through the HTTP layer: payload →
    bridge.mutate → verified ok."""
    env = FakeEnv()
    cua, verifier = FakeCua(), FakeVerifier()
    b = _activated(env, cua=cua, verifier=verifier)

    async def _probe():
        app = build_app(b)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/mutate/s1",
                json={"app": "wechat", "entity_ref": "黄勇",
                      "intent": "发送文本消息：hi", "undo": False})
            return resp.status, await resp.json()

    status, payload = asyncio.run(_probe())
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["verify"]["verdict"] == "changed"


# ── seeding merge directives (the R3-tail generalization) ────────────────────

class RecordingSeedEnv(FakeEnv):
    """FakeEnv + set_state records the patch, so seeding-merge tests can
    assert the composed patch (the shared fake only records call names)."""

    def __init__(self):
        super().__init__()
        self.patches: list[dict] = []

    async def set_state(self, state, deep=False):
        self.patches.append(state)
        await super().set_state(state, deep=deep)


def test_seed_merge_directive_works_for_any_store_app():
    """``merge_<field>`` under ANY catalog store app merges into that
    app's top-level list field — the generalization of the old
    wechat-only special case (notes carries a top-level ``notes`` list
    with ``id`` keys, so it exercises the generic path end to end)."""
    env = RecordingSeedEnv()
    b = _activated(env)
    out = asyncio.run(b.inject_task("s1", None, "", {
        "notes": {"merge_notes": [{"id": "n2", "title": "读书单",
                                   "content": "三体"}]}}))
    assert out["status"] == "ok"
    assert env.patches == [{"apps": {"notes": {"notes": [
        {"id": "n1", "title": "购物清单", "content": "牛奶 面包"},
        {"id": "n2", "title": "读书单", "content": "三体"},
    ]}}}]


def test_seed_legacy_wechat_spellings_still_merge():
    """The frozen bench fixtures speak add_chats/add_contacts (app-keyed
    wechat shape) — they must keep merging exactly as before the
    generalization."""
    env = RecordingSeedEnv()
    b = _activated(env)
    asyncio.run(b.inject_task("s1", None, "", {
        "wechat": {"add_chats": [{"id": "c9",
                                  "user": {"name": "张三",
                                           "wxid": "wxid_zs"},
                                  "messages": []}],
                   "add_contacts": [{"wxid": "wxid_zs", "name": "张三"}]}}))
    wc = env.patches[-1]["apps"]["wechat"]
    assert [c["id"] for c in wc["chats"]] == ["c1", "c9"]
    assert [c["wxid"] for c in wc["contacts"]] == ["wxid_hy", "wxid_zs"]


def test_seed_merge_dedupes_by_primary_key():
    """An entry whose primary key already exists is NOT appended — the
    original entry survives verbatim (no duplicate growth across re-runs
    of the same seed)."""
    env = RecordingSeedEnv()
    b = _activated(env)
    asyncio.run(b.inject_task("s1", None, "", {
        "wechat": {"add_chats": [{"id": "c1", "user": {"name": "dup"},
                                  "messages": []}]}}))
    chats = env.patches[-1]["apps"]["wechat"]["chats"]
    assert [c["id"] for c in chats] == ["c1"]
    assert chats[0]["user"]["name"] == "黄勇"     # the original survived


def test_seed_without_directives_is_plain_replace():
    """A seed with no merge directive takes the plain-replace path
    unchanged (the legacy truthiness: empty directives are no
    directives)."""
    env = RecordingSeedEnv()
    b = _activated(env)
    asyncio.run(b.inject_task("s1", None, "",
                              {"wechat": {"chats": []}}))
    assert env.patches[-1] == {"apps": {"wechat": {"chats": []}}}


def test_seed_merge_without_a_primary_key_is_an_honest_error():
    """Entries carrying none of the candidate primary keys are rejected
    loudly — an undedupable directive would corrupt the seed with
    duplicates on every re-run."""
    env = RecordingSeedEnv()
    b = _activated(env)
    with pytest.raises(ValueError):
        asyncio.run(b.inject_task("s1", None, "", {
            "notes": {"merge_notes": [{"title": "无主键"}]}}))


# ── catalog-driven projections: session_state / html_view ───────────────────

def test_session_state_is_catalog_driven():
    env = FakeEnv()
    b = _activated(env)
    out = asyncio.run(b.session_state("s1"))
    # every app with a store joins by HAVING one — no enumeration
    assert out["apps"]["wechat"]["chats"] == 1
    assert out["apps"]["notes"]["notes"] == 1
    assert out["apps"]["calculator2"]["history"] == 1
    assert "calculator" not in out["apps"], (
        "storeless apps have no store fields — they must not appear with "
        "fabricated counts")
    # legacy compat summary retained byte-stable for existing consumers
    assert out["summary"]["n_chats"] == 1
    assert out["summary"]["n_contacts"] == 1
    assert out["summary"]["n_tx"] == 1
    assert out["summary"]["balance"] == 900


def test_html_view_renders_generic_sections_for_new_apps():
    env = FakeEnv()
    b = _activated(env)
    html = b.html_view("s1")
    # generic section: catalog display name as heading, per-field cells
    assert "笔记 · notes" in html
    assert "购物清单" in html
    assert 'data-field="title"' in html
    # storeless apps honestly have no section (their state IS the screen);
    # calculator2 HAS a store so its section is legitimately present —
    # the assertion pins the exact storeless heading, not a substring
    assert "计算器 ·" not in html
    # legacy tables keep their byte-stable markup
    assert "wechat chats (1)" in html
    assert "alipay transactions (1)" in html


def test_html_view_escapes_user_content():
    env = FakeEnv()
    env._apps["notes"]["notes"] = [{"id": "n1",
                                    "title": "<script>alert(1)</script>"}]
    b = _activated(env)
    html = b.html_view("s1")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ── display names used in composition come from the catalog ─────────────────

def test_mutate_uses_catalog_display_names():
    env = FakeEnv()
    cua, verifier = FakeCua(), FakeVerifier()
    b = _activated(env, cua=cua, verifier=verifier)
    asyncio.run(b.mutate("s1", "redbook", "美食探店笔记", "点赞这条笔记"))
    instruction = cua.instructions[0]
    assert instruction is not None
    assert DISPLAY_NAMES["redbook"] in instruction
