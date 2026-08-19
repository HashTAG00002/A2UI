"""B-07 trial-integrity tests — every requested trial materializes.

The round's five regression oracles (Before evidence:
``eval_results/rm0/B-07_before.txt``):

1. an architect-plane failure inside the REAL ``bootstrap_real_full``
   path materializes an honest error record (classified
   ``architect_contract_error`` via the shared ledger's real telemetry,
   ``stage_reached="architect"``) instead of a raw exception escaping
   and killing the batch;
2. trial 1 failing never blocks trial 2 — both records land on disk;
3. a failing trial inherits NOTHING from the previous trial's success
   (``last_*`` handles are cleared at trial start);
4. the Stage Survival Funnel computes the hand-checked oracle;
5. the CLI substrate/suite defaults resolve coherently (Task E) and the
   batch loop guards residuals (Task B): a leaked exception becomes a
   synthetic record and the batch continues; only
   ``infrastructure_fatal`` stops it (clear reason, rc=2).
"""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from taskvm.architect import ModelReply
from taskvm_bench.benchmark.mobilegym_fixtures import TOP3_EXPENSE_TO_WECHAT
from taskvm_bench.evaluation.funnel import build_funnel, render_funnel
from taskvm_bench.evaluation.results import RunDirectory, TrialRecord
from taskvm_bench.tests.evaluation.test_mobilegym_factory import (
    FakeBridge, FakeDriver, _factory, _spec,
)


@pytest.fixture()
def fake_bridge():
    bridge = FakeBridge()
    yield bridge
    bridge.close()


# ── the scripted provider (REAL bootstrap path, Task D single path) ─────────

class ScriptedPort:
    """One scripted reply per REAL provider request, in call order."""

    default_model = "scripted-b07"

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []

    def complete_json(self, *, system, user, model=None, max_tokens=3072,
                      temperature=None, image_data_url=None):
        self.calls.append(user)
        item = self.script.pop(0) if self.script else {"kind": "done"}
        return ModelReply(parsed=item,
                          raw=json.dumps(item, ensure_ascii=False),
                          model=model or self.default_model,
                          prompt_tokens=5, completion_tokens=3)


def _compiler_reply() -> dict:
    """A valid StateCompiler reply for the fake L1 observation.

    Evidence surface_label must match the compiler view's region label:
    the mobilegym session's surface display_name comes from the app
    catalog's user-visible name — "wechat" → "微信". No value_pattern
    → the regex re-read check is skipped (the fake visible text carries
    no machine-readable value syntax)."""
    return {
        "variables": [{
            "semantic_key": "wechat_peer", "label": "wechat_peer",
            "value_type": "text", "mutability": "editable",
            "observed": "黄勇", "confidence": 0.9,
            "evidence": [{
                "surface_label": "微信", "visible_label": "黄勇",
                "visible_context": "微信 黄勇 通讯录"}]}],
        "ambiguities": [], "needs_clarification": False,
    }


# ── Test 1: architect failure materializes (REAL bootstrap path) ────────────

def test_architect_failure_materializes_honest_record(
        fake_bridge, monkeypatch, tmp_path):
    """The Before-1 hole, closed: a RuntimeError raised by the frozen
    architect plane (BEFORE its model call) inside the REAL
    ``bootstrap_real_full`` composition escapes ``run_trial`` as a
    MATERIALIZED record — never a raw exception, never a batch kill.

    Classification is by REAL telemetry: the scripted compiler call
    landed (ledger ``state_compiler`` row) but no ``task_architect``
    row did → ``architect_contract_error``, ``stage_reached=architect``."""
    import taskvm.architect.architect as arch_mod

    def boom(self, *a, **kw):
        raise RuntimeError("architect contract invalid")

    monkeypatch.setattr(arch_mod.TaskArchitect, "compose", boom)

    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    port = ScriptedPort([_compiler_reply()])
    driver = FakeDriver()
    spec = _spec(model="m1", condition="taskvm-real-full")

    record = factory.run_trial(spec, model_port=port, driver=driver)

    # the compiler stage REALLY ran (real bootstrap path — Task D
    # evidence): exactly one scripted provider request was consumed
    assert len(port.calls) == 1
    # the architect died: the driver never ran, no ops recorded
    assert driver.executed == []
    assert record.user_ops == []
    # honest materialized record — no raise, verdict error, classified
    assert record.trial_verdict == "error"
    assert record.failure_class == "architect_contract_error"
    assert record.stage_reached == "architect"
    assert "architect contract invalid" in (record.evaluation_error or "")
    assert record.cua_entered is False
    # the record serializes with the schema-2 fields (disk proof)
    run_dir = RunDirectory("b07-t1", root=str(tmp_path))
    path = run_dir.write_trial(record, 0)
    blob = json.load(open(path, encoding="utf-8"))
    assert blob["failure_class"] == "architect_contract_error"
    assert blob["stage_reached"] == "architect"
    assert blob["cua_entered"] is False
    assert blob["trial_verdict"] == "error"


