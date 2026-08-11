"""X app toggle kill-test — the VM-moment existence proof for MobileGym (E14).

This is the FIRST non-wechat MobileGym write-path kill-test. X's
``toggleLike`` / ``toggleRetweet`` / ``toggleBookmark`` are DISCRETE one-click
writes (vs. wechat's type+send sequence), so they're the natural existence
proof that the TaskVM grounding loop (``gui_act_async``) can drive MobileGym
when the harness coordinate pipeline is correct.

What this tests:
  - **toggle_like**: can the model find a post by its content text (from
    ``posts.json``), locate the heart icon in the action bar, and tap it
    precisely enough for ``toggleLike(postId)`` to fire?
  - **toggle_retweet / toggle_bookmark**: same, for the repost/bookmark icons.
  - **success criterion**: the post id appears in ``likedPostIds`` /
    ``retweetedPostIds`` / ``bookmarkedPostIds`` after the gesture loop
    (trusted ``get_state`` read — NOT a screenshot heuristic).

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
# Posts visible on the X timeline without scrolling (the first 3 in
# posts.json — chosen because they're immediately visible, so the test
# measures "can the model find + tap the right icon" not "can it scroll").
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


def _run_one_toggle(host: str, sid: str, post_id: str, operator: str) -> dict:
    """Run one toggle operation via the bridge and return the result record.

    Each call: reset → toggle → record HTTP status + trace + verification.
    The bridge's ``mutate_x`` already verifies the toggle landed via
    ``get_state`` (trusted read path), so HTTP 200 = success.
    """
    url = f"http://{host}:{BRIDGE_PORT}/api/x/{sid}/{post_id}"
    payload = {"operator": operator, "value": True}
    t0 = time.time()
    try:
        r = requests.post(url, json=payload, timeout=180)
        elapsed = round(time.time() - t0, 1)
        if r.status_code == 200:
            d = r.json()
            trace = d.get("trace", {})
            return {
                "post_id": post_id,
                "operator": operator,
                "http_status": 200,
                "success": True,
                "elapsed_s": elapsed,
                "steps": trace.get("steps"),
                "done": trace.get("done"),
                "actions": [a.get("desc", "") for a in trace.get("actions", [])],
                "error": None,
            }
        else:
            # HTTP 500 = the toggle didn't land (bridge RuntimeError) or
            # the model didn't finish in max_steps
            try:
                err_body = r.json()
                err_msg = str(err_body.get("detail", err_body))[:300]
            except Exception:
                err_msg = r.text[:300]
            return {
                "post_id": post_id,
                "operator": operator,
                "http_status": r.status_code,
                "success": False,
                "elapsed_s": elapsed,
                "steps": None,
                "done": None,
                "actions": [],
                "error": err_msg,
            }
    except Exception as e:
        elapsed = round(time.time() - t0, 1)
        return {
            "post_id": post_id,
            "operator": operator,
            "http_status": 0,
            "success": False,
            "elapsed_s": elapsed,
            "steps": None,
            "done": None,
            "actions": [],
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
                try:
                    requests.post(
                        f"http://{args.host}:{BRIDGE_PORT}/api/reset/{sid}",
                        timeout=30)
                except Exception:
                    pass
                logger.info(f"  {operator} on {post_id} ...")
                rec = _run_one_toggle(args.host, sid, post_id, operator)
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

    PASS_THRESHOLD = 0.8  # ≥80% success = PASS (VM moment achieved)
    passed = success_rate >= PASS_THRESHOLD

    report = {
        "ts": ts,
        "test": "x_toggle_killtest",
        "description": (
            "X app toggle (like/retweet/bookmark) via gui_act_async — the "
            "FIRST non-wechat MobileGym write path. Proves the TaskVM "
            "grounding loop can drive MobileGym when the harness coordinate "
            "pipeline (env.step + norm_0_1000) and content-based instruction "
            "(posts.json content_hint) are correct."),
        "posts_tested": args.posts,
        "operators_tested": args.operators,
        "n_samples_per_post_op": args.samples,
        "n_total": n_total,
        "n_success": n_success,
        "success_rate": success_rate,
        "pass_threshold": PASS_THRESHOLD,
        "PASS": passed,
        "per_operator": per_op,
        "samples": all_samples,
        "honest_framing": {
            "what_PASS_means": (
                "≥80% of toggle operations succeeded — the model can find a "
                "post by its content text, locate the correct action icon, "
                "and tap it precisely enough for the store toggle to fire. "
                "This is a VM MOMENT: the TaskVM grounding loop drives a "
                "real MobileGym app write via vision + gestures, with no "
                "set_state backdoor."),
            "what_FAIL_means": (
                "<80% success — the grounding loop or harness pipeline has "
                "issues. Check: coordinate calibration, content_hint "
                "matching, max_steps (scroll-needed posts), instruction "
                "clarity."),
            "caveats": (
                "Only tests posts visible without scrolling (the first 3 in "
                "posts.json). Posts requiring scroll may need higher "
                "max_steps. The DOM content reader (_flatten_x_posts_async) "
                "has a closest-selector bug producing wrong content previews "
                "for the observation path — but the WRITE path uses "
                "posts.json directly, so this doesn't affect toggle success."),
        },
    }

    out_path = Path(args.out) if args.out else EVAL_DIR / f"x_toggle_killtest_{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"\nWrote {out_path}")
    print(f"\n=== X TOGGLE KILL-TEST: {'PASS' if passed else 'FAIL'} ===")
    print(f"  success rate: {n_success}/{n_total} = {success_rate:.1%} "
          f"(threshold {PASS_THRESHOLD:.0%})")
    for op, stats in per_op.items():
        print(f"  {op}: {stats['n_success']}/{stats['n']} = {stats['success_rate']:.1%}")
    if passed:
        print(f"\n  VM MOMENT: the TaskVM grounding loop drives MobileGym X app")
        print(f"  toggle writes via vision + gestures — no set_state backdoor.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
