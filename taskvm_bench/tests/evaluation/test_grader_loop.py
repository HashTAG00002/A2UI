"""R1 — the deterministic grader loop over a FAKE PORT (unit level).

The loop under test, end to end and with zero real infrastructure::

    FakePort (hidden world + public projection digests)
        → EvidenceRecorder.begin / before_op / bracket_user_op / finish
        → EvidenceBundle
        → grade_task(spec, bundle)            # the UNIQUE grading entry
        → ContractVerdict (EXACTLY five fields)
        → TrialRecord.contract_verdict + finalize()

Iron rules pinned here (R1 work order):

* ``grade_task`` is a PURE deterministic function — same bundle twice
  gives the equal verdict, and grading never mutates the bundle;
* the verdict carries EXACTLY five fields (world_contract /
  governance_contract / projection_consistency / progress /
  failure_codes) — ``passed`` is derived, never stored;
* the failure-code vocabulary is CLOSED: every code the grader can
  emit is a member of ``predicates.FAILURE_CODES`` — a test fires
  each family at least once and nothing outside the vocabulary;
* the no-op loophole stays closed: a rollback trial whose final state
  equals its seed still FAILS unless the witness values appeared on
  the oracle timeline (the system must have done the work it undid);
* irreversible keys may NOT be silently undone inside the rollback
  window, and the eval plane may not perform its own restore after
  the rollback request (the hidden world-write predicates);
* per-op "applied" is a DIAGNOSTIC signal: an all-applied trial with
  no landed ``contract_verdict`` finalizes to honest ``pending``,
  never to ``pass``.

Substrate independence: the fake port speaks the SAME normalized
oracle shape ``{surface: {key: value}}`` the builtin world and the
MobileGym adapter produce, so these tests grade the grader — not any
particular substrate.
"""
from __future__ import annotations

import json
from dataclasses import replace

from taskvm_bench.benchmark.schema import Family, Split, TaskSpec
from taskvm_bench.evaluation.evidence import (
    EVIDENCE_SCHEMA_VERSION, EvidenceBundle, EvidenceRecorder,
    InterventionEvidence, WorldEvidenceRecorder, diff_states,
    protected_view,
)
from taskvm_bench.evaluation.grader import ContractVerdict, grade_task
from taskvm_bench.evaluation.predicates import FAILURE_CODES
from taskvm_bench.evaluation.results import TrialRecord, UserOpRecord
from taskvm_bench.evaluation.user_ops import OpOutcome, UserOp


# ── the fixture: one mini "mark then true rollback" task ────────────────────

#: a ROLLBACK-family task: publish the note (witness), then roll back.
#: ``audit_seq`` models the irreversible audit trail — the publish bump
#: must SURVIVE an honest rollback (a silent revert is a hidden undo).
SPEC = TaskSpec(
    task_id="fake-port-rb-1",
    family=Family.ROLLBACK,
    split=Split.ID,
    goal="publish the note, then roll back to the checkpoint",
    surfaces=("notes",),
    seed={"notes": {"title": "draft", "status": "idle",
                    "author": "amy", "audit_seq": "3"}},
    success={"notes": {"title": "draft", "status": "idle"}},
    protected=(("notes", "author"),),
    irreversibles=("audit_seq",),
    witness=(("notes", "status", "published"),
             ("notes", "audit_seq", "4")),
)


# ── the fake port ───────────────────────────────────────────────────────────

