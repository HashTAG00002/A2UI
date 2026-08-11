"""A2UI v0.9 protocol spec + TaskVM binding-discovery contract.

The compiler's system prompt = ``A2UI_V09_SPEC`` (transcribed verbatim from the
**official** A2UI protocol repo, not a paper appendix — see
``docs/references/A2UI-protocol-spec/SOURCE.txt`` for the exact file mapping
and fetch date) + ``TASKVM_BINDING_CONTRACT`` (the TaskVM-specific extension
that asks the model to ALSO emit a typed ``task_binding`` — the gate-critical
output).

**2026-08-11: upgraded from v0.8 to v0.9** (user decision — proxy configured in
``~/.bashrc`` made ``a2ui.org`` / ``github.com/a2ui-project/a2ui`` reachable, so
the previous "a2ui.org is network-blocked, hand-transcribe from the Macaron
paper appendix" workaround is no longer necessary). The v0.9 text below is
copied from the protocol's own source-of-truth files
(``specification/v0_9/docs/a2ui_protocol.md`` +
``specification/v0_9/catalogs/basic/{catalog.json,rules.txt}`` in
https://github.com/a2ui-project/a2ui), which is a stronger provenance than a
paper's abbreviated appendix. Full copies of these files (plus the formal JSON
Schema and the v0.8->v0.9 evolution guide) are checked into
``docs/references/A2UI-protocol-spec/v0_9/`` for independent verification.

This honors handoff §7 / locked-decision: an A2UI spec injected into the system
prompt; no Macaron model download; not pinned to any one version (protocol
upgrade = swap this string — this file is the only place that changes).

**v0.8 -> v0.9 is NOT a wording tweak** (see
``docs/references/A2UI-protocol-spec/v0_9/docs/evolution_guide.md`` for the
full diff, confirmed against the official repo, not the paper): message names
``beginRendering``/``surfaceUpdate``/``dataModelUpdate`` are replaced by
``createSurface``/``updateComponents``/``updateDataModel``; the component
wrapper changes from a keyed object (``{"Text": {...}}``) to a flat
discriminator (``{"component": "Text", ...}``); ``dataModelUpdate``'s
array-of-typed-pairs (``[{"key":"name","valueString":"Alice"}]``) becomes a
plain JSON object (``{"name": "Alice"}``); bound values collapse from
``{"literalString": ...}``/``{"path": ...}`` into a single ``DynamicString``
union (a bare string OR ``{"path": "..."}``); button ``context`` becomes a
plain object instead of an array of key-value pairs; ``primary: true`` becomes
``variant: "primary"``. Any code, examples, or fixtures written against the old
v0.8 message shape will NOT validate against v0.9 — this is a breaking
protocol version, not a superset.

Also verified: diffing the official v0.9 vs v0.9.1 doc, the only changes are
wording clarifications + version-string bumps (no schema/message changes) — so
pinning to v0.9 (rather than also tracking v0.9.1) loses nothing.
"""

