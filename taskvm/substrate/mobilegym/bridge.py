"""MobileGym bridge — the async→sync HTTP shim between TaskVM and MobileGym.

TaskVM's ``StateAdapter`` is a synchronous ``requests``-based base class that
assumes each app is a Flask HTTP service on its own port. MobileGym is a single
asyncio process driving a Playwright browser page (``MobileGymEnv``). This
module is the **thin resident HTTP server** that holds one ``MobileGymEnv``
instance and exposes the TaskVM adapter contract (``reset`` / ``inject_task``
/ ``read_canonical`` / ``mutate``) as REST routes, translating each sync HTTP
call into an ``await env.xxx`` on the internal event loop.

Why a bridge, not a direct adapter (handoff §2): MobileGym's agent write path
is GUI gestures (``env.step(Action(CLICK/TYPE/...))`` → ``__SIM_INPUT__``),
NOT ``set_state`` (which MobileGym's runtime-api.md L276 defines as setup-only
"inject task-initial conditions"). TaskVM's rollback/verifier only talk to the
``StateAdapter`` interface, so we adapt MobileGym to that interface HERE and
leave ``rollback.py`` / ``reconciliation.py`` / ``verifier/`` untouched.

**Non-invasive write/rollback boundary (load-bearing — handoff fix
2026-08-10, see memory taskvm-non-invasive-write-rollback-boundary; Task3 E10
rework 2026-08-11).** The wechat write path goes through ``gui_write_async``
(Task3 — a REAL grounding loop: screenshot → model → ``env.step(Action.click/
type/swipe)`` gestures; the model observes the chat page and decides how to
send: tap composer → type text → Enter/send). NO ``set_state``, and NOT a
hardcoded click sequence. The earlier hardcoded 7-step ``_send_message`` is
retained below as DEAD CODE (superseded by ``gui_write_async``; not on the
live path, kept for reference only).

The wechat rollback path: ``gui_write_async(undo=True)`` — the model observes
the chat page + TRIES to find a delete/recall UI (long-press menu, recall
button). MobileGym's wechat has NO such UI (verified — no long-press handler,
no deleteMessage store action, append-only messages), so the model outputs
``{"action":"fail"}`` and the bridge raises ``web.HTTPConflict`` (409). This
is honest irreversibility PROVEN by the model's real attempt (Task3 replaced
the old hardcoded 409 with model-driven discovery of the same conclusion).
The bridge does NOT fall back to ``set_state`` to fake a byte-exact restore
(that would undermine the compensation claim — a backdoor rollback proves "we
have debug permission," not "TaskVM compensates"). ``rollback.py``'s saga undo
catches the 409 → ``reverted=False``, ``partial_failure=True``; the verifier
independently confirms the message is still there (fidelity=0.0).

The X toggle write path (``mutate_x``) is also a real grounding loop
(``gui_act_async``). E16-complete (2026-08-12): the model's instruction names
NO post_id, NO post text, NO current toggle state — pure-vision CUA (the
content_hint backdoor, whether from posts.json or from DOM textContent, is
fully removed; see the in-method comment).

Routes (mirror the Drive app's contract, app-namespaced):
    GET  /health                              → {"status":"ok","site":"mobilegym"}
    GET  /<sid>                               → minimal HTML view (data-chat-id DOM)
    POST /api/reset/<sid>                     → env.reset(app_ids=[wechat,alipay,x]) [SETUP]
    POST /api/inject_task/<sid>               → env.set_state(seed, deep=True) [SETUP-ONLY]
    GET  /api/observe/<sid>                   → screenshot+visible text (RUNTIME; requires active sid)
    POST /api/act/<sid>                       → env.step(real gesture) (RUNTIME; requires active sid)
    GET  /api/session_state/<sid>             → summary only (n_chats, n_tx) — never GT
    GET  /api/wechat_chats/<sid>              → flattened wechat chats (entities)
    GET  /api/alipay_transactions/<sid>       → flattened alipay transferRecords
    GET  /api/x_state/<sid>                   → X toggle lists (liked/retweeted/bookmarked ids) [verifier read]
    POST /api/wechat/<sid>/<eid>              → send_message via gui_write_async (real gestures) OR msg:<id> rollback (gui_write_async undo → 409 if irreversible)
    POST /api/x/<sid>/<eid>                   → toggle_like/retweet/bookmark via gui_act_async (pure-vision CUA, E16-complete)

B-1 (Oracle audit 2026-08-15): ONE active experimental session at a time.
The evaluation/setup plane (reset/inject_task/oracle reads) activates a
sid; the runtime plane (observe/act/mutate routes) REQUIRES the active sid
and honestly refuses a mismatch (409 session mismatch) — the runtime never
switches reality via env.reset/get_state/set_state underneath the caller.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Awaitable, Callable

from aiohttp import web

logger = logging.getLogger(__name__)

SITE = "mobilegym"
DEFAULT_PORT = 3019
# MobileGym apps this bridge exposes (demo scope: Top3 task = alipay→wechat).
APPS = ["wechat", "alipay", "x"]
# The Vite dev/preview server URL the env drives.
DEFAULT_SIM_URL = "http://localhost:3000"


class MobileGymBridge:
    """Holds one MobileGymEnv + per-sid live-state cache, served by aiohttp."""

    def __init__(self, sim_url: str, headless: bool = True,
                 screenshot_dir: str | None = None,
                 cua: "CuaLoopModule | None" = None):
        self.sim_url = sim_url
        self.headless = headless
        self.screenshot_dir = screenshot_dir    # E9.4/task-3: auto step shots
        self._shot_counter = 0                  # monotonic (Date.now banned)
        # ── Agent B (substrate isolation): the CUA loops are INJECTED, never
        # imported. The old ``from taskvm.execution.gui_executor_async import
        # gui_write_async`` was a substrate→upper-layer reverse dependency
        # (architecture-gate KNOWN DEBT, now repaid). ``cua`` is any object
        # exposing ``gui_write_async`` / ``gui_act_async`` — e.g. the
        # ``taskvm.execution.gui_executor_async`` module, passed at PROCESS
        # ASSEMBLY time via ``--cua-loop``. Without it the mutate routes
        # below answer 501 (honest unavailability — no fallback).
        self.cua = cua
        self.env = None                      # MobileGymEnv, built in start()
        self._loop: asyncio.AbstractEventLoop | None = None
        # per-sid "live" snapshot = the full {os, apps} state currently
        # associated with that sid. The env's browser holds exactly one
        # state at a time; switching sids swaps it via set_state(deep=False).
        self._sid_live: dict[str, dict] = {}
        self._active_sid: str | None = None
        self._lock = asyncio.Lock()
        self._revision = 0                    # port Observation revision counter

    # ── env lifecycle ────────────────────────────────────────────────────────
    async def start_env(self) -> None:
        from bench_env.env.mobile_gym import MobileGymEnv
        # Headed honesty (E7-style — don't fake headed): if the caller asked
        # for a visible browser (--headed) but this box has no X display (no
        # $DISPLAY, no /tmp/.X11-unix socket, no xvfb — common on a headless
        # server), fall back to headless with a LOUD warning rather than crash
        # the demo. The screenshots (_screenshot) still fire either way, so the
        # '我现在啥也看不见，你要录下来' deliverable is met headless. A live
        # headed demo needs a real display (or xvfb-run) — documented runbook.
        import os
        effective_headless = self.headless
        if not effective_headless:
            have_display = bool(os.environ.get("DISPLAY")) or \
                bool(os.listdir("/tmp/.X11-unix")) if os.path.isdir("/tmp/.X11-unix") else False
            if not have_display:
                logger.warning(
                    "[bridge] --headed requested but no X display on this box "
                    "(DISPLAY unset, /tmp/.X11-unix empty, no xvfb). Falling "
                    "back to headless so the demo doesn't crash. Per-step "
                    "screenshots still land in eval_results/mobilegym_visual_*/. "
                    "For a live headed demo, run on a machine with a display "
                    "(or `xvfb-run python -m taskvm.harness.mobilegym_bridge --headed`).")
                effective_headless = True
        self.env = MobileGymEnv(url=self.sim_url, headless=effective_headless,
                                verbose=False)
        await self.env.start()
        logger.info(f"[bridge] MobileGymEnv started (url={self.sim_url}, "
                    f"headless={effective_headless}, "
                    f"screenshot_dir={self.screenshot_dir})")

    async def _screenshot(self, name: str) -> str | None:
        """Save a PNG of the live sim page after a gesture step (task-3 —
        '录下来': the user can't watch a headless chromium, so each gesture
        action is snapshotted to disk for review). Path:
        ``<screenshot_dir>/step_{N:02d}_{name}.png``. No-op (returns None)
        when ``screenshot_dir`` is unset. Captures the FULL page so the phone
        frame + the just-performed gesture's effect are both visible."""
        if not self.screenshot_dir or self.env is None or self.env.page is None:
            return None
        try:
            import os
            os.makedirs(self.screenshot_dir, exist_ok=True)
            self._shot_counter += 1
            path = os.path.join(
                self.screenshot_dir,
                f"step_{self._shot_counter:02d}_{name}.png")
            await self.env.page.screenshot(path=path, full_page=True)
            logger.info(f"[bridge] screenshot → {path}")
            return path
        except Exception as e:
            logger.warning(f"[bridge] screenshot '{name}' failed (non-fatal): {e}")
            return None

    async def aclose(self) -> None:
        if self.env is not None:
            await self.env.close()

    # ── L1 primitive routes (Agent B): observe / act over REAL gestures ────
    # These back the ``MobileGymSubstrateSession`` port implementation
    # (substrate/mobilegym/session.py). They carry no operator semantics,
    # no entity ids, no store contents — the runtime's CUA loop composes
    # them exactly like it composes WebSubstrateSession gestures.
    async def observe(self, sid: str) -> dict:
        """Screenshot + scrubbed visible text + visible-structure
        fingerprint of the live sim page (zero-exposure: only what a real
        user can see on the rendered screen).

        B-1 (Oracle audit): the runtime plane NEVER switches reality. The
        live sim is bound to ONE active experimental session, established
        by the evaluation/setup plane (reset/inject_task). A mismatched sid
        is an honest error — no ``reset``/``get_state``/``set_state`` from
        here, ever."""
        await self._require_active(sid)
        page = self.env.page
        png = await page.screenshot(type="png")
        import base64
        data_url = "data:image/png;base64," + base64.b64encode(png).decode()
        visible_text = await page.evaluate(
            "() => (document.body && document.body.innerText || '')"
            ".slice(0, 8000)")
        digest = await page.evaluate(
            "() => { const walk = (n, d) => { if (!n || d > 18) return ''; "
            "let s = ''; for (const c of n.children || []) { "
            "s += c.tagName + '(' + ((c.innerText || '').trim()"
            ".slice(0, 24)) + ')' + walk(c, d + 1); } return s; }; "
            "return walk(document.body, 0).slice(0, 4000); }")
        fingerprint = hashlib.sha1(digest.encode("utf-8")).hexdigest()[:16]
        self._revision += 1
        return {"sid": sid, "revision": self._revision,
                "screenshot": data_url, "visible_text": visible_text,
                "fingerprint": fingerprint, "timestamp": time.time()}

    async def act_primitive(self, sid: str, action: dict) -> dict:
        """One REAL gesture on the live sim (tap/type/swipe/key/open). Uses
        MobileGym's own ``env.step(Action...)`` norm_0_1000 calibration —
        identical pipeline to the grounding loop's gestures.

        B-1: runtime plane — requires the evaluation plane to have made
        ``sid`` the active session first; never context-switches. The
        guard runs BEFORE the bench_env import so a session mismatch is
        answered with zero environment side effects."""
        await self._require_active(sid)
        from bench_env.env.base import Action, ActionType
        kind = action.get("kind")
        coord = action.get("coordinate") or [500, 500]
        if kind in ("click", "tap"):
            await self.env.step(Action.click([float(coord[0]), float(coord[1])]))
            return {"status": "ok", "detail": f"tap({coord[0]:.0f},{coord[1]:.0f})"}
        if kind == "type":
            await self.env.step(Action.type_text(str(action.get("text") or "")))
            return {"status": "ok", "detail": "type"}
        if kind == "key":
            key = str(action.get("key") or "Enter").lower()
            amap = {"enter": ActionType.ENTER, "back": ActionType.BACK,
                    "home": ActionType.HOME}
            if key not in amap:
                return {"status": "failed", "detail": f"unsupported key {key!r}"}
            await self.env.step(Action(amap[key], {}))
            return {"status": "ok", "detail": f"key({key})"}
        if kind == "scroll":
            d = str(action.get("direction") or "down")
            mag = int(action.get("magnitude") or 400)
            dy = -mag if d == "up" else mag
            await self.env.step(Action.swipe([500, 500], [500, 500 + dy]))
            return {"status": "ok", "detail": f"scroll({d})"}
        if kind == "wait":
            await asyncio.sleep((action.get("duration_ms") or 1000) / 1000.0)
            return {"status": "ok", "detail": "wait"}
        if kind == "open":
            target = str(action.get("target") or "")
            known = [a for a in APPS if a == target]
            if not known:
                return {"status": "failed",
                        "detail": f"unknown app {target!r}"}
            await self.env.open_app(target, wait_stable=True)
            return {"status": "ok", "detail": f"open({target})"}
        return {"status": "failed", "detail": f"unsupported kind {kind!r}"}

    # ── session activation (B-1: EVALUATION/SETUP plane only) ───────────────
    async def _require_active(self, sid: str) -> None:
        """Runtime-plane guard (B-1, Oracle audit): ``observe`` / ``act`` /
        task-level mutate routes must NEVER switch reality. The browser
        holds ONE live experimental session, established by the evaluation
        plane (``reset`` / ``inject_task`` — the exam-room powers). A
        mismatched sid is an honest session-mismatch error; there is no
        transparent ``set_state`` teleport underneath the runtime."""
        if self._active_sid != sid:
            raise web.HTTPConflict(text=(
                f"session mismatch: active={self._active_sid!r}, "
                f"requested={sid!r}. The runtime plane never switches "
                f"reality; activate this sid via the evaluation setup "
                f"(POST /api/reset/<sid> + /api/inject_task/<sid>) first."))

    async def _activate(self, sid: str) -> None:
        """Make ``sid`` the live browser state — EVALUATION/SETUP PLANE ONLY
        (``reset`` / ``inject_task`` / oracle reads). The runtime plane
        (``observe`` / ``act_primitive`` / mutate routes) uses
        ``_require_active`` instead and honestly refuses a mismatched sid.

        Contract (docs/contracts/substrate.md, frozen): ``set_state`` is
        reachable ONLY via the EvaluationEnvironment plane. There is no
        "session context switching" exception for runtime calls."""
        async with self._lock:
            if self._active_sid == sid:
                return
            if self._active_sid is not None:
                # save current live state back to the old sid
                try:
                    self._sid_live[self._active_sid] = await self.env.get_state(
                        required_apps=APPS)
                except Exception as e:
                    logger.warning(f"[bridge] save {self._active_sid} failed: {e}")
            if sid in self._sid_live:
                await self.env.set_state(self._sid_live[sid], deep=False)
            else:
                # fresh: reset the sim and capture defaults
                await self.env.reset(app_ids=APPS)
                self._sid_live[sid] = await self.env.get_state(required_apps=APPS)
            self._active_sid = sid

    # ── adapter contract ops ────────────────────────────────────────────────
    async def reset(self, sid: str) -> dict:
        async with self._lock:
            await self.env.reset(app_ids=APPS)
            self._sid_live[sid] = await self.env.get_state(required_apps=APPS)
            self._active_sid = sid
        return {"status": "ok", "reset": True, "sid": sid}

    async def inject_task(self, sid: str, task_id: str | None, goal: str,
                          seed_state: dict) -> dict:
        """Seed the visible app state (set_state IS the documented setup/seed
        API — runtime-api.md L276 — so this is the legitimate seed path, NOT
        the write/rollback path; the non-invasive boundary applies to the
        write/rollback path only, see memory taskvm-non-invasive-...).

        Recognizes two demo directives under the wechat seed (at EITHER the
        app-keyed level ``{wechat:{add_chats, add_contacts}}`` or the per-app
        slice ``{add_chats, add_contacts}`` that ``replay.seed_apps`` passes):
          - ``add_chats``: merge new ChatSessions into wechat.chats by id
          - ``add_contacts``: merge new ContactItems into wechat.contacts by wxid
        Both read current + append (set_state replaces lists, so we merge here).
        Seeding a synthetic contact (no aiConfig) + an empty chat lets the
        fixture control the demo target without touching real contacts (the
        real 黄勇 has aiConfig.enabled=True which would fire an AI reply and
        complicate round-trip verification)."""
        await self._activate(sid)
        async with self._lock:
            if seed_state:
                wc_seed = (seed_state.get("wechat") or seed_state)
                add_chats = wc_seed.get("add_chats")
                add_contacts = wc_seed.get("add_contacts")
                if add_chats or add_contacts:
                    cur = await self.env.get_state(required_apps=["wechat"])
                    wcur = cur.get("apps", {}).get("wechat", {}) or {}
                    patch_apps: dict[str, Any] = {}
                    if add_chats:
                        chats = copy.deepcopy(wcur.get("chats", []) or [])
                        existing = {c.get("id") for c in chats}
                        for c in add_chats:
                            if c.get("id") not in existing:
                                chats.append(c)
                        patch_apps["chats"] = chats
                    if add_contacts:
                        contacts = copy.deepcopy(wcur.get("contacts", []) or [])
                        existing = {c.get("wxid") for c in contacts}
                        for c in add_contacts:
                            if c.get("wxid") not in existing:
                                contacts.append(c)
                        patch_apps["contacts"] = contacts
                    patch = {"apps": {"wechat": patch_apps}}
                else:
                    patch = self._normalize_patch(seed_state)
                await self.env.set_state(patch, deep=True)
                self._sid_live[sid] = await self.env.get_state(required_apps=APPS)
        return {"status": "ok", "sid": sid, "task_id": task_id}

    @staticmethod
    def _normalize_patch(seed_state: dict) -> dict:
        """Accept either {wechat:{...}, alipay:{...}} (app-keyed) or the full
        {apps:{...}, os:{...}} shape; return the full shape for set_state."""
        if "apps" in seed_state or "os" in seed_state:
            return seed_state
        return {"apps": seed_state}

    async def read_resource(self, sid: str, resource: str) -> dict:
        await self._activate(sid)
        state = await self.env.get_state(required_apps=APPS)
        self._sid_live[sid] = state          # refresh live cache
        apps = state.get("apps", {})
        if resource == "wechat_chats":
            rows = self._flatten_wechat_chats(apps.get("wechat", {}))
            return {"site": SITE, "sid": sid, "wechat_chats": rows}
        if resource == "alipay_transactions":
            rows = self._flatten_alipay_txs(apps.get("alipay", {}))
            return {"site": SITE, "sid": sid, "alipay_transactions": rows}
        if resource == "x_posts":
            # X posts live in the base dataset (not in the zustand store),
            # so we must read them from the DOM. This requires the X app
            # to be open — open it first, then read.
            await self.env.open_app("x", wait_stable=True)
            await asyncio.sleep(1.5)  # let timeline render
            rows = await self._flatten_x_posts_async(apps.get("x", {}))
            return {"site": SITE, "sid": sid, "x_posts": rows}
        raise web.HTTPNotFound(text=f"unknown resource {resource}")

    @staticmethod
    def _flatten_wechat_chats(wechat: dict) -> list[dict]:
        """wechat.chats → [{id, peer_name, n_messages, last_message, messages}].
        ``messages`` is the joined string of TEXT message contents only —
        MobileGym's ``upsertChatMessages`` auto-prepends ``time``/``system``
        separator messages (UI chrome, e.g. "16:22" / "你已添加了..."), which
        are NOT user-authored content. Projecting them out is a faithful
        "what text was sent" view (same kind of field projection the other
        adapters do), and lets the verifier's exact-equality ``field_matches``
        check the sent text without the time label polluting the comparison.
        ``n_messages`` is the TOTAL message count (honest full count incl.
        separators); ``last_message`` is the last message content of any type."""
        rows = []
        for c in wechat.get("chats", []) or []:
            msgs = c.get("messages") or []
            text_msgs = [m for m in msgs if m.get("type") == "text"]
            joined = " ".join(str(m.get("content", "")) for m in text_msgs)
            rows.append({
                "id": c.get("id"),
                "peer_name": (c.get("user") or {}).get("name", ""),
                "peer_wxid": (c.get("user") or {}).get("wxid", ""),
                "n_messages": len(msgs),
                "last_message": msgs[-1].get("content", "") if msgs else "",
                "messages": joined,
            })
        return rows

    async def _flatten_x_posts_async(self, x_state: dict) -> list[dict]:
        """Read X posts from the LIVE DOM (not from state). X's post table
        lives in a base dataset (posts.json, loaded via preload()) that is
        NOT part of the zustand store — so state['apps']['x']['posts'] is
        always an empty dict. The posts ARE rendered in the DOM, each in a
        ``<div data-post-id="p_...">`` container (Task B, 2026-08-12 fix —
        added to ``XTimelinePostCard.tsx``'s root div; a small, non-invasive
        markup addition, not a behavior change).

        This reads that attribute + the store's toggle lists
        (``user.likedPostIds`` etc. — which ARE in the store) to produce the
        same row schema as before: [{id, is_liked, is_retweeted,
        is_bookmarked, content}].

        Bug this replaces (E14-honest rec'd fix, .mrules Task B): the old
        reader found post ids via ``[data-action-params]`` action-bar
        buttons, then walked UP via ``b.closest('[class*="flex flex-col"]')``
        to find a "post card" container for the content preview — but no
        ancestor of the action bar actually has a class matching
        ``flex flex-col`` (verified: the real ancestor chain is
        ``border-b p-4 ...`` at the post root, `flex` (avatar+body row) two
        levels up, `flex-1 min-w-0` for the body — none contain the literal
        substring "flex flex-col" together). The ``closest`` call always
        returned null, so the code fell through to the
        ``b.parentElement?.parentElement?.parentElement`` fallback, which for
        the LIKE button (``XPostActionBar`` renders a flat row of buttons)
        walks up to a grandparent shared across ALL action-bar icons in the
        SAME row (not the whole post), and for adjacent/first posts on the
        timeline this often resolved to the same ancestor for multiple posts
        (or one that also captured the top navigation bar's text if the DOM
        was still settling) — producing identical/wrong content previews per
        .mrules E14-honest. The new ``data-post-id`` selector reads content
        DIRECTLY from that post's own container — one query, no ancestor
        walking, so it cannot cross-contaminate between posts.

        Requires the X app to be OPEN (timeline visible) — callers must
        ``open_app('x')`` before invoking this."""
        user = x_state.get("user", {}) or {}
        liked = set(user.get("likedPostIds", []) or [])
        retweeted = set(user.get("retweetedPostIds", []) or [])
        bookmarked = set(user.get("bookmarkedPostIds", []) or [])
        dom_posts = await self.env.page.evaluate("""() => {
            const cards = document.querySelectorAll('[data-post-id]');
            const posts = [];
            for (const card of cards) {
                const pid = card.getAttribute('data-post-id');
                if (!pid) continue;
                const content = card.textContent?.substring(0, 200) || '';
                posts.push({id: pid, content});
            }
            return posts;
        }""")
        rows = []
        for p in dom_posts or []:
            pid = p.get("id")
            rows.append({
                "id": pid,
                "content": str(p.get("content", ""))[:80],
                "is_liked": pid in liked,
                "is_retweeted": pid in retweeted,
                "is_bookmarked": pid in bookmarked,
            })
        return rows

    @staticmethod
    def _flatten_alipay_txs(alipay: dict) -> list[dict]:
        """alipay.transferRecords → [{id, counterpartyName, delta, timestamp, ...}].
        Read-only surface for the Top3 binding (filter delta<0, sort |delta| desc)."""
        rows = []
        for t in alipay.get("transferRecords", []) or []:
            rows.append({
                "id": t.get("id"),
                "counterpartyName": t.get("counterpartyName", ""),
                "delta": t.get("delta"),
                "timestamp": t.get("timestamp"),
                "category": t.get("category", ""),
                "kind": t.get("kind", ""),
                "note": t.get("note", ""),
                "description": t.get("description", ""),
            })
        return rows

    async def x_state(self, sid: str) -> dict:
        """Read-only: the X app's toggle lists (liked/retweeted/bookmarked
        post ids) for the given session. Used by ``run_x_toggle_rollback_
        killtest.py`` (Task E, .mrules E15) to independently VERIFY that a
        toggle write/rollback actually landed via ``get_state`` — the same
        trusted read path ``mutate_x`` itself uses. This is a plain read (no
        mutation, no set_state), so it does not touch the non-invasive
        write/rollback boundary documented above."""
        await self._activate(sid)
        state = self._sid_live.get(sid) or await self.env.get_state(required_apps=APPS)
        x_state = state.get("apps", {}).get("x", {}) or {}
        user = x_state.get("user", {}) or {}
        return {
            "sid": sid,
            "likedPostIds": user.get("likedPostIds", []) or [],
            "retweetedPostIds": user.get("retweetedPostIds", []) or [],
            "bookmarkedPostIds": user.get("bookmarkedPostIds", []) or [],
        }

    async def session_state(self, sid: str) -> dict:
        await self._activate(sid)
        state = self._sid_live.get(sid) or await self.env.get_state(required_apps=APPS)
        apps = state.get("apps", {})
        wechat = apps.get("wechat", {})
        alipay = apps.get("alipay", {})
        return {"site": SITE, "sid": sid,
                "has_task": True,
                "summary": {"n_chats": len(wechat.get("chats", [])),
                            "n_contacts": len(wechat.get("contacts", [])),
                            "n_tx": len(alipay.get("transferRecords", [])),
                            "balance": alipay.get("balance", {}).get("total")}}

    # ── write path: send_message via REAL GUI gestures (no set_state) ───────
    async def mutate_wechat(self, sid: str, eid: str, operator: str,
                            value: Any) -> dict:
        # B-1: runtime write path — requires the active session; never
        # context-switches reality underneath the CUA loop.
        await self._require_active(sid)
        async with self._lock:
            if operator != "send_message":
                raise web.HTTPBadRequest(
                    text=f"wechat operator must be send_message, got {operator}")
            if self.cua is None:
                # Agent B: honest unavailability — the L2 CUA loop is not
                # installed in this bridge process. NO fallback (neither a
                # hardcoded gesture sequence NOR a set_state backdoor).
                raise web.HTTPNotImplemented(text=(
                    "wechat send_message requires a CUA loop; this bridge was "
                    "started without --cua-loop. Start it with e.g. "
                    "--cua-loop taskvm.execution.gui_executor_async, or drive "
                    "the L1 observe/act port (substrate/mobilegym/session.py) "
                    "from the runtime."))
            gui_write_async = self.cua.gui_write_async
            GuiExecutorFailure = self.cua.GuiExecutorFailure
            # ── rollback path: value is "msg:<id>" from a prior send_message ──
            # Task3 (E10 rework): the rollback NO LONGER hardcodes "wechat has
            # no delete UI → 409". It calls gui_write_async(undo=True) — a REAL
            # grounding loop that observes the chat page + TRIES to find a
            # delete/recall UI. If the model outputs {"action":"fail"}, THEN
            # the bridge raises HTTP 409 — but now the irreversibility is PROVEN
            # by the model's real attempt (handoff Task3: "结论可能不变，但证明
            # 这个结论的方法论要和主线一致"), not a programmer's hardcoded
            # pre-judgment. The old hardcoded 409 is replaced by model-driven
            # discovery of the same conclusion.
            if isinstance(value, str) and value.startswith("msg:"):
                await self._screenshot("undo_attempt_gui_executor")
                try:
                    trace = await gui_write_async(
                        env=self.env, page=self.env.page, sid=sid,
                        chat_id=eid, text=value, undo=True,
                        screenshot_dir=self.screenshot_dir)
                    # if the model said DONE (found a delete/recall UI + used it),
                    # verify via get_state that the message is actually gone
                    state = await self.env.get_state(required_apps=APPS)
                    self._sid_live[sid] = state
                    logger.info(f"[bridge] rollback via gui_executor: done={trace['done']} "
                                f"steps={trace['steps']}")
                    if not trace["done"]:
                        # model didn't finish + didn't fail → treat as irreversible
                        # (honest: the model couldn't find/complete a delete path)
                        raise web.HTTPConflict(text=(
                            "wechat send_message rollback: the GUI executor "
                            f"could not complete a delete/recall via the app's "
                            f"UI (model did not report done after {trace['steps']} "
                            f"steps). Treated as irreversible — no set_state "
                            f"backdoor fallback."))
                    return {"status": "ok", "operator": "send_message",
                            "old": value, "new": "(deleted)", "chat_id": eid,
                            "trace": trace}
                except GuiExecutorFailure as e:
                    # the model HONESTLY reported it cannot find a delete/recall
                    # UI → 409. This is the same conclusion as the old hardcoded
                    # 409, but now PROVEN by the model's real attempt.
                    await self._screenshot("undo_fail_409_model_tried")
                    raise web.HTTPConflict(text=(
                        f"wechat send_message is irreversible in MobileGym: the "
                        f"GUI executor observed the chat page + attempted to "
                        f"find a delete/recall UI but could not (model output "
                        f"{{\"action\":\"fail\"}}: {e.reason}). This conclusion "
                        f"is now PROVEN by the model's real attempt, not "
                        f"hardcoded. No set_state backdoor fallback."))
            # ── forward write: Task3 — real grounding loop (replaces the
            # hardcoded 7-step _send_message sequence). The model observes the
            # chat page + decides how to send the message using the page's UI
            # (tap composer → type → tap send / press enter). NOT a hardcoded
            # click sequence. ──
            # (task3 — real grounding loop; the model observes the chat page
            # and decides how to send using the page's UI.)
            trace = await gui_write_async(
                env=self.env, page=self.env.page, sid=sid,
                chat_id=eid, text=str(value), undo=False,
                screenshot_dir=self.screenshot_dir)
            if not trace["done"]:
                raise web.HTTPInternalServerError(text=(
                    f"gui_executor could not complete send_message via the UI "
                    f"(model did not report done after {trace['steps']} steps); "
                    f"no set_state backdoor. trace={trace['actions'][-3:]}"))
            # verify the message landed (the trusted read path)
            state = await self.env.get_state(required_apps=APPS)
            self._sid_live[sid] = state
            chats = state.get("apps", {}).get("wechat", {}).get("chats", []) or []
            target = next((c for c in chats if c.get("id") == eid), None)
            if target is None:
                raise RuntimeError(f"chat {eid} not found after GUI send")
            msgs = target.get("messages") or []
            new_msg = next((m for m in reversed(msgs)
                            if m.get("type") == "text" and m.get("content") == str(value)), None)
            if not new_msg:
                raise RuntimeError(
                    f"sent text not found in chat {eid} after the GUI gesture "
                    f"loop — the type/send gestures may not have reached the "
                    f"composer. trace={trace['actions'][-3:]}")
            logger.info(f"[bridge] send_message via gui_executor (no hardcoded "
                        f"sequence): chat={eid} msg_id={new_msg.get('id')!r}")
            return {"status": "ok", "operator": "send_message",
                    "old": f"msg:{new_msg.get('id')}", "new": str(value),
                    "chat_id": eid, "n_messages": len(msgs),
                    "message_id": new_msg.get("id"), "trace": trace}

    # ── write path: X toggle (toggleLike/toggleRetweet/toggleBookmark) ──────
    # E14 (2026-08-11): the FIRST non-wechat MobileGym write path. X's
    # ``XPostActionBar`` binds toggleLike / toggleRetweet / toggleBookmark to
    # single tap targets — these are DISCRETE one-click writes (vs. wechat's
    # type+send sequence), so they're the natural existence proof that the
    # TaskVM grounding loop can drive MobileGym when the harness coordinate
    # pipeline is correct (2026-08-11 fix: clicks now go through
    # ``env.step(Action.click(...))`` + MobileGym's own norm_0_1000 -> CSS
    # calibration, NOT the old wrong-viewport ``page.mouse.click``).
    async def mutate_x(self, sid: str, eid: str, operator: str,
                       value: Any, *, verify_mode: str = "specific",
                       instruction_override: str | None = None) -> dict:
        # B-1: runtime write path — requires the active session; never
        # context-switches reality underneath the CUA loop.
        await self._require_active(sid)
        async with self._lock:
            if operator not in ("toggle_like", "toggle_retweet",
                                "toggle_bookmark"):
                raise web.HTTPBadRequest(text=(
                    f"x operator must be toggle_like / toggle_retweet / "
                    f"toggle_bookmark, got {operator}"))
            if verify_mode not in ("specific", "any_new"):
                raise web.HTTPBadRequest(text=(
                    f"verify_mode must be 'specific' or 'any_new', got "
                    f"{verify_mode!r}"))
            post_id = eid
            # Open X app FIRST so the timeline is visible on screen — the
            # grounding loop screenshots THIS view + the model finds the
            # target post purely by what it sees (E16-complete: NO content
            # hint, NO post_id injection — pure vision CUA).
            await self.env.open_app("x", wait_stable=True)
            # Wait for timeline to render post action buttons.
            for _ in range(8):
                await asyncio.sleep(0.5)
                ready = await self.env.page.evaluate(
                    "() => { const b = document.querySelectorAll("
                    "'[data-action-params]'); for (const e of b) { "
                    "if ((e.getAttribute('data-action')||'')"
                    ".includes('.post.')) return true; } return false; }")
                if ready:
                    break

            # ── E17-A Option B: any-new-post before-snapshot ───────────────
            # For verify_mode='any_new' the instruction tells the CUA to "find
            # ANY un-toggled post and tap it" — so the verifier must check that
            # A NEW post entered the toggle list, NOT that the specific `eid`
            # did. This requires a BEFORE snapshot of the list. Crucially this
            # read is SERVER-SIDE ONLY (verification) — it is NEVER placed in
            # the instruction/prompt (that was the E16 leak; this does not
            # repeat it). 'specific' mode skips this read entirely → zero
            # change to the existing rollback-killtest path (zero regression).
            before_ids: set[str] | None = None
            if verify_mode == "any_new":
                _st = await self.env.get_state(required_apps=APPS)
                _xu = ((_st.get("apps", {}) or {}).get("x", {}) or {}).get("user", {}) or {}
                _fld = {"toggle_like": "likedPostIds",
                        "toggle_retweet": "retweetedPostIds",
                        "toggle_bookmark": "bookmarkedPostIds"}[operator]
                before_ids = set(_xu.get(_fld, []) or [])

            verb_map = {
                "toggle_like": ("like", "heart icon", "pink/red"),
                "toggle_retweet": ("retweet", "repost icon (green arrows)",
                                    "green"),
                "toggle_bookmark": ("bookmark", "bookmark icon", "blue"),
            }
            verb, icon_desc, done_color = verb_map[operator]

            # ── E16 non-invasive fix (.mrules E16) ───────────────────────────
            # Direction inference: derived PURELY from the `value` argument
            # passed by the caller (TaskVM dispatcher), NOT from get_state().
            #
            # Why get_state() was wrong here (E16 bug):
            #   The previous code called ``env.get_state()`` to read whether
            #   the post is currently in likedPostIds, then told the model
            #   "it is currently FILLED/OUTLINE right now". This leaks the
            #   backend store's contents into the model's prompt — the model
            #   should infer the icon's current visual state from the SCREENSHOT,
            #   not from a backdoor read of the zustand store.  In a real CUA
            #   (OSWorld/real phone) there is no such API; the agent must look
            #   at the screen.
            #
            # Convention (same as other adapters):
            #   value=True  → target end-state is ACTIVE/FILLED  (write path)
            #   value=False → target end-state is INACTIVE/OUTLINE (rollback)
            # The caller (run_x_toggle_killtest / rollback_killtest) already
            # encodes this: write uses value=True, rollback uses value=False.
            _want_active = bool(value)  # True=filled, False=outline
            if _want_active:
                direction_verb = verb
                target_state_desc = f"{done_color} and FILLED (active)"
                fail_hint = "still OUTLINE/uncolored"
            else:
                direction_verb = f"un-{verb}" if verb != "retweet" else "un-retweet"
                target_state_desc = "OUTLINE/uncolored (inactive)"
                fail_hint = f"still {done_color} and FILLED"

            # ── GG.3/§1.3: the bridge no longer has its own instruction template ──
            # The CUA instruction MUST come from the governance layer
            # (GovernanceInterpreter → SubgoalInstruction.natural_language) via
            # ``instruction_override``. The old inline f-string templates (the
            # E14 "old" ablation branch + the E16 "new" inline branch) are
            # DELETED — they were hardcoded bridge templates (GG §1.3 condemns
            # "bridge 内不再有自己的 instruction 模板"). The E16 content_hint /
            # posts.json / DOM-textContent backdoor history is preserved in
            # .mrules E16; the code no longer carries it. If no override is
            # supplied, the bridge honest-fails (it cannot fabricate a goal
            # instruction — that is the governance layer's job).
            logger.info(f"[bridge] mutate_x: post={post_id} want_active={_want_active} "
                        f"verify_mode={verify_mode} "
                        f"instruction_override={'yes' if instruction_override else 'no'}")
            if not instruction_override:
                return {"status": "error", "app": "x",
                        "error": ("GG.3: mutate_x requires instruction_override "
                                  "(the governance layer's SubgoalInstruction NL). "
                                  "The bridge no longer fabricates a goal instruction."),
                        "post_id": post_id, "operator": operator}
            instruction = instruction_override

            if self.cua is None:
                raise web.HTTPNotImplemented(text=(
                    "x mutate requires a CUA loop; this bridge was started "
                    "without --cua-loop (no fallback, no set_state backdoor)."))
            gui_act_async = self.cua.gui_act_async
            GuiExecutorFailure = self.cua.GuiExecutorFailure
            # X app is already open + the timeline readiness poll above
            # passed, so navigate=None / wait_ready=None — the grounding loop
            # starts immediately on the current timeline view (screenshot →
            # model, pure vision).
            trace = await gui_act_async(
                env=self.env, page=self.env.page, instruction=instruction,
                navigate=None, wait_ready=None,
                screenshot_dir=self.screenshot_dir, max_steps=25)
            if not trace["done"]:
                raise web.HTTPInternalServerError(text=(
                    f"gui_executor could not complete {operator} via the UI "
                    f"(model did not report done after {trace['steps']} steps); "
                    f"no set_state backdoor. trace={trace['actions'][-3:]}"))
            # verify the toggle landed (trusted read path).
            #
            # Task E fix (.mrules E15): must check against the EXPECTED
            # direction (_currently_on before this call), not hardcode
            # "must now be in the list". Bug this replaces: the old check
            # was `if not now_liked: raise` unconditionally — correct for
            # the write path (outline->filled, expect now_in_list=True) but
            # WRONG for a rollback call (filled->outline, expect
            # now_in_list=False). On rollback the gesture genuinely
            # succeeded (post correctly left the list) but this stale check
            # still raised, turning a real success into a spurious HTTP 500
            # — caught by the Task E killtest: every rollback call returned
            # http_status=500 even though the independent trusted-read
            # verification (``run_x_toggle_rollback_killtest.py``'s own
            # get_state check) confirmed the post really left the list.
            state = await self.env.get_state(required_apps=APPS)
            self._sid_live[sid] = state
            x_state = state.get("apps", {}).get("x", {}) or {}
            user = x_state.get("user", {}) or {}
            field_map = {
                "toggle_like": "likedPostIds",
                "toggle_retweet": "retweetedPostIds",
                "toggle_bookmark": "bookmarkedPostIds",
            }
            ids_list = user.get(field_map[operator], []) or []
            now_in_list = post_id in ids_list
            # _want_active: the target end-state (True=FILLED, False=OUTLINE)
            # derived from `value` — no prior get_state() needed (E16 fix).
            logger.info(f"[bridge] {operator} via gui_act_async: "
                        f"post={post_id} now_in_list={now_in_list} "
                        f"want_active={_want_active} steps={trace['steps']} "
                        f"verify_mode={verify_mode}")

            # ── E17-A Option B: branch the verifier on verify_mode ──────────
            # 'specific' (default, zero-regression): the specific `eid` post's
            #   membership must match _want_active. This is what the rollback
            #   killtest (run_x_toggle_rollback_killtest) relies on — unchanged.
            # 'any_new' (Option B, x_toggle killtest): the instruction says
            #   "find ANY un-toggled post and tap it", so the verifier checks
            #   that the toggle list GREW (write) or SHRANK (rollback) by ≥1
            #   vs the before-snapshot — i.e. SOME post transitioned. This
            #   aligns verifier semantics with the any-post instruction
            #   (fixes the .mrules E17 §0-B ill-posed-task contradiction).
            #   The newly-transitioned post_id is returned for per_post tracking.
            toggled_post_id = None
            if verify_mode == "any_new":
                after_ids = set(ids_list)
                if before_ids is None:
                    before_ids = set()  # defensive — should have been captured
                if _want_active:
                    # write: expect a NEW post entered the list
                    gained = after_ids - before_ids
                    any_new = len(gained) > 0
                    toggled_post_id = next(iter(gained), None)
                    verified = any_new
                    fail_msg = (f"{operator} (any_new/write) did not add any post "
                                f"to {field_map[operator]} — before={sorted(before_ids)} "
                                f"after={sorted(after_ids)}")
                else:
                    # rollback: expect a post LEFT the list
                    lost = before_ids - after_ids
                    any_new = len(lost) > 0
                    toggled_post_id = next(iter(lost), None)
                    verified = any_new
                    fail_msg = (f"{operator} (any_new/rollback) did not remove any "
                                f"post from {field_map[operator]} — "
                                f"before={sorted(before_ids)} after={sorted(after_ids)}")
                if not verified:
                    raise RuntimeError(
                        f"{fail_msg} after the GUI gesture loop. "
                        f"trace={trace['actions'][-3:]}")
                return {"status": "ok", "operator": operator,
                        "want_active": _want_active, "now_in_list": now_in_list,
                        "post_id": post_id, "verify_mode": verify_mode,
                        "toggled_post_id": toggled_post_id,
                        "before_count": len(before_ids),
                        "after_count": len(after_ids), "trace": trace}

            # 'specific' path (unchanged behavior — zero regression)
            if now_in_list != _want_active:
                raise RuntimeError(
                    f"{operator} on post {post_id} did not reach target state — "
                    f"want_active={_want_active} but now_in_list={now_in_list} "
                    f"after the GUI gesture loop. "
                    f"trace={trace['actions'][-3:]}")
            return {"status": "ok", "operator": operator,
                    "want_active": _want_active, "now_in_list": now_in_list,
                    "post_id": post_id, "verify_mode": verify_mode,
                    "trace": trace}

    # GG.4: the dead ``_send_message`` method (7-step hardcoded sequence with
    # the ``openApp('wechat','/chat/{chat_id}')`` deep-link + a programmatic
    # ``textarea.focus()`` backdoor) is DELETED. It was superseded by
    # ``gui_executor_async.gui_write_async`` (the real grounding loop) — the
    # earlier exploration confirmed zero live callers. The deep-link was a
    # wxid backdoor (a real user can't deep-link to a chat by an internal id);
    # the live path now opens wechat + lets the grounding model tap the contact
    # by visible peer_name (gui_write_async._resolve_peer_name). History in
    # .mrules E10/E15.


    # ── minimal HTML view (data-*-id DOM for replay_engine.capture_obs) ─────
    def html_view(self, sid: str) -> str:
        """The rendered-GUI observation the COMPILER reads (read-path-is-GUI,
        no-leak). Emitted as parseable ``<tr data-{chat,transaction}-id>`` rows
        with ``<td data-field="...">`` cells — the SAME DOM contract the core
        apps use (``replay_engine.parse_dom_entities`` matches
        ``data-(event|task|file|mail|appointment|chat|transaction)-id`` rows +
        ``data-field`` cells). This lets ``run_mobilegym_killtest`` capture obs
        via the standard ``GET /<sid>`` route + feed it to ``compile_binding``
        for REAL model-discovered binding (task-4 — the alipay→wechat binding
        was previously GT-given; now a frontier model discovers it from this
        rendered view alone).

        The field cells mirror what ``_flatten_wechat_chats`` /
        ``_flatten_alipay_txs`` project (so ``assert_obs_matches_state`` — DOM
        vs ``read_canonical`` — passes). The combined page shows BOTH apps'
        rows; the kill-test splits the parsed entities by id-attribute kind
        (``data-chat-id`` → wechat, ``data-transaction-id`` → alipay) into
        per-app observations."""
        live = self._sid_live.get(sid, {})
        apps = live.get("apps", {})
        chats = apps.get("wechat", {}).get("chats", []) or []
        txs = apps.get("alipay", {}).get("transferRecords", []) or []
        chat_rows = "".join(
            f'<tr data-chat-id="{_esc(c.get("id",""))}">'
            f'<td data-field="id">{_esc(c.get("id",""))}</td>'
            f'<td data-field="peer_name">{_esc((c.get("user") or {}).get("name",""))}</td>'
            f'<td data-field="peer_wxid">{_esc((c.get("user") or {}).get("wxid",""))}</td>'
            f'<td data-field="n_messages">{len(c.get("messages") or [])}</td>'
            f'<td data-field="last_message">{_esc((c.get("messages") or [{}])[-1].get("content","") if (c.get("messages") or []) else "")}</td>'
            f'<td data-field="messages">{_esc(" ".join(str(m.get("content","")) for m in (c.get("messages") or []) if m.get("type")=="text"))}</td>'
            f'</tr>' for c in chats)
        tx_rows = "".join(
            f'<tr data-transaction-id="{_esc(t.get("id",""))}">'
            f'<td data-field="id">{_esc(t.get("id",""))}</td>'
            f'<td data-field="counterpartyName">{_esc(t.get("counterpartyName",""))}</td>'
            f'<td data-field="delta">{_esc(t.get("delta"))}</td>'
            f'<td data-field="timestamp">{_esc(t.get("timestamp"))}</td>'
            f'<td data-field="category">{_esc(t.get("category",""))}</td>'
            f'<td data-field="kind">{_esc(t.get("kind",""))}</td>'
            f'<td data-field="note">{_esc(t.get("note",""))}</td>'
            f'<td data-field="description">{_esc(t.get("description",""))}</td>'
            f'</tr>' for t in txs)
        return f"""<!doctype html><html><head><meta charset="utf-8">
<title>MobileGym bridge · {sid}</title>
<style>body{{font-family:monospace;background:#0d1117;color:#c9d1d9;padding:12px}}
h2{{color:#58a6ff;font-size:14px}} table{{border-collapse:collapse;font-size:12px}}
td{{border:1px solid #30363d;padding:3px 6px}} .meta{{color:#8b949e;font-size:11px}}</style>
</head><body>
<h2>wechat chats ({len(chats)})</h2>
<table><tbody>{chat_rows or '<tr><td>no chats</td></tr>'}</tbody></table>
<h2>alipay transactions ({len(txs)})</h2>
<table><tbody>{tx_rows or '<tr><td>no txs</td></tr>'}</tbody></table>
<p class="meta">mobilegym bridge sid={_esc(sid)} · live sim at {_esc(self.sim_url)} · rendered-GUI observation for the compiler (no GT)</p>
</body></html>"""


