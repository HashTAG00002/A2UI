"""L4 — ``UserBehaviorDriver`` + ``UserBehaviorEvent`` (E17-B).

The user-behavior layer of the protocol stack (handoff §1.1). A
``UserBehaviorDriver`` is an abstract source of user-behavior events: a
programmatic script (``ScriptedUserDriver``, for killtests/evaluation) and a
real human (``HumanWebSocketDriver``, via the workspace_ui WebSocket endpoint)
are two interchangeable implementations. L3 (``GovernanceInterpreter``) and
below are completely transparent to which driver produced an event — that is
the "无缝切换" property (handoff §1.1).

Modeling note (handoff §1.2): a ``UserBehaviorEvent`` models the USER'S
high-level action on the GenUI surface ("edit field X to Y", "rollback to
checkpoint C1"), NOT the CUA instruction. The CUA instruction is inferred by
``GovernanceInterpreter`` from the event + current VM state.

Event types (handoff §1.2 enum, resolved against the EXISTING workspace_ui
HTTP verbs per recon area 9):
  - ``edit_field``: user edited a GenUI field. Payload: {var_id, new_value,
    old_value?}. Maps 1:1 to the existing POST /<sid>/edit contract.
  - ``rollback_to``: user dragged the progress bar back to a checkpoint.
    Payload: {target_checkpoint_id}. NEW semantics — the existing per-app
    /undo/<app> route is the substrate, but checkpoint-addressed rollback is
    new (recon area 9: the progress bar is currently read-only client-side).
  - ``set_milestone``: user set a new target depth. Payload:
    {target_checkpoint_id}. Advances toward a checkpoint (forward).
  - ``checkpoint``: user pressed the checkpoint button (snapshot current
    state). Payload: {}. Maps to POST /<sid>/checkpoint.
  - ``undo``: user pressed an app's undo button. Payload: {app}. Maps to
    POST /<sid>/undo/<app> (the existing per-app undo route).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # avoid runtime import cycle (vm_state imports nothing heavy)
    from taskvm.governance.vm_state import VMStateSnapshot


# The canonical event-type enum. ``undo`` is the existing per-app verb;
# ``rollback_to`` / ``set_milestone`` are the new checkpoint-addressed verbs.
# FF.4 §5.3: ``loop_field`` is the new event for LOOP workflows (payload:
# {var_id, values: [v1,v2,...], loop_label}) — the interpreter builds a
# WorkflowNode(LOOP) with ``values`` as the per-iteration substitution set.
EVENT_TYPES = (
    "edit_field",      # user edited a GenUI field
    "rollback_to",     # user dragged progress bar back to a checkpoint
    "set_milestone",   # user set a new target depth (advance toward checkpoint)
    "checkpoint",      # user pressed the checkpoint button (snapshot)
    "undo",            # user pressed an app's undo button (per-app, existing)
    "loop_field",      # FF.4: user invoked a loop (batch op on N entities)
)


@dataclass
class UserBehaviorEvent:
    """One user-behavior event — what the user did on the GenUI surface.

    This is high-level semantic intent, NOT a CUA instruction. The
    GovernanceInterpreter infers the CUA subgoal(s) from this + VM state.
    """
    event_type: str                       # one of EVENT_TYPES
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.event_type not in EVENT_TYPES:
            raise ValueError(
                f"unknown event_type {self.event_type!r}; must be one of "
                f"{EVENT_TYPES}")


class UserBehaviorDriver(ABC):
    """Abstract source of user-behavior events (L4).

    Two implementations:
      - ``ScriptedUserDriver`` (taskvm.governance.scripted_driver): programmatic,
        for killtests/evaluation. Generates events from a CanonicalTaskGraph.
      - ``HumanWebSocketDriver`` (taskvm.governance.human_driver): real human,
        via the workspace_ui WebSocket endpoint.

    Both implement the same interface — L3 and below cannot tell them apart.
    """

    @abstractmethod
    def next_event(self) -> UserBehaviorEvent | None:
        """Return the next user-behavior event, or None when the task is done."""
        ...

    @abstractmethod
    def on_state_update(self, vm_state: "VMStateSnapshot") -> None:
        """Callback invoked when VM state changes (for the human driver, this
        pushes the new state to the browser; for the scripted driver, it is a
        no-op or a log)."""
        ...
