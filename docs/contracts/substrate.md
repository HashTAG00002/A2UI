# Contract: `taskvm.substrate` (L1 Substrate Port)

> Owner: Agent B (`03_SUBSTRATE_ISOLATION_AGENT.md`). Frozen 2026-08-14.
> One page, not an implementation manual. Layered-ownership: the substrate
> layer vouches for the *content legality* of everything it constructs
> (Observation has no hidden DB ids; GuiAction is a real-world gesture).

## 1. Goal

Everything outside `taskvm/substrate/` is substrate-blind. Upper layers see
one protocol; Web / OS / MobileGym differences never escape their
subdirectories. Substrate selection happens exactly once, at composition:

```text
SubstrateRegistry.create_session(name, config)   -> SubstrateSession
EvaluationRegistry.create(name, config)          -> EvaluationEnvironment
```

## 2. Runtime port (GUI-only)

```python
class SubstrateSession(Protocol):
    def list_surfaces(self) -> list[SurfaceInfo]
    def observe(self, surface, previous_fingerprint=None) -> Observation
    def act(self, surface, action: GuiAction, *, epoch: str) -> ActionReceipt
    def capture(self, surface) -> VisualArtifact
    def close(self) -> None
```

- `GuiAction` admits ONLY real-world actions: `click | tap | type | key |
  scroll | wait | open`. No `set`, `mutate`, `restore`, `assign`.
- `Observation` carries: screenshot reference, scrubbed visible text /
  accessibility info, surface fingerprint, TaskVM-owned handle candidates,
  revision + timestamp. Anything a real user cannot see on the rendered
  screen (hidden DB ids, `data-*-id`, internal object ids, deep-link URLs)
  MUST NOT appear.
- `ActionReceipt` reports what was really performed; honest failure is a
  status, never a fallback to a non-GUI write path.
- There is no `reset`/`seed`/oracle read on this object. Not "hidden" —
  absent.

## 3. SurfaceHandle (ephemeral, TaskVM-owned)

`SurfaceHandle` = runtime cache entry: `handle_id`, visible semantic anchor
(role, visible name/text, proximity), bounding info, structural fingerprint,
`last_seen_revision`. It is created/invalidated by TaskVM from Observations.
It is NEVER an app database primary key; fingerprints describe visible
structure only. Stale handles rebind via a fresh `observe`.

## 4. Evaluation plane (physically separate object)

```python
class EvaluationEnvironment(Protocol):
    def reset(self, sid) -> dict
    def seed(self, sid, *, task_id, goal, seed_state) -> dict
    def oracle_state(self, sid) -> dict          # hidden ground truth read
    def session_state(self, sid) -> dict          # non-GT summary
    def close(self) -> None
```

- May use simulator/setup APIs (`set_state`, snapshot restore, internal
  HTTP). Lives in `taskvm/substrate/<name>/evaluation.py`; importing it from
  runtime/projection/governance code is an architecture violation.
- The verifier/benchmark consume `oracle_state`; the runtime decision chain
  (model prompts, patch compilation, rollback planning) must not.

## 5. Implementations

| name | directory | notes |
|---|---|---|
| `builtin_web` | `substrate/builtin_web/` | Playwright; write = real mouse/keyboard; browser paths from config/env, never hardcoded user paths; app URLs only in provider config |
| `mobilegym` | `substrate/mobilegym/` | bridge keeps MobileGym async loop; `set_state` reachable only via its EvaluationEnvironment; session exposes observe/act over real gestures |
| `osworld` | `substrate/osworld/` | minimal real adapter: connect, list desktop surface, screenshot, click/type/key/scroll; honest error when env unavailable |

The MobileGym bridge must not import upper layers. Long-running CUA loops
are injected at process assembly time (`--cua-loop module:attr`), never a
library-level dependency.

## 6. Banned (static-gate enforced)

In `taskvm/substrate/**` (except explicit evaluation adapters and
prohibitive tests) and in every upper-layer directory:

- `executor="api"` / API mutation executors / `requests.post` app-mutation
- `read_canonical` (superseded by `EvaluationEnvironment.oracle_state`)
- hidden `set_state` / snapshot restore on the runtime port
- `data-event-id` / `data-task-id` / `data-file-id` in any
  observation/model-facing metadata
- upper layers importing `taskvm.substrate.builtin_web` /
  `taskvm.substrate.mobilegym` / `taskvm.substrate.osworld` (concrete
  implementations); only `taskvm.substrate` (the port root) is importable
- `if substrate == ...` branching outside composition/bootstrap

## 7. Tests

`tests/substrate/`: port contract (fake + builtin share semantics),
web visibility scrubbing, API-backdoor static gate, handle-cache
fingerprint invalidation, MobileGym honest-irreversibility (no cua loop →
501, never a hidden restore), portable browser paths (no user absolute
paths in repo), OSWorld contract. Gate file: `tests/substrate/test_no_api_backdoor.py`.

## 8. Transitional Debt Register — EMPTY (Wave-3 landed 2026-08-16)

> **This section does NOT amend the contract.** §6 stays frozen exactly as
> written. This register recorded KNOWN, STILL-STANDING §6 violations that
> predated the freeze and were scheduled for DELETION (not for permission).
> Adding an entry requires an explicit RFC accepted by the governance
> owner — a test allowlist may not silently amend this contract (Oracle
> audit B-F1, 2026-08-16).

**Register: EMPTY.** Both entries are resolved:

| # | Violation (file) | Resolution |
|---|---|---|
| T1 | `taskvm/execution/gui_driver.py` | DELETED 2026-08-16 by Agent G Wave-3, together with its three live import hosts (`governance/vm_state.py` / `execution/action_dispatcher.py` / `execution/rollback.py`) and the whole legacy execution / governance / workspace_ui / verifier cluster (one commit). Runtime consumes `SubstrateSession` + ActionContract→CUA→GuiAction. |
| T2 | `taskvm/workspace_ui/server.py` | RESOLVED 2026-08-16 by Agent D (anchor_lookup deleted; targeting via Observation → State Compiler → SurfaceHandle); the whole legacy server followed in Wave-3. |

**Formal LOCK condition** (mechanical, no judgment call):

```bash
# 1. the register in tests/substrate/test_no_api_backdoor.py
#    (TRANSITIONAL_DEBT_REGISTER) is {}  — DONE (Wave-3)
# 2. the explicit lock audit — ALL GREEN as of Wave-3:
TASKVM_SUBSTRATE_LOCK_AUDIT=1 pytest tests/substrate -q
#    → 35 passed (2026-08-16, Agent G; evidence eval_results/)
```

Agent B status: **OWNER-COMPLETE / CODE-FROZEN / FORMAL LOCK CONDITION
MET — register empty, lock audit green; Oracle stamp pending.** B does not
reopen substrate scope.
