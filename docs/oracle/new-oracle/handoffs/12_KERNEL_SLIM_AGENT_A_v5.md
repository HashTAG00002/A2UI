# Coding Agent A v5：Kernel 瘦身（按分层协议下放内容校验）

> **适用分支**：从 `gg-phase@bf60e0c`（v4）开新分支。
> **唯一目标**：把 kernel.py 从 1194 行瘦到 ≤ 600 行，把"内容/静态"校验下放到 domain 构造器与 typed result，Kernel 只保留"时序/状态"不变量。**不新增 feature，不碰 substrate/runtime/architect/projection/benchmark/frontend/README。**
> **冻结原则**（见 [docs/contracts/layered_ownership_protocol.md](../../contracts/layered_ownership_protocol.md)）：**内容合法性由生产者负责；时序合法性由 Kernel 负责。一个性质只有一个 owner，不在下一层重证。**

## 先阅读（顺序重要）

1. `docs/contracts/layered_ownership_protocol.md` —— 本任务的**冻结 spec**，§4 保留清单 / §5 下放清单 / §6 disposition 表是机械执行依据。
2. `docs/contracts/kernel.md` —— Kernel 公共合同（瘦身后更新到 v5）。
3. 当前 `taskvm/kernel/kernel.py`（1194 行）、`taskvm/kernel/workflow_store.py`、`taskvm/kernel/checkpoint_store.py`、`taskvm/kernel/session_store.py`、`taskvm/kernel/projection_store.py`。
4. `taskvm/domain/*.py`（构造器校验的落点）。
5. `tests/kernel/test_adversarial_contracts.py` + `tests/kernel/test_v4_audit_fixes.py`（要拆分：时序留、内容移走）。

## 你的唯一任务

### A. 下放内容校验到 domain 构造器 / typed result（删 Kernel 侧的同一改动里补等价）

1. **`TaskArchitecture.validate()` / `domain/workflow.py` 构造器** 拥有 workflow 静态合法性：SEQUENCE 单链、FAN_OUT lane 互不依赖、BARRIER 精确 fan-in、terminal 唯一且 sink、无 orphan。删 Kernel 的 `_check_primitive_shapes` 等价物。
2. **`TaskArchitecture` 构造器** 拥有 ActionContract.desired_state.keys ⊆ variables、以及 contract desired 与 TaskVariable desired 一致（split-brain guard）。删 Kernel 的 `_validate_composition_locked` 里的 contract/binding/split-brain 分支。Kernel 只装"已 validate 的 TaskArchitecture"。
3. **`domain/projection.py ProjectionSchema.__post_init__`** 已做引用完整性；补齐真 tree（cycle / root-no-parent / non-root-single-parent / unreachable）。Kernel 不再校验 schema。
4. **typed `CompensationResult`**：只能由 `CompensationPlan.entries` 构造，接口上无 `dict[str,Any]`、无塞 extra key 的能力。`record_compensation_result(plan_id, result: CompensationResult)` 改签；删 Kernel 的 extra-key/部分匹配 if 防御（等价由类型消除）。**§4 时序部分保留**：plan.epoch==current_epoch、single-use、fresh-observed 全匹配（这个"全匹配"是时序落地确认，不是内容校验——保留）。
5. **typed `VerificationResult`**（action_id, epoch, passed, observed_effect, evidence_ref）：替换 `record_verification(node, bool)` 为 `land_verification(result)`。Kernel 只查 action_id/epoch/lifecycle/node 允许 commit；**不**再比较 observed==desired（那是 E verifier 的内容职责）。
6. **`domain/patch.py`** 已拒 duplicate key；确认 LocalPatch/NodeContractOverride 构造器完整拒重。删 Kernel 等价检查。`NodeContractOverride` 收紧为"只能表达与 variable update 一致的 target-value retarget"，不能换整张 contract。
7. **frozen immutable 替代无差别 deepcopy**：跨层公共对象 `@dataclass(frozen=True)`（含 observed/desired 不可变容器）；Kernel 私有 mutable state 不暴露引用。删 `request_action` 的 `copy.deepcopy(node.contract)`（若类型已不可变则多余）、删 store write 边界的无脑 deepcopy。**保留**真正 mutable private state 的隔离。
8. **namespaced ID**：checkpoint/plan ID 由生成策略保证不撞 workflow node id（typed/前缀），删 Kernel 的 id-collision if。

### B. 修 v4 真时序 bug（Kernel-owned，§4，必须保留/补）

这些是 v4 的真实缺口（runtime probe 在 bf60e0c 执行确认），**不能因为"瘦身"删**，必须补：

