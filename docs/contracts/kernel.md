# TaskVM Kernel 公共合同（L3）

> 状态：**冻结基线 v5**（layered ownership slim）。后续 agent 只消费本页列出的公开接口；需要新跨层接口时先在本目录提交一页 RFC（`kernel_rfc_backlog.md`）。
> **所有权模型**（见 [layered_ownership_protocol.md](layered_ownership_protocol.md)，本 wave 最高优先级冻结 spec）：
> **内容合法性由 producer / domain constructor 负责；时序合法性由 Kernel 负责。一个性质只有一个 owner，不在下一层重证。**
> Kernel 不是 hostile-caller firewall：B/C/D/E 是 TaskVM 自己控制的层，Kernel 接收已验证的 typed 对象，只判断它能否落在当前时间线上。
> 本页是接口合同，不是实现文档。实现见 `taskvm/domain/`、`taskvm/kernel/`。

## 0. 一句话

Kernel 是 TaskVM 的控制内核：它只拥有 **STATE / TIME / HISTORY / TRANSITION** 四件事——持有"TaskVM 当前相信的任务世界"及其时间线，并把每一次被接受的变更变成一条事件。它不是 Flask server、不是 GUI driver、不是模型调用器、不是内容审查员。

```text
Kernel owns（时序）:        STATE / TIME / HISTORY / TRANSITION
Producer/domain owns（内容）: CONTENT VALIDITY（构造期一次证明）
```

## 1. 依赖规则（由 tests/architecture 强制执行）

```text
taskvm.domain   → 仅标准库（stdlib 白名单，非框架 denylist）
taskvm.kernel   → 仅 taskvm.domain + 标准库
taskvm.runtime  → domain/kernel + substrate PORT（taskvm.substrate 根），
                  禁止任何 concrete substrate 子树（builtin*/mobilegym/osworld）
taskvm.substrate→ 仅 taskvm.domain（反向 gate：底层不得 import 任何上层）
benchmark/evaluation 对所有上述层永远禁止
相对 import 会被解析成绝对模块后判定（from ..benchmark import x 同样被抓）。
上层只依赖 taskvm.kernel 公共 facade/snapshot；taskvm.kernel.*_store /
event_log 等内部模块禁止被 kernel 包外 import（gate 强制执行）。
```

## 2. 领域类型（`taskvm.domain`）

