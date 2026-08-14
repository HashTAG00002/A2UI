# 当前仓库结构化审计矩阵（main 分支；Agent A v5 已合并；审计方法论见 docs/contracts/audit_charter.md）

> 审计对象：`main` 分支。Agent A v5 Kernel/domain 瘦身 + rollback closure 已合并；B/C/D/E 尚未启动，故本页缺陷清单对 02–09 owner 仍为待办 backlog。本文缺陷路由仍有效；审计方法论（一个性质一个 owner、不跨层重证）以 docs/contracts/audit_charter.md 为冻结权威。

---

## 1. 总体结论

当前代码不是“缺少几个 Bug 修复”，而是存在四种系统性漂移：

1. **权威文档漂移**：心智模型、`.mrules`、README、pyproject、phase handoff 同时充当架构权威，且互相矛盾。
2. **权限面漂移**：runtime、测试环境和 oracle 权限混在 adapter/canonical state 中。
3. **层级漂移**：UI、planner、kernel、executor、substrate 彼此直接引用具体实现。
4. **阶段产物漂移**：kill-test、mock/fallback、wrong-target/no-op 等阶段性机制进入生产路径。

因此正确策略是“冻结层级契约 → 按所有权迁移 → 删除旧真源”，而不是继续给旧 `server.py` 和 `StateAdapter` 增加分支。

---

## 2. 权威文档与 Agent 上下文

| 当前文件 | 审计发现 | 风险 | 处理 |
|---|---|---|---|
| `docs/A2UI_开工大纲_v0_心智模型对齐版.md` | 约 500+ 行；既含五性质，又含 SSE、entity ID、operator、exact thresholds、kill-test、字段与路由等实现细节 | 新 agent 将历史实现误认成不可改变的研究原则 | 用 `01_MENTAL_MODEL_ALIGNMENT_REPLACEMENT.md` 整体替换 |
| `.mrules` | 约 1900+ 行演化史；开头要求所有 agent 必读、持续追加 episode、按小阶段 gate/commit | 极长上下文强化旧 W1–GG 目标，是“隔靴搔痒”的主要诱因之一 | 用 `10_AGENT_RULES_REPLACEMENT.md` 精简替换或改为单一 `AGENTS.md` |
| `README.md` | 仍描述 W1 kill-test、读 GUI/写 API、hidden canonical 等旧主链 | 用户、agent 和 reviewer 都会获得错误心智模型 | 最终只保留最终架构、启动、使用、benchmark |
| `pyproject.toml` | `version = 0.1.0-w1`，description 包含 W1 kill-test，注释硬编码个人 conda 路径 | 包元数据仍把项目定义为中间阶段 | 改为正常版本与最终项目描述 |
| `docs/A2UI_EE阶段开工目标.md`、`A2UI_GG阶段开工目标.md`、旧 HANDOFF | 继续作为第二、第三套任务权威 | agent 会挑一份最方便的旧实现执行 | 信息迁移后删除 |

---

## 3. Workspace / Projection 前端

### 3.1 `taskvm/workspace_ui/server.py`

当前约 1300 行，同时包含：

- Flask app 与全部 route；
- `WorkspaceSession` 内存状态；
- HTML template；
- GenUI model invocation；
- fixture/task-id seed；
- adapter/substrate selection；
- workflow construction；
- executor thread；
- pause/rollback/conflict；
- SSE poll；
- CLI flags。

**问题**：任何 UI 修复都可能影响模型、执行和状态；任何 substrate 改动都需要改 server。这与模块透明原则相反。

**Owner**：Projection Agent 拆页面/route/event stream；其他业务逻辑分别迁入 Architect/Kernel/Runtime/Substrate。

### 3.2 405 的确定根因

当前：

- `workspace_ui/editable_components.py` 使用 `action="edit"`、`action="undo/{app}"`、`action="checkpoint"`、`action="adopt_milestone"`、`action="resolve"`。
- `server.py` fallback 表单也使用 `action="edit"`。
- Session 页面 route 是 `GET /<sid>`，URL 无尾斜杠。

