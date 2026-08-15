# TaskVM Runtime 公共合同（L2 Autonomy Runtime）

> 状态：**冻结**（2026-08-15，Agent E 交付）。后续 agent 只消费本页列出的公开接口；需要新跨层接口时先在本目录提交一页 RFC（`runtime_rfc_backlog.md`）。
> **所有权模型**（见 [layered_ownership_protocol.md](layered_ownership_protocol.md)，本 wave 最高优先级冻结 spec）：
> **内容合法性由 producer / domain constructor 负责；时序合法性由 Kernel 负责。一个性质只有一个 owner，不在下一层重证。**
> 本页是接口合同，不是实现文档。实现见 `taskvm/domain/`、`taskvm/runtime/`、`taskvm/verifier/`。

## 0. 一句话

Runtime 是 TaskVM 的**执行时钟**：在治理边界内，它持续推进 Kernel 已就绪的工作——观察世界、让 CUA 预测一个原子 GUI 动作、经 Kernel 时序 gate 落地、重新观察、运行可见验证、提交/补偿。它不是 Planner、不是 Frontend、不是 Concrete Substrate、不是 Benchmark Oracle，也不再拥有第二套 Kernel。

```text
Runtime owns:  EXECUTION CLOCK — observe → think → act → verify → commit/recover,
               bounded autonomy, hot-governance stale-discard, real-GUI compensation,
               active/inactive surface sync, fan-out/loop control flow, CUA call accounting
Kernel owns:   STATE / TIME / HISTORY / TRANSITION (epoch, lifecycle, patch, checkpoint, plan)
```

> 用户不操作 ≠ TaskVM 静止。`governance over autonomy`：用户没有发起治理操作时，CUA 应持续自主推进 Workflow，直到 terminal / pause / budget-exhausted / unrecoverable-verify-failure / need-human / irreversible-boundary。

## 1. 依赖规则（由 tests/architecture + tests/runtime/test_runtime_architecture 强制执行）

```text
taskvm.runtime   → 仅 taskvm.domain + taskvm.kernel(facade) + taskvm.substrate(PORT 根) + 标准库
                    禁止 taskvm.architect / benchmark / evaluation / 任何 concrete substrate 子树
                    禁止 taskvm.kernel.*_store / event_log（只走 facade）
                    禁止 taskvm.verifier（verifier 经依赖注入进入，见 §6）
taskvm.verifier   → 仅 taskvm.domain + 标准库（runtime-visible verifier 不需要 kernel/substrate；
                     它消费 runtime 传入的新鲜观察 + ActionContract，产出 VerificationResult）
```

Runtime 是 **stdlib-only 纯 Python**：真正的 HTTP 模型调用、Playwright 动作执行、移动桥接全部在注入的 Port 实现里（其具体实现住在 `taskvm.architect` / `taskvm.substrate` / 组合层），Runtime 只持有 Protocol。生产路径无任何 `requests`/`flask`/`playwright` import。

### 跨层 Port：依赖注入，不开 reverse import

CUA 目标序列化器（`ActionContractSerializer`）、`ModelPort`、`ModelCallLedger` 全部住在 `taskvm.architect`（Agent C）。Runtime 的 import gate 不允许它 import architect（见上）。**因此 Runtime 在 `taskvm/runtime/ports.py` 定义自己的 Protocol 端口**，由组合层注入具体实现：

| Runtime Port (Protocol) | 结构兼容的 architect 具体实现 | 谁注入 |
|---|---|---|
| `CUAGoalSerializer` | `taskvm.architect.ActionContractSerializer` | 组合层 / 测试 |
| `CUAModel` | architect `ModelPort`+`_LedgeredPort` 的适配器（产出 `CUADecision`） | 组合层（Agent G） |
| `ObservationExtractor` | `StateCompiler.extract_observed` 的确定性包装 | 组合层 / 测试 |
| `CallLedger` | `taskvm.architect.ModelCallLedger`（duck-typed 兼容：同方法名 + 同字段 `ModelCallRecord`） | 组合层（注入**同一实例**给 architect 与 runtime，统一报告） |
| `Verifier` | `taskvm.verifier.VisibleVerifier`（E 自有具体实现） | 组合层 / 测试 |

`ModelCallRecord`（runtime 自有）的字段与 architect 的 `ModelCallRecord` **逐字段相同**，因此 architect 的 `ModelCallLedger.record()` 接受 runtime 产出的记录而不报错——**同一个 ledger 实例**同时记账 state_compiler / task_architect / cua 三角色，benchmark 报告的 CUA call count 即真实 provider request count。详见 `runtime_rfc_backlog.md` RFC-001。

