"""FF.4 §5.4 — WorkflowExecutor: execute a WorkflowPlan (Sequential /
Parallel / Loop).

Walks ``WorkflowPlan.nodes``; each node dispatches its subgoals' patch_ops via
the existing ``action_dispatcher.dispatch`` (which calls each adapter's
``mutate`` — the app's own write surface: gui_agent = real browser gestures,
api = requests.post to the app's Flask API. NEVER ``set_state`` — §12.16/E7).

Three node types (FF.4 §5.1):
  - SEQUENTIAL: subgoals one after another (the existing linear path).
  - PARALLEL: N subgoals issued concurrently via ThreadPoolExecutor, barrier
    at the end (all must complete before the next node). For api executor this
    is truly concurrent (HTTP to different apps); for gui_agent the singleton
    browser serializes (one Playwright page — honest limitation, §13.1).
  - LOOP: one template subgoal instantiated ``loop_count`` times, each with
    ``loop_values[i]`` substituted as the patch_op entity_id. E11: EACH
    iteration is independently verified (canonical re-read confirms the op's
    field landed — not just the final state).

``on_node_complete(node_idx, node_result)`` is called after each node (the
server's SSE workflow_progress push [FF.5] hooks here to update the frontend).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from taskvm.execution.action_dispatcher import dispatch
from taskvm.execution.patch_compiler import PatchOp
from taskvm.governance.subgoal import (SubgoalInstruction, WorkflowNode,
                                       WorkflowNodeType, WorkflowPlan)

logger = logging.getLogger(__name__)


@dataclass
class SubgoalResult:
    subgoal: SubgoalInstruction
    dispatch: dict
    n_ops: int
    n_applied: int
    verified: bool          # FF.4 E11: canonical re-read confirms the op landed
    pass_: bool
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "n_ops": self.n_ops, "n_applied": self.n_applied,
            "verified": self.verified, "pass_": self.pass_,
            "error": self.error,
            "dispatch": self.dispatch,
            "subgoal": self.subgoal.to_dict(),
        }


@dataclass
class NodeResult:
    node_type: str
    subgoal_results: list[SubgoalResult] = field(default_factory=list)
    pass_: bool = False
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "node_type": self.node_type,
            "pass_": self.pass_,
            "n_subgoals": len(self.subgoal_results),
            "subgoal_results": [r.to_dict() for r in self.subgoal_results],
            "meta": self.meta,
        }


@dataclass
class WorkflowResult:
    nodes: list[NodeResult] = field(default_factory=list)
    overall_pass: bool = False
    # GG.5: pause-at-node-boundary. When pause_check returns True between nodes,
    # execute stops after the current node completes (NEVER mid-gesture — the
    # check is at the top of the for-node loop, so an in-flight GUI gesture
    # finishes). paused=True + stopped_at_node=N records where it stopped.
    paused: bool = False
    stopped_at_node: int | None = None

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "overall_pass": self.overall_pass,
            "n_nodes": len(self.nodes),
            "paused": self.paused,
            "stopped_at_node": self.stopped_at_node,
        }


def _instantiate_loop_subgoal(template: SubgoalInstruction, i: int,
                              val: Any) -> SubgoalInstruction:
    """FF.4 §5.4: build iteration ``i`` of a loop by substituting the template
    subgoal's patch_op entity_id with the loop value ``val`` (e.g. T1→T2→T3).
    The op's app/field/operator/value are unchanged (the loop varies the
    ENTITY, not the value — §5.6 batch_task_assign)."""
    new_ops = [
        PatchOp(app=op.app, entity_id=str(val), field=op.field,
                operator=op.operator, value=op.value)
        for op in template.patch_ops
    ]
    return SubgoalInstruction(
        natural_language=(f"[loop iter {i+1}] {template.natural_language} "
                          f"(entity={val})"),
        patch_ops=new_ops,
        verification_criterion=template.verification_criterion,
        source_event_type="loop_field",
        target_checkpoint_id=template.target_checkpoint_id,
        meta={**template.meta, "loop_iteration": i, "loop_value": val},
    )


class WorkflowExecutor:
    """Execute a WorkflowPlan node-by-node. Each node dispatches its subgoals'
    patch_ops via ``action_dispatcher.dispatch`` → ``adapter.mutate`` (the
    app's own write surface — never ``set_state``; §12.16/E7)."""

    def __init__(self, *, verify_each_op: bool = True) -> None:
        """``verify_each_op`` (E11): re-read canonical after each subgoal +
        confirm the op's (app,entity,field) matches the intended value. True by
        default — a loop must verify EACH iteration, not just the final state.
        Set False to skip the per-op re-read (e.g. a fast mock run that only
        needs the dispatch report)."""
        self.verify_each_op = verify_each_op

    def execute(self, plan: WorkflowPlan, adapters: dict, sid: str, *,
                rollback_log=None,
                on_node_complete: Callable[[int, NodeResult], None] | None = None,
                on_subgoal_complete: Callable[[int, SubgoalResult, WorkflowNode], None] | None = None,
                pause_check: Callable[[], bool] | None = None,
                ) -> WorkflowResult:
        """Walk ``plan.nodes``; dispatch each node's subgoals. ``on_node_complete``
        fires after each node (the SSE workflow_progress barrier-update hook);
        ``on_subgoal_complete`` fires after each subgoal (per-lane real-time
        progress — FF.5 §6.3). Both are best-effort (errors logged, never
        abort the execution).

        GG.5 ``pause_check``: if supplied, called at the TOP of each node
        iteration (between nodes — never mid-gesture). If it returns True, the
        loop breaks after recording ``stopped_at_node=i`` + ``paused=True``.
        This enforces "pause stops after the current node, doesn't kill the
        in-flight GUI gesture" (the honest boundary — gui_act_async has no
        cancel token, so pause must be at the node boundary)."""
        results: list[NodeResult] = []
        paused = False
        stopped_at = None
        for i, node in enumerate(plan.nodes):
            if pause_check is not None and pause_check():
                paused = True
                stopped_at = i
                logger.info("[workflow] paused at node %d (before executing it)", i)
                break
            if node.node_type == WorkflowNodeType.SEQUENTIAL:
                r = self._exec_sequential(node, adapters, sid, rollback_log,
                                          on_subgoal_complete)
            elif node.node_type == WorkflowNodeType.PARALLEL:
                r = self._exec_parallel(node, adapters, sid, rollback_log,
                                        on_subgoal_complete)
            elif node.node_type == WorkflowNodeType.LOOP:
                r = self._exec_loop(node, adapters, sid, rollback_log,
                                    on_subgoal_complete)
            else:   # pragma: no cover — unknown node type
                r = NodeResult(node_type=str(node.node_type), pass_=False,
                               meta={"error": f"unknown node type {node.node_type}"})
            results.append(r)
            if on_node_complete is not None:
                try: on_node_complete(i, r)
                except Exception as e: logger.warning("[workflow] on_node_complete: %s", e)
        overall = bool(results) and all(r.pass_ for r in results) and not paused
        return WorkflowResult(nodes=results, overall_pass=overall,
                               paused=paused, stopped_at_node=stopped_at)

    # ── per-subgoal dispatch + E11 verify ─────────────────────────────────
    def _exec_one_subgoal(self, sg: SubgoalInstruction, adapters: dict,
                          sid: str, rollback_log,
                          on_done: Callable[[SubgoalResult], None] | None = None,
                          ) -> SubgoalResult:
        n_ops = len(sg.patch_ops)
        try:
            rep = dispatch(sg.patch_ops, adapters, sid, broken=None,
                           rollback_log=rollback_log)
            verified = True
            if self.verify_each_op:
                verified = self._verify_ops_landed(sg.patch_ops, adapters, sid)
            r = SubgoalResult(
                subgoal=sg, dispatch=rep.to_dict(), n_ops=n_ops,
                n_applied=rep.n_applied, verified=verified,
                pass_=(rep.n_applied == n_ops and verified))
        except Exception as e:
            logger.warning("[workflow] subgoal failed: %s", e)
            r = SubgoalResult(
                subgoal=sg, dispatch={}, n_ops=n_ops, n_applied=0,
                verified=False, pass_=False, error=f"{type(e).__name__}: {e}")
        if on_done is not None:
            try: on_done(r)
            except Exception as e: logger.warning("[workflow] on_subgoal_complete: %s", e)
        return r

    def _verify_ops_landed(self, ops: list[PatchOp], adapters: dict,
                           sid: str) -> bool:
        """E11: re-read canonical + confirm each op's (app, entity_id, field)
        == the intended value. This is the per-iteration verification that
        catches a silently-failed intermediate loop iteration (not just the
        final state)."""
        for op in ops:
            ad = adapters.get(op.app)
            if ad is None:
                return False
            try:
                canon = ad.read_canonical(sid)
            except Exception:
                return False
            ent = (canon.get("entities") or {}).get(op.entity_id) or {}
            if str(ent.get(op.field)).strip().lower() != str(op.value).strip().lower():
                return False
        return True

    # ── SEQUENTIAL ────────────────────────────────────────────────────────
    def _exec_sequential(self, node: WorkflowNode, adapters: dict, sid: str,
                          rollback_log,
                          on_subgoal_complete=None) -> NodeResult:
        results = []
        for sg in node.subgoals:
            r = self._exec_one_subgoal(sg, adapters, sid, rollback_log,
                                        on_done=on_subgoal_complete)
            results.append(r)
        return NodeResult(node_type="sequential", subgoal_results=results,
                          pass_=all(r.pass_ for r in results))

    # ── PARALLEL (E7: each lane uses adapter.mutate, NOT set_state) ──────
    def _exec_parallel(self, node: WorkflowNode, adapters: dict, sid: str,
                        rollback_log, on_subgoal_complete=None) -> NodeResult:
        use_gui = any(getattr(a, "use_gui_executor", False) for a in adapters.values())
        results: list[SubgoalResult] = []
        if not node.subgoals:
            return NodeResult(node_type="parallel", pass_=True)
        if use_gui:
            # gui_executor is a singleton (one Playwright page per backend);
            # concurrent calls on one page race. Serialize honestly — the
            # parallel STRUCTURE + barrier still hold (all lanes complete
            # before the next node), but gui_agent lanes run in sequence.
            # §13.1: true max(N) latency needs N browsers (future work).
            for sg in node.subgoals:
                results.append(self._exec_one_subgoal(sg, adapters, sid, rollback_log,
                                                        on_done=on_subgoal_complete))
        else:
            # api executor: HTTP requests to different apps are safely
            # concurrent (same-app different-entity writes are independent).
            with ThreadPoolExecutor(max_workers=len(node.subgoals)) as pool:
                futs = {pool.submit(self._exec_one_subgoal, sg, adapters, sid,
                                    rollback_log, on_subgoal_complete): sg
                        for sg in node.subgoals}
                for f in as_completed(futs):
                    results.append(f.result())
        # preserve the plan's lane order in the result (as_completed scrambles)
        order = {id(sg): i for i, sg in enumerate(node.subgoals)}
        results.sort(key=lambda r: order.get(id(r.subgoal), 0))
        return NodeResult(node_type="parallel", subgoal_results=results,
                          pass_=all(r.pass_ for r in results),
                          meta={"barrier": "verifier", "gui_serialized": use_gui})

    # ── LOOP (E11: each iteration independently verified) ────────────────
    def _exec_loop(self, node: WorkflowNode, adapters: dict, sid: str,
                    rollback_log, on_subgoal_complete=None) -> NodeResult:
        if not node.subgoals:
            return NodeResult(node_type="loop", pass_=False,
                               meta={"error": "loop node has no template subgoal"})
        template = node.subgoals[0]
        results: list[SubgoalResult] = []
        for i, val in enumerate(node.loop_values[:node.loop_count]):
            sg_i = _instantiate_loop_subgoal(template, i, val)
            r = self._exec_one_subgoal(sg_i, adapters, sid, rollback_log,
                                        on_done=on_subgoal_complete)
            results.append(r)
            # E11: each iteration independently verified (the SubgoalResult
            # already carries verified= from _verify_ops_landed). If an
            # iteration fails, we CONTINUE (so the verifier sees exactly
            # which iterations landed + which didn't — honest partial-failure
            # reporting, not an all-or-nothing abort).
            if not r.pass_:
                logger.warning("[workflow] loop iter %d failed (val=%s): %s",
                               i, val, r.error or "verify failed")
        return NodeResult(node_type="loop", subgoal_results=results,
                          pass_=all(r.pass_ for r in results) if results else False,
                          meta={"loop_count": len(results),
                                "n_passed": sum(1 for r in results if r.pass_)})
