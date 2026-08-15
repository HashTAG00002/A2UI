"""tests/substrate/test_builtin_web_handles.py — builtin-web handle-cache
semantics (B-3; contract §3: SurfaceHandle invalidation).

``WebSubstrateSession`` accepts an injected browser controller, so the
producer side of the handle cache is testable without Playwright. What is
locked:

  * every Observation REBUILDS its handle candidates from the CURRENT
    visible structure — handles carry the current fingerprint + revision,
    so a consumer can detect staleness by comparing fingerprints (a stale
    handle is never silently reused);
  * ``previous_fingerprint_matched`` reports structure change honestly;
  * handle ids are TaskVM-owned opaque tokens (h1/h2/…), not app DB keys;
  * the visible text is scrubbed (data-*-id never enters the Observation).
"""
from __future__ import annotations

import re

from taskvm.substrate import SurfaceHandle
from taskvm.substrate.builtin_web.session import WebSubstrateSession


class FakeBrowser:
    """BrowserController double: the exact surface WebSubstrateSession
    touches (visible text / dom digest / a11y tree / screenshots)."""

    def __init__(self):
        self.digest = "digest-v1"
        self.tree = {
            "role": "root",
            "children": [
                {"role": "button", "name": "Save", "children": []},
                {"role": "link", "name": "Open task", "children": []},
                {"role": "textbox", "name": "Search", "children": []},
            ],
        }

    def goto(self, url):
        pass

    def wait_load(self):
        pass

    def screenshot_data_url(self):
        return "data:image/png;base64,ZmFrZQ=="

    def save_screenshot(self, path):
        pass

    def visible_text(self):
        return 'Save Open task Search <tr data-task-id="T9">x</tr>'

    def dom_digest(self):
        return self.digest

    def accessibility_tree(self):
        return self.tree


def _session(browser=None) -> WebSubstrateSession:
    return WebSubstrateSession(app="taskboard",
                               url="http://localhost:3014/tb1",
                               browser=browser or FakeBrowser())


def test_handles_are_rebuilt_per_observation_with_current_fingerprint():
    b = FakeBrowser()
    s = _session(b)
    obs1 = s.observe()
    assert obs1.handle_candidates, "expected button/link/textbox handles"
    fp1 = obs1.fingerprint

    # the structure changes → fingerprint changes → obs2's handles carry
    # the NEW fingerprint (stale handles are detectable, never reused)
    b.digest = "digest-v2"
    obs2 = s.observe(previous_fingerprint=fp1)
    assert obs2.fingerprint != fp1
    assert obs2.previous_fingerprint_matched is False
    for h in obs2.handle_candidates:
        assert h.fingerprint == obs2.fingerprint
        assert h.last_seen_revision == obs2.revision
    # obs1's handles are not carried over verbatim: same anchors, new stamp
    assert all(h.fingerprint == fp1 for h in obs1.handle_candidates)


def test_structure_unchanged_reports_match_and_new_revision():
    s = _session()
    obs1 = s.observe()
    obs2 = s.observe(previous_fingerprint=obs1.fingerprint)
    assert obs2.previous_fingerprint_matched is True
    assert obs2.revision > obs1.revision


def test_handle_ids_are_opaque_taskvm_tokens():
    s = _session()
    obs = s.observe()
    for h in obs.handle_candidates:
        assert isinstance(h, SurfaceHandle)
        assert re.fullmatch(r"h\d+", h.handle_id), (
            f"handle_id {h.handle_id!r} must be an opaque TaskVM token "
            "(h1/h2/…), never an app DB primary key")
        assert h.surface_id == "web:taskboard"
    roles = {h.anchor_role for h in obs.handle_candidates}
    assert roles == {"button", "link", "textbox"}
    names = {h.anchor_text for h in obs.handle_candidates}
    assert names == {"Save", "Open task", "Search"}


def test_visible_text_is_scrubbed_in_observation():
    s = _session()
    obs = s.observe()
    assert "data-task-id" not in obs.visible_text
    assert "data-[redacted]" in obs.visible_text
    assert "Save" in obs.visible_text


def test_first_observation_reports_none_for_previous_match():
    obs = _session().observe()
    assert obs.previous_fingerprint_matched is None