class FakePort:
    """A fake substrate: one hidden world dict (the oracle) + honest op
    semantics + per-test dishonesty knobs.

    Plays BOTH sides the EvidenceRecorder brackets — the oracle read
    (``snapshot``, the eval plane's own power) and the public
    projection digests the SUT renders (the ``_projection_digest``
    shape: ``{key: [desired, observed]}``). Honest semantics:

    * ``checkpoint``  — stores a world snapshot, answers its id;
    * ``local_patch`` — lands every update (minus ``drop``);
    * ``rollback``    — restores the checkpoint EXCEPT the port-level
      ``irreversible`` keys and the per-op ``keep`` keys.

    The knobs simulate the dishonest variants the grader must catch:
    ``keep`` (partial restore), a port built WITHOUT the irreversibles
    (silent undo of history), ``lies`` (projection claims a value the
    world nowhere holds), ``drop`` (a patch the world never absorbed),
    ``omit_projection`` (the driver's honest ``available: False``),
    ``verdict``/``http_status``/``response_patch`` (rejected /
    unsettled / incomplete-disposition ops), ``gui_sse`` (the public
    GUI-action trajectory).
    """

    def __init__(self, seed, *, irreversible=()):
        self.world = {s: dict(kv) for s, kv in seed.items()}
        self.irreversible = set(irreversible)
        self.checkpoints: dict = {}

    # the hidden oracle read (eval-plane power only)
    def snapshot(self) -> dict:
        return {s: dict(kv) for s, kv in self.world.items()}

    # the PUBLIC projection digest (what the SUT renders)
    def digest(self, surface: str, lies=None) -> dict:
        variables = {k: [v, v] for k, v in self.world[surface].items()}
        for key, lie in (lies or {}).items():
            if key in variables:
                variables[key] = [variables[key][0], lie]
        return {"revision": 1, "variables": variables}

    def execute(self, op: UserOp, *, verdict: str = "applied",
                http_status: int = 200, gui_sse=(), keep=(), drop=(),
                lies=None, response_patch=None, omit_projection=False,
                extra_effect=None) -> OpOutcome:
        surface = next(iter(self.world))
        sse = [{"sse_type": t, "event_id": f"e{i}"}
               for i, t in enumerate(gui_sse)]
        absent = {"available": False} if omit_projection else {}
        projection_before = absent or self.digest(surface)
        response: dict = {"ok": True}

        if verdict == "applied":
            if op.kind == "checkpoint":
                cid = f"ckpt-{len(self.checkpoints) + 1}"
                self.checkpoints[cid] = self.snapshot()
                response = {"ok": True, "checkpoint_id": cid}
            elif op.kind == "local_patch":
                updates = dict(op.payload.get("updates") or {})
                for k, v in updates.items():
                    if k not in drop:
                        self.world[surface][k] = v
                response = {"ok": True, "updates": updates}
            elif op.kind == "rollback":
                cid = op.payload.get("target_checkpoint_id", "")
                target = self.checkpoints.get(cid, {})
                for s, kv in target.items():
                    for k, v in kv.items():
                        if k not in self.irreversible and k not in keep:
                            self.world[s][k] = v
                response = {"ok": True, "disposition": "complete",
                            "checkpoint_id": cid}
        else:
            response = {"ok": False}
        if response_patch:
            response.update(response_patch)
        if extra_effect:
            extra_effect(self)

        projection_after = absent or self.digest(surface, lies)
        return OpOutcome(op=op, verdict=verdict, http_status=http_status,
                         response=response, sse_window=sse,
                         projection_before=projection_before,
                         projection_after=projection_after)


def run_program(spec, port, steps, *, ledger_counts=None, env_writes=()):
    """The factory's R1 driver loop in miniature: seed baseline →
    per-op BEFORE/bracket (the recorder is the ONLY diff writer) →
    sealed bundle. ``steps`` is ``[(UserOp, execute-kwargs), ...]``."""
    recorder = EvidenceRecorder(port.snapshot, spec)
    recorder.begin()
    for surf, kv in spec.seed.items():    # the factory's seed annotations
        recorder.note_environment_write(surf, "<seed_directive>",
                                        dict(kv), reason="seed")
    outcomes = []
    for op, knobs in steps:
        before = recorder.before_op()
        outcome = port.execute(op, **(knobs or {}))
        recorder.bracket_user_op(outcome, oracle_before=before)
        outcomes.append(outcome)
    for w in env_writes:
        recorder.note_environment_write(**w)
    counts = {"cua": 4} if ledger_counts is None else ledger_counts
    return recorder.finish(model_ledger_counts=dict(counts)), outcomes


def _port(irreversible=SPEC.irreversibles) -> FakePort:
    return FakePort(SPEC.seed, irreversible=irreversible)


def _happy_steps():
    """checkpoint → publish (the witnessed forward work) → rollback with
    a REAL GUI compensation trajectory (2 GUI actions in the bracket)."""
    return [
        (UserOp.checkpoint("before publish"), {}),
        (UserOp.local_patch({"status": "published", "audit_seq": "4"}), {}),
        (UserOp.rollback("ckpt-1"),
         {"gui_sse": ("action.observed", "action.landed")}),
    ]


# ── pure state helpers ──────────────────────────────────────────────────────