## 2. 领域类型（Runtime 消费 / 生产；定义在 `taskvm.domain`）

| 概念 | 类型 | Runtime 角色 |
|---|---|---|
| 工作单元 | `ActionContract(contract_id, semantic_goal, desired_state, completion_condition, target_evidence, reversibility, risk_note)` | 消费（从 Kernel `request_action` 拿到） |
| 验证结果 | `VerificationResult(node_id, epoch, passed, action_id, evidence_ref, detail)` | **生产**（ACTION 必带当前 epoch 的 FINISHED `action_id`；VERIFY 为 None） |
| 补偿结果 | `CompensationResult`（经 `for_plan(plan, *, epoch, outcomes)` 构造） | **生产**（outcomes 只能命名真实 plan entry） |
| 补偿条目结果 | `CompensationEntryResult(node_id, semantic_key, final_observed, compensated)` | **生产**（`compensated` 是 E 的内容判断） |
| 观察值 | `ObservedValue(semantic_key, value, evidence, confidence)` | 生产（经注入的 `ObservationExtractor` 从 substrate `Observation` 确定性抽取） |
| 工作流 | `WorkflowGraph` / `WorkflowNode` / `NodeKind` / `NodeStatus` | 消费（`kernel.workflow()` 快照 + `graph.ready_nodes(statuses)`） |
| 补偿计划 | `CompensationPlan` / `CompensationEntry` / `UncompensatableAction` | 消费（Kernel 从自己的 committed action history 产生，**Runtime 不生成**） |

Runtime **不创造** `dict[str, Any]` 式自由结果通道。所有跨层结果经 typed domain 构造（`VerificationResult` / `CompensationResult.for_plan`），坏输入在构造期即被拒绝。

## 3. Runtime 服务（`taskvm.runtime.AutonomyRuntime`）

一个 session 一个 runtime 实例。Runtime **不持有第二套 authoritative epoch**——epoch/revision/action-lifecycle 全归 Kernel。Runtime 是单线程执行时钟（CHI prototype；logical parallel ≠ mandatory physical parallel，见 §10）。

### 公开 facade

- `AutonomyRuntime(kernel, substrate, *, cua_model, serializer, extractor, verifier, ledger, budgets, surfaces)` — 组合层构造，注入全部 Port。
- `run(step_budget=None)` — **自治主循环**：在没有治理事件时连续推进多个 ACTION/control 节点，直到 terminal / pause / budget / unrecoverable verify / need-human / irreversible-boundary。每轮围绕 Kernel action lifecycle。
- `request_pause()` / `request_resume()` — 软中断入口（soft pause：当前 atomic action 完成后停止下一 action）。
- `execute_compensation(plan: CompensationPlan) -> str` — 执行 Kernel 产出的补偿计划（真实 GUI 补偿），返回 Kernel `record_compensation_result` 的 disposition（`complete`/`partial`/`failed`/`discarded`）。**这是 RollbackRequested 的执行半边**——计划由治理层 `kernel.request_compensation` 产生，Runtime 只负责把它在真实世界落地。
- `poll_inactive_surfaces()` — inactive surface 心跳（补漏，不重复 active surface）。
- `runtime_events() -> tuple[RuntimeEvent, ...]` — Runtime 自产事件流（`StructureInvalidated` / `CompensationEntryExecuted` / `BudgetExhausted` / `SurfaceConflict` / `ActionLanded` artifact 引用），供 Projection(D) / 评估消费。**Runtime 不直接写 D 的 Projection Store**。

### Action lifecycle（围绕 Kernel facade，绝不绕过）

每个 ACTION 节点：

