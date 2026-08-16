"""tests/substrate/test_no_api_backdoor.py — the substrate contract's
API-backdoor + layering gate (B-3, Oracle audit 2026-08-15).

The frozen contract (docs/contracts/substrate.md) requires this exact file.
It locks, mechanically and forever:

  1. FACADE PURITY — ``taskvm.substrate`` root exports Protocol / DTO /
     Registry ONLY. (B-2: the root once exported ``builtin_web_app_url`` /
     ``mobilegym_bridge_url`` — an import leak hidden behind a fake port
     surface; ruled a BLOCKER and deleted.)
  2. RUNTIME SESSIONS HAVE NO SETUP POWERS — no SubstrateSession
     implementation may define reset/seed/set_state/get_state/oracle/…
     (those live on EvaluationEnvironment only).
  3. THE RUNTIME PLANE NEVER SWITCHES REALITY — bridge runtime methods
     (observe / act_primitive / task-level mutates) must not call
     ``_activate`` / ``env.reset`` / ``env.set_state`` (B-1: the
     "session context switching" teleport underneath the runtime).
  4. NO EXECUTOR KNOB ANYWHERE — no ``--executor``, no ``executor="api"``,
     no ``CLI_EXECUTOR`` in any taskvm source (E30's grep acceptance,
     promoted to a permanent test so the argparse-choices false-negative
     class can never regress).
  5. UPPER-LAYER SUBSTRATE KNOWLEDGE IS QUARANTINED — the transitional
     platform knowledge in the legacy execution layer (gui_driver) and
     the oracle-derived anchor lookup in workspace_ui are REGISTERED
     VIOLATIONS of §6, enumerated in the Transitional Debt Register
     (docs/contracts/substrate.md §8) and mirrored here as
     TRANSITIONAL_DEBT_REGISTER. The quarantine test only enforces
     NO GROWTH during the transition — it is NOT a contract-satisfaction
     check and may not be cited as one (audit B-F1). Formal LOCK is a
     separate, explicit audit: TASKVM_SUBSTRATE_LOCK_AUDIT=1.

Run: ``python -m pytest tests/substrate -q``.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SUBSTRATE = REPO_ROOT / "taskvm" / "substrate"

# ── 1. facade purity ────────────────────────────────────────────────────────

#: The complete, frozen export surface of ``taskvm.substrate``. Protocol
#: types, DTOs, registries — nothing else. Adding an entry needs a contract
#: amendment, not just a code change.
FROZEN_FACADE_EXPORTS = {
    "ActionReceipt",
    "EvaluationEnvironment",
    "EvaluationProvider",
    "EvaluationRegistry",
    "GUI_ACTION_KINDS",
    "GuiAction",
    "IrreversibleAction",
    "Observation",
    "SubstrateProvider",
    "SubstrateRegistry",
    "SubstrateSession",
    "SubstrateUnavailable",
    "SurfaceHandle",
    "SurfaceInfo",
    "VisualArtifact",
    "evaluation_registry",
    "scrub_hidden_ids",
    "substrate_registry",
}


def test_facade_exports_protocol_dto_registry_only():
    import taskvm.substrate as facade
    exported = set(facade.__all__)
    assert exported == FROZEN_FACADE_EXPORTS, (
        f"substrate root facade drifted: +{sorted(exported - FROZEN_FACADE_EXPORTS)} "
        f"-{sorted(FROZEN_FACADE_EXPORTS - exported)}. The root is Protocol/DTO/"
        "Registry ONLY (B-2, Oracle audit): URL/port/config helpers belong in "
        "the provider config or a composition root, never the port facade.")
    for name in ("builtin_web_app_url", "mobilegym_bridge_url"):
        assert not hasattr(facade, name), (
            f"facade must not carry substrate-specific URL helpers ({name}) — "
            "they hide the import leak while keeping the semantic leak")


# ── 2. runtime sessions have no setup powers ────────────────────────────────

RUNTIME_SESSION_FILES = [
    SUBSTRATE / "builtin_web" / "session.py",
    SUBSTRATE / "mobilegym" / "session.py",
    SUBSTRATE / "osworld" / "session.py",
]

#: Methods that must NEVER appear on a SubstrateSession implementation —
#: they are EvaluationEnvironment (exam-room) powers.
SETUP_POWER_METHODS = {
    "reset", "seed", "set_state", "get_state", "inject_task",
    "mutate", "read_canonical", "oracle_state", "snapshot", "restore",
}


def test_runtime_sessions_have_no_setup_powers():
    problems: list[str] = []
    for path in RUNTIME_SESSION_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and item.name in SETUP_POWER_METHODS):
                        problems.append(
                            f"{path.relative_to(REPO_ROOT)}:"
                            f"{node.name}.{item.name}()")
    assert not problems, (
        "SubstrateSession implementations carry setup-plane powers "
        f"(exam-room only): {problems}. reset/seed/set_state/oracle live on "
        "EvaluationEnvironment and must never be reachable from a runtime "
        "session (contract §2/§4).")


# ── 3. the runtime plane never switches reality (B-1) ──────────────────────

BRIDGE = SUBSTRATE / "mobilegym" / "bridge.py"

#: bridge methods on the RUNTIME plane (backing /api/observe, /api/act and
#: the legacy operator routes). ``_require_active`` must gate them and the
#: reality-switching calls must be absent.
BRIDGE_RUNTIME_METHODS = ("observe", "act_primitive",
                          "mutate_wechat", "mutate_x")
REALITY_SWITCH_CALLS = ("_activate", "env.reset", "env.set_state")


def _method_source(class_def: ast.ClassDef, name: str) -> ast.AST | None:
    for item in class_def.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and item.name == name:
            return item
    return None


def test_bridge_runtime_plane_never_switches_reality():
    tree = ast.parse(BRIDGE.read_text(encoding="utf-8"), filename=str(BRIDGE))
    bridge_cls = next(n for n in tree.body
                      if isinstance(n, ast.ClassDef)
                      and n.name == "MobileGymBridge")
    problems: list[str] = []

    guard = _method_source(bridge_cls, "_require_active")
    assert guard is not None, (
        "MobileGymBridge._require_active is missing — the runtime-plane "
        "session-mismatch guard (B-1) must exist")

    for meth in BRIDGE_RUNTIME_METHODS:
        node = _method_source(bridge_cls, meth)
        assert node is not None, f"MobileGymBridge.{meth} disappeared?"
        src = ast.get_source_segment(BRIDGE.read_text(encoding="utf-8"),
                                     node) or ""
        for call in REALITY_SWITCH_CALLS:
            if call in src:
                problems.append(f"MobileGymBridge.{meth} references {call}")
        if "_require_active" not in src:
            problems.append(f"MobileGymBridge.{meth} lacks the "
                            "_require_active(sid) guard")
    assert not problems, (
        "B-1 violation — the runtime plane switches reality or is unguarded: "
        f"{problems}. The live sim binds ONE active session, established by "
        "the evaluation/setup plane; runtime methods honestly refuse a "
        "mismatched sid instead of teleporting state via "
        "reset/get_state/set_state.")


# ── 4. no executor knob anywhere (E30 acceptance → permanent test) ─────────

EXECUTOR_KNOB_PATTERNS = (
    re.compile(r"--executor"),
    re.compile(r"executor\s*=\s*['\"]api['\"]"),
    re.compile(r"\bCLI_EXECUTOR\b"),
    re.compile(r"choices\s*=\s*\[\s*['\"]api['\"]"),
)


def _string_constants(path: Path) -> list[str]:
    """All string literals in ``path`` (AST-based — # comments are not
    code; a comment SAYING '--executor knob is DELETED' must not trip the
    gate, while an argparse definition using it always would be a string
    literal and stays caught)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_no_executor_api_backdoor_knob_anywhere():
    hits: list[str] = []
    for path in (REPO_ROOT / "taskvm").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for literal in _string_constants(path):
            for pat in EXECUTOR_KNOB_PATTERNS:
                if pat.search(literal):
                    hits.append(f"{path.relative_to(REPO_ROOT)}: "
                                f"{pat.pattern!r} in {literal[:60]!r}")
    assert not hits, (
        f"API-write executor knob resurrected: {hits}. The runtime is "
        "GUI-only; there is no 'api' executor, no CLI selector for it.")


