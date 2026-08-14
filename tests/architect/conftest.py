"""tests/architect/ — OWNER: Agent C (Task Architect).

Contract tests for the architect's producer obligations (layered
ownership protocol §1): semantic variables unique, ActionContracts
reference declared variables, workflow uses only the three primitives
with legal shapes, projection bindings point at declared variables,
contract desired == architecture desired (split-brain guard).

Agent A (kernel) does NOT implement these: the STATIC rules themselves
are already locked by domain constructors and pinned in
tests/domain/test_architecture.py + tests/domain/test_static_shapes.py.
This directory holds the ARCHITECT-side producer contract tests (e.g.
"the architect's output always passes TaskArchitecture validation") once
Agent C exists.
"""