```text
kernel.request_action(node_id)         -> handle {action_id, epoch, contract}   (Kernel 分配 epoch)
  ↓ Runtime 持有 handle；记录 request_epoch = handle["epoch"]
substrate.observe(surface)             -> Observation（active surface 主同步信号）
serializer.cua_goal(contract, labels)  -> goal str（确定性，0 模型调用）
  ↓ 如果 contract.requires_confirmation（IRREVERSIBLE）：再次检查 Kernel epoch + 治理态
cua_model.predict_action(goal=, observation=, attempt=) -> CUADecision
  ↓ ↓ ↓ Hot-governance gate ↓ ↓ ↓
kernel.start_action(action_id)         -> bool   (REQUESTED→STARTED; stale epoch → DISCARDED + False)
  ↓ 若 False（stale / 被治理取消）：丢弃该 CUA response，绝不 substrate.act()，回到循环顶
substrate.act(surface, decision.action, epoch=str(kernel.epoch)) -> ActionReceipt
substrate.observe(surface)             -> fresh Observation
extractor.extract(observation, vars)   -> tuple[ObservedValue]
kernel.finish_action(action_id, observations=...) -> bool   (记录 after_observed; stale→DISCARDED)
verifier.verify(contract, before, after_observed, observation) -> VerificationResult
kernel.land_verification(result)       (ACTION: result.action_id = 该 FINISHED attempt)
  ↓ passed → 节点 COMMITTED → continue / advance_control / next ready node
  ↓ failed → 受预算约束的 context-preserving repair（见 §7）；预算尽 → safe pause / escalate
```

VERIFY 节点：READY 状态下 runtime 独立观察后 `land_verification(VerificationResult(..., action_id=None))`，无 action handle。CHECKPOINT/BARRIER/TERMINAL：`advance_control`。BOUNDED_LOOP：`begin_loop_iteration` / `evaluate_loop_termination`（见 §11）。

## 4. Hot Governance：中断边界 = 原子 GUI Action

治理可在 CUA 自主运行中介入。**Runtime 不维护第二套 epoch**；它利用 Kernel action lifecycle 做 stale-discard：

- CUA `predict_action` 返回后、`substrate.act()` **之前**，Runtime 调 `kernel.start_action(action_id)`。Kernel 若判 stale / blocked / cancelled-by-governance（epoch 已被 GoalPatch/Pause/compensation bump）→ 返回 `False` + `ACTION_DISCARDED` 事件。Runtime **绝不** `substrate.act()`，丢弃该 response，回循环顶。
- **已经开始的原子动作**（`click`/`type`/`key`/`scroll` 已进入 actuator）：允许该单动作完成；下一 atomic action 前必须重新进入 Kernel gate。不在 `mouseDown`/`mouseUp` 之间硬杀进程。
- **不可逆/高风险动作**（`Send`/`Purchase`/`Submit`/`Delete`，即 `contract.requires_confirmation`）：`act()` 前再次检查 Kernel epoch + 治理态；用户刚介入时旧 action 不落地。
- **soft pause**：`request_pause()` → 当前 atomic action 完成后阻止下一 action（不杀进行中的动作）。
- **pending compensation 阻断 forward**：Kernel 在存在当前 epoch 的 pending 补偿计划时拒绝一切 forward autonomy（`request_action` 等抛 `ValidationError`）。Runtime 的主循环检测到该状态（或被显式交给 plan）→ 切换到 `execute_compensation` 模式；forward autonomy 在 `record_compensation_result` 落地前不再推进。

### Stale-discard 测试契约

`request action at epoch N → CUA request in flight → governance GoalPatch/Pause bumps Kernel epoch → 旧 CUA response 返回`：断言 `substrate.act()` 未被调用、`kernel.start_action` 返回 `False`、`ACTION_DISCARDED` 事件落库。

## 5. Autonomy Budget：消灭三级放大重试

旧设计的 `max_steps × max_attempts × outer full retries` 已废除。Runtime 使用单层、分种类预算（`taskvm/runtime/config.py` 的 `RuntimeBudgets`）：

```text
max_actions_per_contract         # 一个 ActionContract 最多多少个原子 GUI 动作
max_invalid_predictions_per_contract  # CUA timeout / invalid JSON / 非法 action（非 GUI action）
max_repairs_per_contract          # verifier fail 后的 context-preserving repair 上限（默认 1）
max_model_calls_per_task          # 任务级 CUA provider call 硬顶
wall_clock_budget                 # 任务级墙钟
```

`max_replans_per_task` 属于上层 governance/architect budget，**Runtime 不自己执行 replan**。

### 分种类记账（注入的 `CallLedger`，统一报告）

每类量分开记录，`ModelCallRecord(role="cua", purpose=..., ok=, is_repair=, ...)`：

| 量 | 计入 CUA call? | 计入 GUI action? |
|---|---|---|
| CUA provider call（成功预测一个 action） | 是 | 否 |
| invalid prediction（timeout / invalid JSON / 无法 parse / 非法 action） | 是（`ok=False`） | 否（受 `max_invalid_predictions` 约束） |
| successful GUI action（`substrate.act()` 返回 `ok`） | 否 | 是 |
| verifier fail 后的 repair call | 是（`is_repair=True`） | 否 |

