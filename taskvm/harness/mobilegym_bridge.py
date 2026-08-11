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
2026-08-10, see memory taskvm-non-invasive-write-rollback-boundary).** The
write path (``_send_message``) goes through the app's OWN write pipeline —
NO ``set_state``: ``env.open_app("wechat")`` (OS launcher) → deep-link
``openApp('wechat','/chat/<id>')`` (OS navigation, like an Android Intent) →
programmatic textarea focus (grounding — the composer renders below the
viewport, so a coordinate tap can't reach it; documented in ``_send_message``)
→ ``Action.type_text`` (gesture via ``__SIM_INPUT__``) → ``Action(ENTER)``
(gesture → WeChat ``handleKeyDown`` → ``handleSend`` → ``sendMessage`` store
mutation). The rollback
path: MobileGym's wechat has NO delete/recall UI for messages (verified — no
long-press handler, no deleteMessage store action, append-only messages), so a
real-gesture rollback of a sent message is NOT possible; the bridge HONESTLY
raises ``NotImplementedError`` rather than falling back to ``set_state`` to
fake a byte-exact restore (that would undermine the compensation claim — a
backdoor rollback proves "we have debug permission," not "TaskVM compensates").
``rollback.py``'s saga undo catches this and marks ``reverted=False`` (honest
partial-failure). This is Option C (honest irreversibility) pending the user's
rollback-branch decision.

