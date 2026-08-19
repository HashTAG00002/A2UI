"""policy — TaskVM semantic policy validation for model-generated
component trees (workplan §4 `policy.py`).

This is the layer ABOVE raw A2UI schema conformance: even a
schema-perfect ``updateComponents`` payload is REJECTED unless it obeys
TaskVM's governance boundaries. Every check returns an honest,
human-readable error — no best-effort repair, no silent coercion.

Checks:
1. structure: exactly one root; children ids resolve; no orphans, no
   cycles; component count ≤ 80; tree depth ≤ 8; children are plain id
   arrays (template children are out of scope for wave 1); components
   referenced only via single-id refs (Button/Card ``child``, Modal
   ``trigger``/``content``, Tabs tab children — the official sample
   form) count as reachable;
2. bindings: every ``{"path": ...}"` must address a whitelisted data-model
   path; the WRITE channel of INPUT components (TextField/CheckBox/
   ChoicePicker/Slider/DateTimeInput — their ``value`` property) may only
   bind ``/variables/<key>/desired`` of an EDITABLE variable (display
   channels like ``label`` may bind any whitelisted path);
3. actions: name must be in the surface allowlist (``taskvm.local_patch``
   only); governance names are rejected with an explicit
   governance-owned error; the action context's ``semanticKey`` must
   exist and be editable; the context's ``value`` may be a LITERAL
   (type-checked against the variable's value_type, bool never posing
   as a number) or a protocol-native DataBinding ``{"path": …}``
   (A5-IFACE-01: judged by the binding whitelist, never by literal
   type checks — the client resolves bindings before dispatch);
4. content: no absolute/deep-link URLs, no script-ish payloads; text
   length capped;
5. governance shell integrity: component ids may never squat the
   reserved ``governance-`` / ``gov-`` namespace.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from taskvm.genui.protocol import (
    ACTION_LOCAL_PATCH, ALLOWED_SURFACE_ACTIONS, GOVERNANCE_ACTION_NAMES,
    RESERVED_ID_PREFIXES, ROOT_COMPONENT_ID,
)

#: Hard limits (workplan §7-P3 generation policy).
MAX_COMPONENTS = 80
MAX_TREE_DEPTH = 8
MAX_TEXT_LENGTH = 2000
MAX_ID_LENGTH = 64

#: Components whose value binding is a WRITE affordance.
INPUT_COMPONENT_TYPES = frozenset({
    "TextField", "CheckBox", "ChoicePicker", "Slider", "DateTimeInput",
})

#: All container-ish components that reference children by id.
_CONTAINER_CHILD_KEYS = ("children",)

#: Single-id child references (official sample form: a Button's label
#: Text need not hang in any children array — Button.child IS its edge).
#: Used for ORPHAN reachability only; depth and multi-parent checks stay
#: on the plain children arrays (a Button label is a leaf, not a subtree).
_SINGLE_REF_KEYS = ("child", "trigger", "content")


def _referenced_ids(comp: Mapping) -> list[str]:
    """Every component id ``comp`` points at: children array entries,
    single-id refs (Button/Card ``child``, Modal ``trigger``/``content``)
    and Tabs tab children. Used by the orphan-reachability walk."""
    ids: list[str] = []
    children = comp.get("children")
    if isinstance(children, list):
        ids.extend(ch for ch in children if isinstance(ch, str))
    for key in _SINGLE_REF_KEYS:
        value = comp.get(key)
        if isinstance(value, str):
            ids.append(value)
    tabs = comp.get("tabs")
    if isinstance(tabs, list):
        for tab in tabs:
            if isinstance(tab, dict) and isinstance(tab.get("child"), str):
                ids.append(tab["child"])
    return ids

_FORBIDDEN_TEXT_MARKERS = ("<script", "javascript:", "data:text/html")


def _iter_paths(node: Any, prefix: str = "") -> Iterable[str]:
    """Yield every JSON-Pointer path appearing via DataBinding dicts."""
    if isinstance(node, dict):
        if set(node.keys()) == {"path"} and isinstance(node["path"], str):
            yield node["path"]
            return
        for key, value in node.items():
            yield from _iter_paths(value, f"{prefix}/{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _iter_paths(item, f"{prefix}/{i}")


def _iter_strings(node: Any) -> Iterable[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_strings(item)


class SurfacePolicy:
    """Semantic policy over one model-generated component list."""

    def __init__(self, context, data_model: Mapping) -> None:
        """``context``: TaskSurfaceContext (variable mutability ground
        truth). ``data_model``: the CURRENT surface data model (binding
        whitelist ground truth — from TaskDataModelProjector)."""
        self._context = context
        self._whitelist = _whitelisted_paths(data_model)
        self._editable = {v.semantic_key for v in context.variables
                          if v.editable}
        self._known_keys = {v.semantic_key for v in context.variables}

    # ── entry ──────────────────────────────────────────────────────────
    def check_components(self, components: list[dict[str, Any]]
                         ) -> list[str]:
        """Return every policy violation (empty list == acceptable)."""
        errors: list[str] = []
        if not isinstance(components, list) or not components:
            return ["components must be a non-empty list"]
        errors += self._check_limits(components)
        errors += self._check_ids_and_structure(components)
        errors += self._check_bindings(components)
        errors += self._check_actions(components)
        errors += self._check_content(components)
        return errors

    # ── 1. limits ──────────────────────────────────────────────────────
    def _check_limits(self, components: list[dict[str, Any]]
                      ) -> list[str]:
        errors = []
        if len(components) > MAX_COMPONENTS:
            errors.append(
                f"component count {len(components)} exceeds the limit "
                f"of {MAX_COMPONENTS}")
        for c in components:
            cid = c.get("id", "")
            if not isinstance(cid, str) or not cid:
                errors.append("every component needs a non-empty string id")
            elif len(cid) > MAX_ID_LENGTH:
                errors.append(
                    f"component id {cid[:24]!r}… exceeds {MAX_ID_LENGTH} chars")
        return errors

    # ── 2. ids, tree shape, depth ──────────────────────────────────────
    def _check_ids_and_structure(self, components: list[dict[str, Any]]
                                 ) -> list[str]:
        errors: list[str] = []
        ids = [c.get("id") for c in components]
        roots = [i for i in ids if i == ROOT_COMPONENT_ID]
        if len(roots) != 1:
            errors.append(
                f"exactly one component must have id "
                f"{ROOT_COMPONENT_ID!r} (found {len(roots)})")

        for cid in ids:
            if isinstance(cid, str) and cid.startswith(RESERVED_ID_PREFIXES):
                errors.append(
                    f"component id {cid!r} squats the reserved governance "
                    "namespace — the fixed shell owns those controls")

        by_id: dict[str, dict[str, Any]] = {}
        for c in components:
            cid = c.get("id")
            if isinstance(cid, str) and cid:
                if cid in by_id:
                    errors.append(f"duplicate component id {cid!r}")
                by_id[cid] = c

        # children edges: must be plain id arrays (no templates this wave)
        parents: dict[str, str] = {}
        edges: dict[str, list[str]] = {}
        for cid, comp in by_id.items():
            for key in _CONTAINER_CHILD_KEYS:
                if key not in comp:
                    continue
                children = comp[key]
                if not isinstance(children, list) or not all(
                        isinstance(ch, str) for ch in children):
                    errors.append(
                        f"component {cid!r}: {key} must be a plain array of "
                        "component ids (template children are not allowed "
                        "in this wave)")
                    continue
                for ch in children:
                    if ch == ROOT_COMPONENT_ID:
                        errors.append(
                            f"component {cid!r}: the root must not be "
                            "referenced as a child")
                        continue
                    if ch not in by_id:
                        errors.append(
                            f"component {cid!r} references unknown child {ch!r}")
                        continue
                    if ch in parents and parents[ch] != cid:
                        errors.append(
                            f"component {ch!r} has multiple parents "
                            f"({parents[ch]!r} and {cid!r})")
                    parents[ch] = cid
                edges[cid] = list(children)

        # orphans: every non-root must be reachable from root — via
        # children arrays AND single-id refs (Button.child et al., the
        # official sample form: the button label Text hangs only off the
        # Button, never in a container's children list)
        if ROOT_COMPONENT_ID in by_id:
            seen = set()
            stack = [ROOT_COMPONENT_ID]
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                stack.extend(cid for cid in _referenced_ids(by_id[cur])
                             if cid in by_id)
            for cid in by_id:
                if cid not in seen:
                    errors.append(
                        f"component {cid!r} is unreachable from root "
                        "(orphan)")

        # depth via BFS from root
        if ROOT_COMPONENT_ID in edges:
            depth = {ROOT_COMPONENT_ID: 1}
            queue = [ROOT_COMPONENT_ID]
            while queue:
                cur = queue.pop(0)
                for ch in edges.get(cur, ()):
                    d = depth[cur] + 1
                    if ch not in depth:
                        depth[ch] = d
                        queue.append(ch)
                        if d > MAX_TREE_DEPTH:
                            errors.append(
                                f"tree depth exceeds the limit of "
                                f"{MAX_TREE_DEPTH} at component {ch!r}")
        return errors

    # ── 3. bindings ────────────────────────────────────────────────────
    def _check_bindings(self, components: list[dict[str, Any]]
                        ) -> list[str]:
        errors: list[str] = []
        for comp in components:
            cid = comp.get("id", "?")
            ctype = comp.get("component", "?")
            paths = list(_iter_paths(comp))
            for p in paths:
                if p not in self._whitelist:
                    errors.append(
                        f"component {cid!r}: binding path {p!r} is not a "
                        "whitelisted path of the current data model")
            if ctype in INPUT_COMPONENT_TYPES:
                # Only the WRITE channel (the ``value`` property) is
                # restricted: it may bind /variables/<key>/desired of an
                # EDITABLE variable only. Display channels (``label`` and
                # friends) may bind any whitelisted path — an input's
                # label legitimately shows the variable's label plane.
                value_binding = comp.get("value")
                if isinstance(value_binding, dict) and \
                        set(value_binding.keys()) == {"path"} and \
                        isinstance(value_binding["path"], str):
                    p = value_binding["path"]
                    if not (p.startswith("/variables/")
                            and p.endswith("/desired")):
                        errors.append(
                            f"component {cid!r} ({ctype}): input components "
                            "may only bind /variables/<key>/desired")
                    else:
                        key = p[len("/variables/"):-len("/desired")]
                        if key not in self._known_keys:
                            errors.append(
                                f"component {cid!r}: unknown variable key "
                                f"{key!r} in binding {p!r}")
                        elif key not in self._editable:
                            errors.append(
                                f"component {cid!r}: variable {key!r} is "
                                "not editable — inputs may only bind "
                                "editable variables")
        return errors

    # ── 4. actions ─────────────────────────────────────────────────────
    def _check_actions(self, components: list[dict[str, Any]]
                       ) -> list[str]:
        errors: list[str] = []
        for comp in components:
            cid = comp.get("id", "?")
            action = comp.get("action")
            if action is None:
                continue
            event = (action or {}).get("event") if isinstance(action, dict) else None
            if not isinstance(event, dict):
                errors.append(
                    f"component {cid!r}: action must carry an event object")
                continue
            name = event.get("name")
            if name not in ALLOWED_SURFACE_ACTIONS:
                if name in GOVERNANCE_ACTION_NAMES:
                    errors.append(
                        f"component {cid!r}: action {name!r} is a "
                        "governance action owned by the fixed shell — the "
                        "dynamic surface may never emit it")
                else:
                    errors.append(
                        f"component {cid!r}: action name {name!r} is not "
                        f"in the allowlist {sorted(ALLOWED_SURFACE_ACTIONS)}")
                continue
            ctx = event.get("context") or {}
            if name == ACTION_LOCAL_PATCH:
                key = ctx.get("semanticKey")
                if not isinstance(key, str) or not key:
                    errors.append(
                        f"component {cid!r}: {ACTION_LOCAL_PATCH} requires "
                        "a non-empty context.semanticKey")
                elif key not in self._known_keys:
                    errors.append(
                        f"component {cid!r}: action context references "
                        f"unknown semantic key {key!r}")
                elif key not in self._editable:
                    errors.append(
                        f"component {cid!r}: action context key {key!r} "
                        "is not editable — local_patch only targets "
                        "editable variables")
                if "value" in ctx:
                    errors += self._check_value_type(cid, key, ctx["value"])
        return errors

    def _check_value_type(self, cid: str, key: str | None,
                          value: Any) -> list[str]:
        """Type-check a LITERAL ``context.value``.

        A protocol-native DataBinding ``{"path": …}`` (A5-IFACE-01,
        option 1 of the ticket's adjudication) is NOT a literal: its
        legality is the binding whitelist's to judge — the same rule as
        every other binding in ``_check_bindings`` — and the eventual
        resolved value's type is re-proved on the write path (the
        client resolves bindings before dispatch, and the transport
        re-validates the POSTed literal). Never isinstance it against
        the variable's value_type."""
        var = self._context.variable(key) if key else None
        if var is None:
            return []
        if isinstance(value, dict) and set(value.keys()) == {"path"} \
                and isinstance(value["path"], str):
            p = value["path"]
            if p not in self._whitelist:
                return [
                    f"component {cid!r}: action value binding path {p!r} "
                    "is not a whitelisted path of the current data model"]
            return []
        vt = var.value_type
        if vt == "boolean" and not isinstance(value, bool):
            return [f"component {cid!r}: variable {key!r} expects a boolean"]
        if vt in ("number", "integer") and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (vt == "integer" and not isinstance(value, int))):
            return [f"component {cid!r}: variable {key!r} expects a number"]
        if vt in ("string", "date", "text", "status") and not isinstance(value, str):
            return [f"component {cid!r}: variable {key!r} expects a string"]
        return []

    # ── 5. content safety ──────────────────────────────────────────────
    def _check_content(self, components: list[dict[str, Any]]
                       ) -> list[str]:
        errors: list[str] = []
        for comp in components:
            cid = comp.get("id", "?")
            url = comp.get("url")
            if isinstance(url, str) and _is_forbidden_url(url):
                errors.append(
                    f"component {cid!r}: absolute/protocol-relative URLs "
                    f"are forbidden ({url[:48]!r})")
            for s in _iter_strings(comp):
                if len(s) > MAX_TEXT_LENGTH:
                    errors.append(
                        f"component {cid!r}: text exceeds "
                        f"{MAX_TEXT_LENGTH} chars")
                    break
                low = s.lower()
                if any(marker in low for marker in _FORBIDDEN_TEXT_MARKERS):
                    errors.append(
                        f"component {cid!r}: script-like content is "
                        "forbidden")
                    break
        return errors


def _is_forbidden_url(url: str) -> bool:
    """Absolute URLs, protocol-relative URLs and deep-link-looking strings
    are forbidden; only same-origin relative refs survive (contract §3
    bans deep-link URLs from model input entirely)."""
    if not url:
        return False
    if "://" in url:
        return True
    if url.startswith("//"):
        return True
    if url.lower().startswith(("javascript:", "data:", "vbscript:")):
        return True
    return any(ch.isspace() for ch in url)


def _whitelisted_paths(data_model: Mapping) -> set[str]:
    """Import-safe binding whitelist (same rules as
    data_model.binding_path_whitelist)."""
    from taskvm.genui.data_model import binding_path_whitelist
    return binding_path_whitelist(data_model)
