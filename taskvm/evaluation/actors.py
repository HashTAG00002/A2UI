"""taskvm.evaluation.actors — the deterministic capability model.

The fairness contract (handoff 07 §系统条件): same task, same CUA backend,
same budgets — only the harness differs. This module IS that shared
backend, as deterministic fakes:

* :class:`TemplateCUA` — the CUAModel port implementation. Its capability
  model is LOCAL template execution: parse the goal text's targets, find
  the first unsatisfied one in the visible ``k=v`` rows, type it. It has
  NO memory across calls, NO rebind intelligence (a target key that is
  not on screen → honest FAIL), NO governance understanding. Whatever a
  condition achieves beyond this must come from its harness structure.
* :class:`TemplateModelPort` — the ModelPort implementation serving the
  State Compiler (slow path) and the Task Architect roles. The planner /
  architect capability is GLOBAL template composition: parse the whole
  goal into an ordered program, re-emit remaining instructions each turn,
  and (architect role) map the program onto the frozen domain workflow
  JSON schema. It can rebind a goal key to a visible key when exactly one
  visible variable carries the goal key's current value — the semantic
  reading a frontier model would make of a relabelled screen; the CUA
  layer deliberately cannot.

What this buys the paper: with fakes installed, the difference between
``direct-cua`` / ``planner-cua`` / ``taskvm`` is the difference in
HARNESS STRUCTURE at fixed model capability — the causal claim the
benchmark exists to test. Real-model runs swap these fakes for the HTTP
port; nothing else changes.

Parsing note: the supported goal dialects are exactly the ones the task
taxonomy writes (``Set k to v.`` / ``Repeat: Set g to v until k is v.``)
plus the two serializer outputs the runtime actually feeds a CUA
(``Set: k = 'v', ...`` / ``Restore '<k>': the visible value should
return to '<v>' (it currently reads '<cur>').`` — and the plain
``restore k back to v`` spelling). Everything else is an honest parse
failure — the fake never guesses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from taskvm.architect.port import ModelReply
from taskvm.substrate import GuiAction
from taskvm.runtime import CUADecision, CUADecisionKind

__all__ = [
    "TemplateCUA", "TemplateModelPort", "GoalProgram", "parse_goal_program",
    "parse_visible_kv",
]


# ── goal-text parsing (the shared capability's grammar) ─────────────────────

_SET_TO_RE = re.compile(r"Set\s+([a-z][a-z0-9_.]*)\s+to\s+([^\n.;]+)")
_SER_SET_RE = re.compile(r"Set:\s*([^\n.]+)", re.IGNORECASE)
_SER_PAIR_RE = re.compile(r"([a-z][a-z0-9_.]*)\s*=\s*'([^']*)'")
_REPEAT_RE = re.compile(
    r"Repeat:\s*Set\s+([a-z][a-z0-9_.]*)\s+to\s+(\S+?)\s+"
    r"until\s+([a-z][a-z0-9_.]*)\s+is\s+(\S+?)\s*[.\n]", re.IGNORECASE)
_RESTORE_RE = re.compile(
    r"restore\s+([a-z][a-z0-9_.]*)\s+back\s+to\s+(\S+?)[\s.,\n]",
    re.IGNORECASE)
#: the serializer's ACTUAL compensation dialect (ActionContractSerializer
#: .compensation_goal): "Restore '<key>': the visible value should
#: return to '<v>' (it currently reads '<cur>')."
_SER_RESTORE_RE = re.compile(
    r"Restore\s+'([a-z][a-z0-9_.]*)':\s*the visible value should "
    r"return to\s+'([^']*)'", re.IGNORECASE)


@dataclass(frozen=True)
class GoalProgram:
    """The parsed goal: ordered plain sets, one optional repeat block,
    one optional restore directive. This is the ENTIRE capability —
    conditions structure around it, they cannot extend it."""

    sets: tuple[tuple[str, str], ...] = ()
    repeat: tuple[tuple[str, str], tuple[str, str]] | None = None
    restore: tuple[str, str] | None = None


def parse_visible_kv(text: str) -> dict[str, str]:
    """Parse a visible observation into ``{key: value}`` — only bare
    ``k=v`` tokens count. External notice lines contain no ``=`` token by
    construction (world._notice), so they can never masquerade as fields."""
    out: dict[str, str] = {}
    for tok in (text or "").split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            if k:
                out[k] = v
    return out


def parse_goal_program(goal: str) -> GoalProgram:
    """Parse the supported dialects into a GoalProgram (deterministic,
    order-preserving). Raises ValueError on an unparseable goal — the
    fake's honest boundary."""
    if not goal or not goal.strip():
        raise ValueError("empty goal")
    restore = None
    m = _SER_RESTORE_RE.search(goal)
    if m:
        restore = (m.group(1), m.group(2))
    else:
        m = _RESTORE_RE.search(goal)
        if m:
            restore = (m.group(1), m.group(2))
    repeat = None
    m = _REPEAT_RE.search(goal)
    if m:
        repeat = ((m.group(1), m.group(2)), (m.group(3), m.group(4)))
    sets: list[tuple[str, str]] = []
    # serializer dialect first: one "Set: k = 'v', k2 = 'v2'." line
    m = _SER_SET_RE.search(goal)
    if m:
        for k, v in _SER_PAIR_RE.findall(m.group(1)):
            sets.append((k, v))
    else:
        # plain dialect: every "Set k to v." in order
        for k, v in _SET_TO_RE.findall(goal):
            v = v.strip().rstrip(".").strip()
            sets.append((k, v))
    if not sets and repeat is None and restore is None:
        raise ValueError(f"goal dialect not supported: {goal[:80]!r}")
    return GoalProgram(sets=tuple(sets), repeat=repeat, restore=restore)


