"""Layering declaration — taskvm.genui is a plain-JSON port layer: it may
import nothing from other taskvm layers (kernel/projection/governance/
substrate/architect/runtime). The composition root adapts snapshots into
the public dict shape; substrate independence holds by construction."""
from __future__ import annotations

import ast
from pathlib import Path

GENUI_DIR = Path(__file__).resolve().parents[2] / "taskvm" / "genui"


def _imported_modules(tree: ast.AST) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.add(node.module)
    return mods


def test_genui_imports_no_other_taskvm_layer():
    offenders: dict[str, set[str]] = {}
    for py in GENUI_DIR.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        bad = {m for m in _imported_modules(tree)
               if m.startswith("taskvm.")
               and not m.startswith("taskvm.genui")}
        if bad:
            offenders[py.name] = bad
    assert not offenders, (
        f"genui layer imports other taskvm layers: {offenders}")
