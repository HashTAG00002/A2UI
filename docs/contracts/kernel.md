# TaskVM Kernel 公共合同（L3）

> 状态：**冻结基线 v1**（Wave 1, Agent A 交付）。后续 agent 只消费本页列出的公开接口；需要新跨层接口时先在本目录提交一页 RFC（master handoff §8.2）。
> 本页是接口合同，不是实现文档。实现见 `taskvm/domain/`、`taskvm/kernel/`。

## 0. 一句话

Kernel 是 TaskVM 的状态机器：它持有"TaskVM 当前相信的任务世界"，并把每一次被接受的变更变成一条事件。它不是 Flask server、不是 GUI driver、不是模型调用器。

## 1. 依赖规则（由 tests/architecture 强制执行）

```text
taskvm.domain   → 仅标准库
taskvm.kernel   → 仅 taskvm.domain + 标准库
禁止（domain/kernel）：flask / playwright / openai / requests / aiohttp /
  benchmark / evaluation / 具体 substrate / harness / workspace_ui /
  execution / apps / baselines / _migration
```

## 2. 领域类型（`taskvm.domain`）

| 概念 | 类型 | 要点 |
|---|---|---|
| 意图 | `TaskIntent(goal, constraints, scope, success_criteria)` | 只能被 GoalPatch 改变 |
| 任务变量 | `TaskVariable(semantic_key, label, value, value_type, mutability, confidence, evidence)` | `semantic_key` 是跨层唯一身份 |
| 任务状态 | `TaskState(intent, variables, revision)` | 不可变；revision 由 kernel 单调分配 |
| 表面句柄 | `SurfaceHandle(handle_id, opaque_token)` | TaskVM 自有短期句柄；token 不透明，禁止数据库主键语义 |
| 可见证据 | `SurfaceEvidence(surface, visible_label, visible_context, observed_value, confidence)` | 只装用户肉眼可见的内容 |
| 投影结构 | `ProjectionSchema(root_id, components, revision)` | 稳定组件树；只在 (re)composition 时变 |
| 投影数据 | `ProjectionData(values, node_status, progress, revision)` | 高频变化；与 schema 独立计数 |
| 工作流 | `WorkflowGraph(nodes)`；`WorkflowNode(node_id, kind, label, depends_on, parent_id, contract, verification, termination_predicate, max_iterations)` | `NodeKind ∈ {SEQUENCE, FAN_OUT, BARRIER, BOUNDED_LOOP, ACTION, VERIFY, CHECKPOINT, TERMINAL}`；bounded loop 必须同时带终止谓词 + max_iterations |
| 节点状态 | `NodeStatus ∈ {PENDING, READY, RUNNING, COMMITTED, FAILED, INVALIDATED, COMPENSATED}` | 状态住在 WorkflowStore，不住在图里 |
| 动作合同 | `ActionContract(contract_id, semantic_goal, desired_state, completion_condition, target_evidence, reversibility, risk_note)` | 跨层唯一工作单元；**禁止** app 内部动词 / 存储主键 / 平台 selector |
| 可逆性 | `Reversibility ∈ {REVERSIBLE, PARTIALLY_REVERSIBLE, IRREVERSIBLE}` | 不可逆 ⇒ `requires_confirmation` |
| 补丁 | `LocalPatch(variable_updates, node_overrides)` / `GoalPatch(new_intent)` / `CompensationPatch(target_checkpoint_id, observed_before)` | 类型即语义；`requires_replan(patch)` 只有 GoalPatch 为 True |
| 补偿计划 | `CompensationPlan(plan_id, target_checkpoint_id, entries, epoch)` | kernel 对 CompensationPatch 的校验结果 |
| 事件 | `Event(event_id, session_id, kind, revision, epoch, timestamp, correlation_id, payload)`；`EventKind` 覆盖 handoff 02 列出的全部 17 类 | 每次 kernel 变更恰好一条 |

## 3. Kernel 服务（`taskvm.kernel.TaskVMKernel`）

一个 session 一个 kernel 实例。全部方法线程安全。

