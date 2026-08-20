#!/usr/bin/env python
"""GATE-G0 driver — RM-C04-01 real-model automatic grading (no LLM judge).

The R1 acceptance instrument (master handover §6.3): ONE MobileGym trial
of the RM-C04-01 anchor (bench_design §九, "social_mark_and_true_rollback")
under the REAL model (gpt-5.6-sol via the company FRIDAY gateway), graded
by the deterministic five-field grader (``grade_task`` — no LLM judge,
ever).

Methodology (mirrors gate_arch_rerun.py + the B-08 factory chain):

  1. env gate: ``OPENAI_API_KEY`` must be set — this is a REAL-MODEL
     gate; scripted/fake ports can never stand in (B-06 env-gated
     discipline; the run honestly skips when the env is absent);
  2. the factory spawns an ISOLATED bridge on its own port (its own
     Playwright browser ⇒ its own MobileGym world — other agents'
     stacks on 3019/3016 are never touched);
  3. the projection public API is served in-process (the UserOpDriver's
     ONLY handle on the session — B-04 iron rule);
  4. ONE ``MobileGymFactory.run_trial`` with the TaskSpec-speaking
     fixture (the R1 thin adapter): setup (reset/seed/invariant) →
     bootstrap_real_full (REAL StateCompiler → TaskArchitect → CUA) →
     the public user-op program U0-U5 (checkpoint → start → deferred
     rollback → stop) → integrity → ``grade_task``;
  5. every real model call lands in the call archive verbatim
     (``TASKVM_CALL_ARCHIVE_DIR`` — the RecordingModelPort wraps the
     HttpModelPort inside THIS process; the archive is read back only);
  6. products (all inside --out): the five-field ContractVerdict JSON,
     the full EvidenceBundle (every intervention's before/after oracle
     snapshots), the TrialRecord, the stage-survival funnel, the trial
     manifest — every verdict field points back at raw evidence files
     in the same directory.

Usage:
  OPENAI_API_KEY=... python scripts/gate_g0_rm_c04_01.py \
      --out eval_results/gate_g0_rm_c04_01_20260820

Exit codes: 0 = graded PASS · 1 = graded FAIL (honest, evidence kept) ·
2 = error (infrastructure / evaluation_error) · 3 = env-gated skip.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _env_gate() -> str | None:
    """The B-06 real-model env gate (verbatim discipline)."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    return key


def _serve_projection(app, port: int) -> int:
    """Serve the projection Flask app on a daemon thread; 0 = auto-port."""
    import threading
    if not port:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port,
                                threaded=True, debug=False,
                                use_reloader=False),
        daemon=True).start()
    return port