1. `_TRANSITIONS` 加 `READY→FAILED`，但**只允许 VERIFY 走**（Kernel node-kind gate）。修 F1。
2. `land_verification`/ACTION 提交必须对应当前 epoch 下**已 FINISHED** 的 handle（不允许 start→verify 跳过 finish）。修 F2。
3. **GoalPatch 两阶段**：`apply_goal_patch` = bump epoch + 更新 intent + **invalidate 未提交 future + block execution**；`recompose(new_variables, new_graph, new_schema)` 是唯一闭环入口。GoalPatch 不再半装 graph/schema。修 F3a/b/c。
4. **compensation 从 committed action history 生成**（before/after @ action time），不从 state-vs-snapshot diff；IRREVERSIBLE 报 uncompensatable 不假还原。修 F5。补偿落地后按条目恢复 observed、按 checkpoint 全量恢复 desired + intent + structure（含 metadata：label/type/mutability，不仅 key set）。修 F12。
5. **rollback workflow rewind**：成功回退后 scheduler-visible frontier 恢复到 checkpoint 执行边界；同 intent/structure 路径确定性 rewind（不需 Task Architect），跨 GoalPatch 走 requires_recompose+invalidate_future。修 F6。Terminal 不能在被撤销 predecessor 后错误完成。
6. **CHECKPOINT node 先逻辑提交再拍 boundary**（自己在自己 committed_nodes 里）。修 F7。
7. **set_plan / init_task_state one-shot**：用 explicit initialized flag，不用"variables 非空"判。修 F8、F13a。
8. **checkpoint 稳定边界**：manual checkpoint 不允许 in-flight action（无 STARTED/RUNNING）。修 F9a。
9. observation batch duplicate semantic_key 在 domain 构造器拒（不静默 last-write-wins）。修 F13b。
10. `requeue` 只允许 ACTION/VERIFY；loop max-failure 不走 requeue。修 F13c。

### C. 测试拆分（移动，不删不弱化）

- `tests/kernel/` 只留时序/状态机对抗（epoch discard、exactly-once、atomicity、rewind、boundary、one-shot、two-phase、CHECKPOINT-self-in-snapshot）。
- 内容子集移到 `tests/domain/`（TaskArchitecture.validate、typed result 无法表达坏输入、schema tree、duplicate key）。
- 占位 `tests/architect/`、`tests/runtime/`、`tests/verifier/`（可空目录 + conftest，标注 owner），但 Kernel 不得顶替。
- 新增 `tests/integration/`（C→Kernel→E 接得上）的最小骨架。
- **禁止通过删/弱化断言让套件变绿。**

## 不得触碰（scope cap）

substrate/runtime/architect/projection UI/benchmark/evaluation/oracle/README/心智模型总清理/legacy substrate 反向 import/Flask/前端/CUA cancel token/Task Architect model call/killtest 删除。这些是各自 owner（B/C/D/E/07/08/10）的活。**你只封 Kernel contract。**

## 硬规则

1. **删一条 Kernel 内容检查 ⇔ 同改动在 domain 构造器/contract test 补等价**。B/C/E 现在不存在——所以下放落点是**现在就存在的 domain 类型**，不是"以后的人"。无构造器可承接的，保留薄 Kernel guard + `TODO(owner=...)`，不裸删。
2. **每改一处先写会失败的 adversarial test，再改实现**（审计要求）。
3. `pytest -q tests/domain tests/kernel tests/architecture` + `python -m compileall taskvm/domain taskvm/kernel` 必须全绿。
4. 时序不变量（§4 全部 15 条）在瘦身后必须仍被测试钉死。

## 验收（你提交时自查，审计复验）

- `git rev-parse HEAD`、`wc -l taskvm/kernel/kernel.py`（≤ 600）、上述 pytest/compileall 全绿。
- §4 时序不变量逐条有对应绿测试。
- Kernel 中不再存在"内容校验"代码（grep `_validate_composition_locked`/`_check_primitive_shapes`/extra-key if 应为空或已移到 domain）。
- 提交信息只写：`Kernel contract v5 slimmed per layered ownership protocol (content validation devolved to domain constructors + typed results; temporal invariants retained).` —— 不写"full complete"。

## 下一轮审计的边界（已写进 charter §8）

审计只做：(a) 逐条验证 §4 时序不变量堵死；(b) 查次生 regression。新 invariant category 走 RFC。审计不得再以"假设下游 hostile"扩 Kernel 防御面积——那是 producer owner 职责。**这就是止 treadmill 的闸门。**
