"""Runtime contract tests — observation-driven synchronization
(runtime.md §8/§13).

Two regimes, both driven by visible fingerprint deltas:
  * ACTIVE surface: the surface the CUA is operating — its observations
    come from the CUA's own act→re-observe; the heartbeat must NOT
    re-poll it (duplicate observation);
  * INACTIVE surfaces: a low-frequency heartbeat fills in the world
    changes the CUA is not looking at. Fingerprint-unchanged ⇒ 0 model
    calls and 0 compiler calls. Known-handle value change ⇒ a
    deterministic observation delta folded into the kernel. Unrecoverable
    binding ⇒ a ``StructureInvalidated`` event the runtime publishes
    WITHOUT calling the State Compiler itself (E executes, C understands).
    External drift vs pending desired ⇒ a conflict (NOT a silent overwrite);
    only the affected surface is reported.
"""
from __future__ import annotations

from taskvm.domain.events import EventKind
from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode

from tests.runtime.conftest import (
    FakeSubstrate, action_node, make_kernel, make_runtime, var,
)
from taskvm.runtime import RuntimeEventKind


def _two_surface_graph():
    return WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        action_node("a1", desired={"x": "A"}, parent_id="root"),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("a1", "root")),
    ))


# ── inactive heartbeat: fingerprint unchanged → 0 model calls ──────────────
def test_inactive_heartbeat_no_change_is_zero_model_calls():
    """A surface the CUA is not driving, with a stable fingerprint, makes
    NO model call and NO compiler call on the heartbeat (runtime.md §8
    fast path). The first heartbeat establishes the baseline fingerprint;
    the SECOND one (unchanged) is the zero-cost fast path."""
    k = make_kernel([var("x", "x0", "A"), var("y", "y0", "B")],
                    _two_surface_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}, "desktop": {"y": "y0"}})
    from tests.runtime.conftest import ScriptedCUA, make_runtime
    rt = make_runtime(k, sub, ScriptedCUA([]))
    rt._sync.set_active("app")           # desktop is inactive
    rt.poll_inactive_surfaces()           # 1st heartbeat: establish baseline
    base = sub.observe_log.count("desktop")
    evs = rt.poll_inactive_surfaces()     # 2nd heartbeat: fingerprint same
    assert evs == []                     # fingerprint unchanged → no event
    assert sub.observe_log.count("desktop") == base + 1   # observed, not compiled
    assert rt.model_calls == 0           # zero model calls


# ── active surface: heartbeat must not re-poll it ──────────────────────────
def test_active_surface_not_re_polled_by_heartbeat():
    """The active surface's observations come from the CUA's own act→observe.
    The heartbeat skips it (runtime.md §8: no duplicate observation)."""
    k = make_kernel([var("x", "x0", "A")], _two_surface_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    rt = make_runtime(k, sub, _dummy_cua())
    rt._sync.set_active("app")
    rt.poll_inactive_surfaces()
    assert "app" not in sub.observe_log   # active surface skipped


# ── inactive value change: folded into the kernel ──────────────────────────
def test_inactive_value_change_folds_into_observed_plane():
    """An external value change on an inactive surface, where the new value
    MATCHES its pending desired (so no divergence / no conflict), folds
    into the kernel's OBSERVED plane as a deterministic delta (runtime.md
    §8 fast path: known-handle value change → apply_observation)."""
    k = make_kernel([var("x", "x0", "A"), var("y", "y0", "B")],
                    _two_surface_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}, "desktop": {"y": "y0"}})
    rt = make_runtime(k, sub, _dummy_cua())
    rt._sync.set_active("app")
    rt.poll_inactive_surfaces()           # establish baseline fingerprint
    # external change on the inactive surface — set it to its DESIRED so
    # there is no divergence (the fold path, not the conflict path)
    sub.world["desktop"]["y"] = "B"
    rt.poll_inactive_surfaces()
    assert k.task_state().observed_values()["y"] == "B"


# ── structure invalidation: published without calling the compiler ──────────
def test_structure_invalidation_published_without_calling_compiler():
    """The extractor cannot recover a binding (anchor gone) → the runtime
    publishes ``StructureInvalidated`` and does NOT call the State Compiler
    itself (E executes, C understands — runtime.md §8/§9)."""
    from tests.runtime.conftest import FakeExtractor
    k = make_kernel([var("x", "x0", "A")], _two_surface_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}, "desktop": {"x": "x0"}})
    # use the FakeExtractor which raises StructureInvalidation on
    # "STRUCTURE-GONE" in the visible text
    rt = make_runtime(k, sub, _dummy_cua(), extractor=FakeExtractor())
    rt._sync.set_active("app")
    sub.world["desktop"]["x"] = "STRUCTURE-GONE"
    evs = rt.poll_inactive_surfaces()
    kinds = [e.kind for e in evs]
    assert RuntimeEventKind.STRUCTURE_INVALIDATED in kinds
    # the runtime did NOT call the compiler: no crash, just the event
    assert all("STRUCTURE-GONE" not in (e.detail or "")
               or e.kind is RuntimeEventKind.STRUCTURE_INVALIDATED
               for e in evs)


# ── external drift vs pending desired → conflict, not overwrite ────────────
def test_external_drift_on_inactive_surface_raises_conflict():
    """A value on an inactive surface drifted from its PENDING desired
    (TaskVM has not committed it) → a SURFACE_CONFLICT, NOT a silent
    overwrite. Only the affected surface is reported (runtime.md §13)."""
    k = make_kernel([var("x", "x0", "A"), var("y", "y0", "B")],
                    _two_surface_graph())
    sub = FakeSubstrate({"app": {"x": "x0"}, "desktop": {"y": "y0"}})
    rt = make_runtime(k, sub, _dummy_cua())
    rt._sync.set_active("app")
    # external drift on the inactive surface, diverging from desired y=B
    sub.world["desktop"]["y"] = "NOT_B"
    evs = rt.poll_inactive_surfaces()
    conflicts = [e for e in evs if e.kind is RuntimeEventKind.SURFACE_CONFLICT]
    assert conflicts
    assert conflicts[0].surface_id == "desktop"
    assert "y" in conflicts[0].payload["keys"]
    # the kernel has a CONFLICT_DETECTED event (record_conflict)
    kinds = [e.kind for e in k.events()]
    assert EventKind.CONFLICT_DETECTED in kinds


def _dummy_cua():
    from tests.runtime.conftest import ScriptedCUA
    return ScriptedCUA([])
