# Layered Ownership Protocol — 冻结基线

> 状态：**冻结**（2026-08-14，Wave-A.5 slim）。本页是所有 Agent 的开工约束。Kernel contract 见 [kernel.md](kernel.md)；本页规定**谁负责证明什么**，从而规定 Kernel 该有多瘦。
> 一句话原则：**内容合法性由生产者负责；时序合法性由 Kernel 负责。一个性质只有一个 owner，不在下一层重证。**

## 0. 为什么有这一页

Wave-A.2（v4）把 Kernel 当成 hostile-input 防火墙来审，导致 kernel.py 膨胀到 1194 行、两份"对抗 caller"测试 1100+ 行。但 B/C/D/E 是我们自己控制的模块，不是任意第三方插件。把"假设下游会喂坏东西 → Kernel 全部重查"换成"每个性质一个 owner + 类型无法表达坏输入"，Kernel 回到 STATE/TIME/HISTORY/TRANSITION 四件事，代码量与理解成本同时下降，且不损失任何核心性质。

**这不是降标准。** 是把标准放回正确的层，并用 typed construction + cross-layer contract test 证明接口拼起来仍成立。

## 1. 谁负责什么（单一 owner）

| 层 | owns（内容/静态） | 证明方式 |
|---|---|---|
| **B / Substrate** | Observation 格式、timestamp/revision、surface handle 是 TaskVM-owned、无 hidden DB ID、GuiAction 是真实 click/type/scroll | `tests/substrate/` contract test |
| **C / Architect** | semantic variables 唯一、ActionContract 引用变量存在、workflow 只有三种 primitive 且 shape 合法、fan-out lane 独立、barrier 精确 fan-in、loop 有 max、terminal 唯一且 sink、projection binding 指向存在变量、contract desired 与 architecture desired 一致（split-brain guard） | `TaskArchitecture.validate()` 构造时一次性 + `tests/architect/` |
| **D / Projection** | Governance command 的 HTTP/UI 输入 schema（LocalPatch/GoalPatch/checkpoint id 字段） | `tests/projection/` |
| **E / Runtime + Verifier** | before/after 来自执行前后新鲜观察、VerificationResult 来自独立 visible verifier、completion_condition 已查、不可逆能力正确报告、CompensationResult 只含当前 plan entries | `tests/runtime/` `tests/verifier/` |

| 层 | owns（时序/状态） | 为什么只能在 Kernel |
|---|---|---|
| **Kernel** | revision 分配、epoch stale-discard、one active action per (node,epoch)、terminal handle 不重复 land、committed history 不被 future rewrite、patch atomicity、pending governance/compensation 阻止 forward、checkpoint 钉住时间边界、compensation 从 Kernel 自己的 committed action history 生成、rollback desired/frontier/loop/timeline、observed/desired 平面权威、set_plan/init one-shot、GoalPatch 两阶段 | 这些都依赖 **Kernel 此刻自己的状态**；生产者发消息的那一瞬间无法保证 Kernel epoch 没被另一个 governance event 改掉。**只有 Kernel 能判。** |

## 2. 默认下放模式：让类型无法表达坏输入

不是"信任未来的 producer"，也不是"边界十个 if"。而是：

- 用 **typed result** 替代 `dict[str, Any]` + extra-key 防御：`CompensationResult` 只能由 `CompensationPlan.entries` 构造，接口上根本没有塞 `y:999` 的能力。
- 用 **validating domain constructor** 拥有静态合法性：`TaskArchitecture(...)` 构造时 `validate()`，Kernel 不再遍历整个 plan 重查 shape / key / binding。
- 用 **frozen immutable types** 拥有 alias 隔离：跨层公共对象 `@dataclass(frozen=True)`；Kernel 私有 mutable state 不暴露引用。不是"每个边界无脑 deepcopy"。

## 3. 硬规则：禁止把检查删成洞

> 一条 Kernel 内容检查，**只能**在**同一改动**里把它等价移进 domain 构造器或 producer/contract test 之后，才能从 Kernel 删除。绝不裸删后等未来 agent。

B/C/E 现在还不存在——所以"下放"的落点是**现在就存在的 domain 类型**（构造器校验、typed result），不是"以后的人"。任何目前没有 domain 构造器可承接的内容检查，**保留薄 Kernel guard + 标记 TODO-owner=该 agent**，不得裸删。

## 4. Kernel 保留清单（时序 — 不得删，否则打回）

由 runtime probe 验证（确认 bug 存在、v5 slim 后已修复），真实且 Kernel-owned：