| 概念 | 类型 | 要点 |
|---|---|---|
| 意图 | `TaskIntent(goal, constraints, scope, success_criteria)` | 只能被 GoalPatch 改变；`describes_same_terminal` 比较全部四者 |
| 任务变量 | `TaskVariable(semantic_key, label, observed, desired, value_type, mutability, confidence, evidence)` | **双值平面**：`observed`（现实，仅观察路径可写）与 `desired`（目标，仅治理路径可写）；`diverged` 属性 = pending divergence |
| 观察合同 | `ObservedValue(semantic_key, value, evidence, confidence)`；`ObservationBatch(observations)` | 值与可见证据同体到达；**同批重复 semantic_key 在 `ObservationBatch` 构造期拒绝**（唯一 owner） |
| 任务状态 | `TaskState(intent, variables, revision)` | 不可变；`observed_values()/desired_values()/diverged_keys()` |
| 表面句柄 | `SurfaceHandle(handle_id)` | **跨层只有 TaskVM 自有 handle id**；handle_id → 具体 locator 的 registry 由 substrate 会话私有持有 |
| 可见证据 | `SurfaceEvidence(surface, visible_label, visible_context, observed_value, confidence)` | 只装用户肉眼可见的内容 |
| 投影结构 | `ProjectionSchema(root_id, components, revision)` | 稳定组件树；构造期证明真 tree（cycle/root-no-parent/single-parent/unreachable） |
| 投影数据 | `ProjectionData(values, node_status, progress, revision)` | `values[key] = {"observed", "desired", "diverged"}`；kernel 每次刷新**全量替换** |
| 工作流 | `WorkflowGraph(nodes)`；`WorkflowNode(...)` | `NodeKind ∈ {SEQUENCE, FAN_OUT, BARRIER, BOUNDED_LOOP, ACTION, VERIFY, CHECKPOINT, TERMINAL}`；构造期证明三 primitive shape + 无环 + 单 TERMINAL sink；bounded loop 双强制且 body 必须是 ACTION/VERIFY 直接子节点 |
| 组合 | `TaskArchitecture(variables, graph, schema, exempt_node_ids)` | **组合静态一致性唯一 owner**：binding ⊆ variables、contract keys ⊆ variables、final-writer/split-brain 一致、非豁免节点均可达 TERMINAL（no orphan）；`exempt_node_ids` 承载 Kernel 的"冻结历史"时序知识（其合同是已验证工作的记录，豁免一致性检查，也允许是死端） |
| 节点状态 | `NodeStatus ∈ {PENDING, READY, RUNNING, COMMITTED, FAILED, INVALIDATED, COMPENSATED}` | 状态住在 WorkflowStore；SEQUENCE/FAN_OUT 容器在全部子节点 COMMITTED 后自动 COMMITTED；**BOUNDED_LOOP 不自动提交** |
| 动作合同 | `ActionContract(contract_id, semantic_goal, desired_state, completion_condition, target_evidence, reversibility, risk_note)` | 跨层唯一工作单元；**禁止** app 内部动词 / 存储主键 / 平台 selector |
| 可逆性 | `Reversibility ∈ {REVERSIBLE, PARTIALLY_REVERSIBLE, IRREVERSIBLE}` | 不可逆 ⇒ `requires_confirmation` |
| 补丁 | `LocalPatch(variable_updates)` / `GoalPatch(new_intent)` / `CompensationPatch(target_checkpoint_id)` | 类型即语义；`requires_replan` 只有 GoalPatch 为 True；**LocalPatch 只有 variable_updates 一个真源**（重复 key 构造期拒绝）；**CompensationPatch 只有 target 一个载荷字段** |
| 补偿计划 | `CompensationPlan(plan_id, target_checkpoint_id, entries, epoch, uncompensatable, requires_recompose)`；`CompensationEntry(node_id, semantic_key, from_observed, to_observed, to_desired, reversibility)`；`UncompensatableAction(...)` | kernel 从**自己记录的已提交动作历史**（checkpoint 之后、LIFO）生成；IRREVERSIBLE 动作进 `uncompensatable` 诚实上报 |
| 验证结果 | `VerificationResult(node_id, epoch, passed, action_id, evidence_ref, detail)` | **typed landing**：绑定一个节点、一个 epoch；ACTION 必须带 `action_id`（它所证明的 FINISHED attempt），VERIFY 必须为 None。Kernel 只查时序，内容判断（freshness、observed==desired、evidence 充分性）是 verifier（E）的职责 |
| 补偿结果 | `CompensationResult(plan_id, epoch, entry_results, detail)`；`CompensationEntryResult(node_id, semantic_key, final_observed, compensated)` | **只能经 `CompensationResult.for_plan(plan, ...)` 构造**：outcome 只能命名真实 plan entry——多余 key 在类型上无法表达；没有 `dict[str, Any]` 自由同步通道。coverage 可部分（未尝试的 entry 缺席），时间线 disposition 由 Kernel 归档 |
| 事件 | `Event(...)`；`EventKind` 23 类（含 `COMPENSATION_PARTIAL`） | 见 §3 的 mutation→event 表；EventLog 是 audit/history stream，不是 event-sourcing 框架 |
| 检查点记录 | `CheckpointRecord(checkpoint_id, label, state_revision, event_index, epoch, intent, structure, observed, desired, committed_nodes, created_at)` | **TaskVM logical checkpoint（非 App DB 快照）**；record id 全部在 **`ckpt:` namespace**（见 §3），不会与工作流节点/action/plan id 混淆 |

## 3. Kernel 服务（`taskvm.kernel.TaskVMKernel`）

一个 session 一个 kernel 实例。全部方法线程安全。

**Event semantics（固定）**：每个公开变更调用追加**恰好一条**事件，kind 命名该语义操作；`finish_action` 折叠的观察是动作落地的一部分（keys 在 ACTION_FINISHED payload 里），不产生第二条事件。

