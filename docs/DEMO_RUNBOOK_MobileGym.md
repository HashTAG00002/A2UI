# TaskVM × MobileGym Demo Runbook

> A demo-oriented runbook (not a full migration). Goal: show a mentor a running,
> honest TaskVM arc on the MobileGym substrate — bind → write → verify →
> **honest** rollback. Built on the work in `.mrules` E7–E9: the write path is
> real GUI gestures (no `set_state` backdoor), and the rollback **honestly
> reports irreversibility** (WeChat has no recall UI) rather than faking a
> restore.
>
> **Read before demoing**: §3 (the honest-boundary verbal script). The single
> most important thing not to get wrong: this demo proves **honest
> identification of an irreversible operation**, NOT "TaskVM achieved
> reversible compensation on MobileGym." Those are different technical claims
> (`.mrules` E9.3).

---

## 0. What this demo is and is NOT (read first)

| | |
|---|---|
| **IS** | A stress test of the TaskVM abstraction's generality on a new, write-restricted substrate (a Playwright-driven phone sim). The adapter/bridge/dispatcher/verifier/governance stack is reusable enough to mount a completely different backend. The write path is real gestures; the rollback honestly fails and says so in the UI. |
| **IS NOT** | A positive proof of any of the VM five properties. Binding is model-discovered on a single 2-app task (f1_triples=1.0, but byte-exact var_id is 0.0 — a granularity mismatch, not a failure). Reversibility is an **honest reverse example (irreversible)**, not a positive example. Verification numbers ARE persisted (`eval_results/`), but only for this one task. Reconciliation is not exercised here. |
| **Carries the paper's core claim?** | **No.** The Calendar/TaskBoard/Drive/Mail main line (W1–W4) carries the five-property claim. MobileGym is icing — a generality stress test, not foundation. Do NOT present it as "five properties validated on a new substrate." |

---

## 1. Prerequisites / environment (verified 2026-08-10)

- **Python**: the `taskvm` conda env (3.10; cloned from senseact 2026-08-12)
  is canonical. The `senseact` env ALSO works (ships Playwright + chromium),
  and the startup commands below use its absolute interpreter path on
  purpose — `conda run -n senseact` MIS-RESOLVES to codelab on this box, so
  use the absolute path directly. The `codelab` env (3.8) will NOT work.
  ```
  SENSEACT=/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/conda/envs/senseact/bin/python
  ```
- **MobileGym** is symlinked at `taskvm/thirdparty/mobilegym` (the source repo
  is left untouched per handoff §1). `node_modules` and `mobilegym-data` must
  exist (one-time `npm install` + the data clone — already done on this box).
- **Chromium**: the senseact env ships Playwright + a chromium build. The
  `.chromelibs/` lib bucket is set up (see memory `taskvm-chromium-launch-recipe`).
- **Display**: this box is **headless** (no X server, no `xvfb`). Headed
  Chromium is therefore NOT possible here — the bridge auto-falls-back to
  headless with a warning (`--headed` works on a display-equipped machine, or
  under `xvfb-run`). The "see it" deliverable is met by the **per-step
  screenshots** the bridge auto-captures to `eval_results/mobilegym_visual_*/`.
- The MobileGym **sim state is per-browser-session**: the bridge's Playwright
  chromium drives ONE session; the workspace_ui iframe (§3) is a *separate*
  session showing a default phone. The real data flow is workspace_ui edit →
  bridge gestures → bridge `read_canonical` → workspace_ui re-render; the
  read-only zone cards (not the iframe) show the bridge's driven state.

---

## 2. Startup sequence (verified commands)

From the repo root (`/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui`),
start the three services in order. Each is a long-running background process.

### 2.1 Vite dev server — the MobileGym phone sim (port 3000)

```bash
cd /mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/mobilegym
npm run dev   # → http://localhost:3000  (vite.config.ts pins port 3000)
```
Verify: `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000` → `200`.
(First boot ~15s; watch the vite log for `VITE ... ready`.)

### 2.2 The MobileGym bridge (port 3019)

