# Coding Agent E：CUA Autonomy Runtime、同步、热中断、验证与真实回退

> **分层协议（冻结）**：见 [docs/contracts/layered_ownership_protocol.md](../../contracts/layered_ownership_protocol.md)。**内容合法性由生产者负责。** 你 own `VerificationResult` 与 `CompensationResult` 的内容合法性：before/after 来自执行前后新鲜观察、独立 visible verifier、completion_condition 已查、IRREVERSIBLE 能力正确报告、CompensationResult 只含当前 plan entries（typed，按 plan.entries 构造，无 `dict[str,Any]`）。Kernel 只查 action_id/epoch/plan 的时序与 lifecycle，**不重做 verifier**、不替你证明 freshness。Verifier 只存在一份（你），不 Runtime 验一次 Kernel 再验一次。

## 你的唯一任务

实现一个 substrate-neutral 的自治执行循环：TaskVM 在用户不介入时持续推进；每个 CUA action 后产生观察并更新 runtime；用户热插拔时阻止过时动作；验证后才 commit；回退只走真实 GUI compensation。

你不负责目标规划、projection 页面或 benchmark oracle。

依赖：Agent A kernel/events；Agent B SubstrateSession；Agent C ActionContract/Governance command。

---

## Owned paths

```text
taskvm/runtime/**
taskvm/verifier/**        # runtime-visible verifier；oracle verifier 留给 evaluation
tests/runtime/**
tests/verifier/**
docs/contracts/runtime.md
```

迁移/删除：

```text
taskvm/execution/**
```

不要修改具体 substrate 实现或 frontend。

---

## 当前必须解决的问题

- `workflow_executor.py` 只在 node 顶部检查 pause，node 内 CUA 不能热中断。
- `gui_executor.py` 使用共享 Playwright page/lock，generic execution 直接知道 Web controller。
- `action_dispatcher.py` 混有 API adapter、wrong-target/no-op kill-test。
- `_mutate_via_gui()` 外层最多三轮完整重试，内层 `max_steps=18`、`max_attempts=54`，理论调用成本失控。
- CUA 每步截图、Patch 前后 `read_canonical()`、SSE 5 秒 poll、workflow verifier 多套观察重复存在。
- rollback 仍能在 API adapter 下直接写回，before value 来自 hidden canonical。
- `replanner.py` 不工作；旧 subgoal 文本和实际 CUA instruction 双重生成。

---

## Autonomy Loop

实现统一状态机：

```text
select ready work
→ issue action contract
→ observe
→ CUA predicts one action
→ epoch/cancel check
→ substrate.act
→ observe resulting world
→ update Projection Store / Event Log
→ continue or declare action-contract done
→ runtime verification
→ commit / repair / escalate
```

用户不操作时 loop 自动继续，直到：

- terminal success；
- pause/stop；
- budget exhausted；
- unrecoverable verification failure；
- need-human decision；
- irreversible boundary policy 要求确认。

不要以 5 秒 heartbeat 驱动正在执行的 surface；CUA action 产生的 observation 是 active surface 的主同步信号。

---

## Execution Epoch 与热中断

每个 session 有单调 `execution_epoch`。

### 发模型请求

记录：

```text
request_id
session_id
epoch
state_revision
action_contract_revision
surface
```

### 收到回复

执行前再次检查：

```text
response.epoch == current_epoch
contract revision 仍有效
session 未 stop
```

否则记录 `ActionDiscarded(stale_epoch)`，绝不落地。

### 已经开始的动作

- 单个 click/type/key/scroll 是最小 atomic action；已经开始则完成该动作。
- 每个动作后立刻检查 cancellation/epoch，再决定是否进行下一步。
- 不可逆按钮执行前再做一次 epoch/policy 检查。
- soft pause：当前 atomic action 后暂停。
- emergency stop：不再启动下一 action，并将 session 标为需要重新观察。

写并发测试模拟“旧 CUA response 在 GoalPatch 后返回”，必须被丢弃。

---

## 调用预算与重试

废除三级无限放大的隐式预算。建议配置：

```text
max_actions_per_contract
max_invalid_predictions_per_contract
max_repairs_per_contract
max_model_calls_per_task
max_replans_per_task
wall_clock_budget
```

原则：

- provider timeout/invalid JSON 不计为真实 GUI action，但受小的 invalid prediction 上限约束；
- verifier failure 后最多一次上下文保持的 repair，不从首页完全重跑三次；
- repair prompt/contract 携带当前截图、已执行动作和 discrepancy；
- 达到预算后安全暂停并通知 UI，而不是继续盲跑。

删除 `GUI_WRITE_RETRIES=2` 形式的整轮三次默认重试。

---

## Observation-driven Synchronization

### Active surface

每个动作后：

1. substrate 返回 ActionReceipt；
2. 捕获 Observation；
3. 计算 visible delta/fingerprint；
4. 更新 Projection Store；
5. 发布 screenshot artifact/event；
6. 继续 CUA 或验证。

### Inactive surface heartbeat

低频观察 inactive surfaces：

- fingerprint 不变：0 模型调用；
- 已知 handle 的值改变：确定性 data delta；
- 结构/binding 失效：发布 `StructureInvalidated`，交给 State Compiler slow path。

heartbeat 是补漏，不是与 CUA observation 并行重复读取同一 active surface。

### Conflict

如果外部变化与 pending desired state 冲突：