**ID namespaces（固定）**：内部 ID 全部由 Kernel 按 namespace 生成——`action:NNNNN`（动作句柄）、`comp:NNNNN`（补偿计划）、`ckpt:<name>`（检查点记录，含治理手势与 CHECKPOINT 节点）、`evt:NNNNN`（事件）、`conflict:NNNNN`。caller 提供的名字永远进不了这些 namespace，撞名在构造上不可能。

**Execution gate（固定）**：两种阻断都经 `_require_executable` 判定——(a) GoalPatch / 跨边界 rollback 之后，直到 `recompose()` 原子闭环；(b) **存在当前 epoch 的 pending 补偿计划时，一切 forward autonomy（request_action / land_verification / advance_control / requeue / loop 协议 / commit_checkpoint / apply_local_patch）一律拒绝**，直到计划落地（complete/partial/failed）或被治理手势（epoch bump）超越为 stale。

**节点推进协议（固定）**：
- `ACTION`（executable）：`request_action → start_action → finish_action → land_verification`。只有 ACTION 节点产生 CUA 工作句柄。Handle 生命周期 REQUESTED → STARTED → FINISHED/DISCARDED，**单次落地**；同一 (node, epoch) 最多一个 active handle；terminal handle 再落地抛 `ValidationError`；`finish_action` 只接受 STARTED + 当前 epoch；已提交节点不被旧结果改写。**`land_verification` 对 ACTION 要求 result 命名的 action_id 是当前 epoch 的 FINISHED attempt**——start→verify 跳过 finish、旧 epoch 的 finish、别人的 attempt 都不能成为落地证明。`start_action` 记录合同目标键的 before_observed，`finish_action` 记录 after_observed——这是补偿历史的原料。
- `VERIFY`（control）：READY 状态下直接 `land_verification(VerificationResult(..., action_id=None))`——runtime 独立观察后上报，没有动作句柄。**VERIFY 是唯一允许 READY→FAILED 的种类**。
- `BARRIER / CHECKPOINT / TERMINAL`（control）：READY 状态下 `advance_control(node)`；CHECKPOINT 额外写 CheckpointRecord（**节点先逻辑 COMMIT 再拍 boundary，因此它在自己的 committed_nodes 里**；record id 为 `ckpt:<node_id>`）；TERMINAL 提交 = 计划完成。
- 容器（SEQUENCE/FAN_OUT）不需要显式推进：全部子节点 COMMITTED 后自动 COMMITTED。
- `BOUNDED_LOOP`：显式 loop 协议——`begin_loop_iteration(node) -> iteration`（READY→RUNNING，body 子节点重新武装为 READY）+ `evaluate_loop_termination(node, terminated)`（body 全部 COMMITTED 才可评估；True ⇒ loop COMMITTED；False 且未达 max ⇒ 回 READY；False 且已达 max_iterations ⇒ FAILED + payload `reason=max_iterations_exceeded`）。body 只在 loop RUNNING 期间可调度；body 的 per-iteration 提交是 ephemeral，不算历史。

### 组合（State Compiler / Task Architect 输出）

- `init_task_state(variables)` — 一次性装入初始变量；**one-shot 以显式 initialized flag 判定**（空变量集也是合法初始组合）
- `recompose(variables, *, reason, new_graph=None, new_schema=None)` — 结构级重组唯一入口，**也是 GoalPatch 后唯一的闭环入口**：GoalPatch 后 `new_graph` 强制；无 pending 时可省略 graph/schema，但保留的旧 graph/schema 同样接受完整组合校验。**静态一致性由 `TaskArchitecture` 构造器一次证明**（Kernel 只安装已 validate 的组合；冻结历史节点经 `exempt_node_ids` 豁免）；先全量校验后变更；bump epoch；成功后执行闸打开
- `set_plan(graph, schema=None)` — 装入初始计划，发 `PlanCreated`；**one-shot**：二次调用抛 `ValidationError`，防止绕过 GoalPatch 保护擦掉执行历史

### 观察（自底向上投影）

