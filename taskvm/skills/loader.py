"""taskvm.skills.loader — the SKILL.md prompt-injection mechanism (R2.5).

The Skill-Ladder's harness side (bench_design §17.2): each model role
(compiler / architect / cua / verifier) owns a ``SKILL.md`` under
``taskvm/skills/<role>/`` carrying distilled general world knowledge and
operation priors. The frozen-layer prompt assembly points route their
system prompts through :func:`inject_skill` — the ONLY wiring the R2.5
card authorizes inside the frozen layers.

Design rules:

* **OFF is the default and is byte-identical.** With the env flag unset
  (or off/0/false) ``inject_skill`` returns the very SAME string object
  it was handed — the assembled prompt is unchanged down to identity,
  which the loader regression tests pin.
* **A skeleton injects nothing.** A SKILL.md whose front-matter status
  starts with ``skeleton`` carries no distilled content yet; injection
  is honestly a no-op until distillation flips the status. This keeps
  the L0 before/after comparison clean: OFF vs ON differ only by real
  distilled content, never by placeholder text.
* **The flag admits subsets.** ``TASKVM_SKILL_INJECTION=architect,cua``
  turns the mechanism on for named roles only — the L0 experiment loads
  one role's skill at a time.
* **No new leak surface.** The loader appends the SKILL.md BODY (the
  front-matter never enters a prompt); the appended text then flows
  through the caller's existing no-leak gate like any other prompt
  text — the gate at each assembly point covers skill content for free.
* **No caching.** The file is re-read on every call (a few KB against
  a seconds-scale model call) so a freshly distilled skill takes effect
  on the very next assembly — the right behaviour while the ladder
  iterates.
"""
from __future__ import annotations

import os
import re

__all__ = ["SKILL_ROLES", "SKILL_ENV_FLAG", "skill_enabled",
           "read_skill", "inject_skill"]

#: the four model roles with a skill directory (bench_design §17.2)
SKILL_ROLES = ("compiler", "architect", "cua", "verifier")

#: the env flag: unset / off / 0 / false → OFF (default); on / 1 / true
#: → every role; otherwise a comma-separated role subset
SKILL_ENV_FLAG = "TASKVM_SKILL_INJECTION"

_OFF_VALUES = frozenset({"", "off", "0", "false", "no"})
_ON_VALUES = frozenset({"on", "1", "true", "yes", "all"})

_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL)

#: the fixed separator the injected skill lands under — part of the
#: frozen mechanism, so the prompt shape is reproducible and hashable
_INJECTION_HEADER = "\n\n---\n\n# 通用先验知识（skill）\n\n"


def _flag() -> str:
    return (os.environ.get(SKILL_ENV_FLAG) or "").strip()


def skill_enabled(role: str) -> bool:
    """Is injection ON for ``role`` under the current env flag?"""
    flag = _flag().lower()
    if flag in _OFF_VALUES:
        return False
    if flag in _ON_VALUES:
        return role in SKILL_ROLES
    return role in {r.strip() for r in flag.split(",") if r.strip()}


def read_skill(role: str) -> str | None:
    """The role's SKILL.md BODY (front-matter stripped) or ``None``.

    ``None`` when: the file is absent, the front-matter is missing
    (unparseable → inject nothing, never a malformed prompt), the body
    is empty, or the status starts with ``skeleton`` (no distilled
    content yet — an honest no-op, not placeholder injection)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        role, "SKILL.md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    m = _FRONTMATTER.match(text)
    if m is None:
        return None
    status = re.search(r"^status:[ \t]*(.+)$", m.group(1), re.MULTILINE)
    if status and status.group(1).strip().lower().startswith("skeleton"):
        return None
    body = text[m.end():]
    body = body.strip()
    return body or None


def inject_skill(role: str, system_prompt: str) -> str:
    """Append the role's distilled skill to a system prompt.

    OFF (the default) returns ``system_prompt`` UNCHANGED — the same
    object, so the assembled prompt stays byte-identical to the
    no-skill build. ON with a distilled (non-skeleton) SKILL.md returns
    the prompt extended under the fixed separator; ON with nothing to
    inject is the same-object no-op. The result must flow through the
    caller's existing no-leak gate — the loader adds no gate of its
    own and no new vocabulary."""
    if not skill_enabled(role):
        return system_prompt
    body = read_skill(role)
    if body is None:
        return system_prompt
    return system_prompt + _INJECTION_HEADER + body + "\n"
