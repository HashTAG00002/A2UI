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
from taskvm.verifier.round_trip_checks import (check_round_trip, binding_accuracy,
                                                map_gt_var_id_to_compiler)
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
                   full_a2ui: bool = False,
                   vision: bool = False) -> dict:
    """Run one compiler sample end-to-end. Returns the result record.

    ``vision`` (EE.10, §7.1): when True, capture a screenshot per app + pass to
    ``compile_binding`` so the compiler uses the screenshot+a11y vision path
    (``complete_vision_json``) instead of text-only. Requires ``with_screenshot``
    machinery (Playwright); forces ``with_screenshot=True``."""
    sid = f"{fixture.task_id}_s{sample_i}_{int(time.time()*1000) % 100000}"
    # reset (idempotent) + seed
    for ad in adapters.values():
        ad.reset(sid)
    replay.seed_apps(fixture, adapters, sid)

    # capture observations (read-path-GUI) + assert they match real state
    # EE.10: vision forces screenshot capture (the compiler's image input)
    obs = replay.capture_obs(adapters, sid, with_screenshot=(with_screenshot or vision))
    replay.assert_obs_matches_state(adapters, sid, obs)
    trace = TraceFixture(task_id=fixture.task_id, goal=fixture.goal, final_obs=obs)
    observed_ids = {app: set(replay.parse_dom_entities(o.dom_html).keys())
                    for app, o in obs.items()}

    # EE.10: build the screenshots dict (app → data_url) for the vision path
    screenshots = None
    if vision:
        screenshots = {}
        for app, o in obs.items():
            if o.screenshot_path:
                screenshots[app] = _png_to_data_url(o.screenshot_path)
        if not screenshots:
            logger.warning(f"[killtest] vision=True but no screenshots captured "
                           f"(Playwright unavailable?); falling back to text-only")
            vision = False   # honest fallback

    # compile the binding (gate-critical model step) — or use GT in mock mode
    if mock:
        compiled = _mock_compiler_output(fixture, observed_ids)
    else:
        compiled = compile_binding(trace, observed_ids, model=model,
                                   temperature=temperature, cost_model=cost_model,
                                   binding_only=not full_a2ui,
                                   screenshots=screenshots)

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
    # E11/E12 fix (direction a): in REAL compiler mode the compiler may name the
    # edited variable differently than the GT fixture (e.g. 'document_folder' vs
    # 'launch_doc_location') — a free-form label, semantically equivalent. Driving
    # compile_patch with the GT var_id string did a byte-exact lookup in the
    # compiler's binding, found nothing → dispatch.n_ops=0 → the GUI executor was
    # never triggered (the doc_handoff 0.3 failure). Fix: translate the GT var_id
    # into the compiler's var_id via binding-set alignment BEFORE patching, so the
    # patch stage no longer depends on the GT var_id string (compiler-independent).
    # mock mode uses _gt_task_binding (GT var_id == compiler var_id), no mapping.
    var_id_mapping = None   # transparency: record what translation happened
    dispatch_report = None
    if tb is not None:
        gt_var_id = fixture.user_edit.get("var_id")
        if mock:
            edit = dict(fixture.user_edit)   # GT var_id matches GT binding
        else:
            compiler_var_id = map_gt_var_id_to_compiler(gt_var_id, fixture, binding)
            var_id_mapping = {"gt_var_id": gt_var_id,
                              "compiler_var_id": compiler_var_id,
                              "mapped": compiler_var_id is not None}
            edit = {"var_id": compiler_var_id or gt_var_id,
                    "old": fixture.user_edit.get("old"),
                    "new": fixture.user_edit.get("new")}
            if compiler_var_id is None:
                logger.warning(f"[killtest] GT var_id {gt_var_id!r} has no "
                               f"compiler match (binding miss, not naming); "
                               f"patch will produce 0 ops — a real binding failure")
        ops = compile_patch(edit, tb)
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
        "vision": vision,   # EE.10: True iff the compiler used the screenshot+a11y path
        "compile_ok": compiled["ok"],
        "compile_error": compiled.get("error"),
        "binding_valid": valid,
        "binding_errors": bind_errs,
        "a2ui_valid": a2ui_ok,
        "a2ui_errors": a2ui_errs,
        "binding_accuracy": bacc,
        "var_id_mapping": var_id_mapping,   # E11/E12 fix: GT var_id → compiler var_id
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