**承诺**：论文报告的 CUA call count = 实际 provider request count。Runtime 不隐藏 provider 内部 retry——`repair_retries`/transport retry 由注入的 ModelPort 自行记账到同一 ledger。

### Verifier failure → 受预算 repair，不整轮重跑

verifier fail 后**最多** `max_repairs_per_contract` 次上下文保持 repair：repair prompt/contract 携带当前 observation、已执行动作、verification discrepancy、剩余 completion condition。**不** `失败→回首页→整个 Patch 重跑→再失败→再整轮重跑`。预算耗尽 → `safe pause / escalate`（`BudgetExhausted` runtime event + Kernel `request_governance("pause")`），而非盲跑。

## 6. Runtime-visible Verifier（`taskvm.verifier.VisibleVerifier`）

严格区分：

```text
CUA says done   ≠   TaskVM verified
```

只有 Runtime-visible verifier 通过，工作才进入 verified/committed 语义。Verifier 的内容合法性是 **E 唯一所有**（layered protocol §1）：before/after 来自执行前后的新鲜可见观察、`completion_condition` 确实被检查、IRREVERSIBLE 能力诚实报告、`CompensationResult` 只含当前 plan entries。

- **Kernel 不重新证明这些内容**——Kernel 只查 `node_id` / `action_id` / `epoch` / lifecycle / kind gate。Runtime 不在 E 里重复 Kernel 的时序校验，也不要求 Kernel 再做 verifier。
- **Verifier 只用真人/Agent 可见证据**：fresh screenshot / accessibility-visible state / visible text / TaskVM-owned `SurfaceHandle` evidence。`evidence_ref` 指向真实捕获的 artifact。
- **禁止**：hidden DB / benchmark oracle / fixture ground truth / internal `entity_id` / internal API state。
- **default 实现确定性**（`VisibleVerifier`）：`passed` iff 对 `contract.desired_state` 的每个 key，fresh `after_observed[key] == desired_state[key]`，且 `completion_condition` 的可见判据满足；IRREVERSIBLE contract 在 `act()` 前被 `requires_confirmation` 拦截。0 模型调用。组合层可注入模型增强版，但只有一份 verifier（E），不 Runtime 验一次 Kernel 再验一次。
- **Evaluation oracle verifier 完全独立**：最终 benchmark 可另读 hidden ground truth 判卷，但**绝不**控制 Runtime。

## 7. Compensation / Rollback：真实世界回退

Rollback **不**恢复 App 数据库快照。真正语义：TaskVM 根据历史"自己做过什么"，再通过真实 GUI 做补偿操作。

**所有权边界**：Runtime **不自行生成 `CompensationPatch` / `CompensationPlan`**。治理层负责 `RollbackRequested → kernel.request_compensation(...)`；Kernel 从**自己保存的 committed action history**（`start_action` 记录 before、`finish_action` 记录 after、`land_verification` 入 history）产生 `CompensationPlan`。Runtime 的工作从拿到 plan 开始。

### `execute_compensation(plan)` 流程

对 `plan.entries`（**不**含 `plan.uncompensatable`——IRREVERSIBLE 由 Kernel 诚实标 `uncompensatable`，Runtime 不碰）的每个可逆 entry：

```text
serializer.compensation_goal(entry, labels) -> goal str（确定性，0 模型调用，"逆操作文案"不调模型）
  ↓ 同一 SubstrateSession + 同一 CUA + 同一 Atomic-Action lifecycle
kernel 无 forward autonomy（pending plan 阻断）→ Runtime 在补偿模式驱动
CUA predict -> start gate -> substrate.act（真实 GUI）-> fresh observe
verifier.verify(entry: observed == entry.to_observed?) -> CompensationEntryResult(node_id, semantic_key, final_observed, compensated)
  ↓ 全部 entry 完成
CompensationResult.for_plan(plan, epoch=plan.epoch, outcomes=...)   # 只能命名真实 plan entry
kernel.record_compensation_result(plan.plan_id, result) -> "complete"|"partial"|"failed"|"discarded"
```

