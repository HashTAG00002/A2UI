# Agent F 会话交接说明（2026-08-16，F1–F7 + F6b 完成后，F8–F12 待办）

> **交接对象**：接手 Agent F（Final Benchmark）的下一个 coding agent。
> **任务书**：`docs/oracle/new-oracle/handoffs/07_FINAL_BENCHMARK_AGENT.md`（owned paths、验收标准、指标清单以它为准）。
> **本会话 episode 记录**：`.mrules.log` E42 条目（规则 1）。
> **一句话状态**：final benchmark 主体（schema/tasks/registry/world/actors/oracle/harness/runner/statistics/aggregation/cli）已全部落地且全量测试零回归（437 passed / 5 skipped / 0 failed）；governance suite 的 3 个 taskvm false_done 已修复；剩余 2 个**预先存在**的任务级失败（loop-inbox-zero、drift-relabel）+ F8–F12 收尾。

---

## 0. 一键开工（环境与常用命令）

```bash
cd /mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui
export PY=/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/conda/envs/taskvm/bin/python

# 统一入口（任务书要求的唯一 runner）
PYTHONPATH=$PWD $PY -m taskvm.evaluation.cli list
PYTHONPATH=$PWD $PY -m taskvm.evaluation.cli run --suite governance --condition taskvm --seeds 1 --budget smoke --run-id gov-postfix --out eval_results/gov-postfix
PYTHONPATH=$PWD $PY -m taskvm.evaluation.cli report --input eval_results/gov-postfix

# 全量回归（当前基线：437 passed / 5 skipped / 0 failed，2026-08-16 复跑确认）
PYTHONPATH=$PWD $PY -m pytest tests/ -q
```

复现两个遗留失败（单任务调试，秒级）：

```python
# /tmp/f6b_debug_failures.py（若 /tmp 被清，按此重建）
from taskvm.benchmark.registry import get_task, Condition
from taskvm.evaluation.runner import run_trial
from taskvm.evaluation.harness import TrialBudget
for task_id in ("loop-inbox-zero", "drift-relabel"):
    rec = run_trial(get_task(task_id), Condition.TASKVM, seed=0,
                    run_id="debug", budget=TrialBudget())
    print(task_id, rec.stop_reason, rec.failure_class, rec.detail[:300])
```

---

## 1. 本会话交付了什么（F1–F7）

### 1.1 Benchmark 层（`taskvm/benchmark/`，均未提交前已在本会话 commit）

| 文件 | 内容 |
|---|---|
| `schema.py` | `TaskSpec` / `Family`(12) / `Split`(5) / `Injection` / `InjectionKind`。**`seed`/`success`/`protected` 是 Evaluation-plane 秘密，runtime 不可见**（权限隔离） |
| `tasks.py` | **15 个任务、12 个结构族、5 个 split**。ID×11（seq-release-sync / fanout-launch / loop-inbox-zero / cross-reschedule / conflict-budget / rollback-pricing / drift-relabel / fanout-partial / send-announce / localpatch-shift / pause-hold），task_holdout（goalpivot-review），operation_holdout（rsvp-confirm），surface_holdout（venues-book），cross_product（venues-rsvp） |
| `registry.py` | 4 个 suite（smoke 3 / final 15 / open-world 4 / governance 7）+ `Condition` 6 条件 + `BUDGET_PRESETS`（smoke/paper） |
| `__init__.py` | final surface 重导出；legacy 模块（fixtures/ood_fixtures 等）标注 owner=Agent G Wave-3 |

### 1.2 Evaluation 层（`taskvm/evaluation/`）

