"""X app toggle kill-test — the VM-moment existence proof for MobileGym (E14).

E17-A Option B (2026-08-12): the verifier is now ALIGNED with the instruction.
Prior to E17 the task was ill-posed (.mrules E17 §0-B): the instruction told
the CUA "find ANY un-toggled post and tap it" while the verifier required a
SPECIFIC post_id to appear in the toggle list — on a fresh reset with 3
un-toggled posts the model had no screenshot-visible way to know which post
was "expected", so the test measured 1/3 blind-guess luck, not CUA ability.

E17-A Option B fix: ``verify_mode='any_new'`` — the bridge captures a
SERVER-SIDE before-snapshot of the toggle list (never leaked to the prompt —
that was the E16 bug; this does not repeat it) and verifies that SOME post
transitioned (the list grew for a write / shrank for a rollback). The
instruction is unchanged ("find an un-toggled post and tap it") — now the
verifier matches it. The ill-posed contradiction is resolved.

What this tests (E16-complete pure-vision + E17-A Option B verifier):
  - **toggle_like / toggle_retweet / toggle_bookmark**: can the model, given
    ONLY a screenshot + a goal-level instruction naming no post_id, find an
    un-toggled post, locate the right action-bar icon, and tap it precisely
    enough for the store toggle to fire on ANY post?
  - **success criterion (Option B)**: the toggle list GREW by ≥1 vs the
    before-snapshot (trusted ``get_state`` read — NOT model self-judge). The
    ``toggled_post_id`` field records WHICH post the model actually chose, so
    the per-post distribution reveals position bias (does it always tap
    post[0]?) without that bias being scored as failure.

The strong discriminating task is MG-1 ``social_morning_brief`` (visible-
uniqueness: instruction names the target by content). THIS killtest is the
easy aligned baseline — Option B makes it a clean "can the CUA toggle any
post" existence proof, no longer a 1/3-guess lottery.

Lands a PERSISTED JSON artifact at
``eval_results/x_toggle_killtest_<ts>.json`` — the repo artifact anyone can
re-audit (memory is NOT a substitute — see .mrules E9.1).

Usage:
    # bridge (:3019) + Vite (:3000) must be running first
    python -m taskvm.evaluation.run_x_toggle_killtest --samples 3
    python -m taskvm.evaluation.run_x_toggle_killtest --samples 5 --posts p_1879539450872778943 p_1879539026291785845
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
BRIDGE_PORT = 3019
VITE_PORT = 3000
# Posts visible on the X timeline without scrolling (the first 3 in the X
# app's base dataset — chosen because they're immediately visible, so the
# test measures "can the model find + tap the right icon" not "can it scroll").
# Under E16-complete the model gets NO post-id/text hint — it must pick an
# un-toggled post from the screenshot alone, so WHICH of these is the expected
# target matters (see the per-post breakdown in the report).
DEFAULT_POSTS = [
    "p_1879539450872778943",
    "p_1879539026291785845",
    "p_1879526642210808148",
]
OPERATORS = ["toggle_like", "toggle_retweet", "toggle_bookmark"]


def _health_check(host: str) -> bool:
    """Verify bridge + Vite are reachable (E8: no fabricating scores against
    unreachable services)."""
    try:
        r = requests.get(f"http://{host}:{BRIDGE_PORT}/health", timeout=5)
        if r.status_code != 200 or r.json().get("status") != "ok":
            logger.error(f"bridge :{BRIDGE_PORT} unhealthy: {r.status_code} {r.text[:100]}")
            return False
    except Exception as e:
        logger.error(f"bridge :{BRIDGE_PORT} not reachable: {e}")
        return False
    try:
        r = requests.get(f"http://{host}:{VITE_PORT}", timeout=4)
        if r.status_code != 200:
            logger.error(f"Vite :{VITE_PORT} returned {r.status_code}")
            return False
    except Exception as e:
        logger.error(f"Vite :{VITE_PORT} not reachable: {e}")
        return False
    logger.info(f"bridge :{BRIDGE_PORT} + Vite :{VITE_PORT} healthy")
    return True


def _run_one_toggle(host: str, sid: str, post_id: str, operator: str,
                    *, use_driver: bool = False) -> dict:
    """Run one toggle operation via the bridge and return the result record.

    E17-A Option B: passes ``verify_mode='any_new'`` so the bridge verifier
    checks that SOME post transitioned (list grew), not that the specific
    ``post_id`` did — aligning the verifier with the any-post instruction.
    The ``toggled_post_id`` (which post the model actually tapped) is captured
    for the per-post distribution.

    E17-B ``use_driver``: route through ScriptedUserDriver + GovernanceInterpreter
    to produce the CUA instruction (the "完整链路" — user behavior → governance →
    CUA, not the segmented intent → f-string → CUA). The subgoal's
    natural_language is passed to the bridge via ``instruction_override``,
    skipping the inline f-string. When False (default), the bridge builds its
    own f-string (unchanged — zero regression).
    """
    # Agent B (substrate isolation): raw bridge write POSTs moved behind
    # MobileGymTaskAdapter (bridge routes wrap the injected CUA loop = real gestures).
    payload_extra = {"verify_mode": "any_new"}  # E17-A Option B
    instruction_override = None
    driver_nl = None
    if use_driver:
        # Build a one-shot ScriptedUserDriver event for this toggle + interpret
        # it through GovernanceInterpreter to get the subgoal NL. This is the
        # de-segmentation: the instruction comes from the governance layer's
        # interpretation of a user-behavior event, not a hardcoded f-string.
        # (Agent C role collapse: the drivers now live in tests/fakes.)
        from tests.fakes.scripted_driver import ScriptedUserDriver
        from tests.fakes.governance_interpreter import GovernanceInterpreter
        from tests.fakes.user_behavior_driver import UserBehaviorEvent
        from taskvm.governance.vm_state import VMStateSnapshot
        from taskvm.execution.rollback import RollbackLog
        from taskvm.benchmark.mobilegym_fixtures import MORNING_BRIEF_POST_ID
        from taskvm.task_state.entity_binding import TaskBinding
        # minimal binding for this one toggle op (no compiler call)
        binding = TaskBinding(
            task_id="x_toggle_driver",
            variables=[{"var_id": "x_toggle", "label": "x_toggle",
                        "value": "", "editable": True,
                        "bindings": [{"var_id": "x_toggle", "app": "x",
                                      "entity_id": post_id, "field": "liked",
                                      "operator": operator}]}])
        vm_state = VMStateSnapshot(
            sid=sid, binding=binding, adapters={}, rollback_log=RollbackLog())
        ev = UserBehaviorEvent(
            "edit_field", {"var_id": "x_toggle", "new_value": True})
        subgoals = GovernanceInterpreter().interpret(ev, vm_state)
        if subgoals:
            instruction_override = subgoals[0].natural_language
            driver_nl = instruction_override
            payload_extra["instruction_override"] = instruction_override
    t0 = time.time()
    try:
        from taskvm.execution.gui_driver import MobileGymTaskAdapter
        status, body = MobileGymTaskAdapter(
            "x", bridge_url=f"http://{host}:{BRIDGE_PORT}").mutate_raw(
            sid, post_id, operator, True, payload_extra=payload_extra)
        elapsed = round(time.time() - t0, 1)
        if status == 200 and isinstance(body, dict):
            d = body
            trace = d.get("trace", {})
            return {
                "post_id": post_id,             # the requested (session-label) post
                "toggled_post_id": d.get("toggled_post_id"),  # E17: which post the model actually tapped
                "before_count": d.get("before_count"),
                "after_count": d.get("after_count"),
                "operator": operator,
                "http_status": 200,
                "success": True,
                "elapsed_s": elapsed,
                "steps": trace.get("steps"),
                "done": trace.get("done"),
                "actions": [a.get("desc", "") for a in trace.get("actions", [])],
                "instruction_source": "governance_driver" if use_driver else "bridge_fstring",
                "driver_nl": (driver_nl or "")[:200],
                "error": None,
            }
        else:
            # HTTP 500 = the toggle didn't land (bridge RuntimeError) or
            # the model didn't finish in max_steps; 409 = honest irreversible
            if isinstance(body, dict):
                err_msg = str(body.get("detail", body))[:300]
            else:
                err_msg = str(body)[:300]
            return {
                "post_id": post_id,
                "toggled_post_id": None,
                "before_count": None,
                "after_count": None,
                "operator": operator,
                "http_status": status,
                "success": False,
                "elapsed_s": elapsed,
                "steps": None,
                "done": None,
                "actions": [],
                "instruction_source": "governance_driver" if use_driver else "bridge_fstring",
                "driver_nl": (driver_nl or "")[:200],
                "error": err_msg,
            }
    except Exception as e:
        elapsed = round(time.time() - t0, 1)
        return {
            "post_id": post_id,
            "toggled_post_id": None,
            "before_count": None,
            "after_count": None,
            "operator": operator,
            "http_status": 0,
            "success": False,
            "elapsed_s": elapsed,
            "steps": None,
            "done": None,
            "actions": [],
            "instruction_source": "governance_driver" if use_driver else "bridge_fstring",
            "driver_nl": (driver_nl or "")[:200],
            "error": f"{type(e).__name__}: {e}",
        }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="X app toggle kill-test (VM-moment existence proof)")
    parser.add_argument("--samples", type=int, default=3,
                        help="number of rounds (each round tests all posts × operators)")
    parser.add_argument("--posts", nargs="+", default=DEFAULT_POSTS,
                        help="post ids to test (default: first 3 visible posts)")
    parser.add_argument("--operators", nargs="+", default=OPERATORS,
                        help="toggle operators to test")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--out", default=None)
    parser.add_argument("--use-driver", action="store_true",
                        help="E17-B: route the CUA instruction through "
                             "ScriptedUserDriver + GovernanceInterpreter "
                             "(instruction_override) — the full user-behavior → "
                             "governance → CUA pipeline. Default off (bridge "
                             "f-string, zero regression).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    if not _health_check(args.host):
        sys.exit(2)

    ts = time.strftime("%Y%m%d_%H%M%S")
    all_samples = []
    n_total = 0
    n_success = 0

    for sample_i in range(args.samples):
        logger.info(f"=== sample {sample_i + 1}/{args.samples} ===")
        for post_id in args.posts:
            for operator in args.operators:
                sid = f"xkill_{sample_i}_{post_id}_{operator}_{int(time.time()) % 100000}"
                # reset the sim for each test (clean state)
                # Agent B (substrate isolation): reset moved behind the
                # MobileGymEvaluationEnvironment (setup plane, not a write).
                try:
                    from taskvm.substrate.mobilegym.evaluation import (
                        MobileGymEvaluationEnvironment)
                    MobileGymEvaluationEnvironment(
                        "x", sid, f"http://{args.host}:{BRIDGE_PORT}",
                        timeout=30).reset(sid)
                except Exception:
                    pass
                logger.info(f"  {operator} on {post_id} ...")
                rec = _run_one_toggle(args.host, sid, post_id, operator,
                                      use_driver=args.use_driver)
                rec["sample"] = sample_i
                rec["sid"] = sid
                all_samples.append(rec)
                n_total += 1
                if rec["success"]:
                    n_success += 1
                    logger.info(f"    ✓ SUCCESS steps={rec['steps']} "
                                f"elapsed={rec['elapsed_s']}s")
                else:
                    logger.warning(f"    ✗ FAIL http={rec['http_status']} "
                                   f"err={rec['error'][:80]}")

    success_rate = round(n_success / n_total, 4) if n_total else 0.0
    # per-operator breakdown
    per_op: dict[str, dict] = {}
    for op in args.operators:
        op_samples = [s for s in all_samples if s["operator"] == op]
        op_success = sum(1 for s in op_samples if s["success"])
        per_op[op] = {
            "n": len(op_samples),
            "n_success": op_success,
            "success_rate": round(op_success / len(op_samples), 4) if op_samples else 0.0,
        }

    PASS_THRESHOLD = 0.8  # kept at the E14/E15 bar — NOT lowered for E16.
    # Pure-vision CUA is expected to score LOWER than the backdoor-assisted
    # E15 run (94.4%); a FAIL here is honest, not a regression to mask.
    passed = success_rate >= PASS_THRESHOLD

    # per-post breakdown — under E17-A Option B the verifier is any_new, so
    # per_post by REQUESTED post_id is no longer a pass/fail discriminator
    # (any post toggled = success). We keep it for continuity but it now just
    # shows the success rate is uniform across requested labels (sanity).
    per_post: dict[str, dict] = {}
    for pid in args.posts:
        ps = [s for s in all_samples if s["post_id"] == pid]
        ps_ok = sum(1 for s in ps if s["success"])
        per_post[pid] = {
            "n": len(ps),
            "n_success": ps_ok,
            "success_rate": round(ps_ok / len(ps), 4) if ps else 0.0,
        }

    # E17-A Option B: the MEANINGFUL per-post signal — which post did the
    # model ACTUALLY tap (toggled_post_id)? A strong position bias (always
    # post[0], never post[2]) would show the model isn't discriminating, even
    # though any_new scores it PASS. This is descriptive (not scored) — it
    # surfaces whether the CUA explores or always picks the top post.
    per_toggled_post: dict[str, int] = {}
    for s in all_samples:
        tp = s.get("toggled_post_id")
        if tp:
            per_toggled_post[tp] = per_toggled_post.get(tp, 0) + 1

    report = {
        "ts": ts,
        "test": "x_toggle_killtest",
        "user_behavior_driver": "scripted" if args.use_driver else "none",
        "governance_interpreter": "dynamic" if args.use_driver else "none",
        "instruction_source": "governance_driver" if args.use_driver else "bridge_fstring",
        "e17_note": (
            "E17-A Option B (2026-08-12): verifier now ALIGNED with the "
            "any-post instruction (verify_mode='any_new' — bridge checks the "
            "toggle list grew, not that a specific post_id entered). Resolves "
            "the .mrules E17 §0-B ill-posed-task contradiction (instruction "
            "said 'any untoggled post' but verifier required a specific post). "
            "The before-snapshot is SERVER-SIDE only (verification) — NEVER "
            "leaked to the prompt (that was the E16 bug; this does not repeat "
            "it). NOT comparable to E16's 38.9% (that was the ill-posed "
            "specific-post verifier on an any-post instruction — a 1/3-guess "
            "lottery). This number measures 'can the CUA toggle ANY post'."),
        "description": (
            "X app toggle (like/retweet/bookmark) via gui_act_async — E16-"
            "complete pure-vision + E17-A Option B any-new-post verifier. The "
            "easy aligned baseline; the strong discriminating task is MG-1 "
            "social_morning_brief (visible-uniqueness)."),
        "posts_tested": args.posts,
        "operators_tested": args.operators,
        "n_samples_per_post_op": args.samples,
        "n_total": n_total,
        "n_success": n_success,
        "success_rate": success_rate,
        "pass_threshold": PASS_THRESHOLD,
        "PASS": passed,
        "per_operator": per_op,
        "per_post": per_post,
        "per_toggled_post": per_toggled_post,
        "samples": all_samples,
        "honest_framing": {
            "what_PASS_means": (
                ">=80% of toggle operations succeeded — the model can find "
                "an un-toggled post from the screenshot alone, locate the "
                "correct action icon, and tap it precisely enough for SOME "
                "post's store toggle to fire. VM MOMENT (aligned baseline): "
                "the TaskVM grounding loop drives a real MobileGym app write "
                "via vision + gestures, no set_state backdoor, no "
                "content_hint backdoor, AND the verifier now matches the "
                "instruction semantics (E17-A Option B)."),
            "what_FAIL_means": (
                "<80% success means the model could not reliably toggle ANY "
                "post — this IS a real CUA/grounding signal now (unlike the "
                "pre-E17 ill-posed version where failure was 1/3-guess "
                "luck). Likely causes: icon mis-localization, tap-coordinate "
                "calibration, or the model not finding an un-toggled post. "
                "Check per_operator to see if one icon type fails more."),
            "per_toggled_post_interpretation": (
                "Shows WHICH post the model actually tapped. If one post "
                "dominates (e.g. always post[0]) the model has a position "
                "bias — it passes any_new but isn't discriminating content. "
                "A balanced distribution is stronger evidence of real visual "
                "grounding. This is descriptive, not scored."),
            "caveats": (
                "Only tests posts visible without scrolling. The strong "
                "discriminating task (MG-1, visible-uniqueness by content) "
                "is in run_mg_vm_killtest, not here. This killtest is the "
                "easy baseline — do not over-claim from a high score here."),
        },
    }

    out_path = Path(args.out) if args.out else EVAL_DIR / f"x_toggle_killtest_{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"\nWrote {out_path}")
    print(f"\n=== X TOGGLE KILL-TEST: {'PASS' if passed else 'FAIL'} ===")
    print(f"  success rate: {n_success}/{n_total} = {success_rate:.1%} "
          f"(threshold {PASS_THRESHOLD:.0%})  [E17-A Option B: any-new-post "
          f"verifier aligned with any-post instruction]")
    for op, stats in per_op.items():
        print(f"  {op}: {stats['n_success']}/{stats['n']} = {stats['success_rate']:.1%}")
    print(f"  per_toggled_post (which post the model ACTUALLY tapped):")
    for pid, cnt in sorted(per_toggled_post.items()):
        print(f"    {pid}: {cnt}")
    if passed:
        print(f"\n  VM MOMENT (aligned baseline): the TaskVM grounding loop drives")
        print(f"  MobileGym X app toggle writes via vision + gestures — verifier")
        print(f"  now matches the any-post instruction (E17-A Option B).")
    else:
        print(f"\n  [honest: under Option B, failure is a REAL CUA signal (not the")
        print(f"   pre-E17 1/3-guess lottery). Check per_operator for icon-specific")
        print(f"   failures.]")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
