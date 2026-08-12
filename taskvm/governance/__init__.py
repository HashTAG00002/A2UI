"""taskvm.governance — L3 (GovernanceInterpreter) + L4 (UserBehaviorDriver) layers.

E17-B (2026-08-12). The protocol-stack layers above L1 (vm_state) + L2
(workspace_ui):
  - L4 UserBehaviorDriver: abstract source of user-behavior events.
    Implementations: ScriptedUserDriver (killtests), HumanWebSocketDriver
    (real human via the workspace_ui WebSocket endpoint).
  - L3 GovernanceInterpreter: translates UserBehaviorEvent + VMStateSnapshot
    into SubgoalInstruction(s) (CUA NL + patch_ops + structured verification).

These two layers are the "拦腰截断" fix (handoff §0-A): the pipeline no longer
starts at the intent layer ({operator, value}); it starts at the user-behavior
layer and is interpreted into subgoals. Scripted and human drivers are
interchangeable — L3 and below cannot tell them apart.
"""
from taskvm.governance.user_behavior_driver import (
    UserBehaviorDriver, UserBehaviorEvent, EVENT_TYPES,
)
from taskvm.governance.vm_state import VMStateSnapshot
from taskvm.governance.subgoal import (SubgoalInstruction, WorkflowNode,
                                        WorkflowNodeType, WorkflowPlan)
from taskvm.governance.checkpoint_graph import CheckpointGraph, CheckpointDirection
from taskvm.governance.scripted_driver import (
    ScriptedUserDriver, make_scripted_driver, get_task_event_sequence,
)
# FF.2: UISimDriver — drives the governance loop THROUGH the rendered GenUI
# surface (GET /<sid> → parse <form> → POST /<sid>/edit → read changed_vars),
# vs. ScriptedUserDriver's direct intent emission.
from taskvm.governance.ui_sim_driver import UISimDriver
from taskvm.governance.governance_interpreter import GovernanceInterpreter

__all__ = [
    "UserBehaviorDriver", "UserBehaviorEvent", "EVENT_TYPES",
    "VMStateSnapshot",
    "SubgoalInstruction", "WorkflowNode", "WorkflowNodeType", "WorkflowPlan",
    "CheckpointGraph", "CheckpointDirection",
    "ScriptedUserDriver", "make_scripted_driver", "get_task_event_sequence",
    "UISimDriver",
    "GovernanceInterpreter",
]