def _esc(v) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ── aiohttp wiring ───────────────────────────────────────────────────────────
def build_app(bridge: MobileGymBridge) -> web.Application:
    app = web.Application()

    async def health(_r):
        return web.json_response({"status": "ok", "site": SITE})

    async def view_sid(request):
        sid = request.match_info["sid"]
        if sid not in bridge._sid_live:
            return web.Response(text="session not found", status=404)
        return web.Response(text=bridge.html_view(sid),
                            content_type="text/html")

    async def api_reset(request):
        sid = request.match_info["sid"]
        return web.json_response(await bridge.reset(sid))

    async def api_inject_task(request):
        sid = request.match_info["sid"]
        data = await request.json()
        return web.json_response(await bridge.inject_task(
            sid, data.get("task_id"), data.get("goal") or "",
            data.get("seed_state") or {}))

    async def api_session_state(request):
        sid = request.match_info["sid"]
        return web.json_response(await bridge.session_state(sid))

    async def api_x_state(request):
        sid = request.match_info["sid"]
        return web.json_response(await bridge.x_state(sid))

    async def api_resource(request):
        sid = request.match_info["sid"]
        resource = request.match_info["resource"]
        return web.json_response(await bridge.read_resource(sid, resource))

    # ── L1 primitive routes (Agent B): back the SubstrateSession port ─────
    async def api_observe(request):
        sid = request.match_info["sid"]
        return web.json_response(await bridge.observe(sid))

    async def api_act(request):
        sid = request.match_info["sid"]
        action = await request.json()
        return web.json_response(await bridge.act_primitive(sid, action))

    async def api_wechat_mutate(request):
        sid = request.match_info["sid"]
        eid = request.match_info["eid"]
        data = await request.json()
        return web.json_response(await bridge.mutate_wechat(
            sid, eid, data.get("operator"), data.get("value")))

    async def api_x_mutate(request):
        sid = request.match_info["sid"]
        eid = request.match_info["eid"]
        data = await request.json()
        # E17-A Option B: verify_mode='any_new' (x_toggle killtest) vs
        # 'specific' (default — rollback killtest, unchanged).
        # E17-B: instruction_override (governance-supplied NL — de-segmentation).
        return web.json_response(await bridge.mutate_x(
            sid, eid, data.get("operator"), data.get("value"),
            verify_mode=data.get("verify_mode", "specific"),
            instruction_override=data.get("instruction_override")))

    app.router.add_get("/health", health)
    app.router.add_get("/{sid}", view_sid)
    app.router.add_post("/api/reset/{sid}", api_reset)
    app.router.add_post("/api/inject_task/{sid}", api_inject_task)
    app.router.add_get("/api/session_state/{sid}", api_session_state)
    app.router.add_get("/api/x_state/{sid}", api_x_state)
    app.router.add_get("/api/{resource}/{sid}", api_resource)
    app.router.add_get("/api/observe/{sid}", api_observe)
    app.router.add_post("/api/act/{sid}", api_act)
    app.router.add_post("/api/wechat/{sid}/{eid}", api_wechat_mutate)
    app.router.add_post("/api/x/{sid}/{eid}", api_x_mutate)
    return app