浏览器对相对路径 `edit` 的标准解析会把 `/<sid>` 的最后一段替换为 `/edit`，而不是追加成 `/<sid>/edit`。`POST /edit` 随后匹配 GET-only `/<sid>`，因此 Flask 返回 405。

这必须通过统一 `url_for`/JSON API 与 route contract tests 根治，不能只给一个按钮改绝对路径。

### 3.3 GenUI 重渲染

- `render_two_zone_html()` 在 `use_genui` 时调用 `_genui_rw_zone_html()`。
- decoder 在页面 render 流程中调用模型。
- `WorkspaceSession.use_genui` dataclass 默认仍为 `False`，CLI 又提供 `--no-genui`。
- GenUI 输出后还有 f-string editable fallback。

**目标**：Projection Schema 持久化；Projection Data 增量更新；普通值/进度/screenshot 更新不调用模型；删除 production fallback。

### 3.4 Prompt 泄漏

`workspace_ui/genui_decoder.py` 的输入构造包含：

- `var_id`；
- `app.entity_id.field`；
- operator；
- dependency 中的 internal entity identifiers。

这与“模型只看用户可见语义”的最终标准冲突。

### 3.5 前端缺失能力

当前静态资源存在 timeline/workflow animation CSS/JS，但主链没有形成稳定的 workflow view model 和 screenshot artifact store；页面依赖重渲染与零散 SSE event，导致视觉组件不等于真实可用功能。

必须补：

- persistent workflow view model；
- sequence/fan-out/barrier/loop 动态图；
- surface cards；
- latest screenshot artifact endpoint；
- 点击只读缓存截图，不触发执行；
- SSE revision/reconnect；
- console/route E2E tests。

---

## 4. Task State / Kernel

### `taskvm/task_state/entity_binding.py`

当前 `EntityBinding` 包含 app、field、operator、`entity_id`，并在同层维护 app-specific operator registry。注释把 `entity_id` 定义成 DB primary key，由 control plane 解析。

**问题**：

- 领域状态携带底层 app mutation 语义；
- 真机未必存在 DB primary key；
- 上层被迫认识 operator；
- substrate independence 只能靠不断加 registry。

**目标**：TaskVM-owned opaque SurfaceHandle + visible evidence + semantic ActionContract；底层 locator 只存在具体 substrate session 中。

### `taskvm/task_state/compiler.py`

当前 compiler 依赖 benchmark spec/model client 与 harness fixture 类型。

**问题**：领域/编译层反向依赖实验层，生产输入与 GT fixture 难以隔离。

**目标**：compiler 接收统一 visible Observation；model infrastructure 通过 port 注入；benchmark 只能调用 compiler，compiler 不能 import benchmark。

### `taskvm/vm_state/` 与 `governance/vm_state.py`

存在状态/checkpoint/saga 职责重叠，且大量 session 状态还保存在 `WorkspaceSession`。

**目标**：集中为 Kernel Stores + Event Log，projection/server 不拥有业务真源。

---

## 5. Substrate 与 Harness

### `taskvm/substrate/base.py`

当前同一大文件承载：

- `StateAdapter`；
- 多个具体 App adapter；
- API 与 GUI executor 分支；
- `read_canonical()`；
- mutation；
- factory/registry。

即使 Workspace CLI 默认 `gui_agent`，factory/CLI 仍有 API executor，rollback 也可以沿 API branch 写回。

**最终标准**：runtime 无 API executor；环境 reset/seed/oracle 迁出为 Evaluation Plane。

### `taskvm/harness/state_adapter.py`

是兼容 shim，重新导出 substrate base；多处旧 import 让 harness/substrate 双真源持续存在。

**处理**：迁移完成后删除，禁止保留长期 alias。

### `taskvm/harness/browser_controller.py`

- Web/Playwright 特有逻辑位于 generic harness；
- 含个人绝对 browser/library 路径；
- element metadata 读取隐藏 `data-event-id`、`data-task-id`、`data-file-id`。

