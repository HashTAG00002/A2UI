"""Evaluation plane: the final benchmark.

Modules: ``world`` (deterministic exam room), ``actors`` (the shared
capability fakes), ``oracle`` (hidden ground-truth grader), ``harness``
(the system conditions), ``runner`` (environment controller + matrix
executor), ``statistics`` / ``aggregation`` (report schema), ``cli``
(the unified entry point). Legacy phase-gate scripts are deleted; the
runner is ``python -m taskvm.evaluation.cli``.
"""
