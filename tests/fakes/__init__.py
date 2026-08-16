"""tests/fakes/ — TEST DOUBLES ONLY (master handoff §3.4 / architect
contract §5: "测试替身只能存在于 tests/fakes/，不得通过生产 CLI flag 启用").

Residents (migrated out of production governance in the Agent-C role
collapse, 2026-08-14):

- scripted_driver / ui_sim_driver / user_behavior_driver / human_driver —
  the legacy L4 event-source stack that fed the scripted-event planner.
- governance_interpreter — the legacy rule-based workflow classifier +
  subgoal interpreter (killed as the PRODUCTION planner; survives here as
  test fixture machinery for the legacy evaluation entries until Agent F
  replaces them).
- subgoal_generator — the legacy LLM NL candidate generator (killed as the
  production CUA-instruction path; the deterministic replacement is
  taskvm.architect.serializer).
- fake_model / fake_architect — deterministic FakeModelPort / scripted
  architect outputs for contract tests.

NOTHING under taskvm/ may import this package; production code must stay
free of mock=True / mock=False research forks.
"""
