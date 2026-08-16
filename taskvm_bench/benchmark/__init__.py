"""Benchmark layer: fixtures (hidden canonical task graphs), model client
(frontier-API), cost model (real-token accounting), A2UI v0.9 spec.

FINAL BENCHMARK surface (Agent F): ``schema`` / ``tasks`` / ``registry``
define the final task taxonomy, open-world splits and suites consumed by
``taskvm_bench.evaluation``. The legacy modules (``fixtures`` /
``mobilegym_fixtures`` / ``ood_fixtures`` / ``a2ui_spec`` /
``a2ui_schema_manager`` / ``model_client`` / ``cost_model``) are still
imported by the not-yet-deleted workspace UI / execution stack; their
deletion owner is Agent G (Wave 3) once those callers are gone.

No-leak boundary: fixture/GT modules are verifier-only. They MUST NOT be
imported by the compiler path (``task_state/``, ``execution/``). The final
``TaskSpec`` keeps the same rule: ``seed``/``success``/``protected`` are
Evaluation-plane secrets.
"""
from taskvm_bench.benchmark.registry import (
    SUITES, Condition, all_conditions, condition_of, get_suite, list_suites,
)
from taskvm_bench.benchmark.schema import (
    Family, Injection, InjectionKind, Split, TaskSpec,
)
from taskvm_bench.benchmark.tasks import (
    all_tasks, get_task, tasks_in_family, tasks_in_split,
)

__all__ = [
    # final benchmark surface
    "SUITES", "Condition", "all_conditions", "condition_of",
    "get_suite", "list_suites",
    "Family", "Injection", "InjectionKind", "Split", "TaskSpec",
    "all_tasks", "get_task", "tasks_in_family", "tasks_in_split",
]
