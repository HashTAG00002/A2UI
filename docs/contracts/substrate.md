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

## 8. Transitional Debt Register — FORMAL LOCK PENDING (audit B-F1)

> **This section does NOT amend the contract.** §6 stays frozen exactly as
> written. This register records KNOWN, STILL-STANDING §6 violations that
> predate the freeze and are scheduled for DELETION (not for permission).
> Every entry below is a violation today, remains a violation until its
> exit criterion lands, and no test may interpret its presence here as
> contract satisfaction. Adding an entry requires an explicit RFC accepted
> by the governance owner — a test allowlist may not silently amend this
> contract (Oracle audit B-F1, 2026-08-16).

| # | Violation (file) | What violates §6 | Owner | Exit criterion |
|---|---|---|---|---|
| T1 | `taskvm/execution/gui_driver.py` | upper layer imports concrete substrate impls; keeps `_WEB_APPS`/`_MOBILEGYM_APPS`/`_OP_FIELD`/`_ENTITY_KIND` platform tables; legacy task-level `POST /api/{app}/{sid}/{entity_id}` transport | Agent E | file DELETED; runtime consumes `SubstrateSession` + ActionContract→CUA→GuiAction; killtest write paths migrated or the scripts deleted (F) |

**Formal LOCK procedure** (mechanical, no judgment call):

```bash
# 1. shrink the register in tests/substrate/test_no_api_backdoor.py
#    (TRANSITIONAL_DEBT_REGISTER) to {} as T1 lands
# 2. run the explicit lock audit — must be ALL GREEN before anyone stamps
#    the substrate contract as formally LOCKED:
TASKVM_SUBSTRATE_LOCK_AUDIT=1 pytest tests/substrate -q
```

The lock-audit test FAILS while any register entry remains (default CI
skips it so parallel waves are not blocked; skipping is visibility, not
silence — the register itself is asserted to mirror this table).

Agent B status per audit: **OWNER-COMPLETE / CODE-FROZEN / FORMAL LOCK
WAITING ON E + G cleanup**. B does not reopen substrate scope; the LOCK
stamps mechanically when the register is empty.