# ── the CUA (local execution capability) ───────────────────────────────────

class TemplateCUA:
    """The shared CUA backend. ONE step per call: the first unsatisfied
    target becomes one ``type`` gesture; all targets satisfied → DONE; a
    target that is not on screen → FAIL (honest — no rebind at this
    layer). Idempotent re-typing of an already-satisfied repeat gesture
    is allowed (that is how the loop button re-fires)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def predict_action(self, *, goal: str, observation,
                       labels: Mapping[str, str] | None = None,
                       attempt: int = 1,
                       model: str | None = None) -> CUADecision:
        self.calls.append({"goal": goal, "attempt": attempt,
                           "text": observation.visible_text})
        try:
            prog = parse_goal_program(goal)
        except ValueError as e:
            return CUADecision(kind=CUADecisionKind.FAIL, reason=str(e))
        current = parse_visible_kv(observation.visible_text or "")
        if prog.restore is not None:
            key, want = prog.restore
            if current.get(key) == want:
                return CUADecision(kind=CUADecisionKind.DONE)
            if key not in current:
                return CUADecision(
                    kind=CUADecisionKind.FAIL,
                    reason=f"cannot see {key} on screen to restore")
            return CUADecision(
                kind=CUADecisionKind.ACT,
                action=GuiAction(kind="type", text=f"{key}={want}"))
        if prog.repeat is not None:
            (gk, gv), (uk, uv) = prog.repeat
            if current.get(uk) == uv:
                # termination satisfied: fall through to plain sets
                pass
            else:
                if uk not in current:
                    return CUADecision(
                        kind=CUADecisionKind.FAIL,
                        reason=f"cannot read loop condition {uk}")
                return CUADecision(
                    kind=CUADecisionKind.ACT,
                    action=GuiAction(kind="type", text=f"{gk}={gv}"))
        for key, want in prog.sets:
            if current.get(key) != want:
                if key not in current:
                    return CUADecision(
                        kind=CUADecisionKind.FAIL,
                        reason=f"cannot see {key} on screen")
                return CUADecision(
                    kind=CUADecisionKind.ACT,
                    action=GuiAction(kind="type", text=f"{key}={want}"))
        return CUADecision(kind=CUADecisionKind.DONE)


# ── the model port (global composition capability) ────────────────────────

def _parse_state_lines(user: str) -> dict[str, str]:
    """Architect prompt's observed-state lines:
    ``- key (label, type=..., mutability=..., observed='v')``."""
    out: dict[str, str] = {}
    for m in re.finditer(
            r"-\s+([a-z][a-z0-9_.]*)\s+\([^)]*observed=('?)([^'()]*)\2\)",
            user):
        out[m.group(1)] = m.group(3)
    return out


def _goal_of(user: str) -> str:
    m = re.search(r"# Task goal\n(.+?)(?:\nConstraints:|\nSuccess criteria:|\n# Current task state|\n# Previously|\Z)",
                  user, re.DOTALL)
    return m.group(1).strip() if m else ""


_RELABEL_RE = re.compile(
    r"the visible field\s+([a-z][a-z0-9_.]*)\s+on\s+\S+\s+"
    r"is now labelled\s+([a-z][a-z0-9_.]*)")

#: committed-history lines of the architect's recompose prompt
#: ("- [committed] step-2 (action) — did: ...") — labels are FROZEN;
#: the model must design the remaining future without reusing them.
_COMMITTED_RE = re.compile(r"- \[committed\] (\S+) \((\w+)\)")


def _committed_labels(user: str) -> list[str]:
    return [m.group(1) for m in _COMMITTED_RE.finditer(user)]


class TemplateModelPort:
    """The shared ModelPort backend (State Compiler slow path + Task
    Architect). Deterministic: the reply depends only on the prompt.
    Token accounting is estimated deterministically (the fakes emit no
    real tokens; the ledger still gets non-None numbers so the report's
    per-role columns stay populated)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, *, system: str, user: str,
                      model: str | None = None, max_tokens: int = 3072,
                      temperature: float | None = None,
                      image_data_url: str | None = None) -> ModelReply:
        self.calls.append({"system": system[:60], "user": user})
        if "Task Architect" in system:
            payload = self._architect_reply(user)
        elif "State Compiler" in system:
            payload = self._compiler_reply(user)
        else:
            payload = None
        raw = "" if payload is None else str(payload)
        return ModelReply(
            parsed=payload, raw=raw,
            model=model or "template-fake",
            prompt_tokens=len(user.split()) if payload is not None else None,
            completion_tokens=(len(raw.split()) if payload is not None
                               else None))

    # ── state compiler slow path ────────────────────────────────────────
    @staticmethod
    def _compiler_reply(user: str) -> dict[str, Any]:
        """Extract every visible ``k=v`` as a variable with verbatim
        evidence (the reading a model would make of the screen). A visible
        relabel notice ("the visible field X is now labelled Y") keeps the
        OLD business semantic_key under the NEW visible label — that is
        the compiler-layer rebind; the architect layer never renames."""
        relabels: dict[str, str] = {}          # new visible key -> old key
        for m in _RELABEL_RE.finditer(user):
            relabels[m.group(2)] = m.group(1)
        variables = []
        for m in re.finditer(r"## Surface:\s*(\S+)\n(.*)", user, re.DOTALL):
            surface_label, body = m.group(1), m.group(2)
            for tok in body.split():
                if "=" in tok:
                    k, _, v = tok.partition("=")
                    if not k or " " in k:
                        continue
                    semantic = relabels.get(k, k)
                    variables.append({
                        "semantic_key": semantic,
                        "label": k,
                        "value_type": "string",
                        "mutability": "editable",
                        "observed": v,
                        "confidence": 1.0,
                        "evidence": [{
                            "surface_label": surface_label,
                            "visible_label": k,
                            "visible_context": body[:120],
                            "value_pattern": rf"{re.escape(k)}=(\S+)",
                        }],
                    })
        return {"variables": variables, "ambiguities": [],
                "needs_clarification": False}

    # ── task architect ─────────────────────────────────────────────────
    def _architect_reply(self, user: str) -> dict[str, Any]:
        goal = _goal_of(user)
        observed = _parse_state_lines(user)
        prog = parse_goal_program(goal)
        fan_out = "at once" in goal.lower()
        checkpoint = "place a checkpoint" in goal.lower()

        # recompose context: committed labels are frozen — new labels must
        # not collide, and the remaining future chains after the last
        # committed node (the reading a frontier model makes of the
        # "Already COMMITTED history" section).
        committed = _committed_labels(user)
        used: set[str] = set(committed)

        def fresh(lbl: str) -> str:
            if lbl not in used:
                used.add(lbl)
                return lbl
            m2 = re.search(r"^(.*?)(\d+)$", lbl)
            base, n = (m2.group(1), int(m2.group(2))) if m2 else (lbl + "-", 1)
            while True:
                n += 1
                cand = f"{base}{n}"
                if cand not in used:
                    used.add(cand)
                    return cand

        # variables: everything observed (desired = observed) + program
        # targets (desired = target). NO architect-level rebind: a goal
        # key that left the visible world stays as-is — recovering it is
        # the State Compiler's job (semantic_key stable, label moves).
        variables: list[dict[str, Any]] = []
        desired_by_key: dict[str, str] = {}
        for key, want in prog.sets:
            desired_by_key[key] = want
        for key, val in observed.items():
            variables.append({
                "semantic_key": key, "label": key, "value_type": "string",
                "mutability": "editable", "desired": val,
            })
        for key, want in desired_by_key.items():
            if key in observed:
                for v in variables:
                    if v["semantic_key"] == key:
                        v["desired"] = want
            else:
                variables.append({
                    "semantic_key": key, "label": key,
                    "value_type": "string", "mutability": "editable",
                    "desired": want,
                })

        nodes: list[dict[str, Any]] = []
        prev: list[str] = ([committed[-1]] if committed else [])
        loop_lbl = ""                        # set when a repeat block exists
        if checkpoint:
            ck = fresh("checkpoint-1")
            nodes.append({"kind": "checkpoint", "label": ck,
                          "after": prev})
            prev = [ck]
        targets = tuple(desired_by_key.items())
        if prog.repeat is not None:
            (gk, gv), (uk, uv) = prog.repeat
            loop_lbl = fresh("sweep-loop")
            nodes.append({
                "kind": "bounded_loop", "label": loop_lbl,
                "termination": f"{uk} == {uv}",
                "max_iterations": 8,
            })
            nodes.append({
                "kind": "action", "label": fresh("sweep-once"),
                "container": loop_lbl,
                "semantic_goal": f"perform one {gv} pass",
                "sets": {gk: gv},
                # RFC-003 deterministic form (the contract the runtime
                # verifier freezes; a compliant model writes this shape)
                "completion": f"{gk} == {gv}",
                "reversibility": "reversible", "risk": "",
                "target_evidence": [gk],
            })
        if fan_out and len(targets) > 1:
            lanes_lbl = fresh("lanes")
            nodes.append({"kind": "fan_out", "label": lanes_lbl})
            lane_labels = []
            for i, (key, want) in enumerate(targets):
                lbl = fresh(f"lane-{i+1}")
                nodes.append({
                    "kind": "action", "label": lbl,
                    "container": lanes_lbl,
                    "after": list(prev),
                    "semantic_goal": f"{key} becomes {want}",
                    "sets": {key: want},
                    "completion": f"{key} == {want}",
                    "reversibility": "reversible", "risk": "",
                    "target_evidence": [key],
                })
                lane_labels.append(lbl)
            joined = fresh("joined")
            nodes.append({"kind": "barrier", "label": joined,
                          "after": lane_labels})
            sink = joined
        else:
            chain = list(prev)
            for i, (key, want) in enumerate(targets):
                lbl = fresh(f"step-{i+1}")
                nodes.append({
                    "kind": "action", "label": lbl, "after": chain,
                    "semantic_goal": f"{key} becomes {want}",
                    "sets": {key: want},
                    "completion": f"{key} == {want}",
                    "reversibility": "reversible", "risk": "",
                    "target_evidence": [key],
                })
                chain = [lbl]
            sink = chain[0] if chain else (
                loop_lbl if prog.repeat is not None else "")
        nodes.append({"kind": "terminal", "label": fresh("done"),
                      "after": [sink] if sink else []})

        components = [{"label": f"field-{v['semantic_key']}",
                       "type": "field", "binds": v["semantic_key"],
                       "editable": True, "children": []}
                      for v in variables]
        return {
            "variables": variables,
            "workflow": {"nodes": nodes},
            "projection": {
                "root": "task-card",
                "components": ([{
                    "label": "task-card", "type": "card", "binds": None,
                    "editable": False,
                    "children": [c["label"] for c in components],
                }] + components),
            },
        }
