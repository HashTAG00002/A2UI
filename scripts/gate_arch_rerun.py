#!/usr/bin/env python
"""GATE-ARCH rerun driver — replay the demo-baseline goals against a
running TaskVM APP instance and record architect-stage survival.

Methodology (mirrors eval_results/taskvm_demo_run_20260819):
  1. inject each goal verbatim through the PUBLIC HTTP route
     (POST /api/app/goals — the same route the browser UI uses);
  2. poll the goal record until `ready` (composition passed: StateCompiler
     + TaskArchitect products accepted by the validating constructors) or
     `failed` (honest ArchitectOutputError after the bounded repair);
  3. a `ready` goal gets the PUBLIC governance start; survival is
     confirmed by the FIRST archived CUA call (the work order reached
     the executor), then the session is stopped (a legal user gesture);
  4. every model call lands in the call archive verbatim
     (TASKVM_CALL_ARCHIVE_DIR on the APP process) — this driver only
     reads it, never writes it.

Usage:
  python scripts/gate_arch_rerun.py --app http://127.0.0.1:3026 \
      --sid archgate --archive eval_results/arch_gate_rerun_20260819

Pass criterion (W0.2 card): >=5/6 goals survive the architect stage.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

# The six goals, VERBATIM from eval_results/taskvm_demo_run_20260819/
# 00_SESSION.txt (same order as the baseline session).
GOALS = [
    ("goal-1", "打开支付宝查看最近的账单，找出最大的一笔支出金额和收款方，"
               "然后打开微信把这个信息发给黄勇"),
    ("goal-2", "在支付宝的账单里找出金额最大的一笔支出，把这笔支出的金额和"
               "收款方记录下来，然后打开微信，把这笔最大支出的金额和收款方"
               "发给黄勇"),
    ("goal-1", "从支付宝最近账单的交易记录里找出金额最大的那笔支出，记下这笔"
               "支出的金额和收款方，然后在微信里给黄勇发一条消息，汇报这笔"
               "最大支出的金额和收款方"),
    ("goal-2", "看一下支付宝最近30天支出最高的3笔，把金额发给微信里的黄勇，"
               "提醒他省着点。"),
    ("goal-3", "给微信里的黄勇发一条消息，内容是：明天上午十点开会"),
    ("goal-4", "给微信里的黄勇发一条消息，内容是：明天上午十点开会，发完确认"
               "消息已经发送成功"),
]


def _req(method: str, url: str, body: dict | None = None,
         timeout: int = 30) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return {"_http_status": e.code,
                    "_body": e.read().decode("utf-8", "replace")}
        except Exception:
            return {"_http_status": e.code}


def _cua_calls_in_archive(archive_dir: str) -> int:
    try:
        with open(f"{archive_dir}/INDEX.txt", encoding="utf-8") as f:
            return sum(1 for line in f if "| cua |" in line)
    except FileNotFoundError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="http://127.0.0.1:3026")
    ap.add_argument("--sid", default="archgate")
    ap.add_argument("--archive", required=True,
                    help="the APP's TASKVM_CALL_ARCHIVE_DIR (read-only)")
    ap.add_argument("--goal-timeout", type=float, default=600.0)
    ap.add_argument("--cua-timeout", type=float, default=300.0)
    args = ap.parse_args()

    results = []
    for i, (goal_id, goal) in enumerate(GOALS, start=1):
        rec = {"seq": i, "goal_id": goal_id, "goal": goal,
               "survived_architect": False, "cua_entry": False}
        t0 = time.time()
        r = _req("POST", f"{args.app}/api/app/goals",
                 {"goal": goal, "app": "wechat"})
        if not r.get("ok"):
            rec["status"] = "inject_failed"
            rec["error"] = json.dumps(r, ensure_ascii=False)[:500]
            results.append(rec)
            continue
        gid = r["goal"]["goal_id"]
        rec["gid"] = gid
        # poll until ready / failed
        status = "bootstrapping"
        deadline = time.time() + args.goal_timeout
        while time.time() < deadline:
            g = _req("GET", f"{args.app}/api/app/goals/{gid}")
            status = (g.get("goal") or {}).get("status", "unknown")
            if status in ("ready", "failed"):
                break
            time.sleep(2.0)
        rec["status"] = status
        rec["model_calls_at_ready"] = \
            (g.get("goal") or {}).get("model_calls")
        rec["error"] = (g.get("goal") or {}).get("error")
        if status == "ready":
            rec["survived_architect"] = True
            # CUA entry: public governance start → first archived cua row
            cua0 = _cua_calls_in_archive(args.archive)
            s = _req("POST",
                     f"{args.app}/api/sessions/{args.sid}/governance/start")
            rec["start_ok"] = bool(s.get("ok", s.get("_http_status") == 200))
            deadline = time.time() + args.cua_timeout
            while time.time() < deadline:
                if _cua_calls_in_archive(args.archive) > cua0:
                    rec["cua_entry"] = True
                    break
                time.sleep(3.0)
            _req("POST",
                 f"{args.app}/api/sessions/{args.sid}/governance/stop")
        rec["elapsed_s"] = round(time.time() - t0, 1)
        rec["cua_calls_total"] = _cua_calls_in_archive(args.archive)
        results.append(rec)
        print(json.dumps(rec, ensure_ascii=False), flush=True)

    survived = sum(1 for r in results if r["survived_architect"])
    cua = sum(1 for r in results if r["cua_entry"])
    summary = {
        "gate": "GATE-ARCH",
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pass_criterion": ">=5/6 survive architect stage",
        "survived_architect": f"{survived}/6",
        "cua_entry": f"{cua}/6",
        "verdict": "PASS" if survived >= 5 else "FAIL",
        "goals": results,
    }
    out = f"{args.archive}/GATE_ARCH_RESULT.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in summary.items()
                      if k != "goals"}, ensure_ascii=False))
    print(f"written: {out}")
    return 0 if survived >= 5 else 1


if __name__ == "__main__":
    raise SystemExit(main())
