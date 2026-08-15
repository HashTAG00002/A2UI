"""tests/substrate/test_visibility_scrubbing.py — zero-exposure scrubbing
(B-3; contract §6: no app-internal identity may enter an Observation).

``scrub_hidden_ids`` is load-bearing for the judgement "can a real user
SEE this string on the rendered screen?" — data attributes are never
rendered, so they are redacted before the observation leaves the
substrate.
"""
from __future__ import annotations

from taskvm.substrate import scrub_hidden_ids


def test_scrubs_every_visible_id_pattern():
    """Every pattern in the frozen VISIBLE_ID_PATTERNS family is redacted."""
    samples = [
        '<tr data-chat-id="wx_123"><td>Hi</td></tr>',
        '<tr data-event-id="E1"><td>Standup</td></tr>',
        '<tr data-task-id="T9"><td>ship</td></tr>',
        '<tr data-file-id="F2"><td>a.pdf</td></tr>',
        '<tr data-post-id="p7"><td>hello</td></tr>',
        '<tr data-transaction-id="tx1"><td>¥20</td></tr>',
        '<tr data-appointment-id="ap3"><td>dentist</td></tr>',
        '<tr data-mail-id="m4"><td>re: hi</td></tr>',
        'data-action-params="{}"',
    ]
    for s in samples:
        out = scrub_hidden_ids(s)
        assert "data-[redacted]" in out, f"not redacted: {s!r} -> {out!r}"
        for marker in ("wx_123", "E1\"", "T9\"", "F2\"", "p7\"", "tx1\"",
                       "ap3\"", "m4\"", "x99"):
            assert marker not in out, f"leaked {marker!r} from {s!r}"


def test_visible_text_is_untouched():
    """Rendered content survives scrubbing byte-for-byte."""
    visible = "<td>黄勇: 把周会改到周三</td><button>Send</button>"
    assert scrub_hidden_ids(visible) == visible


def test_scrubbing_is_idempotent():
    once = scrub_hidden_ids('<tr data-chat-id="c1"><td>x</td></tr>')
    assert scrub_hidden_ids(once) == once


def test_serialized_observation_roundtrip_is_clean():
    """A serialized observation carrying hidden attrs is scrubbed BEFORE a
    model ever sees it (the E16/E21 leak class)."""
    blob = ('<div data-post-id="p7" class="row">'
            '<span>Team offsite poll</span></div>')
    clean = scrub_hidden_ids(blob)
    assert "data-post-id" not in clean
    assert "Team offsite poll" in clean