### 初始化与观察

- `TaskVMKernel(session_id, intent)`
- `init_task_state(variables, *, correlation_id="") -> TaskState` — 装入 State Compiler 的输出
- `set_plan(graph, schema=None, *, correlation_id="") -> WorkflowGraph` — 装入 Task Architect 的计划（+ 可选投影 schema），发 `PlanCreated`
- `apply_observation(values, evidence=(), *, correlation_id="") -> TaskState` — 折叠新观察；**未知 semantic_key 直接拒绝**（结构发现属于 re-composition）

### 动作生命周期（epoch 盖戳）

- `request_action(node_id) -> {action_id, node_id, epoch, contract, verification}` — 仅 READY 的 ACTION/VERIFY 节点
- `start_action(action_id)` — 节点 → RUNNING
- `finish_action(action_id, *, observed_values=None, evidence=()) -> bool` — **stale epoch 一律丢弃**：不改状态、发 `ActionDiscarded`、返回 False、节点回 READY
- `record_verification(node_id, passed, *, detail="")` — RUNNING → COMMITTED / FAILED；commit 会传递解锁下游
- `requeue(node_id)` — FAILED → READY（重试）

### Checkpoint

- `commit_checkpoint(checkpoint_id, label) -> CheckpointRecord` — 钉住 event_index + state_revision + epoch + 变量快照 + 已提交节点集合

### 治理补丁

- `apply_local_patch(patch: LocalPatch) -> {epoch, requires_replan: False}` — 只允许改已声明变量 + 未提交节点的合同；提升 epoch（在途工作已过时）
- `apply_goal_patch(patch: GoalPatch, new_graph=None, new_schema=None) -> {epoch, requires_replan: True, intent_changed, graph_revision}` — 提升 epoch；`new_graph` 中已提交节点必须**逐字段原样携带**，否则抛 `CommittedNodeViolationError`；不传 `new_graph` 表示上层必须先 replan 再继续执行
- `request_compensation(patch: CompensationPatch) -> CompensationPlan` — 用 kernel **自己记录**的 checkpoint 快照校验 `observed_before`（不符抛 `CompensationMismatchError`）；生成逐变量回退项
- `record_compensation_result(plan_id, applied, *, observed_values=None, detail="")` — `applied=True` **必须**附最新观察值（诚实回退：记录现实，不记录计划回声）；checkpoint 之后提交的节点标记 COMPENSATED；失败发 `CompensationFailed`，状态不被假装回滚

### 治理事件与冲突

- `request_governance(action, detail="")` — pause（提升 epoch）/ resume / 其他治理动作
- `record_conflict(description, semantic_keys=()) -> correlation_id` / `resolve_conflict(resolution, *)`

### 只读快照（全部防御性深拷贝）

- `task_state()` / `projection()` / `workflow()` / `checkpoints()` / `events()` / `epoch` / `session_id`

## 4. Kernel 保证的不变量（测试锁定）

1. 所有 revision 由 store 分配、严格单调（`tests/kernel/test_kernel_invariants.py::test_state_revisions_monotonic`）。
2. 投影 schema/data revision 独立（`test_schema_and_data_revisions_are_independent`）。
3. GoalPatch 不得静默改写/丢弃已提交节点（`test_goal_patch_cannot_*`）。
4. stale epoch 的 ActionFinished 不改变 TaskState（`test_stale_epoch_action_result_is_discarded`）。
5. checkpoint 引用确定的 event/revision 边界（`test_checkpoint_pins_event_revision_boundary`）。
6. CompensationPatch 的 before 必须匹配 kernel 记录的历史观察（`test_compensation_rejects_fabricated_before_values`）。
7. store 对外只给不可变快照/防御性拷贝（`test_snapshots_are_defensive_copies`）。

## 5. 迁移表（旧类型 → 新类型）

| 旧（将被删除） | 新 | 说明 |
|---|---|---|
| `task_state.representation.TaskStateGraph` | `domain.TaskState` | var_id → semantic_key |
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
