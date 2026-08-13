# TaskVM Kernel 公共合同（L3）

> 状态：**冻结基线 v2**（Wave 1, Agent A；Wave-A 评审修订后）。后续 agent 只消费本页列出的公开接口；需要新跨层接口时先在本目录提交一页 RFC（master handoff §8.2）。
> 本页是接口合同，不是实现文档。实现见 `taskvm/domain/`、`taskvm/kernel/`。

## 0. 一句话

Kernel 是 TaskVM 的状态机器：它持有"TaskVM 当前相信的任务世界"，并把每一次被接受的变更变成一条事件。它不是 Flask server、不是 GUI driver、不是模型调用器。

## 1. 依赖规则（由 tests/architecture 强制执行）

```text
taskvm.domain   → 仅标准库（stdlib 白名单，非框架 denylist）
taskvm.kernel   → 仅 taskvm.domain + 标准库
taskvm.runtime  → domain/kernel + substrate PORT（taskvm.substrate 根），
                  禁止任何 concrete substrate 子树（builtin*/mobilegym/osworld）
taskvm.substrate→ 仅 taskvm.domain（反向 gate：底层不得 import 任何上层）
benchmark/evaluation 对所有上述层永远禁止
相对 import 会被解析成绝对模块后判定（from ..benchmark import x 同样被抓）。
```

## 2. 领域类型（`taskvm.domain`）

| 概念 | 类型 | 要点 |
|---|---|---|
| 意图 | `TaskIntent(goal, constraints, scope, success_criteria)` | 只能被 GoalPatch 改变；`describes_same_terminal` 比较全部四者 |
| 任务变量 | `TaskVariable(semantic_key, label, observed, desired, value_type, mutability, confidence, evidence)` | **双值平面**：`observed`（现实，仅观察路径可写）与 `desired`（目标，仅治理路径可写）；`diverged` 属性 = pending divergence |
| 观察合同 | `ObservedValue(semantic_key, value, evidence, confidence)` | 值与可见证据同体到达，落进同一个变量 |
| 任务状态 | `TaskState(intent, variables, revision)` | 不可变；`observed_values()/desired_values()/diverged_keys()` |
| 表面句柄 | `SurfaceHandle(handle_id)` | **跨层只有 TaskVM 自有 handle id**；handle_id → 具体 locator 的 registry 由 substrate 会话私有持有 |
| 可见证据 | `SurfaceEvidence(surface, visible_label, visible_context, observed_value, confidence)` | 只装用户肉眼可见的内容 |
| 投影结构 | `ProjectionSchema(root_id, components, revision)` | 稳定组件树；只在 (re)composition 时变 |
| 投影数据 | `ProjectionData(values, node_status, progress, revision)` | `values[key] = {"observed", "desired", "diverged"}`；kernel 每次刷新**全量替换**（被删的节点/变量立即消失） |
| 工作流 | `WorkflowGraph(nodes)`；`WorkflowNode(...)` | `NodeKind ∈ {SEQUENCE, FAN_OUT, BARRIER, BOUNDED_LOOP, ACTION, VERIFY, CHECKPOINT, TERMINAL}`；bounded loop 双强制（终止谓词 + max_iterations） |
| 节点状态 | `NodeStatus ∈ {PENDING, READY, RUNNING, COMMITTED, FAILED, INVALIDATED, COMPENSATED}` | 状态住在 WorkflowStore；容器（sequence/fan_out/loop）在全部子节点 COMMITTED 后自动 COMMITTED |
| 动作合同 | `ActionContract(contract_id, semantic_goal, desired_state, completion_condition, target_evidence, reversibility, risk_note)` | 跨层唯一工作单元；**禁止** app 内部动词 / 存储主键 / 平台 selector |
| 可逆性 | `Reversibility ∈ {REVERSIBLE, PARTIALLY_REVERSIBLE, IRREVERSIBLE}` | 不可逆 ⇒ `requires_confirmation` |
| 补丁 | `LocalPatch(variable_updates, node_overrides)` / `GoalPatch(new_intent)` / `CompensationPatch(target_checkpoint_id)` | 类型即语义；`requires_replan` 只有 GoalPatch 为 True；**CompensationPatch 只有 target 一个载荷字段**（调用者无法提供/伪造历史） |
| 补偿计划 | `CompensationPlan(plan_id, target_checkpoint_id, entries, epoch)`；`CompensationEntry(semantic_key, from_observed, to_observed, to_desired)` | kernel 从自己的 CheckpointRecord 生成 |
| 事件 | `Event(...)`；`EventKind` 18 类（含 `NODE_COMMITTED`） | 见 §3 的 mutation→event 表 |
| 检查点记录 | `CheckpointRecord(checkpoint_id, label, state_revision, event_index, epoch, observed, desired, committed_nodes, created_at)` | 双平面快照 |

## 3. Kernel 服务（`taskvm.kernel.TaskVMKernel`）

一个 session 一个 kernel 实例。全部方法线程安全。

**Event semantics（固定）**：每个公开变更调用追加**恰好一条**事件，kind 命名该语义操作；`finish_action` 折叠的观察是动作落地的一部分（keys 在 ACTION_FINISHED payload 里），不产生第二条事件。

**节点推进协议（固定）**：
- `ACTION`（executable）：`request_action → start_action → finish_action → record_verification`。只有 ACTION 节点产生 CUA 工作句柄。
- `VERIFY`（control）：READY 状态下直接 `record_verification(node, passed)` —— runtime 独立观察后上报，没有动作句柄。
- `BARRIER / CHECKPOINT / TERMINAL`（control）：READY 状态下 `advance_control(node)`；CHECKPOINT 额外写 CheckpointRecord（fan-in 点即已验证边界）；TERMINAL 提交 = 计划完成。
- 容器（SEQUENCE/FAN_OUT/BOUNDED_LOOP）不需要显式推进：全部子节点 COMMITTED 后自动 COMMITTED。

