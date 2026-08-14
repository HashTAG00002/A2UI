# Coding Agent A：层级骨架、TaskVM Domain 与 Kernel 重构

> **分层协议（冻结）**：见 [docs/contracts/layered_ownership_protocol.md](../../contracts/layered_ownership_protocol.md)。**内容合法性由生产者负责；时序合法性由 Kernel 负责。** 你只保留 §4 的时序/状态不变量（epoch/lifecycle/atomicity/rewind/boundary/timeline）；**内容校验下放到 domain 构造器与 typed result**，不在 Kernel 重查。

## 你的唯一任务

建立整个重构的稳定骨架与跨层协议。你不负责实现具体 Web/Mobile substrate，不负责做前端视觉，不负责调用真实模型，也不负责写最终 benchmark。你的输出必须让后续四个 agent 可以在不互相读实现细节的情况下并行工作。

先阅读：

1. `00_README_MASTER_HANDOFF.md`
2. `01_MENTAL_MODEL_ALIGNMENT_REPLACEMENT.md`
3. 当前 `taskvm/task_state/`、`taskvm/vm_state/`、`taskvm/governance/vm_state.py`
4. 当前 `taskvm/execution/patch_compiler.py`、`rollback.py`、`workflow_executor.py`

不要把旧文档中的字段设计当成约束。

---

## Owned paths

你可以创建或修改：

```text
taskvm/domain/**
taskvm/kernel/**
taskvm/ports/**          # 若选择集中放跨层 Protocol
tests/domain/**
tests/kernel/**
tests/architecture/**
docs/contracts/**
pyproject.toml           # 仅包发现、测试工具与基础依赖
```

除迁移 import 所必需的最小兼容外，不修改：

```text
taskvm/substrate/**
taskvm/projection/** 或 workspace_ui/**
taskvm/architect/**
taskvm/runtime/**
taskvm/evaluation/**
```

---

## 当前问题

1. `task_state/entity_binding.py` 把 app、数据库 `entity_id`、operator registry 写入领域模型。
2. `task_state/compiler.py` 依赖 benchmark fixture、model client 和 harness observation，依赖方向倒置。
3. `PatchOp` 同时承载任务语义和 app-specific 写法，导致上层必须知道底层 operator。
4. workflow、checkpoint、rollback、projection 值分散在多个 dataclass 和 server session 内，没有一个明确 kernel。
5. `vm_state/` 与 `governance/vm_state.py` 重叠。
6. 没有可执行的 import boundary gate，后续 agent 很容易再次越层。

---

## 目标公共模型

请根据现有代码迁移，但最终必须具备等价于以下概念的纯领域类型。名字可以微调，语义不能偏离。

### Intent 与状态

```text
TaskIntent
  goal
  constraints
  scope
  success_criteria

TaskVariable
  semantic_key
  label
  value
  value_type
  mutability
  confidence

SurfaceEvidence
  surface_handle
  visible_label
  visible_context
  observed_value
  confidence

TaskState
  variables
  evidence
  revision
```

`SurfaceHandle` 是 TaskVM 自己产生的短期句柄；它可以携带 opaque token，但 domain 不知道 token 是否来自 DOM、坐标或 mobile node。禁止出现数据库 primary key 语义。

### Projection

```text
ProjectionSchema
  stable component/tree structure

ProjectionData
  current values/status/progress

ProjectionRevision
  schema_revision
  data_revision
```

Schema 与 data 必须分离，使普通值变化不要求重新生成 UI 结构。

### Workflow

只支持研究所需三种 primitive：

```text
SequenceNode
FanOutNode + BarrierNode
BoundedLoopNode
ActionNode
VerifyNode
CheckpointNode
TerminalNode
```

不要建设任意图编程语言。必须表达：依赖、ready 状态、执行状态、业务可见 label、验证条件、最大循环次数。

### Patch

```text
LocalPatch
GoalPatch
CompensationPatch
```

判定规则：是否改变 terminal success predicate / scope / workflow topology。

跨层 action 使用 `ActionContract`，只包含语义目标、可见定位证据、desired state、completion condition、risk/reversibility。不得包含 app operator 名或数据库 ID。

### Event

最少：

```text
ObservationReceived
StateUpdated
PlanCreated / PlanPatched
ActionRequested / ActionStarted / ActionFinished / ActionDiscarded
VerificationPassed / VerificationFailed
CheckpointCommitted
GovernanceRequested
ConflictDetected / Resolved
CompensationRequested / Applied / Failed
```

所有 event 有 session、revision/epoch、时间、correlation id。

---

## Kernel 服务

实现内存版即可，重点是接口和不变量：

```text
ProjectionStore
WorkflowStore
EventLog
CheckpointStore
TaskSessionStore
```

### 必须保证的不变量

1. revision 单调增加。
2. schema revision 与 data revision 独立。
3. 已 committed 的 workflow node 不能被 GoalPatch 静默改写；只能保留或产生明确 compensation。
4. stale epoch 的 ActionFinished 不能改变当前 TaskState。
5. checkpoint 必须引用确定的 event/revision 边界。
6. CompensationPatch 必须引用历史观察到的 before/after，不得从 oracle 获取。
7. 每个 store 对外返回不可变 snapshot 或 defensive copy。

---

## 依赖 Gate

写自动测试扫描 import，至少阻止：

```text
taskvm.domain  imports flask/playwright/openai/requests/benchmark/evaluation/substrate
taskvm.kernel  imports flask/playwright/openai/benchmark/evaluation/concrete substrate
taskvm.runtime imports benchmark/evaluation
taskvm.projection imports concrete substrate modules
taskvm.architect imports benchmark fixtures
```

推荐使用 Python AST，不要只 grep 字符串。Gate 失败必须给出文件和违规 import。

---

## 迁移策略

1. 新建纯 domain/kernel，不在第一步删除所有旧模块。
2. 为旧类型写短期 adapter，仅放 `taskvm/_migration/`，并标注删除日期/后续 owner。
3. 将新公开接口记录在 `docs/contracts/kernel.md`。
4. 不要让兼容层反向污染新类型；新目录绝不能 import 旧 `task_state/entity_binding.py` 或 `PatchOp`。
5. 提供至少一个纯内存 scenario test：
   - 初始化 task state；
   - fan-out workflow；
   - action success；
   - verification；
   - checkpoint；
   - LocalPatch；
   - GoalPatch 只替换未来；
   - stale epoch result 被拒绝；
   - CompensationPatch 恢复 observed before。

---

## 明确不做

- 不实现 OpenAI prompt。
- 不实现 CUA/Playwright。
- 不实现 Flask/SSE/UI。
- 不读取 benchmark fixture。
- 不实现通用任意 DAG、递归或 nested loop DSL。
- 不以 `entity_id`、operator registry 作为新领域模型。

---

## 验收

必须全部通过：

```bash
pytest -q tests/domain tests/kernel tests/architecture
python -m compileall taskvm/domain taskvm/kernel
```

并人工检查：

```bash
grep -R "entity_id\|move_event\|set_deadline\|read_canonical" taskvm/domain taskvm/kernel
```

结果应为空或只出现在明确说明“禁止这些概念”的测试字符串中。

---

## 交付报告

提交：

- 新目录树；
- 所有公开 Protocol/dataclass；
- dependency gate；
- 迁移表：旧类型 → 新类型；
- 哪些兼容层必须由后续哪个 agent 删除；
- 测试真实输出；
- 不得宣称完整 E2E 已完成。