- `apply_observation(observations)` — 只写 `observed` 平面；**未知 semantic_key 拒绝**（结构发现属于 `recompose`）；同批重复 key 由 `ObservationBatch` 构造器拒绝；空 evidence 保留旧 evidence

### 动作生命周期（epoch 盖戳）

- `request_action(node_id) -> {action_id, node_id, epoch, status, contract}` — 仅 READY 的 ACTION 节点；同 (node, epoch) 已有 active handle 则拒绝。合同是不可变 domain 对象，边界不再做多余 deepcopy
- `start_action(action_id) -> bool` — REQUESTED→STARTED；stale epoch → DISCARDED + False
- `finish_action(action_id, *, observations=()) -> bool` — 只接受 STARTED + 当前 epoch；stale epoch 或节点已进入历史状态 ⇒ DISCARDED + False
- `land_verification(result: VerificationResult)` — typed 落地（见上"节点推进协议"）；Kernel 只查 action identity / epoch / lifecycle / kind gate
- `advance_control(node_id) -> CheckpointRecord | None` — BARRIER/CHECKPOINT/TERMINAL
- `requeue(node_id)` — FAILED → READY，发 `ActionRequeued`；**仅 ACTION/VERIFY**（达 max 的 loop 必须交治理/新计划）

### Checkpoint

- `commit_checkpoint(name, label)` — 治理手势驱动的检查点，record id 为 `ckpt:<name>`。**稳定边界守卫**：存在 in-flight action / RUNNING 节点时拒绝。工作流 CHECKPOINT 节点走 `advance_control`

### 治理补丁（全部 atomic：先全量校验，后变更；被拒则 state/epoch/graph/events 完全不变）

- `apply_local_patch(patch)` — 只改已声明且 **mutability=editable** 变量的 **desired**（单一真源）；kernel 对所有未提交且引用这些 key 的 ACTION 合同做**确定性 retarget**；成功时 bump epoch；返回 `retargeted_nodes`
- `apply_goal_patch(patch)` — **两阶段封闭转换的第一阶段**：bump epoch + 更新 intent + **作废全部未提交未来（INVALIDATED）** + 阻断执行（直到 recompose 闭环）。**不接受 new_graph/new_schema 参数**——不存在"半安装"状态
- `request_compensation(patch) -> CompensationPlan` — **从 kernel 自己的已提交动作历史生成**：只覆盖 checkpoint 之后 verified 的动作（before/after 是动作当时真实记录）；TaskVM 没碰过的外部漂移不产生回退项；IRREVERSIBLE 动作进 `uncompensatable`；intent/structure（含 label/type/mutability）不同 ⇒ `requires_recompose=True`
- `record_compensation_result(plan_id, result: CompensationResult) -> str` — **epoch 绑定 + 单次落地**，返回时间线 disposition：
  - `"discarded"`：stale plan（独立 `CompensationDiscarded` 事件，不混同执行失败）；terminal plan 再落地抛错
- `"complete"`：**全部 reversible entry 落地 compensated 且无 uncompensatable standing work**（IRREVERSIBLE 提交仍在 ⇒ 诚实的 partial，绝不限伪 complete）⇒ 折叠上报的新鲜观察；恢复 checkpoint 的 desired 平面（**全部幸存变量**，含无物理 entry 的 LocalPatch-only 漂移）+ label/type/mutability（**不恢复陈旧 evidence**）；重新挂上结构上消失的 checkpoint 变量（**observed = unknown**：kernel 没有眼睛，现实由 verifier 重新观察填入，绝不从 checkpoint 快照伪造，§4.12）；**绝不逻辑删除后出现的变量**；恢复 checkpoint intent。同 intent/结构 ⇒ frontier 确定性倒回边界并重新武装同一路径，且对幸存未来合同做**确定性 retarget 回 checkpoint desired**（与 LocalPatch 同一通道；committed 历史永不动）+ 被倒回的 loop counter 重置（仍 COMMITTED 的 loop 保留）；跨边界 ⇒ 被撤销的提交标 COMPENSATED + 剩余未来 INVALIDATED + 等待 recompose。**target 之后的 active future checkpoints 从 active timeline 截断**（`truncated_checkpoint_ids` 进事件 payload）
- `"partial"`：有 entry 未撤销**或存在 uncompensatable standing work** ⇒ 诚实归档（`CompensationPartial` 事件，payload 含 `uncompensated` 明细与 `uncompensatable_nodes`）；被撤销的工作标 COMPENSATED（绝不伪装成仍成立，也不静默重新武装）；standing 写入（含不可逆提交）保持 COMMITTED；**不截断未来 checkpoint history**；只消费已撤销条目的动作历史（standing 写入的历史保留给更早 checkpoint 的回滚）；forward autonomy 阻断，等待治理（recompose）；被作废未来的 loop counter 同样重置
  - `"failed"`：无 entry 被撤销 ⇒ `CompensationFailed` 事件；**状态完全不变**

