"""tests/substrate — MobileGym full-app catalog invariants (MG-FULL-APPS).

The catalog (``taskvm/substrate/mobilegym/app_catalog.py``) is the single
source of truth for app metadata, generated from the sim's own manifests
(27 ``manifest.ts`` files: 13 daily + 14 system; 25 with a zustand store,
calculator/theme_store without). These tests lock the frozen public
surface so a regenerated catalog that drifts from the sim fails loudly.

Ground truth: docs/04_RM&APP时代/handover_full_app_integration.md §2.1
(verified against mobilegym/apps/*/manifest.ts + system/*/manifest.ts).
"""
from __future__ import annotations

import pytest

from taskvm.substrate.mobilegym import app_catalog as ac


def test_catalog_counts_match_sim_manifests():
    assert len(ac.ALL_APP_IDS) == 27, (
        "the sim registers 27 apps (13 daily + 14 system) — the catalog "
        "must mirror the PackageManagerService's auto-discovered set")
    assert len(set(ac.ALL_APP_IDS)) == 27, "duplicate app_id in catalog"
    assert len(ac.STORE_APP_IDS) == 25, (
        "25 of the 27 apps own a zustand store (state.ts)")
    assert set(ac.STORE_APP_IDS) <= set(ac.ALL_APP_IDS)


def test_storeless_apps_are_the_two_known_manifest_gaps():
    # calculator and theme_store have NO state.ts: openable + GUI-drivable,
    # but get_state() honestly returns no store slice for them.
    assert "calculator" not in ac.STORE_APP_IDS
    assert "theme_store" not in ac.STORE_APP_IDS
    assert ac.HAS_STORE["calculator"] is False
    assert ac.HAS_STORE["theme_store"] is False


def test_daily_vs_system_split():
    daily = [a for a, c in ac.CATEGORIES.items() if c == "daily"]
    system = [a for a, c in ac.CATEGORIES.items() if c == "system"]
    assert len(daily) == 13 and len(system) == 14
    for a in ("wechat", "alipay", "x", "redbook", "railway12306"):
        assert ac.CATEGORIES[a] == "daily"
    for a in ("settings", "sms", "calendar", "notes", "clock",
              "calculator", "theme_store"):
        assert ac.CATEGORIES[a] == "system"


def test_is_valid_app_accepts_all_27_rejects_unknown():
    for aid in ac.ALL_APP_IDS:
        assert ac.is_valid_app(aid), f"catalog app {aid!r} must be valid"
    for bogus in ("phone", "camera", "qqmusic", "WeChat", "支付宝", ""):
        assert not ac.is_valid_app(bogus), (
            f"{bogus!r} is not a registered app id")


def test_is_valid_app_or_raise_is_honest():
    assert ac.is_valid_app_or_raise("notes") == "notes"
    with pytest.raises(ValueError) as ei:
        ac.is_valid_app_or_raise("phone")
    assert "phone" in str(ei.value)
    assert "notes" in str(ei.value), (
        "the error must name the valid set so callers can self-correct")


def test_display_names_are_the_home_screen_chinese_names():
    expected = {
        "wechat": "微信",
        "alipay": "支付宝",
        "x": "X",
        "redbook": "小红书",
        "bilibili": "哔哩哔哩",
        "railway12306": "铁路12306",
        "tencent_meeting": "腾讯会议",
        "wechat_reading": "微信读书",
        "settings": "设置",
        "calculator": "计算器",
        "calculator2": "计算器2",
        "theme_store": "主题商店",
        "file_manager": "文件",
        "contacts": "电话",
        "sms": "短信",
    }
    for aid, name in expected.items():
        assert ac.get_display_name(aid) == name
    # honest fallback for unknown ids: the id itself, never a fake name
    assert ac.get_display_name("not_an_app") == "not_an_app"
