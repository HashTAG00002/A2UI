"""Store tests (contract §5): composition seam — session registration,
artifact store, surface declarations.
"""
from __future__ import annotations

import pytest
import threading
import time

from taskvm.domain import TaskIntent, TaskVariable
from taskvm.kernel import TaskVMKernel

from taskvm.projection.store import (
    ArtifactStore,
    ProjectionSession,
    ProjectionSessionStore,
    SurfaceDecl,
)


# ── ArtifactStore ─────────────────────────────────────────────────────────

class TestArtifactStore:
    def test_put_and_get(self):
        store = ArtifactStore()
        store.put("ref1", b"data1")
        art = store.get("ref1")
        assert art is not None
        assert art.data == b"data1"
        assert art.mime == "image/png"
        assert art.captured_at > 0

    def test_get_missing_returns_none(self):
        store = ArtifactStore()
        assert store.get("nonexistent") is None

    def test_has(self):
        store = ArtifactStore()
        store.put("ref1", b"data")
        assert store.has("ref1")
        assert not store.has("ref2")

    def test_put_empty_ref_raises(self):
        store = ArtifactStore()
        with pytest.raises(ValueError, match="non-empty"):
            store.put("", b"data")

    def test_latest_ref(self):
        store = ArtifactStore()
        store.put("ref1", b"d1", captured_at=1.0)
        store.put("ref2", b"d2", captured_at=2.0)
        store.put("ref3", b"d3", captured_at=3.0)
        assert store.latest_ref(["ref1", "ref2", "ref3"]) == "ref3"
        assert store.latest_ref(["ref1", "ref2"]) == "ref2"

    def test_latest_ref_empty(self):
        store = ArtifactStore()
        assert store.latest_ref([]) is None
        assert store.latest_ref(["nonexistent"]) is None

    def test_put_overwrites(self):
        store = ArtifactStore()
        store.put("ref1", b"old", captured_at=1.0)
        store.put("ref1", b"new", captured_at=2.0)
        art = store.get("ref1")
        assert art.data == b"new"
        assert art.captured_at == 2.0

    def test_thread_safe(self):
        """Concurrent put/get from multiple threads — no crash."""
        store = ArtifactStore()
        errors = []

        def writer():
            try:
                for i in range(50):
                    store.put(f"ref_{threading.current_thread().name}_{i}",
                              b"data", captured_at=float(i))
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(50):
                    store.get("ref_0_0")
                    store.latest_ref(["ref_0_0"])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, name=f"w{i}")
                   for i in range(3)]
        threads += [threading.Thread(target=reader, name=f"r{i}")
                    for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# ── ProjectionSessionStore ───────────────────────────────────────────────

def _make_kernel(sid="s1"):
    intent = TaskIntent(goal="测试")
    kernel = TaskVMKernel(sid, intent)
    kernel.init_task_state([
        TaskVariable(semantic_key="x", label="X", observed="a", desired="b"),
    ])
    return kernel


class TestProjectionSessionStore:
    def test_register_and_get(self):
        store = ProjectionSessionStore()
        kernel = _make_kernel("s1")
        sess = store.register("s1", kernel)
        assert sess.sid == "s1"
        assert store.get("s1") is sess

    def test_register_empty_sid_raises(self):
        store = ProjectionSessionStore()
        with pytest.raises(ValueError, match="non-empty"):
            store.register("", _make_kernel())

    def test_register_duplicate_raises(self):
        store = ProjectionSessionStore()
        store.register("s1", _make_kernel())
        with pytest.raises(ValueError, match="already registered"):
            store.register("s1", _make_kernel())

    def test_get_missing_returns_none(self):
        store = ProjectionSessionStore()
        assert store.get("nonexistent") is None

    def test_drop(self):
        store = ProjectionSessionStore()
        store.register("s1", _make_kernel())
        assert store.drop("s1") is True
        assert store.get("s1") is None
        assert store.drop("s1") is False

    def test_sids_sorted(self):
        store = ProjectionSessionStore()
        store.register("c", _make_kernel())
        store.register("a", _make_kernel())
        store.register("b", _make_kernel())
        assert store.sids() == ["a", "b", "c"]

    def test_register_with_surfaces_and_artifacts(self):
        store = ProjectionSessionStore()
        art = ArtifactStore()
        art.put("ref1", b"data")
        surfaces = [SurfaceDecl(surface_id="s1", display_name="X平台")]
        sess = store.register("s1", _make_kernel(),
                              surfaces=surfaces, artifacts=art)
        assert len(sess.surfaces) == 1
        assert sess.surfaces[0].display_name == "X平台"
        assert sess.artifacts.has("ref1")

    def test_register_with_model_call_probe(self):
        store = ProjectionSessionStore()
        calls = [0]
        def probe():
            calls[0] += 1
            return calls[0]
        sess = store.register("s1", _make_kernel(),
                              model_call_probe=probe)
        assert sess.model_call_probe() == 1
        assert sess.model_call_probe() == 2


# ── ProjectionSession.governance_port ────────────────────────────────────

class TestGovernancePortLazy:
    def test_lazy_init_governance_port(self):
        """governance_port() lazily initializes KernelGovernancePort."""
        kernel = _make_kernel("s1")
        sess = ProjectionSession(sid="s1", kernel=kernel)
        assert sess.governance is None
        port = sess.governance_port()
        assert port is not None
        assert sess.governance is port  # cached

    def test_pre_injected_governance_port(self):
        from taskvm.projection.services.governance import KernelGovernancePort
        kernel = _make_kernel("s1")
        port = KernelGovernancePort(kernel)
        sess = ProjectionSession(sid="s1", kernel=kernel, governance=port)
        assert sess.governance_port() is port


# ── SurfaceDecl ───────────────────────────────────────────────────────────

class TestSurfaceDecl:
    def test_surface_decl_fields(self):
        decl = SurfaceDecl(surface_id="surf1", display_name="X平台")
        assert decl.surface_id == "surf1"
        assert decl.display_name == "X平台"

    def test_surface_decl_frozen(self):
        decl = SurfaceDecl(surface_id="surf1", display_name="X")
        with pytest.raises(Exception):
            decl.display_name = "Y"