# ── A2UI v0.9 "prompt-first" system prompt ────────────────────────────────────
# Transcribed from the official spec (see module docstring for provenance).
# v0.9's own design principle (evolution_guide.md §1) is to be embedded
# directly in a prompt (vs v0.8's structured-output-only assumption), so this
# transcription is *closer* to the protocol's intended usage than the old v0.8
# text was.
A2UI_V09_SPEC = """\
## A2UI (Agent to UI) Protocol v0.9

A2UI renders UI from a stream of JSON **messages**. Each message is a JSON
object with **exactly ONE** of these four keys (plus a top-level `version`
key set to the literal string `"v0.9"`):

- `createSurface`: {surfaceId: string, catalogId: string, theme?: object,
  sendDataModel?: boolean} — creates a new surface. Must be sent before any
  `updateComponents`/`updateDataModel` for that surfaceId. `catalogId` is just
  a string identifier (does not need to resolve to a URL); use
  `"https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"` for the
  basic catalog used here.
- `updateComponents`: {surfaceId: string, components: array} — defines/updates
  the component tree. `components` is a **flat list**; parent-child structure
  is expressed purely via `id` references (an adjacency list), so components
  can arrive in any order. Exactly ONE component across all `updateComponents`
  messages for a surface MUST have `"id": "root"`.
- `updateDataModel`: {surfaceId: string, path?: string, value?: any} — upserts
  data. `path` is a JSON Pointer (e.g. `/user/name`); omit or use `/` to
  replace the whole data model. Omitting `value` deletes the key at `path`.
- `deleteSurface`: {surfaceId: string} — removes a surface and its contents.

### Component object shape (flat discriminator, NOT a keyed wrapper)

Each item in `components` is `{"id": "...", "component": "TypeName", ...props}`
— i.e. the component type is the **value** of the `component` property, not a
wrapper key. Example: `{"id": "title", "component": "Text", "text": "Hello"}`
— NOT `{"id": "title", "component": {"Text": {"text": "Hello"}}}`.

### Data binding (`Dynamic*` union — a bare literal OR a path OR a function call)

Any bindable property accepts EITHER a literal value directly (e.g.
`"text": "Hello"`), OR `{"path": "/json/pointer"}` to read from the data
model, OR a `FunctionCall` object `{"call": "fnName", "args": {...},
"returnType": "string"}`. There is no separate `literalString`/`valueString`
wrapper in v0.9 — a bare string/number/boolean IS the literal.

- **Absolute path** (starts with `/`): resolves from the surface's data-model
  root, e.g. `{"path": "/articles/a1/title"}`.
- **Relative path** (no leading `/`): only valid inside a template-generated
  child (see `ChildList` below); resolves against the current list item.

### `ChildList` — how containers reference children

A container's children property is EITHER a static array of child `id`
strings (`["child_a", "child_b"]`), OR a template object
`{"componentId": "template_id", "path": "/list/in/data/model"}` that
instantiates one copy of `template_id` per item in the array at `path`
(each copy gets its own relative-path scope).

### Actions

Interactive components (`Button`, etc.) use an `action` property with EITHER:
- `{"event": {"name": "...", "context": {...plain JSON object...}}}` — sends
  an event to the server; `context` is a plain object (values may themselves
  be `Dynamic*`), NOT an array of key-value pairs.
- `{"functionCall": {"call": "...", "args": {...}}}` — runs a local
  client-side function only (no server round-trip).

### Validation `checks` (used by TextField/CheckBox/etc. and by Button)

`"checks": [{"call": "required", "args": {"value": {"path": "..."}},
"message": "..."}]` — each check's `call` must return a boolean; if a check on
a Button fails, the client disables the Button.

## Available Components (Basic Catalog — use ONLY these 18)

Text, Image, Icon, Video, AudioPlayer, Row, Column, List, Card, Tabs, Divider,
Modal, Button, CheckBox, TextField, DateTimeInput, ChoicePicker, Slider

## Key Rules (from the official basic-catalog `rules.txt`, verbatim)

1. You MUST include ALL required properties for every component, even inside a
   template or if the value will be bound to data.
2. For `Text`, you MUST provide `text` (bare string or `{"path": "..."}`).
3. For `Image`, you MUST provide `url` (bare string or `{"path": "..."}`).
4. For `Button`, you MUST provide `action`.
5. For `TextField`, `CheckBox`, etc., you MUST provide `label`.

## Additional structural rules (from the protocol doc, not the rules.txt)

6. Exactly one component across the whole surface must have `"id": "root"`.
7. Use only ONE `surfaceId` per reply. Do not create multiple surfaces.
8. `TextField`/`CheckBox`/`Slider`/`ChoicePicker`/`DateTimeInput` are two-way
   bound: use `value` (not `text`) for their bound value.
9. `Button`/`Text`/`Image` styling uses a `variant` string enum (e.g.
   `variant: "primary"` for Button, NOT a boolean like `primary: true`).
10. `ChoicePicker` (not `MultipleChoice`) takes `value` (array) + `variant`
    (`"multipleSelection"` or `"mutuallyExclusive"`) + `options`
    (`[{"label":..., "value":...}]`).
11. `Row`/`Column` use `justify`/`align` (not `distribution`/`alignment`).

## Example message stream (JSONL, abbreviated from the official spec)

{"version": "v0.9", "createSurface": {"surfaceId": "s1", "catalogId": "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"}}
{"version": "v0.9", "updateComponents": {"surfaceId": "s1", "components": [{"id": "root", "component": "Column", "children": ["title", "submit"]}, {"id": "title", "component": "Text", "text": {"path": "/user/name"}}, {"id": "submit", "component": "Button", "child": "submit_label", "variant": "primary", "action": {"event": {"name": "submit_form", "context": {"userId": "123"}}}}, {"id": "submit_label", "component": "Text", "text": "Submit"}]}}
{"version": "v0.9", "updateDataModel": {"surfaceId": "s1", "path": "/user", "value": {"name": "Alice"}}}
{"version": "v0.9", "deleteSurface": {"surfaceId": "s1"}}
"""

# Back-compat alias: some call-sites may still refer to the old name during
# the transition. New code should use A2UI_V09_SPEC directly.
A2UI_V08_SPEC = A2UI_V09_SPEC  # noqa: N816 - intentional compat alias, see docstring