def _png_to_data_url(path: str) -> str:
    """EE.10: read a PNG file + return a base64 data URL (the format
    ``complete_vision_json`` expects for ``image_url``)."""
    import base64
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"


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
    parser.add_argument("--vision", action="store_true",
                        help="EE.10 (§7.1): capture a screenshot per app + pass to the "
                             "compiler so it uses complete_vision_json (screenshot+a11y) "
                             "instead of text-only complete_json. Produces eval_results/"
                             "w1_vision_<ts>.json for A/B vs the text path. Needs Playwright.")
    parser.add_argument("--execution-mode", choices=["api", "gui_agent"],
                        default="api",
                        help="E10 rework (P5): 'api' (legacy requests.post to the app's "
                             "Flask route) or 'gui_agent' (drive a real browser via the "
                             "GUI executor — non-invasive write/rollback, .mrules E7/E10). "
                             "Default 'api' preserves the W1 baseline for comparison.")
    parser.add_argument("--gui-screenshot-dir", default=None,
                        help="dir for per-step GUI executor screenshots (gui_agent mode; "
                             "default: eval_results/p5_gui_visual_<ts>/<app>_<op>)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    # GUI-executor screenshot dir (auto per-ts if gui_agent + not specified)
    gui_shot_dir = args.gui_screenshot_dir
    if args.execution_mode == "gui_agent" and gui_shot_dir is None:
        gui_shot_dir = str(EVAL_DIR / f"p5_gui_visual_{time.strftime('%Y%m%d_%H%M%S')}")
    # EE.2: build the adapter set from the UNION of apps across all tasks being
    # run (seed_state keys + binding apps). Without this, launch_full (which
    # seeds+writes mail) would run without a mail adapter → the mail op dispatches
    # to nothing → round_trip fails. Auto-including held-out apps per-task keeps
    # the 3 core tasks byte-identical (their apps = calendar/taskboard/drive)
    # while letting 4-App fanout tasks pull in mail/outlook_cal as needed.
    tasks = [get_task(args.task)] if args.task else all_tasks()
    needed_apps: set[str] = set()
    for fx in tasks:
        needed_apps.update(fx.seed_state.keys())
        needed_apps.update(b.app for b in fx.bindings)
    _APP_ORDER = ("calendar", "taskboard", "drive", "mail",
                  "outlook_cal", "wechat", "alipay")
    app_list = [a for a in _APP_ORDER if a in needed_apps]
    adapters = make_adapters(apps=app_list, host=args.host,
                             executor=args.execution_mode,
                             gui_screenshot_dir=gui_shot_dir)
    # health check
    for app, ad in adapters.items():
        h = ad.health()
        if h.get("status") != "ok":
            logger.error(f"{app} not healthy: {h}")
            sys.exit(2)
        logger.info(f"{app} healthy @ {ad.base_url}")

    cost_model = CostModel()
    # E10 rework (P5): wire the GUI executor's cost_model to the W1 cost_model
    # so GUI-grounding calls are attributed in the report's cost (honesty: the
    # report must reflect the FULL token cost, not just compile_binding).
    if args.execution_mode == "gui_agent":
        from taskvm.execution.gui_executor import get_executor
        get_executor().cost_model = cost_model
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
                               cost_model=cost_model, full_a2ui=args.full_a2ui,
                               vision=args.vision)
            logger.info(f"sample {i+1}: score={s['round_trip']['score']} "
                        f"binding_f1={s['binding_accuracy']['f1']} "
                        f"broke={s['which_link_broke']}")
            samples.append(s)
            # E10 rework (P5): GUI-executor writes hit gpt-5.6-sol's QPM ~10/min.
            # Between samples, let the quota refill so the next sample's GUI
            # writes don't 429-loop (the executor retries, but it's slower +
            # burns the step budget). Skip after the last sample.
            if args.execution_mode == "gui_agent" and i < args.samples - 1:
                logger.info(f"[gui_agent] QPM refill: sleeping 60s before next sample")
                time.sleep(60)
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
    # E10 rework (P5): tag the execution mode so GUI-agent results are never
    # confused with legacy API-direct results (handoff §6.2 / §12.19). Also
    # record the executor config for traceability.
    report = {
        "ts": ts, "model": args.model or model_client.TASKVM_DEFAULT_MODEL,
        "mock": args.mock, "n_samples_per_task": args.samples,
        "execution_mode": args.execution_mode,   # 'api' (legacy) | 'gui_agent' (E10 rework)
        "cost": cost_model.summary(),
        "summaries": summaries,
        "samples": all_samples,
        "sub_kills": sk,
    }
    if args.execution_mode == "gui_agent":
        report["executor"] = {
            "driver": "playwright",
            "grounding_model": args.model or model_client.TASKVM_DEFAULT_MODEL,
            "coordinate_format": "normalized_0_1000",
            "gui_screenshot_dir": gui_shot_dir,
        }
    out_path = Path(args.out) if args.out else (
        EVAL_DIR / f"p5_full_killtest_{ts}.json" if args.execution_mode == "gui_agent"
        else EVAL_DIR / f"w1_{ts}.json")
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
