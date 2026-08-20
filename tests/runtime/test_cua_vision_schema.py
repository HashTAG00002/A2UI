"""B-01 — vision-capable HttpCUAModel + the COMPLETE frozen GuiAction schema.

Pre-fix reality (DSA RM wave): the CUA adapter sent ONLY ``visible_text``
(the ``Observation.screenshot_ref`` was ignored) and its action parser
accepted just 4 kinds with 2 fields (kind/text/coordinate) while the
substrate ``GuiAction`` vocabulary (substrate.md §2) is 7 kinds
(click|tap|type|key|scroll|wait|open) with the full field set
(coordinate/text/key/direction/magnitude/duration_ms/target).

Post-fix (B-01):
  * a fresh screenshot whose ref is a REAL data URL
    ("data:image/…;base64,…") travels as the multimodal image part
    (``complete_json(image_data_url=…)``) — never as prompt text;
  * any other ref shape (file path / artifact id / internal locator) is
    honestly degraded to text-only — never inlined, never guessed;
  * the outgoing protocol exposes the FULL frozen vocabulary and every
    ``GuiAction`` field;
  * missing REQUIRED fields, unknown kinds, illegal directions and
    non-numeric numeric fields RAISE ``CUAReplySchemaError`` — a
    malformed reply is an INVALID PREDICTION the runtime's §5 loop
    re-asks within its small ceiling (GATE-G0 2026-08-20: the old
    FAIL-decision conversion killed a whole real-model trial on one
    schema slip); a DELIBERATE model ``fail`` decision still passes
    through (no guessing, no silent field drops);
  * the no-leak gate covers initial / retry / vision message construction
    — and a rejected prompt issues ZERO provider requests.
"""
from __future__ import annotations

import pytest

from taskvm.architect import ModelReply
from taskvm.runtime.ports import CUADecisionKind
from taskvm.substrate import GUI_ACTION_KINDS, Observation, SurfaceInfo
from taskvm.workspace_ui.composition import (
    CUAReplySchemaError, HttpCUAModel, _CUA_SYSTEM_PROMPT)

DATA_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


class CapturePort:
    """Deterministic port stub: records every message construction and
    echoes a scripted parsed reply (the real port's 1-call-1-request
    discipline is A-13's test; here we only need the messages)."""

    default_model = "stub-model"

    def __init__(self, reply: object = None, raw: str = ""):
        self.reply = reply
        self.raw = raw or str(reply)
        self.calls: list[dict] = []

    def complete_json(self, *, system: str, user: str,
                      model: str | None = None, max_tokens: int = 3072,
                      temperature: float | None = None,
                      image_data_url: str | None = None) -> ModelReply:
        self.calls.append(dict(system=system, user=user, model=model,
                               image_data_url=image_data_url))
        if isinstance(self.reply, Exception):
            raise self.reply
        return ModelReply(parsed=self.reply, raw=self.raw,
                          model=model or "stub")


def _obs(text: str = "标题: 产品发布\n日期: 2026-08-17",
         shot: str | None = None) -> Observation:
    return Observation(
        surface=SurfaceInfo(surface_id="app", display_name="日历"),
        revision=1, timestamp=0.0,
        screenshot_ref=shot, visible_text=text)


# ── vision path ────────────────────────────────────────────────────────────

def test_data_url_screenshot_travels_as_image_part():
    port = CapturePort(reply={"kind": "done"})
    cua = HttpCUAModel(port=port)
    cua.predict_action(goal="把日期改到 2026-08-18",
                       observation=_obs(shot=DATA_URL))
    assert len(port.calls) == 1
    call = port.calls[0]
    assert call["image_data_url"] == DATA_URL          # multimodal part
    assert DATA_URL not in call["user"]                # never prompt TEXT
    assert "iVBOR" not in call["system"]


def test_non_data_url_ref_degrades_to_text_only_honestly():
    port = CapturePort(reply={"kind": "done"})
    cua = HttpCUAModel(port=port)
    # a file path / artifact locator is NOT a screen-visible image payload
    cua.predict_action(goal="把日期改到 2026-08-18",
                       observation=_obs(shot="/internal/shots/ha0042.png"))
    call = port.calls[0]
    assert call["image_data_url"] is None              # not sent as image
    assert "/internal/shots/" not in call["user"]      # not inlined either


def test_no_screenshot_is_plain_text_round():
    port = CapturePort(reply={"kind": "done"})
    HttpCUAModel(port=port).predict_action(
        goal="g", observation=_obs(shot=None))
    assert port.calls[0]["image_data_url"] is None


# ── prompt shape: initial / retry / no-leak ────────────────────────────────

def test_initial_prompt_carries_goal_and_visible_text_only():
    port = CapturePort(reply={"kind": "done"})
    HttpCUAModel(port=port).predict_action(
        goal="把日历事件「产品发布」改期到 2026-08-18",
        observation=_obs())
    user = port.calls[0]["user"]
    assert "产品发布" in user and "2026-08-17" in user
    assert "重试" not in user                       # attempt 1 → no retry tag


def test_retry_prompt_is_tagged_with_attempt():
    port = CapturePort(reply={"kind": "done"})
    HttpCUAModel(port=port).predict_action(
        goal="g", observation=_obs(), attempt=3)
    assert "第 3 次重试" in port.calls[0]["user"]


def test_system_prompt_advertises_full_frozen_vocabulary():
    for kind in GUI_ACTION_KINDS:                   # click..open, all 7
        assert kind in _CUA_SYSTEM_PROMPT
    for field in ("coordinate", "text", "key", "direction",
                  "magnitude", "duration_ms", "target"):
        assert field in _CUA_SYSTEM_PROMPT


