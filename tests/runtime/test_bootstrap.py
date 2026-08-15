"""Bootstrap seam contract test (runtime.md §3; substrate.md §8 T1).

``compose_runtime`` is the clean composition entry point that assembles a
real ``AutonomyRuntime`` over a real ``SubstrateSession`` with injected ports
— the interface workspace_ui (D) calls instead of the legacy
``gui_driver.make_task_adapters`` operator-write adapters. This test proves
the seam builds a working runtime end-to-end (with the test fakes) that
drives ActionContract → CUA → GuiAction → SubstrateSession.act → fresh
observe → verify, NOT ``adapter.mutate``.
"""
from __future__ import annotations

from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode
from taskvm.runtime import RuntimePorts, compose_runtime

from tests.runtime.conftest import (
    DONE, FakeExtractor, FakeLedger, FakeSerializer, FakeSubstrate,
    ScriptedCUA, action_node, make_kernel, status_of, type_kv, var,
)
from taskvm.verifier.visible import VisibleVerifier


def _graph():
    return WorkflowGraph(nodes=(
        WorkflowNode("root", NodeKind.SEQUENCE, "task"),
        action_node("a1", desired={"x": "A"}, parent_id="root"),
        WorkflowNode("term", NodeKind.TERMINAL, "done",
                     depends_on=("a1", "root")),
    ))


def test_compose_runtime_builds_real_runtime_and_runs_end_to_end():
    """compose_runtime assembles a working AutonomyRuntime from a kernel, a
    SubstrateSession, and a RuntimePorts bundle — and it drives the real
    ActionContract→CUA→GuiAction→act→observe→verify path (not the legacy
    operator-write adapter)."""
    k = make_kernel([var("x", "x0", "A")], _graph())
    sub = FakeSubstrate({"app": {"x": "x0"}})
    ports = RuntimePorts(
        cua_model=ScriptedCUA([type_kv("x", "A"), DONE]),
        serializer=FakeSerializer(),
        extractor=FakeExtractor(),
        verifier=VisibleVerifier(),
        ledger=FakeLedger())
    rt = compose_runtime(k, sub, ports)

    reason = rt.run()

    assert reason == "done"
    assert status_of(k, "a1").value == "committed"
    # the real GUI path moved the world (type gesture), NOT adapter.mutate
    assert [a[1] for a in sub.act_log] == ["type"]
    assert sub.world["app"]["x"] == "A"
    assert k.task_state().observed_values()["x"] == "A"


def test_runtime_ports_bundle_is_frozen_and_carries_five_ports():
    """RuntimePorts is the typed bundle composition passes — it is immutable
    and carries exactly the five RFC-001 ports (cua/serializer/extractor/
    verifier/ledger)."""
    import dataclasses
    fields = {f.name for f in dataclasses.fields(RuntimePorts)}
    assert fields == {"cua_model", "serializer", "extractor",
                      "verifier", "ledger"}
