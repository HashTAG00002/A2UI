#!/usr/bin/env python
"""SKILL-LADDER L0 driver — the six GATE-ARCH demo goals, FULL trajectories.

The R2.5 Skill-Ladder's L0 rung (bench_design §17.2): simple trajectories,
NO interventions, single app, 1-3 steps, pure plumbing — replaying the six
demo-baseline goals VERBATIM (the GATE-ARCH set; no new benchmark task is
written, keeping the GATE-G0 iron rule intact).

What this driver does per phase (baseline vs with-skill):

  1. spawn an ISOLATED bridge subprocess (own port ⇒ own Playwright
     browser ⇒ own MobileGym world; other agents' stacks on
     3016/3019/3026/3029 are never touched), then activate the sid on
     the bridge's SETUP plane (POST /api/reset/<sid> — the ONE operator
     gesture the open launcher deliberately cannot do, substrate
     contract §4), then spawn the ISOLATED APP connecting to that
     already-activated bridge with ``TASKVM_CALL_ARCHIVE_DIR`` pointed
     inside the phase's output dir — every real model call lands in
     the archive verbatim;
  2. per goal (the six, in the GATE-ARCH order): inject through the PUBLIC
     route → poll to ready/failed (architect survival) → PUBLIC governance
     start → poll to the runtime TERMINAL state (autonomy bar + CUA
     ledger counts + a quiet window; honest `timeout` when neither) →
     PUBLIC governance stop;
  3. record per-goal: survived_architect / cua_entry / terminal_state /
     terminal_reason / cua_calls / elapsed; the phase summary carries the
     three L0 plumbing rates (architect survival, CUA entry, terminal
     reach) — the ledger-backed evidence for the distillation A/B.

Success criteria are PUBLIC signals only (goal records, governance bar,
call archive counts) — no hidden oracle read exists on this path (the
demo goals are not TaskSpec fixtures; L0 is plumbing, judged by stage
survival, never by the frozen-task grader).

Usage:
  OPENAI_API_KEY=... python scripts/skill_ladder_l0.py \
      --phase baseline --out eval_results/skill_ladder_l0_20260820/baseline
  # the with-skill phase carries TASKVM_SKILL_INJECTION=on into the APP
  # subprocess env via --phase with_skill (no ambient export needed):
  OPENAI_API_KEY=... \
      python scripts/skill_ladder_l0.py --phase with_skill \
      --out eval_results/skill_ladder_l0_20260820/with_skill
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# The six goals, VERBATIM from scripts/gate_arch_rerun.py (which holds
# them verbatim from eval_results/taskvm_demo_run_20260819/00_SESSION.txt).
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

#: driver statuses meaning "still working" (driver.py: running/
#: compensating; governance_view: replanning during a pending
#: recompose) — every OTHER word the driver publishes (done / stopped /
#: paused / idle / step_budget / no_ready_work / no_plan /
#: budget_exhausted / blocked / escalated / error: …) is a stable
#: disposition the poller may accept, recorded verbatim
_ACTIVE_AUTONOMY = frozenset({"running", "compensating", "replanning"})


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
    except urllib.error.URLError as e:
        # refused / timeout while the APP subprocess is still booting —
        # a retry-able network state, never a driver crash (the first
        # poll of a 300s wait window routinely hits this)
        return {"_network_error": str(getattr(e, "reason", e))}


def _proc_env(archive_dir: str, skill_injection: str | None) -> dict:
    """The shared subprocess env: interpreter-derived Playwright path,
    repo .chromelibs, mobilegym on PYTHONPATH."""
    env = dict(os.environ)
    env["TASKVM_CALL_ARCHIVE_DIR"] = archive_dir
    # strip ANY ambient injection export from the driver's shell first —
    # the A/B is decided by --phase alone, never by what the shell
    # happens to carry (a leaked ambient "on" would silently void the
    # baseline arm); then re-arm the flag ONLY for the with_skill phase
    env.pop("TASKVM_SKILL_INJECTION", None)
    if skill_injection:
        env["TASKVM_SKILL_INJECTION"] = skill_injection
    py_home = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    pw = env.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if not pw and os.path.isdir(os.path.join(py_home, "opt", "ms-playwright")):
        env["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
            py_home, "opt", "ms-playwright")
    chromelibs = os.path.join(REPO_ROOT, ".chromelibs", "lib")
    if os.path.isdir(chromelibs):
        env["LD_LIBRARY_PATH"] = (
            chromelibs + os.pathsep + env.get("LD_LIBRARY_PATH", ""))
    mobilegym_dir = env.get(
        "MOBILEGYM_DIR", os.path.join(REPO_ROOT, "..", "mobilegym"))
    if os.path.isdir(os.path.join(mobilegym_dir, "bench_env")):
        env["PYTHONPATH"] = (os.path.abspath(mobilegym_dir) + os.pathsep
                             + env.get("PYTHONPATH", ""))
    return env


def _spawn_bridge(bridge_port: int, sim_url: str, env: dict,
                  log_path: str):
    """The ISOLATED bridge subprocess — its own Playwright browser is
    its own MobileGym world (other agents' bridges stay untouched)."""
    cmd = [sys.executable, "-m", "taskvm.substrate.mobilegym.bridge",
           "--port", str(bridge_port), "--sim-url", sim_url,
           "--screenshot-dir", ""]   # per-call images already land in the
    #                                APP call archive; no duplicate PNG spam
    logf = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdout=logf,
                            stderr=subprocess.STDOUT), logf