# ── TaskVM binding-discovery contract (the gate-critical extension) ──────────
# The model must ALSO emit a typed task_binding: each task variable bound to the
# real app entities (identified by the data-event-id / data-task-id attributes
# visible in the DOM) with an executable operator. This is the output W1 tests
# (PASS requires "no hand-written binding" — the model discovers it).
TASKVM_BINDING_CONTRACT = """\
## TaskVM Task-Binding Contract (CRITICAL — gate-critical output)

Alongside the A2UI surface, you MUST compile a **task_binding**: a typed
task-state graph that binds each task variable to the real application entities
you can observe, with the executable operator that would change each.

You are given: (a) the task goal, (b) rendered observations of one or more
running web apps (screenshot + DOM HTML + accessibility-tree text), and (c) a
tool schema listing the executable operators each app exposes. The DOM marks
every entity with a stable id attribute: Calendar events carry
`data-event-id="E1"`, TaskBoard tasks carry `data-task-id="T3"`, Drive files
carry `data-file-id="F1"`. USE THESE ids as `entity_id` — do not invent new ones.

### Output shape (add a `task_binding` key alongside `text_response` and `a2ui`)

{
  "text_response": "one-line summary of the task surface you compiled",
  "a2ui": [ ...A2UI v0.9 messages (createSurface/updateComponents/updateDataModel)... ],
  "task_binding": {
    "task_id": "<the task_id you were given>",
    "variables": [
      {
        "var_id": "release_date",
        "label": "发布日期",
        "value": "2026-08-14",
        "editable": true,
        "bindings": [
          {"app": "calendar",   "entity_id": "E1", "field": "date",     "operator": "move_event"},
          {"app": "taskboard",  "entity_id": "T1", "field": "deadline", "operator": "set_deadline"}
        ]
      }
    ],
    "dependencies": [
      {"from_var": "release_date",
       "to_entity": {"app": "taskboard", "entity_id": "T1"},
       "relation": "deadline_tracks_release_date"}
    ]
  }
}

### Rules
1. `var_id` is a stable snake_case identifier for a task quantity the user might
   edit (e.g. release_date, owner, meeting_time). `value` is its current value
   read from the observed app state.
2. Each binding's `entity_id` MUST be one of the `data-event-id` / `data-task-id`
   / `data-file-id` values present in the supplied DOM. Do not use an id that
   does not appear.
3. Each binding's `operator` MUST be one of the operators listed in the tool
   schema supplied to you. `app` and `field` identify where the entity lives.
4. `editable: true` for quantities a user can directly change; `false` for
   derived/display-only ones.
5. `dependencies` captures effect propagation: when `from_var` changes, the
   `to_entity` must sync. Use this for deadlines that track a date, statuses that
   track a milestone, etc.
6. Compile ONLY from the observations — do not assume entities you cannot see.
   If the task references an entity not visible in the DOM, omit it from bindings
   (do not hallucinate an id).
7. Keep the A2UI surface minimal: a structured view of the variables (a Text
   label + value per variable is sufficient). Do NOT generate fancy UI. The
   binding is what matters.
8. **var_id granularity follows the USER's mental-model quantity, NOT one var_id
   per entity.** A `var_id` names ONE quantity the user thinks of as a single
   editable thing; it MAY bind MANY entities that must move together when it
   changes. The patch layer applies the edit's single `new` value to EVERY
   binding of that var_id, so they cascade together — this is the bidirectional
   binding contract (one user edit → all bound entities sync).
   - MERGE under ONE shared `var_id` when several entities are assigned the SAME
     target value by the same user instruction AND they semantically track that
     one quantity. Example: the user moves "the release date" 8/14→8/18; the
     release meeting (calendar.E1.date) and every dependent task deadline
     (taskboard.T1/T2.deadline) all track that one date and all become 8/18 →
     ONE `var_id` `release_date` bound to E1 + T1 + T2.
   - SPLIT into SEPARATE `var_id`s when two entities take DIFFERENT target
     values, or the same value for semantically-independent reasons. Example:
     "raise the announcement's priority to high and lower the digest's priority
     to low" → two independent quantities → `announcement_priority` (→M1) and
     `digest_priority` (→M2), NOT one merged `mail_priority`.
   - Heuristic: would the user say "I changed THE X" (one quantity → one shared
     var_id) or "I changed X AND Y" (two quantities → two var_ids)? When a
     single `new` value is meant for several entities that all track it, prefer
     one shared `var_id`; when distinct target values are involved, the var_ids
     MUST be distinct.
"""


# The full compiler system prompt.
COMPILER_SYSTEM_PROMPT = A2UI_V09_SPEC + "\n" + TASKVM_BINDING_CONTRACT


