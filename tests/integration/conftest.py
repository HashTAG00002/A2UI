"""tests/integration/ — cross-layer protocol integration (C → Kernel → E).

Minimal skeleton for proving the layers compose: a producer-constructed
TaskArchitecture installs into the kernel; typed VerificationResult /
CompensationResult produced by the (future) runtime/verifier land on the
kernel timeline with the documented dispositions. Filled when Agents
C/E exist; until then the domain contract tests (tests/domain/) plus the
kernel temporal tests (tests/kernel/) pin both ends of the protocol.
"""
