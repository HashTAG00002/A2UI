"""decoder — the A4 decode loop: model → two-layer validation → bounded
repair → honest baseline fallback. All model I/O is faked; the ledger
contract and prompt assembly are asserted structurally."""
from __future__ import annotations

import json

import pytest

from taskvm.genui.baseline import baseline_components
from taskvm.genui.context import TaskSurfaceContext
from taskvm.genui.decoder import (
    DecodeAttempt, DecodeResult, GenUIDecoder, SOURCE_FALLBACK,
    SOURCE_MODEL, load_system_prompt,
)
from taskvm.genui.protocol import GENUI_DECODER_MODEL_ENV


class _Reply:
    def __init__(self, parsed, model="fake-model", pt=10, ct=5):
        self.parsed = parsed
        self.raw = json.dumps(parsed, ensure_ascii=False) if parsed else ""
        self.model = model
        self.prompt_tokens = pt
        self.completion_tokens = ct


class _FakePort:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    def complete_json(self, *, system, user, model=None, max_tokens=3072,
                      temperature=None, image_data_url=None):
        self.calls.append({"system": system, "user": user, "model": model,
                           "max_tokens": max_tokens,
                           "temperature": temperature})
        if not self._outcomes:
            raise AssertionError("fake port exhausted (unexpected extra "
                                 "model call — the loop overshot)")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeLedger:
    def __init__(self):
        self.records: list = []

    def record(self, rec):
        self.records.append(rec)
        return rec


def _bad_components(valid):     # one honest, repairable violation
    out = json.loads(json.dumps(valid))
    for c in out:
        if c["id"] == "submit":
            c["action"] = {"event": {"name": "pause", "context": {}}}
    return out


# ── the happy path ──────────────────────────────────────────────────────────

def test_first_pass_success(context, valid_components):
    port = _FakePort([_Reply(valid_components)])
    ledger = _FakeLedger()
    result = GenUIDecoder(port, ledger).decode(context)
    assert result.source == SOURCE_MODEL
    assert result.components == valid_components
    assert result.model_calls == 1
    assert len(result.attempts) == 1 and result.attempts[0].ok
    assert len(port.calls) == 1                       # exactly one request
    assert len(ledger.records) == 1                   # one row per request
    rec = ledger.records[0]
    assert rec.role == "genui_decoder"
    assert rec.purpose == "surface_compose"
    assert rec.ok is True and rec.is_repair is False
    assert rec.model == "fake-model" and rec.request_id


def test_default_temperature_is_none_not_sent(context, valid_components):
    """Regression lock (2026-08-20 real-run evidence): FRIDAY-gateway models
    like gpt-5.6-sol reject any non-default temperature with HTTP 400
    "Unsupported value" — the decoder must NOT send one by default. None is
    the safe default; HttpModelPort omits the field entirely when it sees
    None, so the provider's own default applies."""
    port = _FakePort([_Reply(valid_components)])
    GenUIDecoder(port).decode(context)
    assert port.calls[0]["temperature"] is None

    # an explicit temperature still reaches the port (caller's own choice)
    port2 = _FakePort([_Reply(valid_components)])
    GenUIDecoder(port2, temperature=0.2).decode(context)
    assert port2.calls[0]["temperature"] == 0.2


def test_bare_array_recovered_from_raw_text(context, valid_components):
    """Regression lock (2026-08-20 real-run evidence, gpt-5.6-sol): the
    shared port's dict-first extractor hands the decoder the FIRST
    COMPONENT OBJECT (a dict) when the reply is a bare JSON array — which
    _coerce_components rightly refuses. The decoder's array-first fallback
    must recover the FULL array from the raw text instead of declaring a
    parse failure and wasting the repair round."""
    class _DictFirstReply:
        def __init__(self):
            self.parsed = valid_components[0]      # the generic extractor
            self.raw = json.dumps(valid_components, ensure_ascii=False)
            self.model = "gpt-5.6-sol"
            self.prompt_tokens = 100
            self.completion_tokens = 50

    port = _FakePort([_DictFirstReply()])
    result = GenUIDecoder(port).decode(context)
    assert result.source == SOURCE_MODEL
    assert result.components == valid_components
    assert len(port.calls) == 1                     # no wasted repair round