def test_no_leak_prompt_is_rejected_before_any_provider_request():
    port = CapturePort(reply={"kind": "done"})
    cua = HttpCUAModel(port=port)
    decision = cua.predict_action(
        goal="操作 entity_id=123 那一行",           # internal id → red line
        observation=_obs())
    assert decision.kind is CUADecisionKind.FAIL
    assert port.calls == [] and cua.request_count == 0   # zero requests


def test_vision_message_construction_stays_leak_free():
    # the screenshot-bearing round must keep the TEXT side clean too
    port = CapturePort(reply={"kind": "done"})
    HttpCUAModel(port=port).predict_action(
        goal="把日期改到 2026-08-18", observation=_obs(shot=DATA_URL))
    assert "data-entity-id" not in port.calls[0]["user"]
    assert "set_state" not in port.calls[0]["user"]


# ── complete GuiAction schema — every legal kind parses ───────────────────

LEGAL = [
    ({"kind": "act", "action": {"kind": "click", "coordinate": [120, 340]}},
     dict(kind="click", coordinate=(120.0, 340.0))),
    ({"kind": "act", "action": {"kind": "tap", "coordinate": [10, 999]}},
     dict(kind="tap", coordinate=(10.0, 999.0))),
    ({"kind": "act", "action": {"kind": "type", "text": "2026-08-18"}},
     dict(kind="type", text="2026-08-18")),
    ({"kind": "act", "action": {"kind": "key", "key": "Enter"}},
     dict(kind="key", key="Enter")),
    ({"kind": "act", "action": {"kind": "scroll", "direction": "down",
                                "magnitude": 3}},
     dict(kind="scroll", direction="down", magnitude=3)),
    ({"kind": "act", "action": {"kind": "wait", "duration_ms": 500}},
     dict(kind="wait", duration_ms=500)),
    ({"kind": "act", "action": {"kind": "open", "target": "日历"}},
     dict(kind="open", target="日历")),
    # click may carry auxiliary text too (label echo) — optional fields
    ({"kind": "act", "action": {"kind": "click", "coordinate": [5, 5],
                                "text": "确定"}},
     dict(kind="click", coordinate=(5.0, 5.0), text="确定")),
]


@pytest.mark.parametrize("reply,expected", LEGAL)
def test_every_legal_action_kind_and_field_parses(reply, expected):
    port = CapturePort(reply=reply)
    decision = HttpCUAModel(port=port).predict_action(
        goal="g", observation=_obs())
    assert decision.kind is CUADecisionKind.ACT
    action = decision.action
    for field, want in expected.items():
        assert getattr(action, field) == want, field
    assert action.kind in GUI_ACTION_KINDS


# ── honest-fail discipline — illegal/missing never guesses ─────────────────

ILLEGAL = [
    # missing REQUIRED field per kind
    ({"kind": "act", "action": {"kind": "click"}}, "coordinate"),
    ({"kind": "act", "action": {"kind": "tap"}}, "coordinate"),
    ({"kind": "act", "action": {"kind": "type"}}, "text"),
    ({"kind": "act", "action": {"kind": "key"}}, "key"),
    ({"kind": "act", "action": {"kind": "open"}}, "target"),
    # unknown kind (not in the frozen vocabulary)
    ({"kind": "act", "action": {"kind": "swipe", "coordinate": [1, 1]}},
     "未知操作类型"),
    # malformed values
    ({"kind": "act", "action": {"kind": "click",
                                "coordinate": ["left", "top"]}},
     "coordinate 不是数值对"),
    ({"kind": "act", "action": {"kind": "scroll", "direction": "diagonal"}},
     "非法滚动方向"),
    ({"kind": "act", "action": {"kind": "wait",
                                "duration_ms": "half-a-second"}},
     "duration_ms 不是整数"),
    ({"kind": "act", "action": {"kind": "scroll", "magnitude": "lots"}},
     "magnitude 不是整数"),
    # missing action object entirely / unknown decision kind
    ({"kind": "act", "action": "click here"}, "缺少 action 对象"),
    ({"kind": "ponder"}, "未知决策类型"),
]


@pytest.mark.parametrize("reply,why", ILLEGAL)
def test_illegal_or_missing_fields_raise_schema_error(reply, why):
    """A reply that violates the frozen decision schema raises
    ``CUAReplySchemaError`` (an INVALID PREDICTION — the runtime's §5
    loop owns the bounded re-ask), never a business FAIL decision.

    The adapter still consumed exactly ONE provider request (the reply
    WAS delivered; the parse rejected it — no hidden re-ask here)."""
    port = CapturePort(reply=reply)
    with pytest.raises(CUAReplySchemaError, match=why):
        HttpCUAModel(port=port).predict_action(
            goal="g", observation=_obs())
    assert len(port.calls) == 1


def test_unparseable_reply_raises_schema_error():
    port = CapturePort(reply=None, raw="抱歉我不能……")
    with pytest.raises(CUAReplySchemaError, match="无法解析"):
        HttpCUAModel(port=port).predict_action(
            goal="g", observation=_obs())
    assert len(port.calls) == 1                     # one request, no re-ask


def test_done_and_fail_decisions_pass_through():
    for reply, want in (({"kind": "done"}, CUADecisionKind.DONE),
                        ({"kind": "fail", "reason": "页面没有该事件"},
                         CUADecisionKind.FAIL)):
        decision = HttpCUAModel(
            port=CapturePort(reply=reply)).predict_action(
                goal="g", observation=_obs())
        assert decision.kind is want