Routes (mirror the Drive app's contract, app-namespaced):
    GET  /health                              → {"status":"ok","site":"mobilegym"}
    GET  /<sid>                               → minimal HTML view (data-chat-id DOM)
    POST /api/reset/<sid>                     → env.reset(app_ids=[wechat,alipay])
    POST /api/inject_task/<sid>               → env.set_state(seed, deep=True) [SETUP-ONLY]
    GET  /api/session_state/<sid>             → summary only (n_chats, n_tx) — never GT
    GET  /api/wechat_chats/<sid>              → flattened wechat chats (entities)
    GET  /api/alipay_transactions/<sid>       → flattened alipay transferRecords
    POST /api/wechat/<sid>/<eid>              → send_message (real gestures) OR msg:-rollback
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import threading
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

SITE = "mobilegym"
DEFAULT_PORT = 3019
# MobileGym apps this bridge exposes (demo scope: Top3 task = alipay→wechat).
APPS = ["wechat", "alipay"]
# The Vite dev/preview server URL the env drives.
DEFAULT_SIM_URL = "http://localhost:3000"


class MobileGymBridge:
    """Holds one MobileGymEnv + per-sid live-state cache, served by aiohttp."""

    def __init__(self, sim_url: str, headless: bool = True,
                 screenshot_dir: str | None = None):
        self.sim_url = sim_url
        self.headless = headless
        self.screenshot_dir = screenshot_dir    # E9.4/task-3: auto step shots
        self._shot_counter = 0                  # monotonic (Date.now banned)
        self.env = None                      # MobileGymEnv, built in start()
        self._loop: asyncio.AbstractEventLoop | None = None
        # per-sid "live" snapshot = the full {os, apps} state currently
        # associated with that sid. The env's browser holds exactly one
        # state at a time; switching sids swaps it via set_state(deep=False).
        self._sid_live: dict[str, dict] = {}
        self._active_sid: str | None = None
        self._lock = asyncio.Lock()

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

    # ── sid switching (session context, NOT write/rollback) ─────────────────
    async def _activate(self, sid: str) -> None:
        """Make ``sid`` the live browser state. The browser holds ONE app
        state at a time, so switching between concurrent sids (e.g. a
        workspace-UI demo sid vs a kill-test sid) saves the current state and
        loads the target sid's cached state. This is session CONTEXT
        SWITCHING via ``set_state`` — a state-loading use (like
        ``inject_task``/seed), NOT a write or rollback of a business
        operation, so it does not violate the non-invasive write/rollback
        boundary (that boundary governs ``mutate`` + undo only)."""
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
        await self._activate(sid)
        async with self._lock:
            if operator != "send_message":
                raise web.HTTPBadRequest(
                    text=f"wechat operator must be send_message, got {operator}")
            # ── rollback path: value is "msg:<id>" from a prior send_message ──
            # MobileGym's wechat has NO delete/recall UI for messages (verified
            # 2026-08-10: no long-press handler on message bubbles, no
            # deleteMessage/recallMessage store action, messages array is
            # append-only — see memory taskvm-non-invasive-write-rollback-
            # boundary). TaskVM HONESTLY reports this write is not compensable
            # via the app's own gestures; it does NOT fall back to the
            # set_state backdoor to fake a byte-exact restore (that would
            # undermine the compensation claim). The caller's saga/undo catches
            # this NotImplementedError and marks reverted=False (honest
            # partial-failure, per execution/rollback.py §no-atomic-cross-app-
            # txn). This is Option C (honest irreversibility) — the default
            # pending the user's rollback-branch decision (Option A: swap to a
            # genuinely reversible op; Option B: set_state teardown labelled as
            # env-reset, not compensation).
            if isinstance(value, str) and value.startswith("msg:"):
                # MobileGym's wechat has NO delete/recall UI for messages
                # (verified 2026-08-10: no long-press handler on message
                # bubbles, no deleteMessage/recallMessage store action,
                # messages array is append-only). TaskVM HONESTLY reports
                # this write is not compensable via the app's own gestures;
                # it does NOT fall back to the set_state backdoor to fake a
                # byte-exact restore (that would undermine the compensation
                # claim). Raise a clean HTTP 409 (Conflict — cannot reverse)
                # so the adapter's raise_for_status → the saga's undo_saga
                # catches it and marks reverted=False, partial_failure=True
                # (honest partial-failure, per execution/rollback.py). This
                # is Option C (honest irreversibility) — the default pending
                # the user's rollback-branch decision (Option A: swap to a
                # genuinely reversible op; Option B: set_state teardown
                # labelled as env-reset, not compensation).
                # task-3: snapshot the 'message still there' state so the
                # honesty is VISIBLE — the screenshot proves the sent message
                # was NOT secretly deleted before the 409 was raised.
                await self._screenshot("undo_attempt_409_message_still_there")
                raise web.HTTPConflict(text=(
                    "wechat send_message is irreversible in MobileGym: the "
                    "app exposes no delete/recall UI for messages (no long-"
                    "press handler on message bubbles, no deleteMessage/"
                    "recallMessage store action — messages are append-only). "
                    "TaskVM honestly reports this write cannot be compensated "
                    "via the app's own gestures; no set_state backdoor "
                    "fallback (non-invasive write/rollback boundary)."))
            # ── forward write: real GUI gesture sequence ──
            return await self._send_message(sid, eid, str(value))

    async def _send_message(self, sid: str, chat_id: str, text: str) -> dict:
        """Send a message via the app's OWN write pipeline — NO set_state on
        the write path (handoff fix 2026-08-10; the prior set_state(deep=True)
        patch bypassed the app's gesture/business layer — see memory
        taskvm-non-invasive-write-rollback-boundary).

        Sequence (the WRITE itself goes through ``__SIM_INPUT__`` gestures →
        the app's own ``handleKeyDown`` → ``handleSend`` → ``sendMessage``
        store mutation — MobileGym runtime-api.md L134: "the same gestures the
        benchmark dispatches when an agent emits actions"):
          1. ``env.open_app("wechat")`` — the OS launcher
             (``window.__OS__.openApp`` = tapping the app icon). Warms wechat.
          2. Deep-link to the chat: ``window.__OS__.openApp('wechat',
             '/chat/<id>')`` — the OS launcher with an initialRoute (like an
             Android Intent deep link). The app's OWN router mounts ChatDetail
             + resolves the peer. NOT a state-injection backdoor. (The 2-arg
             form requires wechat to be warmed first — step 1 — else it
             no-ops; verified 2026-08-10.)
          3. Wait for ChatDetail's composer ``<textarea>`` to mount.
          4. Focus the textarea. ⚠️ HONESTY CAVEAT: the composer is rendered
             BELOW the 800px CSS viewport in this sim's layout (rect y≈858,
             verified — a coordinate ``Action.click`` cannot reach it and
             ``scrollIntoView`` does not move it, likely a reserved keyboard
             area). So focus is done programmatically via
             ``page.evaluate(el => el.focus())`` — a GROUNDING step (like the
             DOM rect read), NOT a state mutation. A real mobile agent would
             tap the input box; the bridge cannot tap an off-screen element, so
             it focuses directly. The WRITE itself is still a gesture.
          5. ``Action.type_text(text)`` (no point — types into the focused
             textarea via ``__SIM_INPUT__.type``). GESTURE.
          6. ``Action(ActionType.ENTER, {})`` — synthetic Enter keydown via
             ``__SIM_INPUT__.enter``; WeChat's ``handleKeyDown`` catches Enter
             (no Shift) → ``handleSend`` → ``sendMessage``. GESTURE. (Using
             Enter instead of tapping the 发送 button because the button has
             no stable selector and shifts with the keyboard.)
          7. Verify via ``get_state()`` (the trusted read path) that the
             message really landed in ``chats[<id>].messages``.
        """
        import asyncio
        from bench_env.env.base import Action, ActionType
        # 1. open wechat (OS launcher) — warms the app
        await self.env.open_app("wechat", wait_stable=True)
        await self._screenshot("open_app_wechat")
        # 2. deep-link to the chat (OS navigation, NOT a state backdoor)
        await self.env.page.evaluate(
            f"window.__OS__?.openApp?.('wechat', '/chat/{chat_id}')")
        await self._screenshot("deep_link_to_chat")
        # 3. wait for ChatDetail's composer textarea to mount (retry ~4s)
        found = False
        for _ in range(8):
            await asyncio.sleep(0.5)
            found = await self.env.page.evaluate(
                "() => !!document.querySelector('textarea')")
            if found:
                break
        if not found:
            await self._screenshot("FAIL_composer_not_found")
            raise RuntimeError(
                f"wechat ChatDetail composer textarea not found after "
                f"deep-link to /chat/{chat_id} — is the wxid a seeded "
                f"contact/chat (add_contacts + add_chats)?")
        # 4. focus the textarea (programmatic — see HONESTY CAVEAT above; the
        # composer is below the viewport so a coordinate tap can't reach it)
        await self.env.page.evaluate(
            "document.querySelector('textarea')?.focus()")
        await asyncio.sleep(0.3)
        await self._screenshot("focus_composer")
        # 5. type the text (GESTURE via __SIM_INPUT__.type → activeElement)
        await self.env.step(Action.type_text(text))
        await self._screenshot("type_text")
        # 6. send via Enter (GESTURE via __SIM_INPUT__.enter → handleKeyDown → handleSend)
        await self.env.step(Action(ActionType.ENTER, {}))
        await self._screenshot("enter_send")
        # 7. verify the message really landed in the live sim
        state = await self.env.get_state(required_apps=APPS)
        self._sid_live[sid] = state
        chats = state.get("apps", {}).get("wechat", {}).get("chats", []) or []
        target = next((c for c in chats if c.get("id") == chat_id), None)
        if target is None:
            raise RuntimeError(
                f"chat {chat_id} not found after send — sendMessage likely "
                f"no-op'd (wxid is neither a contact nor a seeded chat)")
        msgs = target.get("messages") or []
        new_msg = next((m for m in reversed(msgs)
                        if m.get("type") == "text" and m.get("content") == text), None)
        if not new_msg:
            raise RuntimeError(
                f"sent text not found in chat {chat_id} after the gesture "
                f"sequence — the type/enter gestures may not have reached "
                f"the composer (verify focus + __SIM_INPUT__ delivery)")
        logger.info(f"[bridge] send_message via app pipeline (no set_state): "
                    f"chat={chat_id} msg_id={new_msg.get('id')!r} n_msgs={len(msgs)}")
        await self._screenshot("verify_message_landed")
        # ``old`` = "msg:<id>" so a future real-gesture delete (if the app
        # gains one) could target it; ``new`` = the text. Until then the
        # msg: rollback branch honestly reports irreversibility (above).
        return {"status": "ok", "operator": "send_message",
                "old": f"msg:{new_msg.get('id')}", "new": text,
                "chat_id": chat_id, "n_messages": len(msgs),
                "message_id": new_msg.get("id")}

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

    async def api_resource(request):
        sid = request.match_info["sid"]
        resource = request.match_info["resource"]
        return web.json_response(await bridge.read_resource(sid, resource))

    async def api_wechat_mutate(request):
        sid = request.match_info["sid"]
        eid = request.match_info["eid"]
        data = await request.json()
        return web.json_response(await bridge.mutate_wechat(
            sid, eid, data.get("operator"), data.get("value")))

    app.router.add_get("/health", health)
    app.router.add_get("/{sid}", view_sid)
    app.router.add_post("/api/reset/{sid}", api_reset)
    app.router.add_post("/api/inject_task/{sid}", api_inject_task)
    app.router.add_get("/api/session_state/{sid}", api_session_state)
    app.router.add_get("/api/{resource}/{sid}", api_resource)
    app.router.add_post("/api/wechat/{sid}/{eid}", api_wechat_mutate)
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
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    # chromium launch recipe (see memory: taskvm-chromium-launch-recipe)
    sp = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    # senseact chromium + .chromelibs lib bucket
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        "/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/conda/envs/senseact/opt/ms-playwright")
    cl = "/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui/.chromelibs/lib"
    os.environ["LD_LIBRARY_PATH"] = cl + ":" + os.environ.get("LD_LIBRARY_PATH", "")

    ts = time.strftime("%Y%m%d_%H%M%S")
    shot_dir = args.screenshot_dir
    if shot_dir is None:
        shot_dir = f"eval_results/mobilegym_visual_{ts}"
    elif shot_dir == "":
        shot_dir = None

    bridge = MobileGymBridge(sim_url=args.sim_url, headless=not args.headed,
                             screenshot_dir=shot_dir)

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