def test_repair_prompt_carries_context_payload(context, valid_components):
    """Regression lock (2026-08-20 real-run evidence): the repair round's
    user prompt must repeat the FULL TaskSurfaceContext — repair prompts
    that carried only the rejection reasons made the model regenerate
    blind (generic 4-component shells that bound nothing)."""
    port = _FakePort([_Reply(_bad_components(valid_components)),
                      _Reply(valid_components)])
    GenUIDecoder(port).decode(context)
    repair_user = port.calls[1]["user"]
    assert "TaskSurfaceContext" in repair_user      # the payload rides along
    assert "release_date" in repair_user
    assert "REJECTED" in repair_user                # ...plus the reasons


def test_dict_components_wrapper_accepted(context, valid_components):
    port = _FakePort([_Reply({"components": valid_components})])
    result = GenUIDecoder(port).decode(context)
    assert result.source == SOURCE_MODEL
    assert result.components == valid_components


# ── bounded repair ──────────────────────────────────────────────────────────

def test_repair_feeds_rejection_reasons_back(context, valid_components):
    port = _FakePort([_Reply(_bad_components(valid_components)),
                      _Reply(valid_components)])
    ledger = _FakeLedger()
    result = GenUIDecoder(port, ledger).decode(context)
    assert result.source == SOURCE_MODEL
    assert result.model_calls == 2
    # the repair round's user prompt carries the concrete rejection
    assert "governance" in port.calls[1]["user"]
    assert "pause" in port.calls[1]["user"]
    assert ledger.records[0].purpose == "surface_compose"
    assert ledger.records[1].purpose == "surface_repair"
    assert ledger.records[1].is_repair is True
    # attempt trail keeps the first failure's errors verbatim
    assert not result.attempts[0].ok
    assert any("governance" in e for e in result.attempts[0].errors)


def test_unparseable_reply_is_repairable(context, valid_components):
    port = _FakePort([_Reply(None), _Reply(valid_components)])
    result = GenUIDecoder(port).decode(context)
    assert result.source == SOURCE_MODEL
    assert any("parse" in e for e in result.attempts[0].errors)


def test_non_list_reply_rejected(context):
    port = _FakePort([_Reply({"foo": "bar"})])
    ledger = _FakeLedger()
    result = GenUIDecoder(port, ledger, max_repairs=0).decode(context)
    assert result.source == SOURCE_FALLBACK
    assert any("parse" in e for e in result.attempts[0].errors)


# ── honest fallback ─────────────────────────────────────────────────────────

def test_exhausted_repair_falls_back_to_baseline(context, valid_components):
    port = _FakePort([_Reply(_bad_components(valid_components)),
                      _Reply(_bad_components(valid_components))])
    ledger = _FakeLedger()
    result = GenUIDecoder(port, ledger).decode(context)
    assert result.source == SOURCE_FALLBACK
    assert result.used_fallback is True
    # the fallback IS the deterministic baseline, never a task template
    assert result.components == baseline_components(context)
    # honest trail: 2 model attempts + 1 zero-call fallback event
    assert [a.purpose for a in result.attempts] == \
        ["surface_compose", "surface_repair", "surface_fallback"]
    assert result.model_calls == 2
    fallback_attempt = result.attempts[-1]
    assert fallback_attempt.model == "" and fallback_attempt.ok
    # ledger holds the two REAL requests only (fallback adds no rows)
    assert len(ledger.records) == 2
    assert all(r.ok for r in ledger.records)


def test_transport_failure_is_honest_and_falls_back(context):
    port = _FakePort([TimeoutError("gateway down")])
    ledger = _FakeLedger()
    result = GenUIDecoder(port, ledger, max_repairs=0).decode(context)
    assert result.source == SOURCE_FALLBACK
    rec = ledger.records[0]
    assert rec.ok is False
    assert "TimeoutError" in rec.error
    assert any("failed" in e for e in result.attempts[0].errors)


def test_zero_repair_config(context, valid_components):
    """max_repairs=0 → exactly one model attempt, then fallback."""
    port = _FakePort([_Reply(_bad_components(valid_components))])
    result = GenUIDecoder(port, max_repairs=0).decode(context)
    assert result.source == SOURCE_FALLBACK
    assert len(port.calls) == 1


# ── prompt assembly ─────────────────────────────────────────────────────────

def test_system_prompt_carries_directive_and_catalog_digest(context,
                                                             valid_components):
    port = _FakePort([_Reply(valid_components)])
    GenUIDecoder(port).decode(context)
    system = port.calls[0]["system"]
    assert "GenUI Decoder" in system                  # the directive file
    assert "A2UI Basic Catalog" in system             # the catalog digest
    assert "taskvm.local_patch" in system             # action vocabulary
    assert '"path"' in system                         # v0.9 binding syntax