def test_diff_states_changed_added_removed():
    before = {"notes": {"a": "1", "b": "2"}}
    after = {"notes": {"a": "9", "c": "3"}}
    assert diff_states(before, after) == {
        "notes": {
            "a": {"old": "1", "new": "9"},
            "b": {"old": "2", "new": None},
            "c": {"old": None, "new": "3"},
        }}


def test_diff_states_is_type_strict():
    """JSON-scalar coercion must not equate 1 with "1" nor True with
    "true" — a silent type-widening would launder protected diffs."""
    assert diff_states({"s": {"k": 1}}, {"s": {"k": "1"}}) != {}
    assert diff_states({"s": {"k": True}}, {"s": {"k": "true"}}) != {}
    assert diff_states({"s": {"k": 1}}, {"s": {"k": 1}}) == {}


def test_protected_view_restricts_to_spec_pairs():
    diff = {"notes": {"status": {"old": "idle", "new": "published"},
                      "author": {"old": "amy", "new": "mallory"}}}
    assert protected_view(diff, (("notes", "author"),)) == {
        "notes": {"author": {"old": "amy", "new": "mallory"}}}
    assert protected_view(diff, ()) == {}    # nothing protected → held


# ── recorder mechanics ──────────────────────────────────────────────────────

def test_bracket_fills_outcome_diffs_harness_side():
    """The driver-side OpOutcome receives world/protected diffs ONLY
    from the recorder (the harness bracket) — it started as honest
    None and lands as the measured diff."""
    bundle, outcomes = run_program(SPEC, _port(), _happy_steps())
    assert outcomes[1].world_diff == {
        "notes": {"status": {"old": "idle", "new": "published"},
                  "audit_seq": {"old": "3", "new": "4"}}}
    assert outcomes[1].protected_diff == {}     # author untouched
    # the same measurement is what the bundle's bracket carries
    assert bundle.interventions[1].world_diff == outcomes[1].world_diff


def test_gui_actions_counted_from_the_public_sse_window():
    bundle, _ = run_program(SPEC, _port(), _happy_steps())
    rb = bundle.rollback_brackets()[0]
    assert rb.gui_actions == 2      # action.observed + action.landed
    assert bundle.checkpoint_brackets()[0].gui_actions == 0


def test_checkpoint_snapshots_recorded_only_when_applied():
    steps = [(UserOp.checkpoint("cp"),
              {"verdict": "rejected", "http_status": 409})]
    bundle, _ = run_program(SPEC, _port(), steps)
    assert bundle.checkpoint_brackets()[0].status == "rejected"
    assert bundle.checkpoint_snapshots == []    # no fabricated checkpoint


def test_oracle_timeline_is_causal():
    bundle, _ = run_program(SPEC, _port(), _happy_steps())
    labels = [label for label, _ in bundle.oracle_timeline()]
    assert labels[0] == "seed"
    assert labels[-1] == "final"
    assert [l.split(":", 1)[1] for l in labels[1:-1]] == [
        "checkpoint:after", "local_patch:after", "rollback:after"]


def test_injection_notes_are_carried_in_the_bundle():
    recorder = EvidenceRecorder(_port().snapshot, SPEC)
    recorder.begin()
    recorder.note_injection("external_field_change", {"key": "status"})
    bundle = recorder.finish()
    assert bundle.injected_events == [
        {"kind": "external_field_change", "payload": {"key": "status"}}]


def test_world_recorder_seals_the_write_ledger():
    class _Row:
        seq, surface, key = 1, "notes", "status"
        old, new, actor, accepted = "idle", "published", "system", True

    class _World:
        def snapshot(self):
            return {"notes": {"status": "idle"}}

        def write_ledger(self):
            return [_Row()]

    recorder = WorldEvidenceRecorder(_World(), SPEC)
    recorder.begin()
    bundle = recorder.finish()
    assert bundle.write_ledger == [
        {"seq": 1, "surface": "notes", "key": "status", "old": "idle",
         "new": "published", "actor": "system", "accepted": True}]


# ── persistence round-trips ─────────────────────────────────────────────────

def test_bundle_json_roundtrip(tmp_path):
    bundle, _ = run_program(SPEC, _port(), _happy_steps())
    assert bundle.schema_version == EVIDENCE_SCHEMA_VERSION
    path = bundle.dump(str(tmp_path / "evidence.json"))
    loaded = EvidenceBundle.load(path)
    assert loaded.to_json() == bundle.to_json()
    assert [iv.op_id for iv in loaded.interventions] == \
        [iv.op_id for iv in bundle.interventions]