def test_failed_provider_request_never_launders_the_stage(
        fake_bridge):
    """A provider request that DIED (e.g. HTTP 401) still leaves a
    ledger row — ``ok=False``, written from the call site's finally.
    The classification must count only COMPLETED (``ok``) rows: a
    401 in the compiler call is ``compiler_contract_error``, never a
    laundered ``architect_contract_error``.

    Found live on the real bridge (no-credentials smoke): both trials
    were mis-attributed to the architect stage before this fix."""
    class DeadProviderPort(ScriptedPort):
        def complete_json(self, **kw):
            self.calls.append(kw.get("user", ""))
            raise RuntimeError("model endpoint HTTP 401")

    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    port = DeadProviderPort([])
    record = factory.run_trial(
        _spec(model="m1", condition="taskvm-real-full"),
        model_port=port, driver=FakeDriver())
    assert len(port.calls) == 1          # the compiler request really fired
    assert record.trial_verdict == "error"
    assert record.failure_class == "compiler_contract_error"
    assert record.stage_reached == "compiler"
    assert record.cua_entered is False


# ── Test 2: trial 1 failing never blocks trial 2 ────────────────────────────

def test_first_trial_failure_does_not_block_second(
        fake_bridge, monkeypatch, tmp_path):
    """The Before-2 hole, closed: the two-trial batch where trial 1's
    architect fails now runs trial 2 to completion — both records on
    disk, nothing skipped."""
    import taskvm.architect.architect as arch_mod

    def boom(self, *a, **kw):
        raise RuntimeError("architect contract invalid")

    monkeypatch.setattr(arch_mod.TaskArchitect, "compose", boom)

    factory = _factory(fake_bridge)
    factory.ensure_bridge()
    run_dir = RunDirectory("b07-cont", root=str(tmp_path))

    # trial 1 — REAL path architect failure
    port = ScriptedPort([_compiler_reply()])
    r1 = factory.run_trial(
        _spec(model="m1", condition="taskvm-real-full"),
        model_port=port, driver=FakeDriver())
    p1 = run_dir.write_trial(r1, 0)

    # trial 2 — same factory, next sample: a normal (fake-bootstrap)
    # trial must run to completion AFTER a failure
    driver2 = FakeDriver()
    r2 = factory.run_trial(
        _spec(model="m1", condition="taskvm-real-full", sample_index=1),
        bootstrap_fn=lambda **kw: {"sid": kw["sid"]}, driver=driver2)
    p2 = run_dir.write_trial(r2, 1)

    assert r1.trial_verdict == "error"
    assert r1.failure_class == "architect_contract_error"
    # R1: trial 2 ran to completion but was never graded (no evidence
    # recorder injected) — honest "pending", never a fake "pass"
    assert r2.trial_verdict == "pending"
    assert r2.failure_class == "ungraded"
    assert [op.kind for op in driver2.executed] == ["start", "stop"]
    for p in (p1, p2):
        assert json.load(open(p, encoding="utf-8"))["substrate"] == \
            "mobilegym"


# ── Test 3: no cross-trial dirty state ──────────────────────────────────────