def _spawn_app(port: int, bridge_url: str, sim_url: str, sid: str,
               env: dict, model: str | None):
    """The ISOLATED APP subprocess — CONNECTS to the pre-activated
    bridge (no --start-bridge: the open launcher deliberately has no
    reset/seed power, so the sid was activated by the operator BEFORE
    the APP's read-only observe probe runs)."""
    cmd = [sys.executable, "-m", "taskvm.workspace_ui.app_open",
           "--port", str(port), "--bridge-url", bridge_url,
           "--sim-url", sim_url, "--sid", sid]
    if model:
        cmd += ["--model", model]
    log_path = os.path.join(env["TASKVM_CALL_ARCHIVE_DIR"], "..",
                            "app_process.log")
    logf = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdout=logf,
                            stderr=subprocess.STDOUT), logf


def _wait_http(url: str, timeout_s: float = 300.0) -> bool:
    """Poll until the endpoint answers with a healthy payload."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = _req("GET", url, timeout=5)
        if r.get("status") == "ok" or r.get("ok"):
            return True
        time.sleep(2.0)
    return False


def _wait_app(base: str, timeout_s: float = 300.0) -> bool:
    return _wait_http(f"{base}/api/app/status", timeout_s)


def _autonomy_of(base: str, sid: str) -> dict:
    r = _req("GET", f"{base}/api/sessions/{sid}/governance", timeout=10)
    gov = r.get("governance") if isinstance(r, dict) else None
    if not isinstance(gov, dict):
        gov = r if isinstance(r, dict) else {}
    return gov


def _cua_calls(archive_dir: str) -> int:
    try:
        with open(f"{archive_dir}/INDEX.txt", encoding="utf-8") as f:
            return sum(1 for line in f if "| cua |" in line)
    except FileNotFoundError:
        return 0


def _run_goal(base: str, sid: str, seq: int, goal_id: str, goal: str,
              archive_dir: str, *, goal_timeout_s: float,
              terminal_timeout_s: float, poll_s: float = 5.0) -> dict:
    rec = {"seq": seq, "goal_id": goal_id, "goal": goal,
           "survived_architect": False, "cua_entry": False,
           "terminal_state": "not_started", "terminal_reason": "",
           "cua_calls": 0, "elapsed_s": 0.0}
    t0 = time.time()
    r = _req("POST", f"{base}/api/app/goals", {"goal": goal})
    if not r.get("ok"):
        rec["terminal_state"] = "inject_failed"
        rec["terminal_reason"] = json.dumps(r, ensure_ascii=False)[:300]
        rec["elapsed_s"] = round(time.time() - t0, 1)
        return rec
    gid = r["goal"]["goal_id"]

    # ── architect stage: poll to ready / failed ─────────────────────────
    status = "bootstrapping"
    deadline = time.time() + goal_timeout_s
    while time.time() < deadline:
        g = _req("GET", f"{base}/api/app/goals/{gid}")
        status = (g.get("goal") or {}).get("status", "unknown")
        if status in ("ready", "failed"):
            break
        time.sleep(2.0)
    rec["status_at_ready"] = status
    rec["model_calls_at_ready"] = (g.get("goal") or {}).get("model_calls")
    rec["error"] = (g.get("goal") or {}).get("error")
    if status != "ready":
        rec["terminal_state"] = f"architect_{status}"
        rec["terminal_reason"] = rec.get("error") or ""
        rec["elapsed_s"] = round(time.time() - t0, 1)
        return rec
    rec["survived_architect"] = True

    # ── execution: PUBLIC start → poll to the terminal state ────────────
    cua0 = _cua_calls(archive_dir)
    s = _req("POST", f"{base}/api/sessions/{sid}/governance/start")
    rec["start_ok"] = bool(s.get("ok", s.get("_http_status") == 200))
    if not rec["start_ok"]:
        rec["terminal_state"] = "start_rejected"
        rec["terminal_reason"] = json.dumps(s, ensure_ascii=False)[:300]
        rec["elapsed_s"] = round(time.time() - t0, 1)
        return rec

    deadline = time.time() + terminal_timeout_s
    quiet_deadline = time.time() + 90.0     # CUA-quiet window (2×poll×9)
    last_cua = -1
    stable_word, stable_hits = "", 0
    while time.time() < deadline:
        time.sleep(poll_s)
        cua = _cua_calls(archive_dir)
        if cua > cua0 and not rec["cua_entry"]:
            rec["cua_entry"] = True
        if cua != last_cua:                 # still moving → reset quiet
            last_cua = cua
            quiet_deadline = time.time() + 90.0
        gov = _autonomy_of(base, sid)
        autonomy = str(gov.get("autonomy") or "")
        rec["autonomy_last"] = autonomy
        if autonomy and autonomy not in _ACTIVE_AUTONOMY:
            # a non-active disposition must hold across consecutive
            # polls — TWO with CUA activity (~2×poll_s), FOUR without
            # (~4×poll_s, guarding the pre-run transient) — never a
            # between-tick race; the no-CUA branch exists so a graph
            # with zero ready action nodes (an architect defect) is
            # accepted as terminal instead of burning the full window
            need = 2 if rec["cua_entry"] else 4
            if autonomy == stable_word:
                stable_hits += 1
            else:
                stable_word, stable_hits = autonomy, 1
            if stable_hits >= need:
                rec["terminal_state"] = f"autonomy_{autonomy}"
                rec["terminal_reason"] = ("driver status held a stable "
                                          "non-active disposition")
                break
        else:
            stable_word, stable_hits = "", 0
        if rec["cua_entry"] and time.time() > quiet_deadline:
            # the world went quiet without a terminal autonomy word —
            # honest label, never a fabricated success
            rec["terminal_state"] = "quiet"
            rec["terminal_reason"] = ("no new CUA call and no autonomy "
                                      "change for 90s (task may be done "
                                      "or wedged — public signals cannot "
                                      "distinguish)")
            break
    else:
        rec["terminal_state"] = "timeout"
        rec["terminal_reason"] = (f"no terminal state within "
                                  f"{terminal_timeout_s:.0f}s")
    rec["cua_calls"] = _cua_calls(archive_dir) - cua0
    # the legal closing gesture, whatever happened above
    _req("POST", f"{base}/api/sessions/{sid}/governance/stop")
    rec["elapsed_s"] = round(time.time() - t0, 1)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="baseline",
                    choices=("baseline", "with_skill"),
                    help="baseline = skills OFF (loader default); "
                         "with_skill = TASKVM_SKILL_INJECTION=on")
    ap.add_argument("--out", required=True,
                    help="phase output dir (e.g. eval_results/"
                         "skill_ladder_l0_20260820/baseline)")
    ap.add_argument("--port", type=int, default=3032)
    ap.add_argument("--bridge-port", type=int, default=3033)
    ap.add_argument("--sim-url", default="http://127.0.0.1:3000")
    ap.add_argument("--sid", default="skill-ladder-l0")
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--goal-timeout-s", type=float, default=600.0)
    ap.add_argument("--terminal-timeout-s", type=float, default=480.0)
    args = ap.parse_args()

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        print("SKIP: OPENAI_API_KEY is not set — L0 is a REAL-MODEL rung "
              "(B-06 env-gated discipline, .mrules §8).")
        return 3

    out_dir = os.path.abspath(args.out)
    archive_dir = os.path.join(out_dir, "call_archive")
    os.makedirs(archive_dir, exist_ok=True)

    # the phase's injection flag rides ONLY in the subprocess env —
    # baseline can never inherit an ambient "on", with_skill can never
    # lose the flag to the env scrub (the A/B stays driver-deterministic)
    skill_flag = "on" if args.phase == "with_skill" else None
    env = _proc_env(archive_dir, skill_flag)
    bridge_url = f"http://127.0.0.1:{args.bridge_port}"

    # (1) the ISOLATED bridge first — its browser is its own world
    bridge, blogf = _spawn_bridge(
        args.bridge_port, args.sim_url, env,
        os.path.join(out_dir, "bridge_process.log"))
    app, logf = None, None
    try:
        print(f"• waiting for the isolated bridge on {bridge_url} ...",
              flush=True)
        if not _wait_http(f"{bridge_url}/health"):
            print("FATAL: the bridge subprocess never became healthy — "
                  "see bridge_process.log", flush=True)
            return 2
        # (2) SETUP-plane activation — the one operator gesture the
        # open launcher deliberately cannot do (substrate contract §4):
        # a FRESH sid gets the sim's default world, exactly like
        # scripts/app_mobilegym.sh does for the demo stack
        r = _req("POST", f"{bridge_url}/api/reset/{args.sid}")
        print(f"• session '{args.sid}' activated on the setup plane: "
              f"{json.dumps(r, ensure_ascii=False)[:200]}", flush=True)
        # (3) the APP connects to the pre-activated bridge
        app, logf = _spawn_app(args.port, bridge_url, args.sim_url,
                               args.sid, env, args.model)
        base = f"http://127.0.0.1:{args.port}"
        print(f"• waiting for the isolated APP on {base} ...", flush=True)
        if not _wait_app(base):
            print("FATAL: the APP subprocess never became healthy — see "
                  "app_process.log", flush=True)
            return 2
        print("• APP healthy; replaying the six GATE-ARCH goals", flush=True)
        results = []
        for i, (goal_id, goal) in enumerate(GOALS, start=1):
            rec = _run_goal(base, args.sid, i, goal_id, goal, archive_dir,
                            goal_timeout_s=args.goal_timeout_s,
                            terminal_timeout_s=args.terminal_timeout_s)
            results.append(rec)
            print(json.dumps(rec, ensure_ascii=False), flush=True)

        survived = sum(1 for r in results if r["survived_architect"])
        cua = sum(1 for r in results if r["cua_entry"])
        terminal = sum(1 for r in results
                       if r["terminal_state"].startswith(("autonomy_",
                                                          "quiet")))
        summary = {
            "ladder": "SKILL-LADDER L0",
            "phase": args.phase,
            "skill_injection": ("on" if args.phase == "with_skill"
                                else "off (default)"),
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": args.model,
            "goals_replayed": "GATE-ARCH six (verbatim)",
            "architect_survival": f"{survived}/6",
            "cua_entry": f"{cua}/6",
            "terminal_reach": f"{terminal}/6",
            "goal_terminal_states": [r["terminal_state"] for r in results],
            "results": results,
        }
        out = os.path.join(out_dir, "L0_RESULT.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(json.dumps({k: v for k, v in summary.items()
                          if k != "results"}, ensure_ascii=False))
        print(f"written: {out}", flush=True)
        return 0
    finally:
        if app is not None:
            try:
                app.send_signal(signal.SIGTERM)
                app.wait(timeout=20)
            except Exception:
                try:
                    app.kill()
                except Exception:
                    pass
            if logf is not None:
                logf.close()
        try:
            bridge.send_signal(signal.SIGTERM)
            bridge.wait(timeout=20)
        except Exception:
            try:
                bridge.kill()
            except Exception:
                pass
        blogf.close()


if __name__ == "__main__":
    raise SystemExit(main())