### 组合（State Compiler / Task Architect 输出）

- `init_task_state(variables)` — 一次性装入初始变量；**one-shot**，已有变量时调用抛 `ValidationError`（结构更新必须走 `recompose`）
- `recompose(variables, *, reason, new_graph=None, new_schema=None)` — 结构级重组唯一入口（GoalPatch 后 / 结构漂移后）：可增删变量 + evidence，可同步换图换 schema；先全量校验后变更；bump epoch
- `set_plan(graph, schema=None)` — 装入初始计划，发 `PlanCreated`

### 观察（自底向上投影）

- `apply_observation(observations: Iterable[ObservedValue])` — 只写 `observed` 平面；**未知 semantic_key 拒绝**（结构发现属于 `recompose`）；空 evidence 保留旧 evidence

### 动作生命周期（epoch 盖戳）

- `request_action(node_id) -> {action_id, node_id, epoch, contract}` — 仅 READY 的 ACTION 节点
- `start_action(action_id)` — 节点 → RUNNING
- `finish_action(action_id, *, observations=()) -> bool` — **stale epoch 一律丢弃**：不改状态、发 `ActionDiscarded`、返回 False、节点回 READY
- `record_verification(node_id, passed)` — ACTION 需 RUNNING；VERIFY 从 READY 直接确认
- `advance_control(node_id) -> CheckpointRecord | None` — BARRIER/CHECKPOINT/TERMINAL
- `requeue(node_id)` — FAILED → READY

### Checkpoint

- `commit_checkpoint(checkpoint_id, label)` — 治理手势驱动的检查点；工作流 CHECKPOINT 节点走 `advance_control`

### 治理补丁（全部 atomic：先全量校验，后变更；被拒则 state/epoch/graph/events 完全不变）

- `apply_local_patch(patch)` — 只改已声明变量的 **desired** + 未提交 ACTION 节点的合同；成功时 bump epoch
- `apply_goal_patch(patch, new_graph=None, new_schema=None)` — 恒 bump epoch + `requires_replan=True`；`new_graph` 中已提交节点必须逐字段原样携带（否则 `CommittedNodeViolationError`，且 intent/epoch/graph 不受影响）
- `request_compensation(patch) -> CompensationPlan` — 只从 kernel 自己的 CheckpointRecord 生成回退项（observed 平面差异）
- `record_compensation_result(plan_id, applied, *, observed_values=None, detail="")` — `applied=True` **必须**附新鲜观察值且**每个 plan 条目的 to_observed 全匹配**（缺 key 或值不符 ⇒ `CompensationFailed`，状态不变）；全匹配才恢复双平面（observed 按条目、desired 按 checkpoint 全量）并把 checkpoint 后提交的节点标记 COMPENSATED

### 治理事件与冲突

- `request_governance(action, detail="")`（pause 会 bump epoch）/ `record_conflict(...)` / `resolve_conflict(...)`

### 只读快照（全部防御性深拷贝）

- `task_state()` / `projection()` / `workflow()` / `checkpoints()` / `events()` / `epoch` / `session_id`

### mutation → event 表

| 调用 | 事件 |
|---|---|
| init_task_state / recompose | `StateUpdated`（payload.source 区分） |
| set_plan | `PlanCreated` |
| apply_observation | `ObservationReceived`（单条） |
| request/start/finish/discard | `ActionRequested/Started/Finished/Discarded` |
| record_verification | `VerificationPassed/Failed` |
| advance_control（BARRIER/TERMINAL） | `NodeCommitted` |
| advance_control（CHECKPOINT）/ commit_checkpoint | `CheckpointCommitted` |
| apply_local/goal_patch | `PlanPatched`（payload.patch_class 区分） |
| request/record compensation | `CompensationRequested/Applied/Failed` |
| governance/conflict | `GovernanceRequested/ConflictDetected/ConflictResolved` |

## 4. Kernel 保证的不变量（测试锁定）

1. revision 由 store 分配、严格单调。
2. 投影 schema/data revision 独立。
3. GoalPatch 不得静默改写/丢弃已提交节点。
4. stale epoch 的 ActionFinished 不改变 TaskState。
5. checkpoint 引用确定的 event/revision/epoch 边界。
6. compensation 只认 kernel 自录历史，且 applied 必须全量匹配新鲜观察。
7. store 对外只给不可变快照/防御性拷贝。
8. patch atomic：先全量校验后变更；被拒的 patch 零副作用（含 event log）。
9. observed/desired 双平面：观察只写 observed，补丁只写 desired；projection 可见 pending divergence。

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

## 6. 兼容层与删除 owner

- `taskvm/_migration/`：唯一的短期兼容层（旧 → 新单向转换）。**删除 owner：Agent G（08_INTEGRATION_RELEASE_CLEANUP_AGENT），Wave 3 集成时删除**；前提：Agent B-E 把调用点迁到本合同。
- 旧模块（`task_state/`、`vm_state/`、`governance/vm_state.py`、`execution/patch_compiler.py`、`execution/rollback.py`、`governance/subgoal.py` 等）由各自 owner 在 Wave 1-2 迁移调用点后删除；本 wave 不做物理删除（handoff 02 §迁移策略 1）。
- **已知遗留违规（响亮标记，非静默）**：legacy `taskvm/substrate` 反向 import `taskvm.execution.gui_executor*`，architecture gate 以精确匹配的 xfail 记录；owner：Agent B（03_SUBSTRATE_ISOLATION_AGENT）。债务清除后该 gate 自动转绿。