1. revision 由 Kernel 分配、单调。
2. epoch stale result discard（finish_action / record_compensation_result 都查 epoch）。
3. one active action per (node, epoch)；terminal handle 不重复 land；result 不重写 committed node。
4. ACTION verify 必须对应当前 epoch 下已 FINISHED 的 handle（不允许 start→verify 跳过 finish）。
5. VERIFY 是唯一允许 READY→FAILED 的 kind（修复 _TRANSITIONS）。
6. committed history 不被 GoalPatch/recompose 静默改写/丢弃（ephemeral loop-body commit ≠ 历史）。
7. patch atomicity：先全量校验后变更；被拒零副作用。
8. pending compensation 阻止 forward timeline。
9. checkpoint 钉住 event-log index + state revision + epoch，且只在**稳定 action boundary** 拍（无 in-flight）；CHECKPOINT node 先逻辑提交再拍 boundary（自己在自己快照里）。
10. compensation 从 Kernel 自己的 committed action history（before/after @ action time）生成，不从 state-vs-snapshot diff；IRREVERSIBLE 报为 uncompensatable，不假还原。
11. rollback：恢复 desired / frontier rewind / loop counter / 跨 GoalPatch → requires_recompose；timeline COMPLETE/PARTIAL/FAILED；COMPLETE 截断 active future checkpoints。
12. observed/desired 双平面：observation 只写 observed，patch 只写 desired/intent。
13. set_plan / init_task_state one-shot；GoalPatch 后 execution blocked 直到 recompose。
14. compensation plan epoch-bound + single-use；stale/repeat → DISCARDED（独立 event，不混同执行失败）。
15. bounded loop 只经 termination decision 提交（begin/evaluate），不经 children auto-commit；max_iterations ceiling → FAILED+escalation。

## 5. 下放清单（内容 — 移进 domain 构造器 / producer）

| 原 Kernel 检查 | 下放到 | 落点 |
|---|---|---|
| workflow static shape（SEQUENCE 单链 / FAN_OUT lane 独立 / BARRIER fan-in / terminal 唯一 sink / 无 orphan） | `TaskArchitecture.validate()` / `domain/workflow.py` 构造器 | `tests/architect/` |
| ActionContract.desired_state keys ⊆ variables | `TaskArchitecture` 构造器 | `tests/architect/` |
| ProjectionSchema binding ⊆ variables + 真 tree（cycle/single-parent/unreachable） | `domain/projection.py ProjectionSchema.__post_init__`（已部分） | `tests/domain/` |
| contract desired 与 TaskVariable desired 一致（split-brain） | `TaskArchitecture` 构造器 | `tests/architect/` |
| duplicate LocalPatch VariableUpdate / NodeContractOverride key | `domain/patch.py LocalPatch.__post_init__`（已部分） | `tests/domain/` |
| compensation result extra-key / 部分匹配防御 | typed `CompensationResult`（按 plan.entries 构造） | `tests/domain/` + runtime |
| observation batch duplicate semantic_key | `domain` ObservedValue batch 构造器 | `tests/domain/` |
| 无差别 write-boundary deepcopy | frozen immutable 公共对象 + 私有 mutable state 隔离（不 deepcopy action contract 等小对象） | — |
| `record_verification(node, bool)` 裸布尔 | `land_verification(VerificationResult)` typed | runtime + kernel |
| checkpoint/plan ID 命名冲突 | namespaced ID 生成策略（typed） | producer |

## 6. kernel.py 瘦身 disposition（机械执行表）

| 区域 | 动作 |
|---|---|
| `_validate_composition_locked`（contract key / binding / split-brain） | **删** → 移进 `TaskArchitecture.validate()`；Kernel 只装已 validate 的对象 |
| GoalPatch `new_graph` desired-vs-variable 一致性 | **删 Kernel 侧** → `TaskArchitecture` 构造器 |
| `record_verification(node, bool)` | **改签** → `land_verification(VerificationResult)`；Kernel 只查 action_id/epoch/lifecycle |
| `record_compensation_result(plan, applied, observed_values=dict)` | **改签** → `record_compensation_result(plan_id, CompensationResult)`；typed，无 extra-key if |
| universal deepcopy（request_action contract / store write） | **删多余** → frozen 公共类型 + 私有 state 隔离；仅保留真正 mutable private state 的隔离 |
| GoalPatch two-phase（invalidate future + block until recompose） | **保留**（已实现，probe 确认）；slim 勿回归 |
| `_TRANSITIONS` READY→FAILED（VERIFY only） | **保留**（已实现，probe 确认） |
| workflow rewind after rollback（frontier/loop/timeline） | **保留**（已实现，probe 确认） |
| CHECKPOINT self-in-snapshot + stable-boundary | **保留**（已实现，probe 确认） |
| set_plan/init one-shot guard | **保留**（已实现，probe 确认） |

目标：kernel.py 1194 → ≤ 600 行。删除的每个检查必须在同改动里在 domain 构造器/contract test 补等价。

## 7. 测试去向

- `tests/kernel/`：**只留时序/状态机对抗测试**（epoch discard、exactly-once、atomicity、rewind、boundary、one-shot、two-phase）。`test_adversarial_contracts.py` / `test_v4_audit_fixes.py` 拆分：时序子集留，内容子集移 `tests/domain/`。
- `tests/domain/`：构造器校验、typed result 无法表达坏输入、schema tree、duplicate key。
- `tests/architect/`、`tests/substrate/`、`tests/projection/`、`tests/runtime/`、`tests/verifier/`：producer 侧 contract（占位可，但要在对应 agent 开工时填；不得让 Kernel 顶替）。
- `tests/integration/`：C→Kernel→E 协议接得上。
- **禁止通过删/弱化测试让套件变绿**；移动测试时断言不变。

## 8. 下一轮审计的边界（止 tread­mill）

审计只做两件事：(a) 逐条验证 §4 时序不变量被堵死；(b) 查次生 regression。新 invariant category 走 RFC（本目录一页），不算 audit finding。审计不得再以"假设下游 hostile"为由扩 Kernel 防御面积——那是 §1 的 producer owner 职责。
