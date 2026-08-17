"""B-02 — provider retry / error taxonomy on ``benchmark/model_client.py``.

Pre-fix reality: ``complete`` / ``complete_vision`` retried EVERY exception
the same way (4 blind attempts with backoff — a 401 spent 3 sleeps too),
the temperature downgrade looped silently, ``complete_json`` re-prompted
on parse failure by DEFAULT (a hidden low-level re-request the caller
never asked for), and nothing counted real provider requests.

Post-fix (B-02 rules, verbatim from the RM-0 work order):
  * 401 / 402 / 403            → immediate fatal (ONE request, no sleep);
  * other explicit non-429 4xx → fatal;
  * 429 / 5xx                  → bounded exponential backoff;
  * transport timeout / connection transient (no HTTP status) → bounded
    retry;
  * unsupported temperature    → at most ONE explicit downgrade (a second
    temperature rejection after the downgrade is fatal);
  * every REAL provider request (primary / backoff retry / downgrade /
    explicit repair) lands exactly ONE journal entry —
    ``request_count()`` can never under-count a hidden retry;
  * parse failure never re-requests at the low level
    (``repair_retries`` defaults to 0); the ONLY repair path is the
    explicit upper-layer orchestration passing its own budget.

The production port ``taskvm/architect/http_port.py`` keeps its stricter
contract (zero retry, 1 complete_json = 1 provider request — C-2) —
pinned by tests/architect; this module covers the BENCH client.
"""
from __future__ import annotations

import pytest

from taskvm_bench.benchmark import model_client
from taskvm_bench.benchmark.model_client import (
    ProviderFatalError, _classify, _status_of,
)


class FakeResp:
    def __init__(self, content: str = "ok"):
        self.choices = [type("C", (), {"message": type(
            "M", (), {"content": content})()})()]


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script):
        self._completions = FakeCompletions(script)
        self.chat = type("Chat", (), {"completions": self._completions})()


class StatusError(Exception):
    """openai-APIStatusError-shaped: carries .status_code."""

    def __init__(self, status: int, msg: str = ""):
        super().__init__(msg or f"HTTP {status}")
        self.status_code = status


class TimeoutError_(Exception):
    """openai-APITimeoutError-shaped: request timed out, NO status."""

    def __init__(self):
        super().__init__("Request timed out.")


class _State:
    """Test-scope holder (client wiring + captured sleeps)."""

    def __init__(self):
        self.client: "FakeClient | None" = None
        self.sleeps: list = []


_state = _State()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    model_client.reset_request_bookkeeping()
    monkeypatch.setattr(model_client, "_get_client",
                        lambda: _state.client)
    _state.sleeps = []
    monkeypatch.setattr(model_client.time, "sleep",
                        lambda s: _state.sleeps.append(s))
    yield
    model_client.reset_request_bookkeeping()


def _wire(monkeypatch, script):
    client = FakeClient(script)
    _state.client = client
    return client


def _http_ok(text: str = "hello") -> FakeResp:
    return FakeResp(text)


# ── status extraction ──────────────────────────────────────────────────────

def test_status_of_reads_status_code_then_code():
    assert _status_of(StatusError(429)) == 429
    import urllib.error
    try:
        raise urllib.error.HTTPError(None, 403, "forbidden", None, None)  # type: ignore[arg-type]
    except urllib.error.HTTPError as e:
        assert _status_of(e) == 403
    assert _status_of(TimeoutError_()) is None
    assert _status_of(RuntimeError("plain")) is None


# ── classification table ───────────────────────────────────────────────────

@pytest.mark.parametrize("err,active,verdict", [
    (StatusError(401, "bad key"), False, "fatal"),
    (StatusError(402, "quota"), False, "fatal"),
    (StatusError(403, "denied"), False, "fatal"),
    (StatusError(400, "unsupported_value"), False, "fatal"),
    (StatusError(404, "no route"), False, "fatal"),
    (StatusError(422, "bad body"), False, "fatal"),
    (StatusError(429, "rate limited"), False, "backoff"),
    (StatusError(500, "oops"), False, "backoff"),
    (StatusError(503, "unavailable"), False, "backoff"),
    (TimeoutError_(), False, "backoff"),
    # temperature downgrade: only while a temperature is still being sent
    (StatusError(400, "temperature unsupported_value"), True, "downgrade"),
    (TimeoutError_(), True, "backoff"),          # no temp wording → backoff
])
def test_classification_table(err, active, verdict):
    assert _classify(err, temperature_active=active) == verdict


# ── immediate-fatal statuses: ONE request, no sleep ───────────────────────

@pytest.mark.parametrize("status", [401, 402, 403, 400, 404, 422])
def test_fatal_statuses_raise_immediately(monkeypatch, status):
    client = _wire(monkeypatch, [StatusError(status), _http_ok()])
    with pytest.raises(ProviderFatalError):
        model_client.complete([{"role": "user", "content": "hi"}])
    assert len(client._completions.calls) == 1      # never re-requested
    assert _state.sleeps == []                      # no backoff burned
    assert model_client.request_count() == 1        # journaled once


def test_fatal_is_runtime_error_subclass(monkeypatch):
    _wire(monkeypatch, [StatusError(401)])
    with pytest.raises(RuntimeError):               # legacy catch still works
        model_client.complete([{"role": "user", "content": "hi"}])