def test_user_prompt_carries_context_payload(context, valid_components):
    port = _FakePort([_Reply(valid_components)])
    GenUIDecoder(port).decode(context)
    user = port.calls[0]["user"]
    assert "TaskSurfaceContext" in user
    assert "把发布会日期改到 8 月底并通知所有参会人" in user
    assert "release_date" in user
    assert "JSON array" in user                       # output contract


def test_load_system_prompt_is_cached():
    a = load_system_prompt()
    b = load_system_prompt()
    assert a is b
    assert "Button" in a and "18 components" in a


# ── model routing (§20.2) ───────────────────────────────────────────────────

def test_constructor_model_wins(context, valid_components, monkeypatch):
    monkeypatch.setenv(GENUI_DECODER_MODEL_ENV, "env-model")
    port = _FakePort([_Reply(valid_components)])
    GenUIDecoder(port, model="cheap-fast-model").decode(context)
    assert port.calls[0]["model"] == "cheap-fast-model"


def test_env_var_model_used_when_no_constructor_model(
        context, valid_components, monkeypatch):
    monkeypatch.setenv(GENUI_DECODER_MODEL_ENV, "env-model")
    port = _FakePort([_Reply(valid_components)])
    GenUIDecoder(port).decode(context)
    assert port.calls[0]["model"] == "env-model"


def test_port_default_when_no_routing_configured(
        context, valid_components, monkeypatch):
    monkeypatch.delenv(GENUI_DECODER_MODEL_ENV, raising=False)
    port = _FakePort([_Reply(valid_components)])
    GenUIDecoder(port).decode(context)
    assert port.calls[0]["model"] is None             # port decides


def test_ledger_row_records_routed_model(context, valid_components):
    port = _FakePort([_Reply(valid_components, model="routed-model")])
    ledger = _FakeLedger()
    GenUIDecoder(port, ledger, model="routed-model").decode(context)
    assert ledger.records[0].model == "routed-model"


# ── the shared-ledger contract lock ─────────────────────────────────────────

def test_decoder_records_accepted_by_architect_ledger(context,
                                                       valid_components):
    """The injected ledger is the architect's shared ModelCallLedger: a
    DecoderCallRecord must land under the genui_decoder bucket with all
    accounting fields intact (1 provider request = 1 row)."""
    from taskvm.architect.port import ModelCallLedger
    port = _FakePort([_Reply(_bad_components(valid_components)),
                      _Reply(valid_components)])
    ledger = ModelCallLedger()
    result = GenUIDecoder(port, ledger).decode(context)
    assert result.source == SOURCE_MODEL
    assert ledger.counts_by_role() == {"genui_decoder": 2}
    rows = ledger.snapshot()
    assert {r["purpose"] for r in rows} == \
        {"surface_compose", "surface_repair"}
    assert [r["is_repair"] for r in rows] == [False, True]
    assert len({r["request_id"] for r in rows}) == 2  # unique per request


def test_request_ids_unique_per_attempt(context, valid_components):
    port = _FakePort([_Reply(_bad_components(valid_components)),
                      _Reply(_bad_components(valid_components))])
    ledger = _FakeLedger()
    GenUIDecoder(port, ledger).decode(context)
    ids = [r.request_id for r in ledger.records]
    assert len(ids) == len(set(ids)) and all(ids)


# ── result trail ────────────────────────────────────────────────────────────

def test_result_summary_is_machine_readable(context, valid_components):
    port = _FakePort([_Reply(_bad_components(valid_components)),
                      _Reply(valid_components)])
    result = GenUIDecoder(port).decode(context)
    s = result.summary()
    assert s["source"] == "model"
    assert s["model_calls"] == 2
    assert s["component_count"] == len(valid_components)
    assert [a["index"] for a in s["attempts"]] == [1, 2]
    assert s["attempts"][0]["ok"] is False


def test_invalid_max_repairs_rejected():
    with pytest.raises(ValueError):
        GenUIDecoder(_FakePort([]), max_repairs=-1)


def test_decode_attempt_is_immutable_value_object():
    a = DecodeAttempt(index=1, ok=True)
    with pytest.raises(Exception):
        a.ok = False
    r = DecodeResult(components=[], source=SOURCE_MODEL,
                     attempts=(a,))
    assert r.model_calls == 1 and not r.used_fallback
