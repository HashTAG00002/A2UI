"""The final evaluation-plane contract tests (handoff 07 §最终测试):

1. Runtime import graph has NO benchmark/evaluation module.
2. Oracle no-leak: captured prompts/events contain no hidden state.
3. Seed/reset reproducibility: same (task, condition, seed) → same trial.
4. The fault injector never touches production: physical object
   separation + injection routing only through the public governance
   seam.
5. Report aggregation never silently drops failed trials.
6. Phase vocabulary (the W-era gate-script word) is gone from
   code/README/pyproject and the final docs (git history and the
   historical governance record under docs/oracle/** are allowed to
   keep it).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from taskvm_bench.benchmark.registry import Condition
from taskvm_bench.benchmark.schema import Family, Injection, InjectionKind, Split, TaskSpec
from taskvm_bench.evaluation.aggregation import (
    aggregate_trials, classify_failure, report_from_trials,
)
from taskvm_bench.evaluation.harness import TrialBudget
from taskvm_bench.evaluation.runner import run_trial

REPO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", ".."))


# ── 1. runtime import graph is benchmark/evaluation-free ────────────────────

#: The prototype-side source files that still import taskvm_bench.benchmark.*
#: — every one would be a registered transitional-debt item (shrink-only;
#: a new offender fails the test below, E36 discipline). Wave-3 + the
#: taskvm_bench directory split (2026-08-16) emptied the register: the two
#: bench-plane survivors (harness/replay_engine, task_state/compiler)
#: migrated WITH the bench, so the prototype (taskvm/) imports nothing
#: from the bench plane.
LEGACY_BENCHMARK_IMPORTERS = frozenset({})


def test_runtime_import_graph_clean():
    """The MODERN runtime plane (runtime / kernel / architect / domain /
    projection) must import ZERO benchmark/evaluation modules in a fresh
    interpreter. The legacy stack (old governance/verifier/execution
    files) is registered debt pending Agent G's deletion wave."""
    code = (
        "import sys, pkgutil, importlib\n"
        "import taskvm.runtime, taskvm.kernel, taskvm.architect, "
        "taskvm.domain, taskvm.projection\n"
        "pkgs = [taskvm.runtime, taskvm.kernel, taskvm.architect, "
        "taskvm.domain, taskvm.projection]\n"
        "for pkg in pkgs:\n"
        "    for m in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + '.'):\n"
        "        importlib.import_module(m.name)\n"
        "bad = [m for m in sys.modules\n"
        "       if m.startswith('taskvm_bench')]\n"
        "print(','.join(bad))\n"
        "sys.exit(1 if bad else 0)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=REPO, timeout=120)
    assert r.returncode == 0, (
        f"modern runtime plane imports the bench plane (taskvm_bench.*): "
        f"{r.stdout.strip()}")


def test_legacy_benchmark_import_register_shrink_only():
    """The prototype (taskvm/) imports ZERO benchmark-plane modules: the
    set of source files importing taskvm_bench.benchmark.* equals the
    registered debt (now empty) — no new offenders, ever."""
    offenders = set()
    for dirpath, dirnames, filenames in os.walk(os.path.join(REPO, "taskvm")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, REPO)
            with open(path, encoding="utf-8", errors="replace") as f:
                src = f.read()
            if re.search(r"\b(?:from|import)\s+taskvm_bench\.benchmark\b", src):
                offenders.add(rel)
    assert offenders == set(LEGACY_BENCHMARK_IMPORTERS), (
        f"benchmark-import register drifted: "
        f"new={sorted(offenders - LEGACY_BENCHMARK_IMPORTERS)} "
        f"removed={sorted(LEGACY_BENCHMARK_IMPORTERS - offenders)} "
        f"(register is shrink-only; update it ONLY on deletion)")


# ── 2. oracle no-leak ────────────────────────────────────────────────────────

def test_oracle_secrets_never_reach_the_system_under_test(monkeypatch):
    """A canary spec whose success value exists NOWHERE in the system-
    visible world (not in the goal, not on any surface, never injected).
    Every prompt the fakes receive and every runtime event detail is
    captured; the canary and the protected-set must never appear."""
    CANARY = "ZZSECRET-SUCCESS-VALUE-XYZ"
    spec = TaskSpec(
        task_id="canary-no-leak",
        family=Family.SEQUENCE,
        split=Split.ID,
        goal="Set desktop_alpha to 2.",
        surfaces=("desktop",),
        seed={"desktop": {"desktop_alpha": "1", "desktop_prot": "keep"}},
        success={"desktop": {"desktop_alpha": CANARY}},
        protected=(("desktop", "desktop_prot"),),
    )

    prompts: list[str] = []

    from taskvm_bench.evaluation import harness as H

    class RecordingModelPort(H.TemplateModelPort):
        def complete_json(self, *, system, user, **kw):
            prompts.append(system + "\n" + user)
            return super().complete_json(system=system, user=user, **kw)

    class RecordingCUA(H.TemplateCUA):
        def predict_action(self, *, goal, observation, **kw):
            prompts.append(goal + "\n" + (observation.visible_text or ""))
            return super().predict_action(goal=goal,
                                          observation=observation, **kw)

    monkeypatch.setattr(H, "TemplateModelPort", RecordingModelPort)
    monkeypatch.setattr(H, "TemplateCUA", RecordingCUA)

    rec = run_trial(spec, Condition.TASKVM, seed=0, run_id="no-leak",
                    budget=TrialBudget(max_rounds=4))
    # the trial itself may pass or fail — the leak check is the point
    blob = "\n".join(prompts) + json.dumps(rec.trace, default=str)
    assert CANARY not in blob, "success-plane secret leaked into prompts"
    assert "protected" not in blob.replace("desktop_prot", ""), (
        "the protected-set concept leaked into system-visible text")


# ── 3. reproducibility ───────────────────────────────────────────────────────

def test_same_seed_same_trial():
    from taskvm_bench.benchmark.tasks import get_task
    spec = get_task("seq-release-sync")
    a = run_trial(spec, Condition.TASKVM, seed=7, run_id="repro",
                  budget=TrialBudget(max_rounds=8))
    b = run_trial(spec, Condition.TASKVM, seed=7, run_id="repro",
                  budget=TrialBudget(max_rounds=8))
    da, db = a.to_json(), b.to_json()
    for d in (da, db):
        d.pop("elapsed_ms", None)          # wall clock is not semantics
    assert da == db, "same (task, condition, seed) must reproduce exactly"


# ── 4. the fault injector never touches production ─────────────────────────

def test_world_and_oracle_cannot_reach_production():
    """Physical separation: the exam room and the grader import nothing
    from the runtime plane (kernel/runtime/governance/execution)."""
    for mod in ("world", "oracle"):
        path = os.path.join(REPO, "taskvm_bench", "evaluation", f"{mod}.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for forbidden in ("taskvm.kernel", "taskvm.runtime",
                          "taskvm.governance", "taskvm.execution",
                          "taskvm.architect"):
            assert forbidden not in src, (
                f"evaluation/{mod}.py imports {forbidden} — the exam room "
                "must be physically unable to reach the runtime")


def test_injections_route_only_through_public_seams():
    """Governance-shaped injections flow through ``gov.handle(...)`` typed
    events and the runtime's public request_pause/execute_compensation —
    never through kernel mutators. AST-based (comments don't count)."""
    import ast
    path = os.path.join(REPO, "taskvm_bench", "evaluation", "harness.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    accessed: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "kernel"):
            accessed.add(node.attr)
    readonly = {"task_state", "workflow", "checkpoints", "epoch",
                "pending_recompose"}
    violators = sorted(accessed - readonly)
    assert not violators, (
        f"harness reaches kernel mutators directly: {violators}")
    # injections are delivered as typed governance events
    with open(path, encoding="utf-8") as f:
        src = f.read()
    for ev in ("GoalPatchRequested", "LocalPatchRequested",
               "RollbackRequested", "ConflictResolutionRequested"):
        assert ev in src, f"governance event {ev} not routed"


def test_environment_writes_cannot_advance_injection_milestones():
    """The injection milestone counter counts SYSTEM writes only — an
    environment write must never trigger another injection."""
    from taskvm_bench.evaluation.world import ENVIRONMENT, BenchmarkWorld
    spec = TaskSpec(
        task_id="milestone-probe", family=Family.CONFLICT, split=Split.ID,
        goal="Set k to 1.",
        surfaces=("desktop",),
        seed={"desktop": {"k": "0", "j": "0"}},
        success={"desktop": {"k": "1"}},
        injections=(
            Injection(kind=InjectionKind.EXTERNAL_FIELD_CHANGE,
                      after_writes=1,
                      payload={"surface": "desktop", "key": "j",
                               "value": "9"}),
            Injection(kind=InjectionKind.EXTERNAL_FIELD_CHANGE,
                      after_writes=2,
                      payload={"surface": "desktop", "key": "j",
                               "value": "8"}),
        ),
    )
    world = BenchmarkWorld(spec)
    world.begin_trial()
    assert world.fired_injections() == ()       # 0-milestone: nothing yet
    world.apply_write("desktop", "j", "7", actor=ENVIRONMENT)
    assert world.fired_injections() == (), (
        "an environment write advanced the injection milestone")


# ── 5. aggregation never drops failed trials ────────────────────────────────

def _synthetic_trials() -> list[dict]:
    base = dict(run_id="t", task_id="x", family="sequence", split="id",
                condition="taskvm", seed=0, stop_reason="done",
                verdict={"success": True}, evaluation_error=None,
                harness_crash=None, model_calls_by_role={}, gui_actions=1,
                total_interactions=1, required_ops=1, elapsed_ms=1.0,
                system_writes=1, injections_fired=[], trace=[], detail="",
                extras={})
    fail = dict(base, task_id="f", stop_reason="cua_fail",
                verdict={"success": False})
    fail["failure_class"] = classify_failure(fail)
    evalerr = dict(base, task_id="e", stop_reason="done", verdict=None,
                   evaluation_error="oracle blew up")
    evalerr["failure_class"] = classify_failure(evalerr)
    crash = dict(base, task_id="c", stop_reason="harness_crash",
                 verdict=None, harness_crash="ValueError: boom")
    crash["failure_class"] = classify_failure(crash)
    ok = dict(base, task_id="s")
    ok["failure_class"] = classify_failure(ok)
    return [ok, fail, evalerr, crash]


def test_aggregation_counts_every_trial():
    recs = _synthetic_trials()
    rep = report_from_trials({"run_id": "t"}, recs)
    b = rep["by_condition"]["taskvm"]
    assert b["n_trials"] == 4
    assert b["graded"] == 3                    # eval-error excluded from rate
    assert b["successes"] == 1
    assert b["evaluation_errors"] == 1
    assert b["harness_crashes"] == 1
    assert rep["totals"]["evaluation_errors"] == 1
    assert rep["totals"]["harness_crashes"] == 1
    # failure taxonomy classified all four (nothing left "?" or dropped)
    tax = b["failure_taxonomy"]
    assert tax.get("success") == 1 and tax.get("cua") == 1
    assert tax.get("evaluation_error") == 1 and tax.get("harness_crash") == 1


# ── 6. phase vocabulary is gone ─────────────────────────────────────────────

def test_phase_vocabulary_gone():
    """The phase vocabulary must be absent from production code, tests,
    README, pyproject and the final docs. Allowed: git history, the
    historical governance record (docs/oracle/**), the ledger (.mrules*),
    and persisted eval artifact filenames on disk (never entry points).
    (The needle is assembled at runtime so this file cannot self-match.)"""
    needle = "kill" + "test"
    roots = [
        os.path.join(REPO, "taskvm"),
        os.path.join(REPO, "taskvm_bench"),
        os.path.join(REPO, "tests"),
    ]
    files: list[str] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", ".git")]
            for name in filenames:
                if name.endswith(".py"):
                    files.append(os.path.join(dirpath, name))
    for extra in ("README.md", "pyproject.toml", "docs/benchmark.md"):
        p = os.path.join(REPO, extra)
        if os.path.isfile(p):
            files.append(p)
    offenders = []
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as f:
            if needle in f.read().lower():
                offenders.append(os.path.relpath(path, REPO))
    assert not offenders, f"phase vocabulary remains in: {offenders}"
