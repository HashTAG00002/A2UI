"""validator — the two-layer gate every model-generated component tree
must pass (workplan §4 `validator.py`).

Layer 1 — A2UI protocol/catalog conformance: the official SDK validator
(``schema.get_validator()``) checks the raw message shapes against the
vendored v0.9 mirror (component types, required props, flat
discriminator, envelope structure).

Layer 2 — TaskVM semantic policy: ``SurfacePolicy`` checks governance
boundaries (editable-only input bindings, whitelisted paths, action
allowlist, limits, no URLs/scripts, no governance-namespace squatting).

A component list is accepted ONLY when both layers are clean. Errors
are accumulated, never repaired silently — the decoder gets one bounded
repair retry, then an honest deterministic fallback (workplan §7-P3).
"""
from __future__ import annotations

from typing import Any

from taskvm.genui import policy as _policy
from taskvm.genui import schema as _schema
from taskvm.genui.context import TaskSurfaceContext
from taskvm.genui.data_model import TaskDataModelProjector
from taskvm.genui.protocol import update_components_message


class ComponentValidationError(ValueError):
    """Raised by validate_components(strict=True) — carries every layer's
    errors so callers can build a repair prompt or a 4xx response."""

    def __init__(self, protocol_errors: list[str],
                 policy_errors: list[str]) -> None:
        self.protocol_errors = protocol_errors
        self.policy_errors = policy_errors
        parts = []
        if protocol_errors:
            parts.append("protocol: " + "; ".join(protocol_errors))
        if policy_errors:
            parts.append("policy: " + "; ".join(policy_errors))
        super().__init__(" | ".join(parts) or "invalid components")


def validate_components(components: list[dict[str, Any]],
                        context: TaskSurfaceContext,
                        data_model: dict[str, Any] | None = None,
                        *, surface_id: str = "taskvm-task-validation",
                        strict: bool = False) -> list[str]:
    """Two-layer validation of one ``updateComponents.components`` list.

    ``context`` supplies the semantic ground truth; ``data_model``
    defaults to the deterministic projection of that context (the
    whitelist the renderer will actually resolve against).

    Returns all errors (empty list == valid). With ``strict=True`` a
    non-empty result raises ComponentValidationError instead.
    """
    protocol_errors = _schema.validate_protocol_messages(
        [update_components_message(surface_id, components)])

    model = data_model if data_model is not None else \
        TaskDataModelProjector().project(context)
    policy_errors = _policy.SurfacePolicy(context, model).check_components(
        components)

    errors = protocol_errors + policy_errors
    if errors and strict:
        raise ComponentValidationError(protocol_errors, policy_errors)
    return errors
