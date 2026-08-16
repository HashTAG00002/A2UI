"""MobileGym demo canonical task graph — verifier-only GT (no-leak).

Holds the GT binding for the ONE demo task that proves the TaskVM four-step arc
(bind → write → verify → rollback) on a new substrate (MobileGym, a
Playwright-driven React phone sim) accessed via ``harness/mobilegym_bridge.py``.

  ``top3_expense_to_wechat`` — a port of MobileGym's
  ``bench_env/task/crossapp_commerce/defs/Top3ExpenseSummaryToWechat.py``:
  read alipay's top-3 expenses (within 30d), send a summary text to a wechat
  contact. 2 apps (alipay read-only → wechat write), 1 write surface
  (wechat.chats[<id>].messages via ``send_message``).

**No-leak boundary (load-bearing)**: same as ``fixtures.py`` / ``ood_fixtures.py``
— imported ONLY by the verifier path + the orchestrator
(the now deleted legacy MobileGym entry). MUST NOT be imported by the compiler
path (``task_state/``, ``execution/``). The compiler sees only rendered
observations (the bridge's HTML view). The ``CanonicalBinding.operator`` field
(``send_message``) is verifier-only GT; the compiler-visible
``OPERATOR_REGISTRY`` (in ``task_state/entity_binding.py``) carries ONLY the
operator signature, no var_ids.

Demo-scope simplifications (honest — handoff §5 item 5):
  - The real Top3 task sends to 黄勇 (default contact param). MobileGym's
    default wechat state has no 黄勇 chat, so the fixture seeds one (empty
    messages) via the bridge's ``add_chats`` merge directive. This keeps the
    post-edit ``messages`` field EXACTLY equal to the new text (the verifier's
    ``field_matches`` is exact equality), avoiding joined-string fragility.
  - The top-3 amounts (520 / 168 / 42) are pinned to the sim's fixed clock
    (the 30-day window resolves deterministically). A different sim clock
    shifts the set — the bridge pins the clock via the env's os.time.
  - ``non_interference_set`` lists the other wechat chats + one alipay tx
    (enough to catch an over-applied dispatch; alipay is read-only so it is
    inherently unchanged, but listing one tx proves the read app wasn't
    corrupted by the wechat write).
"""
from __future__ import annotations

from taskvm_bench.benchmark.fixtures import CanonicalBinding, CanonicalTaskGraph, Checkpoint


# The wechat chat the demo writes to (seeded fresh, empty messages).
HUANGYONG_WXID = "wxid_huangyong_demo"
HUANGYONG_CHAT = {
    "id": HUANGYONG_WXID,
    "user": {"wxid": HUANGYONG_WXID, "name": "黄勇",
             "avatar": "/@app-assets/Wechat/avatars/avatar_default.jpg"},
    "isMuted": False, "isSticky": False, "isAlert": False, "isOfficial": False,
    "messages": [],
}

# Synthetic contact for 黄勇 (demo-controlled). Using a synthetic wxid rather
# than the real wxid_huangyong_brave because the real one has
# aiConfig.enabled=True, which would fire triggerAIReply on send (an async LLM
# call that appends an AI reply message and complicates the round-trip
# verification). Seeding this contact lets ChatDetail.resolveChatPeerByWxid
# find the peer cleanly; the empty chat above lets sendMessage upsert+append.
HUANGYONG_CONTACT = {
    "wxid": HUANGYONG_WXID,
    "name": "黄勇",
    "avatar": "/@app-assets/Wechat/avatars/avatar_default.jpg",
    "aiConfig": {"enabled": False},
}

# The pinned top-3 (sim-clock-relative 30-day window). The summary text the
# user edits the binding to — exactly what the verifier expects post-write.
TOP3_TEXT = "本月top3支出: 520, 168, 42。省着点。"