- **历史必须来自当时真实观察**：`before` 若当时没被真实观察，以后**绝不**从 benchmark oracle 查回来假装可回退。
- **绝对禁止**：DB snapshot restore / `set_state` / internal API mutation / hidden canonical write / GUI rollback 失败后偷偷改数据库。
- **不可逆**（substrate 无真实可见撤回能力，或 `plan.uncompensatable` 非空）：返回 `PARTIAL` / 该 entry `compensated=False`，**不伪造 complete**。Kernel 据此判 `complete = (全部 reversible entry 落地 compensated 且无 uncompensatable standing work)`。
- **stale plan**：若 `execute_compensation` 期间治理 bump 了 epoch，`record_compensation_result` 返回 `"discarded"`（独立 `CompensationDiscarded` 事件，不混同执行失败）。Runtime 不重试 stale plan。

## 8. Observation-driven Synchronization

**不**采用"每 5 秒读 hidden canonical API"作为现实同步。CUA 每个真实 GUI action 后的新 observation 就是 active surface 的主同步来源。

### Active surface

每个 atomic action后：`substrate.act → ActionReceipt → fresh substrate.observe() → visible observation → extractor.extract → ObservedValues → kernel.finish_action/apply_observation → runtime event + visual artifact → D 消费`。CUA 正在高频操作某 surface 时，**不**对同一 active surface 再跑另一套完整 heartbeat polling（重复观察）。

### Inactive surface heartbeat（补漏）

低频观察 CUA 当前没注视的 surface，补上外界修改：

- **fingerprint 不变** → `0 模型调用`、`0 高层 compiler 调用`。
- **已知 handle 只有值变** → 确定性 observation delta → `kernel.apply_observation` → `0 高层模型调用`。
- **真正结构失效**（anchor 消失 / binding 无法恢复 / UI 结构物质漂移 / 出现 task-relevant 未知结构）→ Runtime 只发布 `StructureInvalidated` runtime event。**Runtime 自己不调 TaskArchitect/StateCompiler**——由上层 composition/governance 把该事件路由给 C 的 incremental State Compiler slow path。`E executes; C understands/replans`，边界清晰。

### Conflict

若 inactive/active observation 发现 `current observed != pending desired` 且变化**并非 TaskVM 当前 action 自己产生**：

- **不静默覆盖**。记录 `current observed / desired / last committed / surface / affected node/lane`。
- 暂停**受影响**工作；**不**默认暂停完全无关的 lane。
- 发布 `SurfaceConflict` runtime event + `kernel.record_conflict(...)`，让上层治理/UI 选 `keep world / apply desired / edit local target / edit goal`。Runtime 不自己决定用户意图。

## 9. CUA 只产生原子 GUI Action

CUA 每轮只决定一个最小动作（`CUADecision`）：`act`（携带一个 `GuiAction` ∈ `click|tap|type|key|scroll|wait|open`）/ `done`（contract 完成，触发验证）/ `fail`（CUA 无法推进，repair/escalate）。Playwright/Mobile/OS actuator 只是执行手。Runtime **不**一次把整个多步 trajectory 当不可中断原子操作。

## 10. Fan-out / Barrier / Fan-in

不实现 arbitrary workflow engine。只支持 Kernel 已冻结的最小 primitive（`FAN_OUT` + `BARRIER`）：

- 每条 lane 有独立 runtime execution context；Kernel 仍是统一 epoch/timeline 真源。
- concrete substrate 决定能否物理并行；logical parallel ≠ 必须 physical parallel。单一设备只能串行时，诚实记录 `logical parallel / physical serial`。
- 一条 lane 失败**不**摧毁其他已 verified 的 lane。
- `BARRIER` 只有所有 required lane verified/committed 后才 `advance_control` 通过。

不实现 arbitrary nested DAG runtime。

## 11. Bounded Loop

只支持冻结的 `BOUNDED_LOOP`（body 必须是 ACTION/VERIFY 直接子节点，无嵌套 loop）：

```text
begin iteration (kernel.begin_loop_iteration) -> iteration
  ↓ fresh observation / 执行 body / verification
  ↓ body 全部 COMMITTED 后
evaluate_loop_termination(node, terminated) -> {committed|continue|failed}
  ↓ terminated=True → loop COMMITTED
  ↓ terminated=False 且未达 max → 回 READY 下一轮
  ↓ 达 max_iterations → FAILED + escalation（Runtime 不自行重试 maxed loop；交治理/新计划）
```

不实现递归 / nested arbitrary loops / 通用 workflow DSL。

## 12. Runtime 不负责什么

