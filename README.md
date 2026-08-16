# TaskVM

**Compile live state of multiple existing applications into an editable,
executable, verifiable task interface.**

人操作任务，Agent 操作应用。Agent 把多个正在运行的**既有应用**的实时状态，
反向编译成一个可编辑、可执行、可验证的任务界面；用户改一个任务变量，Agent
把改动可靠写回多个真实应用，独立 verifier 读 ground-truth 判定"改的发生、
没改的不动、界面重新同步"。

Four anchors (all must be present): `existing applications` / `live state` /
`executable binding` / `round-trip verification`. First-class contribution =
**Executable Projection Fidelity**. Verifier always reads hidden canonical
sandbox state; the binding-generating model never self-judges.

- Authority doc: [`docs/A2UI_开工大纲_v0_心智模型对齐版.md`](docs/A2UI_开工大纲_v0_心智模型对齐版.md) (单一权威文档·锁定版)
- W1 plan: `~/.claude/plans/mellow-roaming-quilt.md`

## W1 kill-test

2 apps (Calendar :3013 + TaskBoard :3014) → frontier compiler → task surface →
user edits one task variable → agent cross-applies via app-API → independent
verifier reads hidden canonical state → score (changed-happened /
non-interference / interface-re-synced). Replay-mode: the compiler's INPUT is
a frozen hand-authored trace; execute + verify are live.

### Install

```bash
pip install -e .
pip install playwright && playwright install chromium   # only needed for W2 live CUA
export OPENAI_API_KEY=...   # or rely on the default proxy key in taskvm/benchmark/model_client.py
```

### Run the apps

```bash
docker compose up        # calendar :3013, taskboard :3014
# or, without docker:
python -m taskvm.apps.calendar.app --port 3013 &
python -m taskvm.apps.taskboard.app --port 3014 &
```

### Run the kill-test

```bash
python -m taskvm.evaluation.cli run --suite smoke
python -m taskvm.evaluation.cli run --suite final --condition taskvm --seeds 3
```

Results → `eval_results/w1_<ts>.json`: per-sample round-trip score, per-check
fractions, binding-accuracy, which-link-broke, neg-control score.

### What W1 tests (vs. does NOT)

The gate-critical claim is the **model's binding discovery** — can a frontier
model, given only rendered app observations, compile a correct typed
task-state graph + binding (`task_state/compiler.py`)? The PASS criterion "no
hand-written binding" tests THIS. `execution/patch_compiler.py`'s patch
generation (applying an edit to the now-fixed binding's known operators) is
deterministic engineering — rule-based is fine, it is NOT what's tested.

### Load-bearing invariants (violating any voids the kill-test)

1. **Read-path-is-GUI / write-path-is-API split** — compiler reads rendered
   GUI observations; executor writes via app-API. Never let the compiler read
   the DB/session directly.
2. **No-leak canonical state** — `benchmark/fixtures.py` is verifier-only GT;
   never imported by `task_state/` or `execution/`.
3. **Negative-control** — broken-dispatcher run must score ≤0.3.

See the W1 plan for exit criteria + the three sub-kill triggers.
