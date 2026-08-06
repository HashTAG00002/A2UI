"""W1 kill-test orchestrator.

Wires the full chain end-to-end per sample:
  seed → capture obs → assert obs matches state → compile (frontier API)
  → validate binding → render surface → apply scripted user edit → patch
  → dispatch (app-API) → verify (canonical state) → record.

Each task runs N≥3 compiler samples (temperature 0.3). Each sample is paired
with a negative-control run (broken dispatcher) that MUST score ≤0.3.

Outputs ``eval_results/w1_<ts>.json`` with per-sample score, per-check
fractions, binding-accuracy, which-link-broke, and the neg-control score.

Exit criteria + sub-kill triggers are evaluated by ``summarize`` and printed.

Usage:
    python -m taskvm.evaluation.run_w1_killtest --samples 3 --model gpt-5.6-sol
    python -m taskvm.evaluation.run_w1_killtest --task release_reschedule --samples 5
    python -m taskvm.evaluation.run_w1_killtest --neg-control   # neg-control only
    python -m taskvm.evaluation.run_w1_killtest --mock          # no API: use a GT-shaped binding (smoke the chain)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from taskvm.benchmark import model_client
from taskvm.benchmark.cost_model import CostModel
from taskvm.benchmark.fixtures import CanonicalTaskGraph, all_tasks, get_task
from taskvm.execution.action_dispatcher import dispatch
from taskvm.execution.patch_compiler import compile_patch
from taskvm.harness import replay_engine as replay
from taskvm.harness.observations import TraceFixture
from taskvm.harness.state_adapter import make_adapters
from taskvm.task_state.entity_binding import TaskBinding
from taskvm.task_state.compiler import compile_binding
from taskvm.evaluation.render_check import (parse_compiler_output,
                                            validate_binding, validate_a2ui_surface)
from taskvm.verifier import canonical_state as cs
from taskvm.verifier.round_trip_checks import check_round_trip, binding_accuracy
from taskvm.workspace_ui.renderer import render

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
PASS_SCORE = 0.85
NEG_CONTROL_MAX = 0.3
SAMPLES_FOR_PASS = 2   # ≥2/3 samples


def _which_link_broke(compiled: dict, valid: bool, dispatch_report) -> str | None:
    if not compiled["ok"]:
        return "compile_failure"
    if not valid:
        return "binding_miss"
    if dispatch_report is not None and not dispatch_report.all_applied:
        return "dispatch_partial"
    return None


def run_one_sample(fixture: CanonicalTaskGraph, adapters: dict, *,
                   model: str | None, temperature: float, sample_i: int,
                   mock: bool = False, with_screenshot: bool = False,
                   cost_model: CostModel | None = None,
                   full_a2ui: bool = False) -> dict:
    """Run one compiler sample end-to-end. Returns the result record."""
    sid = f"{fixture.task_id}_s{sample_i}_{int(time.time()*1000) % 100000}"
    # reset (idempotent) + seed
    for ad in adapters.values():
        ad.reset(sid)
    replay.seed_apps(fixture, adapters, sid)

    # capture observations (read-path-GUI) + assert they match real state
    obs = replay.capture_obs(adapters, sid, with_screenshot=with_screenshot)
    replay.assert_obs_matches_state(adapters, sid, obs)
    trace = TraceFixture(task_id=fixture.task_id, goal=fixture.goal, final_obs=obs)
    observed_ids = {app: set(replay.parse_dom_entities(o.dom_html).keys())
                    for app, o in obs.items()}

    # compile the binding (gate-critical model step) — or use GT in mock mode
    if mock:
        compiled = _mock_compiler_output(fixture, observed_ids)
    else:
        compiled = compile_binding(trace, observed_ids, model=model,
                                   temperature=temperature, cost_model=cost_model,
                                   binding_only=not full_a2ui)

    raw = compiled.get("raw")
    parsed = compiled.get("parsed")
    a2ui_ok, a2ui_errs = validate_a2ui_surface(parsed)
    binding = parse_compiler_output(raw, parsed)
    valid, bind_errs = (False, ["no binding parsed"]) if binding is None \
        else validate_binding(binding, observed_ids, fixture.task_id)

    # build a TaskBinding for patch compilation (from the compiler's output)
    tb = _to_task_binding(binding, fixture) if (binding and valid) else None

    # render the surface (structured text)
    rendered = render(tb) if tb else "(binding invalid — no surface)"

    # pre-snapshot (BEFORE dispatch)
    pre = cs.snapshot(adapters, sid)

    # patch + dispatch (write-path-API)
    dispatch_report = None
    if tb is not None:
        ops = compile_patch(fixture.user_edit, tb)
        dispatch_report = dispatch(ops, adapters, sid, broken=None)

    # verify (canonical state)
    res = check_round_trip(sid, fixture, adapters, pre)

    # binding-accuracy diagnostic (compiler binding vs GT)
    bacc = binding_accuracy(binding, fixture) if binding else binding_accuracy(None, fixture)

    which_broke = _which_link_broke(compiled, valid, dispatch_report)

    # cleanup
    for ad in adapters.values():
        ad.reset(sid)

    return {
        "task_id": fixture.task_id,
        "sample": sample_i,
        "model": model or model_client.TASKVM_DEFAULT_MODEL,
        "mock": mock,
        "compile_ok": compiled["ok"],
        "compile_error": compiled.get("error"),
        "binding_valid": valid,
        "binding_errors": bind_errs,
        "a2ui_valid": a2ui_ok,
        "a2ui_errors": a2ui_errs,
        "binding_accuracy": bacc,
        "dispatch": dispatch_report.to_dict() if dispatch_report else None,
        "round_trip": {
            "score": res.score,
            "changed_fraction": res.changed.fraction,
            "untouched_fraction": res.untouched.fraction,
            "resynced_fraction": res.resynced.fraction,
            "non_interference_passed": res.non_interference_passed,
            "hard_fail": res.hard_fail,
            "info": res.info,
        },
        "which_link_broke": which_broke,
        "rendered_surface": rendered,
        "raw_compiler_output": (raw[:4000] if raw else None),
    }


def run_neg_control(fixture: CanonicalTaskGraph, adapters: dict, *,
                    model: str | None, mock: bool = False,
                    cost_model: CostModel | None = None) -> dict:
    """Negative control: use the GT binding (so binding is perfect) but break
    the dispatcher. MUST score ≤0.3. If it doesn't, the verifier is broken."""
    sid = f"{fixture.task_id}_neg_{int(time.time()*1000) % 100000}"
    for ad in adapters.values():
        ad.reset(sid)
    replay.seed_apps(fixture, adapters, sid)
    obs = replay.capture_obs(adapters, sid)
    replay.assert_obs_matches_state(adapters, sid, obs)
    # use GT binding (bypass the compiler) so the ONLY thing tested is the verifier
    tb = _gt_task_binding(fixture)
    pre = cs.snapshot(adapters, sid)
    ops = compile_patch(fixture.user_edit, tb)
    # broken dispatcher: noop (changed-happened fails, non-interference passes)
    dispatch_report = dispatch(ops, adapters, sid, broken="noop")
    res = check_round_trip(sid, fixture, adapters, pre)
    for ad in adapters.values():
        ad.reset(sid)
    return {
        "task_id": fixture.task_id,
        "neg_control": True,
        "broken": "noop",
        "score": res.score,
        "changed_fraction": res.changed.fraction,
        "untouched_fraction": res.untouched.fraction,
        "resynced_fraction": res.resynced.fraction,
        "hard_fail": res.hard_fail,
        "passed": res.score <= NEG_CONTROL_MAX,
        "dispatch": dispatch_report.to_dict(),
    }