# ── 5. transitional debt register (mirror of substrate.md §8) ──────────

#: TRANSITIONAL DEBT REGISTER — mechanical mirror of the authoritative
#: table in ``docs/contracts/substrate.md`` §8 (audit B-F1: an allowlist
#: invented by a test must not silently amend the frozen contract, so the
#: contract doc is authoritative and this constant must mirror it 1:1).
#: Every entry is a STILL-STANDING §6 VIOLATION scheduled for DELETION.
#:
#: History:
#:   * T1 ``taskvm/execution/gui_driver.py`` — RESOLVED 2026-08-16 by
#:     Agent G Wave-3: deleted TOGETHER with its three live import hosts
#:     (governance/vm_state.py, execution/action_dispatcher.py,
#:     execution/rollback.py) plus the whole legacy execution/governance/
#:     workspace_ui/verifier cluster (one commit).
#:   * T2 ``taskvm/workspace_ui/server.py`` — RESOLVED 2026-08-16 by
#:     Agent D (anchor_lookup deleted; targeting routes through
#:     Observation → State Compiler → SurfaceHandle).
#:
#: The register is now EMPTY — the substrate contract's FORMAL LOCK
#: mechanical precondition is met (run TASKVM_SUBSTRATE_LOCK_AUDIT=1
#: pytest tests/substrate -q to stamp it).
TRANSITIONAL_DEBT_REGISTER: dict[str, tuple[str, ...]] = {}

#: Scan scope: every runtime-ish upper-layer package EXCEPT the evaluation
#: plane (taskvm/evaluation — exam-room scripts, allowed) and
#: taskvm/runtime + taskvm/projection (Agent E/D's in-flight waves; their
#: import boundaries are owned by tests/architecture/test_import_boundaries).
_UPPER_SCAN_DIRS = ("execution", "workspace_ui", "governance", "harness",
                    "task_state", "verifier")