**处理**：迁入 `substrate/builtin_web`；使用可移植配置；只输出用户可见/a11y 内容和 TaskVM-owned handles。

### MobileGym 重复

同时存在：

- `harness/mobilegym_bridge.py`；
- `substrate/mobilegym/bridge.py`。

**处理**：只保留 substrate 子目录；runtime 不暴露 `setState`/snapshot restore。

### OSWorld

当前基本是 placeholder，不能支持 cross-device claim。核心论文可以先完成 port 与最小真实 adapter；跨设备放 optional bonus。

---

## 6. Architect / Governance

### `governance/governance_interpreter.py`

当前从 scripted user events 解释 workflow，并使用简单规则选择 Sequential/Parallel/Loop；判断跨 App 时依赖 fixture binding。

**问题**：不是 goal-to-workflow planner，也不支持真实 GoalPatch/replan。

### Milestone

`server.py` 独立调用 milestone suggester，但 adopted milestone 主要改变显示，不重新组织 workflow。

**目标**：Task Architect 一次输出 milestones + workflow topology + projection schema + action contracts。

### `subgoal_generator.py`

当前 LLM 模式为每个旧 PatchOp 生成两个 candidate，即 `2N` 高层调用；而下游 GUI writer 又可能重新生成 instruction，形成重复角色和断链。

**处理**：删除 model-based SubgoalGenerator；ActionContract 确定性序列化给 CUA。

### `replanner.py`

当前是 `NotImplementedError` stub。

**处理**：实现 GoalPatch 的 affected-future PlanPatch，不得保留 stub/fallback 到重新 seed。

### Driver 污染

scripted/UI simulator/user behavior drivers 混在 production governance。

**处理**：测试替身迁到 `tests/fakes`；自动用户干预属于 benchmark event injector。

---

## 7. Execution / Synchronization / Rollback

### `execution/action_dispatcher.py`

仍描述 app API，并含 broken/no-op/wrong-target 测试模式。

**处理**：生产 runtime 只消费 Substrate Port；负对照由 benchmark fault injector 实现。

### `execution/gui_executor.py`

- 直接 import Web BrowserController；
- 一个共享 page/lock；
- 每 Patch 允许 18 成功 actions、54 prediction attempts；
- 外层 `_mutate_via_gui` 又允许完整执行三轮；
- before/after 经 `read_canonical()` 获取。

**处理**：

- generic runtime 不知 Playwright；
- 统一任务级预算；
- 一次 context-preserving repair；
- before/after 来自真实 observation；
- action-level epoch/cancel。

### Pause/热插拔

`workflow_executor.py` 当前主要在 node 边界检查 pause；已经发出的模型请求没有 epoch，GoalPatch 后旧 response 可能落地。

**目标**：每个请求绑定 epoch/revision；旧 response 丢弃；单个 GUI action 是最小 atomic boundary；不可逆 action 前二次检查。

### 同步重复

当前至少四套信号并行：

1. CUA 每步 screenshot；
2. `_mutate_via_gui` 前后 canonical read；
3. SSE 每五秒 `resync_with_conflicts()`；
4. workflow verifier 再读 canonical。

**目标**：active surface 以 CUA action→observation 为主同步；heartbeat 只观察 inactive surface；fingerprint fast path 不调用模型；结构失效才增量 compiler。

### Rollback

`RollbackLog.undo_saga()` 在 GUI mode 可重新走 mutate，但因为 API mode 仍在，架构上不能保证无后门；before 又来自 hidden canonical。

**目标**：compensation 使用当时真实观察的 before/after，经同一 GUI/CUA path；不可逆诚实 partial failure；UI 可见。

---

## 8. Benchmark / Evaluation

### 当前目录

`taskvm/evaluation/` 当前包含大量：

```text
run_w1_killtest.py
run_w2_killtest.py
run_w3_killtest.py
run_full_loop_killtest.py
run_open_world_killtest.py
run_reconciliation_killtest.py
run_substrate_invariance_killtest.py
run_mobilegym_killtest.py
run_mg_vm_killtest.py
run_x_toggle_killtest.py
```