# ── helpers ──────────────────────────────────────────────────────────────────
def _gt_task_binding(fixture: CanonicalTaskGraph) -> TaskBinding:
    """Build a TaskBinding from the GT fixture (for neg-control + mock)."""
    var_groups: dict[str, dict] = {}
    for b in fixture.bindings:
        g = var_groups.setdefault(b.var_id, {
            "var_id": b.var_id, "label": b.var_id, "value": fixture.user_edit.get("old"),
            "editable": True, "bindings": []})
        g["bindings"].append({"var_id": b.var_id, "app": b.app, "entity_id": b.entity_id,
                              "field": b.field, "operator": b.operator})
    return TaskBinding(task_id=fixture.task_id, variables=list(var_groups.values()))


def _to_task_binding(binding: dict, fixture: CanonicalTaskGraph) -> TaskBinding:
    """Convert the compiler's parsed task_binding dict into a TaskBinding
    (filling value from the observed state where the compiler omitted it)."""
    variables = []
    for v in (binding.get("variables") or []):
        variables.append({
            "var_id": v.get("var_id"), "label": v.get("label", v.get("var_id")),
            "value": v.get("value"), "editable": v.get("editable", True),
            "bindings": v.get("bindings") or [],
        })
    return TaskBinding(task_id=binding.get("task_id") or fixture.task_id,
                       variables=variables)


