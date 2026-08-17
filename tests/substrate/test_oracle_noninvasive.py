"""tests/substrate/test_oracle_noninvasive.py — B-04 (Oracle audit,
"Non-invasive MobileGym evaluation oracle"): the X-app oracle read must
NEVER move the foreground app, NEVER sleep, and NEVER count as an agent
action. It may only (a) read the in-memory zustand-store dict
(``env.get_state()`` — already-available DATA, not a UI action) and
(b) passively read whatever is ALREADY rendered on the live page (a
``page.evaluate`` DOM query), with zero navigation.

Fixture: a ``FakeEnv`` that behaves like the real MobileGym env enough to
prove the property under test:

  * ``foreground`` — the one thing a real user could see change; starts at
    whatever the agent last navigated to and ONLY changes on
    ``open_app``/``reset``/``set_state`` (never on ``get_state`` or a
    ``page.evaluate`` read).
  * ``page.screenshot``/``observe``-equivalent fingerprint — derived
    PURELY from ``foreground`` (+ a monotonic "user action" counter), so
    it is a faithful stand-in for the real bridge's DOM-structure
    fingerprint: it only changes when something the agent could see
    actually changed.
  * ``agent_action_count`` — incremented only by ``step``/``open_app``
    (the two gesture-producing calls) — an oracle read must not touch it.

These tests exercise ``MobileGymBridge._x_oracle_rows_noninvasive`` (the
B-04 fix) both directly and via the full ``read_resource("x_posts")``
route the evaluation plane's ``oracle_state()`` HTTP client hits.
"""
from __future__ import annotations

import asyncio

from taskvm.substrate.mobilegym.bridge import MobileGymBridge


# ── fakes ───────────────────────────────────────────────────────────────────

class FakePage:
    """Mimics the one thing the oracle's content read touches: a passive
    ``evaluate`` DOM query. Recording calls (not just count) lets tests
    assert exactly what was queried without asserting any navigation."""

    def __init__(self, owner: "FakeEnv"):
        self._owner = owner
        self.evaluate_calls = 0

    async def screenshot(self, type="png", **kw):  # noqa: A002
        return b"\x89PNG-fake-" + self._owner.foreground.encode()

    async def evaluate(self, js: str):
        self.evaluate_calls += 1
        # ``observe()`` (runtime plane) evaluates two different JS snippets
        # that must return STRINGS (visible_text / digest); the oracle's
        # content read evaluates a THIRD snippet querying data-post-id and
        # must return a LIST. Discriminate on the JS source itself — the
        # same discrimination a real Playwright page would resolve simply
        # by what the JS returns.
        if "data-post-id" in js:
            # The fake "DOM" only contains X's post cards when X is the
            # foreground app — a faithful mirror of the real MobileGym sim
            # (data-post-id cards only render while X's timeline is
            # mounted).
            if self._owner.foreground == "x":
                return [{"id": pid, "content": f"content-of-{pid}"}
                        for pid in self._owner.x_post_ids]
            return []
        # observe()'s visible_text / digest snippets — plain strings.
        return f"body::{self._owner.foreground}"


class FakeEnv:
    """Records every reality-touching call AND tracks the two observable
    invariants the B-04 fix must preserve: ``foreground`` (what a real user
    would see) and ``agent_action_count`` (gesture calls only)."""

    def __init__(self, x_liked: list[str] | None = None,
                 x_post_ids: list[str] | None = None,
                 foreground: str = "wechat"):
        self.calls: list[tuple] = []
        self.page = FakePage(self)
        self.foreground = foreground
        self.agent_action_count = 0
        self.x_post_ids = x_post_ids or ["p_1", "p_2"]
        self._x_liked = set(x_liked or [])
        self._x_retweeted: set[str] = set()
        self._x_bookmarked: set[str] = set()

    # ── fingerprint stand-in: pure function of what a user could see ──────
    def fingerprint(self) -> str:
        return f"fp::{self.foreground}::actions={self.agent_action_count}"

    async def reset(self, app_ids=None):
        self.calls.append(("reset", tuple(app_ids or ())))
        self.foreground = "wechat"          # sim boots to the default app

    async def get_state(self, required_apps=None):
        self.calls.append(("get_state", tuple(required_apps or ())))
        return {"apps": {"x": {"user": {
            "likedPostIds": sorted(self._x_liked),
            "retweetedPostIds": sorted(self._x_retweeted),
            "bookmarkedPostIds": sorted(self._x_bookmarked),
        }}}}

    async def set_state(self, state, deep=False):
        self.calls.append(("set_state", deep))

    async def step(self, action):
        self.calls.append(("step", getattr(action, "kind", str(action))))
        self.agent_action_count += 1        # a real gesture — counts

    async def open_app(self, app, wait_stable=True):
        self.calls.append(("open_app", app))
        self.foreground = app               # THE thing the oracle must not do
        self.agent_action_count += 1        # a real navigation — counts


