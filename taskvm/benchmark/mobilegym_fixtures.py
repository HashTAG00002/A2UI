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
(``evaluation/run_mobilegym_killtest``). MUST NOT be imported by the compiler
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

from taskvm.benchmark.fixtures import CanonicalBinding, CanonicalTaskGraph


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


MOBILEGYM_TASKS: dict[str, CanonicalTaskGraph] = {
    TOP3_EXPENSE_TO_WECHAT.task_id: TOP3_EXPENSE_TO_WECHAT,
}


def get_mobilegym_task(task_id: str) -> CanonicalTaskGraph:
    if task_id not in MOBILEGYM_TASKS:
        raise KeyError(f"unknown mobilegym task {task_id!r}; known: {list(MOBILEGYM_TASKS)}")
    return MOBILEGYM_TASKS[task_id]


def all_mobilegym_tasks() -> dict[str, CanonicalTaskGraph]:
    return dict(MOBILEGYM_TASKS)
