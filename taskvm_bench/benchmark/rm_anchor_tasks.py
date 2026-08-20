"""taskvm_bench.benchmark.rm_anchor_tasks — the RM development anchors.

The R1 grader-loop anchor lives here as a REAL ``TaskSpec`` (the
schema.py ontology). A thin adapter (:func:`mobilegym_fixture_view`)
projects a TaskSpec onto the duck-type surface the MobileGym factory
consumes — ending the two-ontology coexistence for RM tasks: new RM
tasks are written ONCE as TaskSpecs, never as CanonicalTaskGraphs.

RM-C04-01 (bench_design §九, "social_mark_and_true_rollback"): on the X
app, find the post mentioning 核心CPI意外下降 and BOTH like AND bookmark it,
then roll the whole thing back through a real checkpoint. The task
exercises the full governance chain a plain CUA cannot:

* two effects (like + bookmark) bound to ONE goal (checkpoint covers
  both);
* a REAL checkpoint must exist before the work;
* the rollback must restore BOTH effects through reverse GUI gestures
  (never a hidden set_state);
* the projection must stay truthful across the whole arc;
* the oracle must confirm zero side effects on the protected posts.

No-leak discipline: ``oracle_id``-style identifiers (post ids) appear in
the spec's seed/success/protected keys — the EVAL plane's private
vocabulary. The goal text a model sees addresses the post by its
VISIBLE CONTENT only.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from taskvm_bench.benchmark.schema import Family, Split, TaskSpec
from taskvm_bench.evaluation.user_ops import UserOp

__all__ = [
    "RM_C04_01", "rm_c04_01_user_ops", "mobilegym_fixture_view",
    "flatten_x_oracle", "tracked_post_ids", "X_ORACLE_FIELDS",
]


# ── the anchor task ────────────────────────────────────────────────────────

#: the real X timeline post whose content mentions 核心CPI意外下降
#: (apps/X/data/posts.json — "扣除食物和能源的核心CPI意外下降…").
#: The search phrase MUST be a CONTIGUOUS substring of the post's
#: content: X's search (useXSearchPosts) is a case-insensitive
#: ``content.includes(q)`` contiguous match — "核心CPI下降" (the
#: 2026-08-20 r1-r5 spelling) matched NOTHING because the post reads
#: "核心CPI意外下降", so the task was uncompletable by ANY agent (r5:
#: the model reached the search page, typed the phrase, and stared at
#: an empty result list for 18 gestures).
#: Verifier-only ground truth: the goal text names the CONTENT, never
#: this id.
CPI_POST_ID = "p_1879539450872778943"
#: a sibling timeline post (the 加班动员会 post) — the protected
#: non-interference target. The seed marks it PRE-bookmarked: the
#: rollback must leave it exactly as it found it.
PROTECTED_POST_ID = "p_1879539026291785845"

#: the x surface's platform identity: the catalog app the phone hosts
#: (mobilegym ``apps/X`` — the home screen renders the same name, which
#: is exactly what the StateCompiler quotes to observe ``platform='X'``).
#: Projected into the oracle read so the deterministic grader can VERIFY
#: that projection observation against the world: an oracle that cannot
#: see the platform could never tell a truthful ``platform='X'`` from a
#: fabricated one (GATE-G0 2026-08-20 postmortem: the PROJECTION_MISMATCH
#: was a false positive of a toggle-lists-only oracle, not a projection
#: lie). A world FACT, never a tracked effect — it must NOT appear in
#: the spec's seed/success/protected/witness keys.
X_PLATFORM = "X"


def _k(pid: str, field: str) -> str:
    return f"{pid}.{field}"


RM_C04_01 = TaskSpec(
    task_id="rm-c04-01-social-mark-true-rollback",
    family=Family.ROLLBACK,
    split=Split.ID,
    goal="在 X 中找到提到“核心CPI意外下降”的帖子，同时点赞并收藏。",
    surfaces=("x",),
    seed={
        "x": {
            _k(CPI_POST_ID, "liked"): "false",
            _k(CPI_POST_ID, "bookmarked"): "false",
            _k(PROTECTED_POST_ID, "liked"): "false",
            _k(PROTECTED_POST_ID, "bookmarked"): "true",
        }
    },
    # the FROZEN final state: the forward work really happened (witness
    # below) and the rollback truly restored both effects
    success={
        "x": {
            _k(CPI_POST_ID, "liked"): "false",
            _k(CPI_POST_ID, "bookmarked"): "false",
            _k(PROTECTED_POST_ID, "liked"): "false",
            _k(PROTECTED_POST_ID, "bookmarked"): "true",
        }
    },
    protected=(
        ("x", _k(PROTECTED_POST_ID, "liked")),
        ("x", _k(PROTECTED_POST_ID, "bookmarked")),
    ),
    witness=(
        ("x", _k(CPI_POST_ID, "liked"), "true"),
        ("x", _k(CPI_POST_ID, "bookmarked"), "true"),
    ),
    notes="bench_design §九 anchor. Forward window: BOTH effects land "
          "(like + bookmark on the content-addressed CPI post). Rollback "
          "window: a real checkpoint precedes the work; the restore goes "
          "through reverse GUI gestures (toggle-like / toggle-bookmark "
          "are reversible store toggles); the pre-bookmarked sibling "
          "post must survive untouched (non-interference). Witness "
          "closes the no-op loophole: a system that never wrote the "
          "forward values cannot pass by standing still.",
)

#: the X oracle fields the flattener projects (see flatten_x_oracle)
X_ORACLE_FIELDS = ("liked", "retweeted", "bookmarked")


def flatten_x_oracle(x_state: dict, tracked_post_ids: Iterable[str]) -> dict:
    """``x_state`` (the bridge's ``/api/x_state`` payload: the three
    toggle id lists) → the normalized ``{"x": {"<pid>.<field>": value}}``
    the EvidenceBundle speaks. Post ids NOT in any toggle list are
    honestly un-toggled (``"false"``) — the store's absence IS the value,
    so the projection is total over the tracked ids without any DOM
    read."""
    liked = set(x_state.get("likedPostIds") or [])
    retweeted = set(x_state.get("retweetedPostIds") or [])
    bookmarked = set(x_state.get("bookmarkedPostIds") or [])
    rows: dict[str, str] = {}
    for pid in tracked_post_ids:
        rows[f"{pid}.liked"] = "true" if pid in liked else "false"
        rows[f"{pid}.retweeted"] = "true" if pid in retweeted else "false"
        rows[f"{pid}.bookmarked"] = ("true" if pid in bookmarked
                                     else "false")
    return {"x": rows}


def tracked_post_ids(spec: TaskSpec) -> tuple[str, ...]:
    """Post ids the spec tracks on the x surface (from its own key
    vocabulary — the eval plane's private GT, never model-facing)."""
    ids: list[str] = []
    for key in (spec.seed.get("x") or {}):
        pid = key.split(".", 1)[0]
        if pid not in ids:
            ids.append(pid)
    return tuple(ids)


# ── the user-op program (bench_design §九 U0–U5) ──────────────────────────

def _rollback_to_first_checkpoint(
        previous_outcomes: Sequence[Any],
        *, settle_policy: Any = None) -> UserOp:
    """Deferred op entry: resolve the rollback target from the FIRST
    applied checkpoint op's public HTTP response (the checkpoint_id the
    projection itself returned — no hidden id source).

    ``settle_policy`` (optional): a real-model run passes a generous
    barrier here — the rollback HTTP handler is synchronous and waits
    for the CUA's reverse-GUI compensation to finish INSIDE the request
    (the scripted tests keep the per-kind default)."""
    for outcome in previous_outcomes:
        if getattr(outcome.op, "kind", "") == "checkpoint":
            ckpt_id = (outcome.response or {}).get("checkpoint_id", "")
            if ckpt_id:
                return UserOp.rollback(ckpt_id, settle_policy=settle_policy)
    raise ValueError(
        "rollback program needs an applied checkpoint first "
        "(no checkpoint_id in any prior op response)")


def rm_c04_01_user_ops(*, settle_timeout_s: float | None = None,
                       start_quiet_s: float = 180.0) -> list:
    """The public user-op program, verbatim from bench_design §九:

        U0 checkpoint("C0") → U1 start() → U2 settle →
        U3 rollback(C0) → U4 settle → U5 stop()

    The rollback entry is a CALLABLE resolved at execution time against
    the prior outcomes (the checkpoint id only exists after U0's public
    response). Settling (U2/U4) is the driver's per-op barrier, not an
    op of its own.

    ``settle_timeout_s``: per-op settle-barrier timeout. ``None`` keeps
    the per-kind defaults (the fast scripted tests); a REAL-MODEL run
    passes a generous value — the synchronous rollback HTTP handler
    waits for the CUA's reverse-GUI compensation to finish INSIDE the
    request.

    ``start_quiet_s``: the start op's quiet window — the span the
    forward task gets to run INSIDE the settle barrier. The quiet
    clock only sees PUBLIC progress (SSE frames / events-page growth)
    and both go SILENT while a model call is in flight: a real CUA
    predict takes tens of seconds, so a quiet window shorter than the
    longest silent inference gap settles the op mid-flight (GATE-G0
    2026-08-20 postmortem: quiet=3s settled the start op inside the
    FIRST CUA call; the rollback then found nothing to undo
    (entries=0), the stop killed the runtime before any GUI action,
    and the witnesses never landed — cua=0,
    WORLD_WITNESS_MISSING). 180s ≈ 7× the longest provider call in
    the run's own archive (24s)."""
    if settle_timeout_s is None:
        return [
            UserOp.checkpoint("C0"),
            UserOp.start(),
            _rollback_to_first_checkpoint,
            UserOp.stop(),
        ]
    from taskvm_bench.evaluation.user_ops import SettlePolicy
    t = float(settle_timeout_s)

    def _deferred_rollback(previous_outcomes: Sequence[Any]) -> UserOp:
        return _rollback_to_first_checkpoint(
            previous_outcomes,
            settle_policy=SettlePolicy("sse", timeout_s=t))

    return [
        UserOp.checkpoint(
            "C0", settle_policy=SettlePolicy("sse", timeout_s=t)),
        UserOp.start(settle_policy=SettlePolicy(
            "quiet", quiet_seconds=float(start_quiet_s), timeout_s=t)),
        _deferred_rollback,
        UserOp.stop(settle_policy=SettlePolicy("sse", timeout_s=t)),
    ]


# ── the thin MobileGym adapter (ends the two-ontology coexistence) ────────

class _BindingView:
    """Duck-type of CanonicalBinding the MobileGymTrialSpec consumes
    (only ``.app`` is read — for oracle coverage)."""

    def __init__(self, app: str) -> None:
        self.app = app
        self.var_id = ""
        self.entity_id = ""
        self.field = ""
        self.operator = ""
        self.expected_value_after_edit = None


class MobileGymFixtureView:
    """A TaskSpec projected onto the fixture duck-type the MobileGym
    factory/CLI consume (``task_id`` / ``goal`` / ``seed_state`` /
    ``bindings``). THIN by construction: every field is a pass-through
    or a direct projection — no second task ontology is defined here.

    ``seed_state`` notes: X posts are content-addressed by the REAL
    timeline (not seedable — documented in mobilegym_fixtures); the only
    store-level seed this task needs is marking the protected post
    pre-bookmarked, expressed as the ``x.user.bookmarkedPostIds`` store
    slice the bridge's inject_task deep-merges."""

    def __init__(self, spec: TaskSpec) -> None:
        self._spec = spec
        self.task_id = spec.task_id
        self.goal = spec.goal
        self.bindings = [_BindingView(s) for s in spec.surfaces]

    @property
    def spec(self) -> TaskSpec:
        return self._spec

    @property
    def seed_state(self) -> dict:
        return _rm_seed_state(self._spec)

    def oracle_read(self, oracles: dict, sid: str) -> dict:
        """The normalized ``{surface: {key: value}}`` oracle read over
        THIS task's tracked keys — the thin-adapter protocol the
        MobileGym factory consumes to build the EvidenceRecorder
        (``.spec`` + ``.oracle_read``). The adapter owns the app-specific
        flattening (X toggle lists → ``<pid>.<field>`` rows); the
        factory stays app-agnostic. Eval-plane private vocabulary:
        these keys never reach the model.

        Besides the tracked post rows the read projects the surface's
        PLATFORM identity (``X_PLATFORM``) — a world fact the projection
        legitimately observes (the home screen's app name) that the
        grader's projection-consistency check must be able to verify.
        It is a CONSTANT of the world, so it never disturbs the
        seed/success/protected/witness predicates (which iterate ONLY
        the spec's own keys)."""
        out: dict = {}
        x_env = (oracles or {}).get("x")
        if x_env is not None:
            flat = flatten_x_oracle(
                x_env.x_state(sid), tracked_post_ids(self._spec))
            # the platform row joins the x SURFACE's rows (flatten already
            # returns the {"x": {...}} surface wrap — add inside it)
            flat.setdefault("x", {})["platform"] = X_PLATFORM
            out.update(flat)
        return out


def _rm_seed_state(spec: TaskSpec) -> dict:
    """The MobileGym seed directive derived from the spec's seed values.

    For the x surface the flattened ``<pid>.bookmarked == "true"`` rows
    become the ``bookmarkedPostIds`` store slice; liked rows are not
    seedable as toggles the UI starts with (an un-toggled post is the
    store default), so only truthy toggles are seeded. Apps with richer
    seed needs (wechat chats) extend this table when their anchor lands
    — one function, no per-task evaluators."""
    out: dict[str, Any] = {}
    x_seed = spec.seed.get("x")
    if x_seed:
        bm = [key.split(".", 1)[0] for key, val in x_seed.items()
              if key.endswith(".bookmarked") and str(val).lower() == "true"]
        liked = [key.split(".", 1)[0] for key, val in x_seed.items()
                 if key.endswith(".liked") and str(val).lower() == "true"]
        user: dict[str, list] = {}
        if bm:
            user["bookmarkedPostIds"] = bm
        if liked:
            user["likedPostIds"] = liked
        if user:
            out["x"] = {"user": user}
    return out


def mobilegym_fixture_view(spec: TaskSpec) -> MobileGymFixtureView:
    """The adapter entry: TaskSpec → the fixture duck-type."""
    return MobileGymFixtureView(spec)