def test_intervention_evidence_from_json_defaults():
    iv = InterventionEvidence.from_json({"op_id": "u-1", "kind": "rollback"})
    assert iv.status == "error" and iv.actor == "user"
    assert iv.gui_actions == 0 and iv.http_status is None


# ── the happy path: all five fields hold ────────────────────────────────────

def test_happy_rollback_trial_passes_all_five_fields():
    bundle, _ = run_program(SPEC, _port(), _happy_steps())
    verdict = grade_task(SPEC, bundle)

    assert verdict.passed is True
    assert verdict.failure_codes == ()
    assert verdict.world_contract["passed"] is True
    assert verdict.governance_contract["passed"] is True
    assert verdict.governance_contract["ops"] == {
        "total": 3, "applied": 3, "rejected": 0, "unsettled": 0,
        "errored": 0, "kinds": ["checkpoint", "local_patch", "rollback"]}
    assert verdict.governance_contract["ledger"]["integrity"] == "ok"
    assert verdict.projection_consistency["passed"] is True
    assert verdict.projection_consistency["status"] == "exact"
    assert verdict.projection_consistency["mismatches"] == []
    assert verdict.progress["passed"] is True
    assert verdict.progress["ops_fraction"] == 1.0
    # the always-on world base group really evaluated
    world_codes = {c["code"] for c in verdict.world_contract["checks"]}
    assert {"WORLD_REQUIRED_WRITE_MISSING", "WORLD_PROTECTED_CHANGED",
            "WORLD_WITNESS_MISSING"} <= world_codes


def test_verdict_shape_is_closed_five_fields():
    bundle, _ = run_program(SPEC, _port(), _happy_steps())
    j = grade_task(SPEC, bundle).to_json()
    assert set(j) == {"world_contract", "governance_contract",
                      "projection_consistency", "progress",
                      "failure_codes", "passed"}
    assert ContractVerdict.from_json(j) == grade_task(SPEC, bundle)


def test_grading_is_pure_over_the_bundle():
    bundle, _ = run_program(SPEC, _port(), _happy_steps())
    snap = json.dumps(bundle.to_json(), sort_keys=True)
    v1 = grade_task(SPEC, bundle)
    v2 = grade_task(SPEC, bundle)
    assert v1 == v2
    assert json.dumps(bundle.to_json(), sort_keys=True) == snap


# ── the no-op loophole (the witness predicate's whole point) ────────────────

def test_noop_rollback_without_forward_work_fails_witness():
    """Final state equals the seed — a system that NEVER did the
    forward work must not be credited just because the end state
    matches: the witness values never appeared on the timeline."""
    port = _port()
    steps = [(UserOp.checkpoint("cp"), {}),
             (UserOp.rollback("ckpt-1"), {"gui_sse": ("action.landed",)})]
    bundle, _ = run_program(SPEC, port, steps)
    verdict = grade_task(SPEC, bundle)
    assert verdict.failure_codes == ("WORLD_WITNESS_MISSING",)
    assert verdict.world_contract["passed"] is False


def test_required_write_missing_at_trial_end():
    spec = replace(SPEC, success={"notes": {"status": "archived"}})
    bundle, _ = run_program(spec, _port(), _happy_steps())
    verdict = grade_task(spec, bundle)
    assert verdict.failure_codes == ("WORLD_REQUIRED_WRITE_MISSING",)


def test_protected_field_violation_is_caught_with_bracket_diff():
    """A mid-run interference that SURVIVES to trial end fails the
    protected predicate — and the local-patch bracket itself recorded
    the non-interference observable (protected_diff)."""
    port = _port()
    steps = [
        (UserOp.checkpoint("cp"), {}),
        (UserOp.local_patch({"status": "published", "author": "mallory",
                             "audit_seq": "4"}), {}),
        (UserOp.rollback("ckpt-1"),
         {"gui_sse": ("action.landed",), "keep": ("author",)}),
    ]
    bundle, outcomes = run_program(SPEC, port, steps)
    verdict = grade_task(SPEC, bundle)
    assert "WORLD_PROTECTED_CHANGED" in verdict.failure_codes
    assert "ROLLBACK_NOT_RESTORED" in verdict.failure_codes   # author left
    # the per-bracket observable: the patch DID touch the protected key
    assert outcomes[1].protected_diff == {
        "notes": {"author": {"old": "amy", "new": "mallory"}}}
    assert bundle.interventions[1].protected_diff == \
        outcomes[1].protected_diff


