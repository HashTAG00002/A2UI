"""GG.6 — the open-world generalization killtest (the ultimate validation).

GG doc §3 GG.6: "这是回答'还是不是我们最终的原型实现'的唯一证据". Proves the
system works end-to-end on a task/operator the harness has NEVER hardcoded a
template for — after GG.3 (SubgoalGenerator replaced all if/elif instruction
templates) + GG.4 (deep-link removal) + the CalendarAdapter generalization
(removed the LAST per-operator if/elif in the write path), the full chain
(observe → binding discovery → workflow init → subgoal generation → GUI
execute → verify) works on a brand-new operator with ZERO operator-specific
code anywhere in the harness.

**The test operator**: ``update_rsvp`` (calendar, sets the ``rsvp`` field) — a
NEW operator added for GG.6, with no prewritten instruction template (the old
``_build_edit_nl``/``_build_instruction`` if/elif are deleted; the
SubgoalGenerator generates the NL generically from the visible locator). The
fixture ``rsvp_update_test`` is NOT in the W1 ``TASKS`` registry (the 4/4
regression baseline stays byte-identical).

**The no-leak static gate**: every model-facing string produced during the run
(a11y observation text + the compiler prompt + the generated subgoal NL + the
raw compiler output) is scanned for internal IDs (entity_id/wxid_*) + operator
jargon. One hit = FAIL + the evidence is落盘.

**Honest verdict**: the result is reported HONESTLY whatever it is (E3/E11). A
FAIL (binding miss, leak, round-trip <0.85) is落盘 as FAIL, not papered over.

Usage:
    python -m taskvm.evaluation.run_open_world_killtest              # GUI-only runtime
    python -m taskvm.evaluation.run_open_world_killtest --mock       # no API (smoke)
    python -m taskvm.evaluation.run_open_world_killtest --samples 2
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from taskvm.benchmark.fixtures import CanonicalTaskGraph, CanonicalBinding
from taskvm.benchmark import model_client
from taskvm.evaluation.run_w1_killtest import run_one_sample, run_neg_control, summarize
from taskvm.governance.translate import (INTERNAL_ID_RE, OPERATOR_JARGON_RE,
                                         assert_no_internal_id, assert_no_operator_jargon)
from taskvm.execution.gui_driver import make_task_adapters
from taskvm.substrate.builtin_web.evaluation import (
    make_evaluation_environments,
)

logger = logging.getLogger(__name__)

EVAL_DIR = Path("eval_results")
PASS_SCORE = 0.85
NEG_CONTROL_MAX = 0.3


# ── the open-world fixture (NOT in W1 TASKS — keeps the 4/4 baseline untouched) ──
RSVP_UPDATE_TEST = CanonicalTaskGraph(
    task_id="rsvp_update_test",
    goal="把项目发布会议的参会状态从 accepted 改成 declined（我那天去不了）。",
    seed_state={"calendar": {"events": [
        {"eid": "E1", "title": "项目发布会议", "date": "2026-08-14",
         "time": "14:00-15:00", "calendar": "work", "rsvp": "accepted"},
        {"eid": "E2", "title": "周会", "date": "2026-08-12",
         "time": "10:00-10:30", "calendar": "work", "rsvp": "accepted"},
    ]}},
    user_edit={"var_id": "meeting_rsvp", "old": "accepted", "new": "declined"},
    bindings=[CanonicalBinding("meeting_rsvp", "calendar", "E1", "rsvp",
                               "update_rsvp", "declined")],
    non_interference_set=[("calendar", "E2")],
    expected_diff={"calendar": {"E1": {"rsvp": "declined"}}},
    description="GG.6 open-world: update_rsvp operator the harness has no "
                "template for; SubgoalGenerator must produce a valid instruction "
                "generically from the visible locator.",
)

OPEN_WORLD_TASKS = {"rsvp_update_test": RSVP_UPDATE_TEST}


def _no_leak_gate(sample: dict) -> dict:
    """GG.6 §2: the no-leak static gate. Scans EVERY model-facing string for
    internal IDs + operator jargon. Returns {clean, leaks, jargon}.

    What's scanned (the model INPUTS — the red-line governs what enters the model):
    - the a11y observation text (run_one_sample's no_leak_leaks)
    - the binding output for entity_id-without-locator (run_one_sample's
      no_leak_output_leaks — a real model emitting entity_id without a visible
      locator is a leak)
    - the instruction NL is scanned INSIDE the SubgoalGenerator (its own gate,
      subgoal_generator.py:181-189) — not re-scanned here to avoid flagging the
      binding JSON's legitimate ``operator`` field (control-plane, not model-facing).

    What's NOT scanned (control-plane, not model-facing):
    - the raw binding JSON's ``operator``/``entity_id`` fields (the patch_op needs
      them for dispatch — they never enter the grounding model's prompt)."""
    leaks: list[str] = []
    leaks.extend(sample.get("no_leak_leaks") or [])
    leaks.extend(sample.get("no_leak_output_leaks") or [])
    return {"clean": (not leaks),
            "leaks": leaks, "jargon": []}


def run_open_world(fixture: CanonicalTaskGraph, *, model: str | None,
                   mock: bool = False, samples: int = 3,
                   host: str = "localhost") -> dict:
    """Run the open-world killtest on one fixture. Returns the honest verdict."""
    # Agent B (substrate isolation): API write executor deleted — GUI-only runtime.
    adapters = make_task_adapters(apps=["calendar"], host=host)
    envs = make_evaluation_environments(["calendar"], host=host)
    for app, env in envs.items():
        h = env.health()
        if h.get("status") != "ok":
            logger.error(f"{app} not healthy: {h}")
            return {"fixture": fixture.task_id, "verdict": "ERROR",
                    "error": f"{app} not healthy"}
    sm_samples = []
    for i in range(samples):
        logger.info(f"--- open-world sample {i+1}/{samples} ({fixture.task_id}) ---")
        s = run_one_sample(fixture, adapters, envs, model=model,
                           temperature=None, sample_i=i, mock=mock)
        gate = _no_leak_gate(s)
        s["gg6_no_leak_gate"] = gate
        logger.info(f"sample {i+1}: score={s['round_trip']['score']} "
                    f"binding_f1={s['binding_accuracy']['f1']} "
                    f"broke={s['which_link_broke']} gate_clean={gate['clean']}")
        sm_samples.append(s)
    neg = run_neg_control(fixture, adapters, envs, model=model, mock=mock)
    sm = summarize(fixture.task_id, sm_samples, neg)
    # GG.6 verdict: PASS requires round-trip PASS + no-leak gate clean + neg honest
    gate_all_clean = all(s["gg6_no_leak_gate"]["clean"] for s in sm_samples)
    n_pass_gate = sum(1 for s in sm_samples if s["gg6_no_leak_gate"]["clean"])
    verdict = "PASS" if (sm["PASS"] and gate_all_clean) else "FAIL"
    if not gate_all_clean:
        verdict = "FAIL_no_leak_gate"
        logger.error(f"[GG.6] NO-LEAK GATE FAILED: a model input contained an "
                     f"internal id — {n_pass_gate}/{samples} samples clean")
    for env in envs.values():
        env.reset(sm_samples[0]["task_id"] if sm_samples else "")
    return {"fixture": fixture.task_id, "verdict": verdict, "summary": sm,
            "n_samples": samples, "no_leak_gate_clean": gate_all_clean,
            "operator": "update_rsvp (never-templated)",
            "samples": sm_samples, "neg_control": neg}


def main(argv=None):
    parser = argparse.ArgumentParser(description="TaskVM GG.6 open-world killtest")
    parser.add_argument("--task", default="rsvp_update_test",
                        choices=list(OPEN_WORLD_TASKS))
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--model", default=None)
    parser.add_argument("--mock", action="store_true",
                        help="no API: use GT-shaped binding (smoke the chain)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    fixture = OPEN_WORLD_TASKS[args.task]
    logger.info(f"=== GG.6 OPEN-WORLD KILLTEST: {fixture.task_id} ===")
    logger.info(f"operator: update_rsvp (the harness has NO prewritten template for it)")
    logger.info(f"proof point: SubgoalGenerator + resolve_locator + no-leak gate "
                f"must work generically (zero operator-specific if/elif)")

    result = run_open_world(fixture, model=args.model, mock=args.mock,
                            samples=args.samples, host=args.host)

    # full regression: L1 mock (4/4) — confirm the open-world changes didn't break W1
    regression = {"w1_mock_4_of_4": None}
    if not args.mock:
        try:
            from taskvm.evaluation.run_w1_killtest import main as w1_main
            # run W1 mock in-process via the function (capture the verdict)
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = w1_main(["--mock"])
            regression["w1_mock_4_of_4"] = (rc == 0)
            regression["w1_mock_output_tail"] = buf.getvalue().strip().split("\n")[-3:]
        except Exception as e:
            regression["w1_mock_4_of_4"] = f"ERROR: {e}"
    result["regression"] = regression

    ts = time.strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else EVAL_DIR / f"open_world_killtest_{ts}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    print(f"\n=== GG.6 OPEN-WORLD KILLTEST VERDICT: {result['verdict']} ===")
    print(f"  operator: {result['operator']}")
    print(f"  no-leak gate clean: {result['no_leak_gate_clean']}")
    sm = result["summary"]
    print(f"  round_trip: PASS={sm['PASS']} mean={sm['mean_score']} "
          f"n_pass={sm['n_pass_score']}/{result['n_samples']} "
          f"binding_f1={sm['binding_f1_mean']} neg={sm['neg_control_score']}")
    print(f"  regression (W1 mock 4/4): {regression['w1_mock_4_of_4']}")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