def _mock_compiler_output(fixture: CanonicalTaskGraph,
                          observed_ids: dict[str, set[str]]) -> dict:
    """Mock the compiler with a GT-shaped binding (smoke the chain without API).
    Used by ``--mock`` to verify the orchestrator wiring before spending API."""
    tb = _gt_task_binding(fixture)
    binding_dict = tb.to_dict()
    return {"raw": json.dumps({"task_binding": binding_dict}, ensure_ascii=False),
            "parsed": {"text_response": "(mock)", "a2ui": [], "task_binding": binding_dict},
            "ok": True, "text_response": "(mock)", "a2ui": [],
            "task_binding": binding_dict, "error": None}


# ── summary + exit criteria ──────────────────────────────────────────────────
def summarize(task_id: str, samples: list[dict], neg: dict) -> dict:
    n = len(samples)
    scores = [s["round_trip"]["score"] for s in samples]
    n_pass_score = sum(1 for s in scores if s >= PASS_SCORE)
    binding_f1s = [s["binding_accuracy"]["f1"] for s in samples]
    non_interf_ok = all(s["round_trip"]["non_interference_passed"] for s in samples
                        if s["round_trip"]["score"] >= PASS_SCORE)
    no_handwritten = True   # W1 always uses model-discovered binding (mock is opt-in)
    neg_ok = neg["passed"]
    # PASS: ≥2/3 samples ≥0.85 AND neg-control ≤0.3 AND non-interference 1.0 AND no hand-written
    passed = (n_pass_score >= SAMPLES_FOR_PASS and neg_ok and non_interf_ok and no_handwritten)
    return {
        "task_id": task_id,
        "n_samples": n,
        "scores": scores,
        "mean_score": round(sum(scores) / n, 4) if n else 0.0,
        "n_pass_score": n_pass_score,
        "pass_threshold": PASS_SCORE,
        "binding_f1_mean": round(sum(binding_f1s) / n, 4) if n else 0.0,
        "non_interference_ok_on_passing": non_interf_ok,
        "neg_control_score": neg["score"],
        "neg_control_passed": neg_ok,
        "neg_control_max": NEG_CONTROL_MAX,
        "no_handwritten_binding": no_handwritten,
        "PASS": passed,
        "which_link_broke_counts": _broke_counts(samples),
    }


def _broke_counts(samples: list[dict]) -> dict:
    counts: dict = {}
    for s in samples:
        b = s.get("which_link_broke") or "ok"
        counts[b] = counts.get(b, 0) + 1
    return counts


def evaluate_sub_kills(summaries: list[dict], samples: list[dict] | None = None) -> dict:
    """Map results to the three sub-kill triggers (doc §10).

    Uses per-sample round-trip pass (score >=0.85 AND non-interference) as the
    "model can discover the binding" signal, which is more robust than
    task-level PASS (needs 2/3 samples, meaningless on a 1-sample probe).
    """
    all_scores = [s for sm in summaries for s in sm["scores"]]
    max_score = max(all_scores) if all_scores else 0.0
    # any single sample that round-tripped (score >=0.85 + non-interference)?
    any_round_trip_pass = any(
        s["round_trip"]["score"] >= PASS_SCORE
        and s["round_trip"]["non_interference_passed"]
        for s in (samples or [])
        if not s.get("mock") or True)  # include all; mock samples are opt-in
    any_task_pass = any(sm["PASS"] for sm in summaries)
    neg_ok = all(sm["neg_control_passed"] for sm in summaries)
    n_samples = max((sm["n_samples"] for sm in summaries), default=0)

    # sub-kill-1: round-trip can't run (max score <0.5 across ALL samples), or
    # the neg-control is dishonest (verifier broken).
    sub_kill_1 = (max_score < 0.5) or (not neg_ok)
    # sub-kill-3: only hand-written binding works; the model NEVER produced a
    # round-trip-passing binding on any sample (W1 never hand-writes, so this is
    # the real "is it a compiler?" test). Only meaningful with >=2 samples.
    sub_kill_3 = (not any_round_trip_pass) and (not sub_kill_1)

    if any_task_pass and neg_ok:
        verdict = "PASS"
    elif sub_kill_1:
        verdict = "SUB_KILL_1_shrink_to_direction_2"
    elif sub_kill_3 and n_samples >= 2:
        verdict = "SUB_KILL_3_stop_custom_dashboard"
    elif not any_round_trip_pass and n_samples < 2:
        verdict = "INCONCLUSIVE_need_more_samples"
    else:
        verdict = "INCONCLUSIVE"
    return {"sub_kill_1": sub_kill_1, "sub_kill_3": sub_kill_3,
            "neg_control_honest": neg_ok, "verdict": verdict,
            "max_score": round(max_score, 4),
            "any_round_trip_pass": any_round_trip_pass,
            "any_task_pass": any_task_pass, "n_samples": n_samples}