这直接证明 final evaluation 仍是历史 gate 的堆叠，而不是一套论文 benchmark。

### 当前 benchmark 问题

- task generator 的结构族有限，随机实例数不能替代任务多样性；
- full-loop 旧路径仍可能使用 GT binding；
- open-world 主要是新 operator/已知 app，不等于真正 app/task holdout；
- interaction compression 含理论 action 估计，不等于真实 trace；
- negative control 思想有价值，但不应通过 production dispatcher 的 broken mode 实现。

### 最终处理

- 删除所有 kill-test runner；
- 统一 final CLI/report schema；
- runtime/environment/oracle 三权隔离；
- 同一 CUA 比 Direct CUA、Planner+CUA、TaskVM；
- 增加 task/app/operation/cross-product/UI drift/cross-substrate splits；
- 客观指标包含 success、non-interference、cost、latency、recovery、GoalPatch、rollback 与同步开销。

---

## 9. 启动与可移植性

当前 `run.sh`：

- 硬编码个人 Python、repo、browser/lib 路径；
- 固定 kill 端口；
- 默认 debug；
- 将 built-in app、workspace 和模型配置耦合在脚本中。

这会造成：

- 换机器直接失败；
- Flask debug reloader 启动重复 worker/thread；
- 旧进程与新进程争端口；
- 用户看到随机路由和状态错误。

**目标**：可移植 `scripts/dev.sh`/`stop.sh`，标准依赖发现，health check，PID/log 管理，默认无 debug reloader，干净环境单命令启动。

---

## 10. Owner 映射

| 缺陷 | Owner 文档 |
|---|---|
| 领域类型、stores、event、import gate | `02_LAYERED_KERNEL_REFACTOR_AGENT.md` |
| API executor、hidden ID、Web/Mobile/OS 隔离 | `03_SUBSTRATE_ISOLATION_AGENT.md` |
| State Compiler、Task Architect、GoalPatch/replan | `04_TASK_ARCHITECT_GOVERNANCE_AGENT.md` |
| 405、GenUI persistent schema、workflow/screenshot UI | `05_PROJECTION_FRONTEND_AGENT.md` |
| CUA loop、epoch、同步、验证、GUI rollback | `06_CUA_EXECUTION_SYNC_ROLLBACK_AGENT.md` |
| final benchmark、kill-test 删除、oracle 隔离 | `07_FINAL_BENCHMARK_AGENT.md` |
| run.sh、README、docs、`.mrules`、最终 E2E | `08_INTEGRATION_RELEASE_CLEANUP_AGENT.md` |
| OSWorld + MobileGym 跨设备 bonus | `09_CROSS_DEVICE_BONUS_AGENT.md` |

---

## 11. 审计 Agent 的最终核对清单

重构完成后，独立审计 Agent 应重新检查：

- [ ] 生产入口是否仍接受 fixture `task_id` 而不是 goal。
- [ ] `workspace_ui/server.py` 是否仍存在或仍承担业务逻辑。
- [ ] runtime 是否 import benchmark/evaluation。
- [ ] concrete substrate 名是否出现在 projection/runtime/kernel。
- [ ] 是否仍有 API mutation、`read_canonical`、snapshot restore、simulator state injection。
- [ ] model prompt 是否含内部 ID/operator/不可见 data attribute。
- [ ] 普通 data delta 是否增加 Task Architect/GenUI call count。
- [ ] 用户不操作时 autonomy 是否持续推进。
- [ ] hot GoalPatch 后旧 response 是否可能执行。
- [ ] rollback before/after 是否来自当时 observation。
- [ ] surface card 点击是否只读取最新 artifact。
- [ ] 所有 route/method 是否无 405/500。
- [ ] workflow 是否真实可视化 sequence/fan-out/fan-in/loop。
- [ ] 不可逆结果是否在 UI 可见。
- [ ] 是否只剩一套 final benchmark runner/report。
- [ ] docs、README、pyproject、agent rules 是否不再出现 phase/kill-test/API runtime 叙事。