| 文件 | 内容 |
|---|---|
| `world.py` | `BenchmarkWorld`（确定性考场：begin_trial/reset/seed、注入按 after_writes revision 确定触发）+ `WorldSubstrate`（L1 port 适配，k=v 文本世界） |
| `actors.py` | `TemplateCUA`（确定性 CUA fake：跟 compiled variables 走，label 不可见即 fail）+ `TemplateModelPort`（确定性模型 fake；从 goal 文本解析 GoalProgram：sets/repeat/checkpoint/fanout） |
| `oracle.py` | 隐藏判卷：只读 hidden world + `TaskSpec.success` 谓词，oracle 失败只判 evaluation error |
| `harness.py` | **6 个系统条件**：direct-cua / planner-cua / taskvm / taskvm-oracle-upper-bound(⚠诊断) / taskvm-no-verifier / taskvm-no-replan（消融即条件，公平性=同任务同 CUA 同预算只改 harness）+ `TrialBudget` + `WorldExtractor`（世界→ObservedValue+SurfaceEvidence）+ runtime 事件反应器（surface_conflict 级联 retarget，见 §2.3） |
| `runner.py` | `BenchmarkRunner`（矩阵执行 + trial JSON 落盘 + trace）+ `run_trial` |
| `statistics.py` | wilson_ci / percentile / mean / safe_div（置信区间，规则 7 反 max） |
| `aggregation.py` | `classify_failure`（failure taxonomy：observation/compiler/architect/cua/verifier/recovery/budget/environment…）+ `aggregate_trials` + `report_from_trials`（schema `taskvm.evaluation.report/1`）+ `render_paper_tables` |
| `cli.py` | 统一入口 `list/run/report/compare`（验收命令 `python -m taskvm.evaluation.cli --help` 已验证可用） |

### 1.3 F6b：governance suite 3 个 taskvm false_done 全修复

修复前基线（已落盘 `eval_results/gov-test-current/`，21 trials）：direct-cua 0.714 / planner-cua 0.714 / **taskvm 0.571（3 false_done）**，taskvm×task_holdout=0.00。

| 任务 | 根因 | 修复（代码锚点） |
|---|---|---|
| **goalpivot-review**（GOAL_PATCH, task_holdout） | GoalPatch 重组合把已提交的 TERMINAL 节点当历史携带 + 新 future 又生成 terminal → 违反 exactly-one-TERMINAL → recompose 失败 | `taskvm/architect/architect.py::historical_node_ids` 排除 TERMINAL（结构哨兵，不是 committed work；重组合的未来自产新 terminal） |
| **localpatch-shift**（LOCAL_PATCH） | `retarget_action_contracts` 只改 desired_state，不改 `completion_condition`（RFC-003 `key == value`）→ verifier fail-closed → false_done | `taskvm/kernel/workflow_store.py::_retarget_completion`：确定性 LHS 命中→RHS 替换；非 conforming 原样保留 |
| **conflict-budget**（CONFLICT） | 两处：① `WorldExtractor.extract` 用 fallback `handle_id="world"`，evidence 永远匹配不上真实 surface handle → 误判结构漂移 → invalidation 循环；② 冲突 retarget `taskboard_approved_budget` 后，同 desired 的 sibling `mail_budget_note` 未级联 → 陈旧合同永不可满足 | ① evidence 的 surface `handle_id` 必须等于 `obs_surface`，删 fallback；② `harness.py::_on_runtime_event` 的 surface_conflict 分支：对 desired==被冲突变量旧 desired 的其他变量级联 retarget |

修复后验证：smoke 3/3 PASS；ID split 11 任务 **9 PASS** + 2 个预先存在失败（见 §3）。goalpivot/localpatch/conflict 三任务均已过。

---

## 2. 当前验证状态与证据边界（诚实声明）

**已证明**：
- 全量 `pytest tests/`：**437 passed / 5 skipped / 0 failed**（2026-08-16 08:17 复跑，与本会话全部改动共存，零回归；基线=E41）。
- CLI 四个子命令可用；15 任务/4 suite/6 条件注册完整。
- 修复前 gov-test 报告落盘 `eval_results/gov-test-current/`；另有 f6-allconds-v4 / f6-final-smoke（早期 all-conditions smoke）。

