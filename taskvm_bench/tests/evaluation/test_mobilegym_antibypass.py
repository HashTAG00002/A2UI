"""B-09 — the bridge semantic-route anti-bypass static gate.

RM-0 work order §B-09 (re-prompt): the RM main path is LOCKED to

    TaskVM CUA → MobileGym L1 GUI act (observe/act port)

and the bypass

    TaskVM CUA → bridge semantic route → nested CUA

must be structurally impossible FROM THE BENCH PLANE. This gate scans
every source file under ``taskvm_bench/evaluation/`` and refuses:

* the semantic mutate route strings ``/api/wechat/`` and ``/api/x/``
  (note the trailing separator — the ORACLE read routes
  ``/api/wechat_chats/`` / ``/api/x_state/`` / ``/api/x_posts/`` live on
  the substrate side and are not matched);
* the bridge's semantic mutate helper names (``mutate_wechat`` /
  ``mutate_x``) and the injected L2 CUA loop entry points
  (``gui_write_async`` / ``gui_act_async``);
* the hidden world-write path (``set_state`` / ``inject_task``) — the
  documented setup API belongs to ``MobileGymEvaluationEnvironment``
  (substrate side); the bench reaches it ONLY through that object's
  ``reset``/``seed``/``oracle_state`` METHODS (allowed solely inside the
  factory, the EvaluationEnvironment wrapper);
* any spelling of a CUA-loop injection flag (the bridge launch line is
  built from a closed whitelist — see test_mobilegym_factory).

Evaluation setup (reset/seed/oracle) is therefore provably confined to
the EvaluationEnvironment plane and never reaches the runtime or the
user-op driver path.
"""
from __future__ import annotations

import os
import re

BENCH_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "evaluation")

#: the semantic MUTATE routes (POST /api/wechat/<sid>/<eid>,
#: POST /api/x/<sid>/<eid>). The trailing "/" is load-bearing: oracle
#: GET routes (/api/wechat_chats/, /api/x_state/, /api/x_posts/) do NOT
#: match and stay legitimately reachable from the substrate-side oracle.
FORBIDDEN_ROUTE_LITERALS = (
    "/api/wechat/",
    "/api/x/",
)

#: bridge semantic mutate helpers + injected nested-CUA loop entry
#: points + hidden world-write APIs. Word-bounded so legitimate
#: identifiers (e.g. ``reset_state_hash``) are never false-flagged.
FORBIDDEN_PATTERNS = (
    r"\bmutate_wechat\b",
    r"\bmutate_x\b",
    r"\bgui_write_async\b",
    r"\bgui_act_async\b",
    r"\bset_state\b",
    r"\binject_task\b",
)

#: any spelling of the CUA-loop injection flag — the RM runner must not
#: even be able to ask the bridge for a nested CUA loop.
FORBIDDEN_INJECTION_SPELLINGS = (
    "cua-loop",
    "cua_loop",
    "--cua",
)

#: the ONLY bench file allowed to speak the setup plane's method
#: vocabulary (``reset``/``seed``/``oracle_state`` — methods ON the
#: EvaluationEnvironment, never raw HTTP).
SETUP_PLANE_OWNER = "mobilegym_factory.py"

#: files that must stay clean of every forbidden token — asserted
#: EXPLICITLY (in addition to the repo-wide sweep) because they are the
#: runtime/user-op driver path (B-04: read-only verification here; these
#: files belong to a completed wave and are never edited by B-09).
USER_OP_PLANE_FILES = ("user_ops.py", "projection_client.py")


def _evaluation_sources():
    for dirpath, dirnames, filenames in os.walk(BENCH_DIR):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                yield name, path, fh.read()


def test_no_semantic_mutate_route_strings():
    """No bench evaluation source references the bridge's semantic
    mutate routes — the RM write path is the L1 observe/act port only."""
    offenders = []
    for name, path, src in _evaluation_sources():
        for literal in FORBIDDEN_ROUTE_LITERALS:
            if literal in src:
                offenders.append((name, literal))
    assert not offenders, (
        f"semantic mutate routes referenced from the bench plane: "
        f"{offenders} — the RM main path is L1 observe/act only")


def test_no_bridge_mutate_helpers_or_hidden_world_writes():
    """No bench evaluation source names the bridge mutate helpers, the
    nested-CUA loop entry points, or the hidden set_state/inject_task
    write path."""
    offenders = []
    for name, path, src in _evaluation_sources():
        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, src):
                offenders.append((name, pattern))
    assert not offenders, (
        f"bridge mutate helpers / hidden world-write paths referenced "
        f"from the bench plane: {offenders}")


def test_no_cua_loop_injection_spelling():
    """No bench evaluation source can even spell a CUA-loop injection
    flag — the bridge launch line is closed-whitelist by construction."""
    offenders = []
    for name, path, src in _evaluation_sources():
        for spelling in FORBIDDEN_INJECTION_SPELLINGS:
            if spelling in src:
                offenders.append((name, spelling))
    assert not offenders, (
        f"CUA-loop injection spellings present in the bench plane: "
        f"{offenders}")


def test_setup_plane_confined_to_evaluation_environment_wrapper():
    """The setup vocabulary (reset/seed/oracle_state as METHOD calls on
    the EvaluationEnvironment) may appear ONLY in the factory — the
    file whose entire job is being that environment's wrapper. Anywhere
    else in the bench evaluation plane, the words themselves are a
    smell worth refusing."""
    setup_call = re.compile(
        r"\.\s*(reset|seed|oracle_state|x_state|session_state)\s*\(")
    offenders = []
    for name, path, src in _evaluation_sources():
        if name == SETUP_PLANE_OWNER:
            continue
        if setup_call.search(src):
            offenders.append(name)
    assert not offenders, (
        f"setup-plane vocabulary outside {SETUP_PLANE_OWNER}: "
        f"{offenders} — evaluation setup belongs to the "
        f"EvaluationEnvironment plane only")


def test_user_op_plane_files_are_clean():
    """The runtime/user-op driver path (B-04 files) carries none of the
    forbidden tokens — read-only verification, never an edit."""
    for name in USER_OP_PLANE_FILES:
        path = os.path.join(BENCH_DIR, name)
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for literal in FORBIDDEN_ROUTE_LITERALS:
            assert literal not in src, (name, literal)
        for pattern in FORBIDDEN_PATTERNS:
            assert not re.search(pattern, src), (name, pattern)
        for spelling in FORBIDDEN_INJECTION_SPELLINGS:
            assert spelling not in src, (name, spelling)


def test_bench_plane_does_not_import_bridge_internals():
    """The bench reaches MobileGym ONLY through the substrate-side
    client objects (session/evaluation env) — never by IMPORTING the
    bridge module (whose ``build_app``/``MobileGymBridge`` would hand
    over the semantic routes wholesale). AST-level: naming the module
    in a ``-m`` SUBPROCESS argv (how the factory launches it) is not
    an import and stays legitimate."""
    import ast
    offenders = []
    for name, path, src in _evaluation_sources():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            offenders.append(f"{name} (unparseable)")
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for mod in mods:
                if (mod == "taskvm.substrate.mobilegym"
                        or mod.startswith("taskvm.substrate.mobilegym.bridge")
                        or (mod.startswith("taskvm.substrate.mobilegym")
                            and mod.count(".") == 3
                            and mod.rsplit(".", 1)[-1] not in (
                                "session", "evaluation"))):
                    offenders.append(f"{name}: import {mod}")
    assert not offenders, (
        f"bench imports bridge internals: {offenders} — use "
        f"MobileGymSubstrateSession / MobileGymEvaluationEnvironment")