```bash
cd /mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui
PYTHONPATH=taskvm/thirdparty/mobilegym:. \
  /mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/conda/envs/senseact/bin/python \
  -m taskvm.harness.mobilegym_bridge \
  --sim-url http://localhost:3000 \
  --screenshot-dir eval_results/mobilegym_visual_$(date +%Y%m%d_%H%M%S) \
  --port 3019
```
- `PYTHONPATH=taskvm/thirdparty/mobilegym:.` is **required** — `bench_env`
  (MobileGym's env) lives there and is not pip-installed.
- `--headed` is available but auto-falls-back to headless on this box (§1).
- The screenshot dir auto-captures each gesture step (§4).
Verify: `curl -s http://localhost:3019/health` → `{"status":"ok","site":"mobilegym"}`.

### 2.3 The workspace_ui governance console (port 3016)

```bash
cd /mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui
PYTHONPATH=. \
  /mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/conda/envs/senseact/bin/python \
  -m taskvm.workspace_ui.server \
  --task top3_expense_to_wechat \
  --sim-url http://localhost:3000 \
  --app-host localhost \
  --port 3016
```
The startup log prints the seeded session URL, e.g.
`http://127.0.0.1:3016/top3_expense_to_wechat_ui_<N>`. Open that in a browser.

### 2.4 Health-check all three (do this before demoing)

```bash
curl -s -o /dev/null -w 'vite %{http_code}\n'      http://localhost:3000
curl -s                http://localhost:3019/health
curl -s -o /dev/null -w 'workspace_ui %{http_code}\n' http://localhost:3016/top3_expense_to_wechat_ui_1
```

---

## 3. The live demo flow + verbal script (HONEST boundary — read carefully)

Open the workspace_ui URL from §2.3. You see the two-zone governance panel:
**只读区** (projected app state — wechat chats + alipay txs) and
**可读可写区** (the `expense_summary` editable field + undo/checkpoint).

### Step A — the write (real gestures, NOT a backdoor)

In the read-write zone, the `expense_summary` field is pre-filled with the
top-3 summary text (`本月top3支出: 520, 168, 42。省着点。`). Click **apply**.

**What happens (say this):**
> TaskVM compiles the edit into a `send_message` PatchOp and dispatches it.
> The bridge does NOT call `set_state` — it drives the app's OWN write
> pipeline with real GUI gestures: `open_app(wechat)` → tap the contact by
> visible name (黄勇) in the chat list → focus the composer → `type_text` →
> `Enter`. The sim's WeChat
> `handleKeyDown → handleSend → sendMessage` store mutation appends the
> message. The read-only zone re-syncs to show the message landed.

**Positive example to highlight:** the write path is real GUI gestures
through the app's own UI, exactly like an OSWorld agent would. The 3+ second
latency (vs. millisecond `set_state`) is itself evidence the gesture
sequence really ran.

### Step B — the honest rollback (THE point of the demo)

Click **↶ undo last wechat write**.

**What happens (say this):**
> TaskVM undoes the saga — one user action, cross-app, LIFO. The bridge
> tries to compensate the `send_message` via the app's own UI. But
> MobileGym's WeChat has **no delete/recall UI** for messages (no long-press
> handler, no `deleteMessage` store action, messages are append-only). So the
> bridge **honestly raises HTTP 409** — it does NOT fall back to `set_state`
> to fake a byte-exact restore. The saga marks `partial_failure=True`. The
> verifier independently re-reads the real state and confirms: the message
> is **still there** (fidelity = 0.0), so the saga's failure claim is TRUE,
> not a false alibi. The timeline below shows the locked step.

**The timeline UI (point to it):** a progress bar with one 🔒 red segment.
Try to drag the handle left — it **snaps to a hard stop** at the 🔒. This is
the "progress bar 拖不回去了" metaphor: you can roll back reversible steps,
but you can't drag past an irreversible one. The honest one-liner names the
irreversible step and why (no `set_state` backdoor).

### §3.1 The honest-boundary verbal script — POSITIVE vs NEGATIVE examples

**POSITIVE examples (say these):**
- ✅ "The write path is real GUI gestures, not a `set_state` backdoor."
- ✅ "TaskVM's rollback framework **honestly identifies and declares** an
  operation that cannot be compensated — it surfaces `partial_failure` to the
  user as a locked step, rather than silently faking a restore."
- ✅ "The verifier independently confirms the failure is real (the message
  is still there), so the honesty is auditable, not just a self-report."
- ✅ "The adapter abstraction is general enough to mount a completely
  different substrate (a Playwright phone sim) without touching
  rollback/reconciliation/verifier code."

**NEGATIVE examples (do NOT say these — they are overclaims a mentor/reviewer will catch):**
- ❌ "TaskVM achieved **reversible compensation** on MobileGym." — WRONG:
  sending a WeChat message is **irreversible**. We proved honest
  *identification* of irreversibility, not reversibility. These are
  technically different; conflating them is the E9.3 trap.
- ❌ "MobileGym validates the VM five properties on a new substrate." —
  WRONG: binding is only model-discovered on one task (with a var_id
  granularity mismatch); reversibility is an honest *reverse* example;
  reconciliation is not exercised here. The five-property claim is carried
  by the Calendar/TaskBoard/Drive/Mail main line, not this.
- ❌ "The rollback restored the state." — WRONG: the rollback **failed
  honestly**; the message is still in the chat. The timeline says so.

---

## 4. The persisted artifacts (E8 — show these to a reviewer)

These live in the repo (not chat/memory) and are re-auditable by anyone:

| Artifact | What it proves |
|---|---|
| `eval_results/mobilegym_killtest_<ts>.json` | round_trip=1.0 (write happened), honest-irreversibility 2/2 (fidelity=0.0, partial_failure, message-still-there), neg-control=0.3. The verdict string states "HONEST IRREVERSIBILITY, NOT reversible compensation." |
| `eval_results/mobilegym_visual_<ts>/step_NN_<action>.png` | Per-gesture screenshots: open_app → tap-contact-by-name → focus → type → enter → verify → undo-409-message-still-there. The "I can't see anything" deliverable. |
| `eval_results/mobilegym_timeline_<ts>.html` | The rendered honesty-based-rollback UI snapshot (the locked 🔒 segment + the honest message). |

Re-run the gate anytime:
```bash
PYTHONPATH=taskvm/thirdparty/mobilegym:. $SENSEACT -m taskvm.evaluation.run_mobilegym_killtest --samples 3
# add --no-binding-discovery to skip the model (GT-binding core gate only)
```

---

## 5. If something breaks (honest fallback)

- **Bridge not reachable :3019** → the MobileGymEnv didn't start. Check the
  bridge log for chromium launch errors; confirm `PLAYWRIGHT_BROWSERS_PATH`
  and `.chromelibs/lib` are set (the bridge's `main()` does this, but only if
  the env is the senseact one). The kill-test exits 2 loudly rather than
  fabricating scores (E8).
- **Vite not reachable :3000** → `npm run dev` didn't boot (first boot ~15s).
  The kill-test exits 2 if Vite is down.
- **Model API 429/timeout (binding discovery)** → the kill-test catches it
  per-sample and reports `compile_error: model_unavailable`; the core gate
  (round-trip + honest-irreversibility + neg) is model-free and still lands.
- **Headed requested but no display** → the bridge auto-falls-back to
  headless with a warning; screenshots still land. Don't pretend headed works.