def test_failing_trial_never_inherits_previous_success(fake_bridge):
    """The Before-3 sibling hole, closed: a mid-trial failure leaves
    THIS trial's partial artifacts (or None) — the previous trial's
    success bundle/integrity can never be mis-attributed to it."""
    factory = _factory(fake_bridge)
    factory.ensure_bridge()

    ok = factory.run_trial(
        _spec(), bootstrap_fn=lambda **kw: {"sid": kw["sid"]},
        driver=FakeDriver())
    # R1: ungraded all-applied trial — honest "pending"
    assert ok.trial_verdict == "pending"
    assert factory.last_bundle is not None
    assert factory.last_integrity == {"status": "ok", "detail": "",
                                      "final_state_hash": None} \
        or factory.last_integrity.get("status") == "ok"

    spec2 = _spec(sample_index=1)

    def boom(**kw):
        raise RuntimeError("bootstrap exploded")

    bad = factory.run_trial(spec2, bootstrap_fn=boom, driver=FakeDriver())
    assert bad.trial_verdict == "error"
    assert bad.failure_class == "compiler_contract_error"  # no compiler
    # row landed in the fresh ledger → the leak pre-dates the compiler
    # the LAST trial's handles are trial 2's, never trial 1's
    assert factory.last_record is bad
    assert factory.last_bundle is None          # NOT trial 1's bundle
    # integrity never ran in trial 2 — the honest 'skipped' marker,
    # never trial 1's stale 'ok'
    assert factory.last_integrity["status"] == "skipped"
    assert factory.last_setup.sid == spec2.resolve_sid()
    # the manifest reports honest 'skipped', not stale 'ok'
    mf = factory.manifest_fields(spec2)
    assert mf["final_integrity_status"] == "skipped"
    assert mf["sid"] == spec2.resolve_sid()


# ── Test 4: the Stage Survival Funnel (hand-computed oracle) ────────────────

def _rec(**kw) -> TrialRecord:
    r = TrialRecord(model="m", substrate="mobilegym", condition="c")
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_funnel_hand_computed_oracle():
    """4 materialized of 5 requested; the survival bars are
    hand-computed here — the funnel never guesses."""
    records = [
        _rec(stage_reached="complete", trial_verdict="pass",
             cua_entered=True),
        _rec(stage_reached="architect", trial_verdict="error",
             failure_class="architect_contract_error"),
        _rec(stage_reached="execution", trial_verdict="error",
             failure_class="execution_error", cua_entered=True),
        _rec(stage_reached="setup", trial_verdict="error",
             failure_class="evaluation_error"),
    ]
    f = build_funnel(records, trials_requested=5)
    assert f.trials_requested == 5
    assert f.trials_materialized == 4
    assert f.trials_missing == 1
    assert f.counts_by_stage == {"complete": 1, "architect": 1,
                                 "execution": 1, "setup": 1}
    assert f.counts_by_failure_class == {
        "architect_contract_error": 1, "execution_error": 1,
        "evaluation_error": 1}
    # survival bars (denominators in comments)
    assert f.entered_bootstrap_count == 3        # r1,r2,r3 / 5 requested
    assert f.entered_bootstrap_rate == pytest.approx(0.6)
    assert f.survived_compiler_count == 3        # r1,r2,r3 / 3 entered
    assert f.survived_compiler_rate == 1.0
    assert f.survived_architect_count == 2       # r1,r3 / 3 entered
    assert f.survived_architect_rate == pytest.approx(2 / 3)
    assert f.cua_entry_count == 2                # r1,r3 / 2 survived
    assert f.cua_entry_rate == 1.0
    assert f.complete_count == 1                 # / 4 materialized
    assert f.complete_rate == 0.25
    assert f.strict_pass_count == 1              # / 4 materialized
    assert f.strict_pass_rate == 0.25
    # dict form (a JSON round-trip) computes the IDENTICAL funnel —
    # report consumers see the same numbers as the live batch
    dicts = [json.loads(json.dumps(asdict(r))) for r in records]
    assert build_funnel(dicts, trials_requested=5).to_dict() == f.to_dict()
    # the terminal block names every bar
    txt = render_funnel(f)
    assert "stage survival funnel" in txt
    assert "missing: 1" in txt
    for needle in ("entered bootstrap", "survived compiler",
                   "survived architect", "entered CUA", "completed",
                   "strict pass"):
        assert needle in txt, needle


def test_funnel_strict_pass_never_laundered():
    """A verdict 'pass' carrying an evaluation_error is NOT a strict
    pass (a graded pass never launders a broken world)."""
    f = build_funnel(
        [_rec(stage_reached="complete", trial_verdict="pass",
              evaluation_error="post-trial integrity: unavailable")],
        trials_requested=1)
    assert f.complete_count == 1
    assert f.strict_pass_count == 0


def test_funnel_empty_batch_is_honest_zeros():
    f = build_funnel([], trials_requested=0)
    assert f.trials_materialized == 0
    assert f.strict_pass_rate == 0.0        # never a crash, never a guess
    assert "strict pass" in render_funnel(f)


# ── Test 5: CLI substrate/suite defaults + routing (Task E) ─────────────────