def _bridge(env: FakeEnv) -> MobileGymBridge:
    b = MobileGymBridge(sim_url="http://localhost:3000")
    b.env = env
    return b


async def _observe_like(env: FakeEnv) -> dict:
    """Minimal stand-in for ``MobileGymBridge.observe`` sufficient to prove
    'the agent's next frame is unaffected' — same ingredients (foreground +
    action-derived fingerprint), without requiring the full bridge route."""
    return {"foreground": env.foreground, "fingerprint": env.fingerprint(),
            "agent_action_count": env.agent_action_count}


# ── priority-1: toggle state is a pure store read (grading-relevant) ──────

def test_x_toggle_rows_is_a_pure_projection_zero_env_calls():
    """``_x_toggle_rows`` must not touch env/page at all — it's a @staticmethod
    dict projection over an already-fetched state dict."""
    x_state = {"user": {"likedPostIds": ["p_1"], "retweetedPostIds": [],
                        "bookmarkedPostIds": ["p_2"]}}
    rows = MobileGymBridge._x_toggle_rows(x_state)
    assert rows == {
        "p_1": {"is_liked": True, "is_retweeted": False, "is_bookmarked": False},
        "p_2": {"is_liked": False, "is_retweeted": False, "is_bookmarked": True},
    }


def test_oracle_read_liked_state_correct_without_any_ui_action():
    """The grading-relevant field must be correct using ONLY get_state —
    proves priority-1 (store read) fully covers what checkpoints check.
    ``p_2`` is neither toggled (not in any *PostIds list) nor currently
    visible (foreground='wechat', not 'x') — the honest, non-fabricated
    result is that it simply doesn't appear in the row set (no invented
    is_liked=False for an entity the oracle has no non-invasive evidence
    about either way at this instant)."""
    env = FakeEnv(x_liked=["p_1"], foreground="wechat")
    b = _bridge(env)
    state = asyncio.run(env.get_state())
    rows = asyncio.run(b._x_oracle_rows_noninvasive(state["apps"]["x"]))
    by_id = {r["id"]: r for r in rows}
    assert by_id["p_1"]["is_liked"] is True
    assert "p_2" not in by_id


# ── the core B-04 invariants ────────────────────────────────────────────────

