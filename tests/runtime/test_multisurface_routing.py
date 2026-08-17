"""A-01 multi-surface target routing — the five DSA counter-examples.

Pre-fix reality (DSA audit, RM wave): the runtime defaulted to
``surfaces[0]`` at every routing decision (autonomy.py:151,231,456-era), so
evidence gathered on the SECOND surface drove a GUI write on the FIRST —
a wrong-surface write the projection then honestly displayed as the task
state it was NOT.

Post-fix contract under test (runtime.md multi-surface + A-01):
  * an ActionContract routes to the surface its TARGET EVIDENCE was
    grounded on (compiler handle → composition resolver → session surface);
  * a VERIFY node reads the surface its predicate key is bound to;
  * a compensation entry lands on the surface that owns the variable's
    binding (the same chain, evidence-driven);
  * one session may bind different variables to different surfaces;
  * a STALE handle rebinds through its visible label (deterministic,
    read-only, 0 model calls) and an UNRESOLVABLE binding in a
    multi-surface session is an honest fail — NEVER a ``surfaces[0]``
    fallback with a GUI gesture.

The single-surface session stays routing-trivial (one candidate, no
ambiguity) — covered by the whole pre-existing runtime suite, not here.
"""
from __future__ import annotations

from taskvm.domain.contract import ActionContract
from taskvm.domain.patch import CompensationPatch
from taskvm.domain.state import (
    ObservedValue, SurfaceEvidence, SurfaceHandle, TaskVariable,
)
from taskvm.domain.workflow import NodeKind, WorkflowGraph, WorkflowNode
from taskvm.runtime import AutonomyRuntime, RuntimeEventKind
from taskvm.runtime.sync import StructureInvalidation
from taskvm.verifier.visible import VisibleVerifier

from tests.runtime.conftest import (
    DONE, FakeExtractor, FakeLedger, FakeSerializer, FakeSubstrate,
    ScriptedCUA, make_kernel, status_of, type_kv,
)


# ── test-local fakes (this suite only) ─────────────────────────────────────
class SurfaceAwareExtractor(FakeExtractor):
    """Same "k=v" token parse as the suite fake, but evidence handles are
    stamped from the OBSERVED surface (``h-<sid>``) — the realistic chain
    observation → evidence handle → resolver → surface, instead of the
    conftest fake's single constant "vis" handle."""

    def extract(self, observation, variables):
        self.calls += 1
        sid = observation.surface.surface_id
        text = observation.visible_text or ""
        if "STRUCTURE-GONE" in text:
            raise StructureInvalidation("visible anchor disappeared")
        known = set(variables)
        out = []
        for tok in text.split():
            if "=" in tok:
                key, _, val = tok.partition("=")
                if key in known:
                    out.append(ObservedValue(
                        semantic_key=key, value=val,
                        evidence=(SurfaceEvidence(
                            surface=SurfaceHandle(handle_id=f"h-{sid}"),
                            visible_label=key, observed_value=val),)))
        return tuple(out)


class MappingResolver:
    """Composition-owned ``SurfaceBindingResolver`` fake: compiler-minted
    handle → session surface id."""

    def __init__(self, mapping: dict[str, str]):
        self.mapping = dict(mapping)
        self.asks: list[tuple[str, str]] = []

    def resolve_surface(self, handle_id: str, *, visible_label: str = ""):
        self.asks.append((handle_id, visible_label))
        return self.mapping.get(handle_id)


class RebindingResolver(MappingResolver):
    """A resolver whose known handles DIED (session re-opened): unknown
    handle ids rebind deterministically by VISIBLE LABEL over a fresh,
    read-only observation of each surface — 0 model calls, never a guess."""

    def __init__(self, substrate: FakeSubstrate,
                 mapping: dict[str, str] | None = None):
        super().__init__(mapping or {})
        self.substrate = substrate
        self.rebinds = 0

    def resolve_surface(self, handle_id: str, *, visible_label: str = ""):
        sid = super().resolve_surface(handle_id, visible_label=visible_label)
        if sid is not None:
            return sid
        if visible_label:
            for cand in list(self.substrate.world):
                text = self.substrate._visible_text(cand)
                for tok in text.split():
                    label, sep, _ = tok.partition("=")
                    if sep and label == visible_label:
                        self.rebinds += 1
                        return cand
        return None


