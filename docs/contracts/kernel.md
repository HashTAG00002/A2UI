# TaskVM Kernel 公共合同（L3）

> 状态：**冻结基线 v4**（Wave 1, Agent A；Wave-A.2 对抗审计 13 组反例全部修复后）。后续 agent 只消费本页列出的公开接口；需要新跨层接口时先在本目录提交一页 RFC（master handoff §8.2）。
> v4 相对 v3（审计组号对应 `tests/kernel/test_v4_audit_fixes.py`）：VERIFY 失败路径 READY→FAILED（G1）；ACTION 验证必须对应当前 epoch 已 FINISHED 的 handle（G2）；**GoalPatch 两阶段封闭转换**——`apply_goal_patch` 只 bump epoch + 改 intent + 作废旧未来 + 阻断执行，`recompose` 是唯一闭环入口且原子安装完整组合（G3）；LocalPatch 只剩 variable_updates 单一真源 + kernel 确定性 retarget，NodeContractOverride 删除（G4）；**补偿改为基于已提交动作历史**（action start/finish 时记录 before/after；IRREVERSIBLE 进 uncompensatable；外部漂移不回滚；不做逻辑删除）（G5）；补偿成功后 frontier 确定性倒回 checkpoint 边界（G6）；CHECKPOINT 节点先 logical commit 再拍 boundary（G7）；set_plan one-shot（G8）；checkpoint 稳定边界 + 撞名守卫（G9）；写方向防御性深拷贝（G10）；validator 锁死三 primitive + 单 TERMINAL（G11）；structure 比较含 label/type/mutability（G12）；initialized flag / 观察批重复 key 拒绝 / requeue 种类 gate / loop body ephemeral commit 语义（G13）。
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
上层只依赖 taskvm.kernel 公共 facade/snapshot；taskvm.kernel.*_store /
event_log 等内部模块禁止被 kernel 包外 import（gate 强制执行）。
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
| 工作流 | `WorkflowGraph(nodes)`；`WorkflowNode(...)` | `NodeKind ∈ {SEQUENCE, FAN_OUT, BARRIER, BOUNDED_LOOP, ACTION, VERIFY, CHECKPOINT, TERMINAL}`；bounded loop 双强制（终止谓词 + max_iterations）且 body 必须是 ACTION/VERIFY 直接子节点（无嵌套 loop） |
| 节点状态 | `NodeStatus ∈ {PENDING, READY, RUNNING, COMMITTED, FAILED, INVALIDATED, COMPENSATED}` | 状态住在 WorkflowStore；SEQUENCE/FAN_OUT 容器在全部子节点 COMMITTED 后自动 COMMITTED；**BOUNDED_LOOP 不自动提交**——只能被显式终止决定提交 |
| 动作合同 | `ActionContract(contract_id, semantic_goal, desired_state, completion_condition, target_evidence, reversibility, risk_note)` | 跨层唯一工作单元；**禁止** app 内部动词 / 存储主键 / 平台 selector |
| 可逆性 | `Reversibility ∈ {REVERSIBLE, PARTIALLY_REVERSIBLE, IRREVERSIBLE}` | 不可逆 ⇒ `requires_confirmation` |
| 补丁 | `LocalPatch(variable_updates)` / `GoalPatch(new_intent)` / `CompensationPatch(target_checkpoint_id)` | 类型即语义；`requires_replan` 只有 GoalPatch 为 True；**LocalPatch 只有 variable_updates 一个真源**（kernel 确定性 retarget 未提交合同）；**CompensationPatch 只有 target 一个载荷字段**（调用者无法提供/伪造历史） |
| 补偿计划 | `CompensationPlan(plan_id, target_checkpoint_id, entries, epoch, uncompensatable, requires_recompose)`；`CompensationEntry(node_id, semantic_key, from_observed, to_observed, to_desired, reversibility)`；`UncompensatableAction(node_id, semantic_keys, reversibility, reason)` | kernel 从**自己记录的已提交动作历史**（checkpoint 之后、LIFO）生成；IRREVERSIBLE 动作进 `uncompensatable` 诚实上报，绝不伪装成可回写；`requires_recompose=True` 表示回滚跨 GoalPatch/结构边界 |
| 事件 | `Event(...)`；`EventKind` 22 类（含 `NODE_COMMITTED` / `ACTION_REQUEUED` / `COMPENSATION_DISCARDED` / `LOOP_ITERATION_STARTED` / `LOOP_ITERATION_EVALUATED`） | 见 §3 的 mutation→event 表；EventLog 是 audit/history stream，不是 event-sourcing 框架 |
| 检查点记录 | `CheckpointRecord(checkpoint_id, label, state_revision, event_index, epoch, intent, structure, observed, desired, committed_nodes, created_at)` | **TaskVM logical checkpoint（非 App DB 快照）**：intent + 语义结构（key→label/type/mutability）+ 双平面值 + 已提交节点；现实恢复只能由 runtime 经真实 CUA compensation 完成 |

