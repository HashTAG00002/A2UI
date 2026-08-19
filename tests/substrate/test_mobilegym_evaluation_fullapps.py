"""tests/substrate — MobileGym evaluation full-app coverage (MG-FULL-APPS).

Fake-HTTP tests over the EvaluationEnvironment's generic oracle reads and
the catalog-driven provider validation:

  * ``app_state`` / ``os_state`` speak the new generic bridge routes
    (any catalog app; the legacy ``oracle_state`` semantic projection is
    untouched for the three historical apps);
  * ``MobileGymProvider`` / ``MobileGymEvaluationProvider`` validate the
    ``app`` config against the catalog — unknown apps raise ValueError at
    composition time (never a silent wechat fallback), a missing app
    falls back to the catalog-derived default surface, and the session's
    surface display name is the catalog's user-visible Chinese name.
"""
from __future__ import annotations

import pytest

from taskvm.substrate.mobilegym import evaluation as eval_mod
from taskvm.substrate.mobilegym.app_catalog import ALL_APP_IDS
from taskvm.substrate.mobilegym.evaluation import (
    MobileGymEvaluationEnvironment,
    make_mobilegym_environments,
)
from taskvm.substrate.mobilegym.provider import (
    DEFAULT_SURFACE_APP,
    MobileGymEvaluationProvider,
    MobileGymProvider,
)


# ── fake HTTP layer ─────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status} (fake)")

    def json(self) -> dict:
        return self._payload


class FakeHTTP:
    """Serves a path→payload table; records every hit. URLs are normalized
    to their path (the client sends full ``http://host/api/...`` URLs, the
    table keys are ``/api/...`` paths)."""

    def __init__(self, routes: dict[str, dict]):
        self.routes = routes
        self.hits: list[tuple[str, str]] = []

    @staticmethod
    def _path(url: str) -> str:
        i = url.find("/api/")
        return url[i:] if i >= 0 else url

    def get(self, url, timeout=None, **kw):
        path = self._path(url)
        self.hits.append(("GET", path))
        if path in self.routes:
            return FakeResponse(self.routes[path])
        return FakeResponse({"error": f"no route for {path}"}, status=404)

    def post(self, url, json=None, timeout=None, **kw):  # noqa: A002
        path = self._path(url)
        self.hits.append(("POST", path))
        if path in self.routes:
            return FakeResponse(self.routes[path])
        return FakeResponse({"error": f"no route for {path}"}, status=404)


@pytest.fixture()
def fake_http(monkeypatch):
    http = FakeHTTP({})
    monkeypatch.setattr(eval_mod, "requests", http)
    return http


# ── generic oracle reads over fake HTTP ─────────────────────────────────────

def test_app_state_reads_the_generic_route(fake_http):
    fake_http.routes["/api/app_state/s1/calculator2"] = {
        "sid": "s1", "app": "calculator2",
        "state": {"history": [{"expr": "1+1", "value": 2}]}}
    env = MobileGymEvaluationEnvironment("wechat", "s1", "http://fake")
    out = env.app_state("s1", "calculator2")
    assert out["app"] == "calculator2"
    assert out["state"]["history"][0]["value"] == 2
    assert ("GET", "/api/app_state/s1/calculator2") in fake_http.hits


def test_app_state_defaults_to_the_envs_own_app(fake_http):
    fake_http.routes["/api/app_state/s1/notes"] = {
        "sid": "s1", "app": "notes", "state": {"notes": []}}
    env = MobileGymEvaluationEnvironment("notes", "s1", "http://fake")
    out = env.app_state()
    assert out["app"] == "notes"
    assert ("GET", "/api/app_state/s1/notes") in fake_http.hits


def test_os_state_reads_the_generic_route(fake_http):
    fake_http.routes["/api/os_state/s1"] = {
        "sid": "s1", "os": {"activeAppId": "calculator", "tasks": {}}}
    env = MobileGymEvaluationEnvironment("wechat", "s1", "http://fake")
    out = env.os_state("s1")
    assert out["os"]["activeAppId"] == "calculator"
    assert ("GET", "/api/os_state/s1") in fake_http.hits


def test_legacy_oracle_state_semantic_projection_untouched(fake_http):
    fake_http.routes["/api/wechat_chats/s1"] = {
        "site": "mobilegym", "sid": "s1",
        "wechat_chats": [{"id": "c1", "peer_name": "黄勇",
                          "n_messages": 1, "last_message": "在吗",
                          "messages": "在吗"}]}
    env = MobileGymEvaluationEnvironment("wechat", "s1", "http://fake")
    out = env.oracle_state("s1")
    assert out["entities"]["c1"]["peer_name"] == "黄勇"
    assert ("GET", "/api/wechat_chats/s1") in fake_http.hits


def test_legacy_oracle_state_is_honest_keyerror_for_new_apps(fake_http):
    """A non-legacy app has NO semantic projection table — the honest
    signal is an explicit KeyError pointing at the generic read, never a
    made-up shape."""
    env = MobileGymEvaluationEnvironment("notes", "s1", "http://fake")
    with pytest.raises(KeyError):
        env.oracle_state("s1")
    assert fake_http.hits == [], (
        "the failure must happen client-side — no request fired")


# ── provider validation (catalog-driven, no wechat default) ─────────────────

def test_provider_create_session_validates_app():
    session = MobileGymProvider().create_session(
        {"sid": "s1", "bridge_url": "http://fake", "app": "notes"})
    surface = session.list_surfaces()[0]
    assert surface.surface_id == "mobilegym:notes"
    assert surface.display_name == "笔记", (
        "the surface display name must be the catalog's user-visible "
        "Chinese name, not an id-derived title()")


def test_provider_create_session_rejects_unknown_app():
    with pytest.raises(ValueError) as ei:
        MobileGymProvider().create_session(
            {"sid": "s1", "bridge_url": "http://fake", "app": "phone"})
    assert "phone" in str(ei.value)


def test_provider_create_session_without_app_uses_catalog_default():
    session = MobileGymProvider().create_session(
        {"sid": "s1", "bridge_url": "http://fake"})
    surface = session.list_surfaces()[0]
    assert surface.surface_id == f"mobilegym:{DEFAULT_SURFACE_APP}"
    assert DEFAULT_SURFACE_APP == ALL_APP_IDS[0], (
        "the no-preference default is catalog-derived (first catalog "
        "entry), never a per-app hardcode")


def test_evaluation_provider_validates_app():
    env = MobileGymEvaluationProvider().create(
        {"sid": "s1", "bridge_url": "http://fake", "app": "settings"})
    assert env.app == "settings"
    with pytest.raises(ValueError):
        MobileGymEvaluationProvider().create(
            {"sid": "s1", "bridge_url": "http://fake", "app": "qqmusic"})


def test_make_mobilegym_environments_still_serves_legacy_apps(fake_http):
    fake_http.routes["/api/alipay_transactions/s1"] = {
        "site": "mobilegym", "sid": "s1",
        "alipay_transactions": [{"id": "t1", "delta": -100}]}
    envs = make_mobilegym_environments(
        ["wechat", "alipay"], "s1", base_url="http://fake")
    assert set(envs) == {"wechat", "alipay"}
    out = envs["alipay"].oracle_state("s1")
    assert out["entities"]["t1"]["delta"] == -100