_VIOLATION_PATTERNS = (
    re.compile(r"from taskvm\.substrate\.(?:builtin_web|mobilegym|osworld)"),
    re.compile(r"\b_WEB_APPS\b"),
    re.compile(r"\b_MOBILEGYM_APPS\b"),
    re.compile(r"\b_OP_FIELD\b"),
    re.compile(r"\b_ENTITY_KIND\b"),
    re.compile(r"\b_make_anchor_lookup\b"),
)


def _upper_violations() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for pkg in _UPPER_SCAN_DIRS:
        d = REPO_ROOT / "taskvm" / pkg
        if not d.is_dir():
            continue
        for path in d.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = str(path.relative_to(REPO_ROOT))
            src = path.read_text(encoding="utf-8")
            found = {p.pattern for p in _VIOLATION_PATTERNS if p.search(src)}
            if found:
                out.setdefault(rel, set()).update(found)
    return out

def test_transitional_debt_register_may_not_grow():
    """QUARANTINE ONLY — NOT a contract-satisfaction check (audit B-F1).

    Every register entry is a STILL-STANDING §6 violation pending its
    owner's deletion (see docs/contracts/substrate.md §8). This test
    merely keeps the transition honest: no NEW upper-layer substrate
    knowledge may appear anywhere outside the registered entries. It
    MUST NOT be cited as "the substrate contract gate is green, ergo
    §6 is satisfied" — that verdict belongs exclusively to
    ``test_formal_lock_audit`` below, which fails while any registered
    violation remains."""
    violations = _upper_violations()
    new: list[str] = []
    for rel, found in sorted(violations.items()):
        allowed = set(TRANSITIONAL_DEBT_REGISTER.get(rel, ()))
        for pattern in sorted(found - allowed):
            new.append(f"{rel}: /{pattern}/")
    assert not new, (
        "NEW upper-layer substrate knowledge (contract §6: all Web/MobileGym/"
        f"OSWorld differences live ONLY in taskvm/substrate/): {new}. "
        "Transitional debt is registered in docs/contracts/substrate.md §8 "
        "— adding entries requires an accepted RFC, and new code must route "
        "through the port.")
    # resolved-entry hints (non-failing): keep the register mirroring §8
    for rel, patterns in TRANSITIONAL_DEBT_REGISTER.items():
        live = violations.get(rel, set())
        resolved = [p for p in patterns if p not in live]
        if resolved or not (REPO_ROOT / rel).exists():
            print(f"[debt] {rel}: entry resolved ({resolved or 'file gone'}) "
                  "— shrink TRANSITIONAL_DEBT_REGISTER (and §8)")


def test_formal_lock_audit():
    """FORMAL LOCK gate (audit B-F1) — the mechanical stamp.

    Run explicitly before claiming the substrate contract is formally
    LOCKED:

        TASKVM_SUBSTRATE_LOCK_AUDIT=1 pytest tests/substrate -q

    FAILS while any registered transitional violation remains (register
    != {}). Default CI skips it so in-flight parallel waves (E/D) are not
    blocked by debt they already own; the skip reason is visible in the
    pytest summary — presence of registered debt is never silent."""
    if not os.environ.get("TASKVM_SUBSTRATE_LOCK_AUDIT"):
        pending = sorted(TRANSITIONAL_DEBT_REGISTER)
        pytest.skip(
            "substrate FORMAL LOCK audit not requested; registered "
            f"transitional debt still pending: {pending or 'none'} "
            "(run with TASKVM_SUBSTRATE_LOCK_AUDIT=1 to audit)")
    assert not TRANSITIONAL_DEBT_REGISTER, (
        "FORMAL LOCK PENDING — registered §6 violations remain: "
        f"{sorted(TRANSITIONAL_DEBT_REGISTER)}. Shrink the register as "
        "T1/T2 exit criteria land (docs/contracts/substrate.md §8); the "
        "substrate contract cannot be stamped formally LOCKED while any "
        "entry remains.")


# ── 6. substrate sources are portable (no machine-specific paths) ──────────

_MACHINE_PATH_PATTERNS = (
    re.compile(r"/mnt/dolphinfs"),
    re.compile(r"/Users/"),
    re.compile(r"C:\\\\Users"),
)


def test_substrate_sources_are_portable():
    """AST string-constant scan: # comments mentioning historical paths
    don't count; executable literals do."""
    hits: list[str] = []
    for path in SUBSTRATE.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for literal in _string_constants(path):
            for pat in _MACHINE_PATH_PATTERNS:
                if pat.search(literal):
                    hits.append(str(path.relative_to(REPO_ROOT)))
                    break
    assert not hits, (
        f"machine-specific absolute paths baked into the substrate: {hits}. "
        "Browser/binary paths come from env vars (PLAYWRIGHT_BROWSERS_PATH) "
        "or provider config, never literals (contract: portable path).")