# ── bounded exponential backoff for 429 / 5xx / transport ─────────────────

def test_429_backs_off_then_succeeds(monkeypatch):
    client = _wire(monkeypatch, [StatusError(429), StatusError(429),
                                 _http_ok("recovered")])
    text, _ = model_client.complete([{"role": "user", "content": "hi"}])
    assert text == "recovered"
    assert len(client._completions.calls) == 3
    assert _state.sleeps == [1, 2]                  # 2**0, 2**1
    assert model_client.request_count() == 3


def test_5xx_backs_off_then_succeeds(monkeypatch):
    _wire(monkeypatch, [StatusError(503), _http_ok("back")])
    text, _ = model_client.complete([{"role": "user", "content": "hi"}])
    assert text == "back" and _state.sleeps == [1]


def test_transport_timeout_retries_bounded(monkeypatch):
    client = _wire(monkeypatch, [TimeoutError_()] * 4)   # retries=4 default
    with pytest.raises(RuntimeError, match="failed after 4 retries"):
        model_client.complete([{"role": "user", "content": "hi"}])
    assert len(client._completions.calls) == 4       # BOUNDED, not endless
    assert model_client.request_count() == 4


def test_backoff_is_exponential_and_capped(monkeypatch):
    _wire(monkeypatch, [StatusError(500)] * 6)
    with pytest.raises(RuntimeError):
        model_client.complete([{"role": "user", "content": "hi"}],
                              retries=6)
    assert _state.sleeps == [1, 2, 4, 8, 16]        # 2**a capped at 16


# ── temperature: at most ONE explicit downgrade ───────────────────────────

def test_temperature_rejected_once_downgrades_and_succeeds(monkeypatch):
    client = _wire(monkeypatch, [
        StatusError(400, "temperature is unsupported_value"),
        _http_ok("fine")])
    text, _ = model_client.complete(
        [{"role": "user", "content": "hi"}], temperature=0.3)
    assert text == "fine"
    assert len(client._completions.calls) == 2
    assert "temperature" in client._completions.calls[0]
    assert "temperature" not in client._completions.calls[1]  # dropped
    assert _state.sleeps == []                      # downgrade ≠ backoff
    assert model_client.request_count() == 2
    journal = model_client.journal_snapshot()
    assert journal[0]["phase"] == "primary"
    assert journal[1]["phase"] == "downgrade"


def test_second_temperature_rejection_is_fatal(monkeypatch):
    client = _wire(monkeypatch, [
        StatusError(400, "temperature unsupported"),
        StatusError(400, "temperature unsupported again")])
    # after the ONE downgrade temp is None → the same 400 is plain fatal
    with pytest.raises(ProviderFatalError, match="non-retryable"):
        model_client.complete([{"role": "user", "content": "hi"}],
                              temperature=0.7)
    assert len(client._completions.calls) == 2      # never a third try


# ── parse failure: NO hidden low-level re-request ─────────────────────────

def test_parse_failure_default_is_one_request(monkeypatch):
    client = _wire(monkeypatch, [_http_ok("I cannot answer that")])
    parsed, raw, _ = model_client.complete_json("sys", "user")
    assert parsed is None                           # honest parse failure
    assert len(client._completions.calls) == 1      # NO hidden repair call
    assert model_client.request_count() == 1


def test_explicit_upper_layer_repair_counts_each_request(monkeypatch):
    client = _wire(monkeypatch, [
        _http_ok("no json here"),
        _http_ok('{"a": 1}')])
    parsed, _, _ = model_client.complete_json("sys", "user",
                                              repair_retries=1)
    assert parsed == {"a": 1}
    assert len(client._completions.calls) == 2      # initial + 1 repair
    journal = model_client.journal_snapshot()
    assert journal[0]["phase"] == "primary"
    assert journal[1]["phase"] == "repair1"


def test_vision_json_same_repair_discipline(monkeypatch):
    client = _wire(monkeypatch, [_http_ok("garbage")])
    parsed, _, _ = model_client.complete_vision_json(
        "sys", "user", "data:image/png;base64,AAA")
    assert parsed is None
    assert len(client._completions.calls) == 1
    # explicit repair budget still works and is journaled
    model_client.reset_request_bookkeeping()       # drop segment-1 entry
    client2 = _wire(monkeypatch, [_http_ok("garbage"), _http_ok("[1,2]")])
    parsed2, _, _ = model_client.complete_vision_json(
        "sys", "user", "data:image/png;base64,AAA", repair_retries=1)
    assert parsed2 == [1, 2]
    phases = [e["phase"] for e in model_client.journal_snapshot()]
    assert phases == ["primary", "repair1"]


# ── the invariant that closes the audit ───────────────────────────────────

def test_request_count_equals_real_provider_calls_always(monkeypatch):
    # 429 retry + temperature downgrade + explicit repair — every path
    client = _wire(monkeypatch, [
        StatusError(429),
        StatusError(400, "temperature unsupported"),
        _http_ok("no json"),
        _http_ok('{"b": 2}')])
    parsed, _, _ = model_client.complete_json("sys", "user",
                                              temperature=0.5,
                                              repair_retries=1)
    assert parsed == {"b": 2}
    assert model_client.request_count() == 4
    assert len(client._completions.calls) == 4      # 1:1 — no hidden retry
