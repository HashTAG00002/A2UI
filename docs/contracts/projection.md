# Contract: `taskvm.projection` (L5 Projection & User Frontend)

> Owner: Agent D (`05_PROJECTION_FRONTEND_AGENT.md`). Frozen 2026-08-15.
> One page, result-oriented. Layered-ownership applies: content legality of
> every object D constructs is D's; STATE/TIME/HISTORY/TRANSITION stay with
> the Kernel; execution stays with the Runtime; composition stays with the
> Architect. D consumes public facades only.
> This contract freezes OUTCOMES, not CSS classes / DOM selectors / Flask
> internals / animation parameters. Audit accepts only against this page +
> handoff 05.

## 0. One-liner

Projection is TaskVM's **read-side mirror + governance command entry**: it
projects Kernel snapshots + Runtime events/artifacts into a continuously
living, editable, verifiable task UI. It never plans, never executes a GUI
action inside a route, never calls a model for ordinary updates, and never
learns which substrate it is looking at.

```text
Kernel (truth)  ──snapshot──▶  Projection view models ──SSE delta──▶ Browser
Runtime (facts) ──event/artifact──▶         ▲
Human (governance) ──HTTP command──▶ Kernel facade (the ONLY write path)
```

## 1. Ownership

| D owns | D does NOT own |
|---|---|
| HTTP request schema of governance commands (LocalPatch / GoalPatch / checkpoint / rollback / conflict resolution request bodies) | kernel state machine, epoch, patch semantics |
| View-model construction (task state, workflow map, surface cards, checkpoint timeline, governance bar) | runtime execution clock, verifier |
| SSE delta semantics + transport (connect / reconnect / revision recovery) | artifact production (runtime/substrate capture) |
| Read-only artifact serving from the projection-side store | session composition (kernel+runtime+substrate wiring) |
| `workspace_ui/**` migration/deletion; T2 transitional debt | GoalPatch recomposition (architect-owned, injected) |

## 2. Dependency rule (enforced by `tests/architecture/test_import_boundaries.py`)

```text
taskvm.projection → taskvm.domain + taskvm.kernel + taskvm.architect +
                    taskvm.runtime (public facades) + Flask/stdlib
banned: taskvm.substrate (WHOLE tree incl. the port root — sessions are
        handed in at composition), benchmark, evaluation, governance,
        task_state, execution, workspace_ui, _migration
```

Consequence: projection **cannot create sessions**. The composition root
(Agent E bootstrap / Agent G integration) constructs kernel + runtime +
substrate and **registers** the bundle with the projection session store.
Projection defines the registration seam (§5); it never re-invents a
facade helper to reach a substrate.

## 3. ProjectionSchema vs ProjectionData (the no-re-render law)

- Source of truth is the Kernel pair `projection().schema` /
  `projection().data` with **independent revisions** (kernel.md §2).
- The browser applies **data deltas** for ordinary value/progress/status
  changes: `Runtime/Kernel state change → typed event/delta → projection
  store → browser delta update`. **0 GenUI / Task Architect calls.**
- A schema re-fetch happens ONLY when `schema_revision` advances
  (GoalPatch recompose / initial composition). One number change must not
  regenerate the page.
- Test-pinned: N≥20 consecutive data deltas ⇒ architect/GenUI call count
  unchanged (fake-counter regression in `tests/projection/`).

## 4. Runtime snapshot/event consumption boundary

- Projection reads: `kernel.task_state() / projection() / workflow() /
  checkpoints() / events() / epoch / pending_recompose` and
  `runtime.runtime_events()` (append-only). Nothing else; no private
  attributes, no second kernel, no substrate re-observe.
- The ONLY writes projection performs go through the eight governance
  command routes of §6 — each landing on the kernel facade.
- Delta generation is server-side data-plane polling of snapshot
  revisions; it costs 0 model calls and never triggers substrate
  `observe/act/capture` side effects.
- If Runtime lacks an event/artifact projection needs: file a public
  interface request to E/Integration — never import runtime internals.

## 5. Surface artifacts & session seam