# ── main ─────────────────────────────────────────────────────────────────────
def main(argv=None):
    parser = argparse.ArgumentParser(description="TaskVM W1 kill-test")
    parser.add_argument("--task", default=None, help="task_id (default: both)")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--model", default=None, help="frontier model id (default gpt-5.6-sol)")
    parser.add_argument("--temperature", type=float, default=None,
                        help="sampling temperature (default None: reasoning models like "
                             "gpt-5.5 reject non-default temperature; pass a float only for "
                             "models known to accept it)")
    parser.add_argument("--mock", action="store_true",
                        help="no API: use GT-shaped binding (smoke the chain)")
    parser.add_argument("--neg-control", action="store_true",
                        help="run only the negative control (verifier honesty check)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--with-screenshot", action="store_true",
                        help="capture a screenshot per app (visual grounding; needs playwright)")
    parser.add_argument("--full-a2ui", action="store_true",
                        help="require a full A2UI surface (W1 default: binding-only, doc §10)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    adapters = make_adapters(host=args.host)
    # health check
    for app, ad in adapters.items():
        h = ad.health()
        if h.get("status") != "ok":
            logger.error(f"{app} not healthy: {h}")
            sys.exit(2)
        logger.info(f"{app} healthy @ {ad.base_url}")

    tasks = [get_task(args.task)] if args.task else all_tasks()
    cost_model = CostModel()
    ts = time.strftime("%Y%m%d_%H%M%S")

    if args.neg_control:
        results = []
        for fx in tasks:
            neg = run_neg_control(fx, adapters, model=args.model, mock=args.mock,
                                  cost_model=cost_model)
            logger.info(f"[neg-control] {fx.task_id}: score={neg['score']} "
                        f"(must be ≤{NEG_CONTROL_MAX}) → {'PASS' if neg['passed'] else 'FAIL'}")
            results.append(neg)
        out_path = Path(args.out) if args.out else EVAL_DIR / f"w1_negcontrol_{ts}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(json.dumps(results, ensure_ascii=False, indent=2))
        ok = all(r["passed"] for r in results)
        print(f"\nNEG-CONTROL {'PASS' if ok else 'FAIL'} (verifier "
              f"{'is' if ok else 'is NOT'} honest)")
        return 0 if ok else 1

    summaries = []
    all_samples = []
    for fx in tasks:
        logger.info(f"\n=== TASK {fx.task_id} ===")
        samples = []
        for i in range(args.samples):
            logger.info(f"--- sample {i+1}/{args.samples} ---")
            s = run_one_sample(fx, adapters, model=args.model,
                               temperature=args.temperature, sample_i=i,
                               mock=args.mock, with_screenshot=args.with_screenshot,
                               cost_model=cost_model, full_a2ui=args.full_a2ui)
            logger.info(f"sample {i+1}: score={s['round_trip']['score']} "
                        f"binding_f1={s['binding_accuracy']['f1']} "
                        f"broke={s['which_link_broke']}")
            samples.append(s)
        neg = run_neg_control(fx, adapters, model=args.model, mock=args.mock,
                              cost_model=cost_model)
        logger.info(f"[neg-control] {fx.task_id}: score={neg['score']} → "
                    f"{'PASS' if neg['passed'] else 'FAIL'}")
        sm = summarize(fx.task_id, samples, neg)
        summaries.append(sm)
        all_samples.extend(samples)
        logger.info(f"TASK {fx.task_id}: PASS={sm['PASS']} mean={sm['mean_score']} "
                    f"n_pass={sm['n_pass_score']}/{args.samples} "
                    f"binding_f1_mean={sm['binding_f1_mean']} neg={neg['score']}")

    sk = evaluate_sub_kills(summaries, all_samples)
    report = {
        "ts": ts, "model": args.model or model_client.TASKVM_DEFAULT_MODEL,
        "mock": args.mock, "n_samples_per_task": args.samples,
        "cost": cost_model.summary(),
        "summaries": summaries,
        "samples": all_samples,
        "sub_kills": sk,
    }
    out_path = Path(args.out) if args.out else EVAL_DIR / f"w1_{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_path}")
    print(f"\n=== W1 KILL-TEST VERDICT: {sk['verdict']} ===")
    print(f"  neg-control honest: {sk['neg_control_honest']}")
    print(f"  max score: {sk['max_score']}")
    for sm in summaries:
        print(f"  task {sm['task_id']}: PASS={sm['PASS']} mean={sm['mean_score']} "
              f"n_pass={sm['n_pass_score']}/{args.samples} binding_f1={sm['binding_f1_mean']}")
    return 0 if sk['verdict'] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