- **不重新实现 Planner**：Runtime 接收已生成的 `WorkflowGraph` / `ActionContract`，只执行。
- **不直接 import concrete substrate**：只认 `taskvm.substrate` 根 Port / `SubstrateSession`。
- **不直接修改 Agent B 的 concrete backend**：B 未满足时用 `FakeSubstrateSession` / protocol fake 完成 contract test；真正缺失记 blocker。
- **不直接拥有 Projection UI state**：Runtime 不写 D 的 Projection Store。`Observation → kernel.apply_observation → Kernel 权威 observed/projection → Runtime event/snapshot/artifact → D 消费`。
- **不调用 Benchmark Oracle**：Runtime perception/verification 永不调用 hidden DB / fixture answer / get_state oracle / benchmark success checker / internal mutation API / snapshot restore。
- **不维护第二套 authoritative epoch / 第二套 Kernel**。

## 13. 物理删除策略（与 kernel.md §6 一致）

`taskvm/execution/**` 是 legacy 双轨，违反本合同（benchmark import / `entity_id` / `read_canonical` / API dispatcher / wrong-target killtest / `NotImplementedError` stub / 独立 SubgoalGenerator 热路径）。**本 wave 不做物理删除**（kernel.md §6：迁移策略 1）——它被 `tests/test_imports.py`（L0 CI）、`taskvm/evaluation/run_*.py`（13 个 killtest，Agent F）、`taskvm/workspace_ui/server.py`（Agent D）、`taskvm/governance/`（Agent C）、`tests/fakes/` 交叉引用。**Runtime 是唯一 production execution truth，绝不 import `taskvm.execution`**；物理删除 gated 于 C/D/F 迁移各自调用点，由 Agent G（Wave 3）执行。`taskvm/verifier/` 的 legacy oracle verifier（`canonical_state` / `non_interference` / `round_trip_checks` / `cross_app_checks` / `reconciliation` / `rollback_verify`）同理为 evaluation-plane，由 Agent F 迁往 `taskvm/evaluation/`；本 wave 新增 `taskvm/verifier/visible.py` 作为 runtime-visible verifier，不触碰 legacy 文件。

## 14. Runtime Contract Tests（`tests/runtime/`、`tests/verifier/`、`tests/architecture/test_import_boundaries.py`、`tests/integration/test_runtime_trace.py`）

至少覆盖：

- **Autonomy**：无治理事件时连续推进多个 ACTION/control node。
- **Hot Pause**：CUA 一个 atomic action 完成后 pause 阻止下一 action。
- **Stale CUA Response**：epoch N 请求 in flight → GoalPatch/Pause bump → 旧 response 返回 → `substrate.act()` 未调用 + `ACTION_DISCARDED`。
- **Active-surface synchronization**：CUA action 后新 observation 成功落入 Kernel；active surface 不被 heartbeat 重复完整 poll。
- **Inactive heartbeat fast path**：fingerprint 不变 → `0 模型调用`、`0 高层 compiler 调用`。
- **Structure invalidation**：binding 无法恢复 → Runtime 发布 `StructureInvalidated`；Runtime 自己不调 TaskArchitect/StateCompiler。
- **Verification**：`CUA done ≠ verified`；verification fail 不 commit。
- **Repair budget**：verifier fail 最多有限 context-preserving repair；预算耗尽 safe stop/escalate。
- **Compensation**：fake substrate trace 体现 `forward GUI actions → checkpoint → later actions → rollback request → reverse GUI compensation → fresh observations → CompensationResult`；测试不得通过改 fake DB snapshot 伪造成功。
- **Irreversible**：不可逆 entry 不被报告为 complete rollback。
- **Fan-out / Fan-in**：lane 独立；required lane 全部 verified → barrier 才通过。
- **Bounded Loop**：`termination=true` 正常结束；`false` 下一轮；`max_iterations` 达后停止。
- **Architecture**：`taskvm.runtime` 只 import domain/kernel-facade/substrate-port/stdlib；不 import architect/benchmark/evaluation/concrete-substrate/kernel-internals/verifier。`taskvm.verifier.visible` 只 import domain/stdlib。

## 15. 已知边界（不在本 wave，路由给对应 agent）

- `taskvm/execution/**` 物理删除：Agent G（Wave 3），gated 于 C/D/F 迁移调用点。
- legacy `taskvm/verifier/*` oracle verifier 迁往 `taskvm/evaluation/`：Agent F。
- Runtime→architect 的 import gate（CUA 适配器等组合层注入）：见 `runtime_rfc_backlog.md` RFC-001（本 wave 以 DI Port 解决，非阻塞）。
- concrete substrate（B）某 public capability 缺失：记 blocker，不替 B 实现。
- Projection(D) 消费 runtime artifact：Agent D；Runtime 只产 typed event/artifact。
