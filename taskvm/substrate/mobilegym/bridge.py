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

**Non-invasive write/rollback boundary (load-bearing).** The task-level
write path is the GENERIC ``mutate`` route: ``gui_act_async`` — a REAL
grounding loop (screenshot → model → ``env.step(Action.click/type/swipe)``
gestures) driven by a natural-language intent composed from caller-supplied
business language (app display name + user-visible entity label + intent).
NO ``set_state``, no operator enum, no per-app branch, no internal id.

Undo runs the same loop with an undo-framed instruction — the model observes
the live screen and TRIES to find a delete/recall/restore UI. If the app's
UI offers no such path, the loop fails and the bridge raises the honest
``web.HTTPConflict`` (409 irreversible): irreversibility is PROVEN by the
model's real attempt, never pre-judged by a hardcoded per-app verdict. The
bridge does NOT fall back to ``set_state`` to fake a byte-exact restore
(that would undermine the compensation claim — a backdoor rollback proves
"we have debug permission," not "TaskVM compensates").

After every write loop, a FRESH observation goes to the INJECTED verifier
(``--verifier``, the taskvm.verifier ModelVerifier contract adapted at
process assembly) which judges the business intent against what the screen
now shows; only its ``changed`` verdict returns ``status:"ok"``.

Routes (mirror the Drive app's contract, app-namespaced):
    GET  /health                              → {"status":"ok","site":"mobilegym"}
    GET  /<sid>                               → minimal HTML view (generic, catalog-driven)
    POST /api/reset/<sid>                     → env.reset(app_ids=<all store apps>) [SETUP]
    POST /api/inject_task/<sid>               → env.set_state(seed, deep=True) [SETUP-ONLY]
    GET  /api/observe/<sid>                   → screenshot+visible text (RUNTIME; requires active sid)
    POST /api/act/<sid>                       → env.step(real gesture) (RUNTIME; requires active sid)
    GET  /api/app_state/<sid>/<app_id>        → raw store state of ANY catalog app [oracle]
    GET  /api/os_state/<sid>                  → OS runtime state (tasks/settings/...) [oracle]
    GET  /api/session_state/<sid>             → generic per-app summary (legacy summary block kept as compat alias)
    GET  /api/wechat_chats/<sid>              → flattened wechat chats (legacy compat alias)
    GET  /api/alipay_transactions/<sid>       → flattened alipay transferRecords (legacy compat alias)
    GET  /api/x_posts/<sid>                   → X post rows (legacy compat alias; non-invasive store read)
    GET  /api/x_state/<sid>                   → X toggle lists (legacy compat alias)
    POST /api/mutate/<sid>                    → GENERIC model-driven write: {"app","entity_ref","intent"}
                                                via gui_act_async + ModelVerifier gate; undo → 409 if
                                                irreversible. App-agnostic — no operator enum, no per-app
                                                branch (catalog validation only).
    POST /api/wechat/<sid>/<eid>              → 302 → /api/mutate/<sid> (compat alias, removal scheduled)
    POST /api/x/<sid>/<eid>                   → 302 → /api/mutate/<sid> (compat alias, removal scheduled)

B-1 (Oracle audit 2026-08-15): ONE active experimental session at a time.
The evaluation/setup plane (reset/inject_task/oracle reads) activates a
sid; the runtime plane (observe/act/mutate routes) REQUIRES the active sid
and honestly refuses a mismatch (409 session mismatch) — the runtime never
switches reality via env.reset/get_state/set_state underneath the caller.

B-04 (Oracle audit — Non-invasive MobileGym evaluation oracle, fixed
2026-08-17): the X oracle read (``GET /api/x_posts/<sid>``, backing
``MobileGymEvaluationEnvironment.oracle_state`` for the "x" app) used to
call ``env.open_app("x", wait_stable=True)`` + ``asyncio.sleep(1.5)`` on
EVERY read — switching the live sim's foreground app and burning wall
clock just to grade it, polluting the very screen/latency the runtime is
being measured on. Fixed: the grading-relevant fields (is_liked/
is_retweeted/is_bookmarked — the only fields any checkpoint criterion
reads) now come straight from the zustand store dict ``env.get_state()``
already returns (``_x_toggle_rows`` — a pure projection, zero env calls,
same data ``x_state()`` has always used). The non-grading ``content``
preview field is best-effort from whatever ``[data-post-id]`` DOM is
ALREADY rendered (no navigation, no sleep); it is honestly blank when X
is not currently the foreground. See ``_x_oracle_rows_noninvasive``.
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

from taskvm.substrate.mobilegym.app_catalog import (
    ALL_APP_IDS,
    STORE_APP_IDS,
    get_display_name,
    is_valid_app_or_raise,
)

logger = logging.getLogger(__name__)

SITE = "mobilegym"
DEFAULT_PORT = 3019
# Catalog-driven discovery (app_catalog is the single source of truth —
# generated from the sim's own manifests):
#   ALL_APPS — every registered app (open whitelist; storeless apps like
#              calculator/theme_store included: their state IS the screen)
#   APPS     — the store-backed subset, for get_state(required_apps=...)
ALL_APPS = list(ALL_APP_IDS)
APPS = list(STORE_APP_IDS)
# The Vite dev/preview server URL the env drives.
DEFAULT_SIM_URL = "http://localhost:3000"


class MobileGymBridge:
    """Holds one MobileGymEnv + per-sid live-state cache, served by aiohttp."""

    def __init__(self, sim_url: str, headless: bool = True,
                 screenshot_dir: str | None = None,
                 cua: "CuaLoopModule | None" = None,
                 verifier: "VerifierContract | None" = None):
        self.sim_url = sim_url
        self.headless = headless
        self.screenshot_dir = screenshot_dir    # auto step shots
        self._shot_counter = 0                  # monotonic (Date.now banned)
        # ── substrate isolation: the CUA loops and the ModelVerifier are
        # INJECTED, never imported. The substrate layer may not import upper
        # layers (architecture gate); ``cua`` is any object exposing
        # ``gui_write_async`` / ``gui_act_async`` / ``GuiExecutorFailure``;
        # ``verifier`` is any object exposing an async
        # ``verify_intent(observation, intent) -> {"verdict", "evidence"}``
        # (the taskvm.verifier.ModelVerifier three-state contract, adapted
        # at process assembly). Both are passed at PROCESS ASSEMBLY time via
        # ``--cua-loop`` / ``--verifier``. Without them the write routes
        # answer 501 (honest unavailability — no fallback).
        self.cua = cua
        self.verifier = verifier
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
                    "(or start this bridge headless and drive it via the "
                    "L1 observe/act port).")
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
            known = [a for a in ALL_APPS if a == target]
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
            # B-04 (Oracle audit 2026-08-15/17 — Non-invasive MobileGym
            # evaluation oracle): the OLD path called
            # ``env.open_app("x", wait_stable=True)`` + ``asyncio.sleep(1.5)``
            # here — switching the live sim's FOREGROUND app + burning wall
            # clock BEFORE every oracle read. That is the exact violation the
            # audit flagged: "per-op 判卷...改变前台 app...改变下一轮模型截图
            # ...把 oracle 时间算进 projection latency" — a judge that moves
            # furniture in the room it is grading.
            #
            # Fix (priority 1 — read the in-memory store, no UI at all):
            # the fields that actually matter for grading (is_liked /
            # is_retweeted / is_bookmarked — the checkpoint criterion in
            # ``mobilegym_fixtures.SOCIAL_MORNING_BRIEF`` only ever asserts
            # ``{"liked": True}``) live in ``apps.x.user.*PostIds`` — plain
            # zustand-store DATA, already reachable from the
            # ``env.get_state()`` call two lines above. No navigation, no
            # sleep, no foreground change: this is what ``x_state()`` below
            # already proves is possible.
            #
            # The ``content`` field (post text preview) is NOT store data —
            # it lives in a base dataset (posts.json, loaded via preload())
            # that MobileGym never puts in the zustand store, so the ONLY
            # way to read it is the rendered DOM (see
            # ``_flatten_x_posts_async``'s docstring). Rather than force a
            # navigation to get it, this is priority-3 non-invasive: read
            # WHATEVER is already on the live screen right now (a passive
            # observation identical in kind to "the agent glancing at its
            # own last screenshot"), and if X's timeline is not currently
            # the foreground view, honestly leave ``content`` blank instead
            # of manufacturing a foreground switch to fetch it. The oracle
            # never calls ``open_app`` and never sleeps.
            rows = await self._x_oracle_rows_noninvasive(apps.get("x", {}))
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

    @staticmethod
    def _x_toggle_rows(x_state: dict) -> dict[str, dict]:
        """PURE, read-only, store-only projection of the X toggle lists
        (B-04 fix, priority 1). ``apps.x.user.{liked,retweeted,bookmarked}
        PostIds`` are plain in-memory zustand-store DATA — the SAME dict
        ``env.get_state()`` already returns, no DOM/UI involved at all.
        This is the ONLY thing MobileGym oracle grading actually checks
        (``mobilegym_fixtures.SOCIAL_MORNING_BRIEF``'s checkpoint criterion
        is ``{"x": {POST_ID: {"liked": True}}}`` — never ``content``), so
        it is the load-bearing half of the oracle read and it is 100%
        non-invasive: a plain dict projection, zero env/page calls, keyed
        by post id -> {is_liked, is_retweeted, is_bookmarked}."""
        user = x_state.get("user", {}) or {}
        liked = set(user.get("likedPostIds", []) or [])
        retweeted = set(user.get("retweetedPostIds", []) or [])
        bookmarked = set(user.get("bookmarkedPostIds", []) or [])
        out: dict[str, dict] = {}
        for pid in liked | retweeted | bookmarked:
            out[pid] = {
                "is_liked": pid in liked,
                "is_retweeted": pid in retweeted,
                "is_bookmarked": pid in bookmarked,
            }
        return out

    async def _x_oracle_rows_noninvasive(self, x_state: dict) -> list[dict]:
        """B-04 (Oracle audit — Non-invasive MobileGym evaluation oracle)
        replacement for the deleted ``_flatten_x_posts_async``. NEVER
        calls ``env.open_app`` and NEVER sleeps — the oracle must not move
        the foreground app or burn wall-clock time the runtime's projection
        latency would otherwise be charged for (the exact B-04 complaint).

        Priority-1 half (grading-relevant, non-invasive by construction):
        the toggle booleans come STRAIGHT from the zustand store
        (``_x_toggle_rows`` — a pure dict projection, zero env calls).

        Priority-3 half (``content``, non-grading, best-effort): the post
        TEXT lives only in a base dataset MobileGym renders into the DOM
        and never puts in the store (see the historical docstring this
        replaces, preserved below), so there is no store path for it. Instead
        of forcing a foreground switch to fetch it, this reads WHATEVER
        ``data-post-id`` cards are ALREADY rendered on the live page right
        now — a passive observation, not a navigation. If the X timeline
        happens to already be the foreground (e.g. the agent itself just
        opened X), the content comes along for free with zero extra
        side-effects; if X is not currently on screen, ``content`` is
        honestly left as "" (not fabricated, not fetched via a manufactured
        app-switch) and the row still carries the (store-sourced) toggle
        booleans, which is everything grading needs.

        Historical note (content DOM-read mechanics, unchanged from the
        pre-B-04 ``_flatten_x_posts_async``): X's post table lives in a base
        dataset (posts.json, loaded via preload()) that is NOT part of the
        zustand store — so state['apps']['x']['posts'] is always an empty
        dict. The posts ARE rendered in the DOM, each in a
        ``<div data-post-id="p_...">`` container (Task B, 2026-08-12 fix —
        added to ``XTimelinePostCard.tsx``'s root div). Reading that
        attribute directly (one query, no ancestor walking) avoids the E14
        cross-contamination bug (.mrules Task B) where a ``closest(...)``
        ancestor walk from the action-bar buttons resolved to a shared
        grandparent across sibling posts."""
        toggle_by_id = self._x_toggle_rows(x_state)
        content_by_id: dict[str, str] = {}
        page = getattr(self.env, "page", None)
        if page is not None:
            try:
                dom_posts = await page.evaluate("""() => {
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
            except Exception as e:
                # Honest best-effort: a transient page/evaluate failure
                # (e.g. mid-navigation) must not crash grading — it just
                # means content stays blank for this read, same as X not
                # being foreground at all.
                logger.warning(f"[bridge] x oracle content DOM read "
                                f"skipped (non-fatal, no UI action taken): {e}")
                dom_posts = []
            for p in dom_posts or []:
                pid = p.get("id")
                if pid:
                    content_by_id[pid] = str(p.get("content", ""))[:80]
        # union of ids known via toggle state OR currently visible on screen
        # — an id can be visible-but-untoggled (freshly rendered, never
        # liked/retweeted/bookmarked) or toggled-but-not-currently-visible
        # (scrolled off / X not foreground); both are honest partial views.
        all_ids = set(toggle_by_id) | set(content_by_id)
        rows = []
        for pid in all_ids:
            t = toggle_by_id.get(pid, {"is_liked": False, "is_retweeted": False,
                                        "is_bookmarked": False})
            rows.append({
                "id": pid,
                "content": content_by_id.get(pid, ""),
                "is_liked": t["is_liked"],
                "is_retweeted": t["is_retweeted"],
                "is_bookmarked": t["is_bookmarked"],
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
        post ids) for the given session — an independent trusted read path
        for verifying that a toggle write/rollback actually landed via
        ``get_state``. This is a plain read (no mutation, no set_state), so
        it does not touch the non-invasive write/rollback boundary
        documented above. Legacy compat route: the generic oracle reads
        are ``app_state`` / ``os_state``."""
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
        """Generic catalog-driven summary: for every app present in the
        live store, the top-level collection counts (``{field: n}`` for
        each list/dict-valued field). No per-app field names are hardcoded
        here — an app joins the summary by HAVING a store, not by being
        enumerated. The legacy ``summary`` block (n_chats/n_contacts/n_tx/
        balance) is retained as a compat alias for existing consumers
        during the compat window (removal announced in the R3 report)."""
        await self._activate(sid)
        state = self._sid_live.get(sid) or await self.env.get_state(required_apps=APPS)
        apps = state.get("apps", {})
        apps_summary: dict[str, dict[str, int]] = {}
        for app_id, app_state in apps.items():
            if not isinstance(app_state, dict):
                continue
            counts = {field: len(value)
                      for field, value in app_state.items()
                      if isinstance(value, (list, dict))}
            if counts:
                apps_summary[app_id] = counts
        wechat = apps.get("wechat", {}) or {}
        alipay = apps.get("alipay", {}) or {}
        return {"site": SITE, "sid": sid,
                "has_task": True,
                "apps": apps_summary,
                # legacy compat projection (fixed consumers read these)
                "summary": {"n_chats": len(wechat.get("chats", []) or []),
                            "n_contacts": len(wechat.get("contacts", []) or []),
                            "n_tx": len(alipay.get("transferRecords", []) or []),
                            "balance": (alipay.get("balance", {}) or {}).get("total")}}

    # ── generic oracle reads (evaluation/setup plane, catalog-driven) ──────
    async def app_state(self, sid: str, app_id: str) -> dict:
        """Raw store state of ANY catalog app (evaluation/benchmark only).

        App-agnostic: the app_id is validated against the catalog (404 for
        unknown apps) and the store slice is returned verbatim. Storeless
        apps (calculator, theme_store) honestly return an empty state —
        they are fully GUI-drivable, they simply have no backend store
        (their state IS the screen)."""
        try:
            is_valid_app_or_raise(app_id)
        except ValueError as e:
            raise web.HTTPNotFound(text=str(e))
        await self._activate(sid)
        state = self._sid_live.get(sid) or await self.env.get_state(required_apps=APPS)
        apps = state.get("apps", {})
        return {"sid": sid, "app": app_id, "state": apps.get(app_id, {}) or {}}

    async def os_state(self, sid: str) -> dict:
        """OS runtime state (tasks, activeAppId, settings, notifications,
        home_screen) — the part of the phone world that belongs to no app.
        Evaluation/setup plane read, plain store projection."""
        await self._activate(sid)
        state = self._sid_live.get(sid) or await self.env.get_state(required_apps=APPS)
        return {"sid": sid, "os": state.get("os", {}) or {}}

    # ── generic write path: model-driven, app-agnostic (no operator enum) ──
    async def mutate(self, sid: str, app: str, entity_ref: str, intent: str,
                     *, undo: bool = False) -> dict:
        """The ONE task-level write path: ``POST /api/mutate/<sid>``.

        Contract: ``{"app": app_id, "entity_ref": <user-visible entity
        label>, "intent": <natural-language intent>, "undo": bool}``.
        App-agnostic by construction — the only app knowledge is the
        catalog validation (any of the registered apps, storeless ones
        included: calculator's state IS the screen, the CUA drives it the
        same way). There is no operator enum, no per-app branch, no
        internal id: ``entity_ref`` is what a real user would read on the
        screen (a chat name, a post title), never a backend key.

        Semantics (honest-boundary preserved):
          * both the CUA loop and the ModelVerifier are INJECTED at process
            assembly; missing either → honest 501 BEFORE touching the world;
          * the write runs through ``gui_act_async`` — a real grounding
            loop (screenshot → model → env.step gestures). NO set_state
            backdoor, ever;
          * after the loop, a FRESH observation goes to the injected
            ModelVerifier against the business-language intent; ONLY the
            ``changed`` verdict returns ``status:"ok"`` (``not_yet`` /
            ``cannot_verify`` are honest non-ok statuses, not failures of
            the route);
          * ``undo=True`` runs the same loop with an undo-framed
            instruction — the model plans the undo gestures from the live
            screen. If no undo path exists in the app's UI, the loop fails
            and the bridge answers the honest 409 irreversible (this is an
            honesty boundary, not an enumeration).
        """
        # B-1: runtime write path — requires the active session; never
        # context-switches reality underneath the CUA loop.
        await self._require_active(sid)
        try:
            is_valid_app_or_raise(app)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        if not intent or not str(intent).strip():
            raise web.HTTPBadRequest(text="intent is required (natural language)")
        entity_ref = str(entity_ref or "").strip()
        intent = str(intent).strip()
        async with self._lock:
            if self.cua is None or self.verifier is None:
                # honest unavailability — the write route's contract is
                # "execute via real gestures AND verify via the model";
                # missing either component means we cannot honestly claim
                # ok, so we refuse BEFORE touching the world (no fallback,
                # no set_state backdoor).
                missing = ("--cua-loop" if self.cua is None else "",
                           "--verifier" if self.verifier is None else "")
                raise web.HTTPNotImplemented(text=(
                    "generic mutate requires BOTH an injected CUA loop "
                    "(--cua-loop) and an injected ModelVerifier (--verifier); "
                    f"missing: {[m for m in missing if m]}. No fallback."))
            gui_act_async = self.cua.gui_act_async
            GuiExecutorFailure = self.cua.GuiExecutorFailure
            # Bring the app to the foreground the way a real user would —
            # the grounding loop is pure-vision: it finds the target purely
            # from what the screen shows, never from backend data.
            await self.env.open_app(app, wait_stable=True)
            # The instruction is COMPOSED from caller-supplied business
            # language (app display name + visible entity label + NL
            # intent) — the bridge fabricates no task semantics of its own.
            app_name = get_display_name(app)
            if undo:
                instruction = (
                    f"在「{app_name}」中，撤销刚才对「{entity_ref}」执行的"
                    f"操作（{intent}）。请观察当前界面，找到可行的撤销方式"
                    f"（如删除、撤回、取消、还原等）并完成它；如果界面上"
                    f"不存在任何可行的撤销方式，请如实报告失败。")
                verify_intent = f"刚才对「{entity_ref}」执行的操作（{intent}）已经被撤销"
            else:
                instruction = (
                    f"在「{app_name}」中，找到「{entity_ref}」，然后执行："
                    f"{intent}。请通过界面上的真实操作完成，完成后报告。")
                verify_intent = f"在「{app_name}」中对「{entity_ref}」执行了：{intent}"
            trace = await gui_act_async(
                env=self.env, page=self.env.page, instruction=instruction,
                navigate=None, wait_ready=None,
                screenshot_dir=self.screenshot_dir, max_steps=25)
            if not trace["done"]:
                if undo:
                    # no undo path reachable through the app's own UI —
                    # the honest irreversibility verdict (NOT a backdoor
                    # set_state restore).
                    raise web.HTTPConflict(text=(
                        f"undo of {intent!r} is irreversible: the GUI "
                        f"executor could not complete an undo path via the "
                        f"app's UI (model did not report done after "
                        f"{trace['steps']} steps). No set_state backdoor "
                        f"fallback."))
                raise web.HTTPInternalServerError(text=(
                    f"gui_executor could not complete the intent via the UI "
                    f"(model did not report done after {trace['steps']} "
                    f"steps); no set_state backdoor. "
                    f"trace={trace['actions'][-3:]}"))
            try:
                # fresh observation → the injected ModelVerifier judges the
                # business intent against what the screen NOW shows
                obs = await self.observe(sid)
                verdict = await self.verifier.verify_intent(
                    observation=obs, intent=verify_intent)
            except GuiExecutorFailure as e:  # pragma: no cover — verifier is not the cua loop
                raise web.HTTPInternalServerError(
                    text=f"verifier raised: {e}")
            v = (verdict or {}).get("verdict")
            evidence = str((verdict or {}).get("evidence", ""))
            if v not in ("changed", "not_yet", "cannot_verify"):
                raise web.HTTPInternalServerError(text=(
                    f"injected verifier returned a malformed verdict "
                    f"{v!r} — expected one of changed/not_yet/"
                    f"cannot_verify with an evidence string."))
            status = "ok" if v == "changed" else v
            return {"status": status, "app": app, "entity_ref": entity_ref,
                    "intent": intent, "undo": undo,
                    "verify": {"verdict": v, "evidence": evidence},
                    "trace": trace}

    # The per-app operator-enum write paths (wechat send_message /
    # x toggle_*) are REPLACED by the generic ``mutate`` above. Their HTTP
    # routes answer 302 → /api/mutate/<sid> for the compat window (one
    # commit), then are deleted. The non-invasive read helpers they shared
    # (``_flatten_wechat_chats`` / ``_flatten_alipay_txs`` /
    # ``_x_toggle_rows``) stay — the legacy oracle read routes still use
    # them.


    @staticmethod
    def _generic_app_sections(apps: dict) -> str:
        """Render every non-legacy app's store generically: one section per
        app (catalog display name as the heading), one table per top-level
        list field, one row per item, one ``data-field`` cell per scalar
        field (values stringified, escaped, capped at 120 chars). Pure
        projection of ``app_state`` + catalog metadata — zero per-app
        knowledge. Apps already served by the legacy tables (wechat /
        alipay) are skipped here so their markup stays byte-stable."""
        sections: list[str] = []
        for app_id in APPS:
            if app_id in ("wechat", "alipay"):
                continue                      # legacy tables own these
            app_state = apps.get(app_id)
            if not isinstance(app_state, dict) or not app_state:
                continue                      # no store / empty store
            for field, items in app_state.items():
                if not isinstance(items, list) or not items:
                    continue
                rows = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    cells = "".join(
                        f'<td data-field="{_esc(k)}">'
                        f'{_esc(str(v)[:120])}</td>'
                        for k, v in item.items()
                        if isinstance(v, (str, int, float, bool)))
                    if cells:
                        rows.append(f"<tr>{cells}</tr>")
                if rows:
                    sections.append(
                        f"<h2>{_esc(get_display_name(app_id))} · "
                        f"{_esc(field)} ({len(rows)})</h2>"
                        f"<table><tbody>{''.join(rows)}</tbody></table>")
        return "\n".join(sections)

    # ── minimal HTML view (rendered-GUI observation for the compiler) ───────
    def html_view(self, sid: str) -> str:
        """The rendered-GUI observation the COMPILER reads (read-path-is-GUI,
        no-leak): parseable ``<tr>`` rows with ``<td data-field="...">`` cells
        — the SAME DOM contract the core apps use
        (``replay_engine.parse_dom_entities`` + ``_row_fields``).

        Catalog-driven and app-agnostic: the two legacy projections
        (wechat chats / alipay transactions) keep their byte-stable table
        markup for existing consumers; EVERY OTHER app with a store is
        rendered generically — one section per app (titled by its catalog
        display name), one table per top-level list field, one row per
        item, one ``data-field`` cell per scalar field. No app is enumerated
        in code; an app appears here by having store data. Storeless apps
        (calculator, theme_store) simply have no section — their state IS
        the live screen, which ``observe`` serves."""
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
{self._generic_app_sections(apps)}
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
        """Compat alias: the per-app wechat operator route is superseded by
        the generic ``POST /api/mutate/<sid>`` (NL intent, no operator
        enum). Answer 302 pointing at the new route for the compat window;
        the body describes the migration so a scripted caller can adapt
        without guessing."""
        sid = request.match_info["sid"]
        raise web.HTTPFound(
            f"/api/mutate/{sid}",
            text=json.dumps({
                "migrated": True,
                "old_route": f"/api/wechat/{sid}/<eid>",
                "new_route": f"/api/mutate/{sid}",
                "new_payload": {"app": "<app_id>",
                                "entity_ref": "<user-visible entity label>",
                                "intent": "<natural-language intent>",
                                "undo": False},
            }, ensure_ascii=False))

    async def api_x_mutate(request):
        """Compat alias: the per-app X operator route is superseded by the
        generic ``POST /api/mutate/<sid>`` (same migration as wechat)."""
        sid = request.match_info["sid"]
        raise web.HTTPFound(
            f"/api/mutate/{sid}",
            text=json.dumps({
                "migrated": True,
                "old_route": f"/api/x/{sid}/<eid>",
                "new_route": f"/api/mutate/{sid}",
                "new_payload": {"app": "<app_id>",
                                "entity_ref": "<user-visible entity label>",
                                "intent": "<natural-language intent>",
                                "undo": False},
            }, ensure_ascii=False))

    async def api_mutate(request):
        sid = request.match_info["sid"]
        data = await request.json()
        return web.json_response(await bridge.mutate(
            sid, str(data.get("app") or ""),
            str(data.get("entity_ref") or ""),
            str(data.get("intent") or ""),
            undo=bool(data.get("undo", False))))

    async def api_app_state(request):
        sid = request.match_info["sid"]
        app_id = request.match_info["app_id"]
        return web.json_response(await bridge.app_state(sid, app_id))

    async def api_os_state(request):
        sid = request.match_info["sid"]
        return web.json_response(await bridge.os_state(sid))

    app.router.add_get("/health", health)
    app.router.add_get("/{sid}", view_sid)
    app.router.add_post("/api/reset/{sid}", api_reset)
    app.router.add_post("/api/inject_task/{sid}", api_inject_task)
    app.router.add_get("/api/session_state/{sid}", api_session_state)
    app.router.add_get("/api/x_state/{sid}", api_x_state)
    app.router.add_get("/api/{resource}/{sid}", api_resource)
    app.router.add_get("/api/observe/{sid}", api_observe)
    app.router.add_post("/api/act/{sid}", api_act)
    app.router.add_post("/api/mutate/{sid}", api_mutate)
    app.router.add_get("/api/app_state/{sid}/{app_id}", api_app_state)
    app.router.add_get("/api/os_state/{sid}", api_os_state)
    # compat aliases (one-commit window; deletion announced in the R3 report)
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
                        help="dotted module path of the CUA loop module to "
                             "INJECT (must expose gui_write_async / "
                             "gui_act_async / GuiExecutorFailure). Without "
                             "it the write routes answer 501 — the bridge "
                             "never imports upper layers itself (substrate "
                             "isolation).")
    parser.add_argument("--verifier", default=None,
                        help="dotted module path of the ModelVerifier "
                             "adapter module to INJECT (must expose "
                             "make_verifier() -> object with async "
                             "verify_intent(observation, intent)). Without "
                             "it the generic mutate route answers 501 — "
                             "no unverified ok, ever.")
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

    # CUA loop + verifier injection (process ASSEMBLY — the only legitimate
    # way upper-layer loops/reachers enter this bridge process).
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

    verifier = None
    if args.verifier:
        import importlib
        try:
            vmod = importlib.import_module(args.verifier)
            verifier = vmod.make_verifier()
            if not hasattr(verifier, "verify_intent"):
                raise AttributeError("verify_intent")
        except (ImportError, AttributeError) as e:
            raise SystemExit(
                f"--verifier {args.verifier!r} is not a valid verifier "
                f"adapter module (needs make_verifier() -> object with "
                f"async verify_intent(observation, intent)): {e}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    shot_dir = args.screenshot_dir
    if shot_dir is None:
        shot_dir = f"eval_results/mobilegym_visual_{ts}"
    elif shot_dir == "":
        shot_dir = None

    bridge = MobileGymBridge(sim_url=args.sim_url, headless=not args.headed,
                             screenshot_dir=shot_dir, cua=cua,
                             verifier=verifier)

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
