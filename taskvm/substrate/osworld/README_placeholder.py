"""taskvm.substrate.osworld — OSWorld adapter (future, E17-C placeholder).

The OSWorld substrate adapter is NOT implemented (handoff §7 遗留问题: OSWorld
adapter reserved but not implemented). This placeholder documents the
integration contract so a future implementation knows where to plug in.

Integration contract (when implemented):
  - Subclass ``taskvm.substrate.base.StateAdapter`` (same contract as the
    builtin + MobileGym adapters: reset / seed / read_canonical / mutate).
  - Implement ``read_canonical(sid) -> {'entities': {eid: {field: val}}}`` over
    OSWorld's desktop state (windows, files, calendar, etc.).
  - Implement ``mutate(sid, entity_id, operator, value)`` via OSWorld's
    keyboard/mouse action API (NOT a set_state backdoor — same non-invasive
    write boundary as the MobileGym bridge, memory: taskvm-non-invasive-
    write-rollback-boundary).
  - Register in ``_ADAPTER_CLASSES`` + ``DEFAULT_PORTS`` in substrate/base.py.
  - The substrate-independence property (VM5) means the SAME CanonicalTaskGraph
    + GovernanceInterpreter flow should drive OSWorld with only the StateAdapter
    swapped — that is the "JVM moment" the handoff §2.2 MG-3 targets.

This file is intentionally NOT a real adapter — do not instantiate.
"""

class OSWorldAdapterPlaceholder:
    """Stub. Raises on construction — the OSWorld adapter is not implemented."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "OSWorld adapter is not implemented (E17-C placeholder only). "
            "See this module's docstring for the integration contract.")