## 3. Kernel 服务（`taskvm.kernel.TaskVMKernel`）

一个 session 一个 kernel 实例。全部方法线程安全。

**Event semantics（固定）**：每个公开变更调用追加**恰好一条**事件，kind 命名该语义操作；`finish_action` 折叠的观察是动作落地的一部分（keys 在 ACTION_FINISHED payload 里），不产生第二条事件。

**节点推进协议（固定）**：
- `ACTION`（executable）：`request_action → start_action → finish_action → record_verification`。只有 ACTION 节点产生 CUA 工作句柄。Handle 生命周期 REQUESTED → STARTED → FINISHED/DISCARDED，**单次落地**；同一 (node, epoch) 最多一个 active handle；terminal handle 再落地抛 `ValidationError`；`finish_action` 只接受 STARTED + 当前 epoch；已提交节点不被旧结果改写。**`record_verification` 对 ACTION 要求当前 epoch 存在 FINISHED handle**——start→verify 直接提交会被拒（G2）。`start_action` 记录合同目标键的 before_observed，`finish_action` 记录 after_observed——这是补偿历史的原料（G5）。
- `VERIFY`（control）：READY 状态下直接 `record_verification(node, passed)` —— runtime 独立观察后上报，没有动作句柄。**VERIFY 是唯一允许 READY→FAILED 的种类**（G1）。
- `BARRIER / CHECKPOINT / TERMINAL`（control）：READY 状态下 `advance_control(node)`；CHECKPOINT 额外写 CheckpointRecord（fan-in 点即已验证边界）；TERMINAL 提交 = 计划完成。
- 容器（SEQUENCE/FAN_OUT）不需要显式推进：全部子节点 COMMITTED 后自动 COMMITTED。
- `BOUNDED_LOOP`：显式 loop 协议——`begin_loop_iteration(node) -> iteration`（READY→RUNNING，body 子节点重新武装为 READY）+ `evaluate_loop_termination(node, terminated)`（body 全部 COMMITTED 才可评估；True ⇒ loop COMMITTED；False 且未达 max ⇒ 回 READY 进下一 iteration；False 且已达 max_iterations ⇒ FAILED + payload `reason=max_iterations_exceeded` 升级信号）。body 只在 loop RUNNING 期间可调度。

### 组合（State Compiler / Task Architect 输出）

- `init_task_state(variables)` — 一次性装入初始变量；**one-shot 以显式 initialized flag 判定**（空变量集也是合法初始组合；G13a）
- `recompose(variables, *, reason, new_graph=None, new_schema=None)` — 结构级重组唯一入口，**也是 GoalPatch 后唯一的闭环入口**（G3）：GoalPatch 后 `new_graph` 强制；无 pending 时可省略 graph/schema，但**保留的旧 graph/schema 同样接受完整组合校验**（dangling 引用或 desired split-brain 一律拒）；先全量校验后变更；bump epoch；成功后执行闸打开
- `set_plan(graph, schema=None)` — 装入初始计划，发 `PlanCreated`；**one-shot**（G8）：二次调用抛 `ValidationError`，防止绕过 GoalPatch 保护擦掉执行历史

### 观察（自底向上投影）

- `apply_observation(observations: Iterable[ObservedValue])` — 只写 `observed` 平面；**未知 semantic_key 拒绝**（结构发现属于 `recompose`）；空 evidence 保留旧 evidence

### 动作生命周期（epoch 盖戳）

- `request_action(node_id) -> {action_id, node_id, epoch, status, contract}` — 仅 READY 的 ACTION 节点；同 (node, epoch) 已有 active handle 则拒绝
- `start_action(action_id) -> bool` — REQUESTED→STARTED；stale epoch → DISCARDED + False
- `finish_action(action_id, *, observations=()) -> bool` — 只接受 STARTED + 当前 epoch；stale epoch 或节点已进入历史状态（COMMITTED/COMPENSATED）⇒ DISCARDED + False
- `record_verification(node_id, passed)` — ACTION 需 RUNNING + 当前 epoch 的 FINISHED handle；VERIFY 从 READY 直接确认（可 FAILED）
- `advance_control(node_id) -> CheckpointRecord | None` — BARRIER/CHECKPOINT/TERMINAL
- `requeue(node_id)` — FAILED → READY，发 `ActionRequeued`；**仅 ACTION/VERIFY**（G13c：达 max 的 loop 必须交治理/新计划，不能假装可重试）

### Checkpoint

- `commit_checkpoint(checkpoint_id, label)` — 治理手势驱动的检查点；工作流 CHECKPOINT 节点走 `advance_control`。**稳定边界守卫（G9）**：存在 in-flight action / RUNNING 节点时拒绝；id 撞上工作流节点时仅当它是 READY 的 CHECKPOINT 节点才允许（等同于推进该节点）。**CHECKPOINT 节点先 logical commit 再拍 boundary record**（G7：节点属于它自己的边界）

