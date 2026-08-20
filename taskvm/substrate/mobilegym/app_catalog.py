"""MobileGym full app catalog — the single source of truth for app metadata.

Generated from ``mobilegym/apps/*/manifest.ts`` + ``mobilegym/system/*/
manifest.ts`` (27 manifest files, auto-discovered by the sim's
PackageManagerService via ``import.meta.glob``). Field-by-field source of
truth per app is the manifest's ``id`` / ``displayName`` / ``type`` fields
plus the presence of a ``state.ts`` (zustand store) in the app directory.

Store semantics (verified against the sim's ``get_state`` retry loop): the
25 apps WITH ``state.ts`` answer ``get_state(required_apps=[...])`` with a
populated store slice; the 2 WITHOUT one (``calculator``, ``theme_store``)
trigger the retry-then-warn path and honestly return no store slice —
they are still fully openable and GUI-drivable, they just have no readable
backend store (their state IS the screen).

This module is pure data + validators (stdlib only). Every MobileGym-aware
module (bridge / provider / session / evaluation) imports from here instead
of carrying its own app list — one catalog, zero per-file app enumerations.
"""
from __future__ import annotations

# (app_id, display_name, category, has_store)
# category: "daily" (mobilegym/apps/*) | "system" (mobilegym/system/*)
_APP_TUPLES: tuple[tuple[str, str, str, bool], ...] = (
    # ── daily apps (13, apps/*/manifest.ts) — all have state.ts ──────────
    ("alipay",          "支付宝",    "daily", True),
    ("bilibili",        "哔哩哔哩",  "daily", True),
    ("ebay",            "eBay",      "daily", True),
    ("map",             "地图",      "daily", True),
    ("railway12306",    "铁路12306", "daily", True),
    ("redbook",         "小红书",    "daily", True),
    ("reddit",          "Reddit",    "daily", True),
    ("spotify",         "Spotify",   "daily", True),
    ("tencent_meeting", "腾讯会议",  "daily", True),
    ("weather",         "天气",      "daily", True),
    ("wechat",          "微信",      "daily", True),
    ("wechat_reading",  "微信读书",  "daily", True),
    ("x",               "X",         "daily", True),
    # ── system apps (14, system/*/manifest.ts) — calculator/theme_store
    #    have NO state.ts: openable + GUI-drivable, but no readable store ──
    ("answer_sheet",    "答题卡",    "system", True),
    ("browser",         "浏览器",    "system", True),
    ("calculator",      "计算器",    "system", False),
    ("calculator2",     "计算器2",   "system", True),
    ("calendar",        "日历",      "system", True),
    ("clock",           "时钟",      "system", True),
    ("compass",         "指南针",    "system", True),
    ("contacts",        "电话",      "system", True),
    ("file_manager",    "文件",      "system", True),
    ("gallery",         "相册",      "system", True),
    ("notes",           "笔记",      "system", True),
    ("settings",        "设置",      "system", True),
    ("sms",             "短信",      "system", True),
    ("theme_store",     "主题商店",  "system", False),
)

#: all 27 app ids (daily + system), in catalog order
ALL_APP_IDS: tuple[str, ...] = tuple(t[0] for t in _APP_TUPLES)

#: the 25 apps that own a zustand store (have state.ts)
STORE_APP_IDS: tuple[str, ...] = tuple(t[0] for t in _APP_TUPLES if t[3])

#: app_id -> user-visible display name (the name on the home screen)
DISPLAY_NAMES: dict[str, str] = {t[0]: t[1] for t in _APP_TUPLES}

#: app_id -> "daily" | "system"
CATEGORIES: dict[str, str] = {t[0]: t[2] for t in _APP_TUPLES}

#: app_id -> whether the app has a readable backend store
HAS_STORE: dict[str, bool] = {t[0]: t[3] for t in _APP_TUPLES}


def is_valid_app(app_id: str) -> bool:
    """True if ``app_id`` names an app registered in the sim's package
    manager (all 27, storeless ones included)."""
    return app_id in DISPLAY_NAMES


def is_valid_app_or_raise(app_id: str) -> str:
    """Validate and return ``app_id``, or raise ``ValueError`` naming the
    valid set — callers surface this as an honest 400, never a silent
    fallback to a default app."""
    if app_id not in DISPLAY_NAMES:
        raise ValueError(
            f"unknown app {app_id!r}; valid apps: {', '.join(ALL_APP_IDS)}")
    return app_id


def get_display_name(app_id: str) -> str:
    """The user-visible (Chinese) display name for ``app_id``; falls back
    to the id itself for unknown inputs."""
    return DISPLAY_NAMES.get(app_id, app_id)


#: display_name -> app_id (exact). The manifest displayName is what the
#: RENDERED HOME SCREEN shows, so it is the only spelling a GUI-only
#: speaker (a CUA restricted to visible text) can legitimately produce.
_DISPLAY_TO_ID: dict[str, str] = {v: k for k, v in DISPLAY_NAMES.items()}
#: lowercase app_id -> app_id, for case-insensitive resolution. The 27
#: catalog ids are unique and clash-free under ``lower()`` (locked by
#: test_catalog_ids_unique_case_insensitive).
_ID_LOWER_TO_ID: dict[str, str] = {a.lower(): a for a in ALL_APP_IDS}


def resolve_app_id(name: str) -> str | None:
    """Resolve a GUI-visible app spelling to the canonical catalog app_id.

    GUI-only contract (GATE-G0 2026-08-20 postmortem, r3): the CUA system
    prompt restricts the model to what the rendered screen shows — the
    manifest displayName ("X", "支付宝") — NEVER internal ids ("x",
    "alipay"). The bridge's ``open`` gesture must therefore TRANSLATE the
    visible spelling instead of demanding the internal one (r3 burned 11
    of 12 gestures re-finding an app the model had already named
    correctly on gesture #1). Resolution order (most specific first):
    exact app_id → exact display name → case-insensitive app_id.
    Returns ``None`` for anything else — an honest unknown, never a guess.
    """
    n = (name or "").strip()
    if not n:
        return None
    if n in DISPLAY_NAMES:
        return n
    if n in _DISPLAY_TO_ID:
        return _DISPLAY_TO_ID[n]
    return _ID_LOWER_TO_ID.get(n.lower())