### 治理事件与冲突

- `request_governance(action, detail="")`（pause 会 bump epoch）/ `record_conflict(...)` / `resolve_conflict(...)`

### 只读快照（全部防御性深拷贝）

- `task_state()` / `projection()` / `workflow()` / `checkpoints()` / `events()` / `epoch` / `session_id` / `pending_recompose`

### mutation → event 表

| 调用 | 事件 |
|---|---|
| init_task_state / recompose | `StateUpdated`（payload.source 区分） |
| set_plan | `PlanCreated` |
| apply_observation | `ObservationReceived`（单条） |
| request/start/finish/discard | `ActionRequested/Started/Finished/Discarded` |
| requeue | `ActionRequeued` |
| land_verification | `VerificationPassed/Failed`（payload 含 evidence_ref） |
| advance_control（BARRIER/TERMINAL） | `NodeCommitted` |
| advance_control（CHECKPOINT）/ commit_checkpoint | `CheckpointCommitted` |
| apply_local/goal_patch | `PlanPatched`（payload.patch_class + invalidated_node_ids） |
| request compensation | `CompensationRequested` |
| record_compensation_result | `CompensationApplied`（complete）/ `CompensationPartial` / `CompensationFailed` / `CompensationDiscarded` |
| begin/evaluate loop | `LoopIterationStarted/LoopIterationEvaluated` |
| governance/conflict | `GovernanceRequested/ConflictDetected/ConflictResolved` |

## 4. Kernel 保证的不变量（测试锁定；对应 layered protocol §4 的 15 条）

1. revision 由 store 分配、严格单调。
2. 投影 schema/data revision 独立。
3. GoalPatch/recompose 不得静默改写/丢弃已提交节点（loop 运行中的 body 提交是 ephemeral，不算历史）。
4. stale epoch 的 ActionFinished 不改变 TaskState。
5. checkpoint 引用确定的 event/revision/epoch 边界，且只能在稳定 action boundary 拍摄（无 in-flight）；CHECKPOINT 节点先逻辑提交再拍 boundary（自己在自己快照里）。
6. compensation 只由 kernel 自录的已提交动作历史推导（非快照 diff）；IRREVERSIBLE 诚实上报不可回退；不做逻辑状态删除。
7. store 对外只给不可变快照/防御性拷贝；写方向同样深拷贝。**公共 domain 对象 frozen immutable 后，不再做无脑边界 deepcopy**——只保留保护私有 mutable state 所必需的拷贝。
8. patch atomic：先全量校验后变更；被拒的 patch 零副作用（含 event log）。
9. observed/desired 双平面：观察只写 observed，补丁只写 desired；projection 可见 pending divergence。
10. action handle 单次落地：REQUESTED→STARTED→FINISHED/DISCARDED；同 (node, epoch) 最多一个 active handle；terminal handle 永远不能再落地；验证必须绑定当前 epoch 的 FINISHED attempt。
11. compensation plan epoch 绑定 + 单次落地；stale/重复落地走独立的 `CompensationDiscarded`。
12. bounded loop 只能被显式终止决定提交：iteration 计数、termination 评估、max_iterations 强制（超限 ⇒ FAILED + escalation）。
13. **pending compensation 阻断一切 forward autonomy**，直到计划落地（complete/partial/failed）或被治理超越。
14. **两阶段封闭**：set_plan/init_task_state one-shot；GoalPatch 后执行阻断直至 recompose 原子闭环；VERIFY 独占 READY→FAILED。
15. **rollback 时间线归档**：COMPLETE = 全部 reversible entry 撤销且无 uncompensatable standing work——倒回 frontier + 恢复全部 checkpoint desired + retarget 幸存未来合同 + 重置被倒回的 loop counter + 截断 active future checkpoints；PARTIAL（含不可逆提交仍在）诚实标记 COMPENSATED、不截断、等待治理；跨结构恢复的变量 observed=unknown，等 verifier 重新观察；FAILED 零副作用；组合边界（set_plan/recompose）只安装经 `TaskArchitecture` 验证的组合（dangling 引用 / split-brain / orphan 一律拒；已提交历史节点豁免）。

