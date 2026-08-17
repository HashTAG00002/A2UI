"""B-05 — per-op / per-trial result schema and the run directory.

The user operation is the RM evaluation's minimal verdict unit, so the
persisted record is per-op FIRST, trial second. Files land under
``eval_results/<run-id>/`` (gitignored — never staged):

    eval_results/<run-id>/
        manifest.json       run-level metadata (model, substrate, seeds…)
        trials/trial-<i>.json   one TrialRecord per trial
        artifacts/              screenshots / dumps referenced by records
        reports/                aggregated reports (later waves)

Schema notes (RM-0 work order §B-05):

* ``schema_version`` pins THIS shape — old records are never silently
  re-interpreted by newer code;
* ``environment_seed`` (world initialisation) and ``sample_index``
  (stochastic model replicate) are TWO DIFFERENT concepts and both are
  carried explicitly — a CLI named ``--seeds`` never gets to conflate
  them;
* ``world_diff`` / ``protected_diff`` / ``ledger_request_ids`` are filled
  by the harness layer when those observables exist; honest ``None``
  otherwise (never fabricated).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

#: per-op/trial record schema introduced by RM-0.B (B-05)
SCHEMA_VERSION = "taskvm-userop-1"

_HARNESS_VERSION = "rm0-bench-1"


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


@dataclass
class UserOpRecord:
    op_id: str
    kind: str
    verdict: str                          # applied|rejected|unsettled|error
    world_diff: Optional[dict] = None
    protected_diff: Optional[dict] = None
    projection: dict = field(default_factory=dict)
    rollback: Optional[dict] = None
    ledger_request_ids: Optional[list] = None
    timeline: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)
    http_status: Optional[int] = None
    response: dict = field(default_factory=dict)
    detail: str = ""


@dataclass
class TrialRecord:
    schema_version: str = SCHEMA_VERSION
    git_sha: str = field(default_factory=_git_sha)
    task_version: str = "rm-smoke-0"
    harness_version: str = _HARNESS_VERSION
    model: str = ""
    substrate: str = ""
    condition: str = ""
    environment_seed: Optional[int] = None
    sample_index: int = 0
    user_ops: list = field(default_factory=list)   # list[UserOpRecord-dict]
    trial_verdict: str = "pending"                 # pass|fail|error|pending
    failure_class: str = ""                        # honest, or ""
    evaluation_error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    development_only: bool = True                  # RM-0.B plumbing smoke

    def add_op(self, record: UserOpRecord) -> None:
        self.user_ops.append(asdict(record))

    def finalize(self) -> None:
        """Trial verdict from per-op verdicts (mean/majority discipline —
        one lucky op never passes a trial; any error is an error)."""
        verdicts = [op.get("verdict", "error") for op in self.user_ops]
        if not verdicts:
            self.trial_verdict = "error"
            self.failure_class = "no-user-ops-recorded"
            return
        if all(v == "applied" for v in verdicts):
            self.trial_verdict = "pass"
            return
        if any(v == "error" for v in verdicts):
            self.trial_verdict = "error"
        elif all(v in ("applied", "rejected") for v in verdicts):
            self.trial_verdict = "fail"        # honest rejection ≠ crash
            self.failure_class = "user-op-rejected"
        else:
            self.trial_verdict = "fail"
            self.failure_class = "user-op-unsettled"


class RunDirectory:
    """The on-disk result layout for ONE evaluation run."""

    def __init__(self, run_id: str, root: str = "eval_results") -> None:
        self.run_id = run_id
        self.root = os.path.join(root, run_id)
        self.trials_dir = os.path.join(self.root, "trials")
        self.artifacts_dir = os.path.join(self.root, "artifacts")
        self.reports_dir = os.path.join(self.root, "reports")
        for d in (self.trials_dir, self.artifacts_dir, self.reports_dir):
            os.makedirs(d, exist_ok=True)

    # ── writers ────────────────────────────────────────────────────────
    def write_trial(self, record: TrialRecord, index: int) -> str:
        path = os.path.join(self.trials_dir, f"trial-{index:03d}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(record), fh, indent=1, ensure_ascii=False)
        return path

    def write_artifact(self, name: str, data: bytes) -> str:
        path = os.path.join(self.artifacts_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def write_report(self, name: str, payload: Any) -> str:
        path = os.path.join(self.reports_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, ensure_ascii=False)
        return path

    def write_manifest(self, **fields: Any) -> str:
        manifest = dict(
            run_id=self.run_id,
            schema_version=SCHEMA_VERSION,
            harness_version=_HARNESS_VERSION,
            git_sha=_git_sha(),
            created_at=time.time(),
            development_only=True,      # RM-0.B: plumbing smoke, NOT an RM result
        )
        manifest.update(fields)
        path = os.path.join(self.root, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1, ensure_ascii=False)
        return path
