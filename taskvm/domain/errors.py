"""Domain/kernel error types.

These are the ONLY exceptions the kernel raises for contract violations.
Upper layers (architect / runtime / projection) catch these instead of
parsing error strings. Pure stdlib — no framework imports allowed here.
"""
from __future__ import annotations


class TaskVMError(Exception):
    """Base class for all domain/kernel errors."""


class ValidationError(TaskVMError):
    """A domain object violated its own invariants (e.g. workflow cycle)."""


class RevisionConflictError(TaskVMError):
    """A store was handed a non-monotonic revision (invariant 1)."""


class StaleEpochError(TaskVMError):
    """An action result arrived from a superseded execution epoch
    (invariant 4). The kernel normally converts this into an
    ActionDiscarded event; the error exists for direct store misuse."""


class CommittedNodeViolationError(TaskVMError):
    """A patch attempted to silently rewrite or drop a committed workflow
    node (invariant 3). Committed history can only be kept as-is or
    explicitly compensated."""


class UnknownCheckpointError(TaskVMError):
    """A compensation/rollback request referenced a checkpoint that was
    never committed in this session (invariant 5)."""


class CompensationMismatchError(TaskVMError):
    """A CompensationPatch claimed 'before' values that do not match what
    was actually observed and recorded at the target checkpoint boundary
    (invariant 6). The kernel only trusts its own observation history —
    never an external oracle."""


class PatchSemanticsError(TaskVMError):
    """A patch was applied through the wrong entry point or carried
    semantics outside its class (e.g. a LocalPatch trying to change the
    terminal intent or the workflow topology)."""