- `ProjectionSessionStore.register(sid, kernel, runtime=None,
  surfaces=(), artifacts=())` is the composition seam. `artifacts` are
  `(ref, mime, bytes)` visual artifacts already captured by the
  runtime/composition; a composition-side pusher may append more as
  runtime events arrive.
- `GET .../artifacts/<ref>` serves ONLY stored bytes: no capture-on-click,
  no model call, no CUA execution, no substrate re-observation. Missing
  ref ⇒ honest 404 with a business-readable message; the UI shows
  "尚无观察" — never a 500.
- Surface identity = user-visible `display_name` (+ opaque surface token
  from RuntimeEvents). No entity ids, no `data-*-id`, no deep links, no
  operator vocabulary anywhere in model inputs or rendered output
  (GG red line §0; `noleak` discipline inherited).

## 6. Route / control semantics (frozen route matrix)

All URLs are generated server-side (`url_for`) or absolute API paths —
relative form actions that lose the `/<sid>` prefix are a contract
violation (the legacy 405 class). Commands are JSON POST; pages render
from one embedded snapshot then go delta-only.

| Route | Method | Request semantics | Success | Error semantics |
|---|---|---|---|---|
| `/sessions/<sid>` | GET | page load | 200 HTML + embedded snapshot | unknown sid ⇒ 404 page |
| `/api/sessions/<sid>/snapshot` | GET | full view-model bundle + revisions | 200 JSON | 404 JSON |
| `/api/sessions/<sid>/events` | GET | SSE stream (`Last-Event-ID`/revision reconnect) | 200 `text/event-stream` | 404 JSON |
| `/api/sessions/<sid>/artifacts/<ref>` | GET | stored artifact bytes | 200 `image/*` | 404 JSON business message |
| `/api/sessions/<sid>/start` | POST | begin/resume autonomy via runtime driver | 200 `{state}` | 404 / 409 (pending recompose) |
| `/api/sessions/<sid>/pause` | POST | soft pause at next action boundary | 200 `{state}` | 404 |
| `/api/sessions/<sid>/resume` | POST | resume autonomy | 200 `{state}` | 404 / 409 |
| `/api/sessions/<sid>/stop` | POST | governance stop | 200 `{state}` | 404 |
| `/api/sessions/<sid>/checkpoints` | POST | `{label}` governance checkpoint | 201 `{checkpoint_id}` | 409 unstable boundary / 400 |
| `/api/sessions/<sid>/local-patches` | POST | `{updates:{key:value}, rationale}` | 200 `{retargeted_nodes}` | 400 schema / 404 / 422 non-editable key |
| `/api/sessions/<sid>/goal-patches` | POST | `{goal, constraints?, scope?, success_criteria?, rationale}` | 202 `{phase}` (see §8) | 400 / 404 |
| `/api/sessions/<sid>/rollback` | POST | `{target_checkpoint_id, rationale?}` | 202 plan accepted (execution async) | 404 unknown checkpoint / 400 |
| `/api/sessions/<sid>/conflicts/<cid>/resolve` | POST | `{resolution: keep_world\|apply_desired\|edit_*, detail?}` | 200 | 404 / 400 |

Normal-path guarantees: **0 unexpected 405, 0 unexpected 500, 0 unhandled
browser console errors**; invalid input ⇒ structured 4xx JSON; unknown sid
⇒ understandable 404; method mismatch on a known path ⇒ 405 (allowed ONLY
when the client actually used the wrong verb — the served page never
generates one).

## 7. SSE event vocabulary (transport is replaceable; semantics are frozen)

`projection_delta` (values/progress), `workflow_delta` (node statuses /
loop iteration), `runtime_status` (autonomy state machine), 
`surface_observation` + `screenshot_available` (artifact refs),
`verification_result` (passed/failed + evidence ref),
`checkpoint_committed`, `compensation` (requested/applied/partial/failed/
discarded disposition), `conflict`, `error` (business-readable). Events
carry monotone ids; clients may drop out-of-order/duplicate events and
recover from snapshot + revisions. Whole-page reload as a sync mechanism
is a contract violation.

