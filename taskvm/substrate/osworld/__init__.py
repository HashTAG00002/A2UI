"""taskvm.substrate.osworld — the OSWorld substrate (minimal).

``session``: unified-port SubstrateSession over an OSWorld remote-agent
transport (connect / list desktop surface / screenshot / click / type /
key / scroll). Honest ``SubstrateUnavailable`` when no VM is attached;
contract-tested against a fake transport in ``tests/substrate``.
"""
from taskvm.substrate.osworld.provider import (
    OSWorldProvider, OSWorldEvaluationProvider,
)
from taskvm.substrate.osworld.session import (
    HttpOSWorldRuntime, OSWorldRuntime, OSWorldSubstrateSession,
)

__all__ = [
    "OSWorldProvider", "OSWorldEvaluationProvider",
    "HttpOSWorldRuntime", "OSWorldRuntime", "OSWorldSubstrateSession",
]
