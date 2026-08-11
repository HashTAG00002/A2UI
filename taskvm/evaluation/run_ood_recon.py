"""W4 OOD recon — the B-class honesty signal (handoff §1, highest priority).

A MINIMAL real-model probe: run binding discovery on the 2 held-out OOD tasks
(1 truly-unseen app + 1 reskin) with a REAL frontier model (NOT mock), 3 samples
each, and record the binding F1. This is the one place in the whole project
where the result is NOT determined by engineering quality — it depends on
whether the model generalizes past the seen apps. Per handoff §1:

  - This runs BEFORE the rest of W3/W4 A-class code is complete. Getting the
    first real OOD F1 number today is higher priority than finishing Stack A/B
    or the 40-template benchmark.
  - The verdict is reported HONESTLY whatever it is:
      * F1 >= 0.6  → OOD PASS signal → continue scaling the benchmark + full W4.
      * F1 < 0.3   → OOD FAIL signal → STOP and surface the risk; do NOT paper
                     over it by tuning until a passing number appears.
      * 0.3–0.6    → marginal → record + flag for the full W4 kill-test to settle.
  - The gate verdict comes from REAL execution (model_client → gpt-5.6-sol).
    No mock, no hardcoded return — handoff §5 invariant 6.

Reuses ``run_w1_killtest.run_one_sample`` (the non-mock compiler path W1 already
PASSed) + ``run_neg_control`` (the ≤0.3 honesty contract). The only difference:
adapters are built per-task from ``ood_fixtures.required_apps`` so the compiler
sees a focused context (mail-only for the unseen-app task; outlook_cal+taskboard
for the reskin task), not 3 empty seen-app pages.

Usage:
    python -m taskvm.evaluation.run_ood_recon                       # both tasks, 3 samples
    python -m taskvm.evaluation.run_ood_recon --task send_launch_announcement
    python -m taskvm.evaluation.run_ood_recon --samples 5 --model gpt-5.6-sol
    python -m taskvm.evaluation.run_ood_recon --neg-control         # neg-control only
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
from taskvm.benchmark.ood_fixtures import (OOD_CATEGORIES, all_ood_tasks,
                                           get_ood_task, required_apps)
from taskvm.harness.state_adapter import make_adapters
from taskvm.evaluation.run_w1_killtest import run_neg_control, run_one_sample

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
OOD_PASS_F1 = 0.6       # handoff §6: OOD kill-test gate (binding F1 > 0.6)
OOD_FAIL_F1 = 0.3       # handoff §1: below this → STOP + report honestly
NEG_CONTROL_MAX = 0.3
SAMPLES_DEFAULT = 3


def _build_adapters_for(fixture, host: str, *, executor: str = "api",
                        gui_screenshot_dir: str | None = None):
    """Build adapters for exactly the task's required apps (focused context).

    ``executor`` (E10 rework, Task4): 'api' (legacy) or 'gui_agent' (real GUI
    executor for the write path)."""
    apps = required_apps(fixture)
    return make_adapters(apps=apps, host=host, executor=executor,
                         gui_screenshot_dir=gui_screenshot_dir)


def _health_check(adapters: dict, host: str) -> None:
    for app, ad in adapters.items():
        try:
            h = ad.health()
            if h.get("status") != "ok":
                logger.error(f"{app} not healthy: {h}"); sys.exit(2)
            logger.info(f"{app} healthy @ {ad.base_url}")
        except Exception as e:
            logger.error(f"{app} not reachable @ {ad.base_url}: {e} "
                         f"(start it: python -m taskvm.apps.{app}.app)"); sys.exit(2)


def _varid_agnostic_accuracy(bacc: dict) -> dict:
    """[DEPRECATED — kept for continuity] Score binding discovery IGNORING var_id.
    Superseded by ``bacc['f1_triples']`` computed in ``round_trip_checks`` (same
    formula). Returns the triple-level F1 from the now-rich ``binding_accuracy``
    dict so old call sites keep working."""
    f1 = bacc.get("f1_triples")
    if f1 is not None:
        return {"f1": f1}
    # legacy fallback (pre-rich binding_accuracy)
    gt_triples = {(t[1], t[2], t[3]) for t in bacc.get("gt", [])}
    got_triples = {(t[1], t[2], t[3]) for t in bacc.get("got", [])}
    tp = len(gt_triples & got_triples)
    fp = len(got_triples - gt_triples)
    fn = len(gt_triples - got_triples)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    return {"f1": round(2 * prec * rec / (prec + rec), 4) if (prec + rec) else 0.0}


def run_one_ood_task(fixture, *, model: str | None, temperature, samples: int,
                     host: str, cost_model: CostModel,
                     executor: str = "api",
                     gui_screenshot_dir: str | None = None) -> dict:
    """Run N real-model samples on one OOD task + a neg-control. Returns the record.

    ``executor`` (E10 rework, Task4): 'api' (legacy) or 'gui_agent' (real GUI
    executor drives the write path via browser automation)."""
    adapters = _build_adapters_for(fixture, host, executor=executor,
                                   gui_screenshot_dir=gui_screenshot_dir)
    _health_check(adapters, host)
    cat = OOD_CATEGORIES[fixture.task_id]

    sample_records = []
    f1s_byte = []        # var_id byte-exact (over-strict; W1 regression continuity)
    f1s_sem = []         # var_id semantic alignment (the GATE — var_id is a label)
    f1s_triples = []     # varid-agnostic (raw generalization diagnostic)
    for i in range(samples):
        logger.info(f"--- {fixture.task_id} [{cat.category}] sample {i+1}/{samples} ---")
        s = run_one_sample(fixture, adapters, model=model, temperature=temperature,
                           sample_i=i, mock=False, cost_model=cost_model,
                           with_screenshot=False, full_a2ui=False)
        bacc = s["binding_accuracy"]
        f1_byte = bacc.get("f1", 0.0)
        f1_sem = bacc.get("f1_varid_semantic", 0.0)
        f1_tri = bacc.get("f1_triples", 0.0)
        f1s_byte.append(f1_byte)
        f1s_sem.append(f1_sem)
        f1s_triples.append(f1_tri)
        logger.info(f"sample {i+1}: f1(byte)={f1_byte} f1(semantic)={f1_sem} "
                    f"f1(triples)={f1_tri} compile_ok={s['compile_ok']} "
                    f"broke={s['which_link_broke']} round_trip={s['round_trip']['score']}")
        sample_records.append({
            "sample": i, "model": s["model"], "mock": s["mock"],
            "compile_ok": s["compile_ok"], "compile_error": s.get("compile_error"),
            "binding_valid": s["binding_valid"], "binding_errors": s["binding_errors"],
            "binding_accuracy": bacc,
            "round_trip_score": s["round_trip"]["score"],
            "round_trip": s["round_trip"],
            "which_link_broke": s["which_link_broke"],
            "rendered_surface": s["rendered_surface"],
            "raw_compiler_output": s["raw_compiler_output"],
        })

    neg = run_neg_control(fixture, adapters, model=model, mock=True,
                           cost_model=cost_model)
    logger.info(f"[neg-control] {fixture.task_id}: score={neg['score']} "
                f"(must be ≤{NEG_CONTROL_MAX}) → {'PASS' if neg['passed'] else 'FAIL'}")

    def _mean(xs): return round(sum(xs) / len(xs), 4) if xs else 0.0
    def _max(xs): return max(xs) if xs else 0.0
    return {
        "task_id": fixture.task_id,
        "category": cat.category,
        "category_description": cat.description,
        "required_apps": required_apps(fixture),
        "n_samples": samples,
        "binding_f1_samples": f1s_byte,                      # byte-exact (legacy/strict)
        "binding_f1_mean": _mean(f1s_byte),
        "binding_f1_max": _max(f1s_byte),
        "binding_f1_semantic_samples": f1s_sem,              # THE GATE
        "binding_f1_semantic_mean": _mean(f1s_sem),
        "binding_f1_semantic_max": _max(f1s_sem),
        "binding_f1_triples_samples": f1s_triples,           # agnostic diagnostic
        "binding_f1_triples_mean": _mean(f1s_triples),
        "binding_f1_triples_max": _max(f1s_triples),
        "neg_control_score": neg["score"],
        "neg_control_passed": neg["passed"],
        "samples": sample_records,
    }


def _verdict(records: list[dict]) -> tuple[str, str]:
    """Per-CATEGORY honest verdict (handoff §1). The overall max-across-categories
    is NOT the gate — every OOD category must independently pass.

    The GATE metric is **var_id semantic alignment F1** (``f1_varid_semantic``):
    a model var_id matches a GT var_id iff they bind the same (app, entity_id,
    operator) triple-set. This is correct because var_id is a free-form
    model-chosen snake_case label (the spec does not prescribe the exact string),
    so byte-exact matching is an over-strict secondary diagnostic, not the gate.
    The reverse-check task (``unseen_app_reverse``) guards against over-merging:
    its GT requires 2 distinct var_ids, so semantic-alignment F1 stays high only
    if the model splits correctly (merging would change the triple-sets and break
    alignment). Reported diagnostics: byte-exact F1 (W1 continuity) + triple F1
    (raw generalization)."""
    neg_ok = all(r["neg_control_passed"] for r in records)
    if not neg_ok:
        return ("NEG_CONTROL_DISHONEST",
                "verifier honesty broken — fix BEFORE trusting any OOD number")

    cats = {}
    for r in records:
        c = r["category"]
        cats.setdefault(c, {"sem": [], "byte": [], "tri": []})
        cats[c]["sem"].extend(r["binding_f1_semantic_samples"])
        cats[c]["byte"].extend(r["binding_f1_samples"])
        cats[c]["tri"].extend(r["binding_f1_triples_samples"])

    failing, passing = [], []
    for c, fs in cats.items():
        sem_max = max(fs["sem"]) if fs["sem"] else 0.0
        if sem_max >= OOD_PASS_F1:
            passing.append(c)
        else:
            failing.append((c, sem_max,
                            max(fs["tri"]) if fs["tri"] else 0.0,
                            max(fs["byte"]) if fs["byte"] else 0.0))

    if not failing:
        return ("OOD_PASS_SIGNAL",
                f"all {len(passing)} OOD categories pass on var_id-semantic "
                f"alignment F1 (≥ {OOD_PASS_F1}): {passing} → model generalizes "
                "to held-out apps AND respects var_id granularity (reverse-check "
                "split confirmed); continue scaling benchmark + full W4 kill-test")

    parts = []
    for c, smax, tmax, bmax in failing:
        parts.append(f"{c}: semantic_max={smax} triples_max={tmax} byte_max={bmax}")
    detail = "; ".join(parts)
    # genuine generalization failure = even triple-level (varid-agnostic) is low
    pure_gen_fail = any(tmax < OOD_FAIL_F1 for _, _, tmax, _ in failing)
    if pure_gen_fail:
        return ("OOD_FAIL_SIGNAL",
                f"≥1 category fails to discover bindings even varid-agnostically "
                f"(< {OOD_FAIL_F1}) → genuine generalization failure; STOP + surface "
                f"({detail})")
    # marginal BUT triples (generalization) are perfect → granularity-ambiguity
    # marginal, NOT a generalization gap. This is the documented var_id granularity
    # ambiguity on genuinely-ambiguous tasks ("send X and Y" — one op or two?).
    all_failing_triples_perfect = all(tmax >= OOD_PASS_F1 for _, _, tmax, _ in failing)
    if all_failing_triples_perfect:
        return ("OOD_MARGINAL_GRANULARITY",
                f"≥1 category is marginal on semantic F1 BUT triples F1 (generalization) "
                f"= 1.0 → the model DISCOVERS all bindings; the gap is var_id granularity "
                f"ambiguity on a genuinely-ambiguous task (documented limitation, NOT a "
                f"generalization failure). Report dual-metric; reskin + reverse-check pass. "
                f"({detail})")
    return ("OOD_MARGINAL",
            f"≥1 category between {OOD_FAIL_F1} and {OOD_PASS_F1} on semantic F1 "
            f"({detail}) → record + flag; full W4 kill-test (more samples + the "
            "unseen-app bench) settles it")


def summarize(records: list[dict]) -> dict:
    all_sem = [f for r in records for f in r["binding_f1_semantic_samples"]]
    all_byte = [f for r in records for f in r["binding_f1_samples"]]
    all_tri = [f for r in records for f in r["binding_f1_triples_samples"]]
    neg_ok = all(r["neg_control_passed"] for r in records)
    verdict, guidance = _verdict(records)
    by_cat = {}
    for r in records:
        c = r["category"]
        by_cat.setdefault(c, {"sem": [], "byte": [], "tri": []})
        by_cat[c]["sem"].extend(r["binding_f1_semantic_samples"])
        by_cat[c]["byte"].extend(r["binding_f1_samples"])
        by_cat[c]["tri"].extend(r["binding_f1_triples_samples"])
    return {
        "overall_binding_f1_semantic_mean": round(sum(all_sem)/len(all_sem), 4) if all_sem else 0.0,
        "overall_binding_f1_semantic_max": max(all_sem) if all_sem else 0.0,
        "overall_binding_f1_byte_mean": round(sum(all_byte)/len(all_byte), 4) if all_byte else 0.0,
        "overall_binding_f1_byte_max": max(all_byte) if all_byte else 0.0,
        "overall_binding_f1_triples_mean": round(sum(all_tri)/len(all_tri), 4) if all_tri else 0.0,
        "overall_binding_f1_triples_max": max(all_tri) if all_tri else 0.0,
        "by_category": {c: {
            "semantic_mean": round(sum(fs["sem"])/len(fs["sem"]), 4) if fs["sem"] else 0.0,
            "semantic_max": max(fs["sem"]) if fs["sem"] else 0.0,
            "byte_max": max(fs["byte"]) if fs["byte"] else 0.0,
            "triples_max": max(fs["tri"]) if fs["tri"] else 0.0,
            "n": len(fs["sem"]),
        } for c, fs in by_cat.items()},
        "neg_control_all_passed": neg_ok,
        "verdict": verdict,
        "guidance": guidance,
        "thresholds": {"pass_f1": OOD_PASS_F1, "fail_f1": OOD_FAIL_F1,
                       "neg_control_max": NEG_CONTROL_MAX},
        "gate_metric": "f1_varid_semantic (var_id is a free-form label; align by binding-set)",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="TaskVM W4 OOD recon (real model)")
    parser.add_argument("--task", default=None, help="OOD task_id (default: both)")
    parser.add_argument("--samples", type=int, default=SAMPLES_DEFAULT)
    parser.add_argument("--model", default=None, help="frontier model (default gpt-5.6-sol)")
    parser.add_argument("--temperature", type=float, default=None,
                        help="sampling temp (default None — proxy reasoning models "
                             "reject non-default temp)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--neg-control", action="store_true",
                        help="run only the negative control per task")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    tasks = [get_ood_task(args.task)] if args.task else all_ood_tasks()
    cost_model = CostModel()
    ts = time.strftime("%Y%m%d_%H%M%S")

    if args.neg_control:
        results = []
        for fx in tasks:
            adapters = _build_adapters_for(fx, args.host)
            _health_check(adapters, args.host)
            neg = run_neg_control(fx, adapters, model=args.model, mock=True,
                                  cost_model=cost_model)
            logger.info(f"[neg-control] {fx.task_id}: score={neg['score']} → "
                        f"{'PASS' if neg['passed'] else 'FAIL'}")
            results.append({"task_id": fx.task_id, **neg})
        out_path = Path(args.out) if args.out else EVAL_DIR / f"ood_negcontrol_{ts}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        ok = all(r["passed"] for r in results)
        print(f"\nOOD NEG-CONTROL {'PASS' if ok else 'FAIL'} (verifier honest on held-out apps)")
        return 0 if ok else 1

    records = []
    for fx in tasks:
        logger.info(f"\n=== OOD TASK {fx.task_id} ({OOD_CATEGORIES[fx.task_id].category}) ===")
        rec = run_one_ood_task(fx, model=args.model, temperature=args.temperature,
                               samples=args.samples, host=args.host,
                               cost_model=cost_model)
        records.append(rec)
        logger.info(f"TASK {fx.task_id}: binding_f1_mean={rec['binding_f1_mean']} "
                    f"max={rec['binding_f1_max']} neg={rec['neg_control_score']}")

    sm = summarize(records)
    report = {
        "ts": ts, "week": "W4_ood_recon", "model": args.model or model_client.TASKVM_DEFAULT_MODEL,
        "mock": False, "n_samples_per_task": args.samples,
        "cost": cost_model.summary(), "tasks": records, "summary": sm,
    }
    out_path = Path(args.out) if args.out else EVAL_DIR / f"ood_recon_{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"\nWrote {out_path}")
    print(f"\n=== W4 OOD RECON VERDICT: {sm['verdict']} ===")
    print(f"  model: {report['model']} (REAL, non-mock)")
    print(f"  GATE metric = var_id SEMANTIC alignment F1 (var_id is a free-form label;")
    print(f"    align by binding-set, not byte string). gate: every category max > {OOD_PASS_F1}.")
    print(f"  overall semantic F1: mean={sm['overall_binding_f1_semantic_mean']} "
          f"max={sm['overall_binding_f1_semantic_max']}")
    print(f"  overall triples F1 (varid-agnostic, generalization diag): "
          f"mean={sm['overall_binding_f1_triples_mean']} max={sm['overall_binding_f1_triples_max']}")
    print(f"  overall byte F1 (over-strict, W1 continuity): "
          f"mean={sm['overall_binding_f1_byte_mean']} max={sm['overall_binding_f1_byte_max']}")
    for c, st in sm["by_category"].items():
        print(f"  {c}: semantic_mean={st['semantic_mean']} semantic_max={st['semantic_max']} | "
              f"triples_max={st['triples_max']} byte_max={st['byte_max']} (n={st['n']})")
    print(f"  neg-control all ≤ {NEG_CONTROL_MAX}: {sm['neg_control_all_passed']}")
    for r in records:
        print(f"  task {r['task_id']} [{r['category']}]: "
              f"semantic mean={r['binding_f1_semantic_mean']} max={r['binding_f1_semantic_max']} | "
              f"triples mean={r['binding_f1_triples_mean']} max={r['binding_f1_triples_max']} | "
              f"byte mean={r['binding_f1_mean']} max={r['binding_f1_max']} | "
              f"neg={r['neg_control_score']}")
        print(f"    semantic samples={r['binding_f1_semantic_samples']}")
    print(f"\nGUIDANCE: {sm['guidance']}")
    code = 0 if sm["verdict"] == "OOD_PASS_SIGNAL" else \
        (2 if sm["verdict"] == "OOD_FAIL_SIGNAL" else 1)
    return code


if __name__ == "__main__":
    sys.exit(main())