TOP3_EXPENSE_TO_WECHAT = CanonicalTaskGraph(
    task_id="top3_expense_to_wechat",
    goal="看一下支付宝最近30天支出最高的3笔，把金额发给微信里的黄勇，提醒他省着点。",
    seed_state={
        "wechat": {"add_chats": [HUANGYONG_CHAT],
                   "add_contacts": [HUANGYONG_CONTACT]},
    },
    user_edit={"var_id": "expense_summary", "old": "", "new": TOP3_TEXT},
    bindings=[
        CanonicalBinding("expense_summary", "wechat", HUANGYONG_WXID,
                         "messages", "send_message", TOP3_TEXT),
    ],
    non_interference_set=[
        # other wechat chats must not change (catches over-applied dispatch)
        ("wechat", "wxid_zhangwei_888"),
        ("wechat", "wxid_wangfang_123"),
        ("wechat", "wxid_boss_007"),
        # one alipay tx — proves the read-only app wasn't corrupted by the write
        ("alipay", "tr_20260115_1"),
    ],
    expected_diff={
        "wechat": {HUANGYONG_WXID: {"messages": TOP3_TEXT}},
    },
    description="MobileGym demo: read alipay top-3 expenses (30d window) → send "
                "a summary text to the wechat 黄勇 chat via send_message. 2 apps "
                "(alipay read-only → wechat write), 1 write surface. Rollback is "
                "snapshot-based (bridge set_state restore), not a field-setter inverse.",
)


# ── E17 MG-1: social_morning_brief (cross-app, visible-uniqueness, VM5) ──────
# The strong discriminating task. A user opens X, sees a post about a core-CPI
# drop, likes it, and forwards a note to 黄勇 on wechat. Satisfies VM5:
#   - bottom-up live projection: liked-status read from real X state (x_state),
#     message read from real wechat read_canonical — neither is a static seed.
#   - cross-app fanout: ONE task drives writes on 2 apps (x.toggle_like +
#     wechat.send_message) in one governance flow.
#   - governance + checkpoint: C1 (liked), C2 (messaged) — the driver advances
#     C0→C1→C2 and the evaluation exercises rollback_to C1 (un-like) which is
#     reversible, vs rollback_to C0 from C2 (un-send) which is honest-409.
#   - reversibility spectrum: toggle_like is reversible (re-tap), send_message
#     is honest-irreversible (bridge 409). Both tested in one task.
#   - substrate-independence: same CanonicalTaskGraph shape as the builtin
#     release_reschedule; only the StateAdapter differs.
#
# NOT a bidirectional-binding task (honest): the handoff forced ONE var
# "morning_brief" → 2 bindings, but toggle_like wants value=True while
# send_message wants the message TEXT — different semantic quantities with
# different values. compile_patch propagates a single `new` to all bindings of
# a var, so one var would set toggle_like.value=TEXT (wrong). MG-1 uses TWO
# var_ids (morning_brief_liked + morning_brief_message). Bidirectional binding
# (1 var → N bindings, SAME value) is demonstrated by release_reschedule
# (release_date → calendar E1.date + taskboard T1/T2.deadline, all "2026-08-18").
#
# VISIBLE-UNIQUENESS (principle 1, load-bearing): the target X post is
# identified to the CUA by its VISIBLE CONTENT ("核心CPI … 下降"), NOT by post_id.
# The post p_1879539450872778943 is a real entry in apps/X/data/posts.json with
# content "扣除食物和能源的核心CPI意外下降 哈哈哈哈哈" — visually unique among the
# 3 default timeline posts (the others are 加班-mobilization and an X-app tip).
# The post_id is verifier-only GT; the instruction (built by the governance
# evaluation / GovernanceInterpreter) names the content token, never the id.
#
# HONEST DEVIATION from HANDOFF §2.2 MG-1: the handoff's seed_state included
# {"alipay": {"portfolio_value": 52860}} and a wechat message containing that
# figure. Recon confirmed (a) X posts CANNOT be seeded via inject_task (the
# bridge loads them from posts.json, seed_state['x']['posts'] is ignored) and
# (b) alipay portfolio_value is NOT a verified set_state field (the existing
# top3 fixture deliberately reads alipay from default sim state rather than
# seeding). So MG-1 uses the REAL existing CPI post (no X seeding) and drops
# the alipay-portfolio read (the cross-app property is already satisfied by
# x+wechat; adding an unverified alipay seed would be a fake field).
MORNING_BRIEF_POST_ID = "p_1879539450872778943"   # real posts.json entry, content "…核心CPI意外下降…"
MORNING_BRIEF_POST_TOKEN = "核心CPI"               # visible-uniqueness token (content, not id)
MORNING_BRIEF_TEXT = "看到一条关于核心CPI下降的帖子，值得关注一下风险。"

