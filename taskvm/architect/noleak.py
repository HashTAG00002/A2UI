"""Prompt no-leak gate — the L4 execution of the GG red line §0.

The one-question test (docs/A2UI_GG阶段开工目标 §0): *\"this string — could
a real user see it on the rendered screen?\"* If not, it must never enter a
model input. This module scans **actually-built messages** (never just
templates) for the internal vocabulary of the stack:

- database-ish ids: ``E1`` / ``T12`` / ``wxid_xxx`` / ``evt:00042`` /
  ``action:00007`` / ``ckpt:C2`` / ``comp:00001`` …
- internal operator jargon: ``move_event`` / ``set_deadline`` /
  ``toggle_like`` / ``send_message`` / ``read_canonical`` / ``set_state`` …
- DOM-internal attributes: ``data-entity-id`` / ``data-field`` …
- kernel-internal namespaces that are not business semantics.

A hit raises :class:`PromptLeakError` — an HONEST failure. The gate never
silently strips the offending text: a prompt that needed stripping was built
from the wrong inputs and must be fixed at the producer.
"""
from __future__ import annotations

import re


class PromptLeakError(Exception):
    """A model-facing message carried internal, non-visible vocabulary."""


# When a model OUTPUT leaks internal vocabulary, the
# repair note sent BACK to the model states the error CLASS only — repeating
# the offending tokens would re-leak them into a model input (the very
# violation the gate exists to stop). Error detail with the tokens stays in
# the exception for the honest failure path; it never enters a prompt.
LEAK_REPAIR_GUIDANCE = (
    "\n\nYour previous output was rejected: it contained forbidden internal "
    "vocabulary (an internal id or an operator name). Rebuild the output "
    "from visible task semantics ONLY — business-meaning keys and strings a "
    "real user could read on the rendered screen. Output the corrected JSON "
    "object only."
)


# the SAME hole via a different, entirely normal
# branch — domain validators legitimately report failures in terms of the
# ids the assembly minted (workflow node ids n001…, contract ids c001…,
# projection ids p001…, handle ids ha001…), which _DB_ID_RE above
# deliberately does NOT enumerate (chasing every id shape is a losing
# game). So the SOURCE is fixed instead: a repair note NEVER carries the
# raw exception text. The message is classified here into a short
# business-level guidance snippet; the full detail stays in the exception
# and the log for the honest-failure path.
_REPAIR_GUIDANCE: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"not a JSON object", re.I),
     "your reply was not a JSON object with the required keys — output "
     "ONLY the JSON object, no prose"),
    (re.compile(r"depends on sibling", re.I),
     "in a fan-out, lanes must be independent — remove any lane-to-lane "
     "'after' ordering; lanes re-join only at the barrier"),
    (re.compile(
        r"single ordered chain|found a fork|listed order|listed step",
        re.I),
     "inside a sequence, steps run in the LISTED order: list them in "
     "execution order and give each step 'after' the previous step of "
     "the same sequence (it may also wait on nodes outside the sequence); "
     "for steps that should run in parallel, use fan-out lanes that "
     "re-join at one barrier — never put parallel steps in a sequence"),
    (re.compile(r"fan[- ]?in|barrier", re.I),
     "a barrier must re-join exactly one fan-out: give it 'after' the "
     "fan-out container (or all of its lanes)"),
    (re.compile(r"exactly one TERMINAL|must be a sink", re.I),
     "the plan needs EXACTLY ONE terminal node and nothing may come "
     "after it"),
    (re.compile(r"can never reach the TERMINAL|orphan", re.I),
     "every step must lead (directly or through its container) to the "
     "terminal — reconnect or drop work that can never finish"),
    (re.compile(r"references unknown task variables|binds unknown", re.I),
     "every 'sets' key and every projection binding must be one of the "
     "declared variables' semantic keys"),
    (re.compile(r"multiple final writers|split-brain", re.I),
     "for each variable, order the writers so the LAST one targets the "
     "variable's desired value; two unordered final writers must agree"),
    (re.compile(r"duplicate", re.I),
     "labels and semantic keys must be unique within the output"),
    (re.compile(r"termination predicate|max_iterations", re.I),
     "a bounded loop needs BOTH a termination predicate and "
     "max_iterations >= 1"),
    (re.compile(r"cycle|circular", re.I),
     "remove circular 'after' dependencies — the workflow must be "
     "acyclic"),
    (re.compile(r"task-level governance handle", re.I),
     "no action in the task writes a variable: at least one action must "
     "have a non-empty 'sets' mapping (navigation / observation / trigger "
     "steps may keep 'sets' empty, but the plan needs at least one "
     "writing action)"),
    (re.compile(r"non-empty 'sets'|'sets' must be an object|"
                r"needs a 'condition'|needs a contract|"
                r"non-empty label|needs a label|not a container", re.I),
     "fill in every required field of that node kind (label, action "
     "'sets' — an object that may be empty {} for navigation/observation/"
     "trigger steps, verify 'condition', a valid container reference)"),
)

