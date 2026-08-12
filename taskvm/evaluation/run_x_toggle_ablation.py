"""X toggle kill-test ABLATION script (Task A, .mrules E14-core).

Runs the X toggle kill-test under 4 different harness configurations to
isolate the contribution of each E14 fix:

  1. baseline (all-old):  coord=old, posts=old, instr=old
     → reproduces pre-E14 ~0% success (coordinate bug + empty content_hint
       + no post-tap verification)
  2. +coord only:          coord=new, posts=old, instr=old
     → isolates the coordinate-pipeline fix (env.step + norm_0_1000)
  3. +coord +posts:        coord=new, posts=new, instr=old
     → adds the posts.json path fix (content_hint no longer empty)
  4. all-new (E14 final):  coord=new, posts=new, instr=new
     → adds the instruction verification step (check icon color change)

For each config:
  - kills + restarts the bridge with the config's env vars
  - runs run_x_toggle_killtest.py with --samples 2 (2 rounds × 3 posts × 3 ops = 18 tests)
  - saves the JSON report as eval_results/x_toggle_ablation_<config>_<ts>.json
  - records the success rate

This directly answers E14-core's question: "which fix contributed most?"
NO MORE "coupled fixes, no ablation" gap.

Usage (from a2ui/):
    python -m taskvm.evaluation.run_x_toggle_ablation

Prerequisites:
  - Vite dev server (:3000) running
  - bridge (:3019) will be killed + restarted by this script
  - conda senseact env activated
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
BRIDGE_PORT = 3019
VITE_PORT = 3000
BRIDGE_HOST = "localhost"

# 4 ablation configs, ordered to show incremental contribution.
CONFIGS = [
    {
        "name": "baseline_all_old",
        "desc": "pre-E14: coord=old(400,800)+mouse.click, posts=old(wrong path→empty), instr=old(no verify)",
        "env": {"TASKVM_ABLATION_COORD": "old", "TASKVM_ABLATION_POSTS": "old", "TASKVM_ABLATION_INSTRUCTION": "old"},
    },
    {
        "name": "coord_only",
        "desc": "+coordinate fix: env.step+norm_0_1000 (posts still empty, instr still no-verify)",
        "env": {"TASKVM_ABLATION_COORD": "new", "TASKVM_ABLATION_POSTS": "old", "TASKVM_ABLATION_INSTRUCTION": "old"},
    },
    {
        "name": "coord_plus_posts",
        "desc": "+posts.json path fix: content_hint now populated (instr still no-verify)",
        "env": {"TASKVM_ABLATION_COORD": "new", "TASKVM_ABLATION_POSTS": "new", "TASKVM_ABLATION_INSTRUCTION": "old"},
    },
    {
        "name": "all_new_e14_final",
        "desc": "+instruction verify step: check icon color change before done (E14 final 94.4%)",
        "env": {"TASKVM_ABLATION_COORD": "new", "TASKVM_ABLATION_POSTS": "new", "TASKVM_ABLATION_INSTRUCTION": "new"},
    },
]

SAMPLES = 2  # 2 rounds × 3 posts × 3 ops = 18 tests per config


def _kill_bridge():
    """Kill any existing bridge on :3019."""
    try:
        subprocess.run(
            ["fuser", "-k", f"{BRIDGE_PORT}/tcp"],
            capture_output=True, timeout=10)
        time.sleep(2)
    except Exception as e:
        logger.warning(f"fuser kill failed: {e}")
        time.sleep(1)


def _start_bridge(config_env: dict, screenshot_dir: str) -> subprocess.Popen:
    """Start the bridge with the given ablation env vars."""
    env = os.environ.copy()
    env.update(config_env)
    # Ensure PYTHONPATH includes mobilegym + a2ui
    env["PYTHONPATH"] = (
        "/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/mobilegym:"
        "/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui:"
        ".")
    env["PATH"] = (
        "/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/conda/envs/senseact/bin:"
        + env.get("PATH", ""))

    log_file = f"/tmp/bridge_ablation_{int(time.time())}.log"
    cmd = [
        "python", "-m", "taskvm.harness.mobilegym_bridge",
        "--port", str(BRIDGE_PORT),
        "--screenshot-dir", screenshot_dir,
    ]
    logger.info(f"  starting bridge: env={config_env}")
    proc = subprocess.Popen(
        cmd, env=env, stdout=open(log_file, "w"), stderr=subprocess.STDOUT,
        cwd="/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui")
    # wait for bridge to be healthy
    for _ in range(30):
        time.sleep(1)
        try:
            r = requests.get(f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/health", timeout=3)
            if r.status_code == 200 and r.json().get("status") == "ok":
                logger.info(f"  bridge healthy (pid={proc.pid}, log={log_file})")
                return proc
        except Exception:
            pass
    logger.error(f"  bridge failed to start within 30s — check {log_file}")
    proc.kill()
    return None


def _run_killtest(out_path: str) -> dict:
    """Run run_x_toggle_killtest.py and return the parsed JSON report."""
    cmd = [
        "python", "-m", "taskvm.evaluation.run_x_toggle_killtest",
        "--samples", str(SAMPLES),
        "--out", out_path,
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        "/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/mobilegym:"
        "/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui:"
        ".")
    env["PATH"] = (
        "/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/conda/envs/senseact/bin:"
        + env.get("PATH", ""))
    logger.info(f"  running killtest → {out_path}")
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800)
    if result.returncode not in (0, 1):
        logger.error(f"  killtest exited {result.returncode}")
        logger.error(f"  stderr: {result.stderr[-500:]}")
    try:
        return json.loads(Path(out_path).read_text())
    except Exception as e:
        logger.error(f"  failed to parse {out_path}: {e}")
        return {}


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    # verify Vite is up
    try:
        r = requests.get(f"http://{BRIDGE_HOST}:{VITE_PORT}", timeout=4)
        if r.status_code != 200:
            logger.error(f"Vite :{VITE_PORT} not healthy ({r.status_code})")
            sys.exit(2)
    except Exception as e:
        logger.error(f"Vite :{VITE_PORT} not reachable: {e}")
        sys.exit(2)
    logger.info(f"Vite :{VITE_PORT} healthy — starting ablation")

    ts_global = time.strftime("%Y%m%d_%H%M%S")
    results = []

    for i, cfg in enumerate(CONFIGS):
        logger.info(f"\n{'='*60}")
        logger.info(f"CONFIG {i+1}/4: {cfg['name']}")
        logger.info(f"  desc: {cfg['desc']}")
        logger.info(f"  env:  {cfg['env']}")
        logger.info(f"{'='*60}")

        _kill_bridge()
        screenshot_dir = str(EVAL_DIR / f"x_toggle_ablation_{cfg['name']}_{ts_global}")
        proc = _start_bridge(cfg["env"], screenshot_dir)
        if proc is None:
            logger.error(f"  SKIP config {cfg['name']} — bridge failed to start")
            results.append({"config": cfg["name"], "error": "bridge_failed"})
            continue

        out_path = str(EVAL_DIR / f"x_toggle_ablation_{cfg['name']}_{ts_global}.json")
        report = _run_killtest(out_path)
        if not report:
            results.append({"config": cfg["name"], "error": "killtest_failed"})
            continue

        sr = report.get("success_rate", 0.0)
        ns = report.get("n_success", 0)
        nt = report.get("n_total", 0)
        per_op = report.get("per_operator", {})
        logger.info(f"  RESULT: {ns}/{nt} = {sr:.1%}  PASS={report.get('PASS')}")
        for op, st in per_op.items():
            logger.info(f"    {op}: {st['n_success']}/{st['n']} = {st['success_rate']:.1%}")

        results.append({
            "config": cfg["name"],
            "desc": cfg["desc"],
            "env": cfg["env"],
            "n_success": ns,
            "n_total": nt,
            "success_rate": sr,
            "PASS": report.get("PASS"),
            "per_operator": per_op,
            "report_path": out_path,
        })

        # kill bridge before next config
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    # write summary
    summary_path = EVAL_DIR / f"x_toggle_ablation_summary_{ts_global}.json"
    summary = {
        "ts": ts_global,
        "test": "x_toggle_ablation_summary",
        "description": (
            "Ablation experiment (Task A, .mrules E14-core): isolates the "
            "contribution of each E14 fix by running the X toggle kill-test "
            "under 4 incremental configurations. Answers E14-core's question: "
            "'which fix contributed most to the 0%→94.4% improvement?'"),
        "samples_per_config": SAMPLES,
        "n_tests_per_config": SAMPLES * 3 * 3,
        "configs": results,
        "honest_framing": {
            "what_this_proves": (
                "The incremental success rates across configs show how much "
                "each fix (coordinate pipeline, posts.json path, instruction "
                "verification) contributed to the final 94.4% — closing "
                "E14-core's 'no ablation was ever run' gap."),
            "caveats": (
                "Only tests the first 3 posts (no scrolling). Success rates "
                "may vary with different posts/models. The coordinate fix's "
                "contribution is somewhat masked by the fact that posts=old "
                "(empty content_hint) makes ALL taps unreliable regardless of "
                "coordinate accuracy — so coord_only may show LOW success even "
                "if the coordinate fix is necessary for later configs."),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n{'='*60}")
    print(f"ABLATION SUMMARY")
    print(f"{'='*60}")
    for r in results:
        if "error" in r:
            print(f"  {r['config']:25s}  ERROR: {r['error']}")
        else:
            print(f"  {r['config']:25s}  {r['n_success']:2d}/{r['n_total']:2d} = "
                  f"{r['success_rate']:5.1%}  PASS={r['PASS']}")
    print(f"\nSummary: {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