# ── builders ────────────────────────────────────────────────────────────────
def ev(handle_id: str, label: str, value=None) -> SurfaceEvidence:
    return SurfaceEvidence(surface=SurfaceHandle(handle_id=handle_id),
                           visible_label=label, observed_value=value)


def var_on(key: str, observed, desired, handle_id: str) -> TaskVariable:
    """A task variable whose evidence grounds it on one surface."""
    return TaskVariable(semantic_key=key, label=key, observed=observed,
                        desired=desired,
                        evidence=(ev(handle_id, key, observed),))


def routed_action_node(node_id: str, desired: dict, evidence: list,
                       *, parent_id: str = "root",
                       depends_on: tuple[str, ...] = ()
                       ) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id, kind=NodeKind.ACTION, label=node_id,
        parent_id=parent_id, depends_on=depends_on,
        contract=ActionContract(
            contract_id=f"c-{node_id}",
            semantic_goal=f"realise {node_id}",
            desired_state=dict(desired),
            target_evidence=tuple(evidence)))


def ms_runtime(kernel, substrate, cua, resolver) -> AutonomyRuntime:
    """Multi-surface runtime: two surfaces discovered from the substrate,
    a composition-owned binding resolver injected (A-01)."""
    return AutonomyRuntime(
        kernel, substrate,
        cua_model=cua, serializer=FakeSerializer(),
        extractor=SurfaceAwareExtractor(), verifier=VisibleVerifier(),
        ledger=FakeLedger(), surface_resolver=resolver)


TWO_SURFACES = {"app": {}, "notes": {}}          # surfaces[0] is ALWAYS "app"


# ── ① action lands ONLY on the target surface ───────────────────────────────
def test_action_evidence_on_second_surface_writes_second_surface():
    """DSA counter-example ①: the contract's target evidence is grounded on
    surface ``notes``; the write must land there — ``app`` (surfaces[0])
    must stay untouched even though both surfaces show the same label."""
    k = make_kernel(
        [var_on("topic", "old", "new", "h-notes")],
        WorkflowGraph(nodes=(
            WorkflowNode("root", NodeKind.SEQUENCE, "task"),
            routed_action_node("a1", {"topic": "new"},
                               [ev("h-notes", "topic", "old")]),
            WorkflowNode("term", NodeKind.TERMINAL, "done",
                         depends_on=("a1",)),
        )))
    sub = FakeSubstrate({"app": {"topic": "old"}, "notes": {"topic": "old"}})
    cua = ScriptedCUA([type_kv("topic", "new"), DONE])
    rt = ms_runtime(k, sub, cua,
                    MappingResolver({"h-notes": "notes", "h-app": "app"}))
    rt.run(step_budget=1)

    assert status_of(k, "a1").value == "committed"
    assert sub.world["notes"]["topic"] == "new"
    assert sub.world["app"]["topic"] == "old"      # surfaces[0] untouched
    assert [sid for sid, kind in sub.act_log if kind == "type"] == ["notes"]


# ── ② verify reads the TARGET surface ───────────────────────────────────────
def test_verify_condition_reads_the_surface_its_key_is_bound_to():
    """DSA counter-example ②: ``done_flag`` reads false on surfaces[0]
    (app) and true on ``notes``. A surface-0 verify fails; routing must
    observe the surface the predicate key is bound to (notes) and pass."""
    k = make_kernel(
        [var_on("done_flag", "false", "true", "h-notes")],
        WorkflowGraph(nodes=(
            WorkflowNode("root", NodeKind.SEQUENCE, "task"),
            WorkflowNode("v1", NodeKind.VERIFY, "verify",
                         parent_id="root",
                         verification="done_flag == true"),
            WorkflowNode("term", NodeKind.TERMINAL, "done",
                         depends_on=("v1",)),
        )))
    sub = FakeSubstrate({"app": {"done_flag": "false"},
                         "notes": {"done_flag": "true"}})
    rt = ms_runtime(k, sub, ScriptedCUA([]),
                    MappingResolver({"h-notes": "notes", "h-app": "app"}))
    rt.run(step_budget=1)

    assert status_of(k, "v1").value == "committed"
    # the verify observation was served by the TARGET surface
    assert sub.observe_log[-1] == "notes"


