# TaskVM 最终重构总交接：Award-grade Prototype + Benchmark + User Frontend

> **适用分支**：`gg-phase` 当前代码基础上创建新的重构分支。  
> **适用对象**：审计 Agent、规划 Agent、各模块 Coding Agent、最终集成 Agent。  
> **唯一目标**：交付最终可运行的 TaskVM prototype、最终 benchmark 和稳定用户前端。  
> **不再保留**：W1/W2/W3/GG/EE 等阶段叙事、kill-test 产物、API executor、hidden-ID runtime、fixture 驱动的生产工作流、临时 fallback UI。
> **分层协议（冻结，2026-08-14）**：见 [docs/contracts/layered_ownership_protocol.md](../../contracts/layered_ownership_protocol.md) —— **内容合法性由生产者负责；时序合法性由 Kernel 负责。一个性质只有一个 owner，不在下一层重证。** 各 Agent 产出的对象必须在**构造时**自我合法，Kernel 不替下游重查内容。审计不得再以"假设下游 hostile"为由扩 Kernel 防御面积（treadmill 闸门）。

---

## 1. 为什么必须重构，而不是继续补 Bug

当前仓库已经包含 binding、GenUI、workflow、CUA、rollback、reconciliation、benchmark 等大量组件，但它们横向交叉、职责重叠：

- `taskvm/workspace_ui/server.py` 同时承担路由、HTML、模型调用、session、workflow、SSE、执行、回退与冲突处理，是典型 god module。
- `taskvm/substrate/base.py` 同时承担 app-specific API 读写、GUI 写入、状态归一化、adapter registry；`taskvm/harness/state_adapter.py` 又作为兼容 shim 被多处引用。
- `taskvm/task_state/compiler.py` 反向依赖 `benchmark`、`harness` 和 model client，领域层不再纯净。
- `taskvm/governance/` 混入 scripted driver、UI simulator、模型 subgoal writer 和真实用户治理。
- `taskvm/execution/action_dispatcher.py` 仍带 no-op / wrong-target 等 kill-test 分支。
- `taskvm/workspace_ui/live_sync.py` 通过 `read_canonical()` 读取内部 API，生产投影与实验 oracle 没有隔离。
- 生产 CLI 仍有 `--executor api`、`--no-genui`，README、`pyproject.toml`、`.mrules` 和 docs 仍反复要求 W1/kill-test/hidden canonical 等历史实现。
- 前端多个表单使用相对地址，例如 `action="edit"`、`action="undo/..."`。当前页面地址是 `/<sid>` 且无尾斜杠，浏览器会把 `edit` 解析成 `/edit`；POST 命中只支持 GET 的 `/<sid>` 动态路由后直接产生 `405 Method Not Allowed`。这不是随机 Flask 报错，而是明确的 URL 生成错误。

继续逐点修复会不断制造新的局部补丁；必须先冻结层级、接口和目录所有权。

---

## 2. 唯一主架构图

后续论文叙事、README、代码目录和前端心智模型均使用下面这一张图。初始化、自治、用户介入、漂移和回退不是五套架构，而是同一层级栈上的不同事件路径。

```text
┌────────────────────────────────────────────────────────────┐
│ L5  Human Governance & Projection UI                       │
│     用户目标、可操作 GenUI、进度拓扑、checkpoint、干预      │
├────────────────────────────────────────────────────────────┤
│ L4  Task Architect                                          │
│     可见世界 → 任务抽象；目标 → milestone / workflow / UI结构│
├────────────────────────────────────────────────────────────┤
│ L3  TaskVM Kernel                                           │
│     Task State、Projection Store、Workflow Store、Event Log │
│     Local / Goal / Compensation Patch、Checkpoint           │
├────────────────────────────────────────────────────────────┤
│ L2  Autonomy Runtime                                        │
│     调度 → CUA observe/act → verify → commit/recover         │
│     execution epoch、预算、热中断、同步                      │
├────────────────────────────────────────────────────────────┤
│ L1  Substrate Port                                          │
│     统一 observe / act / capture 接口                        │
│     ├─ builtin-web  ├─ MobileGym  └─ OSWorld                │
└────────────────────────────────────────────────────────────┘

      ┌──────────────── Evaluation Plane ─────────────────┐
      │ reset / seed / hidden oracle / metrics / baselines│
      │ 只布置考场和判卷；永远不能进入 runtime 决策链       │
      └───────────────────────────────────────────────────┘
```

### 一句话职责

- **L5** 决定“人看到什么、能够治理什么”。
- **L4** 决定“任务应该被组织成什么结构”。
- **L3** 保存“TaskVM 当前相信的任务世界是什么”。
- **L2** 决定“下一步让 CUA 做什么，以及结果是否可提交”。
- **L1** 只解决“在这一种设备/平台上怎么观察和怎么动作”。
- **Evaluation Plane** 只负责实验环境重置和客观判卷。

---

## 3. 模块隔离硬规则

### 3.1 依赖方向只能向下

建议最终目录：

