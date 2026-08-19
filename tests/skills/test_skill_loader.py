"""tests/skills — the R2.5 skill LOADER regression gate.

What is locked (the R2.5 card / bench_design §17.2):

* **OFF is byte-identical.** With ``TASKVM_SKILL_INJECTION`` unset (the
  default) or explicitly off, ``inject_skill`` returns the VERY SAME
  string object it was handed — the assembled prompts of all four
  frozen-layer assembly points stay byte-identical to the no-skill
  build. This is the load-bearing guarantee of the whole mechanism:
  the L0 before/after comparison must differ ONLY by real distilled
  content, never by the wiring itself.
* **A skeleton injects nothing.** Every SKILL.md whose front-matter
  status starts with ``skeleton`` is an honest no-op even with the
  flag ON — placeholder text never reaches a real prompt.
* **Subsets.** The flag admits comma-separated role lists so the L0
  experiment can load one role's skill at a time.
* **The wiring cannot silently rot.** A static source check pins that
  every assembly point routes its system prompt through
  ``inject_skill`` — a bare ``system=_SYSTEM_PROMPT`` added later would
  bypass the mechanism undetected.
* **Injected content rides the existing no-leak gates.** The assembly
  points inject BEFORE ``assert_prompt_clean`` / the verifier's
  ``prompt_gate``, so a distilled skill is scanned like any other
  prompt text — no new gate, no new vocabulary, no bypass.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from taskvm.skills.loader import (
    SKILL_ENV_FLAG, SKILL_ROLES, inject_skill, read_skill, skill_enabled,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "taskvm" / "skills"

#: the four frozen-layer prompt assembly points (file, role, the bare
#: prompt constant whose uses must all be routed through inject_skill)
ASSEMBLY_POINTS = (
    ("taskvm/architect/architect.py", "architect", "_SYSTEM_PROMPT"),
    ("taskvm/architect/compiler.py", "compiler", "_SYSTEM_PROMPT"),
    ("taskvm/verifier/model_verifier.py", "verifier", "_SYSTEM_PROMPT"),
    ("taskvm/workspace_ui/composition.py", "cua", "_CUA_SYSTEM_PROMPT"),
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from the documented default: flag unset."""
    monkeypatch.delenv(SKILL_ENV_FLAG, raising=False)


# ── OFF is byte-identical (the load-bearing guarantee) ──────────────────────

def test_unset_flag_returns_the_same_object():
    prompt = "You are the Task Architect."
    out = inject_skill("architect", prompt)
    assert out is prompt          # identity, not just equality


def test_explicit_off_values_return_the_same_object(monkeypatch):
    prompt = "system prompt"
    for value in ("", "off", "0", "false", "no", "OFF", " Off "):
        monkeypatch.setenv(SKILL_ENV_FLAG, value)
        assert inject_skill("cua", prompt) is prompt, value


def test_on_with_a_skeleton_status_file_is_still_the_same_object(
        monkeypatch, tmp_path):
    """A SKILL.md whose front-matter status starts with ``skeleton``
    injects NOTHING even with the flag ON — placeholder text never
    reaches a real prompt. Pinned with a FIXTURE file (not the repo's
    real files) so the lock stays valid after real distillation
    lands (the L0 v1 distillation flipped the repo files on
    2026-08-20)."""
    (tmp_path / "cua").mkdir()
    (tmp_path / "cua" / "SKILL.md").write_text(
        "---\nrole: cua\nversion: 0.1.0\n"
        "status: skeleton — no distilled content yet\ndistill_policy: x\n"
        "---\n\n## 触发条件\n- 占位\n", encoding="utf-8")
    monkeypatch.setattr("taskvm.skills.loader.__file__",
                        str(tmp_path / "loader.py"))
    monkeypatch.setenv(SKILL_ENV_FLAG, "on")
    prompt = "prompt"
    assert inject_skill("cua", prompt) is prompt


def test_missing_role_directory_is_off(monkeypatch):
    monkeypatch.setenv(SKILL_ENV_FLAG, "on")
    assert inject_skill("nonexistent-role", "prompt") is "prompt"


# ── ON semantics ────────────────────────────────────────────────────────────

def test_on_with_distilled_content_appends_the_body(monkeypatch):
    monkeypatch.setenv(SKILL_ENV_FLAG, "on")
    monkeypatch.setattr(
        "taskvm.skills.loader.read_skill",
        lambda role: "## 触发条件\n- 总是\n\n## 通用领域与操作先验\n- 先验内容")
    out = inject_skill("architect", "base prompt")
    assert out.startswith("base prompt")
    assert "先验内容" in out
    assert "通用先验知识" in out          # the fixed separator header
    assert "role:" not in out             # front-matter never enters


def test_role_subset_selection(monkeypatch):
    monkeypatch.setenv(SKILL_ENV_FLAG, "architect,cua")
    assert skill_enabled("architect") is True
    assert skill_enabled("cua") is True
    assert skill_enabled("verifier") is False
    assert skill_enabled("compiler") is False


def test_on_flag_variants_enable_every_role(monkeypatch):
    for value in ("on", "1", "true", "yes", "all"):
        monkeypatch.setenv(SKILL_ENV_FLAG, value)
        assert all(skill_enabled(r) for r in SKILL_ROLES), value


def test_read_skill_of_the_real_files_is_distilled_v1():
    """After the L0 distillation (2026-08-20, eval_results/
    skill_ladder_l0_20260820/) the four real SKILL.md files carry
    distilled v1 content: every role returns a non-empty body in the
    frozen three-section shape the loader can inject."""
    for role in SKILL_ROLES:
        body = read_skill(role)
        assert body, role
        for section in ("## 触发条件", "## 通用领域与操作先验",
                        "## 蒸馏少样本"):
            assert section in body, (role, section)


# ── the wiring cannot silently rot ─────────────────────────────────────────

def test_every_assembly_point_routes_through_inject_skill():
    """A bare ``system=_PROMPT`` at an assembly point would bypass the
    mechanism silently — the static check pins the routed shape."""
    for rel, role, constant in ASSEMBLY_POINTS:
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert f'inject_skill("{role}", {constant})' in src, (
            f"{rel}: the {role} assembly point must route its system "
            f"prompt through inject_skill (R2.5)")
        bare = f"system={constant}"
        assert bare not in src, (
            f"{rel}: a bare ``{bare}`` bypasses the skill loader")


def test_assembly_point_files_exist():
    for rel, _role, _constant in ASSEMBLY_POINTS:
        assert (REPO_ROOT / rel).is_file(), rel


# ── injected content rides the existing no-leak gates ──────────────────────

def test_injected_content_passes_the_no_leak_gate(monkeypatch):
    """The assembly points inject BEFORE the gate; a skill body that
    carries forbidden vocabulary must therefore FAIL the gate — proven
    here with the real gate on a synthetic offending body."""
    from taskvm.architect.noleak import assert_prompt_clean
    monkeypatch.setenv(SKILL_ENV_FLAG, "on")
    clean_body = "## 通用领域与操作先验\n- 账单入口通常在底部 Tab"
    monkeypatch.setattr("taskvm.skills.loader.read_skill",
                        lambda role: clean_body)
    prompt = inject_skill("architect", "base")
    assert_prompt_clean(prompt + "\nuser text", what="test")
    # and a leaking body IS caught by the same composition
    monkeypatch.setattr("taskvm.skills.loader.read_skill",
                        lambda role: "内部 entity_id 泄漏")
    bad = inject_skill("architect", "base")
    with pytest.raises(Exception):
        assert_prompt_clean(bad + "\nuser text", what="test")