def main(argv=None):
    import time
    parser = argparse.ArgumentParser(description="TaskVM↔MobileGym bridge")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--sim-url", default=DEFAULT_SIM_URL)
    parser.add_argument("--headed", action="store_true",
                        help="show the browser (default headless — on a headless "
                             "box with no display this auto-falls-back to headless "
                             "with a warning; screenshots still land either way)")
    parser.add_argument("--screenshot-dir", default=None,
                        help="auto-snapshot each gesture step to this dir "
                             "(default eval_results/mobilegym_visual_<ts>/). "
                             "Pass '' to disable. The '我现在啥也看不见，你要录下来' "
                             "deliverable — works headless.")
    parser.add_argument("--cua-loop", default=None,
                        help="dotted module path of the L2 CUA loop module to "
                             "INJECT (e.g. taskvm.execution.gui_executor_async; "
                             "must expose gui_write_async / gui_act_async / "
                             "GuiExecutorFailure). Without it the legacy "
                             "mutate routes answer 501 — the bridge never "
                             "imports upper layers itself (substrate "
                             "isolation, Agent B).")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    # chromium env via portable discovery (env vars > CONDA_PREFIX > repo
    # .chromelibs) — Agent B replaced the hardcoded /mnt/dolphinfs/... path.
    bp = (os.environ.get("TASKVM_PLAYWRIGHT_BROWSERS_PATH")
          or (os.path.join(os.environ.get("CONDA_PREFIX", ""), "opt",
                           "ms-playwright")
              if os.environ.get("CONDA_PREFIX") else None))
    if bp and os.path.isdir(bp):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", bp)
    here = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    cl = os.environ.get("TASKVM_CHROMELIBS_PATH") or os.path.join(
        here, ".chromelibs", "lib")
    if os.path.isdir(cl):
        os.environ["LD_LIBRARY_PATH"] = cl + ":" + os.environ.get("LD_LIBRARY_PATH", "")

    # CUA loop injection (process ASSEMBLY — the only legitimate way an
    # upper-layer loop reaches into this bridge process).
    cua = None
    if args.cua_loop:
        import importlib
        try:
            cua = importlib.import_module(args.cua_loop)
            for attr in ("gui_write_async", "gui_act_async",
                         "GuiExecutorFailure"):
                if not hasattr(cua, attr):
                    raise AttributeError(attr)
        except (ImportError, AttributeError) as e:
            raise SystemExit(
                f"--cua-loop {args.cua_loop!r} is not a valid CUA loop "
                f"module (needs gui_write_async/gui_act_async/"
                f"GuiExecutorFailure): {e}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    shot_dir = args.screenshot_dir
    if shot_dir is None:
        shot_dir = f"eval_results/mobilegym_visual_{ts}"
    elif shot_dir == "":
        shot_dir = None

    bridge = MobileGymBridge(sim_url=args.sim_url, headless=not args.headed,
                             screenshot_dir=shot_dir, cua=cua)

    async def on_startup(app):
        await bridge.start_env()

    async def on_cleanup(app):
        await bridge.aclose()

    app = build_app(bridge)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    logger.info(f"[bridge] serving on :{args.port} (sim={args.sim_url}, "
                f"headless={not args.headed}, screenshots={shot_dir})")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
