"""A-01 — the production multi-surface resolver wiring, end to end.

``tests/runtime/test_multisurface_routing.py`` pins the RUNTIME's routing
behaviour with a fake resolver. THIS file pins the COMPOSITION side that
the runtime contract says must exist: ``bootstrap_real_full`` wires the
REAL ``EvidenceSurfaceResolver`` (compiler handle provenance → bootstrap
VisibleRegion labels → session surface ids) into
``compose_task_runtime`` → ``compose_runtime`` → execution / verification
/ compensation.

Contract under test (A-01, no-``surfaces[0]`` discipline):
  * bootstrap builds the resolver from the frozen provenance chain
    (compiler handle_id → handle_evidence.surface_label → the same label
    the VisibleRegions carried → list_surfaces().surface_id);
  * two surfaces each receive EXACTLY their own actions;
  * the verifier observes the surface the binding points at;
  * a compensation entry rolls back on the surface that owns the binding;
  * a recreated / conflicting / vanished surface FAILS CLOSED — honest
    StructureInvalidated + node failure, never a write on another surface.

The whole chain runs the REAL composition objects (StateCompiler /
TaskArchitect / kernel init from the architect product / HttpCUAModel
decision parsing / VisibleVerifier / HandleCacheExtractor) — only the
provider transport is a scripted port (contract-wiring policy, B-07).
"""
from __future__ import annotations

import pytest

from taskvm.architect import ModelCallLedger, ModelReply
from taskvm.runtime import RuntimeEventKind
from taskvm.substrate import SurfaceInfo
from taskvm.workspace_ui.composition import (
    EvidenceSurfaceResolver, bootstrap_real_full,
)

from tests.runtime.conftest import FakeSubstrate

GOAL = "把 app 的 x 设为 A,再把 notes 的 y 设为 B"

# ── scripted provider replies (call order: compiler → architect → CUA…) ────

COMPILER_REPLY = {
    "variables": [
        {"semantic_key": "x", "label": "x", "value_type": "string",
         "mutability": "editable", "observed": "x0", "confidence": 0.9,
         "evidence": [{
             "surface_label": "app", "visible_label": "x",
             "visible_context": "x=x0", "value_pattern": r"x=(\S+)"}]},
        {"semantic_key": "y", "label": "y", "value_type": "string",
         "mutability": "editable", "observed": "y0", "confidence": 0.9,
         "evidence": [{
             "surface_label": "notes", "visible_label": "y",
             "visible_context": "y=y0", "value_pattern": r"y=(\S+)"}]},
    ],
    "ambiguities": [], "needs_clarification": False,
}

ARCHITECT_REPLY_BASE = {
    "variables": [
        {"semantic_key": "x", "label": "x", "desired": "A"},
        {"semantic_key": "y", "label": "y", "desired": "B"},
    ],
    "workflow": {"nodes": [
        {"kind": "action", "label": "set-x", "semantic_goal": "set x",
         "sets": {"x": "A"}, "completion": "x==A",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["x"]},
        {"kind": "checkpoint", "label": "cp", "after": ["set-x"]},
        {"kind": "action", "label": "set-y", "semantic_goal": "set y",
         "sets": {"y": "B"}, "completion": "y==B",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["y"], "after": ["cp"]},
        {"kind": "terminal", "label": "done", "after": ["set-y"]},
    ]},
}

ARCHITECT_REPLY_VERIFY = {
    "variables": ARCHITECT_REPLY_BASE["variables"],
    "workflow": {"nodes": [
        ARCHITECT_REPLY_BASE["workflow"]["nodes"][0],
        {"kind": "action", "label": "set-y", "semantic_goal": "set y",
         "sets": {"y": "B"}, "completion": "y==B",
         "reversibility": "reversible", "risk": "",
         "target_evidence": ["y"], "after": ["set-x"]},
        {"kind": "verify", "label": "check-y", "condition": "y==B",
         "after": ["set-y"]},
        {"kind": "terminal", "label": "done", "after": ["check-y"]},
    ]},
}


def _act(text: str) -> dict:
    return {"kind": "act", "action": {"kind": "type", "text": text}}


def _done() -> dict:
    return {"kind": "done"}


class ScriptedPort:
    """One scripted reply per REAL provider request, in call order."""

    default_model = "scripted-ms"

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []

    def complete_json(self, *, system, user, model=None, max_tokens=3072,
                      temperature=None, image_data_url=None):
        self.calls.append(system + "\n--\n" + user)
        item = self.script.pop(0) if self.script else _done()
        return ModelReply(parsed=item, raw=str(item), model=model or "s",
                          prompt_tokens=5, completion_tokens=3)


