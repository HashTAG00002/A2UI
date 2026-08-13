# TaskVM Repository Agent Rules（建议替换根目录 `.mrules` 或 `AGENTS.md`）

> 本文件是 coding agent 的短期执行规则，不记录项目演化史，不追加 episode，不保存聊天摘要。研究目标以 `docs/A2UI_开工大纲_v0_心智模型对齐版.md` 为准；具体任务以当前 agent handoff 为准。

## 1. 最终目标

只交付三类产物：

1. 可运行的 TaskVM prototype；
2. 可用的用户前端；
3. 可复现的 final benchmark。

不要新增 phase、gate、kill-test 或中间 demo 作为长期架构。

## 2. 五个不可削弱的结果

- 实时任务投影；
- 双向真实执行；
- substrate independence；
- governance over autonomy；
- 独立验证与诚实可逆性。

## 3. 层级

```text
Projection UI
→ Task Architect
→ TaskVM Kernel
→ Autonomy Runtime
→ Substrate Port
```

Evaluation Plane 独立，只负责 reset/seed/oracle/metrics。

## 4. 依赖规则

- `domain` 无外部框架依赖。
- `kernel` 只依赖 `domain`。
- `runtime` 只依赖 domain/kernel/substrate port。
- `projection` 不 import concrete substrate。
- runtime/architect/projection/kernel 不 import benchmark/evaluation。
- substrate 不 import projection/server/governance。

## 5. Runtime 权限

- 只使用真人可获得的可见观察。
- 写操作只走真实 GUI gesture。
- 禁止 app mutation API、simulator state injection、snapshot restore。
- 禁止 runtime 依赖数据库 entity ID、fixture binding、operator registry。
- DOM/a11y 可作 Web 观察，但隐藏 data attributes 不进入模型或跨层 identity。

## 6. 模型角色

只保留：

- State Compiler；
- Task Architect / Projection Composer；
- CUA。

普通值变化不调用 Task Architect。禁止独立 LLM SubgoalGenerator 产生两个候选。

## 7. 用户治理

- 无用户操作时 CUA 应持续自治推进。
- LocalPatch 不改变最终目标/拓扑。
- GoalPatch 改变终点/范围/拓扑，只重构未来。
- CompensationPatch 回到历史 checkpoint。
- 热中断使用 epoch；旧 response 不得执行。

## 8. 工作流范围

只需：Sequence、Fan-out/Fan-in、Bounded Loop。不要建设任意通用工作流语言。

## 9. 前端

- Agentic GenUI 始终开启。
- Projection schema 与 data 分离。
- 值更新只推 data delta。
- 点击 surface card 显示已缓存最新截图，不触发执行/模型。
- 所有 route 使用统一 URL 生成并有 method tests。

## 10. Benchmark

- final benchmark 与 runtime 权限隔离。
- 同一 CUA 比较不同 harness。
- 报告成功、非干涉、成本、延迟、恢复、GoalPatch、rollback、OOD。
- 禁止 API executor 作为论文系统条件。

## 11. Coding 纪律

- 只修改 handoff 指定 owned paths。
- 需要跨层接口先提交小 RFC。
- 不新增 compatibility shim/fallback 逃避迁移。
- 不留 `NotImplementedError`、TODO 或双实现作为交付。
- 不弱化测试、不读取答案、不伪造结果。
- 每次提交报告改动、测试真实输出、局限和 diff stat。

## 12. 完成标准

代码可从干净环境启动；用户主要流程无 405/500；architecture gate、route test、runtime tests、final benchmark smoke 全部通过；README 和 docs 不再包含 W1/W2/W3/GG/EE、kill-test、API executor 或 hidden canonical runtime 叙事。
