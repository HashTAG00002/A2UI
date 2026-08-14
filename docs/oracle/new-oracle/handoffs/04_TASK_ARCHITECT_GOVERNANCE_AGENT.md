# Coding Agent C：State Compiler、Task Architect 与 Governance/Replan

> **分层协议（冻结）**：见 [docs/contracts/layered_ownership_protocol.md](../../contracts/layered_ownership_protocol.md)。**内容合法性由生产者负责。** 你 own `TaskArchitecture` 的静态合法性：workflow 三 primitive shape（SEQUENCE 单链 / FAN_OUT lane 独立 / BARRIER fan-in / terminal 唯一 sink / 无 orphan）、ActionContract.desired_state keys ⊆ variables、contract desired 与 TaskVariable desired 一致（split-brain guard）、ProjectionSchema binding ⊆ variables、duplicate key。`TaskArchitecture.validate()` 在**构造时一次性**自我合法；Kernel 不再遍历 plan 重查 shape/key/binding/split-brain。

## 你的唯一任务

把初始化阶段的多个零散模型角色合并成清晰的两步高层智能：

1. State Compiler：可见观察 → 任务相关状态与 binding evidence。
2. Task Architect / Projection Composer：目标 + task state → milestone、workflow topology、projection schema、action contracts。

同时实现 LocalPatch / GoalPatch / CompensationPatch 的治理语义和受影响未来重规划。不要负责 CUA 具体点击，不负责 Flask 页面视觉。

依赖：Agent A 的 domain/kernel contract；使用 Agent B 暴露的 Observation 类型，但不要 import 具体 substrate。

---

## Owned paths

```text
taskvm/architect/**
taskvm/governance/**      # 重构后可删除旧目录或变为 facade
tests/architect/**
tests/governance/**
docs/contracts/architect.md
```

只通过公开接口修改 kernel，不修改 substrate/projection/execution 实现。

---

## 当前必须移除的问题

- `task_state/compiler.py` 直接依赖 benchmark fixture/model client。
- `governance_interpreter.py` 根据 scripted events 和 GT binding 用规则猜 Sequential/Parallel/Loop。
- `_suggest_milestones()` 与 workflow topology 分开调用，milestone 只影响显示。
- `scripted_driver.py`、`ui_sim_driver.py`、`human_driver.py` 混入 production governance。
- `subgoal_generator.py` 对每个旧 PatchOp 调模型生成两个候选，增加 `2N` 调用，而且生成文本没有稳定传到 CUA。
- `replanner.py` 仍是 stub。
- model-facing prompt 包含 `entity_id`、operator、内部字段名。

---

## State Compiler

输入只能是：

- 用户目标与约束；
- Substrate Observation 中的截图、可见文本、a11y/DOM 可见结构；
- 已有 TaskVM-owned handle cache；
- 过去已确认的语义状态。

输出：

- task variables；
- visible surface evidence；
- semantic relation/binding hypotheses；
- confidence 与 ambiguity；
- 新/复用/失效 handle；
- 是否需要用户澄清或 slow-path re-observe。

禁止输入输出 DB primary key、内部 API operator、fixture answer。

### Fast path / slow path

- 结构指纹未变、已知 handle 的可见值变化：确定性更新，0 次模型调用。
- 局部结构变化但能通过既有 label/role/fingerprint 恢复：确定性 rebind，0 次模型调用。
- handle 消失、同名歧义、任务相关结构新增或彻底漂移：增量 State Compiler 调用。

不要每 5 秒完整重新编译整个世界。

---

## Task Architect / Projection Composer

一次调用输出同一个 coherent artifact：

```text
TaskArchitecture
  milestones/checkpoints
  workflow topology
  projection schema
  action contracts
  risk/reversibility declarations
  verification intents
```

### 支持的 workflow primitive

只做：

- Sequence；
- Fan-out → Barrier/Fan-in；
- Bounded Loop。

Loop 必须有状态驱动 termination predicate 和 `max_iterations`。不做 nested arbitrary graph DSL。

### Projection schema