GENERIC_REPAIR_GUIDANCE = (
    "your previous output violated the required structure — rebuild it "
    "using only the business labels from your previous output"
)


def repair_guidance(err: BaseException) -> str:
    """Business-level guidance for a rejected model output.

    The raw error text is NEVER returned or embedded: domain errors
    legitimately quote internal ids (n001/ha001/…), and repeating them
    to the model would push them into a model input — the exact
    violation the gate exists to stop. Callers log the exception
    themselves; this returns only the classified category text.
    """
    text = str(err)
    for pattern, guidance in _REPAIR_GUIDANCE:
        if pattern.search(text):
            return guidance
    return GENERIC_REPAIR_GUIDANCE


# DB-primary-key-shaped tokens: standalone E1 / T2 / wxid_* / C3 used as an
# ADDRESS (not inside prose). Word-boundary anchored so ordinary English
# words survive.
_DB_ID_RE = re.compile(
    r"\b(?:[ETW]\d{1,6})\b"          # E1, T12, W3 …
    r"|\bwxid_[A-Za-z0-9_]+\b"       # wechat internal ids
    r"|\b(?:evt|action|ckpt|comp|plan|node|saga)[:#]\w+\b"  # kernel/log ids
    r"|\bentity_id\b|\bdata-[a-z-]*id\b",
)

# internal operator / API vocabulary of the stack (extendable via
# extra_terms at call sites for task-specific operators)
_OPERATOR_JARGON = (
    "move_event", "set_deadline", "toggle_like", "send_message",
    "read_canonical", "set_state", "get_state", "undo_saga",
    "compile_patch", "interpret_as_workflow", "_gt_binding",
)

_OPERATOR_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _OPERATOR_JARGON) + r")\b")


def scan(text: str, *, extra_terms: tuple[str, ...] = ()) -> list[str]:
    """Return the list of offending snippets found in ``text`` (empty = clean)."""
    if not text:
        return []
    hits: list[str] = []
    hits.extend(m.group(0) for m in _DB_ID_RE.finditer(text))
    hits.extend(m.group(0) for m in _OPERATOR_RE.finditer(text))
    for term in extra_terms:
        if term and re.search(rf"\b{re.escape(term)}\b", text):
            hits.append(term)
    return hits


def assert_prompt_clean(text: str, *, extra_terms: tuple[str, ...] = (),
                        what: str = "model-facing message") -> None:
    """Raise :class:`PromptLeakError` listing every hit (honest, complete)."""
    hits = scan(text, extra_terms=extra_terms)
    if hits:
        raise PromptLeakError(
            f"{what} carries internal (non screen-visible) vocabulary: "
            f"{sorted(set(hits))}; the prompt must be rebuilt from visible "
            f"evidence only — stripping is not allowed")


def scan_json_values(obj, *, extra_terms: tuple[str, ...] = ()) -> list[str]:
    """Scan every string inside a parsed JSON object/list (model OUTPUT side:
    guards against the model echoing internal ids back as semantic keys)."""
    hits: list[str] = []
    if isinstance(obj, str):
        hits.extend(scan(obj, extra_terms=extra_terms))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                hits.extend(scan(k, extra_terms=extra_terms))
            hits.extend(scan_json_values(v, extra_terms=extra_terms))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            hits.extend(scan_json_values(v, extra_terms=extra_terms))
    return hits