```text
taskvm/
  domain/          # 纯数据与不变量，无 Flask/Playwright/model/benchmark
  kernel/          # stores、event log、patch/checkpoint 状态机
  architect/       # state compiler + task architect + model ports
  runtime/         # autonomy loop、scheduler、epoch、sync、verification
  projection/      # Flask/API/SSE、templates、static、view models
  substrate/       # ports + builtin_web/mobilegym/osworld 实现
  evaluation/      # 最终实验 runner、oracle、metrics
  benchmark/       # task specs/generation/splits；不被 runtime import
  environments/    # 自建测试 App 及启动器，可选
```

允许依赖：

```text
projection → architect/kernel/runtime 的公开接口
architect  → domain + model port
runtime    → domain/kernel + substrate port
kernel     → domain
evaluation → 可以调用全部公开接口
```

禁止依赖：

```text
runtime/projection/architect/kernel → benchmark 或 evaluation
projection/runtime/kernel          → 某个具体 substrate 实现
substrate                           → projection/governance/server
kernel/domain                       → Flask/Playwright/OpenAI/requests
```

### 3.2 Substrate 完全透明

除 `taskvm/substrate/` 与启动配置外，任何文件不得出现：

- `if substrate == "mobilegym"`
- `if app == "calendar"`
- `MobileGymBridge` / `BrowserController` / OSWorld 特有类型
- app-specific URL、DOM selector、内部 API、数据库 ID

外部只得到一个统一的 `SubstrateSession`/port，平台差异全部在对应子目录封装。

### 3.3 Runtime 与 Evaluation 权限隔离

Runtime 只能使用一个真人在相同设备上可以获得的观察与动作能力：截图、可见文本、允许的 accessibility/DOM 观察、真实鼠标键盘/触摸动作。

Evaluation 可以使用 hidden state 来：

- reset；
- seed；
- 读取最终 ground truth；
- 判定 success/non-interference/rollback fidelity。

但 hidden oracle 对象不得被注入 runtime、模型 prompt、projection store 或 rollback 逻辑。

### 3.4 不再有运行时 fallback

最终 prototype 只保留一条真实主链：

- GUI/CUA-only actuator；
- Agentic GenUI 始终开启；
- goal + visible observations 初始化；
- persistent Projection Store；
- verifier-gated commit；
- 无 `_gt_binding()`、无 scripted driver、无 API executor、无 static f-string 可写区 fallback。

测试替身只能存在于 `tests/fakes/`，不得通过生产 CLI flag 启用。

---

## 4. 同一层级栈在不同时间节点如何运行

| 场景 | L5 UI | L4 Architect | L3 Kernel | L2 Runtime | L1 Substrate |
|---|---|---|---|---|---|
| 初始化 | 接收自然语言目标，随后显示投影 | 运行 state compile + task architecture | 创建 state/workflow/schema/event log | 尚未执行或等待 start | 捕获初始可见观察 |
| 用户不介入、CUA 自治 | 持续显示高层进度和增量状态 | 通常不调用 | 持续接收 observation/action/verification 事件 | 主动连续推进 ready node | CUA 不断观察并执行真实动作 |
| Local Patch | 局部控件/约束被修改 | 通常不调用 | 修改现有节点目标，不改终结条件与拓扑 | epoch 更新后继续受影响节点 | 执行新的局部目标 |
| Goal Patch | 用户改终点、范围或约束 | 只重构受影响未来 | 保留已 commit 历史，替换未完成子图 | 丢弃旧 epoch 的返回，稳定后继续 | 从当前真实状态继续 |
| Compensation | 用户回退 checkpoint | 不调用 | 查补偿记录并创建逆向意图 | CUA 经同一真实 GUI 路径执行补偿 | 不允许快照/内部 API 恢复 |
| 结构漂移 | UI 只显示必要警告 | 仅在 binding/结构失效时增量调用 | invalidate 受影响 handle/node | 正常 action observation 是主同步；heartbeat 补充 inactive surface | 快速指纹比较，必要时重新观察 |

关键语义：**用户不操作时不是系统空闲，而是 governance 边界内的 autonomy 正在最大幅度推进。**

---

## 5. 三类 Patch 的最终边界

- **LocalPatch**：改变局部执行目标，但不改变 terminal success predicate、任务范围和 workflow topology。通常不重新调用 Task Architect。
- **GoalPatch**：改变终点、范围、约束、milestone 或 fan-out/loop 结构。必须提升 execution epoch，并只重构尚未提交的未来。
- **CompensationPatch**：要求回到历史 checkpoint。由真实已观察的 before/after 与补偿意图驱动，不重新“发明”目标。

旧 `PatchOp(app, entity_id, field, operator, value)` 不再作为跨层核心协议；底层可有平台内部 locator，但跨层协议必须是 substrate-neutral semantic action contract。

---

## 6. 最终只保留的模型角色

1. **State Compiler**：初始观察或结构失效时，把可见世界抽象为任务相关状态与可复用 surface handles。
2. **Task Architect / Projection Composer**：一次产生 milestone、sequence/fan-out/bounded-loop topology、projection schema 和 action contracts。原独立 milestone suggester、rule planner、GenUI decoder 的结构设计职责在此合并。
3. **CUA**：在具体 substrate 上根据 action contract 和当前观察逐动作执行。