def test_oracle_read_never_calls_open_app_or_sleeps():
    """AST-level guarantee mirrors test_no_api_backdoor's style: the fixed
    method's CODE (not its docstring — hence AST, not a raw substring scan)
    must not contain an ``open_app(...)`` or ``sleep(...)`` call anywhere —
    the exact two calls the audit's evidence cited (bridge.py:382-383,
    old)."""
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(
        inspect.getsource(MobileGymBridge._x_oracle_rows_noninvasive))
    tree = ast.parse(src)
    func = tree.body[0]
    assert isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))
    body = list(func.body)
    # Strip the docstring (first statement, if a bare string expression)
    # so prose mentioning "sleep"/"open_app" for explanatory purposes can
    # never trip this gate — only executable Call nodes count.
    if body and isinstance(body[0], ast.Expr) and isinstance(
            getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    call_names: set[str] = set()
    for node in body:
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                target = n.func
                name = getattr(target, "attr", None) or getattr(
                    target, "id", None) or ""
                call_names.add(name)
    assert "open_app" not in call_names, (
        f"B-04 regression: the non-invasive oracle path calls open_app "
        f"again — this is the exact foreground-switch the audit flagged: "
        f"{call_names}")
    assert "sleep" not in call_names, (
        f"B-04 regression: the non-invasive oracle path sleeps again — "
        f"burning wall-clock the runtime's projection latency is measured "
        f"on: {call_names}")


def test_foreground_unchanged_by_oracle_read():
    """foreground_before == foreground_after — the oracle must not move the
    live sim's active app, whatever it was before the read."""
    for starting_app in ("wechat", "alipay", "x"):
        env = FakeEnv(x_liked=["p_1"], foreground=starting_app)
        b = _bridge(env)
        foreground_before = env.foreground
        state = asyncio.run(env.get_state())
        asyncio.run(b._x_oracle_rows_noninvasive(state["apps"]["x"]))
        foreground_after = env.foreground
        assert foreground_before == foreground_after == starting_app, (
            f"oracle read changed the foreground app from "
            f"{foreground_before!r} to {foreground_after!r}")


def test_fingerprint_unchanged_by_oracle_read():
    """screenshot_fingerprint_before == screenshot_fingerprint_after — an
    equivalent, stable assertion to a literal screenshot byte-compare (the
    fingerprint here is derived purely from foreground + action count,
    mirroring the real bridge's DOM-structure-hash fingerprint)."""
    env = FakeEnv(x_liked=["p_1"], foreground="wechat")
    b = _bridge(env)
    before = asyncio.run(_observe_like(env))
    state = asyncio.run(env.get_state())
    asyncio.run(b._x_oracle_rows_noninvasive(state["apps"]["x"]))
    after = asyncio.run(_observe_like(env))
    assert before["fingerprint"] == after["fingerprint"], (
        "oracle read changed the agent-visible fingerprint — the agent's "
        "next frame would be polluted by grading.")


def test_agent_action_count_unchanged_by_oracle_read():
    """oracle reads must not produce any act() — agent_action_count is
    only ever incremented by step()/open_app() (real gestures)."""
    env = FakeEnv(x_liked=["p_1"], foreground="x")
    b = _bridge(env)
    before = env.agent_action_count
    state = asyncio.run(env.get_state())
    asyncio.run(b._x_oracle_rows_noninvasive(state["apps"]["x"]))
    after = env.agent_action_count
    assert before == after == 0, (
        f"oracle read produced an action: before={before} after={after}")


def test_oracle_read_uses_get_state_and_page_evaluate_only_no_gestures():
    """The full call ledger for an oracle read must contain ONLY
    'get_state' (data read) — the FakeEnv.calls list only records
    reset/get_state/set_state/step/open_app, and step/open_app (the
    gesture-producing calls) must be absent."""
    env = FakeEnv(x_liked=["p_1"], foreground="x")
    b = _bridge(env)
    state = asyncio.run(env.get_state())
    env.calls.clear()
    asyncio.run(b._x_oracle_rows_noninvasive(state["apps"]["x"]))
    forbidden = [c for c in env.calls if c[0] in ("step", "open_app", "reset",
                                                   "set_state")]
    assert not forbidden, (
        f"oracle read performed reality-mutating/foreground-switching "
        f"calls: {forbidden}")


def test_content_is_best_effort_present_when_x_already_foreground():
    """When X already happens to be on screen (e.g. the agent itself
    navigated there), content comes through — the oracle benefits from
    what's already rendered without having caused it."""
    env = FakeEnv(x_liked=["p_1"], foreground="x")
    b = _bridge(env)
    state = asyncio.run(env.get_state())
    rows = asyncio.run(b._x_oracle_rows_noninvasive(state["apps"]["x"]))
    by_id = {r["id"]: r for r in rows}
    assert by_id["p_1"]["content"] == "content-of-p_1"
    assert by_id["p_1"]["is_liked"] is True


def test_content_honestly_blank_when_x_not_foreground_no_fallback_navigation():
    """When X is NOT already on screen, content is honestly "" — the fix
    must NOT fall back to open_app to fetch it (that would resurrect B-04)."""
    env = FakeEnv(x_liked=["p_1"], foreground="wechat")
    b = _bridge(env)
    state = asyncio.run(env.get_state())
    rows = asyncio.run(b._x_oracle_rows_noninvasive(state["apps"]["x"]))
    by_id = {r["id"]: r for r in rows}
    # grading-relevant field still correct even with no DOM access
    assert by_id["p_1"]["is_liked"] is True
    assert by_id["p_1"]["content"] == ""
    assert env.foreground == "wechat", "content miss must not trigger a navigation"


# ── full route: read_resource("x_posts") end-to-end (evaluation plane) ─────

def test_read_resource_x_posts_end_to_end_is_noninvasive():
    """Drives the actual public method the aiohttp route + evaluation.py's
    ``oracle_state()`` HTTP client hit (``GET /api/x_posts/<sid>`` →
    ``read_resource(sid, 'x_posts')``), proving the fix all the way up the
    call chain, not just the helper in isolation."""
    env = FakeEnv(x_liked=["p_1"], foreground="wechat")
    b = _bridge(env)
    asyncio.run(b.reset("s1"))
    env.calls.clear()
    foreground_before = env.foreground
    actions_before = env.agent_action_count

    result = asyncio.run(b.read_resource("s1", "x_posts"))

    foreground_after = env.foreground
    actions_after = env.agent_action_count
    assert foreground_before == foreground_after == "wechat"
    assert actions_before == actions_after == 0
    forbidden = [c for c in env.calls if c[0] in ("step", "open_app")]
    assert not forbidden, f"end-to-end oracle route touched: {forbidden}"
    by_id = {r["id"]: r for r in result["x_posts"]}
    assert by_id["p_1"]["is_liked"] is True, (
        "grading-relevant field lost in the end-to-end route")


def test_read_resource_wechat_and_alipay_unaffected_by_x_fix():
    """No regression on the sibling resources — they never touched
    open_app/sleep to begin with; the fix must not have disturbed them."""
    env = FakeEnv(foreground="wechat")
    b = _bridge(env)
    asyncio.run(b.reset("s1"))
    result = asyncio.run(b.read_resource("s1", "wechat_chats"))
    assert result["wechat_chats"] == []
    result2 = asyncio.run(b.read_resource("s1", "alipay_transactions"))
    assert result2["alipay_transactions"] == []


# ── active surface (runtime session) is untouched by an oracle read ───────

def test_runtime_session_active_surface_unchanged_by_oracle_read():
    """The evaluation-plane oracle read must not perturb what the RUNTIME
    plane (MobileGymSubstrateSession) considers its active surface — a
    session object constructed independently keeps reporting the same
    surface_id/display_name regardless of oracle activity happening on
    the shared bridge underneath it."""
    from taskvm.substrate.mobilegym.session import MobileGymSubstrateSession

    session = MobileGymSubstrateSession(
        sid="s1", bridge_url="http://localhost:3019", surface_app="wechat")
    surfaces_before = [s.surface_id for s in session.list_surfaces()]

    env = FakeEnv(x_liked=["p_1"], foreground="wechat")
    b = _bridge(env)
    asyncio.run(b.reset("s1"))
    asyncio.run(b.read_resource("s1", "x_posts"))

    surfaces_after = [s.surface_id for s in session.list_surfaces()]
    assert surfaces_before == surfaces_after == ["mobilegym:wechat"]


# ── online-control-loop discipline: oracle calls during a simulated loop ──

def test_oracle_calls_do_not_grow_with_runtime_observe_act_cycles():
    """Simulates a small online control loop (repeated ``observe()`` calls,
    the runtime's own read of the live page — ``act_primitive`` itself is
    exercised by ``test_mobilegym_runtime_purity.py``'s existing
    ``fake_bench_env`` fixture and is out of scope here) interleaved with
    oracle reads, and asserts that NONE of the runtime's own observations
    are perturbed by an oracle read sitting in between them — i.e. the
    oracle is safe to call at any point in the online loop without
    corrupting the runtime's view (the strongest form of priority-1/2: no
    need to demote to priority-3 'end of trial only', since the read is
    provably inert)."""
    env = FakeEnv(x_liked=[], foreground="wechat")
    b = _bridge(env)
    asyncio.run(b.reset("s1"))

    async def one_cycle():
        obs1 = await b.observe("s1")
        # oracle read lands HERE, mid-loop — must be transparent
        await b.read_resource("s1", "x_posts")
        obs2 = await b.observe("s1")
        # a second oracle read, back-to-back — must still be transparent
        await b.read_resource("s1", "x_posts")
        obs3 = await b.observe("s1")
        return obs1["fingerprint"], obs2["fingerprint"], obs3["fingerprint"]

    fp1, fp2, fp3 = asyncio.run(one_cycle())
    assert fp1 == fp2 == fp3, (
        "an oracle read sitting between runtime observe() calls changed "
        f"the observed fingerprint: {fp1!r} -> {fp2!r} -> {fp3!r}")