**没证明 / 证据缺口（必须补）**：
- ⚠️ **规则 2 缺口**：修复后的 smoke 3/3 与 ID 9/11 数字只存在于 `/tmp/f6b_*` 输出与会话转录，**未落盘 `eval_results/`**。接手后第一件事：用 §0 命令重跑 governance suite + ID split 并 `--out eval_results/…` 落盘。
- tests/benchmark/** 与 tests/evaluation/** **尚不存在**（F10）；任务书"最终测试"6 项全部未做。
- builtin_web substrate 是 registered pending dependency（`--substrate` 只有 world）；cross-substrate split 未覆盖（诚实标注，勿伪造）。

---

## 3. 遗留失败 ×2（预先存在，与本会话修复无关；接手优先处理）

### 3.1 loop-inbox-zero — harness_crash（architect 规划缺陷）

```
stop_reason: harness_crash
detail: ArchitectOutputError: task architect failed after 2 attempt(s);
        last error: workflow node(s) ['n002'] can never reach the TERMINAL
        'n004' (orphan work the plan can never finish)
```

- goal = "Repeat: Set taskboard_sweep_action to sweep until taskboard_unread_count is 0." → GoalProgram：`repeat=(("taskboard_sweep_action","sweep"),("taskboard_unread_count","0"))`，`sets=()`。
- 验证器：`taskvm/domain/architecture.py:108 _check_no_orphan_work`（从 TERMINAL 反向可达性：preds = depends_on ∪ children ∪ parent；容器类型豁免；另有 `exempt_node_ids` 机制）。
- 会话内定位（转录 34860-35130 行）：`taskvm/architect/architect.py::_architect_reply` 生成 bounded_loop（sweep-loop）+ loop-body action（sweep-once, `container=loop_lbl`）+ terminal（`after=[loop_lbl]`）。**疑点**：n002 是 ACTION（容器与 TERMINAL 都被孤儿检查豁免，故 n002 只能是 action）——terminal.depends_on 含 loop 且 loop 的 children 应含 loop-body，按理可达；**需核实 loop-body 的 `container` 键是否真的被映射成 `parent_id`**（若没映射，sweep-once 无 parent 无 after → 孤儿）。n003/n004 的编号也提示图里不止 3 个节点，先打印实际 node 列表再动手。
- 调试脚本：`/tmp/f6b_loop_debug.py`（BenchmarkWorld + StateCompiler + TaskArchitect 直连，打印 compiled variables 与全部 nodes 的 after/parent）。

### 3.2 drift-relabel — no_ready_work（failure_class=recovery）

```
stop_reason: no_ready_work
trace[0]: node_failed — "cua reported fail: cannot see taskboard_owner on screen"
```

- 任务：goal "Set taskboard_owner to bo. Set mail_owner_note to bo."，seed 中 `taskboard_owner=ana`；注入（after_writes=1）把 label `taskboard_owner` 改名 `taskboard_assignee`（`tasks.py:208`）。
- **预期行为**：CUA fail → 指纹变化 → `needs_slow_path` → E36 C-F1 的 `_recovery` 三态（新 label 恰出现一次=recovered → 确定性重绑，0 模型调用）→ 继续推进。
- **实际**：node_failed 后直接 no_ready_work，恢复链路没接上。排查方向：harness 是否把 invalidation/指纹变化送进 compiler 慢路径？`WorldExtractor` 对"旧 label 消失+新 label 出现"的提取是否覆盖？runtime 的 node_failed 分支是否本应触发 re-observe/re-compile（对照 `taskvm/runtime/` 合同与 `architect/compiler.py` 的 handoff 四级阶梯）。
- 判断标准：这是任务设计要考察的 UI-drift 恢复能力，**修好它 taskvm 条件应显著优于 direct-cua**（direct-cua 同样看不到旧 label 就死）；若最终判定为 harness 能力边界，须诚实标注并登记，不许 mock 掉。

---

## 4. 待办清单（按建议顺序）

| # | 事项 | 要点 |
|---|---|---|
| **F6b-loop** | 修 loop-inbox-zero | 见 §3.1；修完 ID split 应 10/11 |
| **F6b-drift** | 修 drift-relabel | 见 §3.2；修完 ID split 应 11/11 |
| **F8** | 删 13 个历史入口 + 清引用 | `taskvm/evaluation/` 下：run_full_loop_killtest / run_mg_vm_killtest / run_mobilegym_killtest / run_open_world_killtest / run_reconciliation_killtest / run_substrate_invariance_killtest / run_w1_killtest / run_w2_killtest / run_w3_killtest / run_x_toggle_killtest / run_four_step_arc / run_interaction_compression / run_model_ablation（后两个思想已并入 conditions+report，确认后删）。**同步改**：`tests/test_imports.py` 里 import 这些脚本的测试函数；grep 全仓引用（含 `taskvm/baselines/` W4 遗留——owned path，决定清空或重导出）；任务书要求所有 `killtest` 字符串从代码/README/pyproject 消失（git history 允许）。**⚠️ rm 纪律：所有 git rm 统一放在会话末尾一次性执行**（中途 rm 会阻塞等确认，memory 规则） |
| **F9** | 指标补齐 | 已有：success/false_done/model calls/GUI actions/rollbacks/compensations/conflicts/pause+actions-after-pause/wilson CI/percentile。**缺**（对照任务书"指标"节）：stale response execution rate（应为 0）、GoalPatch 已完成工作复用率/invalidated node 数、interaction compression（实测 trace）、round-trip projection correctness、field/entity non-interference、同步成本（heartbeat 总数/fast-path 比例/projection staleness）、binding/compiler quality。同时补 `docs/benchmark.md`（owned，尚不存在） |
| **F10** | 新测试目录 | 建 `tests/benchmark/` + `tests/evaluation/`，覆盖任务书"最终测试"6 项：① runtime import graph 无 benchmark/evaluation；② oracle no-leak（captured prompts 不含 hidden state）；③ seed/reset 可复现（同 seed 同 trace）；④ fault injector 不碰 production dispatcher；⑤ aggregation 对失败 trial 不静默丢弃；⑥ killtest 字符串清零（依赖 F8） |
| **F11** | 全量回归 + 落盘 | `pytest tests/ -q` 零回归（当前基线 437/5）；**把修复后 governance + ID split 数字落盘 `eval_results/`（补 §2 的规则 2 缺口）**；提交最小 smoke report + paper-matrix 配置（任务书验收项） |
| **F12** | 规则收尾 | 本会话已写 E42；你的会话完成后写 E43（`.mrules` Episode 表 + `.mrules.log` 详录 + 更新页脚日期），commit 只 add 自己的文件 |

验收（任务书原文）：

```bash
pytest -q tests/benchmark tests/evaluation tests/architecture
python -m taskvm.evaluation.cli --help
```

---

## 5. 关键代码地图

```text
taskvm/benchmark/    schema.py · tasks.py · registry.py        （任务/分类/split/suite）
taskvm/evaluation/   world.py · actors.py · oracle.py          （考场/确定性 actors/判卷）
                     harness.py（6 条件+WorldExtractor+事件反应）· runner.py
                     statistics.py · aggregation.py · cli.py   （统计/报告/入口）
taskvm/kernel/workflow_store.py::_retarget_completion          （本会话修复②）
taskvm/architect/architect.py::historical_node_ids             （本会话修复①，⚠C 冻结域）
taskvm/domain/architecture.py:108 _check_no_orphan_work        （loop 失败的验证器）
architect/compiler.py needs_slow_path/_recovery                （drift-relabel 应走的恢复阶梯）
eval_results/gov-test-current/ · f6-allconds-v4/ · f6-final-smoke/   （已落盘证据，不进 git）
```

## 6. 纪律与风险（接手前必读）

1. **跨域修改需 Oracle 复审**：本会话动了 `architect.py`（Agent C 冻结域）与 `workflow_store.py`（Agent A kernel 域）各一处。全量 437 绿说明与现有冻结测试不冲突，但按分层协议应报 Oracle 备案。
2. **多 agent commit 纪律**：只 `git add` 自己本会话的文件（本会话范围：`taskvm/benchmark/**`、`taskvm/evaluation/**`、上述两个跨域修复、本交接文档、`.mrules`/`.mrules.log`）；绝不 `git add -A`。
3. **`eval_results/`、`*.png` 不进 git**（规则 3）。
4. **rm 纪律**：F8 的删除统一放会话末尾（memory 规则）。
5. **builtin_web / cross-substrate**：registered pending dependency，不许 silent stub 冒充（CLI 已诚实报错）。
6. **harness 是唯一变量**：跑分不许改 runtime 行为、不加 evaluator hooks（任务书 owned-path 约束）。
