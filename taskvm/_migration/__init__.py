"""taskvm._migration — SHORT-LIVED compatibility adapters (handoff 02 §迁移策略 2).

One-directional converters FROM legacy types (task_state / execution /
governance / vm_state) TO the new taskvm.domain types, so the old call
sites can be migrated incrementally while later waves physically delete
the old modules.

Hard rules:
  - taskvm.domain / taskvm.kernel must NEVER import this package
    (enforced by tests/architecture).
  - This package may import legacy modules; nothing new may be built on
    top of it.
  - Deletion owner: Agent G (08_INTEGRATION_RELEASE_CLEANUP_AGENT),
    at the Wave-3 integration point, once Agents B-E have moved their
    call sites onto the kernel contracts.
"""