# ── the rollback predicate families ─────────────────────────────────────────

def test_partial_restore_is_not_restored():
    port = _port()
    steps = [
        (UserOp.checkpoint("cp"), {}),
        (UserOp.local_patch({"title": "v2", "status": "published",
                             "audit_seq": "4"}), {}),
        (UserOp.rollback("ckpt-1"),
         {"gui_sse": ("action.landed",), "keep": ("title",)}),
    ]
    bundle, _ = run_program(SPEC, port, steps)
    verdict = grade_task(SPEC, bundle)
    assert "ROLLBACK_NOT_RESTORED" in verdict.failure_codes
    # the left-behind title also breaks the terminal contract — honest
    # compound failure, both codes surface
    assert "WORLD_REQUIRED_WRITE_MISSING" in verdict.failure_codes


def test_silent_irreversible_undo_is_caught():
    """The dishonest port that ALSO reverts the audit trail inside the
    rollback window — exactly the hidden undo the irreversible
    predicate exists to close (nothing else fails)."""
    port = _port(irreversible=())        # port "helpfully" restores all
    bundle, _ = run_program(SPEC, port, _happy_steps())
    verdict = grade_task(SPEC, bundle)
    assert verdict.failure_codes == ("ROLLBACK_IRREVERSIBLE_TOUCHED",)


def test_rollback_without_gui_compensation_is_caught():
    """There was work to undo and ZERO GUI actions inside the bracket —
    the restore did not go through the real GUI (a bare world write)."""
    port = _port()
    steps = [
        (UserOp.checkpoint("cp"), {}),
        (UserOp.local_patch({"status": "published", "audit_seq": "4"}), {}),
        (UserOp.rollback("ckpt-1"), {}),          # no GUI trajectory
    ]
    bundle, _ = run_program(SPEC, port, steps)
    verdict = grade_task(SPEC, bundle)
    assert verdict.failure_codes == ("ROLLBACK_NO_GUI_COMPENSATION",)


def test_rollback_compensation_entry_trace_counts_as_real_gui():
    """GATE-G0 r11 (2026-08-20): the CompensationExecutor (runtime.md
    §7, FROZEN) publishes one ``compensation.entry`` frame per plan
    entry and NEVER the forward loop's per-gesture markers — its only
    world-write primitive is ``substrate.act``, so its public per-entry
    trace IS the real-GUI trajectory. A rollback bracket carrying the
    entry trace (and zero ``action.*`` markers, gui_actions==0) must
    NOT fail ROLLBACK_NO_GUI_COMPENSATION — and nothing else may fail
    either (the exact r11 shape: world restored, disposition complete,
    no hidden write)."""
    port = _port()
    steps = [
        (UserOp.checkpoint("cp"), {}),
        (UserOp.local_patch({"status": "published", "audit_seq": "4"}), {}),
        (UserOp.rollback("ckpt-1"),
         {"gui_sse": ("compensation.entry", "compensation.entry")}),
    ]
    bundle, _ = run_program(SPEC, port, steps)
    verdict = grade_task(SPEC, bundle)
    assert verdict.failure_codes == ()


def test_hidden_eval_restore_after_rollback_is_caught():
    """The eval plane itself re-wrote a value after the rollback request
    — only the system may move the world inside that window."""
    port = _port()
    steps = _happy_steps()
    env = [dict(surface="notes", key="status", value="idle",
                reason="restore", after_op=steps[-1][0].op_id)]
    bundle, _ = run_program(SPEC, port, steps, env_writes=env)
    verdict = grade_task(SPEC, bundle)
    assert "ROLLBACK_HIDDEN_RESTORE" in verdict.failure_codes


def test_rollback_without_checkpoint_is_caught():
    port = _port()
    steps = [
        (UserOp.local_patch({"status": "published", "audit_seq": "4"}), {}),
        (UserOp.rollback("ckpt-missing"), {"gui_sse": ("action.landed",)}),
    ]
    bundle, _ = run_program(SPEC, port, steps)
    verdict = grade_task(SPEC, bundle)
    assert "ROLLBACK_NO_CHECKPOINT" in verdict.failure_codes


