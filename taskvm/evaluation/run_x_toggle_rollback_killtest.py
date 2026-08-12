"""X toggle ROLLBACK kill-test (Task E, .mrules E15).

E14's killtest only tested the WRITE path (toggle on). But toggle is NATURALLY
REVERSIBLE — tapping a liked heart again unlikes it. This is the IDEAL rollback
test case for MobileGym (vs. wechat send_message which is honestly irreversible).

What this tests:
  1. WRITE: toggle_like(post) → post appears in likedPostIds
  2. ROLLBACK: toggle_like(post) again → post REMOVED from likedPostIds
  3. VERIFY: post is NOT in likedPostIds after rollback (trusted get_state read)

This proves the TaskVM grounding loop can drive a REAL reversible compensation
on MobileGym — not just "honest irreversibility" (wechat) but actual
reversibility via GUI gestures (tap again to undo).

Lands a PERSISTED JSON artifact at
``eval_results/x_toggle_rollback_killtest_<ts>.json``.

Usage:
    # bridge (:3019) + Vite (:3000) must be running first
    python -m taskvm.evaluation.run_x_toggle_rollback_killtest --samples 2
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
DEFAULT_POSTS = [
    "p_1879539450872778943",
    "p_1879539026291785845",
    "p_1879526642210808148",
]
OPERATORS = ["toggle_like", "toggle_retweet", "toggle_bookmark"]


def _health_check(host: str) -> bool:
    try:
        r = requests.get(f"http://{host}:{BRIDGE_PORT}/health", timeout=5)
        if r.status_code != 200 or r.json().get("status") != "ok":
            return False
    except Exception:
        return False
    try:
        r = requests.get(f"http://{host}:{VITE_PORT}", timeout=4)
        if r.status_code != 200:
            return False
    except Exception:
        return False
    logger.info(f"bridge :{BRIDGE_PORT} + Vite :{VITE_PORT} healthy")
    return True


def _do_toggle(host: str, sid: str, post_id: str, operator: str,
               timeout: int = 180) -> dict:
    """One toggle call via the bridge. Returns the result record."""
    url = f"http://{host}:{BRIDGE_PORT}/api/x/{sid}/{post_id}"
    payload = {"operator": operator, "value": True}
    t0 = time.time()
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        elapsed = round(time.time() - t0, 1)
        if r.status_code == 200:
            d = r.json()
            trace = d.get("trace", {})
            return {
                "http_status": 200,
                "success": True,
                "elapsed_s": elapsed,
                "steps": trace.get("steps"),
                "actions": [a.get("desc", "") for a in trace.get("actions", [])],
                "error": None,
            }
        else:
            try:
                err_msg = str(r.json().get("detail", r.json()))[:300]
            except Exception:
                err_msg = r.text[:300]
            return {
                "http_status": r.status_code,
                "success": False,
                "elapsed_s": elapsed,
                "steps": None,
                "actions": [],
                "error": err_msg,
            }
    except Exception as e:
        return {
            "http_status": 0,
            "success": False,
            "elapsed_s": round(time.time() - t0, 1),
            "steps": None,
            "actions": [],
            "error": f"{type(e).__name__}: {e}",
        }


def _read_x_state(host: str, sid: str) -> dict:
    """Read the X app's toggle lists via the bridge's read-only
    ``/api/x_state/<sid>`` route (added Task E, .mrules E15 — a plain
    ``get_state`` read, same trusted path ``mutate_x`` itself verifies
    against; no set_state, does not touch the non-invasive boundary)."""
    try:
        r = requests.get(
            f"http://{host}:{BRIDGE_PORT}/api/x_state/{sid}", timeout=30)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def _check_post_in_list(x_state: dict, operator: str, post_id: str) -> bool:
    """Check if post_id is in the toggle list for this operator."""
    field_map = {
        "toggle_like": "likedPostIds",
        "toggle_retweet": "retweetedPostIds",
        "toggle_bookmark": "bookmarkedPostIds",
    }
    ids_list = x_state.get(field_map[operator], []) or []
    return post_id in ids_list


def _run_one_rollback(host: str, sid: str, post_id: str, operator: str) -> dict:
    """Run a full write→rollback→verify cycle for one post+operator.

    Steps:
      0. Reset sim (clean state)
      1. Read initial state — verify post is NOT in the list
      2. WRITE: toggle(post) — should ADD post to the list
      3. Verify post IS in the list (write landed)
      4. ROLLBACK: toggle(post) again — should REMOVE post from the list
      5. Verify post is NOT in the list (rollback landed)
    """
    # 0. reset
    try:
        requests.post(
            f"http://{host}:{BRIDGE_PORT}/api/reset/{sid}", timeout=30)
    except Exception:
        pass

    # 1. initial state
    state0 = _read_x_state(host, sid)
    initially_in = _check_post_in_list(state0, operator, post_id)

    # 2. WRITE
    write_result = _do_toggle(host, sid, post_id, operator)

    # 3. verify write landed
    state1 = _read_x_state(host, sid)
    after_write_in = _check_post_in_list(state1, operator, post_id)
    write_verified = after_write_in and not initially_in

    # 4. ROLLBACK (toggle again)
    rollback_result = _do_toggle(host, sid, post_id, operator)

    # 5. verify rollback landed
    state2 = _read_x_state(host, sid)
    after_rollback_in = _check_post_in_list(state2, operator, post_id)
    rollback_verified = not after_rollback_in

    return {
        "post_id": post_id,
        "operator": operator,
        "initially_in_list": initially_in,
        "write": {
            "success": write_result["success"],
            "http_status": write_result["http_status"],
            "elapsed_s": write_result["elapsed_s"],
            "steps": write_result["steps"],
            "error": write_result["error"],
        },
        "write_verified": write_verified,
        "after_write_in_list": after_write_in,
        "rollback": {
            "success": rollback_result["success"],
            "http_status": rollback_result["http_status"],
            "elapsed_s": rollback_result["elapsed_s"],
            "steps": rollback_result["steps"],
            "error": rollback_result["error"],
        },
        "rollback_verified": rollback_verified,
        "after_rollback_in_list": after_rollback_in,
        "overall_success": write_verified and rollback_verified,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="X toggle ROLLBACK kill-test (Task E, .mrules E15)")
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--posts", nargs="+", default=DEFAULT_POSTS)
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
    n_write_verified = 0
    n_rollback_verified = 0
    n_overall_success = 0

    for sample_i in range(args.samples):
        logger.info(f"=== sample {sample_i + 1}/{args.samples} ===")
        for post_id in args.posts:
            for operator in args.operators:
                sid = f"xrb_{sample_i}_{post_id}_{operator}_{int(time.time()) % 100000}"
                logger.info(f"  {operator} on {post_id} (write→rollback→verify) ...")
                rec = _run_one_rollback(args.host, sid, post_id, operator)
                rec["sample"] = sample_i
                rec["sid"] = sid
                all_samples.append(rec)
                n_total += 1
                # NOTE (Task E fix, .mrules E15): count against the
                # INDEPENDENTLY VERIFIED state (write_verified/
                # rollback_verified, from this script's own get_state check
                # via _check_post_in_list), NOT the bridge's HTTP status
                # code (rec["write"]["success"] / rec["rollback"]["success"]).
                # These can legitimately disagree, e.g. the bridge used to
                # have a bug where a rollback that genuinely landed (post
                # correctly left the list) still returned HTTP 500 because
                # of a stale one-directional check — see mobilegym_bridge.py
                # mutate_x's now_in_list/expected_in_list comment. Trusting
                # HTTP status alone would have UNDER-counted a real success.
                if rec["write_verified"]:
                    n_write_verified += 1
                if rec["rollback_verified"]:
                    n_rollback_verified += 1
                if rec["overall_success"]:
                    n_overall_success += 1
                    logger.info(f"    ✓ OVERALL SUCCESS "
                                f"write={rec['write']['steps']}steps "
                                f"rollback_http={rec['rollback']['success']} "
                                f"rollback_verified={rec['rollback_verified']}")
                else:
                    logger.warning(f"    ✗ FAIL write_ok={rec['write_verified']} "
                                   f"rollback_ok={rec['rollback_verified']} "
                                   f"write_http={rec['write']['success']} "
                                   f"rollback_http={rec['rollback']['success']}")

    overall_rate = round(n_overall_success / n_total, 4) if n_total else 0.0
    write_rate = round(n_write_verified / n_total, 4) if n_total else 0.0
    rollback_rate = round(n_rollback_verified / n_total, 4) if n_total else 0.0

    per_op: dict[str, dict] = {}
    for op in args.operators:
        op_samples = [s for s in all_samples if s["operator"] == op]
        op_overall = sum(1 for s in op_samples if s["overall_success"])
        per_op[op] = {
            "n": len(op_samples),
            "n_overall_success": op_overall,
            "overall_rate": round(op_overall / len(op_samples), 4) if op_samples else 0.0,
        }

    PASS_THRESHOLD = 0.8
    passed = overall_rate >= PASS_THRESHOLD

    report = {
        "ts": ts,
        "test": "x_toggle_rollback_killtest",
        "description": (
            "X toggle ROLLBACK kill-test (Task E, .mrules E15). Tests the "
            "FULL write→rollback→verify cycle: toggle on (write), then toggle "
            "off (rollback), then verify the post is back to its initial "
            "state. This is the FIRST MobileGym REVERSIBLE compensation test "
            "(vs. wechat send_message which is honestly irreversible)."),
        "posts_tested": args.posts,
        "operators_tested": args.operators,
        "n_samples": args.samples,
        "n_total": n_total,
        "n_write_verified": n_write_verified,
        "n_rollback_verified": n_rollback_verified,
        "n_overall_success": n_overall_success,
        "write_success_rate": write_rate,
        "rollback_success_rate": rollback_rate,
        "overall_success_rate": overall_rate,
        "pass_threshold": PASS_THRESHOLD,
        "PASS": passed,
        "per_operator": per_op,
        "samples": all_samples,
        "honest_framing": {
            "what_PASS_means": (
                "≥80% overall success (write AND rollback both verified via "
                "trusted get_state reads). This proves the TaskVM grounding "
                "loop can drive REAL reversible compensation on MobileGym — "
                "toggle on via GUI gesture, then toggle off via GUI gesture, "
                "with independent state verification at each step."),
            "what_this_proves_vs_wechat": (
                "wechat send_message: HONEST IRREVERSIBILITY (409, "
                "message_still_there, fidelity=0.0). X toggle: REAL "
                "REVERSIBILITY (toggle off lands, post removed from list, "
                "verified). Together they span the full reversibility "
                "spectrum — TaskVM can both honestly report irreversibility "
                "(wechat) and achieve real reversibility (X toggle)."),
            "caveats": (
                "Only tests the first 3 posts (no scrolling). Toggle is "
                "naturally reversible (tap again = undo), so this is the "
                "EASIEST rollback case — not representative of all write "
                "operations. wechat send_message remains honestly "
                "irreversible (no delete/recall UI)."),
        },
    }

    out_path = Path(args.out) if args.out else EVAL_DIR / f"x_toggle_rollback_killtest_{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"\nWrote {out_path}")
    print(f"\n=== X TOGGLE ROLLBACK KILL-TEST: {'PASS' if passed else 'FAIL'} ===")
    print(f"  write verified:      {n_write_verified}/{n_total} = {write_rate:.1%}")
    print(f"  rollback verified:   {n_rollback_verified}/{n_total} = {rollback_rate:.1%}")
    print(f"  OVERALL (w&rb):     {n_overall_success}/{n_total} = {overall_rate:.1%} "
          f"(threshold {PASS_THRESHOLD:.0%})")
    for op, stats in per_op.items():
        print(f"  {op}: {stats['n_overall_success']}/{stats['n']} = {stats['overall_rate']:.1%}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