- 不静默覆盖；
- 记录 current observed、desired、last committed；
- 暂停受影响 node/lane，不必暂停无关 lane；
- 发布 Governance event 供 UI 选择 keep world / apply desired / edit goal。

---

## Runtime Verification

区分两类 verifier：

### Runtime-visible verifier

使用新的可见观察检查：

- expected visible outcome；
- action contract completion condition；
- handle/field consistency；
- obvious unexpected visible diff。

用于控制 autonomy loop。

### Evaluation oracle verifier

hidden ground truth 只在 final benchmark 进程中判卷，不得被 runtime import。

论文可以同时报告两者，但不能把 oracle 当 Agent 的实时能力。

---

## Compensation / Rollback

内部名称用 CompensationPlan/CompensationEntry（v5 无 Saga 类型）；UI 不显示术语。

每个 committed action effect 记录：

```text
observed_before
observed_after
semantic action contract
compensation intent/capability
surface artifacts
checkpoint boundary
```

Rollback：

1. 选 checkpoint 后的 committed effects；
2. 逆序产生 CompensationPatch；
3. 通过同一个 SubstrateSession + CUA 执行真实 GUI 操作；
4. 每步重新观察和验证；
5. 可逆成功、部分失败、不可逆分别记录；
6. 不使用 snapshot、DB write、environment setState 或 API mutation。

Compensation instruction 可由 ActionContract 确定性形成，不额外调用 rollback NL 模型。

如果 before 状态没有在当时真实观察到，不得事后从 oracle 补写为“可回退”。

---

## Fan-out/Fan-in 与 Loop

### Fan-out

- 每条 lane 独立 runtime context/epoch child token；
- concrete substrate 可决定是否物理并行；
- UI/事件中明确 logical parallel 与 physical serial；
- barrier 只有所有 required lane verified 后通过；
- lane failure 不销毁其他已验证 lane。

### Bounded Loop

- 每轮重新观察状态；
- termination predicate 来自 workflow contract；
- `max_iterations` 必须硬限制；
- 每轮可产生 checkpoint/verification；
- 不支持递归/nested arbitrary loop。

---

## 删除与迁移

生产 runtime 删除：

- API dispatcher branch；
- no-op/wrong-target kill-test branch；
- app-specific adapter lookup；
- `read_canonical()` 前后验证；
- `gui_executor_async.py` 的 MobileGym 特有实现（迁到 substrate）；
- 独立 model SubgoalGenerator 调用；
- `replanner.py` stub（重规划由 Architect owner 实现）。

---

## 测试

1. Autonomy 在无用户事件时连续推进多个 node。
2. Soft pause 在下一 atomic action 前生效。
3. GoalPatch 使旧请求 response stale，未执行。
4. 已执行单动作后 emergency stop 要求重新观察。
5. Active surface 不再同时被 heartbeat 重复 poll。
6. Inactive surface fingerprint 不变时 0 模型调用。
7. Verifier failure 一次 context-preserving repair；预算到限安全停。
8. GUI-only compensation；不可逆明确 partial failure。
9. Fan-out lane 独立、barrier 正确。
10. Loop termination/max bound。

---

## T1 债务拆除（Oracle audit B-F1 指派给 E；2026-08-16 追加）

`docs/contracts/substrate.md` §8 Transitional Debt Register 的 **T1** 归你：
删除 `taskvm/execution/gui_driver.py` 整个文件（GUITaskAdapter /
MobileGymTaskAdapter / `make_task_adapters` / `_OP_FIELD` / `_ENTITY_KIND` /
`_WEB_APPS` / `_MOBILEGYM_APPS` / 过渡 `mobilegym_bridge_url`）。runtime 直接
消费 `SubstrateSession` + ActionContract→CUA→GuiAction。约束：

1. **保持 `workspace_ui` 可编译**：`server.py` 目前 import
   `make_task_adapters` 与过渡 `mobilegym_bridge_url`（B-2 显式债务）。你删
   文件的同时要么把 workspace bootstrap 接到真实 runtime（你的既有里程碑），
   要么与 Agent D 协调接线；不得让 main 分支编译断掉。
2. **三个 MobileGym killtest 脚本**（`run_mg_vm_killtest.py` 等）的写路径目前
   经 `MobileGymTaskAdapter.mutate_raw`：迁移到 bridge 的 CUA 路由或
   `MobileGymSubstrateSession`，或交给 Agent F 随 killtest 清理一起删除——
   不要为它们保留适配器。
3. 记住 bridge B-1 语义：runtime 路由（observe/act/mutate）要求评测面先激活
   sid（`POST /api/reset/<sid>`），mismatch 诚实 409。
4. 完成后同步收缩 `tests/substrate/test_no_api_backdoor.py` 的
   `TRANSITIONAL_DEBT_REGISTER`（T1 条目删除）+ `tests/test_imports.py` 中
   gui_driver smoke 测试一并删除；T2（workspace_ui anchor_lookup）归 D，
   若 D 已先完成，直接跑正式 LOCK 审计：
   `TASKVM_SUBSTRATE_LOCK_AUDIT=1 pytest tests/substrate -q` 全绿即 mechanical
   LOCK（登记表为空）。

不要重构 substrate 内部（B 已 owner-complete/code-frozen）；你只删上层旧路径。

---

## 验收

```bash
pytest -q tests/runtime tests/verifier tests/architecture
```

交付一份 event trace，覆盖：自治推进 → hot LocalPatch → stale response discard → verify → checkpoint → rollback。Trace 中不得出现 hidden database ID、app API 或 fixture answer。
