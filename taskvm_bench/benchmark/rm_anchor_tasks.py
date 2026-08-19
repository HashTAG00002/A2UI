"""taskvm_bench.benchmark.rm_anchor_tasks — the RM development anchors.

The R1 grader-loop anchor lives here as a REAL ``TaskSpec`` (the
schema.py ontology). A thin adapter (:func:`mobilegym_fixture_view`)
projects a TaskSpec onto the duck-type surface the MobileGym factory
consumes — ending the two-ontology coexistence for RM tasks: new RM
tasks are written ONCE as TaskSpecs, never as CanonicalTaskGraphs.

RM-C04-01 (bench_design §九, "social_mark_and_true_rollback"): on the X
app, find the post mentioning 核心CPI下降 and BOTH like AND bookmark it,
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

#: the real X timeline post whose content mentions 核心CPI下降
#: (apps/X/data/posts.json — "扣除食物和能源的核心CPI意外下降…").
#: Verifier-only ground truth: the goal text names the CONTENT, never
#: this id.
CPI_POST_ID = "p_1879539450872778943"
#: a sibling timeline post (the 加班动员会 post) — the protected
#: non-interference target. The seed marks it PRE-bookmarked: the
#: rollback must leave it exactly as it found it.
PROTECTED_POST_ID = "p_1879539026291785845"


def _k(pid: str, field: str) -> str:
    return f"{pid}.{field}"


RM_C04_01 = TaskSpec(
    task_id="rm-c04-01-social-mark-true-rollback",
    family=Family.ROLLBACK,
    split=Split.ID,
    goal="在 X 中找到提到“核心CPI下降”的帖子，同时点赞并收藏。",
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
        previous_outcomes: Sequence[Any]) -> UserOp:
    """Deferred op entry: resolve the rollback target from the FIRST
    applied checkpoint op's public HTTP response (the checkpoint_id the
    projection itself returned — no hidden id source)."""
    for outcome in previous_outcomes:
        if getattr(outcome.op, "kind", "") == "checkpoint":
            ckpt_id = (outcome.response or {}).get("checkpoint_id", "")
            if ckpt_id:
                return UserOp.rollback(ckpt_id)
    raise ValueError(
        "rollback program needs an applied checkpoint first "
        "(no checkpoint_id in any prior op response)")


def rm_c04_01_user_ops() -> list:
    """The public user-op program, verbatim from bench_design §九:

        U0 checkpoint("C0") → U1 start() → U2 settle →
        U3 rollback(C0) → U4 settle → U5 stop()

    The rollback entry is a CALLABLE resolved at execution time against
    the prior outcomes (the checkpoint id only exists after U0's public
    response). Settling (U2/U4) is the driver's per-op barrier, not an
    op of its own."""
    return [
        UserOp.checkpoint("C0"),
        UserOp.start(),
        _rollback_to_first_checkpoint,
        UserOp.stop(),
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
        these keys never reach the model."""
        out: dict = {}
        x_env = (oracles or {}).get("x")
        if x_env is not None:
            out.update(flatten_x_oracle(
                x_env.x_state(sid), tracked_post_ids(self._spec)))
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
