# Coding Agent G：最终集成、启动可靠性、文档与历史包袱清理

## 你的唯一任务

在各模块 agent 合并后，完成冲突解决、端到端验收、可移植启动、文档权威收敛和旧阶段产物删除。你不是来添加新研究功能，而是确保最终 prototype 真正可启动、可操作、可演示、可复现。

---

## 启动状态与阅读顺序（2026-08-16，E47–E49 裁决后注入；本节取代原"合并顺序"节）

**原"合并顺序"节已作废**：各波次（A→B→C→E→D→F）已顺序落在 main 分支
（`git log` 可证，无分支合并步骤）。开工前置条件"各模块基本完成"已满足。

各层当前状态：
- **A kernel / C architect / D projection**：达 FORMAL LOCK 条件（E47 裁决，
  exact SHA `2c85379`），零在途义务；
- **E runtime**：T1 删除义务已移交你（见下文⭐节），现代面冻结；
- **B substrate**：OWNER-COMPLETE / CODE-FROZEN，FORMAL LOCK 仅等你的
  Wave-3 簇删清空 §8 登记表（机械盖章，B 无新工作）；
- **F benchmark/evaluation**：主体完成（E45），尚未审计，可与你的 wave
  并行（约束见⭐节末）。

阅读顺序：
1. `.mrules` —— 现行仓库纪律（多 agent 提交纪律 / 证据落盘 eval_results /
   开发环境路径）。**你的工作全程适用**；其中"精简替换 .mrules"（见本文
   权威文档清理节）是你的**收尾**步骤，不是开工第一步。
2. 本文件 —— 你的完整 mandate；⭐ 与 ⭐⭐ 两节最高优先。
3. `docs/contracts/`：`substrate.md`（§8 债务登记表——你要把它清空）、
   `audit_charter.md`（冻结/审计语义）、`runtime.md`、`projection.md`。
4. `docs/A2UI_开工大纲_v0_心智模型对齐版.md` —— 心智模型总纲。
5. `11_CURRENT_REPOSITORY_AUDIT.md` —— **仅作历史背景**：那是波次启动前
   的审计快照（页首自证"B/C/D/E 尚未启动"），其缺陷清单绝大多数已由
   E19–E46 修复；禁止当作当前缺陷清单重修一遍。

---

## Owned paths

```text
README.md
run.sh / scripts/**
pyproject.toml
docker-compose.yml
.mrules 或 AGENTS.md
docs/** 中权威与运行文档
tests/integration/**
tests/smoke/**
configs/**
```

可为集成修复最小修改各模块，但必须通知对应 owner；不重新打破边界。

**E49 用户指令追加（2026-08-16）**：全仓目录结构可依 prototype/bench 分离原则调整
（含 `taskvm/` 顶层与 `tests/`），见下文⭐⭐节；调整不得改变冻层内容语义
（保冻结合同的迁移约束同节）。

---

## ⭐ T1 移交（E→G）与 Wave-3 一体化删除（E47 裁决注入，2026-08-16，最高优先）

> 依据：E47 A–E 冻结裁决审计（exact SHA `2c85379`，证据
> `eval_results/freeze_audit_2c85379.json`；ledger `.mrules` E47 行）。
> `substrate.md` §8 的 T1 owner 已显式由 E 改为 G：不是因为 E 未完成，
> 而是 T1 的物理删除与你的 Wave-3 在结构上是同一批多米诺骨牌。

### 结构性交叉（为什么 T1 必须并入你的 Wave-3）

`taskvm/execution/gui_driver.py` 在生产代码仍有**三处活 import 宿主**，
而这三个宿主全部位于你的 Wave-3 legacy 删除范围：

| 宿主（gui_driver import 点） | 下游消费者（删宿主即断这些链） |
|---|---|
| `taskvm/governance/vm_state.py:45` | `governance/__init__.py`、`tests/fakes/*`（5 个）、`tests/test_imports.py` |
| `taskvm/execution/action_dispatcher.py:28` | `execution/workflow_executor.py`、`workspace_ui/server.py` |
| `taskvm/execution/rollback.py:40` | `workspace_ui/server.py`、`verifier/rollback_verify.py`、`tests/fakes/scripted_driver.py`、`tests/test_imports.py` |

交叉证据：`vm_state.py` 与 `workspace_ui/server.py` 均已列入 F 的
`LEGACY_BENCHMARK_IMPORTERS` 债务表（`tests/evaluation/test_final_contract.py`），
表内注释明写 "pending Agent G's deletion wave"。