> 内容合法性（workflow shape、projection tree、contract/binding 引用、duplicate key、typed result 可表达性）**不是** Kernel 不变量——它们是 domain constructor 的所有物，由 `tests/domain/` 锁定。Kernel 不为它们设第二道检查。

## 5. 迁移表（旧类型 → 新类型）

| 旧（将被删除） | 新 | 说明 |
|---|---|---|
| `task_state.representation.TaskStateGraph` | `domain.TaskState` | var_id → semantic_key；value 同时装入 observed+desired（组合时刻意图=现实） |
| `task_state.representation.TaskVariable` | `domain.TaskVariable` | editable → mutability |
| `task_state.entity_binding.EntityBinding/TaskBinding/Dependency` | **不迁移** | 数据库主键 + app 内部动词不进新领域；visible locator → `SurfaceEvidence`（见 `_migration.legacy_state`） |
| `entity_binding.OPERATOR_REGISTRY` | **不迁移** | 平台动词表是 substrate 私有物（Agent B 处理） |
| `execution.patch_compiler.PatchOp` | `domain.ActionContract` | 剥掉平台寻址与动词，只留语义（`_migration.legacy_op_to_action_contract`） |
| `governance.subgoal.WorkflowNode/WorkflowPlan` | `domain.WorkflowNode/WorkflowGraph` | SEQUENTIAL/PARALLEL/LOOP → SEQUENCE/FAN_OUT(+BARRIER)/BOUNDED_LOOP |
| `execution.rollback.RollbackLog/CompensationRecord` | `kernel.CheckpointStore` + `CompensationPlan` | saga → checkpoint 边界的补偿计划；before/after 只认 kernel 记录 |
| `governance.vm_state.VMStateSnapshot` / `vm_state/` re-export 包 | `kernel.TaskVMKernel` 的各 store 快照 | 平行结构合并为唯一真源 |
| `task_state.projection_policy` / `dependency_graph` | kernel 内部投影刷新 + `WorkflowGraph.ready_nodes` | 策略逻辑下沉 |
| `record_verification(node_id, bool)`（v4） | `land_verification(VerificationResult)` | 裸布尔通道删除；验证绑定当前 epoch 的 FINISHED attempt |
| `record_compensation_result(plan_id, applied, observed_values=dict)`（v4） | `record_compensation_result(plan_id, CompensationResult) -> disposition` | free-form dict 同步通道删除；`CompensationResult.for_plan` 是唯一构造路径 |

## 6. 兼容层与删除 owner

- `taskvm/_migration/`：唯一的短期兼容层（旧 → 新单向转换）。**删除 owner：Agent G（08_INTEGRATION_RELEASE_CLEANUP_AGENT），Wave 3 集成时删除**；前提：Agent B-E 把调用点迁到本合同。
- 旧模块（`task_state/`、`vm_state/`、`governance/vm_state.py`、`execution/patch_compiler.py`、`execution/rollback.py`、`governance/subgoal.py` 等）由各自 owner 在 Wave 1-2 迁移调用点后删除；本 wave 不做物理删除（handoff 02 §迁移策略 1）。
- **已知遗留违规（响亮标记，非静默）**：legacy `taskvm/substrate` 反向 import `taskvm.execution.gui_executor*`，architecture gate 以精确匹配的 xfail 记录；owner：Agent B（03_SUBSTRATE_ISOLATION_AGENT）。债务清除后该 gate 自动转绿。
