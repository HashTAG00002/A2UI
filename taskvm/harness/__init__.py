"""taskvm.harness — evaluation-side capture + replay helpers.

Agent B (substrate isolation): the legacy ``state_adapter`` (API write
executor + read_canonical), ``browser_controller`` and ``mobilegym_bridge``
are DELETED. What remains here is substrate-neutral tooling for benchmark scripts:

  * ``observations``  — StepObservation / TraceFixture value objects
  * ``replay_engine`` — DOM capture + parse + obs/state consistency assert
    (consumes EvaluationEnvironments from the substrate layer; seeding and
    canonical reads go through the evaluation plane)
  * ``trace_capture`` — W2 trace capture helper

Runtime write paths live in ``taskvm.execution.gui_driver`` (GUI-only task
adapters); substrate specifics live under ``taskvm.substrate``.
"""
