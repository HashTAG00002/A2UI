# Architect Layer Contract — L4 State Compiler / Task Architect / Governance

> 状态：**冻结**（2026-08-14，Agent C 交付）。Owner：Agent C。
> 分层协议见 [layered_ownership_protocol.md](layered_ownership_protocol.md)；审计规则见 [audit_charter.md](audit_charter.md) §2/§3。
> 一句话：**L4 只做两件高层智能——把可见世界编译成任务状态（State Compiler），把 goal + 状态编译成一个一致的任务架构（Task Architect / Projection Composer）；CUA 是唯一的其他模型角色。**

## 1. 角色收敛（本层存在的理由）

旧角色 **Milestone Suggester / rule-based Workflow Classifier / GenUI structural decoder / LLM SubgoalGenerator / Rollback NL Generator** 在生产路径上全部废除：

- milestones/checkpoints、workflow topology（Sequence / Fan-out–Barrier–Fan-in / Bounded Loop）、projection schema、semantic action contracts、risk/reversibility、verification intent —— **由一次 Task Architect 调用共同产生**（同一 coherent artifact，不存在第二套 topology 逻辑）。
- CUA instruction 由 `ActionContractSerializer` **确定性序列化**（0 次模型调用），不是模型产物。
- Compensation（rollback）意图由 kernel 的已提交动作历史驱动（`CompensationPatch` → `kernel.request_compensation`），**没有任何"逆操作文案生成"模型调用**。

## 2. Public surface（`taskvm.architect` facade）

```text
CompilerObservationView / VisibleRegion / HandleEvidence   输入 DTO（唯一观察类型）
StateCompiler.compile(...) -> CompilerResult               可见世界 → 任务变量 + 证据
StateCompiler.needs_slow_path(...) / extract_observed(...) 确定性 fast path（0 次调用）
TaskArchitect.compose(...)  -> taskvm.domain.TaskArchitecture   一次调用 → 完整架构
TaskArchitect.recompose_future(...) -> RecomposeProposal   GoalPatch 后只重构受影响未来；proposal 携带历史闭包，最终经 kernel.recompose() 原子安装
ActionContractSerializer.cua_goal(...) / compensation_goal(...) / patchop_cua_goal(...)
ModelPort (Protocol) / HttpModelPort                        模型端口 + stdlib HTTP 适配器
ModelCallLedger / ModelCallRecord / MODEL_ROLE_*            调用计量（见 §5）
```

`taskvm.governance` 是 L4 的治理入口薄层：`GovernanceService.handle(event)` 把六类治理事件（Pause/Resume/LocalPatch/GoalPatch/Rollback/ConflictResolution）映射到 kernel facade 命令与（仅 GoalPatch 时的）一次 architect 重构。

## 3. 输入 DTO 合同（C 不耦合 B）

- `CompilerObservationView` 是本层**唯一**接受的观察类型：纯 domain 类型（可见文本 / 截图 data-url / surface 可见标识 / observation revision / handle 证据），**不 import 任何 substrate**。
- B 的 `SubstrateObservation` 由 composition/runtime 层**确定性转换**为本 DTO（类比 Ethernet frame → IP packet）。
- architecture gate：`taskvm/architect` allowed = `{taskvm.domain, taskvm.kernel}` + stdlib。**保持不动，不得放宽。**

## 4. C 拥有的生产义务（A 拥有校验，C 拥有生产）

C **复用** `TaskArchitecture` validating 构造器（A 的单一校验真源），不另起一套 validate：

1. 每次 `compose` / `recompose_future` 的产出必须**通过** `TaskArchitecture(...)` 构造（shape / key ⊆ variables / binding ⊆ variables / split-brain / orphan 全由 A 的 ctor 证明）；组装失败 → **有限 repair**（默认 1 次，把 ValidationError 反馈给模型）→ 仍失败则**诚实抛错**，绝不 fallback 到 fixture/GT plan。
2. `observed` 平面只来自观察：architect 产出的 variables 与 compiler 的 observed/evidence **确定性合并**（architect 只能贡献 desired/mutability/label，不得改写 observed）。
3. GoalPatch 重构：新 graph 必须**原样携带** kernel 判定的历史节点（同 id 同定义，`replace_future` 强制），只替换未提交未来；不允许清空历史重新 seed。
4. LocalPatch：**0 次** compiler/architect 调用（kernel `apply_local_patch` 确定性 retarget）。
5. 普通 observation value 更新：**0 次**高层调用（`StateCompiler.extract_observed` 确定性抽取 → `kernel.apply_observation`）。

## 5. 模型调用预算（`ModelCallLedger`，benchmark 可审计）

