"""taskvm.skills — per-role distilled skill packages (R2.5 Skill-Ladder).

One subdirectory per model role — ``compiler`` / ``architect`` / ``cua``
/ ``verifier`` — each carrying a ``SKILL.md`` in the frozen three-section
format (触发条件 + 通用领域与操作先验 + 蒸馏少样本) with the anti-cheat
front-matter. The prompt-injection mechanism lives in :mod:`.loader`;
the anti-leak gate lives in ``tests/skills/`` (structure + zero
frozen-task ground truth). GenUI decoder skills live under
``taskvm/genui/skills/`` (the APP line's territory).
"""
from taskvm.skills.loader import (
    SKILL_ENV_FLAG, SKILL_ROLES, inject_skill, read_skill, skill_enabled,
)

__all__ = ["SKILL_ROLES", "SKILL_ENV_FLAG", "skill_enabled",
           "read_skill", "inject_skill"]
