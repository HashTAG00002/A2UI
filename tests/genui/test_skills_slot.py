"""tests/genui/test_skills_slot — the GenUI decoder's distillation slot
(A4 step4, workplan §20.3).

The genui decoder gets the SAME skill discipline as the four frozen-layer
roles (bench_design §17.2, mirrored from tests/skills/test_skills_antileak.py
— the blocklists are duplicated because tests/ has no package structure;
keep the two in sync when the canonical list grows):

  * STRUCTURE — taskvm/genui/skills/SKILL.md in the frozen three-section
    format with the anti-cheat front-matter (role / version / status /
    distill_policy);
  * ANTI-LEAK — general priors only, zero frozen-task ground truth (seed
    directives, grading vocabulary, evaluation-protocol internals, bench
    fixture constant names, internal ids);
  * SLOT HYGIENE — no *.py ever lands under taskvm/genui/skills/ (the
    genui import gate scans top-level *.py only; a stray module in the
    subdirectory would bypass it);
  * HONEST STATUS — this slot shipped with real starter content from the
    2026-08-20 A4 real run, so its status must not claim to be a bare
    skeleton (a skeleton status would make a future loader honestly
    inject NOTHING while the file LOOKS distilled).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SLOT = REPO_ROOT / "taskvm" / "genui" / "skills" / "SKILL.md"
FIXTURES_PY = (REPO_ROOT / "taskvm_bench" / "benchmark"
               / "mobilegym_fixtures.py")

REQUIRED_SECTIONS = ("## 触发条件", "## 通用领域与操作先验", "## 蒸馏少样本")
REQUIRED_FRONTMATTER = ("role:", "version:", "status:", "distill_policy:")

#: frozen-task ground-truth vocabulary — SAME list as
#: tests/skills/test_skills_antileak.py (keep the two in sync)
_FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bseed_state\b", "seed injection directive"),
    (r"\badd_chats\b", "seed injection directive"),
    (r"\badd_contacts\b", "seed injection directive"),
    (r"\bcriterion\b", "grading (checkpoint criterion) vocabulary"),
    (r"\bsuccess_predicate\b", "grading vocabulary"),
    (r"\bCanonicalTaskGraph\b", "evaluation-protocol internal"),
    (r"\bprotected\b", "evaluation-protocol vocabulary"),
    (r"\bwitness\b", "evaluation-protocol vocabulary"),
    (r"\bwxid_[A-Za-z0-9_]+\b", "internal wechat id"),
    (r"\bdata-[a-z-]*id\b", "DOM-internal attribute"),
    (r"\bp_\d{10,}\b", "internal post id shape"),
    (r"黄勇", "frozen-task seed persona"),
    (r"核心CPI", "frozen-task seed content token"),
)


def _text() -> str:
    assert SLOT.is_file(), (
        "taskvm/genui/skills/SKILL.md is missing — the decoder's "
        "distillation slot (workplan §20.3) must exist from day one")
    return SLOT.read_text(encoding="utf-8")


# ── structure ───────────────────────────────────────────────────────────────

def test_slot_uses_the_frozen_three_section_format():
    text = _text()
    for section in REQUIRED_SECTIONS:
        assert section in text, (
            f"the genui skill lacks section {section!r} — the three-section "
            "format is frozen by bench_design §17.2")
    for field in REQUIRED_FRONTMATTER:
        assert field in text, f"front-matter field {field!r} is required"


def test_slot_declares_the_genui_decoder_role():
    m = re.search(r"^role:\s*(\S+)", _text(), re.MULTILINE)
    assert m and m.group(1) == "genui_decoder"


def test_slot_status_is_distilled_not_skeleton():
    """This slot shipped with real starter content (2026-08-20 A4 run
    priors); a `skeleton` status would make a future loader honestly
    inject NOTHING while the file looks distilled — the status must
    stay honest."""
    m = re.search(r"^status:\s*(.+)$", _text(), re.MULTILINE)
    assert m, "status: line is required"
    assert not m.group(1).strip().lower().startswith("skeleton"), (
        "the genui slot carries real distilled priors — its status must "
        "not claim skeleton (a skeleton status makes injection a no-op)")


# ── anti-leak ───────────────────────────────────────────────────────────────

def test_slot_carries_no_frozen_task_ground_truth():
    text = _text()
    offenders: list[str] = []
    for pattern, why in _FORBIDDEN_PATTERNS:
        for hit in re.findall(pattern, text):
            offenders.append(f"{hit!r} ({why})")
    if FIXTURES_PY.is_file():
        fixture_src = FIXTURES_PY.read_text(encoding="utf-8")
        for name in re.findall(r"^([A-Z][A-Z0-9_]{3,})\s*=", fixture_src,
                               re.MULTILINE):
            if re.search(rf"\b{re.escape(name)}\b", text):
                offenders.append(f"{name!r} (bench fixture constant)")
    assert not offenders, (
        "the genui skill leaked frozen-task ground truth — skills may "
        f"carry ONLY general priors: {offenders}")


# ── slot hygiene ────────────────────────────────────────────────────────────

def test_slot_directory_stays_python_free():
    """The genui import gate scans taskvm/genui/*.py (top level only);
    a stray .py module inside skills/ would bypass it. Markdown only."""
    offenders = sorted(
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / "taskvm" / "genui" / "skills").rglob("*.py"))
    assert not offenders, (
        f"taskvm/genui/skills/ must stay .py-free (import-gate blind "
        f"spot): {offenders}")