SOCIAL_MORNING_BRIEF = CanonicalTaskGraph(
    task_id="social_morning_brief",
    goal="打开 X，看到一条关于核心CPI下降的帖子，点 like 收藏，然后把这条帖子"
         "转发给微信里的黄勇，提醒他关注一下。",
    seed_state={
        "wechat": {"add_chats": [HUANGYONG_CHAT],
                   "add_contacts": [HUANGYONG_CONTACT]},
        # NOTE: no "x" key — X posts are NOT seedable via inject_task (bridge
        # loads them from posts.json). The target post is the real
        # p_1879539450872778943 already on the timeline.
    },
    # user_edit models the PRIMARY edit (the wechat message). The like is a
    # second edit driven by the governance event sequence (mg1_event_sequence),
    # not by this single user_edit field. expected_diff captures BOTH finals.
    user_edit={"var_id": "morning_brief_message", "old": "", "new": MORNING_BRIEF_TEXT},
    bindings=[
        # binding 1: like the CPI post (x app, toggle_like, reversible, value=True)
        CanonicalBinding("morning_brief_liked", "x", MORNING_BRIEF_POST_ID,
                         "liked", "toggle_like", True),
        # binding 2: send the note to 黄勇 (wechat, send_message, honest-409)
        CanonicalBinding("morning_brief_message", "wechat", HUANGYONG_WXID,
                         "messages", "send_message", MORNING_BRIEF_TEXT),
    ],
    non_interference_set=[
        # other wechat chats must not change
        ("wechat", "wxid_zhangwei_888"),
        ("wechat", "wxid_wangfang_123"),
        ("wechat", "wxid_boss_007"),
        # other X posts' like-state must not change (the 2 non-target default posts)
        ("x", "p_1879539026291785845"),
        ("x", "p_1879526642210808148"),
    ],
    expected_diff={
        "x": {MORNING_BRIEF_POST_ID: {"liked": True}},
        "wechat": {HUANGYONG_WXID: {"messages": MORNING_BRIEF_TEXT}},
    },
    checkpoints=[
        Checkpoint("C1", description="已 like 核心CPI帖子",
                   criterion={"x": {MORNING_BRIEF_POST_ID: {"liked": True}}}),
        Checkpoint("C2", description="已发微信消息给黄勇",
                   criterion={"wechat": {HUANGYONG_WXID: {"messages": MORNING_BRIEF_TEXT}}}),
    ],
    description="E17 MG-1: cross-app morning brief — like the real X post about "
                "核心CPI下降 (identified by VISIBLE CONTENT, not post_id) + send a "
                "note to wechat 黄勇. TWO var_ids (liked + message — different "
                "values, NOT bidirectional-binding; that property is in "
                "release_reschedule). 2 checkpoints (C1 liked, C2 messaged). "
                "Reversibility spectrum: toggle_like reversible, send_message "
                "honest-409. DEVIATION: drops the handoff's alipay portfolio_value "
                "seed (unverified field) — cross-app still holds via x+wechat.",
)


