"""taskvm_bench.harness — evaluation-side capture + replay helpers.

What remains here is substrate-neutral tooling for the benchmark /
baselines plane (this package migrates with the bench split):

  * ``observations``  — StepObservation / TraceFixture value objects
  * ``replay_engine`` — DOM capture + parse + obs/state consistency assert
    (consumes EvaluationEnvironments from the substrate layer; seeding and
    canonical reads go through the evaluation plane)

Runtime write paths live in the runtime plane
(``taskvm.runtime.AutonomyRuntime`` over the ``SubstrateSession`` port);
substrate specifics live under ``taskvm.substrate``.
"""