def test_unsettled_rollback_fails_rollback_governance_and_progress():
    port = _port()
    steps = [
        (UserOp.checkpoint("cp"), {}),
        (UserOp.local_patch({"status": "published", "audit_seq": "4"}), {}),
        (UserOp.rollback("ckpt-1"), {"verdict": "unsettled"}),
    ]
    bundle, _ = run_program(SPEC, port, steps)
    verdict = grade_task(SPEC, bundle)
    for code in ("ROLLBACK_NOT_APPLIED", "GOVERNANCE_OP_UNSETTLED",
                 "PROGRESS_INCOMPLETE"):
        assert code in verdict.failure_codes
    assert verdict.governance_contract["ops"]["unsettled"] == 1


def test_disposition_incomplete_is_caught():
    port = _port()
    steps = [
        (UserOp.checkpoint("cp"), {}),
        (UserOp.local_patch({"status": "published", "audit_seq": "4"}), {}),
        (UserOp.rollback("ckpt-1"),
         {"gui_sse": ("action.landed",),
          "response_patch": {"disposition": "partial"}}),
    ]
    bundle, _ = run_program(SPEC, port, steps)
    verdict = grade_task(SPEC, bundle)
    assert verdict.failure_codes == ("ROLLBACK_DISPOSITION_INCOMPLETE",)


# ── the local-patch predicate families ──────────────────────────────────────

def test_patch_the_world_never_absorbed_is_caught():
    """The patch was accepted (its response even echoes the updates)
    but one key never landed in the world — a governance lie."""
    port = _port()
    steps = [
        (UserOp.checkpoint("cp"), {}),
        (UserOp.local_patch({"status": "published", "audit_seq": "4",
                             "priority": "high"}), {"drop": ("priority",)}),
        (UserOp.rollback("ckpt-1"),
         {"gui_sse": ("action.landed",)}),
    ]
    bundle, _ = run_program(SPEC, port, steps)
    verdict = grade_task(SPEC, bundle)
    assert verdict.failure_codes == ("LOCAL_PATCH_KEY_MISSING",)


def test_rejected_op_is_honest_governance_failure():
    port = _port()
    steps = [
        (UserOp.checkpoint("cp"), {}),
        (UserOp.local_patch({"status": "published", "audit_seq": "4"}),
         {"verdict": "rejected", "http_status": 409}),
        (UserOp.rollback("ckpt-1"), {"gui_sse": ("action.landed",)}),
    ]
    bundle, _ = run_program(SPEC, port, steps)
    verdict = grade_task(SPEC, bundle)
    for code in ("LOCAL_PATCH_NOT_APPLIED", "GOVERNANCE_OP_REJECTED",
                 "PROGRESS_INCOMPLETE", "WORLD_WITNESS_MISSING"):
        assert code in verdict.failure_codes
    assert verdict.governance_contract["ops"]["rejected"] == 1


# ── projection consistency ──────────────────────────────────────────────────

def test_projection_lie_is_caught():
    """The public snapshot claims a value the world nowhere holds at
    that moment — the projection is lying about the hidden state."""
    port = _port()
    steps = [
        (UserOp.checkpoint("cp"), {}),
        (UserOp.local_patch({"status": "published", "audit_seq": "4"}),
         {"lies": {"status": "deleted"}}),
        (UserOp.rollback("ckpt-1"),
         {"gui_sse": ("action.landed",)}),
    ]
    bundle, _ = run_program(SPEC, port, steps)
    verdict = grade_task(SPEC, bundle)
    assert verdict.failure_codes == ("PROJECTION_MISMATCH",)
    mism = verdict.projection_consistency["mismatches"]
    assert mism[0]["key"] == "status"
    assert mism[0]["claimed"] == "deleted"


def test_projection_absence_is_not_a_pass():
    """No projection evidence collected for any bracket (the driver's
    honest ``available: False`` markers) — an unverified dimension is
    NOT a passed dimension."""
    port = _port()
    steps = [(op, {**knobs, "omit_projection": True})
             for op, knobs in _happy_steps()]
    bundle, _ = run_program(SPEC, port, steps)
    verdict = grade_task(SPEC, bundle)
    assert verdict.projection_consistency["status"] == "unavailable"
    assert verdict.projection_consistency["passed"] is False
    assert verdict.failure_codes == ("PROJECTION_UNAVAILABLE",)