角色常量：`MODEL_ROLE_STATE_COMPILER = "state_compiler"`、`MODEL_ROLE_TASK_ARCHITECT = "task_architect"`、`MODEL_ROLE_CUA = "cua"`（CUA 侧由 Agent E 接入记账）、`MODEL_ROLE_MODEL_VERIFIER = "model_verifier"`（PURETY-GEN：模型化验证 `taskvm/verifier/model_verifier.py` 的记账角色——verifier 包以结构兼容的 record 行记账，不 import architect 层）。

| 事件 | state_compiler | task_architect |
|---|---|---|
| 初始化 | 1（+至多 1 repair） | 1（+至多 1 repair） |
| 普通 value delta | 0 | 0 |
| LocalPatch | 0 | 0 |
| GoalPatch | 0 | 1（受影响未来重构） |
| Rollback (CompensationPatch) | 0 | 0 |
| 结构性 binding 失效（slow path） | 1 增量 | 视变量/拓扑影响 0–1 |

每条记录含 role / purpose / model / tokens / latency / revision / is_repair / ok。**HttpModelPort 无内部 retry（C-2）**：一次 `complete_json` = 一次真实 provider request = 一条 ledger record；JSON parse 失败返回 `parsed=None`，由 L4 semantic repair loop 作为唯一 repair owner 决定是否重问（其每次尝试各自落账，`is_repair=True`）。CUA 调用由 E 的 runtime 记账（同 ledger 接口；runtime→architect 的 import gate 由 E 走 RFC，不在本 wave）。**生产路径无 `mock=True/False` 分叉**：Fake 只存在于 `tests/fakes/`。

## 6. Prompt no-leak gate（GG 红线 §0 的 L4 执行）

所有进入模型的 message（compiler 与 architect，system+user）构造后、发送前经 `noleak.scan()`：**包括每个 repair 轮次的完整 message（C-1）**；禁止 `entity_id`、DB key（`E1/T1/wxid_*` 型 token）、内部 operator 词表、`data-*-id`、深链内部路径。命中即 `PromptLeakError`（诚实失败，非静默剔除）。模型**输出**中的 semantic_key 同样过扫描（防模型回声内部 id）；leak 类错误的 repair note 只陈述错误类别，**绝不向模型复述 offending token**（`noleak.LEAK_REPAIR_GUIDANCE`）。测试读**实际构建的 message**（含 repair 轮次），不是模板 grep。

## 7. 治理事件 → kernel 映射（三类 mutation 的最终边界）

| 事件 | kernel 命令序列 | 高层调用 |
|---|---|---|
| `LocalPatchRequested` | `apply_local_patch`（retarget + epoch bump） | 0 |
| `GoalPatchRequested` | `apply_goal_patch`（invalidate future + block）→ architect `recompose_future` → `recompose`（原子闭合并解 block） | 1 architect |
| `RollbackRequested` | `request_compensation`（历史驱动 plan）→ runtime 执行 → `record_compensation_result` | 0 |
| `Pause/ResumeRequested` | `request_governance` | 0 |
| `ConflictResolutionRequested` | `record_conflict` / `resolve_conflict` | 0 |

合法重新调用高层 compiler/architect 的仅有：初始 goal、GoalPatch、结构性 binding 失效、真正影响 topology 的环境变化。

## 8. 禁止（违反即 contract violation）

- `taskvm.architect` import substrate / benchmark / evaluation / governance 旧模块（gate 强制 + 评审）。
- 生产路径出现 fixture `task_id`、`task.bindings`、scripted event 分类器（它们只活在 `tests/fakes/`）。
- 任何模型输入含内部 id / operator / get_state 产物（§6 gate）。
- Kernel 内部模块 import（`taskvm.kernel.*_store` / `event_log`）——只走 facade。

## 9. 测试锚点（每条合同 ≥1 绿测试）

`tests/architect/test_*`：DTO 纯度、compose 一次成型（三 primitive 各一）、repair 上限、no-leak（实构 message）、ledger 计数、serializer 确定性。
`tests/governance/test_*`：LocalPatch 0 调用、GoalPatch 保留历史只换未来（1 architect 调用、epoch 语义）、Compensation 0 调用且 entries 来自历史、pause/resume/conflict 事件。
`tests/architecture/test_import_boundaries.py`：gate 保持原样。

## 10. 已知边界（不在本 wave，路由给对应 agent）

- `workspace_ui/genui_decoder.py`（GenUI structural decoder 文件本体）与 `server.py` god module 的重写：Agent D（本 wave 只拔掉 `_suggest_milestones` 角色与 scripted planner 接线，保持 legacy demo honest-fail）。
- CUA 执行侧（`gui_executor`）的全面 kernel 化：Agent E（本 wave 只把 SubgoalGenerator 热路径替换为确定性 serializer 并删除 `mock_subgoal` 分叉）。
- `taskvm/task_state/**` legacy 栈与 evaluation killtests 的清除：Agent B/F 各自 wave。
