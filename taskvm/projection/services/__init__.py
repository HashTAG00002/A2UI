"""taskvm.projection.services — injected ports for governance + autonomy.

These are thin adapters over the kernel's public facade (and optionally
Agent E's runtime). The composition root may substitute its own
implementations (e.g. wiring governance through C's GovernanceService);
the structural ports in ``store.py`` define the contract.
"""