### 治理补丁（全部 atomic：先全量校验，后变更；被拒则 state/epoch/graph/events 完全不变）

- `apply_local_patch(patch)` — 只改已声明且 **mutability=editable** 变量的 **desired**（单一真源，G4）；kernel 对所有未提交且引用这些 key 的 ACTION 合同做**确定性 retarget**（拓扑/evidence/reversibility/risk 不变；已提交历史不动）；重复 variable key 在构造期即被拒；成功时 bump epoch；返回 `retargeted_nodes`
- `apply_goal_patch(patch)` — **两阶段封闭转换的第一阶段（G3）**：bump epoch + 更新 intent + **作废全部未提交未来（INVALIDATED）** + 阻断执行（request/advance/loop/verify/checkpoint/local-patch 全部拒绝，直到 recompose 闭环）。**不再接受 new_graph/new_schema 参数**（传入即 TypeError）——不存在"半安装"状态
- `request_compensation(patch) -> CompensationPlan` — **从 kernel 自己的已提交动作历史生成**（G5）：只覆盖 checkpoint 之后 verified 的动作（before/after 是动作当时真实记录）；TaskVM 没碰过的外部漂移不产生回退项；IRREVERSIBLE 动作进 `uncompensatable`；intent/structure（含 label/type/mutability，G12）不同 ⇒ `requires_recompose=True`
- `record_compensation_result(plan_id, applied, *, observed_values=None, detail="") -> bool` — **epoch 绑定 + 单次落地**：stale ⇒ `CompensationDiscarded` + False；terminal plan 再落地抛错。`applied=True` **必须**附新鲜观察值且每个条目的最终目标值全匹配。全匹配后：折叠新鲜观察；恢复 checkpoint 变量的 desired + label/type/mutability（**不恢复陈旧 evidence**）；重新挂上结构上消失的 checkpoint 变量；**绝不逻辑删除后出现的变量**（G5）；恢复 checkpoint intent。工作流：同 intent/结构 ⇒ frontier 确定性倒回边界并重新武装同一路径（G6）；跨边界 ⇒ 被撤销的提交标 COMPENSATED + 剩余未来 INVALIDATED + 等待 recompose

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
| requeue | `ActionRequeued` |
| record_verification | `VerificationPassed/Failed` |
| advance_control（BARRIER/TERMINAL） | `NodeCommitted` |
| advance_control（CHECKPOINT）/ commit_checkpoint | `CheckpointCommitted` |
| apply_local/goal_patch | `PlanPatched`（payload.patch_class + invalidated_node_ids） |
| request/record compensation | `CompensationRequested/Applied/Failed/Discarded` |
| begin/evaluate loop | `LoopIterationStarted/LoopIterationEvaluated` |
| governance/conflict | `GovernanceRequested/ConflictDetected/ConflictResolved` |

## 4. Kernel 保证的不变量（测试锁定）

1. revision 由 store 分配、严格单调。
2. 投影 schema/data revision 独立。
3. GoalPatch/recompose 不得静默改写/丢弃已提交节点（loop 运行中的 body 提交是 ephemeral，不算历史）。
4. stale epoch 的 ActionFinished 不改变 TaskState。
5. checkpoint 引用确定的 event/revision/epoch 边界，且只能在稳定 action boundary 拍摄（无 in-flight）。
6. compensation 只由 kernel 自录的已提交动作历史推导（非快照 diff）；IRREVERSIBLE 诚实上报不可回退；applied 必须全量匹配新鲜观察；不做逻辑状态删除。
7. store 对外只给不可变快照/防御性拷贝；**写方向同样深拷贝**（含返回的 action handle）。
8. patch atomic：先全量校验后变更；被拒的 patch 零副作用（含 event log）。
9. observed/desired 双平面：观察只写 observed，补丁只写 desired；projection 可见 pending divergence。
10. action handle 单次落地：REQUESTED→STARTED→FINISHED/DISCARDED；同 (node, epoch) 最多一个 active handle；terminal handle 永远不能再落地；已提交节点不被旧结果改写。
11. compensation plan epoch 绑定 + 单次落地；stale/重复落地走独立的 `CompensationDiscarded`，不与执行失败混淆。
12. bounded loop 只能被显式终止决定提交：iteration 计数、termination 评估、max_iterations 强制（超限 ⇒ FAILED + escalation）。
13. 组合边界（set_plan/recompose）拒绝：引用未知 task variable 的 binding/contract key；**非豁免合同最终写者目标 ≠ 变量 desired（split-brain guard）**；多最终写者目标不一致。已提交历史节点豁免（其合同是冻结记录）。
14. **两阶段封闭**：set_plan/init_task_state one-shot；GoalPatch 后执行阻断直至 recompose 原子闭环；VERIFY 独占 READY→FAILED；ACTION 验证必须跟随当前 epoch 的 FINISHED handle。

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