## 8. Governance UI semantics

- **LocalPatch vs GoalPatch must be semantically distinct in UX**: a
  LocalPatch edits a local target (no terminal/scope/topology change; UI
  shows retargeted nodes immediately, pending divergence visible); a
  GoalPatch changes terminal goal/scope/constraints — the UI must tell the
  user that **committed history is preserved and the uncommitted future
  will be recomposed**, and must not be a plain text-field POST.
- GoalPatch two-phase honesty: phase-1 (`apply_goal_patch`) always lands;
  closure requires the architect. Projection holds an injected
  `GoalRecomposer` port; if absent/failed, the UI honestly shows
  "awaiting recompose / blocked" from `kernel.pending_recompose` — never
  a fake success, never an in-route model call as fallback.
- Checkpoint commit respects the kernel's stable-boundary guard (409 with
  business meaning "an action is in flight"); rollback surfaces per-entry
  outcomes and uncompensatable (irreversible) entries with lock semantics
  — an honest PARTIAL is shown as partial, never as success.
- Pause/resume/stop reflect the runtime's real autonomy state; no timer-
  driven fake progress anywhere.

## 9. Workflow visualization primitives

Server computes the full workflow view model; the browser only renders
it (no client-side topology guessing). The model must express, from the
frozen kernel primitives only:

- **Sequence**: ordered progression A → B → C.
- **Fan-out / Barrier / Fan-in**: one goal node spreading into lanes that
  re-converge at a verify barrier.
- **Bounded loop**: current iteration / max iterations / termination
  state / body progress.
- Node business status: waiting / ready / executing / verified(committed)
  / failed / invalidated / compensated; checkpoint markers with
  business-language labels and rollback affordances.
- Labels are business language (label/semantic goal), never raw ids like
  `node17`, `parallel-fanout-2`, `loop_control_x` in the default view
  (ids may exist in inspectable detail, not as the primary label).

## 10. Animation constraint

Animation may only express real VM state transitions (lane activation,
fan-out, verification, fan-in, checkpoint commit, rollback, conflict,
GoalPatch recompose), driven by runtime/kernel state — never fixed timers
pretending progress. `prefers-reduced-motion` respected. Decoration is
out of scope.

## 11. Platform transparency

No `if substrate == ...` / `if app == ...` / concrete substrate imports /
platform URLs or selectors anywhere in projection code or view models.
Multi-surface rendering treats every surface identically via
`display_name` + artifacts.

## 12. Tests (owner: D, in `tests/projection/` + `tests/e2e_ui/`)

- **Route matrix tests**: every route × method above; no 405 on the
  served page's own actions; structured 4xx on invalid input; 404 on
  unknown sid; SSE content-type; artifact 404 path.
- **View-model tests**: workflow primitives correctness (sequence /
  fan-out / barrier / loop iteration), business labels, checkpoint
  timeline, surface cards from events.
- **Event adapter tests**: kernel `EventKind` + `RuntimeEventKind` →
  SSE vocabulary mapping is total and typed.
- **Model-call regression**: fake counters — 20 data deltas ⇒ +0
  architect/compiler/CUA calls; page render / snapshot / artifact /
  surface-detail ⇒ +0 calls.
- **Import gate**: already armed in `tests/architecture` (§2).
- **Playwright E2E** (real frontend, real browser): open session → start
  → workflow delta → surface card detail with preloaded screenshot →
  LocalPatch → pause/resume → GoalPatch (recompose path) → checkpoint →
  rollback (disposition visible) → conflict resolution → SSE reconnect;
  0 console errors / 0 page errors / 0 unexpected 405/500.

## 13. Known boundaries (routed, not D's)

- Production bootstrap (kernel+runtime+substrate wiring): Agent E /
  Agent G; D consumes the registration seam until `workspace_ui` is
  migrated/deleted (handoff 05 T2 coordination).
- Model-augmented verifier / artifact production cadence: Agent E.
- Final benchmark UI-freezing: Agent F.