**⚠️ 禁令：不得单独删除 gui_driver.py。** 孤立删除将连锁 ImportError
断裂上表三条链，把失败归因搅进你的 wave。

### 删除编排（顺序敏感）

前置两棒已完成（E47 实证）：
- 第一棒（D，E46）：`workspace_ui/server.py` 已切离 gui_driver，生产组合根
  走 `composition.py:251 → runtime/bootstrap.compose_runtime`；legacy 写路由
  已 409 退役。
- 第二棒（F，E45）：引用 gui_driver 的 killtest 脚本已删除；`taskvm/` 与
  `tests/` 之外零 gui_driver 引用（grep 实证）。

你的第三棒（一体化，一次提交簇内完成）：
1. 以 dead-code/import scan 为准，按依赖逆序删除整个 legacy execution /
   governance 簇：`gui_driver.py`、`vm_state.py`、`action_dispatcher.py`、
   `rollback.py`（及随行 `workflow_executor.py`、`gui_executor*.py` 等 scan
   判死文件）；同步拆除 `governance/__init__.py`、`verifier/rollback_verify.py`、
   `workspace_ui/server.py` 的对应 import，清掉 `tests/fakes/*` 与
   `tests/test_imports.py` 中的 gui_driver smoke 测试；
2. 同步收缩两张 shrink-only 债务表：
   - `tests/substrate/test_no_api_backdoor.py` → `TRANSITIONAL_DEBT_REGISTER = {}`；
   - `tests/evaluation/test_final_contract.py` → `LEGACY_BENCHMARK_IMPORTERS`
     移除已删条目（F 合同明确允许 "update ONLY on deletion"）；
3. `TASKVM_SUBSTRATE_LOCK_AUDIT=1 pytest tests/substrate -q` 必须全绿；
4. 全绿即达成 B 的 FORMAL LOCK 机械条件（登记表空），通知 Oracle 盖章。

### 与 F 审计并行的约束

F 尚未审计。允许 F 审计与你并行：F 的证据锚定 exact SHA（E38 协议），
你在其后提交不使 F 在锚定 SHA 上的证据失效。但：除上述
`test_final_contract.py` 的 shrink-only 编辑外，不得改动 F 拥有的
benchmark/evaluation 内部；README/pyproject 你会重写（F 在 E45 刚清理过），
属预期内的顺序接力，不是冲突。

---

## ⭐⭐ 目录终局：prototype 与 bench 物理分离（用户指令，E49 注入）

> 用户心智模型（原话裁定）：TaskVM 是“枪”，substrate（builtin_web 自建 app +
> MobileGym）是“模拟靶场”——用户在靶场里验证枪是否好用，因此 substrate 与
> `apps/`、`thirdparty/` 都属于 prototype；benchmark/evaluation/baselines 是
> 论文计量仪器，与用户试验无关。**用户试验的代码路径不得离开 prototype。**
> （“prototype/infra”理解为“prototype 及其配套基础设施”：组合根、dev.sh、
> configs、apps、thirdparty 均入 prototype 侧。）

### 终态布局

```text
taskvm/         ← prototype（枪 + 靶场；用户试验不离开此包）
  domain/ kernel/ architect/                # 冻结现代平面（A/C）
  governance/service  runtime/  projection/ # 现代治理/运行时/投影（C/E/D）
  substrate/  apps/  thirdparty/            # 靶场（B 层 + 自建 app + MobileGym）
  workspace_ui/composition.py               # 生产组合根（或随迁 runtime/ 旁）
taskvm_bench/   ← 计量（论文测量仪；名字可自定：greppable、单词、taskvm 之外）
  benchmark/ evaluation/ baselines/         # 三包原样迁入，内部结构不动
tests/          ← prototype 合同锁 + integration/smoke；bench 的 tests 随 bench 迁走
```

### 为什么这是“迁移”不是“重构”（已具备的结构事实）

- F 的五包导入图合同已证：现代 runtime 平面零 benchmark/evaluation import；
  E49 复核（grep 全仓）：prototype 侧导入 benchmark 的文件**恰为**
  LEGACY_BENCHMARK_IMPORTERS 的 11 个 legacy 文件，现代五包零命中；
- demo / user-study 路径（dev.sh → projection → composition → runtime →
  substrate/apps）不需要 bench 包。

文件系统只需追认导入面已经成立的事实。

### 执行约束（顺序敏感）