class DisplaySubstrate(FakeSubstrate):
    """FakeSubstrate with explicit display names — lets tests build the
    recreate/rename/conflict scenarios (same label, different surface_id)
    while list_surfaces() stays the single source of truth."""

    def __init__(self, worlds: dict, display: dict[str, str]):
        super().__init__(worlds)
        self._display = dict(display)

    def list_surfaces(self) -> list[SurfaceInfo]:
        return [SurfaceInfo(surface_id=sid,
                            display_name=self._display.get(sid, sid))
                for sid in self.world]


def _bootstrap(sub, script, *, architecture=None):
    ledger = ModelCallLedger()
    port = ScriptedPort([COMPILER_REPLY,
                         architecture or ARCHITECT_REPLY_BASE, *script])
    bundle = bootstrap_real_full(
        goal=GOAL, sid="ms-comp", substrate=sub,
        model_port=port, ledger=ledger)
    return bundle, port


def _node_id(bundle, label):
    for node in bundle["kernel"].workflow().graph.nodes:
        if node.label == label:
            return node.node_id
    raise AssertionError(f"node with label {label!r} not found")


def _status(bundle, label):
    snap = bundle["kernel"].workflow()
    return snap.statuses.get(_node_id(bundle, label))


# ── ① each surface receives EXACTLY its own action ─────────────────────────
def test_two_surfaces_each_receive_their_own_action():
    sub = FakeSubstrate({"app": {"x": "x0"}, "notes": {"y": "y0"}})
    bundle, port = _bootstrap(
        sub, [_act("x=A"), _done(), _act("y=B"), _done()])

    bundle["runtime"].run()

    assert _status(bundle, "set-x").value == "committed"
    assert _status(bundle, "set-y").value == "committed"
    assert sub.world["app"] == {"x": "A"}
    assert sub.world["notes"] == {"y": "B"}
    # the type gestures landed on their OWN surfaces — never surfaces[0]
    assert [("app", "type"), ("notes", "type")] == [
        (sid, kind) for sid, kind in sub.act_log if kind == "type"]
    # the PRODUCTION resolver was actually consulted (not the trivial path)
    assert bundle["surface_resolver"].asks
    assert isinstance(bundle["surface_resolver"], EvidenceSurfaceResolver)


# ── ② the verifier reads the surface the binding points at ─────────────────
def test_verifier_reads_the_bound_surface_not_surface_zero():
    sub = FakeSubstrate({"app": {"x": "x0"}, "notes": {"y": "y0"}})
    bundle, port = _bootstrap(
        sub, [_act("x=A"), _done(), _act("y=B"), _done()],
        architecture=ARCHITECT_REPLY_VERIFY)

    bundle["runtime"].run()

    assert _status(bundle, "check-y").value == "committed"
    # the verify observation was served by the TARGET surface (notes)
    assert sub.observe_log[-1] == "notes"


# ── ③ compensation rolls back on the ORIGINAL surface ──────────────────────
def test_compensation_entry_routes_back_to_its_original_surface():
    from taskvm.domain.patch import CompensationPatch

    sub = FakeSubstrate({"app": {"x": "x0"}, "notes": {"y": "y0"}})
    # forward (x=A, y=B) then the compensation gesture (y back to y0)
    bundle, port = _bootstrap(
        sub, [_act("x=A"), _done(), _act("y=B"), _done(),
              _act("y=y0")])
    bundle["runtime"].run()
    assert _status(bundle, "set-y").value == "committed"

    kernel = bundle["kernel"]
    plan = kernel.request_compensation(CompensationPatch(
        patch_id="rb",
        target_checkpoint_id=f"ckpt:{_node_id(bundle, 'cp')}"))
    assert [e.semantic_key for e in plan.entries] == ["y"]

    fwd_acts = len(sub.act_log)
    disposition = bundle["runtime"].execute_compensation(plan)

    assert disposition == "complete"
    assert sub.world["notes"] == {"y": "y0"}      # restored on the ORIGIN
    assert sub.world["app"] == {"x": "A"}         # pre-checkpoint, untouched
    comp_acts = sub.act_log[fwd_acts:]
    assert comp_acts == [("notes", "type")]       # rollback hit ONLY notes


