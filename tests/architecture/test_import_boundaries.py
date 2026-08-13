"""Architecture gate — the executable dependency rules (handoff 02 §依赖 Gate).

AST-based (not grep): parses every .py under the governed packages and
inspects Import / ImportFrom nodes. A violation fails with the exact file
and the offending import. This is the mechanism that stops the next
layering drift BEFORE it lands (master handoff §3.1: 依赖方向只能向下).

Rules enforced while the refactor proceeds in waves:
  - taskvm.domain  : stdlib only (no taskvm.* at all, no frameworks)
  - taskvm.kernel  : stdlib + taskvm.domain only
  - taskvm.domain/kernel never import: flask / playwright / openai /
    requests / aiohttp / benchmark / evaluation / any concrete substrate /
    harness / workspace_ui / execution / apps / baselines / the migration
    compatibility layer
  - forbidden IDENTIFIERS (not just imports) in domain/kernel: the legacy
    cross-layer concepts that must not become kernel semantics
    (storage primary keys, app-internal operation names, hidden-state
    readers). Checked on Name/Attribute/keyword nodes so comments and
    docstrings stay free to *describe* the ban.
  - future layers (runtime / projection / architect) get their gates the
    moment the directories exist — the rule table is already in place.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# ── import rules ───────────────────────────────────────────────────────────
_FRAMEWORKS = {"flask", "playwright", "openai", "requests", "aiohttp",
               "flask_socketio", "socketio"}

# package dir → allowed taskvm subtrees (the package itself is always
# implicitly allowed; everything else under taskvm is banned)
_RULES: dict[str, tuple[str, ...]] = {
    "taskvm/domain": (),
    "taskvm/kernel": ("taskvm.domain",),
    # waves 1-2 will fill these in; the gates arm themselves automatically:
    "taskvm/runtime": ("taskvm.domain", "taskvm.kernel"),
    "taskvm/architect": ("taskvm.domain", "taskvm.kernel"),
    "taskvm/projection": ("taskvm.domain", "taskvm.kernel",
                          "taskvm.architect", "taskvm.runtime"),
}

# benchmark/evaluation are banned for every production layer above
_ALWAYS_BANNED = ("taskvm.benchmark", "taskvm.evaluation")

# legacy cross-layer concepts banned as identifiers in the new core
_FORBIDDEN_IDENTIFIERS = {
    "entity_id", "operator", "read_canonical", "set_state",
    "move_event", "set_deadline",
}


def _py_files(pkg_dir: str) -> list[Path]:
    root = REPO_ROOT / pkg_dir
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.py"))


def _imports_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.append(node.module)
    return mods


def _violations(pkg_dir: str, allowed_taskvm: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for path in _py_files(pkg_dir):
        for mod in _imports_of(path):
            root = mod.split(".")[0]
            if root in _FRAMEWORKS:
                out.append(f"{path}: imports framework {mod!r}")
            if mod.startswith("taskvm"):
                own = "taskvm." + pkg_dir.split("/")[1]
                allowed = tuple(allowed_taskvm) + (own,)
                if any(mod == b or mod.startswith(b + ".")
                       for b in _ALWAYS_BANNED):
                    out.append(f"{path}: imports banned subtree {mod!r}")
                elif not any(mod == a or mod.startswith(a + ".")
                             for a in allowed):
                    out.append(
                        f"{path}: imports {mod!r} outside allowed "
                        f"{allowed!r}")
    return out


def _identifier_violations(pkg_dir: str) -> list[str]:
    out: list[str] = []
    for path in _py_files(pkg_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.keyword):
                name = node.arg
            if name in _FORBIDDEN_IDENTIFIERS:
                out.append(f"{path}:{node.lineno}: forbidden identifier "
                           f"{name!r}")
    return out


@pytest.mark.parametrize("pkg_dir,allowed", sorted(_RULES.items()))
def test_import_boundaries(pkg_dir: str, allowed: tuple[str, ...]):
    if not (REPO_ROOT / pkg_dir).is_dir():
        pytest.skip(f"{pkg_dir} does not exist yet (future wave)")
    problems = _violations(pkg_dir, allowed)
    assert not problems, "import boundary violations:\n" + "\n".join(problems)


@pytest.mark.parametrize("pkg_dir", ["taskvm/domain", "taskvm/kernel"])
def test_no_legacy_concepts_in_core(pkg_dir: str):
    problems = _identifier_violations(pkg_dir)
    assert not problems, "legacy concept identifiers found:\n" + "\n".join(problems)


def test_core_packages_exist_and_are_pure():
    """The two new packages must exist and contain no framework imports —
    a direct, non-parametrized assertion so the gate can never silently
    skip its primary subjects."""
    assert _py_files("taskvm/domain"), "taskvm/domain missing"
    assert _py_files("taskvm/kernel"), "taskvm/kernel missing"
    assert not _violations("taskvm/domain", ())
    assert not _violations("taskvm/kernel", ("taskvm.domain",))
