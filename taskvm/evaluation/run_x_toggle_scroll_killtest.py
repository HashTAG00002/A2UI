"""X toggle kill-test for posts requiring SCROLL (Task D, .mrules E15).

E14's killtest only tested the first 3 posts (immediately visible, no scroll).
This script tests posts at indices 4-8 in posts.json — these require scrolling
down the timeline to find them. This measures whether the grounding loop can
handle scroll-needed posts, or if the 94.4% success rate was specific to
"posts visible without scrolling."

What this tests:
  - Can the model scroll the timeline to find a post that's not initially visible?
  - Does the instruction's "If the post is not visible, scroll to find it" guidance
    actually work with the current max_steps (25)?
  - Is the success rate for scroll-needed posts significantly lower than 94.4%?

Lands a PERSISTED JSON artifact at
``eval_results/x_toggle_scroll_killtest_<ts>.json``.

Usage:
    # bridge (:3019) + Vite (:3000) must be running first
    python -m taskvm.evaluation.run_x_toggle_scroll_killtest --samples 2
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
# Posts at indices 4-8 in posts.json — these are BELOW the fold and require
# scrolling down the timeline to find. (Index 3 is "p5" — a special test post
# with a different id format, skipped to avoid edge cases.)
SCROLL_POSTS = [
    "p_2011675966666653759",  # index 4
    "p_2011668857447203168",  # index 5
    "p_2011650110065881153",  # index 6
]
OPERATORS = ["toggle_like", "toggle_retweet", "toggle_bookmark"]


def _health_check(host: str) -> bool:
    try:
        r = requests.get(f"http://{host}:{BRIDGE_PORT}/health", timeout=5)
        if r.status_code != 200 or r.json().get("status") != "ok":
            logger.error(f"bridge :{BRIDGE_PORT} unhealthy")
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
    url = f"http://{host}:{BRIDGE_PORT}/api/x/{sid}/{post_id}"
    payload = {"operator": operator, "value": True}
    t0 = time.time()
    try:
        r = requests.post(url, json=payload, timeout=300)
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
        description="X toggle kill-test for SCROLL-NEEDED posts (Task D)")
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--posts", nargs="+", default=SCROLL_POSTS)
    parser.add_argument("--operators", nargs="+", default=OPERATORS)
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
                sid = f"xscroll_{sample_i}_{post_id}_{operator}_{int(time.time()) % 100000}"
                try:
                    requests.post(
                        f"http://{args.host}:{BRIDGE_PORT}/api/reset/{sid}",
                        timeout=30)
                except Exception:
                    pass
                logger.info(f"  {operator} on {post_id} (scroll-needed) ...")
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
    per_op: dict[str, dict] = {}
    for op in args.operators:
        op_samples = [s for s in all_samples if s["operator"] == op]
        op_success = sum(1 for s in op_samples if s["success"])
        per_op[op] = {
            "n": len(op_samples),
            "n_success": op_success,
            "success_rate": round(op_success / len(op_samples), 4) if op_samples else 0.0,
        }

    PASS_THRESHOLD = 0.8
    passed = success_rate >= PASS_THRESHOLD

    report = {
        "ts": ts,
        "test": "x_toggle_scroll_killtest",
        "description": (
            "X toggle kill-test for posts that require SCROLLING to find "
            "(Task D, .mrules E15). Tests whether the 94.4% success rate "
            "from E14 (first 3 posts, no scroll) holds for posts further "
            "down the timeline that need scroll-to-find."),
        "posts_tested": args.posts,
        "post_indices_in_postsjson": "4-6 (below the fold, scroll-needed)",
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
                "≥80% success on scroll-needed posts — the grounding loop can "
                "scroll to find posts and the 94.4% rate generalizes beyond "
                "the first 3 visible posts."),
            "what_FAIL_means": (
                "<80% success — scroll-needed posts are harder. This would "
                "bound the E14 result's generalizability: '94.4% only for "
                "immediately visible posts, lower for scroll-needed ones.'"),
            "comparison_to_e14": (
                "E14 killtest (first 3 posts, no scroll): 94.4% (17/18). "
                "This test: scroll-needed posts (indices 4-6). If this is "
                "significantly lower, it means the scroll instruction + "
                "max_steps=25 are insufficient for scroll-needed posts."),
        },
    }

    out_path = Path(args.out) if args.out else EVAL_DIR / f"x_toggle_scroll_killtest_{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"\nWrote {out_path}")
    print(f"\n=== X TOGGLE SCROLL KILL-TEST: {'PASS' if passed else 'FAIL'} ===")
    print(f"  success rate: {n_success}/{n_total} = {success_rate:.1%} "
          f"(threshold {PASS_THRESHOLD:.0%})")
    for op, stats in per_op.items():
        print(f"  {op}: {stats['n_success']}/{stats['n']} = {stats['success_rate']:.1%}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