# ── ④ recreated / vanished / conflicting surfaces FAIL CLOSED ──────────────
def test_recreated_surface_fails_closed_without_wrong_surface_write():
    """The bound surface ``notes`` died and a DIFFERENT surface id now
    answers to the same visible label — the resolver must return None
    (recreated ⇒ fail closed), the node fails honestly, and NO gesture
    lands on the impostor surface."""
    sub = DisplaySubstrate({"app": {"x": "x0"}, "notes": {"y": "y0"}},
                           {"app": "app", "notes": "notes"})
    bundle, port = _bootstrap(
        sub, [_act("x=A"), _done(), _act("y=B"), _done()])
    # after bootstrap, the original surface is gone; a new id claims the
    # SAME visible label (the classic window-closed-and-reopened shape)
    del sub.world["notes"]
    sub.world["notes-2"] = {"y": "y0"}
    sub._display["notes-2"] = "notes"

    bundle["runtime"].run()

    assert _status(bundle, "set-x").value == "committed"
    assert _status(bundle, "set-y").value == "failed"   # honest fail
    assert sub.world["notes-2"] == {"y": "y0"}   # the impostor untouched
    assert sub.world["app"] == {"x": "A"}        # only set-x's own surface
    assert ("notes-2", "type") not in sub.act_log
    assert any(e.kind is RuntimeEventKind.STRUCTURE_INVALIDATED
               for e in bundle["runtime"].runtime_events())


def test_label_conflict_fails_closed_without_gui_write():
    """Two live surfaces answer to the SAME visible label — the binding is
    ambiguous, the resolver returns None and no gesture is written on
    either claimant. (The conflict is created AFTER bootstrap: a duplicate
    label at observe time is already rejected by the compiler view's own
    constructor — the resolver's LIVE uniqueness check owns the drift
    that appears while the session runs.)"""
    sub = DisplaySubstrate(
        {"app": {"x": "x0"}, "notes": {"y": "y0"}, "notes-b": {"y": "y0"}},
        {"app": "app", "notes": "notes", "notes-b": "notes-b"})
    bundle, port = _bootstrap(
        sub, [_act("x=A"), _done(), _act("y=B"), _done()])
    # mid-session, the second window renames itself onto the FIRST's
    # label — the binding is now ambiguous
    sub._display["notes-b"] = "notes"

    bundle["runtime"].run()

    assert _status(bundle, "set-y").value == "failed"
    assert ("notes", "type") not in sub.act_log
    assert ("notes-b", "type") not in sub.act_log


def test_vanished_surface_fails_closed():
    """The bound surface simply closed (no impostor): honest fail, no
    write anywhere else."""
    sub = FakeSubstrate({"app": {"x": "x0"}, "notes": {"y": "y0"}})
    bundle, port = _bootstrap(
        sub, [_act("x=A"), _done(), _act("y=B"), _done()])
    del sub.world["notes"]

    bundle["runtime"].run()

    assert _status(bundle, "set-y").value == "failed"
    assert sub.world["app"] == {"x": "A"}
    assert ("app", "type") in sub.act_log          # set-x was fine
    type_after = [t for t in sub.act_log if t == ("app", "type")]
    assert len(type_after) == 1                    # no retry storm either


# ── resolver unit semantics (the frozen provenance chain) ───────────────────
def test_resolver_maps_the_frozen_provenance_chain():
    sub = DisplaySubstrate({"app": {"x": "x0"}, "notes": {"y": "y0"}},
                           {"app": "app", "notes": "notes"})
    resolver = EvidenceSurfaceResolver(
        sub,
        handle_labels={"h001": "notes", "h002": "app"},
        bound_surfaces={"app": "app", "notes": "notes"})
    assert resolver.resolve_surface("h001") == "notes"
    assert resolver.resolve_surface("h002") == "app"
    assert resolver.resolve_surface("unknown") is None     # no provenance
    assert resolver.resolve_surface("h001", visible_label="y") == "notes"


def test_resolver_reads_are_live_and_read_only():
    """A rename after construction is caught by the LIVE check — the
    resolver re-reads list_surfaces() every resolve (0 model calls)."""
    sub = DisplaySubstrate({"app": {"x": "x0"}, "notes": {"y": "y0"}},
                           {"app": "app", "notes": "notes"})
    resolver = EvidenceSurfaceResolver(
        sub, handle_labels={"h001": "notes"},
        bound_surfaces={"notes": "notes"})
    sub._display["notes"] = "renamed"     # the surface was renamed live
    assert resolver.resolve_surface("h001") is None