删除独立的 model-based SubgoalGenerator。Action contract 到 CUA instruction 使用确定性序列化；不为每个 patch 额外生成两个自然语言候选。

数值变化只更新 Projection Store，**不重新调用 GenUI/Task Architect**。只有 GoalPatch 或不可局部恢复的结构失效才重新 composition。

---

## 7. Agent 分工、所有权与执行顺序

### Wave 0：先消除错误权威来源

- 使用 `01_MENTAL_MODEL_ALIGNMENT_REPLACEMENT.md` 替换当前心智模型文档。
- 集成 Agent 最终用 `08_INTEGRATION_RELEASE_CLEANUP_AGENT.md` 清理 `.mrules`、旧 handoff、phase docs、README 与启动脚本。

### Wave 1：冻结契约

1. **Agent A：`02_LAYERED_KERNEL_REFACTOR_AGENT.md`**  
   拥有 `domain/`、`kernel/`、依赖 gate 和目录骨架。先合并。
2. 对外发布接口冻结后，以下任务可在独立 worktree 并行：
   - **Agent B：`03_SUBSTRATE_ISOLATION_AGENT.md`**
   - **Agent C：`04_TASK_ARCHITECT_GOVERNANCE_AGENT.md`**
   - **Agent D：`05_PROJECTION_FRONTEND_AGENT.md`**
   - **Agent E：`06_CUA_EXECUTION_SYNC_ROLLBACK_AGENT.md`**

### Wave 2：客观评估

- **Agent F：`07_FINAL_BENCHMARK_AGENT.md`**，在公开接口稳定后执行。

### Wave 3：集成与发布

- **Agent G：`08_INTEGRATION_RELEASE_CLEANUP_AGENT.md`**，按固定顺序合并并运行最终验收。

### Optional Bonus

- **Agent H：`09_CROSS_DEVICE_BONUS_AGENT.md`**，核心端到端通过后再开始，绝不阻塞主论文 prototype。

---

## 8. 跨 Agent 协作规则

每个 agent：

1. 只修改自己文档列出的 owned paths。
2. 需要新跨层接口时，先在 `docs/contracts/` 提交一页 RFC，不直接越界改别人的目录。
3. 不导入旧 compatibility shim 来“先跑起来”。
4. 不保留 TODO、NotImplemented stub 或双实现 fallback 作为交付。
5. 每个 commit 聚焦一个可验收单元，并附：改动、测试命令、实际结果、剩余风险。
6. 不把模型/浏览器不可用当作跳过测试的理由：至少提供 deterministic contract tests；真实 E2E 由集成 Agent统一跑。
7. 禁止通过弱化判据、读取 fixture 答案、内部 API 或 snapshot restore 让测试通过。

---

## 9. 最终 Definition of Done

### 用户前端

- 任意按钮不再出现 405/500；路由和方法有自动化测试。
- 默认只展示跨 App 高层进度；点击 App 卡片立即显示 Runtime Store 中最新截图，不触发模型、不重新执行任务。
- milestone/checkpoint workflow 动态可视化 sequence、fan-out/fan-in、bounded loop 和当前节点。
- LocalPatch、GoalPatch、pause/resume、rollback、conflict resolution 均有明确状态反馈。
- 不可逆动作在 UI 中可见地锁定并解释，不只在后端字段中存在。

### Runtime

- 从自然语言 goal + visible observations 启动，不接受 fixture `task_id` 作为生产入口。
- Runtime 代码中无 app mutation API、无 hidden database entity ID 依赖、无 `_gt_binding()`。
- 用户不操作时 CUA 能持续自治推进。
- 热中断在 action boundary 生效；旧 epoch CUA response 被丢弃。
- 可逆动作通过真实 GUI compensation；不可逆动作诚实失败。
- 普通值更新不触发 Task Architect/GenUI 模型调用。

### 架构

- 平台差异只在 `substrate/<name>/`。
- Runtime 不 import `benchmark`/`evaluation`。
- `server.py` 不再是 god module。
- 依赖规则有自动 gate。

### Benchmark

- 只有一套 final benchmark CLI/report schema，不再有 `run_*killtest.py`。
- 主要比较同一 CUA 下 Direct CUA、Planner+CUA 与 TaskVM harness。
- 报告 task success、non-interference、cost、latency、recovery、goal-change、rollback、OOD splits。
- Runtime 与 oracle 权限在代码和进程边界上隔离。

---

## 10. 交付纪律

任何 agent 完成后，不要只说“已实现”。必须给出：

- 改动文件列表；
- 删除文件列表；
- 新公开接口；
- 测试命令与真实通过结果；
- 一段 end-to-end 行为说明；
- 仍未覆盖的风险；
- `git diff --stat`。

这次重构的判断标准不是“旧 demo 还能不能勉强启动”，而是：**新的层级边界是否让每一个部件只做一件事，并让 TaskVM 的 VM moment 在真实运行和用户界面中直接涌现。**
