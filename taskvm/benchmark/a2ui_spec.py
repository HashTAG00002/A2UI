"""A2UI v0.8 protocol spec + TaskVM binding-discovery contract.

The compiler's system prompt = ``A2UI_V08_SPEC`` (Macaron's own "w/ schema"
full-prompt baseline, transcribed verbatim from
``docs/references/Macaron-A2UI-paper/Appendix/prompts.tex:56-116``) +
``TASKVM_BINDING_CONTRACT`` (the TaskVM-specific extension that asks the model
to ALSO emit a typed ``task_binding`` — the gate-critical output).

This honors handoff §7 / locked-decision: A2UI v0.8 spec injected into the
system prompt; no Macaron model download; not pinned to v0.8 (protocol upgrade
= swap this string). The formal JSON Schema body is abbreviated in the paper
(a2ui.org is network-blocked); the field lists + 6 key rules below are
sufficient for W1.
"""

# ── A2UI v0.8 "w/ schema" system prompt (verbatim, prompts.tex:58-115) ───────
# Note: the paper's component list (lines 82-98) duplicates FullScreenModal and
# MarkdownView — deduplicated to the canonical 15 here.
A2UI_V08_SPEC = """\
You must strictly follow schema field names; do not use onClick/onPress/content or other undefined names. Available components (only these 15): Button, Card, Column, DateTimeInput, Divider, FullScreenModal, Icon, Image, Label, MarkdownView, Row, SelectionList, SelectionWrap, Tabs, TickSlider.

## A2UI Message Protocol (CRITICAL)

The `a2ui` array is a list of **messages**. Each element must be a message object with **exactly ONE** action key (do NOT put raw component items like {"id": "...", "component": {...}} at the top level).

- `beginRendering`: {surfaceId: string, root: string} --- create surface; root is the ID of the top-level component.
- `surfaceUpdate`: {surfaceId: string, components: array} --- define the component tree. Each item in components: {"id": "unique_id", "component": {"TypeName": {...props}}}.
- `dataModelUpdate`: {surfaceId: string, path: "/", contents: array} --- write data. Each entry: {key: "name", valueString: "text"} or valueNumber / valueBoolean.
- `deleteSurface`: {surfaceId: string} --- remove surface.

Correct pattern: use ONE surfaceId; include beginRendering (root pointing to top component), then surfaceUpdate (with components array), then dataModelUpdate if needed. Components go **inside** surfaceUpdate.components, not directly in a2ui.

## Key Rules
1. Every surfaceUpdate must have a matching beginRendering with root pointing to the top-level component ID.
2. All component IDs referenced as children must exist in the same surfaceUpdate.components.
3. Use only ONE surface (one surfaceId) per reply. Do not create multiple surfaces.
4. If using SelectionList/SelectionWrap, selection must include "literalArray": [] alongside "path"; do not add the selection key to dataModelUpdate.
5. If using interactive components (SelectionList, TickSlider, DateTimeInput), include a Button for confirm/submit.
6. dataModelUpdate valueString must not be empty "".

## Available A2UI Components
- Button, Card, Column, DateTimeInput, Divider, FullScreenModal, Icon, Image, Label, MarkdownView, Row, SelectionList, SelectionWrap, Tabs, TickSlider

### Key Concepts
1. **Data Binding with Paths**: Absolute path (starts with /) `{"path": "/articles/a1/title"}` resolves from root; relative path (no leading /) `{"path": "title"}` resolves relative to current context.
2. **Template Path Resolution**: When using List/Column/Row with `template`, children get a nested context — inside template children, use relative paths (without leading /).
3. **Literal Values**: Use `{"literalString": "text"}` / `literalNumber` / `literalBoolean` for static content.
4. **Actions**: Buttons dispatch events with `action.name` and `action.context`.

## A2UI JSON Schema
The four message types and their fields are specified above. Each a2ui message carries exactly one action key plus surfaceId. Component items use {"id": ..., "component": {"TypeName": {...props}}} inside surfaceUpdate.components.
"""


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
`data-event-id="E1"`, TaskBoard tasks carry `data-task-id="T3"`. USE THESE ids
as `entity_id` — do not invent new ones.

### Output shape (add a `task_binding` key alongside `text_response` and `a2ui`)

{
  "text_response": "one-line summary of the task surface you compiled",
  "a2ui": [ ...A2UI messages... ],
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
   values present in the supplied DOM. Do not use an id that does not appear.
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
7. Keep the A2UI surface minimal: a structured view of the variables (Label +
   value per variable is sufficient). Do NOT generate fancy UI. The binding is
   what matters.
"""


# The full compiler system prompt.
COMPILER_SYSTEM_PROMPT = A2UI_V08_SPEC + "\n" + TASKVM_BINDING_CONTRACT


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
``"a2ui": []``) or emit at most a minimal Label-per-variable surface. Do NOT
spend tokens on a rich component tree; the binding is what is evaluated.

Output shape (task_binding complete, a2ui minimal or empty):
{"text_response": "one line", "a2ui": [], "task_binding": {...}}
"""


def compiler_system_prompt(binding_only: bool = True) -> str:
    """Build the compiler system prompt. ``binding_only=True`` (W1 default)
    appends the directive to prioritize task_binding over a rich a2ui surface.
    Pass ``binding_only=False`` for the full-A2UI-fidelity check (W1 close)."""
    base = A2UI_V08_SPEC + "\n" + TASKVM_BINDING_CONTRACT
    if binding_only:
        return base + "\n" + BINDING_ONLY_DIRECTIVE
    return base


if __name__ == "__main__":
    # quick sanity: print token-ish size
    print(f"A2UI_V08_SPEC: {len(A2UI_V08_SPEC)} chars")
    print(f"TASKVM_BINDING_CONTRACT: {len(TASKVM_BINDING_CONTRACT)} chars")
    print(f"COMPILER_SYSTEM_PROMPT: {len(COMPILER_SYSTEM_PROMPT)} chars")