# ── W1 fallback: binding-only directive (doc §10 de-prioritizes fancy UI) ────
# When the model over-spends tokens on the verbose A2UI surface and starves the
# gate-critical task_binding (truncation → json_parse_failure), we direct it to
# emit the task_binding FIRST and treat a2ui as optional/minimal. The A2UI spec
# is STILL injected (locked decision honored); only the output requirement
# relaxes. This is the W1 plan's permitted simplification.
BINDING_ONLY_DIRECTIVE = """\
## Output Priority (W1)

The GATE-CRITICAL output is ``task_binding``. Emit it FIRST and COMPLETELY.
The ``a2ui`` surface is OPTIONAL in W1 — you may omit it entirely (set
``"a2ui": []``) or emit at most a minimal Text-per-variable surface. Do NOT
spend tokens on a rich component tree; the binding is what is evaluated.

Output shape (task_binding complete, a2ui minimal or empty):
{"text_response": "one line", "a2ui": [], "task_binding": {...}}
"""


def compiler_system_prompt(binding_only: bool = True) -> str:
    """Build the compiler system prompt. ``binding_only=True`` (W1 default)
    appends the directive to prioritize task_binding over a rich a2ui surface.
    Pass ``binding_only=False`` for the full-A2UI-fidelity check (W1 close)."""
    base = A2UI_V09_SPEC + "\n" + TASKVM_BINDING_CONTRACT
    if binding_only:
        return base + "\n" + BINDING_ONLY_DIRECTIVE
    return base


# ── P4 (E10 rework): the GenUI decoder directive + prompt builder ────────────
# This is the SECOND model role (separate from the compiler). Given a VM-state
# (TaskBinding + current values), the model emits an A2UI v0.9 message stream
# that renders the two-zone governance surface. The renderer then translates
# that stream to HTML via a THIN layer (no "field X uses control Y" logic — the
# model decides the component tree; the renderer only maps A2UI component types
# to HTML). handoff §5 / §7.2 / §12.17 (GenUI must be a real model call).
GENUI_DECODER_DIRECTIVE = """\
## TaskVM GenUI Decoder Role (P4, E10 rework)

You are the **GenUI decoder**: given a TaskVM task state (variables + their app
bindings + current values), emit the A2UI v0.9 message stream that renders the
**two-zone governance surface** for that task state.

### Two-zone semantics (load-bearing — must be expressed in the component tree)

1. **Read-only zone** (projected app state): one Card per app, showing each
   bound entity's fields as Text. These components MUST be marked
   ``editable: false`` (the user cannot mutate app state directly from here).
2. **Read-write zone** (governance): for each variable with ``editable: true``,
   render an editable control (TextField for free-text/dates, ChoicePicker for
   finite-state fields, etc.) bound to that variable. Also render a Button
   labeled "↶ undo" and a Button labeled "⚑ checkpoint".

### Output shape (A2UI v0.9 messages — JSONL, one message per line)

{"version":"v0.9","createSurface":{"surfaceId":"taskvm","catalogId":"basic"}}
{"version":"v0.9","updateComponents":{"surfaceId":"taskvm","components":[
  {"id":"root","component":"Column","children":["ro_zone","rw_zone"]},
  {"id":"ro_zone","component":"Column","children":[<one Card per app with Text rows>]},
  {"id":"rw_zone","component":"Column","children":[<editable controls + undo + checkpoint buttons>]}
]}}
{"version":"v0.9","updateDataModel":{"surfaceId":"taskvm","path":"/","value":{<var_id: current_value>}}}

### Rules
- Output ONLY the A2UI v0.9 messages (JSONL, one per line). No prose, no fences.
- Every component in the read-only zone MUST have ``editable: false``.
- Every editable control in the read-write zone MUST have ``editable: true`` and
  bind to exactly one ``var_id`` (use a ``dataBinding`` property naming the var_id).
- The component tree structure (Card vs List, TextField vs ChoicePicker, layout)
  is YOUR choice — be generative. But the two-zone split + editable flags MUST be
  semantically correct (read-only zone = projected state, read-write zone = the
  editable variables + governance buttons).
- Use ONLY components from the basic catalog (Text, Image, Icon, Row, Column,
  List, Card, Tabs, Divider, Modal, Button, CheckBox, TextField, DateTimeInput,
  ChoicePicker, Slider).
"""


def genui_decoder_system_prompt() -> str:
    """Build the GenUI decoder system prompt (A2UI v0.9 spec + the two-zone
    governance directive). This is the P4 model role — SEPARATE from the
    compiler (different output shape: A2UI component tree, not task_binding)."""
    return A2UI_V09_SPEC + "\n" + GENUI_DECODER_DIRECTIVE


if __name__ == "__main__":
    # quick sanity: print token-ish size
    print(f"A2UI_V09_SPEC: {len(A2UI_V09_SPEC)} chars")
    print(f"TASKVM_BINDING_CONTRACT: {len(TASKVM_BINDING_CONTRACT)} chars")
    print(f"COMPILER_SYSTEM_PROMPT: {len(COMPILER_SYSTEM_PROMPT)} chars")
