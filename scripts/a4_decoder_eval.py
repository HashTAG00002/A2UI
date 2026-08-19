#!/usr/bin/env python
"""A4 decoder acceptance — REAL model calls (the fake-port tier lives in
tests/genui/test_acceptance.py; this script is the real-provider tier).

The A4 acceptance instrument (workplan §7-P3 DoD + §16): three mutually
unrelated UNSEEN goals (different app domains, different variable shapes —
the SAME snapshots the acceptance test uses) are decoded through the REAL
GenUIDecoder against the REAL provider (gpt-5.6-sol via the company FRIDAY
gateway) with the REAL two-layer gate. Evidence archived verbatim under
--out:

  goal{N}_components.json  the validated component tree (what would ship)
  goal{N}_result.json      DecodeResult.summary() + tree fingerprints +
                           the deterministic data model the tree binds to
  goal{N}_prompt.txt       the EXACT system+user prompt sent (verbatim)
  goal{N}_reply_raw.txt    the EXACT raw model reply (verbatim, both rounds)
  ledger.json              the shared ModelCallLedger snapshot (one row per
                           real provider request — the single-owner contract)
  run_summary.json         the verdict + the three trees' difference
                           fingerprints (sha256 / histogram / depth / count)

Verdict (no LLM judge, ever — deterministic fingerprints only):
  PASS  = all three goals decoded with source=model (no fallback) AND the
          three tree sha256 hashes are pairwise distinct AND the component
          type histograms are pairwise distinct.
  FAIL  = anything else (a fallback is an HONEST failure for this gate —
          the baseline surface works, but A4's "3 distinct generated trees"
          claim is not met). Evidence is kept either way.

Methodology notes:
  - 429 rate-limit handling lives HERE (outer backoff), not in the port:
    HttpModelPort's single-owner contract (one complete_json = one provider
    request = one ledger row) is preserved — the outer loop simply re-runs
    the decode with a fresh round when every attempt died in transport
    (gateway §B.1: 429/5xx are transient, 401/403/402 are fatal and
    abort immediately).
  - The decoder's bounded-repair round is part of the SUT: a repair that
    SAVES the run counts as source=model with is_repair=True rows in the
    ledger (honest accounting, not a failure).

Usage:
  OPENAI_API_KEY=... python scripts/a4_decoder_eval.py \
      --out eval_results/a4_decoder_20260820

Exit codes: 0 = PASS · 1 = graded FAIL (evidence kept) · 2 = infrastructure
error · 3 = env-gated skip (no OPENAI_API_KEY — real-model gate never
silently runs on a fake port).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taskvm.architect.http_port import HttpModelPort
from taskvm.architect.port import ModelCallLedger
from taskvm.genui.context import TaskSurfaceContextBuilder
from taskvm.genui.data_model import TaskDataModelProjector
from taskvm.genui.decoder import GenUIDecoder, SOURCE_MODEL

# ── three UNSEEN goals (same snapshots as tests/genui/test_acceptance.py) ──

GOALS: list[dict[str, Any]] = [
    {   # 1. alarm clock (date/number/string mix, Tabs-shaped domain)
        "slug": "alarm",
        "snapshot": {
            "governance": {"goal": "明早 6:30 叫我起床赶高铁",
                           "autonomy": "ready"},
            "variables": [
                {"key": "alarm_time", "label": "闹钟时间",
                 "value_type": "date", "observed": "2026-08-20T07:00",
                 "desired": "2026-08-21T06:30", "mutability": "editable"},
                {"key": "repeat_mode", "label": "重复方式",
                 "value_type": "string", "observed": "仅一次",
                 "desired": None, "mutability": "readonly"},
                {"key": "volume", "label": "铃声音量",
                 "value_type": "number", "observed": 5, "desired": 7,
                 "mutability": "editable"},
                {"key": "alarm_label", "label": "闹钟备注",
                 "value_type": "string", "observed": "闹钟",
                 "desired": "赶高铁起床", "mutability": "editable"},
            ],
            "workflow": {"has_plan": True, "nodes": []},
        },
    },
    {   # 2. weather digest (readonly-heavy digest, Card-shaped domain)
        "slug": "weather",
        "snapshot": {
            "governance": {"goal": "查一下这周末北京的天气，如果下雨就提醒我带伞",
                           "autonomy": "ready"},
            "variables": [
                {"key": "city", "label": "城市", "value_type": "string",
                 "observed": "北京", "desired": None,
                 "mutability": "readonly"},
                {"key": "weekend_forecast", "label": "周末预报",
                 "value_type": "string",
                 "observed": "周六小雨 22°C，周日多云 25°C",
                 "desired": None, "mutability": "readonly"},
                {"key": "rain_alert", "label": "下雨提醒",
                 "value_type": "boolean", "observed": False,
                 "desired": True, "mutability": "editable"},
            ],
            "workflow": {"has_plan": True, "nodes": []},
        },
    },
    {   # 3. group messaging (text form, Column-shaped domain)
        "slug": "message",
        "snapshot": {
            "governance": {"goal": "给项目群里发消息说设计稿已更新，并@一下设计师",
                           "autonomy": "ready"},
            "variables": [
                {"key": "target_group", "label": "目标群聊",
                 "value_type": "string", "observed": "TaskVM 项目组",
                 "desired": None, "mutability": "readonly"},
                {"key": "message_text", "label": "消息内容",
                 "value_type": "text", "observed": "",
                 "desired": "设计稿已更新，请查收最新版本",
                 "mutability": "editable"},
                {"key": "mention_list", "label": "提醒谁看",
                 "value_type": "string", "observed": "",
                 "desired": "@设计师小王", "mutability": "editable"},
                {"key": "send_status", "label": "发送状态",
                 "value_type": "status", "observed": "未发送",
                 "desired": None, "mutability": "readonly"},
            ],
            "workflow": {"has_plan": True, "nodes": []},
        },
    },
]

_FATAL_STATUS_HINTS = ("401", "403", "402", "欠费", "配额不足")


class _ArchivingPort:
    """Wraps the HttpModelPort; keeps every verbatim exchange in memory.

    The production RecordingModelPort attributes roles by system-prompt
    prefix and does not know the genui decoder yet, so this script keeps
    its own per-goal archive — the same full-fidelity discipline (exact
    prompt, exact raw reply, usage) scoped to one run.
    """

    def __init__(self, inner: HttpModelPort) -> None:
        self._inner = inner
        self.exchanges: list[dict[str, str]] = []

    def complete_json(self, *, system: str, user: str,
                      model: str | None = None, max_tokens: int = 3072,
                      temperature: float | None = None,
                      image_data_url: str | None = None) -> Any:
        reply = self._inner.complete_json(
            system=system, user=user, model=model, max_tokens=max_tokens,
            temperature=temperature, image_data_url=image_data_url)
        self.exchanges.append({
            "system": system, "user": user,
            "raw": getattr(reply, "raw", "") or "",
            "reply_model": getattr(reply, "model", "") or "",
        })
        return reply


# ── deterministic tree fingerprints (no LLM judge, ever) ───────────────────

def _canonical(tree: list[dict[str, Any]]) -> str:
    return json.dumps(tree, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _tree_sha256(tree: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical(tree).encode("utf-8")).hexdigest()


def _histogram(tree: list[dict[str, Any]]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for comp in tree:
        hist[comp.get("component", "?")] = (
            hist.get(comp.get("component", "?"), 0) + 1)
    return hist


def _max_depth(tree: list[dict[str, Any]]) -> int:
    by_id = {c["id"]: c for c in tree}
    memo: dict[str, int] = {}

    def depth(cid: str, seen: frozenset[str]) -> int:
        if cid in memo:
            return memo[cid]
        if cid in seen:            # cycle guard — a malformed tree still
            return 1               # fingerprints deterministically
        children = by_id.get(cid, {}).get("children") or []
        d = 1 + max((depth(ch, seen | {cid})
                     for ch in children if isinstance(ch, str)
                     and ch in by_id), default=0)
        memo[cid] = d
        return d

    return depth("root", frozenset()) if "root" in by_id else 0


def _all_transport_failures(result: Any) -> bool:
    """True when every model attempt died in transport (429/timeout…) —
    the signal for an outer backoff retry. Validation rejections and
    unparseable replies are REAL model answers: re-rolling those would be
    fishing for a luckier sample, so they never trigger an outer retry."""
    model_attempts = [a for a in result.attempts
                      if a.purpose != "surface_fallback"]
    return bool(model_attempts) and all(
        any(e.startswith("model call failed:") for e in a.errors)
        for a in model_attempts)


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="eval_results/a4_decoder_20260820",
                        help="archive directory (created; never git-added)")
    parser.add_argument("--model", default=os.environ.get(
        "TASKVM_GENUI_DECODER_MODEL", "gpt-5.6-sol"))
    parser.add_argument("--outer-retries", type=int, default=3,
                        help="decode re-rolls allowed when every attempt "
                             "died in transport (rate limit)")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("[env-gated skip] OPENAI_API_KEY is not set — this is a "
              "REAL-MODEL gate; a fake port can never stand in. "
              "(exit 3, nothing archived)")
        return 3

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    port = _ArchivingPort(HttpModelPort())
    ledger = ModelCallLedger()
    decoder = GenUIDecoder(port, ledger, model=args.model)
    builder = TaskSurfaceContextBuilder()
    projector = TaskDataModelProjector()

    per_goal: list[dict[str, Any]] = []
    fatal = False

    for goal in GOALS:
        slug = goal["slug"]
        context = builder.build(goal["snapshot"])
        exchanges_before = len(port.exchanges)

        result = None
        for attempt in range(1, 1 + max(1, args.outer_retries)):
            result = decoder.decode(context, surface_id=f"a4-{slug}")
            if result.source == SOURCE_MODEL:
                break
            # honest fallback — inspect WHY before any re-roll
            if _all_transport_failures(result):
                if attempt < max(1, args.outer_retries):
                    wait = min(2 ** attempt, 16)
                    print(f"[{slug}] transport failure on round {attempt} "
                          f"(rate limit?) — backing off {wait}s",
                          file=sys.stderr)
                    time.sleep(wait)
                    continue
            last_err = " | ".join(
                e for a in result.attempts for e in a.errors)[:300]
            if any(h in last_err for h in _FATAL_STATUS_HINTS):
                fatal = True
            break  # validation-level fallback: NO fishing for a luckier roll

        assert result is not None
        fingerprint = {
            "slug": slug,
            "source": result.source,
            "model_calls": result.model_calls,
            "component_count": len(result.components),
            "component_histogram": _histogram(result.components),
            "max_depth": _max_depth(result.components),
            "tree_sha256": _tree_sha256(result.components),
        }
        per_goal.append(fingerprint)

        # ── archive everything verbatim ──
        (out / f"goal_{slug}_components.json").write_text(
            json.dumps(result.components, ensure_ascii=False, indent=2),
            encoding="utf-8")
        (out / f"goal_{slug}_result.json").write_text(json.dumps(
            {"fingerprint": fingerprint,
             "decode_summary": result.summary(),
             "data_model": projector.project(context)},
            ensure_ascii=False, indent=2), encoding="utf-8")
        goal_exchanges = port.exchanges[exchanges_before:]
        lines = [f"# {slug}: {len(goal_exchanges)} provider request(s)"]
        for i, ex in enumerate(goal_exchanges, 1):
            lines.append(f"\n===== request {i} SYSTEM =====\n{ex['system']}"
                         f"\n===== request {i} USER =====\n{ex['user']}")
        (out / f"goal_{slug}_prompt.txt").write_text(
            "\n".join(lines), encoding="utf-8")
        (out / f"goal_{slug}_reply_raw.txt").write_text("\n".join(
            f"===== request {i} ({ex['reply_model']}) RAW REPLY =====\n"
            f"{ex['raw'] or '(empty reply — see ledger error fields)'}"
            for i, ex in enumerate(goal_exchanges, 1)),
            encoding="utf-8")

        print(f"[{slug}] source={result.source} "
              f"calls={result.model_calls} "
              f"components={len(result.components)} "
              f"sha256={fingerprint['tree_sha256'][:12]}")

    (out / "ledger.json").write_text(
        json.dumps(ledger.snapshot(), ensure_ascii=False, indent=2),
        encoding="utf-8")

    # ── deterministic verdict ──
    all_model = all(g["source"] == SOURCE_MODEL for g in per_goal)
    shas = [g["tree_sha256"] for g in per_goal]
    hists = [json.dumps(g["component_histogram"], sort_keys=True)
             for g in per_goal]
    distinct_shas = len(set(shas)) == len(shas)
    distinct_hists = len(set(hists)) == len(hists)
    verdict = "PASS" if (all_model and distinct_shas
                         and distinct_hists) else "FAIL"

    run_summary = {
        "gate": "A4 decoder acceptance (real model)",
        "model": args.model,
        "verdict": verdict,
        "criteria": {
            "all_three_source_model": all_model,
            "pairwise_distinct_tree_sha256": distinct_shas,
            "pairwise_distinct_component_histograms": distinct_hists,
        },
        "goals": per_goal,
        "ledger_rows": len(ledger.snapshot()),
        "note": ("fake-port tier: tests/genui/test_acceptance.py (Set A: "
                 "3 unseen goals -> 3 distinct validated trees; value "
                 "changes -> 0 GenUI calls). This archive is the "
                 "real-provider tier."),
    }
    (out / "run_summary.json").write_text(
        json.dumps(run_summary, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print(f"verdict: {verdict} — archive: {out}")
    if fatal:
        print("note: a FATAL gateway condition (auth/quota) was observed — "
              "check ledger.json error fields", file=sys.stderr)
        return 2
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