- 使用业务可见 label；
- 明确只读/可写；
- 显示影响范围、执行状态、验证状态、不可逆性；
- 绑定 semantic variable/action contract，而不是 internal ID/operator；
- 普通 data delta 不重新生成 schema。

---

## 删除独立 SubgoalGenerator

最终 CUA instruction 由 `ActionContractSerializer` 确定性生成，例如：

```text
目标对象的可见标签与上下文
希望改变的业务字段
目标值
完成条件
风险/不可逆提醒
```

不要调用模型做语言润色；不要生成两个 candidate；不要让 Task Architect 为每个低层 GUI step 写 prompt。

将旧 `SubgoalInstruction.natural_language` 迁移为 action contract 的只读 presentation，不再是单独模型产物。

---

## Governance 事件

实现统一入口：

```text
PauseRequested
ResumeRequested
LocalPatchRequested
GoalPatchRequested
RollbackRequested
ConflictResolutionRequested
```

### LocalPatch

- 不改变 terminal success predicate、scope、topology；
- 更新受影响 action contract/node revision；
- 提升 execution epoch，防止旧请求继续落地；
- 默认不调用 Task Architect。

### GoalPatch

- 改变目标终点、范围、约束、milestone 或拓扑；
- 保留 committed nodes 与仍有效 checkpoint；
- 计算 affected future；
- 只向 Task Architect 提供当前 observed state、已提交历史摘要与新的 goal；
- 产生 `PlanPatch`，包含 keep/invalidate/add/rewire；
- 不允许清空所有历史再重新 seed。

### CompensationPatch

由 runtime/rollback owner 执行；本模块只负责把用户“回到 checkpoint X”的治理意图解析为 kernel command，不重新规划业务目标。

---

## 模型调用预算

实现统一 `ModelCallLedger`/budget policy（接口可由 kernel 提供）：

- 初始 State Compiler：通常 1 次，必要时 1 次 repair；
- 初始 Task Architect：1 次，schema repair 有严格上限；
- 普通 value delta：0 次；
- LocalPatch：0 次 architect；
- GoalPatch：1 次受影响未来重构；
- structure drift：只有 slow path 才调用 incremental compiler。

所有调用记录 role、purpose、tokens、latency、revision、是否 repair。禁止模块自己静默 retry 数十次。

---

## Prompt 安全 Gate

写测试检查所有 model message：

- 不包含 `entity_id`、`saga_id`、`move_event`、`set_deadline` 等内部词；
- 不包含 benchmark fixture object；
- 不包含不可见 DOM data attributes；
- 只包含必要的可见证据和 task-level semantics。

测试必须读取实际构建后的 message，而不是只 grep prompt template。

---

## 删除/迁移

生产目录中删除或迁移到 `tests/fakes/`：

```text
governance/scripted_driver.py
governance/ui_sim_driver.py
governance/user_behavior_driver.py
```

删除独立 LLM `subgoal_generator.py`，或仅保留无模型的 `ActionContractSerializer` 并改名。

废弃 rule-based `_classify_workflow()` 作为生产 planner；可保留在测试 fixture builder 中。

---

## 测试场景

1. 初始化：一次 architect 输出 milestone + fan-out + projection schema。
2. 值变化：schema 不变、architect call count 不增加。
3. LocalPatch：只改一个 action contract，不改 milestone/topology。
4. GoalPatch：保留已 commit 节点，只替换未来并产生新 schema revision（仅当结构变化）。
5. Compensation request：不调用 architect。
6. Bounded loop：当前状态决定下一迭代；无匹配项或达到 max 后终止。
7. Prompt no-leak。
8. Invalid model output 有有限 repair，最终失败可解释，不 fallback 到 GT plan。

---

## 明确不做

- 不直接操作浏览器。
- 不实现 Flask/SSE。
- 不读取 hidden oracle。
- 不做任意通用 DAG。
- 不通过 app/operator registry 规划。
- 不保留 mock flag 控制生产是否调用模型。

---

## 验收

```bash
pytest -q tests/architect tests/governance tests/architecture
```

交付报告必须给出至少三个真实序列化 artifact 示例：Sequence、Fan-out/Fan-in、Bounded Loop；但示例中不得出现内部 ID/operator。