1. **先 Wave-3 簇删、后目录迁移**：11 个 legacy importer 大半是删除对象；
   先删后搬则 import 改写只剩 bench 自身平面与其 tests（给将死文件改
   import 是浪费，且制造审计 diff 噪音）。
2. **prototype 保留 `taskvm` 包名**：五包互 import 全是 `taskvm.*`，保留名字
   = 冻结层（A/C/D 内容、B 全层）零字节改动；git blob 内容寻址不受路径影响，
   E38 式 exact-SHA 证据链不被破坏。
3. **bench 迁移是纯机械动作**：`taskvm.(benchmark|evaluation|baselines)`
   前缀改写到新顶层；同步 pyproject packages/entry-points、
   configs/paper_matrix.json、docs/benchmark.md、Final Gates 的
   `python -m taskvm.evaluation.cli` 调用、tests/benchmark +
   tests/evaluation 随迁。
4. **substrate/ 内 evaluation.py 本 wave 不动**（B 层 CODE-FROZEN；oracle/seed
   评测面原地保留；动它属 post-lock 可选清理，需 RFC）。
5. **等价性验收**：迁移前后全量套件数字逐位一致 + compileall 过；迁移 commit
   不得混入任何“顺手改进”（纯 move + 机械 import 改写，一 commit 一性质）。

### tests/ 处置边界（用户问询裁定，防误删）

用户问“tests 是否都是中间阶段产物、prototype 建好即可全删”——裁定：**不可全删**。

- tests/ 主体是**最终系统的合同锁**（kernel 84 / substrate 34 / architect 16 /
  projection 127 / runtime 37 / 五包导入图 / no-leak / 债务登记表 / LOCK 审计
  本身 / e2e 真浏览器 29）。删除它们 = 冻结合同失去执法器；user study 期间的
  任何修复都可能无声破坏 GUI-only / 零内部暴露 / 回滚诚实。
- 随代码消亡的仅限**测 legacy 的部分**：fakes 对 vm_state/rollback 的引用、
  test_imports 的 gui_driver smoke——它们随 Wave-3 簇删一并删除。
- Smoke Journey / 前端 crawler 属**新增**（tests/integration、tests/smoke）。
- 比方：tests 是靶场的校枪规与质检章，不是脚手架——枪造好后每次改装仍要过规。

---

## 权威文档清理

### 替换心智模型

将 `01_MENTAL_MODEL_ALIGNMENT_REPLACEMENT.md` 的正文替换：

```text
docs/A2UI_开工大纲_v0_心智模型对齐版.md
```

保留用户要求的结果性原则，不加入当前代码字段、接口或临时实现。

### 删除阶段/历史 handoff

完成信息迁移后删除：

```text
docs/A2UI_EE阶段开工目标.md
docs/A2UI_GG阶段开工目标.md
docs/DEMO_RUNBOOK_MobileGym.md
# docs/HANDOFF_E17.md  (already deleted — no longer in tree)
docs/HANDOFF_TaskVM.md
```

若其中有仍然必要的启动说明，只迁移最终事实到一个短 `docs/RUNBOOK.md`，不保留阶段叙事。

### 重写 `.mrules`

当前 `.mrules` 是上千行“心智模型演化史”，又规定所有 agent 必读并持续追加，已经成为上下文污染源。

把它替换为不超过约 150 行的最终 repository contract，内容仅包括：

- 五个结果性原则；
- 六层依赖方向；
- runtime/evaluation 权限隔离；
- GUI-only、无 hidden IDs；
- owned paths/不要越层；
- 测试与诚实汇报要求；
- 不再追加 episode、phase、gate、killtest 历史。

也可改为根目录 `AGENTS.md`，但必须确认本地 coding agent 会读取哪个文件；不要同时保留两个互相冲突的权威规则。

### README/pyproject

删除：

- `version = "0.1.0-w1"`；
- `W1 kill-test` 描述；
- read GUI/write API；
- API executor 用法；
- hidden canonical 作为 runtime 能力；
- 阶段性 gate 数字。

README 只写：概念、最终架构、快速启动、用户 workflow、final benchmark、局限。

---

## 可移植启动

当前 `run.sh` 硬编码个人 conda、仓库和 browser/lib 绝对路径，并以 Flask `--debug` 启动。重写为：

- 从脚本自身解析 repo root；
- 使用当前激活 Python 或可配置 `TASKVM_PYTHON`；
- 依赖由 `pyproject.toml`/安装命令管理；
- Playwright 路径由标准安装发现；
- 无 `/mnt/dolphinfs/...`；
- 默认不启用 debug reloader；
- 每个服务写 PID/log，并可优雅 stop；
- 启动前检查端口，不粗暴 kill 所有进程；
- health check 等待服务可用；
- 失败时输出具体服务与日志路径；
- 提供 `scripts/dev.sh` 和 `scripts/stop.sh`，必要时提供 Docker Compose。