def test_resolve_substrate_and_suite_defaults():
    from taskvm_bench.evaluation.cli import resolve_substrate_and_suite \
        as resolve
    assert resolve(None, None) == ("mobilegym", "rm-smoke")
    assert resolve("world", None) == ("world", "smoke")        # legacy
    assert resolve("mobilegym", None) == ("mobilegym", "rm-smoke")
    assert resolve(None, "final") == ("world", "final")       # suite names
    assert resolve(None, "rm-smoke") == ("mobilegym", "rm-smoke")
    assert resolve("mobilegym", "rm-smoke") == ("mobilegym", "rm-smoke")
    assert resolve("world", "final") == ("world", "final")
    with pytest.raises(SystemExit):
        resolve(None, "nosuch-suite")


def test_bare_run_routes_to_mobilegym_rm_smoke(monkeypatch):
    """Bare ``cli run`` now lands on the RM-0.B default surface —
    mobilegym + rm-smoke (the old default was world/smoke; the flip is
    the prompt's Task E, an intentional oracle change)."""
    import taskvm_bench.evaluation.cli as cli

    seen = {}

    def fake_branch(args):
        seen["substrate"] = args.substrate
        seen["suite"] = args.suite
        return 0

    monkeypatch.setattr(cli, "_run_mobilegym", fake_branch)
    assert cli.main(["run"]) == 0
    assert seen == {"substrate": "mobilegym", "suite": "rm-smoke"}


def test_mobilegym_with_world_suite_is_rejected():
    """default substrate + world-suite string is an explicit conflict —
    SystemExit with the honest reason (never a silent world run)."""
    from taskvm_bench.evaluation.cli import main
    with pytest.raises(SystemExit):
        main(["run", "--substrate", "mobilegym", "--suite", "smoke"])


# ── Test 6/7: the CLI batch guard (Task B) ──────────────────────────────────

def _cli_run(monkeypatch, fake_bridge, tmp_path, factory_cls, extra):
    """Drive ``cli main run`` against a scripted factory class; returns
    (rc, run_dir_path)."""
    import taskvm_bench.evaluation.mobilegym_factory as factory_mod
    from taskvm_bench.evaluation.cli import main
    monkeypatch.setattr(factory_mod, "MobileGymFactory", factory_cls)
    return main([
        "run", "--suite", "rm-smoke", "--substrate", "mobilegym",
        "--condition", "taskvm-real-full",
        "--bridge-port", str(fake_bridge.port),
        "--projection-port", "0",
        "--out", str(tmp_path), *extra]), None


def _run_dirs_under(tmp_path):
    import os
    for name in sorted(os.listdir(tmp_path)):
        d = tmp_path / name
        if d.is_dir():
            return d
    raise AssertionError("no run dir created")


def _ok_record(spec):
    from taskvm_bench.evaluation.results import UserOpRecord
    rec = TrialRecord(model=spec.model or "", substrate="mobilegym",
                      condition=spec.condition,
                      sample_index=spec.sample_index)
    rec.add_op(UserOpRecord(op_id="uop-0001", kind="start",
                            verdict="applied"))
    rec.stage_reached = "complete"
    rec.cua_entered = True
    rec.finalize()
    return rec


def test_cli_residual_leak_materializes_and_batch_continues(
        monkeypatch, fake_bridge, tmp_path):
    """Before: a leaked exception killed the batch. After (Task B): the
    FIRST trial's raw leak becomes a synthetic error record on disk and
    the SECOND trial still runs — both land as trial-000/001."""
    import taskvm_bench.evaluation.mobilegym_factory as factory_mod

    events = []

    class LeakyThenOKFactory(factory_mod.MobileGymFactory):
        def run_trial(self, spec, **kw):
            events.append(spec.sample_index)
            if spec.sample_index == 0:
                raise RuntimeError("residual leak from beyond the "
                                   "stage boundaries")
            rec = _ok_record(spec)
            self.last_setup = factory_mod.TrialSetup(
                sid=spec.resolve_sid())
            self.last_integrity = {"status": "ok", "detail": ""}
            return rec

    rc, _ = _cli_run(monkeypatch, fake_bridge, tmp_path, LeakyThenOKFactory,
                     ["--samples", "2"])
    assert rc == 0                        # non-fatal: batch NOT stopped
    assert events == [0, 1]               # trial 2 really ran
    d = _run_dirs_under(tmp_path)
    t0 = json.load(open(d / "trials" / "trial-000.json",
                        encoding="utf-8"))
    t1 = json.load(open(d / "trials" / "trial-001.json",
                        encoding="utf-8"))
    assert t0["trial_verdict"] == "error"
    assert "residual leak" in t0["evaluation_error"]
    assert t0["failure_class"] == "execution_error"
    # R1: trial 2 completed its ops but was never graded — "pending",
    # and a pending trial is NOT a strict pass (mean/majority discipline:
    # an unverified dimension is not a passed dimension)
    assert t1["trial_verdict"] == "pending"
    assert t1["failure_class"] == "ungraded"
    # the funnel saw both (2 materialized of 2 requested)
    funnel = json.load(open(d / "reports" / "funnel.json",
                           encoding="utf-8"))
    assert funnel["trials_materialized"] == 2
    assert funnel["trials_missing"] == 0
    assert funnel["strict_pass_count"] == 0