# ── ledger integrity + progress ─────────────────────────────────────────────

def test_ledger_integrity_broken_when_gui_without_telemetry():
    """GUI actions were observed but NO role shows a ledger row — the
    trajectory cannot be attributed to real model calls."""
    bundle, _ = run_program(SPEC, _port(), _happy_steps(),
                            ledger_counts={})
    verdict = grade_task(SPEC, bundle)
    assert "LEDGER_INTEGRITY_BROKEN" in verdict.failure_codes
    assert verdict.governance_contract["ledger"]["integrity"] == "broken"


def test_zero_ops_is_progress_incomplete_and_unverifiable():
    bundle, _ = run_program(SPEC, _port(), [])
    verdict = grade_task(SPEC, bundle)
    assert verdict.progress["ops_total"] == 0
    for code in ("PROGRESS_INCOMPLETE", "PROJECTION_UNAVAILABLE",
                 "WORLD_WITNESS_MISSING"):
        assert code in verdict.failure_codes


def test_every_emittable_code_is_in_the_closed_vocabulary():
    """Whatever the scenario, the grader can never emit a code outside
    the frozen vocabulary (spot-check via the nastiest compound case:
    an unsettled partial rollback that also lies in its projection)."""
    port = _port(irreversible=())
    steps = [
        (UserOp.checkpoint("cp"), {}),
        (UserOp.local_patch({"title": "v2", "status": "published",
                             "audit_seq": "4"}),
         {"lies": {"status": "deleted"}}),
        (UserOp.rollback("ckpt-1"),
         {"verdict": "unsettled", "keep": ("title",)}),
    ]
    bundle, _ = run_program(SPEC, port, steps, ledger_counts={})
    verdict = grade_task(SPEC, bundle)
    assert len(verdict.failure_codes) >= 4
    assert all(code in FAILURE_CODES for code in verdict.failure_codes)


# ── finalize: the graded verdict is the ONLY path to pass ───────────────────

def test_graded_trial_record_finalizes_to_pass():
    bundle, outcomes = run_program(SPEC, _port(), _happy_steps())
    verdict = grade_task(SPEC, bundle)
    record = TrialRecord(model="fake-model", substrate="fake-port")
    for oc in outcomes:
        record.add_op(UserOpRecord(**oc.to_record()))
    record.contract_verdict = verdict.to_json()
    record.finalize()
    assert record.trial_verdict == "pass"
    assert record.failure_class == ""
    # the persisted per-op records carry the harness-filled diffs and
    # the rollback's public disposition
    assert record.user_ops[0]["world_diff"] == {}
    assert record.user_ops[1]["world_diff"] == {
        "notes": {"status": {"old": "idle", "new": "published"},
                  "audit_seq": {"old": "3", "new": "4"}}}
    assert record.user_ops[2]["rollback"]["disposition"] == "complete"


def test_failed_grade_finalizes_to_fail_with_contract_violation():
    port = _port()
    bundle, outcomes = run_program(SPEC, port, [
        (UserOp.checkpoint("cp"), {}),
        (UserOp.rollback("ckpt-1"), {"gui_sse": ("action.landed",)}),
    ])
    verdict = grade_task(SPEC, bundle)
    record = TrialRecord(model="fake-model", substrate="fake-port")
    for oc in outcomes:
        record.add_op(UserOpRecord(**oc.to_record()))
    record.contract_verdict = verdict.to_json()
    record.finalize()
    assert record.trial_verdict == "fail"
    assert record.failure_class == "contract-violation"
    # honest persistence: the five-field verdict travels with the record
    assert record.contract_verdict["failure_codes"] == \
        ["WORLD_WITNESS_MISSING"]


def test_evaluation_error_wins_over_the_grade():
    """A trial the grading plane refused (integrity violation) is an
    ERROR even if a verdict had landed — never a graded pass."""
    record = TrialRecord()
    record.add_op(UserOpRecord(op_id="u-1", kind="stop",
                               verdict="applied"))
    record.contract_verdict = {"passed": True, "failure_codes": []}
    record.evaluation_error = "post-trial integrity: state mismatch"
    record.finalize()
    assert record.trial_verdict == "error"