# ── ③ compensation returns to the ORIGINAL surface ──────────────────────────
def test_compensation_entry_routes_back_to_its_surface():
    """DSA counter-example ③: ``y`` was written on ``notes``; rollback of
    that entry must restore it ON notes (the surface owning the binding),
    not on ``app``, and the pre-checkpoint ``x`` on ``app`` stays as
    committed."""
    k = make_kernel(
        [var_on("x", "x0", "A", "h-app"), var_on("y", "y0", "B", "h-notes")],
        WorkflowGraph(nodes=(
            WorkflowNode("root", NodeKind.SEQUENCE, "task"),
            routed_action_node("a1", {"x": "A"}, [ev("h-app", "x", "x0")]),
            WorkflowNode("cp", NodeKind.CHECKPOINT, "cp",
                         parent_id="root", depends_on=("a1",)),
            routed_action_node("a2", {"y": "B"},
                               [ev("h-notes", "y", "y0")],
                               depends_on=("cp",)),
            WorkflowNode("term", NodeKind.TERMINAL, "done",
                         parent_id="root", depends_on=("a2",)),
        )))
    sub = FakeSubstrate({"app": {"x": "x0"}, "notes": {"y": "y0"}})
    cua = ScriptedCUA([type_kv("x", "A"), DONE, type_kv("y", "B"), DONE])
    resolver = MappingResolver({"h-notes": "notes", "h-app": "app"})
    rt = ms_runtime(k, sub, cua, resolver)
    rt.run(step_budget=3)
    assert status_of(k, "a1").value == "committed"
    assert status_of(k, "a2").value == "committed"

    plan = k.request_compensation(CompensationPatch(
        patch_id="rb", target_checkpoint_id="ckpt:cp"))
    assert [e.node_id for e in plan.entries] == ["a2"]

    rt._cua = ScriptedCUA([type_kv("y", "y0"), DONE])
    fwd_acts = len(sub.act_log)               # forward gestures so far
    # NO surface_id override — per-entry A-01 routing must find `notes`
    disposition = rt.execute_compensation(plan)

    assert disposition == "complete"
    assert sub.world["notes"]["y"] == "y0"         # restored on ORIGIN
    assert sub.world["app"]["x"] == "A"            # pre-cp, untouched
    comp_acts = sub.act_log[fwd_acts:]             # ONLY the rollback pass
    assert comp_acts == [("notes", "type")]       # the restore gesture


# ── ④ one session, variables bound to DIFFERENT surfaces ───────────────────
def test_one_session_routes_each_binding_to_its_own_surface():
    """DSA counter-example ④: two variables, two bindings, one session —
    the forward pass must interleave correct-surface writes (x→app,
    y→notes), each verified against ITS surface's world."""
    k = make_kernel(
        [var_on("x", "x0", "A", "h-app"), var_on("y", "y0", "B", "h-notes")],
        WorkflowGraph(nodes=(
            WorkflowNode("root", NodeKind.FAN_OUT, "task"),
            routed_action_node("a1", {"x": "A"}, [ev("h-app", "x", "x0")]),
            routed_action_node("a2", {"y": "B"},
                               [ev("h-notes", "y", "y0")]),            WorkflowNode("b1", NodeKind.BARRIER, "join",
                         depends_on=("a1", "a2")),
            WorkflowNode("term", NodeKind.TERMINAL, "done",
                         depends_on=("b1",)),
        )))
    sub = FakeSubstrate({"app": {"x": "x0"}, "notes": {"y": "y0"}})
    cua = ScriptedCUA([type_kv("x", "A"), DONE, type_kv("y", "B"), DONE])
    rt = ms_runtime(k, sub, cua,
                    MappingResolver({"h-notes": "notes", "h-app": "app"}))
    rt.run(step_budget=2)

    assert status_of(k, "a1").value == "committed"
    assert status_of(k, "a2").value == "committed"
    # each write hit exactly its own surface
    assert sub.world["app"] == {"x": "A"}
    assert sub.world["notes"] == {"y": "B"}
    type_acts = [(sid, kind) for sid, kind in sub.act_log if kind == "type"]
    assert set(type_acts) == {("app", "type"), ("notes", "type")}