def _wait_http(url: str, timeout_s: float = 20.0) -> bool:
    import urllib.request
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None,
                    help="output dir (default: eval_results/gate_g0_rm_"
                         "c04_01_<today>)")
    ap.add_argument("--bridge-port", type=int, default=3039,
                    help="the ISOLATED bridge port the factory spawns "
                         "(WARNING: a HEALTHY bridge already listening "
                         "on this port gets REUSED — never point this "
                         "at another stack's port (3019/3029 belong to "
                         "other agents' sessions))")
    ap.add_argument("--sim-url", default="http://127.0.0.1:3000")
    ap.add_argument("--sid", default=None)
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--settle-timeout-s", type=float, default=1800.0,
                    help="per-op settle barrier timeout (the start op "
                         "must span the whole forward task; the rollback "
                         "HTTP handler waits for the CUA's reverse-GUI "
                         "compensation to finish synchronously)")
    ap.add_argument("--start-quiet-s", type=float, default=180.0,
                    help="the start op's quiet window — must EXCEED the "
                         "longest silent model call (the forward task "
                         "runs inside this barrier; quiet=3s settled "
                         "mid-flight in the first GATE-G0 attempt and "
                         "the witnesses never landed)")
    ap.add_argument("--http-timeout-s", type=float, default=1900.0,
                    help="the UserOpDriver's HTTP timeout (same reason)")
    ap.add_argument("--max-actions-per-contract", type=int, default=12,
                    help="runtime budget: atomic GUI gestures per action "
                         "contract. The runtime default 12 starved the "
                         "search contract on real-model runs (r3 burned 11 "
                         "gestures on a dead 'open' gesture; r4 spent all "
                         "12 on home-screen navigation + X search alone) "
                         "— raise it so the contract is judged by the "
                         "verifier, not by the step ceiling")
    ap.add_argument("--projection-port", type=int, default=0)
    args = ap.parse_args()

    api_key = _env_gate()
    if api_key is None:
        print("SKIP: OPENAI_API_KEY is not set — GATE-G0 is a REAL-MODEL "
              "gate; scripted/fake ports can never stand in "
              "(B-06 env-gated discipline, .mrules §8).")
        return 3

    today = time.strftime("%Y%m%d")
    out_dir = args.out or os.path.join(
        "eval_results", f"gate_g0_rm_c04_01_{today}")
    os.makedirs(out_dir, exist_ok=True)
    archive_dir = os.path.join(out_dir, "call_archive")
    os.makedirs(archive_dir, exist_ok=True)
    # the RecordingModelPort (inside THIS process) reads this env var
    os.environ["TASKVM_CALL_ARCHIVE_DIR"] = archive_dir

    sys.path.insert(0, REPO_ROOT)
    # the bridge subprocess (spawned by the factory below) inherits this
    # process env: it needs bench_env (MobileGym) + the Playwright
    # browsers, exactly like scripts/app_mobilegym.sh provisions them.
    mobilegym_dir = os.environ.get(
        "MOBILEGYM_DIR", os.path.join(REPO_ROOT, "..", "mobilegym"))
    if os.path.isdir(os.path.join(mobilegym_dir, "bench_env")):
        os.environ["PYTHONPATH"] = (
            os.path.abspath(mobilegym_dir) + os.pathsep
            + os.environ.get("PYTHONPATH", ""))
    py_home = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    pw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if not pw and os.path.isdir(os.path.join(py_home, "opt", "ms-playwright")):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
            py_home, "opt", "ms-playwright")
    chromelibs = os.path.join(REPO_ROOT, ".chromelibs", "lib")
    if os.path.isdir(chromelibs):
        os.environ["LD_LIBRARY_PATH"] = (
            chromelibs + os.pathsep
            + os.environ.get("LD_LIBRARY_PATH", ""))

    # ── 1. the projection public API (in-process) ─────────────────────
    from taskvm.projection.store import ProjectionSessionStore
    from taskvm.workspace_ui import serve as serve_projection
    store = ProjectionSessionStore()
    projection_port = _serve_projection(
        serve_projection(store), args.projection_port)
    base_url = f"http://127.0.0.1:{projection_port}"

    # ── 2. the REAL model port, verbatim-archived ─────────────────────
    from taskvm.architect.http_port import HttpModelPort
    from taskvm.workspace_ui.call_archive import maybe_recording_port
    model_port = maybe_recording_port(HttpModelPort())

    # ── 3. the factory + the ISOLATED bridge (own Playwright world) ──
    from taskvm_bench.evaluation.mobilegym_factory import (
        MobileGymFactory, MobileGymTrialSpec)
    from taskvm_bench.benchmark.rm_anchor_tasks import (
        RM_C04_01, mobilegym_fixture_view, rm_c04_01_user_ops)
    factory = MobileGymFactory(
        bridge_port=args.bridge_port, sim_url=args.sim_url,
        # the bridge MUST run under THIS driver's interpreter (the
        # repo contract's TASKVM_PYTHON discipline: a bare "python3"
        # resolves to whatever the launching shell has first on PATH
        # — the 2026-08-20 r2 relaunch died rc=1 because an
        # unactivated shell handed the bridge a Python 3.8)
        bridge_python=sys.executable,
        bridge_log=os.path.join(out_dir, "bridge.log"),
        connect_only=False, bridge_startup_timeout_s=240.0,
        request_timeout_s=30.0)
    bridge = factory.ensure_bridge()
    print(f"• bridge: {bridge.url} ({bridge.instance_id})", flush=True)

    sid = args.sid or f"rm-c04-01-gate-{today}-e0-s0"
    spec = MobileGymTrialSpec(
        fixture=mobilegym_fixture_view(RM_C04_01), sid=sid,
        model=args.model, condition="taskvm-real-full")

    from taskvm_bench.evaluation.projection_client import ProjectionClient
    from taskvm_bench.evaluation.user_ops import UserOpDriver
    driver = UserOpDriver(ProjectionClient(
        base_url, sid, timeout_s=args.http_timeout_s))

    # ── 4. the one trial (the whole U0-U5 arc, real model) ───────────
    print(f"• trial {spec.fixture.task_id} sid={sid} model={args.model}",
          flush=True)
    from taskvm.runtime.config import RuntimeBudgets
    from taskvm.workspace_ui.composition import bootstrap_real_full
    budgets = RuntimeBudgets(
        max_actions_per_contract=args.max_actions_per_contract)

    def _bootstrap(**kwargs):
        return bootstrap_real_full(**kwargs, budgets=budgets)

    t0 = time.time()
    try:
        record = factory.run_trial(
            spec, driver=driver, store=store, model_port=model_port,
            bootstrap_fn=_bootstrap,
            user_ops=rm_c04_01_user_ops(
                settle_timeout_s=args.settle_timeout_s,
                start_quiet_s=args.start_quiet_s))
    finally:
        pass
    elapsed_s = round(time.time() - t0, 1)
    print(f"• trial done in {elapsed_s}s: verdict={record.trial_verdict} "
          f"failure_class={record.failure_class} "
          f"eval_error={record.evaluation_error!r}", flush=True)

    # ── 5. products — every verdict field points back at these ───────
    from dataclasses import asdict
    from taskvm_bench.evaluation.funnel import build_funnel, render_funnel

    trial_path = os.path.join(out_dir, "trial-000.json")
    with open(trial_path, "w", encoding="utf-8") as fh:
        json.dump(asdict(record), fh, indent=1, ensure_ascii=False)

    funnel = build_funnel([record], trials_requested=1)
    funnel_path = os.path.join(out_dir, "funnel.json")
    with open(funnel_path, "w", encoding="utf-8") as fh:
        json.dump(funnel.to_dict(), fh, indent=1, ensure_ascii=False)

    manifest = dict(factory.manifest_fields(spec))
    manifest["gate"] = "GATE-G0"
    manifest["task_id"] = RM_C04_01.task_id
    manifest["goal"] = RM_C04_01.goal
    manifest["model"] = args.model
    manifest["condition"] = spec.condition
    manifest["sid"] = sid
    manifest["settle_timeout_s"] = args.settle_timeout_s
    manifest["start_quiet_s"] = args.start_quiet_s
    manifest["max_actions_per_contract"] = args.max_actions_per_contract
    manifest["bridge_port"] = args.bridge_port
    manifest["sim_url"] = args.sim_url
    manifest["elapsed_s"] = elapsed_s
    manifest["call_archive_dir"] = "call_archive"
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, ensure_ascii=False)

    bundle_path = None
    bundle = getattr(factory, "last_evidence", None)
    if bundle is not None:
        bundle_path = os.path.join(out_dir, "evidence_bundle.json")
        bundle.dump(bundle_path)

    verdict = record.contract_verdict
    result = {
        "gate": "GATE-G0",
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "task_id": RM_C04_01.task_id,
        "goal": RM_C04_01.goal,
        "model": args.model,
        "condition": spec.condition,
        "sid": sid,
        "trial_verdict": record.trial_verdict,
        "failure_class": record.failure_class,
        "evaluation_error": record.evaluation_error,
        "stage_reached": record.stage_reached,
        "cua_entered": record.cua_entered,
        "elapsed_s": elapsed_s,
        "pass_criterion": "graded trial PASS: five-field ContractVerdict "
                          "with empty failure_codes (deterministic "
                          "grade_task, no LLM judge)",
        "verdict": None,           # filled below
        "contract_verdict": verdict,
        "funnel": funnel.to_dict(),
        "user_ops": [
            {"kind": op.get("kind"), "verdict": op.get("verdict"),
             "http_status": op.get("http_status")}
            for op in record.user_ops],
        "evidence_index": {
            "world_contract": [
                "evidence_bundle.json: oracle_final / interventions[]."
                "world_diff / interventions[].protected_diff",
                "trial-000.json: user_ops[].world_diff"],
            "governance_contract": [
                "evidence_bundle.json: interventions[] (op statuses, "
                "sse_window, gui_actions, checkpoint/rollback "
                "brackets), environment_writes, model_ledger_counts",
                "trial-000.json: user_ops[]",
                "call_archive/INDEX.txt: one verbatim txt per real "
                "provider request (role-attributed)"],
            "projection_consistency": [
                "evidence_bundle.json: interventions[].projection_before/"
                "after vs oracle_before/after"],
            "progress": [
                "trial-000.json: user_ops[].verdict",
                "funnel.json: counts_by_stage"],
            "failure_codes": [
                "evidence_bundle.json + trial-000.json (all of the above)"],
        },
    }
    if verdict is None:
        result["verdict"] = "UNGRADED"
        exit_code = 2
    else:
        graded_pass = bool(verdict.get("passed"))
        result["verdict"] = "PASS" if (
            graded_pass and record.trial_verdict == "pass") else "FAIL"
        exit_code = 0 if result["verdict"] == "PASS" else (
            1 if record.trial_verdict in ("pass", "fail") else 2)

    result_path = os.path.join(out_dir, "GATE_G0_RESULT.json")
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, ensure_ascii=False)

    print(render_funnel(funnel))
    if verdict is not None:
        print(json.dumps({k: verdict[k] for k in (
            "world_contract", "governance_contract",
            "projection_consistency", "progress", "failure_codes",
            "passed")}, ensure_ascii=False, indent=1))
    print(f"\nGATE-G0 verdict: {result['verdict']}")
    print(f"products: {out_dir} (GATE_G0_RESULT.json / evidence_bundle.json"
          f"{' (no bundle)' if bundle_path is None else ''} / "
          f"trial-000.json / funnel.json / manifest.json / call_archive/)")

    factory.close()          # ONLY the bridge this factory spawned
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