def test_cli_infrastructure_fatal_stops_batch_with_reason(
        monkeypatch, fake_bridge, tmp_path):
    """Only infrastructure_fatal stops the batch: a record carrying the
    fatal class halts trial 2, lands the manifest with the reason, and
    the CLI exits 2 (loudly, never silently)."""
    import taskvm_bench.evaluation.mobilegym_factory as factory_mod

    events = []

    class FatalFirstFactory(factory_mod.MobileGymFactory):
        def run_trial(self, spec, **kw):
            events.append(spec.sample_index)
            if spec.sample_index == 0:
                rec = TrialRecord(model=spec.model or "",
                                  substrate="mobilegym",
                                  condition=spec.condition,
                                  sample_index=spec.sample_index)
                rec.evaluation_error = \
                    "setup stage: BridgeUnavailableError: bridge down"
                rec.failure_class = "infrastructure_fatal"
                rec.stage_reached = "setup"
                rec.finalize()
                rec.trial_verdict = "error"
                self.last_setup = factory_mod.TrialSetup(
                    sid=spec.resolve_sid())
                self.last_integrity = None
                return rec
            return _ok_record(spec)

    rc, _ = _cli_run(monkeypatch, fake_bridge, tmp_path, FatalFirstFactory,
                     ["--samples", "2"])
    assert rc == 2
    assert events == [0]                  # trial 2 never ran
    d = _run_dirs_under(tmp_path)
    manifest = json.load(open(d / "manifest.json", encoding="utf-8"))
    assert manifest["batch_stopped_reason"]
    assert "bridge down" in manifest["batch_stopped_reason"]
    funnel = manifest["funnel"]
    assert funnel["trials_requested"] == 2
    assert funnel["trials_materialized"] == 1
    assert funnel["trials_missing"] == 1


def test_cli_pre_trial_factory_error_stops_batch(
        monkeypatch, fake_bridge, tmp_path):
    """A pre-trial FactoryError (bridge cannot be established — nothing
    was entered, nothing to materialize) stops the batch with rc=2."""
    import taskvm_bench.evaluation.mobilegym_factory as factory_mod

    class DeadBridgeFactory(factory_mod.MobileGymFactory):
        def run_trial(self, spec, **kw):
            raise factory_mod.BridgeUnavailableError(
                "bridge never became healthy")

    rc, _ = _cli_run(monkeypatch, fake_bridge, tmp_path, DeadBridgeFactory,
                     ["--samples", "2"])
    assert rc == 2
    d = _run_dirs_under(tmp_path)
    manifest = json.load(open(d / "manifest.json", encoding="utf-8"))
    assert "BridgeUnavailableError" in manifest["batch_stopped_reason"]
    # nothing materialized — and the funnel says so honestly
    assert manifest["funnel"]["trials_materialized"] == 0
    assert manifest["funnel"]["trials_missing"] == 2


def test_cli_running_index_two_fixtures_never_overwrite(
        monkeypatch, fake_bridge, tmp_path):
    """Two fixtures × 1 sample: four→two files land as trial-000 AND
    trial-001 (the old per-sample index silently overwrote trial-000)."""
    import taskvm_bench.evaluation.mobilegym_factory as factory_mod

    class OkFactory(factory_mod.MobileGymFactory):
        def run_trial(self, spec, **kw):
            rec = _ok_record(spec)
            self.last_setup = factory_mod.TrialSetup(
                sid=spec.resolve_sid())
            self.last_integrity = {"status": "ok", "detail": ""}
            return rec

    rc, _ = _cli_run(monkeypatch, fake_bridge, tmp_path, OkFactory,
                     ["--task", "top3_expense_to_wechat",
                      "--task", "social_morning_brief"])
    assert rc == 0
    d = _run_dirs_under(tmp_path)
    names = sorted(p.name for p in (d / "trials").iterdir())
    assert names == ["trial-000.json", "trial-001.json"]