# ── ⑤ stale handle: rebind by visible label, never surface-0 fallback ──────
def test_stale_handle_rebinds_by_visible_label():
    """DSA counter-example ⑤a: the handle died (session re-opened) but the
    contract evidence still carries the VISIBLE label, which exists only
    on ``notes``. The resolver rebinds deterministically (read-only label
    match, 0 model calls) — the write lands on notes, never on app."""
    k = make_kernel(
        [var_on("topic", "old", "new", "stale-9")],
        WorkflowGraph(nodes=(
            WorkflowNode("root", NodeKind.SEQUENCE, "task"),
            routed_action_node("a1", {"topic": "new"},
                               [ev("stale-9", "topic", "old")]),
            WorkflowNode("term", NodeKind.TERMINAL, "done",
                         depends_on=("a1",)),
        )))
    sub = FakeSubstrate({"app": {}, "notes": {"topic": "old"}})
    cua = ScriptedCUA([type_kv("topic", "new"), DONE])
    resolver = RebindingResolver(sub)        # knows NO handle mapping
    rt = ms_runtime(k, sub, cua, resolver)
    rt.run(step_budget=1)

    assert status_of(k, "a1").value == "committed"
    assert resolver.rebinds >= 1            # rebound, not guessed
    assert sub.world["notes"]["topic"] == "new"
    assert "topic" not in sub.world["app"]  # surfaces[0] never written


def test_unresolvable_binding_fails_honestly_without_gui_write():
    """DSA counter-example ⑤b: a multi-surface session with an unresolvable
    binding (dead handle, label matches nothing) must land an honest fail +
    ``StructureInvalidated`` — a routing failure must NEVER become a
    wrong-surface GUI gesture."""
    k = make_kernel(
        [var_on("ghost", "?", "v", "stale-404")],
        WorkflowGraph(nodes=(
            WorkflowNode("root", NodeKind.SEQUENCE, "task"),
            routed_action_node("a1", {"ghost": "v"},
                               [ev("stale-404", "ghost_label", "?")]),
            WorkflowNode("term", NodeKind.TERMINAL, "done",
                         depends_on=("a1",)),
        )))
    sub = FakeSubstrate({"app": {"topic": "old"}, "notes": {"topic": "old"}})
    cua = ScriptedCUA([type_kv("ghost", "v"), DONE])
    resolver = RebindingResolver(sub)        # ghost_label matches nothing
    rt = ms_runtime(k, sub, cua, resolver)
    rt.run(step_budget=1)

    assert status_of(k, "a1").value == "failed"
    assert sub.act_log == []                 # NO gesture anywhere — no guess
    assert any(e.kind is RuntimeEventKind.STRUCTURE_INVALIDATED
               for e in rt.runtime_events())


# ── negative control: the resolver failure mode is routing, not crashing ────
def test_broken_resolver_is_routing_failure_not_crash():
    """A resolver that RAISES is a harness bug — the runtime treats it as a
    routing failure (honest fail), never a crash mid-loop."""
    class ExplodingResolver:
        def resolve_surface(self, handle_id, *, visible_label=""):
            raise RuntimeError("resolver backend gone")

    k = make_kernel(
        [var_on("topic", "old", "new", "h-notes")],
        WorkflowGraph(nodes=(
            WorkflowNode("root", NodeKind.SEQUENCE, "task"),
            routed_action_node("a1", {"topic": "new"},
                               [ev("h-notes", "topic", "old")]),
            WorkflowNode("term", NodeKind.TERMINAL, "done",
                         depends_on=("a1",)),
        )))
    sub = FakeSubstrate(dict(TWO_SURFACES, **{"notes": {"topic": "old"}}))
    rt = ms_runtime(k, sub, ScriptedCUA([type_kv("topic", "new"), DONE]),
                    ExplodingResolver())
    rt.run(step_budget=1)
    assert status_of(k, "a1").value == "failed"
    assert sub.act_log == []