最终单命令应能：

```bash
./scripts/dev.sh
```

启动 built-in environment、TaskVM server 和所需 worker。

---

## 完整 Smoke Journey

自动化集成测试必须覆盖：

1. 服务启动；
2. 用户输入任意 goal，而不是 task_id；
3. 初始 visible observation；
4. State Compiler + Task Architect 形成 task projection；
5. 前端显示 workflow；
6. start 后 CUA 自主推进；
7. surface card 显示实时已有截图；
8. fan-out/fan-in 达到 verified checkpoint；
9. 用户在运行中 LocalPatch；
10. 用户在 CUA response 在途时 GoalPatch，旧 response 不执行；
11. inactive surface 外部变化产生 conflict；
12. rollback 经 GUI compensation；
13. 不可逆动作诚实显示；
14. event stream 断线重连；
15. 全程无 405/500/browser console error。

如果真实模型不稳定，提供 deterministic fake model 的 CI smoke 和真实模型的手动 E2E；不得用 fake 的成功声称真实 CUA 已通过。

---

## 前端专项验收

用自动 crawler 收集页面中的：

- form action；
- fetch URL；
- button command；
- SSE endpoint；
- screenshot URL。

逐一发请求验证不出现 405/500。尤其验证旧问题中的：edit、undo、checkpoint、adopt/goal patch、resolve、start、pause、rollback。

检查：

- CSS/JS 文件 200；
- 无重复 debug server；
- screenshot artifact MIME 正确；
- SSE reconnect 不重复执行 command；
- session URL 编码正确。

---

## 清理旧代码

合并后删除已无引用的：

- `taskvm/_shim/`；
- `harness/state_adapter.py`；
- **`taskvm/execution/gui_driver.py`（T1，见上文⭐节——必须与三宿主同簇删除，禁止单删）；**
- **`taskvm/governance/vm_state.py`、`taskvm/execution/action_dispatcher.py`、`taskvm/execution/rollback.py`（gui_driver 三处活 import 宿主，随簇删除）；**
- replay/scripted driver 中间实现；
- legacy `workspace_ui/renderer.py`；
- static f-string editable fallback；
- old API adapter branches；
- `--no-genui`、`--executor api`；
- `replanner.py` stub；
- 所有 phase/killtest 注释与用户可见文案；
- 未使用的 duplicated mobilegym bridges。

### 路由债务：`taskvm/__init__.py` 陈旧文案

`taskvm/__init__.py` 承载与 v5 不一致的陈旧 docstring 与版本号（非 A、非阻塞；owner = G，本 wave 清理）：

- 删除 docstring 中 `Verifier always reads hidden canonical sandbox state` 与 `W1 = kill-test ...` 句子（hidden canonical 已非 runtime 能力；W1 不是当前阶段）；
- 四锚描述与 `__version__ = "0.1.0-w1"` 一并更新到 v5 baseline；版本号 bump 至与 `pyproject.toml` 一致的当前版本。

权威来源以 `docs/contracts/*.md` + 当前 handoffs 为准；该文件陈旧文案仅为路由债务，不回滚 A、不为假想 hostile caller 加校验（audit_charter §3-4）。

运行 dead-code/import scan，不要因为“可能以后有用”保留两个真源。

---

## Final Gates

```bash
pytest -q
python -m compileall taskvm
python -m taskvm.evaluation.cli --help
./scripts/dev.sh
```

再运行：

- architecture import gate；
- API/hidden-ID backdoor scan；
- route/method crawler；
- UI Playwright smoke；
- deterministic full-loop；
- 至少一个真实 GUI/CUA end-to-end；
- git grep 确认不存在 `killtest`、`W1`、`executor=api`、`read_canonical` production path。

---

## 发布报告

最终报告必须包含：

- 最终目录树；
- 一张主架构图；
- 一条完整 runtime trace；
- 一份所有 route 的测试表；
- 一份模型调用账本；
- 一份 final benchmark smoke report；
- 已知局限（尤其真实模型、OSWorld、不可逆动作）；
- 从干净环境启动的复现步骤。

不要再产出新的阶段性“开工目标”文档。后续 issue 进入正常 issue tracker，而不是继续扩写心智模型或 `.mrules` 历史。
