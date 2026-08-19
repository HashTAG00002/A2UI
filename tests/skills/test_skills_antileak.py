"""tests/skills — the skills directory skeleton + anti-cheat gate
(RM1C-SKILLS / bench_design §17.2, R2.5's precondition deliverable).

What is locked:

  * STRUCTURE — one subdirectory per model role
    (taskvm/skills/{compiler,architect,cua,verifier}/), each carrying a
    SKILL.md in the frozen three-section format (触发条件 + 通用领域与
    操作先验 + 蒸馏少样本) with the anti-cheat front-matter (role /
    version / status / distill_policy). The prompt-assembly injection
    point is wired at the R2.5 stage — this gate pins the format so the
    injected content always has a parseable, hashable shape.

  * ANTI-LEAK — skill files may carry ONLY general world knowledge and
    operation priors. They must NEVER contain frozen-task ground truth:
    seed values / injection directives (seed_state, add_chats, ...),
    grading vocabulary (criterion / success_predicate / protected /
    witness), evaluation-protocol internals (fixture constant names,
    CanonicalTaskGraph), or internal ids (wxid_*, data-*-id). The
    fixture-constant blocklist is extracted DYNAMICALLY from
    taskvm_bench/benchmark/mobilegym_fixtures.py so every fixture name
    added later is locked automatically. The scan covers every *.md that
    ever lands under taskvm/skills/ — distillation content (R2.5) is
    gated from day one, not just the skeleton.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "taskvm" / "skills"
FIXTURES_PY = (REPO_ROOT / "taskvm_bench" / "benchmark"
               / "mobilegym_fixtures.py")

ROLE_DIRS = ("compiler", "architect", "cua", "verifier")

#: the frozen three-section format (bench_design §17.2: 触发条件 + 通用
#: 领域/操作先验 + 从真实成功轨迹蒸馏的少样本)
REQUIRED_SECTIONS = ("## 触发条件", "## 通用领域与操作先验", "## 蒸馏少样本")

#: front-matter every SKILL.md must carry (version+content hash go into
#: the frozen manifest at R6; the policy line pins the distill source)
REQUIRED_FRONTMATTER = ("role:", "version:", "status:", "distill_policy:")

#: frozen-task ground-truth vocabulary — hardcoded patterns
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


def _fixture_constant_names() -> list[str]:
    """All UPPER_CASE constant names defined in the bench fixtures —
    dynamically extracted (read as TEXT, no bench import) so future
    fixture names are locked the moment they are written."""
    if not FIXTURES_PY.is_file():
        return []
    src = FIXTURES_PY.read_text(encoding="utf-8")
    return re.findall(r"^([A-Z][A-Z0-9_]{3,})\s*=", src, re.MULTILINE)


def _skill_files() -> list[Path]:
    return sorted(p for p in SKILLS_DIR.rglob("*.md")
                  if p.is_file()) if SKILLS_DIR.is_dir() else []


# ── structure ───────────────────────────────────────────────────────────────

def test_one_subdirectory_per_model_role():
    for role in ROLE_DIRS:
        d = SKILLS_DIR / role
        assert d.is_dir(), (
            f"taskvm/skills/{role}/ is missing — every model role gets its "
            "own skill directory (bench_design §17.2)")
        assert (d / "SKILL.md").is_file(), (
            f"taskvm/skills/{role}/SKILL.md is missing")


def test_skill_files_use_the_frozen_three_section_format():
    files = _skill_files()
    assert files, "taskvm/skills/ carries no SKILL.md at all"
    for path in files:
        text = path.read_text(encoding="utf-8")
        for section in REQUIRED_SECTIONS:
            assert section in text, (
                f"{path.relative_to(REPO_ROOT)} lacks the section "
                f"{section!r} — the three-section format (触发条件 + 通用"
                "领域/操作先验 + 蒸馏少样本) is frozen by bench_design "
                "§17.2; the R2.5 prompt loader parses these headings")
        for field in REQUIRED_FRONTMATTER:
            assert field in text, (
                f"{path.relative_to(REPO_ROOT)} lacks front-matter "
                f"{field!r} — version+content hash go into the frozen "
                "manifest; distill_policy pins the development-split-only "
                "source")


def test_frontmatter_role_matches_directory():
    for role in ROLE_DIRS:
        text = (SKILLS_DIR / role / "SKILL.md").read_text(encoding="utf-8")
        m = re.search(r"^role:\s*(\S+)", text, re.MULTILINE)
        assert m and m.group(1) == role, (
            f"taskvm/skills/{role}/SKILL.md declares role {m and m.group(1)!r} "
            f"— it must match its directory ({role!r})")


# ── anti-leak: general knowledge only, zero frozen-task ground truth ────────

def test_skill_files_carry_no_frozen_task_ground_truth():
    files = _skill_files()
    assert files
    fixture_names = _fixture_constant_names()
    offenders: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(REPO_ROOT))
        for pattern, why in _FORBIDDEN_PATTERNS:
            for hit in re.findall(pattern, text):
                offenders.append(f"{rel}: {hit!r} ({why})")
        for name in fixture_names:
            if re.search(rf"\b{re.escape(name)}\b", text):
                offenders.append(f"{rel}: {name!r} (bench fixture constant)")
    assert not offenders, (
        "skill files leaked frozen-task ground truth — skills may carry "
        "ONLY general world knowledge / operation priors (bench_design "
        "§17.2 anti-cheat rule 1); offenders:\n" + "\n".join(offenders))


def test_fixture_constants_are_actually_extracted():
    """Self-check of the dynamic blocklist: the extraction must find the
    known fixture constants, so the anti-leak gate above is never silently
    scanning against an empty list."""
    names = _fixture_constant_names()
    assert "SOCIAL_MORNING_BRIEF" in names and "EXPENSE_AND_NOTIFY" in names, (
        "fixture-constant extraction came up empty — the dynamic "
        "anti-leak blocklist is broken, not the skills")


def test_distill_policy_pins_development_split_only():
    for role in ROLE_DIRS:
        text = (SKILLS_DIR / role / "SKILL.md").read_text(encoding="utf-8")
        assert "development split" in text and "held-out" in text, (
            f"taskvm/skills/{role}/SKILL.md must pin the distill policy: "
            "development-split successful trajectories ONLY, held-out "
            "variants NEVER participate (bench_design §17.2 rule 2)")
