"""Architecture gate — the executable dependency rules (handoff 02 §依赖
Gate, Wave-A review round).

AST-based (not grep). Wave-A hardening:
  - stdlib WHITELIST (sys.stdlib_module_names) for pure layers — a
    denylist of known frameworks can never enumerate the next one;
  - relative imports resolved to absolute modules
    (``from ..benchmark import x`` inside taskvm/kernel IS caught);
  - runtime may import the substrate PORT (taskvm.substrate root) but any
    CONCRETE substrate implementation subtree is banned;
  - substrate gets a reverse-dependency gate (it is the bottom layer: it
    may not import anything above it);
  - the forbidden-identifier scanner covers ast.Name / ast.Attribute /
    ast.keyword / ast.arg.

The checker core (``check_source``) takes source text + the file's
repo-relative path, so the gate is unit-testable with synthetic sources.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_STDLIB = set(sys.stdlib_module_names) | {"__future__"}


class PkgRule(NamedTuple):
    stdlib_only: bool
    allowed_taskvm: tuple[str, ...] = ()        # besides the package itself
    banned_taskvm_prefixes: tuple[str, ...] = ()


_CONCRETE_SUBSTRATES = (
    "taskvm.substrate.builtin", "taskvm.substrate.builtin_web",
    "taskvm.substrate.mobilegym", "taskvm.substrate.osworld",
)
_ALWAYS_BANNED = ("taskvm_bench",)   # enforced for EVERY package under
                                      # taskvm/ by the repo-wide gate below

_RULES: dict[str, PkgRule] = {
    # the pure core: stdlib only
    "taskvm/domain": PkgRule(stdlib_only=True),
    "taskvm/kernel": PkgRule(stdlib_only=True,
                             allowed_taskvm=("taskvm.domain",)),
    # future layers — gates arm the moment the directories exist:
    "taskvm/runtime": PkgRule(
        stdlib_only=True,
        allowed_taskvm=("taskvm.domain", "taskvm.kernel", "taskvm.substrate"),
        banned_taskvm_prefixes=_CONCRETE_SUBSTRATES),
    "taskvm/architect": PkgRule(stdlib_only=True,
                                allowed_taskvm=("taskvm.domain",
                                                "taskvm.kernel",
                                                # R2.5 Skill-Ladder (bench_
                                                # design §17.2 / master
                                                # handover §4 R2.5 card):
                                                # the frozen-layer prompt
                                                # assembly points route
                                                # system prompts through
                                                # the skill loader.
                                                "taskvm.skills")),
    "taskvm/projection": PkgRule(
        stdlib_only=False,   # the Flask layer
        allowed_taskvm=("taskvm.domain", "taskvm.kernel", "taskvm.architect",
                        "taskvm.runtime"),
        banned_taskvm_prefixes=("taskvm.substrate",)),
    # reverse gate: the bottom layer imports nothing above it
    "taskvm/substrate": PkgRule(stdlib_only=False,
                                allowed_taskvm=("taskvm.domain",)),
    # docs/contracts/runtime.md §1 (frozen): taskvm.verifier →
    # 仅 taskvm.domain + 标准库 — the runtime-visible verifier consumes
    # fresh observations + ActionContract handed in by the runtime; it
    # needs no kernel, no substrate, no architect.
    # R2.5 amendment (bench_design §17.2, the R2.5 card as RFC): the
    # verifier's prompt assembly may read the skill loader too.
    "taskvm/verifier": PkgRule(stdlib_only=True,
                               allowed_taskvm=("taskvm.domain",
                                               "taskvm.skills")),
    # R2.5 Skill-Ladder: the knowledge-asset package — a stdlib-only
    # LEAF every prompt-assembling layer may read; it imports nothing
    # above stdlib, so no reverse dependency can grow here.
    "taskvm/skills": PkgRule(stdlib_only=True),
}

# legacy cross-layer concepts banned as identifiers in the new core
_FORBIDDEN_IDENTIFIERS = {
    "entity_id", "operator", "read_canonical", "set_state",
    "move_event", "set_deadline",
}


def _pkg_of(relpath: str) -> str:
    """taskvm/kernel/foo.py → 'taskvm/kernel'."""
    return str(Path(relpath).parent)


def _resolve_from(module: str | None, level: int, relpath: str) -> str:
    """Resolve an ImportFrom to an absolute dotted module."""
    if level == 0:
        return module or ""
    parts = list(Path(relpath).parent.parts)
    # level=1 → the file's own package; level=2 → its parent; …
    base = parts[:len(parts) - level + 1]
    if module:
        base = base + module.split(".")
    return ".".join(base)


def imports_of_source(source: str, relpath: str) -> list[str]:
    """All absolute import targets in ``source`` (relative resolved)."""
    tree = ast.parse(source, filename=relpath)
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.append(_resolve_from(node.module, node.level, relpath))
    return mods


def check_import(mod: str, pkg_dir: str, rule: PkgRule) -> str | None:
    """One import → a violation string, or None."""
    if not mod:
        return None
    root = mod.split(".")[0]
    if mod.startswith("taskvm"):
        own = "taskvm." + pkg_dir.split("/")[1]
        allowed = tuple(rule.allowed_taskvm) + (own,)
        if any(mod == b or mod.startswith(b + ".") for b in _ALWAYS_BANNED):
            return f"imports always-banned subtree {mod!r}"
        if any(mod == b or mod.startswith(b + ".")
               for b in rule.banned_taskvm_prefixes):
            return f"imports banned subtree {mod!r}"
        if not any(mod == a or mod.startswith(a + ".") for a in allowed):
            return f"imports {mod!r} outside allowed {allowed!r}"
        return None
    if rule.stdlib_only and root not in _STDLIB:
        return f"imports non-stdlib module {mod!r} (pure layer is "
        "stdlib-whitelisted, not framework-denylisted)"
    return None


def check_source(source: str, relpath: str, rule: PkgRule) -> list[str]:
    pkg_dir = _pkg_of(relpath)
    out = []
    for mod in imports_of_source(source, relpath):
        v = check_import(mod, pkg_dir, rule)
        if v:
            out.append(f"{relpath}: {v}")
    return out


def _py_files(pkg_dir: str) -> list[Path]:
    root = REPO_ROOT / pkg_dir
    return sorted(root.rglob("*.py")) if root.is_dir() else []


def gate_violations(pkg_dir: str, rule: PkgRule) -> list[str]:
    out: list[str] = []
    for path in _py_files(pkg_dir):
        rel = str(path.relative_to(REPO_ROOT))
        out.extend(check_source(path.read_text(encoding="utf-8"), rel, rule))
    return out


def identifier_violations(pkg_dir: str) -> list[str]:
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
            elif isinstance(node, ast.arg):      # function parameter names
                name = node.arg
            if name in _FORBIDDEN_IDENTIFIERS:
                out.append(f"{path}:{node.lineno}: forbidden identifier "
                           f"{name!r}")
    return out


# ── the live gates ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("pkg_dir", sorted(_RULES))
def test_import_boundaries(pkg_dir: str):
    if not (REPO_ROOT / pkg_dir).is_dir():
        pytest.skip(f"{pkg_dir} does not exist yet (future wave)")
    problems = gate_violations(pkg_dir, _RULES[pkg_dir])
    assert not problems, "import boundary violations:\n" + "\n".join(problems)


@pytest.mark.parametrize("pkg_dir", ["taskvm/domain", "taskvm/kernel"])
def test_no_legacy_concepts_in_core(pkg_dir: str):
    problems = identifier_violations(pkg_dir)
    assert not problems, "legacy concept identifiers found:\n" + "\n".join(problems)


def test_core_packages_exist_and_are_pure():
    """Direct, non-parametrized: the gate can never silently skip its
    primary subjects."""
    assert _py_files("taskvm/domain"), "taskvm/domain missing"
    assert _py_files("taskvm/kernel"), "taskvm/kernel missing"
    assert not gate_violations("taskvm/domain", _RULES["taskvm/domain"])
    assert not gate_violations("taskvm/kernel", _RULES["taskvm/kernel"])


# ── regressions: the gate itself must catch what it claims to catch ───────
def test_relative_forbidden_import_is_caught():
    """from ..benchmark import x inside taskvm/kernel resolves to
    taskvm.benchmark and is rejected (outside the allowed set; the real
    bench plane taskvm_bench.* is _ALWAYS_BANNED outright)."""
    src = "from ..benchmark import fixtures\n"
    problems = check_source(src, "taskvm/kernel/virtual_mod.py",
                            _RULES["taskvm/kernel"])
    assert problems and "benchmark" in problems[0]


def test_non_stdlib_domain_import_is_caught():
    """The domain layer is stdlib-WHITELISTED: even a harmless third-party
    lib that no denylist anticipated is rejected."""
    src = "import numpy\n"
    problems = check_source(src, "taskvm/domain/virtual_mod.py",
                            _RULES["taskvm/domain"])
    assert problems and "non-stdlib" in problems[0]


def test_legit_relative_import_passes():
    src = "from .errors import ValidationError\nfrom taskvm.domain import patch\n"
    assert check_source(src, "taskvm/kernel/virtual_mod.py",
                        _RULES["taskvm/kernel"]) == []


def test_runtime_port_vs_concrete_substrate():
    rule = _RULES["taskvm/runtime"]
    ok = check_source("from taskvm.substrate import SubstrateSession\n",
                      "taskvm/runtime/virtual_mod.py", rule)
    assert ok == []                                          # the PORT is fine
    for concrete in ("taskvm.substrate.mobilegym.bridge",
                     "taskvm.substrate.osworld.agent",
                     "taskvm.substrate.builtin.server"):
        bad = check_source(f"from {concrete} import X\n",
                           "taskvm/runtime/virtual_mod.py", rule)
        assert bad and "banned subtree" in bad[0]


def test_substrate_reverse_gate():
    rule = _RULES["taskvm/substrate"]
    bad = check_source("from taskvm.projection import server\n"
                       "from taskvm.kernel import TaskVMKernel\n",
                       "taskvm/substrate/virtual_mod.py", rule)
    assert len(bad) == 2                                     # both rejected
    ok = check_source("import requests\nfrom taskvm.domain import ObservedValue\n",
                      "taskvm/substrate/virtual_mod.py", rule)
    assert ok == []   # substrate may use frameworks + domain (bottom layer)


def test_identifier_scanner_covers_function_args():
    src = "def f(entity_id):\n    return entity_id\n"
    tree = ast.parse(src)
    names = {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
    assert "entity_id" in names   # ast.arg coverage is load-bearing


# ── Wave-A.1: kernel facade encapsulation ─────────────────────────────────
_KERNEL_INTERNALS = (
    "taskvm.kernel.event_log",
    "taskvm.kernel.session_store",
    "taskvm.kernel.projection_store",
    "taskvm.kernel.workflow_store",
    "taskvm.kernel.checkpoint_store",
)


def test_upper_layers_cannot_import_kernel_store_implementation():
    """The mutable Store classes are kernel-internal. No module outside
    taskvm/kernel may import them — upper layers talk to the facade and
    its snapshots only."""
    problems: list[str] = []
    kernel_prefix = str(REPO_ROOT / "taskvm" / "kernel") + "/"
    for path in sorted((REPO_ROOT / "taskvm").rglob("*.py")):
        if str(path).startswith(kernel_prefix):
            continue  # the kernel may wire its own internals
        mods = imports_of_source(path.read_text(encoding="utf-8"),
                                 str(path.relative_to(REPO_ROOT)))
        for mod in mods:
            if any(mod == m or mod.startswith(m + ".")
                   for m in _KERNEL_INTERNALS):
                problems.append(f"{path.relative_to(REPO_ROOT)}: imports "
                                f"kernel-internal {mod!r}")
    assert not problems, "kernel encapsulation violations:\n" + \
        "\n".join(problems)


def test_kernel_facade_exports_no_mutable_stores():
    """taskvm.kernel's public surface is the facade + immutable snapshots
    only; store classes are not advertised."""
    import taskvm.kernel as K
    for name in ("EventLog", "TaskSessionStore", "ProjectionStore",
                 "WorkflowStore", "CheckpointStore"):
        assert not hasattr(K, name), f"taskvm.kernel must not export {name}"
    assert K.TaskVMKernel is not None


# ── repo-wide bench-plane ban (audit A-06) ─────────────────────────────────
# The _RULES-driven gate only covers the layers listed above; governance /
# verifier / workspace_ui / apps / thirdparty and any future package were
# reachable by _ALWAYS_BANNED only in name. This gate scans EVERY .py under
# taskvm/ so the "prototype never imports the bench plane" invariant holds
# repo-wide, regardless of what _RULES happens to enumerate.
def bench_import_violation(source: str, relpath: str) -> str | None:
    """One source file → a taskvm_bench import violation, or None.
    Absolute imports of the top-level name AND relative imports that
    resolve into the taskvm_bench tree are both caught
    (``imports_of_source`` resolves relatives against the file's own
    path, so the checker is testable with synthetic sources)."""
    for mod in imports_of_source(source, relpath):
        if mod == "taskvm_bench" or mod.startswith("taskvm_bench."):
            return (f"{relpath}: imports {mod!r} — the prototype (taskvm/) "
                    "must never import the bench plane (taskvm_bench)")
    return None


def taskvm_bench_import_violations_repo_wide() -> list[str]:
    """AST-scan every .py under taskvm/ (rglob — unlisted and future
    packages included) for taskvm_bench imports."""
    out: list[str] = []
    for path in sorted((REPO_ROOT / "taskvm").rglob("*.py")):
        rel = str(path.relative_to(REPO_ROOT))
        v = bench_import_violation(path.read_text(encoding="utf-8"), rel)
        if v:
            out.append(v)
    return out


def test_taskvm_never_imports_taskvm_bench_repo_wide():
    """The bench plane is strictly downstream: no module anywhere under
    taskvm/ — not just the _RULES-listed layers — may import taskvm_bench."""
    problems = taskvm_bench_import_violations_repo_wide()
    assert not problems, "bench-plane imports inside taskvm/:\n" + \
        "\n".join(problems)


def test_bench_gate_catches_synthetic_offenders():
    """Self-check on synthetic sources (never write a real offender
    under taskvm/): the repo-wide gate must catch absolute imports,
    from-imports and subtree imports of taskvm_bench, and leave clean
    imports alone."""
    for src in ("import taskvm_bench\n",
                "from taskvm_bench.benchmark import fixtures\n",
                "import taskvm_bench.evaluation.runner as runner\n"):
        assert bench_import_violation(
            src, "taskvm/workspace_ui/virtual_mod.py"), (
            f"gate must catch: {src!r}")
    clean = ("import json\n"
             "from taskvm.domain import patch\n"
             "from .sibling import thing\n")
    assert bench_import_violation(
        clean, "taskvm/governance/virtual_mod.py") is None