# ── E17 MG-2: expense_and_notify (top3 + dual checkpoint + honest-409) ───────
# The VM-extension of top3_expense_to_wechat: same alipay-read → wechat-write
# core, but modeled as a 2-checkpoint governance flow that exercises the
# reversibility spectrum explicitly:
#   C0 (initial) → C1 (first message sent) → [rollback_to C0 attempt: honest 409]
#   → C2 (resent message)
# The two checkpoints carry DIFFERENT expected message texts (V1 vs V2) so the
# verifier can distinguish "still V1 (rollback honestly failed)" from "now V2
# (resend succeeded)". The honest-409 between C1 and C2 is recorded in the
# evaluation report as vm_properties_covered.reversibility_negative=true (it is
# an execution-history property, NOT a canonical-state criterion).
#
# HONEST NOTE on append-semantics: wechat send_message APPENDS. After sending
# V1 then V2, the chat holds [V1, V2]. The exact-equality `messages` field
# check (field_matches) cannot express "contains V2 among appended messages".
# So C2's criterion uses the _contains semantics (a new criterion mode the
# governance criterion-checker implements; check_round_trip does
# NOT). C1 uses exact equality (chat was empty before, so messages == [V1]
# exactly after the first send). This asymmetry is honest: C1 is verifiable
# via the existing verifier; C2 requires the governance criterion-checker.
EXPENSE_NOTIFY_V1 = "本月top3支出: 520, 168, 42。先发一版。"
EXPENSE_NOTIFY_V2 = "本月top3支出: 520, 168, 42。省着点，重新发一下。"

EXPENSE_AND_NOTIFY = CanonicalTaskGraph(
    task_id="expense_and_notify",
    goal="看一下支付宝最近30天支出最高的3笔，先发一版给微信黄勇，撤回试试，"
         "撤不回就重新编辑发一版。",
    seed_state={
        "wechat": {"add_chats": [HUANGYONG_CHAT],
                   "add_contacts": [HUANGYONG_CONTACT]},
    },
    user_edit={"var_id": "expense_summary", "old": "", "new": EXPENSE_NOTIFY_V2},
    bindings=[
        CanonicalBinding("expense_summary", "wechat", HUANGYONG_WXID,
                         "messages", "send_message", EXPENSE_NOTIFY_V2),
    ],
    non_interference_set=[
        ("wechat", "wxid_zhangwei_888"),
        ("wechat", "wxid_wangfang_123"),
        ("wechat", "wxid_boss_007"),
        ("alipay", "tr_20260115_1"),
    ],
    expected_diff={
        # final state: V2 is present (append-semantics → messages contains V2)
        "wechat": {HUANGYONG_WXID: {"messages": EXPENSE_NOTIFY_V2}},
    },
    checkpoints=[
        Checkpoint("C1", description="已发第一版消息",
                   criterion={"wechat": {HUANGYONG_WXID: {"messages": EXPENSE_NOTIFY_V1}}}),
        Checkpoint("C2", description="撤回失败后重发第二版",
                   criterion={"wechat": {HUANGYONG_WXID: {"messages": EXPENSE_NOTIFY_V2}}}),
    ],
    description="E17 MG-2: top3 + dual-checkpoint governance. C1 = first message "
                "(V1) sent; transition C1→C0 = rollback attempt (honest 409, "
                "send_message irreversible); C2 = resend V2. Reversibility "
                "spectrum: the 409 proves honest-irreversibility "
                "(reversibility_negative); the resend proves a forward write path "
                "still works after a failed undo (reversibility_positive). C1 uses "
                "exact-equality; C2 uses _contains (append-semantics) — the latter "
                "requires the governance criterion-checker.",
)


MOBILEGYM_TASKS: dict[str, CanonicalTaskGraph] = {
    TOP3_EXPENSE_TO_WECHAT.task_id: TOP3_EXPENSE_TO_WECHAT,
    SOCIAL_MORNING_BRIEF.task_id: SOCIAL_MORNING_BRIEF,
    EXPENSE_AND_NOTIFY.task_id: EXPENSE_AND_NOTIFY,
}


def get_mobilegym_task(task_id: str) -> CanonicalTaskGraph:
    if task_id not in MOBILEGYM_TASKS:
        raise KeyError(f"unknown mobilegym task {task_id!r}; known: {list(MOBILEGYM_TASKS)}")
    return MOBILEGYM_TASKS[task_id]


def all_mobilegym_tasks() -> dict[str, CanonicalTaskGraph]:
    return dict(MOBILEGYM_TASKS)
