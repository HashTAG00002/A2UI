"""taskvm_bench.evaluation.evidence — the EvidenceBundle (R1 grader loop).

The deterministic grader (:func:`taskvm_bench.evaluation.grader.grade_task`)
consumes EXACTLY this bundle and nothing else. Everything in it is
collected from signals that ALREADY exist — the eval plane's own powers
(``BenchmarkWorld`` snapshots / the MobileGym oracle HTTP reads), the
projection PUBLIC HTTP API, the user-op driver's per-op records, the
harness trace and the write ledger. NO new prototype-only hidden API is
introduced anywhere (the R1 work-order rule, verbatim).

Bundle contents (R1 work order, verbatim):

* ``oracle_seed``            — the hidden world state the trial started from;
* ``interventions``          — per user-op / injection brackets: the oracle
                               snapshot BEFORE and AFTER each intervention,
                               the public projection digests around it, the
                               SSE window, HTTP status/response, and the
                               derived world/protected diffs;
* ``oracle_final``           — the hidden world state at trial end;
* ``projection_snapshots``   — public projection digests over time;
* ``runtime_trace``          — the harness/runtime trace events;
* ``write_ledger``           — the world write ledger (actor-attributed);
* ``checkpoint_snapshots``   — checkpoint records (public op responses);
* ``injected_events``        — the deterministic injections that fired;
* ``environment_writes``     — writes the EVAL plane itself performed
                               (seed / injections) — the no-hidden-restore
                               check needs to prove this list is empty
                               inside the rollback window;
* ``model_ledger_counts``    — per-role model-call counts (ledger integrity).

Substrate independence: the oracle states are NORMALIZED to
``{surface: {key: scalar_or_json_value}}`` — the builtin world gives
``{surface: {key: value}}`` natively; the MobileGym adapter flattens app
entities to ``"<entity_id>.<field>"`` keys. The grader therefore reads
ONE shape regardless of what the task ran on.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "EVIDENCE_SCHEMA_VERSION", "InterventionEvidence", "EvidenceBundle",
    "diff_states", "protected_view", "EvidenceRecorder",
    "WorldEvidenceRecorder",
]

EVIDENCE_SCHEMA_VERSION = "taskvm-evidence-1"

#: intervention ``status`` vocabulary (mirrors user-op verdicts; injections
#: carry the fixed status "injected").
INTERVENTION_STATUSES = ("applied", "rejected", "unsettled", "error",
                         "injected")


# ── pure state helpers ─────────────────────────────────────────────────────

def _norm_state(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """{surface: {key: value}} with JSON-scalar values coerced to a stable
    comparable form (bools stay bools, everything else goes through JSON so
    ``1`` and ``"1"`` never compare equal by accident)."""
    out: dict[str, dict[str, Any]] = {}
    for surf, kv in (state or {}).items():
        if not isinstance(kv, Mapping):
            continue
        out[str(surf)] = {str(k): _norm_value(v) for k, v in kv.items()}
    return out


def _norm_value(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return json.loads(json.dumps(v, sort_keys=True, ensure_ascii=False,
                                 default=str))


def diff_states(before: Mapping[str, Mapping[str, Any]],
                after: Mapping[str, Mapping[str, Any]]) -> dict[str, dict]:
    """The changed-keys diff between two normalized oracle states:
    ``{surface: {key: {"old": x, "new": y}}}`` — only keys that actually
    changed (added / removed / re-valued) appear."""
    b, a = _norm_state(before), _norm_state(after)
    out: dict[str, dict] = {}
    for surf in sorted(set(b) | set(a)):
        keys = sorted(set(b.get(surf, {})) | set(a.get(surf, {})))
        rows = {}
        for k in keys:
            old = b.get(surf, {}).get(k)
            new = a.get(surf, {}).get(k)
            if old != new:
                rows[k] = {"old": old, "new": new}
        if rows:
            out[surf] = rows
    return out


def protected_view(diff: Mapping[str, Mapping[str, Any]],
                   protected: tuple[tuple[str, str], ...]) -> dict[str, dict]:
    """Restrict a diff to the spec's protected ``(surface, key)`` pairs.
    Empty dict == non-interference held (nothing protected changed)."""
    want = {(s, k) for s, k in protected}
    out: dict[str, dict] = {}
    for surf, rows in (diff or {}).items():
        kept = {k: v for k, v in rows.items() if (surf, k) in want}
        if kept:
            out[surf] = kept
    return out


# ── the per-intervention bracket ───────────────────────────────────────────

@dataclass
class InterventionEvidence:
    """One user op (or injected event) with its BEFORE/AFTER brackets.

    ``status``          — the op verdict (applied/rejected/unsettled/error)
                          or "injected" for eval-plane injections;
    ``oracle_before/after`` — normalized hidden state on each side;
    ``world_diff``      — ``diff_states(before, after)`` (changed keys);
    ``protected_diff``  — the same diff restricted to the spec's protected
                          set (the non-interference observable);
    ``projection_before/after`` — PUBLIC projection digests on each side
                          (the UserOpDriver's own captures);
    ``sse_window``      — the public SSE frames observed during the op;
    ``http_status``/``response`` — the op's public HTTP outcome;
    ``gui_actions``     — GUI actions observed inside this bracket (the
                          real-GUI-compensation observable);
    ``actor``           — "user" for user ops, "environment" for injections
                          (who initiated the intervention).
    """

    op_id: str
    kind: str
    status: str
    actor: str = "user"
    oracle_before: dict = field(default_factory=dict)
    oracle_after: dict = field(default_factory=dict)
    world_diff: dict = field(default_factory=dict)
    protected_diff: dict = field(default_factory=dict)
    projection_before: dict = field(default_factory=dict)
    projection_after: dict = field(default_factory=dict)
    sse_window: list = field(default_factory=list)
    http_status: int | None = None
    response: dict = field(default_factory=dict)
    gui_actions: int = 0

    def to_json(self) -> dict[str, Any]:
        return dict(
            op_id=self.op_id, kind=self.kind, status=self.status,
            actor=self.actor,
            oracle_before=self.oracle_before, oracle_after=self.oracle_after,
            world_diff=self.world_diff, protected_diff=self.protected_diff,
            projection_before=self.projection_before,
            projection_after=self.projection_after,
            sse_window=self.sse_window, http_status=self.http_status,
            response=self.response, gui_actions=self.gui_actions,
        )

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "InterventionEvidence":
        return cls(
            op_id=str(d["op_id"]), kind=str(d["kind"]),
            status=str(d.get("status", "error")),
            actor=str(d.get("actor", "user")),
            oracle_before=dict(d.get("oracle_before") or {}),
            oracle_after=dict(d.get("oracle_after") or {}),
            world_diff=dict(d.get("world_diff") or {}),
            protected_diff=dict(d.get("protected_diff") or {}),
            projection_before=dict(d.get("projection_before") or {}),
            projection_after=dict(d.get("projection_after") or {}),
            sse_window=list(d.get("sse_window") or []),
            http_status=d.get("http_status"),
            response=dict(d.get("response") or {}),
            gui_actions=int(d.get("gui_actions", 0)),
        )


# ── the bundle ─────────────────────────────────────────────────────────────

@dataclass
class EvidenceBundle:
    """Everything the grader may consume for ONE trial."""

    task_id: str
    oracle_seed: dict = field(default_factory=dict)
    interventions: list[InterventionEvidence] = field(default_factory=list)
    oracle_final: dict = field(default_factory=dict)
    projection_snapshots: list[dict] = field(default_factory=list)
    runtime_trace: list[dict] = field(default_factory=list)
    write_ledger: list[dict] = field(default_factory=list)
    checkpoint_snapshots: list[dict] = field(default_factory=list)
    injected_events: list[dict] = field(default_factory=list)
    environment_writes: list[dict] = field(default_factory=list)
    model_ledger_counts: dict = field(default_factory=dict)
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    # ── timeline views the grader uses ─────────────────────────────────
    def oracle_timeline(self) -> list[tuple[str, dict]]:
        """``[(label, state)]`` in causal order: seed → each intervention's
        after-state → final. The witness predicate scans this for values
        that HELD at some point even when the final state moved on."""
        out: list[tuple[str, dict]] = [("seed", _norm_state(self.oracle_seed))]
        for iv in self.interventions:
            out.append((f"{iv.op_id}:{iv.kind}:after", iv.oracle_after))
        if self.oracle_final:
            out.append(("final", _norm_state(self.oracle_final)))
        return out

    def rollback_brackets(self) -> list[InterventionEvidence]:
        """The rollback interventions, in execution order."""
        return [iv for iv in self.interventions
                if iv.kind in ("rollback", "rollback_request")]

    def checkpoint_brackets(self) -> list[InterventionEvidence]:
        return [iv for iv in self.interventions if iv.kind == "checkpoint"]

    # ── persistence ─────────────────────────────────────────────────────
    def to_json(self) -> dict[str, Any]:
        return dict(
            schema_version=self.schema_version,
            task_id=self.task_id,
            oracle_seed=self.oracle_seed,
            interventions=[iv.to_json() for iv in self.interventions],
            oracle_final=self.oracle_final,
            projection_snapshots=self.projection_snapshots,
            runtime_trace=self.runtime_trace,
            write_ledger=self.write_ledger,
            checkpoint_snapshots=self.checkpoint_snapshots,
            injected_events=self.injected_events,
            environment_writes=self.environment_writes,
            model_ledger_counts=self.model_ledger_counts,
        )

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "EvidenceBundle":
        return cls(
            task_id=str(d["task_id"]),
            oracle_seed=dict(d.get("oracle_seed") or {}),
            interventions=[InterventionEvidence.from_json(iv)
                           for iv in d.get("interventions") or []],
            oracle_final=dict(d.get("oracle_final") or {}),
            projection_snapshots=list(d.get("projection_snapshots") or []),
            runtime_trace=list(d.get("runtime_trace") or []),
            write_ledger=list(d.get("write_ledger") or []),
            checkpoint_snapshots=list(d.get("checkpoint_snapshots") or []),
            injected_events=list(d.get("injected_events") or []),
            environment_writes=list(d.get("environment_writes") or []),
            model_ledger_counts=dict(d.get("model_ledger_counts") or {}),
            schema_version=str(d.get("schema_version",
                                     EVIDENCE_SCHEMA_VERSION)),
        )

    def dump(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_json(), fh, indent=1, sort_keys=True,
                      ensure_ascii=False)
        return path

    @classmethod
    def load(cls, path: str) -> "EvidenceBundle":
        with open(path, encoding="utf-8") as fh:
            return cls.from_json(json.load(fh))


# ── the recorders ──────────────────────────────────────────────────────────

class EvidenceRecorder:
    """Brackets user ops with oracle snapshots read through an injected
    read callable — the ONE shape every substrate needs to produce a
    gradable bundle. The callable must already exist (a world method, an
    oracle HTTP read): the recorder introduces NO new read path.

    The eval plane (whoever drives the trial) holds the oracle power;
    this recorder is the ONLY writer of ``world_diff`` /
    ``protected_diff`` — the driver itself never sees the oracle (the
    B-04 iron rule).
    """

    def __init__(self, read_oracle, spec) -> None:
        #: callable() -> {surface: {key: value}} (normalized inside)
        self._read = read_oracle
        self._spec = spec
        self._seed_snapshot: dict = {}
        self._interventions: list[InterventionEvidence] = []
        self._env_writes: list[dict] = []
        self._checkpoints: list[dict] = []
        self._injected: list[dict] = []
        self._last_op_id: str | None = None

    @property
    def task_spec(self):
        return self._spec

    def read(self) -> dict:
        """One normalized oracle read (``{surface: {key: value}}``)."""
        return _norm_state(self._read())

    def begin(self) -> dict:
        """Capture the seed snapshot (call after world construction /
        seeding, BEFORE any user op runs)."""
        self._seed_snapshot = self.read()
        return self._seed_snapshot

    def note_environment_write(self, surface: str, key: str, value: Any,
                               reason: str, *, after_op: str | None = None,
                               ) -> None:
        """Record a write the EVAL plane itself performed (seed /
        injection). ``after_op`` anchors the write in the op timeline
        (None / "setup" = during setup) — the no-hidden-restore predicate
        needs this list to be provably empty inside a rollback window."""
        self._env_writes.append(dict(surface=surface, key=key, value=value,
                                     reason=reason,
                                     after_op=after_op or "setup"))

    def note_injection(self, kind: str, payload: dict) -> None:
        self._injected.append(dict(kind=kind, payload=dict(payload or {})))

    def before_op(self) -> dict:
        """The oracle snapshot to hand to :meth:`bracket_user_op` (taken
        by the driver loop right BEFORE issuing the op)."""
        return self.read()

    def bracket_user_op(self, outcome, *, oracle_before: dict | None = None,
                        gui_actions: int | None = None,
                        ) -> InterventionEvidence:
        """Build the intervention bracket for ONE executed user op.

        ``outcome`` is a ``UserOpDriver`` :class:`OpOutcome`. The caller
        supplies ``oracle_before`` when it took the pre-op snapshot itself
        (otherwise the CURRENT state is used — correct when the op just
        settled and nothing else moved).
        """
        before = _norm_state(oracle_before if oracle_before is not None
                             else self.read())
        after = self.read()
        wdiff = diff_states(before, after)
        iv = InterventionEvidence(
            op_id=outcome.op.op_id, kind=outcome.op.kind,
            status=outcome.verdict, actor="user",
            oracle_before=before, oracle_after=after,
            world_diff=wdiff,
            protected_diff=protected_view(wdiff, self._spec.protected),
            projection_before=dict(outcome.projection_before),
            projection_after=dict(outcome.projection_after),
            sse_window=list(outcome.sse_window),
            http_status=outcome.http_status,
            response=dict(outcome.response),
            gui_actions=(gui_actions if gui_actions is not None
                         else _count_gui_sse(outcome.sse_window)),
        )
        self._interventions.append(iv)
        self._last_op_id = iv.op_id
        # the harness-side fill of the per-op record fields (the driver
        # itself can never fabricate these)
        outcome.world_diff = iv.world_diff
        outcome.protected_diff = iv.protected_diff
        if iv.kind == "checkpoint" and iv.status == "applied":
            self._checkpoints.append(dict(
                op_id=iv.op_id, kind="checkpoint",
                response=iv.response, oracle_at_checkpoint=iv.oracle_after))
        return iv

    def finish(self, runtime_trace: list[dict] | None = None,
               projection_snapshots: list[dict] | None = None,
               model_ledger_counts: dict | None = None,
               write_ledger: list[dict] | None = None,
               ) -> EvidenceBundle:
        """Seal the bundle (call at trial end, after the final oracle read).

        ``write_ledger`` carries the actor-attributed write log when the
        substrate exposes one (the builtin world does); substrates
        without a ledger pass None and the bundle honestly stays empty —
        actor attribution then rests on the bracket diffs alone."""
        return EvidenceBundle(
            task_id=self._spec.task_id,
            oracle_seed=self._seed_snapshot,
            interventions=list(self._interventions),
            oracle_final=self.read(),
            projection_snapshots=list(projection_snapshots or []),
            runtime_trace=list(runtime_trace or []),
            write_ledger=list(write_ledger or []),
            checkpoint_snapshots=list(self._checkpoints),
            injected_events=list(self._injected),
            environment_writes=list(self._env_writes),
            model_ledger_counts=dict(model_ledger_counts or {}),
        )


class WorldEvidenceRecorder(EvidenceRecorder):
    """The builtin-world flavour: oracle reads are the
    :class:`~taskvm_bench.evaluation.world.BenchmarkWorld` snapshots and
    the write ledger is the world's append-only log."""

    def __init__(self, world, spec) -> None:
        super().__init__(world.snapshot, spec)
        self._world = world

    def finish(self, runtime_trace: list[dict] | None = None,
               projection_snapshots: list[dict] | None = None,
               model_ledger_counts: dict | None = None,
               write_ledger: list[dict] | None = None,
               ) -> EvidenceBundle:
        writes = write_ledger
        if writes is None:
            writes = [dict(seq=w.seq, surface=w.surface, key=w.key,
                           old=w.old, new=w.new, actor=w.actor,
                           accepted=w.accepted)
                      for w in self._world.write_ledger()]
        return super().finish(
            runtime_trace=runtime_trace,
            projection_snapshots=projection_snapshots,
            model_ledger_counts=model_ledger_counts,
            write_ledger=writes)


def _count_gui_sse(sse_window: list) -> int:
    """GUI actions observable in a public SSE window (the user-op timeline
    markers — ``action.observed`` / ``action.landed``)."""
    n = 0
    for env in sse_window or []:
        if isinstance(env, Mapping) and env.get("sse_type") in (
                "action.observed", "action.landed"):
            n += 1
    return n
