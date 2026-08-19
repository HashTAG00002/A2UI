"""taskvm.architect — L4: State Compiler + Task Architect (frozen contract:
docs/contracts/architect.md).

Two high-level model roles live here (master handoff §6):

1. **StateCompiler** — visible observations → task variables + binding
   evidence (fast path deterministic, slow path one model call).
2. **TaskArchitect / Projection Composer** — goal + observed state → ONE
   coherent ``TaskArchitecture`` (milestones/checkpoints, workflow topology,
   projection schema, action contracts, risk/reversibility, verification
   intent — jointly, from a single call).

Plus the deterministic ``ActionContractSerializer`` (CUA-goal text without
any model call) and the ``ModelPort``/``ModelCallLedger`` accounting every
model invocation so the benchmark can separate compiler / architect / CUA
overhead.

Layer rules (architecture gate enforces): this package imports ONLY
``taskvm.domain`` + ``taskvm.kernel`` + stdlib — never substrate, never
benchmark, never evaluation.
"""
from taskvm.architect.architect import (
    ArchitectOutputError, RecomposeProposal, TaskArchitect,
    historical_node_ids,
)
from taskvm.architect.compiler import (
    CompilerOutputError, CompilerResult, SlowPathReport, StateCompiler,
)
from taskvm.architect.http_port import HttpModelPort, HttpModelPortError
from taskvm.architect.noleak import (
    PromptLeakError, assert_prompt_clean, scan, scan_json_values,
)
from taskvm.architect.observation import (
    CompilerObservationView, HandleEvidence, VisibleRegion,
)
from taskvm.architect.port import (
    MODEL_ROLE_CUA, MODEL_ROLE_MODEL_VERIFIER, MODEL_ROLE_STATE_COMPILER,
    MODEL_ROLE_TASK_ARCHITECT, MODEL_ROLES, ModelCallLedger,
    ModelCallRecord, ModelPort, ModelReply,
)
from taskvm.architect.serializer import (
    ActionContractSerializer, patchop_cua_goal,
)

__all__ = [
    # observation DTO
    "CompilerObservationView", "VisibleRegion", "HandleEvidence",
    # roles
    "StateCompiler", "CompilerResult", "CompilerOutputError",
    "SlowPathReport",
    "TaskArchitect", "RecomposeProposal", "ArchitectOutputError",
    "historical_node_ids",
    # serializer
    "ActionContractSerializer", "patchop_cua_goal",
    # model port + accounting
    "ModelPort", "ModelReply", "ModelCallLedger", "ModelCallRecord",
    "MODEL_ROLE_STATE_COMPILER", "MODEL_ROLE_TASK_ARCHITECT",
    "MODEL_ROLE_CUA", "MODEL_ROLE_MODEL_VERIFIER", "MODEL_ROLES",
    "HttpModelPort", "HttpModelPortError",
    # no-leak gate
    "PromptLeakError", "assert_prompt_clean", "scan", "scan_json_values",
]
