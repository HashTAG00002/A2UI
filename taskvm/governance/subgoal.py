"""``SubgoalInstruction`` — the L3 output of ``GovernanceInterpreter``.

A subgoal is the unit of work the CUA executes. It carries:
  - ``natural_language``: the instruction string fed to ``gui_act_async`` (via
    the bridge's ``instruction_override`` field) — this is what the CUA "sees".
  - ``patch_ops``: the resolved executable ops (``PatchOp`` from
    ``execution.patch_compiler``) — what the dispatch path applies.
  - ``verification_criterion``: a STRUCTURED criterion (expected_diff-shaped
    dict, or a checkpoint criterion) the governance killtest's verifier checks
    against live canonical state. NOT a string — the existing verifier
    (``check_round_trip``) takes a ``CanonicalTaskGraph`` fixture + snapshots,
    not a string; a string criterion would be incompatible (recon area 7).

Design (E17-B):
  - ``edit_field`` events produce one subgoal per PatchOp (or one batched
    subgoal) with a template-derived NL string + the op's expected post-value
    as the verification criterion.
  - ``rollback_to`` events produce subgoals whose execution is
    ``RollbackLog.undo_saga`` (the proven mechanism — re-dispatches the inverse
    op through the adapter; for wechat this honestly 409s). The NL describes
    what will be undone; ``llm_generated=True`` + ``manual_review_needed=True``
    on first run (handoff §6 boundary 3).

No-leak boundary: ``patch_ops`` carry only resolved (app, entity_id, operator,
value) — the value comes from the user's edit intent, NEVER from GT fixtures.
``verification_criterion`` IS verifier-side GT (it is consumed by the verifier,
NEVER fed to the CUA prompt — same boundary as ``expected_diff``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from taskvm.execution.patch_compiler import PatchOp


class WorkflowNodeType(Enum):
    """FF.4 §5.2: the three structured-program shapes a workflow node can take.
    SEQUENTIAL = the existing linear one-subgoal-after-another execution;
    PARALLEL = N subgoals issued concurrently, barrier at the end; LOOP = one
    template subgoal repeated ``loop_count`` times with ``loop_values[i]``
    substituted per iteration (each iteration independently verified — E11)."""
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    LOOP = "loop"


@dataclass
class WorkflowNode:
    """FF.4 §5.2: one execution node. ``subgoals`` is a list of
    SubgoalInstruction — for PARALLEL it's the N concurrent subgoals (one per
    app lane); for LOOP it's a 1-element list (the template, instantiated
    ``loop_count`` times with ``loop_values[i]``); for SEQUENTIAL it's the
    ordered subgoals."""
    node_type: WorkflowNodeType = WorkflowNodeType.SEQUENTIAL
    subgoals: list["SubgoalInstruction"] = field(default_factory=list)
    loop_count: int = 1
    loop_values: list[Any] = field(default_factory=list)
    display_name: str = ""
    barrier_label: str = ""    # PARALLEL convergence point display name

    def to_dict(self) -> dict:
        return {
            "node_type": self.node_type.value,
            "subgoals": [s.to_dict() for s in self.subgoals],
            "loop_count": self.loop_count,
            "loop_values": list(self.loop_values),
            "display_name": self.display_name,
            "barrier_label": self.barrier_label,
        }


@dataclass
class WorkflowPlan:
    """FF.4 §5.2: the full workflow execution plan (replaces the flat
    list[SubgoalInstruction] for tasks with parallel/loop structure). The
    WorkflowExecutor walks ``nodes`` in order; each node is
    SEQUENTIAL/PARALLEL/LOOP."""
    task_id: str
    nodes: list[WorkflowNode] = field(default_factory=list)
    workflow_type: str = "sequential"   # "sequential" / "parallel" / "loop" / "mixed"
    generated_by: str = "deterministic"   # "deterministic" (rules) / "llm_planner"

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "workflow_type": self.workflow_type,
            "generated_by": self.generated_by,
        }


@dataclass
class SubgoalInstruction:
    """One unit of CUA work, produced by ``GovernanceInterpreter.interpret``.

    Fields:
      natural_language: the CUA instruction (fed to gui_act_async via the
        bridge's ``instruction_override``). Empty for pure undo_saga subgoals
        whose execution does not go through the CUA NL path.
      patch_ops: the resolved ops (``PatchOp`` list). Empty for rollback_to
        subgoals (undo_saga handles execution via the adapter, not patch_ops).
      verification_criterion: STRUCTURED criterion (expected_diff-shaped dict
        OR a checkpoint criterion dict). Consumed by the governance killtest's
        verifier, NEVER by the CUA prompt.
      source_event_type: which UserBehaviorEvent produced this ("edit_field" |
        "rollback_to" | "set_milestone").
      target_checkpoint_id: the checkpoint this subgoal advances to / rolls
        back to (None for a plain edit_field with no checkpoint semantics).
      saga_id: for rollback_to, the saga being undone.
      llm_generated: True if natural_language was produced by the dynamic LLM
        (complete_json) inference path (handoff §1.3 strategy B).
      manual_review_needed: True if this subgoal's NL/ops should be
        human-reviewed before trusting (first-run rollback_to LLM output —
        handoff §6 boundary 3).
      meta: free-form dict for trace/debug (model raw response, saga result,
        etc.).
    """
    natural_language: str
    patch_ops: list[PatchOp] = field(default_factory=list)
    verification_criterion: dict = field(default_factory=dict)
    source_event_type: str = "edit_field"
    target_checkpoint_id: str | None = None
    saga_id: str | None = None
    llm_generated: bool = False
    manual_review_needed: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "natural_language": self.natural_language,
            "patch_ops": [op.to_dict() for op in self.patch_ops],
            "verification_criterion": self.verification_criterion,
            "source_event_type": self.source_event_type,
            "target_checkpoint_id": self.target_checkpoint_id,
            "saga_id": self.saga_id,
            "llm_generated": self.llm_generated,
            "manual_review_needed": self.manual_review_needed,
            "meta": self.meta,
        }
