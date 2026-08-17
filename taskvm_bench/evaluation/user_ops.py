"""B-04 — ``UserOp`` + ``UserOpDriver``: the RM evaluation's minimal
verdict unit, driven EXCLUSIVELY through the projection public API.

A user operation ("user op") is ONE act of human governance — start /
pause / resume / stop / local_patch / goal_patch / checkpoint / rollback —
issued the way a real user would issue it (an HTTP call to the projection
route) and settled only on PUBLIC signals.

Iron rules (RM-0 work order §B-04, verbatim) — the driver:

* only calls Projection public HTTP/API (via ``ProjectionClient``);
* holds NO Kernel, NO Runtime, NO CUAModel;
* never calls ``GovernanceService.handle``;
* never executes a substrate action directly;
* never constructs agent trajectory;
* never reads a hidden oracle to pick the next user move.

Per-op barrier: settle uses ONLY existing public signals — the HTTP
command's own return, the registered ``governance.applied`` SSE ack,
runtime/kernel SSE frames, the projection events page (``/events`` total)
and snapshot revisions. NO prototype-only hidden ``/test/accepted(op_id)``
API exists or is used; op correlation is kept client-side
(``op_id ↔ request/response/SSE window`` via the client's request_log and
the SSE window opened around the op).
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from taskvm_bench.evaluation.projection_client import (
    SSE_GOVERNANCE_APPLIED, SSE_PROGRESS_TYPES, ProjectionClient, SSEWindow,
)

USER_OP_KINDS = (
    "start", "pause", "resume", "stop",
    "local_patch", "goal_patch", "checkpoint", "rollback",
)

_op_counter = itertools.count(1)


def next_op_id() -> str:
    return f"uop-{next(_op_counter):04d}"


# ── settle policy ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SettlePolicy:
    """How the barrier decides an op has genuinely settled.

    ``http``  — the command's HTTP return IS the settle signal (the
                route executed synchronously; nothing further is owed).
    ``sse``   — wait until the SSE window carries ``sse_type`` (default
                the registered ``governance.applied`` ack) OR the events
                page total grows past its pre-op value, whichever first.
    ``quiet`` — wait until the world stops moving: no new progress-type
                SSE frame AND no events-page growth for ``quiet_seconds``
                (used for ops that legitimately kick off work, e.g.
                ``start`` / ``resume`` / ``goal_patch`` recompose).
    """
    mode: str                       # "http" | "sse" | "quiet"
    sse_type: str = SSE_GOVERNANCE_APPLIED
    quiet_seconds: float = 0.75
    timeout_s: float = 15.0


#: per-kind defaults (overridable per op)
DEFAULT_SETTLE: dict = {
    "pause":       SettlePolicy("sse"),
    "resume":      SettlePolicy("quiet"),
    "stop":        SettlePolicy("sse"),
    "checkpoint":  SettlePolicy("sse"),
    "rollback":    SettlePolicy("sse"),
    "local_patch": SettlePolicy("sse"),
    "goal_patch":  SettlePolicy("sse"),
    "start":       SettlePolicy("quiet", quiet_seconds=1.0),
}


# ── the user operation ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class UserOp:
    op_id: str
    kind: str                       # one of USER_OP_KINDS
    payload: dict = field(default_factory=dict)
    expected_http_class: str = "2xx"    # "2xx" | "4xx" | "any"
    settle_policy: Optional[SettlePolicy] = None   # None → per-kind default

    def __post_init__(self) -> None:
        if self.kind not in USER_OP_KINDS:
            raise ValueError(f"unknown user-op kind {self.kind!r}")

    @property
    def policy(self) -> SettlePolicy:
        return self.settle_policy or DEFAULT_SETTLE[self.kind]

    # ── constructors (payload shapes mirror the public routes) ─────────
    @classmethod
    def start(cls, **kw) -> "UserOp":
        return cls(next_op_id(), "start", {}, **kw)

    @classmethod
    def pause(cls, rationale: str = "", **kw) -> "UserOp":
        return cls(next_op_id(), "pause", {"rationale": rationale}, **kw)

    @classmethod
    def resume(cls, rationale: str = "", **kw) -> "UserOp":
        return cls(next_op_id(), "resume", {"rationale": rationale}, **kw)

    @classmethod
    def stop(cls, rationale: str = "", **kw) -> "UserOp":
        return cls(next_op_id(), "stop", {"rationale": rationale}, **kw)

    @classmethod
    def local_patch(cls, updates: dict, rationale: str = "", **kw) -> "UserOp":
        return cls(next_op_id(), "local_patch",
                   {"updates": dict(updates), "rationale": rationale}, **kw)

    @classmethod
    def goal_patch(cls, goal: str, *, constraints=(), scope=(),
                   success_criteria=(), rationale: str = "", **kw) -> "UserOp":
        return cls(next_op_id(), "goal_patch", {
            "goal": goal, "constraints": list(constraints),
            "scope": list(scope),
            "success_criteria": list(success_criteria),
            "rationale": rationale}, **kw)

    @classmethod
    def checkpoint(cls, label: str, **kw) -> "UserOp":
        return cls(next_op_id(), "checkpoint", {"label": label}, **kw)

    @classmethod
    def rollback(cls, target_checkpoint_id: str,
                 rationale: str = "", **kw) -> "UserOp":
        return cls(next_op_id(), "rollback", {
            "target_checkpoint_id": target_checkpoint_id,
            "rationale": rationale}, **kw)


# ── per-op timeline + outcome ──────────────────────────────────────────────

TIMELINE_KEYS = (
    "op_issued", "http_accepted", "first_gui_action", "last_gui_action",
    "verifier_completed", "first_correct_projection", "settled",
)


@dataclass
class OpOutcome:
    op: UserOp
    verdict: str                     # applied | rejected | unsettled | error
    http_status: Optional[int] = None
    response: dict = field(default_factory=dict)
    timeline: dict = field(default_factory=lambda: dict.fromkeys(TIMELINE_KEYS))
    sse_window: list = field(default_factory=list)
    projection_before: dict = field(default_factory=dict)
    projection_after: dict = field(default_factory=dict)
    detail: str = ""

    def to_record(self) -> dict:
        """The B-05 persisted per-op record (world_diff/protected_diff and
        ledger_request_ids are filled by the harness layer — honest
        missing here, never fabricated)."""
        return dict(
            op_id=self.op.op_id, kind=self.op.kind, verdict=self.verdict,
            world_diff=None, protected_diff=None,
            projection=dict(before=self.projection_before,
                            after=self.projection_after),
            rollback=(self.response if self.op.kind == "rollback" else None),
            ledger_request_ids=None,
            timeline={k: self.timeline.get(k) for k in TIMELINE_KEYS},
            artifacts=[],
            http_status=self.http_status, response=self.response,
            detail=self.detail,
        )


#: runtime SSE frames that count as GUI actions for the timeline
_GUI_SSE_TYPES = ("action.observed", "action.landed")
_VERIFIER_MARKERS = ("verif", "state.updated", "check")


def _projection_digest(snapshot: dict) -> dict:
    """Small, stable, PUBLIC summary of a snapshot for per-op records."""
    out: dict = {}
    if not isinstance(snapshot, dict):
        return out
    for key in ("revision", "status", "governance", "task_status"):
        if key in snapshot:
            out[key] = snapshot[key]
    variables = snapshot.get("variables")
    if isinstance(variables, dict):
        out["variables"] = {
            k: (v.get("desired"), v.get("observed"))
            if isinstance(v, dict) else v
            for k, v in list(variables.items())[:64]}
    elif isinstance(variables, list):
        out["variables"] = [str(v)[:80] for v in variables[:64]]
    return out


# ── the driver ─────────────────────────────────────────────────────────────

class UserOpDriver:
    """Executes ONE user op at a time against ONE projection session.

    Holds ONLY a ``ProjectionClient`` — by construction it cannot reach a
    Kernel/Runtime/CUAModel/GovernanceService object; the sole channel is
    the documented projection HTTP surface.
    """

    def __init__(self, client: ProjectionClient, *,
                 poll_interval_s: float = 0.05) -> None:
        self._client = client
        self._poll = poll_interval_s

    # ── op dispatch (public routes only) ───────────────────────────────
    def _dispatch(self, op: UserOp):
        c, p = self._client, op.payload
        if op.kind == "start":
            return c.start()
        if op.kind == "pause":
            return c.pause(p.get("rationale", ""))
        if op.kind == "resume":
            return c.resume(p.get("rationale", ""))
        if op.kind == "stop":
            return c.stop(p.get("rationale", ""))
        if op.kind == "local_patch":
            return c.local_patch(p.get("updates") or {},
                                 p.get("rationale", ""))
        if op.kind == "goal_patch":
            return c.goal_patch(
                p.get("goal", ""),
                constraints=p.get("constraints") or (),
                scope=p.get("scope") or (),
                success_criteria=p.get("success_criteria") or (),
                rationale=p.get("rationale", ""))
        if op.kind == "checkpoint":
            return c.checkpoint(p.get("label", ""))
        if op.kind == "rollback":
            return c.rollback(p.get("target_checkpoint_id", ""),
                              p.get("rationale", ""))
        raise ValueError(f"unroutable op kind {op.kind!r}")

    def execute(self, op: UserOp) -> OpOutcome:
        """Issue → settle → honest outcome, with the full timeline."""
        outcome = OpOutcome(op=op, verdict="error")
        t0 = time.monotonic()
        outcome.timeline["op_issued"] = t0
        window: Optional[SSEWindow] = None
        events_before = 0
        try:
            events_before = self._client.event_count()
        except Exception:
            events_before = -1          # honest: page unavailable
        try:
            window = self._client.open_sse_window()
            try:
                outcome.projection_before = _projection_digest(
                    self._client.snapshot())
            except Exception:
                outcome.projection_before = {"available": False}
            try:
                status, body = self._dispatch(op)
            except Exception as e:
                outcome.detail = f"transport: {e}"
                return outcome
            outcome.http_status = status
            outcome.response = body if isinstance(body, dict) else {"_raw": body}
            outcome.timeline["http_accepted"] = time.monotonic()

            if not _class_matches(status, op.expected_http_class):
                outcome.verdict = "rejected"
                outcome.detail = (f"HTTP {status} outside expected class "
                                  f"{op.expected_http_class!r}")
                self._settle_or_timeout(outcome, window, events_before,
                                        SettlePolicy("http"))
                return outcome

            settled = self._settle_or_timeout(outcome, window,
                                              events_before, op.policy)
            outcome.verdict = "applied" if settled else "unsettled"
            if not settled:
                outcome.detail = (f"settle barrier timed out after "
                                  f"{op.policy.timeout_s}s "
                                  f"(mode={op.policy.mode})")
            return outcome
        finally:
            if window is not None:
                time.sleep(self._poll)      # drain in-flight frames
                outcome.sse_window = window.snapshot_events()
                window.close()
            try:
                outcome.projection_after = _projection_digest(
                    self._client.snapshot())
            except Exception:
                outcome.projection_after = {"available": False}
            self._annotate_timeline(outcome)

    # ── the barrier (public signals only) ──────────────────────────────
    def _settle_or_timeout(self, outcome: OpOutcome, window: Optional[SSEWindow],
                           events_before: int, policy: SettlePolicy) -> bool:
        deadline = time.monotonic() + policy.timeout_s
        last_progress = time.monotonic()
        seen: set = set()
        while time.monotonic() < deadline:
            frames = window.snapshot_events() if window else []
            for env in frames:
                marker = (env.get("sse_type"), env.get("event_id"))
                if marker in seen:
                    continue
                seen.add(marker)
                sse_type = env.get("sse_type", "")
                if sse_type in _GUI_SSE_TYPES:
                    ts = time.monotonic()
                    if outcome.timeline["first_gui_action"] is None:
                        outcome.timeline["first_gui_action"] = ts
                    outcome.timeline["last_gui_action"] = ts
                if sse_type in policy_sse_markers(policy) and \
                        outcome.timeline["first_correct_projection"] is None:
                    outcome.timeline["first_correct_projection"] = \
                        time.monotonic()
                if _looks_verified(sse_type) and \
                        outcome.timeline["verifier_completed"] is None:
                    outcome.timeline["verifier_completed"] = time.monotonic()
                if sse_type in SSE_PROGRESS_TYPES:
                    last_progress = time.monotonic()
            if policy.mode == "http":
                outcome.timeline["settled"] = time.monotonic()
                return True
            if policy.mode == "sse":
                if any(env.get("sse_type") == policy.sse_type
                       for env in frames):
                    outcome.timeline["settled"] = time.monotonic()
                    return True
                if events_before >= 0:
                    try:
                        if self._client.event_count() > events_before:
                            outcome.timeline["settled"] = time.monotonic()
                            return True
                    except Exception:
                        pass
            if policy.mode == "quiet":
                quiet = time.monotonic() - last_progress
                if quiet >= policy.quiet_seconds and frames:
                    outcome.timeline["settled"] = time.monotonic()
                    return True
                try:
                    if events_before >= 0 and \
                            self._client.event_count() > events_before:
                        events_before = self._client.event_count()
                        last_progress = time.monotonic()
                except Exception:
                    pass
            time.sleep(self._poll)
        return False

    def _annotate_timeline(self, outcome: OpOutcome) -> None:
        """first/last GUI action + verifier completion, honest when absent
        (None — never fabricated)."""
        for key in TIMELINE_KEYS:
            if outcome.timeline.get(key) is not None:
                outcome.timeline[key] = round(outcome.timeline[key], 4)


def _class_matches(status: int, expected: str) -> bool:
    if expected == "any":
        return True
    if expected == "2xx":
        return 200 <= status < 300
    if expected == "4xx":
        return 400 <= status < 500
    raise ValueError(f"unknown expected_http_class {expected!r}")


def policy_sse_markers(policy: SettlePolicy) -> tuple:
    """SSE types that count as 'the projection now reflects the op'."""
    if policy.mode == "quiet":
        return (SSE_GOVERNANCE_APPLIED,)
    return (policy.sse_type,)


def _looks_verified(sse_type: str) -> bool:
    return any(m in sse_type for m in _VERIFIER_MARKERS)
