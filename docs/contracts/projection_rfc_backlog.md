# Projection RFC Backlog

> scope 外问题登记处（projection.md §1：需要修改冻结验收标准时，先在这里登记一页）。
> 每条登记包含：问题、证据、裁决、不修改的冻结面、状态。镜像 `kernel_rfc_backlog.md`
> / `runtime_rfc_backlog.md` 的体例。

## RFC-D1：路由矩阵与 SSE 词汇的 doc-vs-code 对齐（D 审计终局返工 D-F1..D-F4）

- **提出**：D audit end-game（2026-08-16，audit rework order 授权，基线 SHA a63570a）。
  审计发现 frozen projection.md §6（路由矩阵）/ §7（SSE 词汇）与 `taskvm/projection`
  实现之间存在 doc-vs-code 偏差：文档冻结的是**起草时的设想拼写**，而实现（E40 落地 +
  135 测试锁定）演化出了更诚实、更完整的语义。
- **问题**（逐条偏差，均以代码为证）：
  1. **路径拼写**：§6 把 governance 命令写成顶层路径（`/start`、`/pause`、
     `/checkpoints` POST、`/local-patches`、`/goal-patches`、`/rollback`、
     `/conflicts/<cid>/resolve`）；实现统一挂在 `/api/sessions/<sid>/governance/<cmd>`
     前缀下（`start` / `pause` / `resume` / `stop` / `checkpoint` / `local_patch` /
     `goal_patch` / `rollback` / `resolve_conflict`）。
  2. **events 与 SSE 混写一行**：§6 的 `/events` 行同时承担 SSE 流语义；实现拆成两个
     路由——`GET /events`（分页 JSON 事件日志，`?limit=`）与 `GET /sse`
     （`text/event-stream` 流：连接即 `snapshot` 帧 + 之后全部 typed delta 帧）。
  3. **读路由缺行**：§6 缺 `GET /api/sessions`（会话列表）、`GET /governance`、
     `/variables`、`/workflow`、`/checkpoints`（GET 时间线）、`/surfaces`、`/conflicts`
     ——实现有、文档无（E40 的 16 路由矩阵）。
  4. **rollback 语义**：§6 写"202 plan accepted (execution async)"；实现经
     `DriverPort.execute_compensation` **同步执行**补偿计划并诚实回报 disposition
     （`complete` / `partial` / `failed`；无 driver/runtime 时诚实 `pending`），
     仍 202，异步语义由 SSE disposition 帧承载。审计指出旧行为（计划永远 pending、
     无执行路径）是 doc-vs-code lie 的实例。
  5. **状态码**：checkpoint 成功 = **201**（资源创建）；goal_patch = **202**（两阶段
     异步）；local_patch 撞 non-editable key = **422**；start 在 pending recompose /
     未注册 runtime 时 = **409**；未知 checkpoint = **404**（typed，class-based 映射，
     非字符串匹配）。
  6. **SSE 词汇表**：§7 列的是**语义类别名**（`projection_delta` / `workflow_delta` /
     `runtime_status` / `surface_observation` / `screenshot_available` / …），其中三个
     从来不是任何 `RuntimeEventKind` 值（dead mapping），又漏了五个真实 kind；实现是
     逐 `EventKind`（23 项）/ `RuntimeEventKind`（8 项）的全量冻结 dot.notation 映射
     + 两个 transport 级帧类型（`snapshot` / `governance.applied`），合计 33 项单一
     真源 `SSE_TYPE_VOCABULARY`，emission chokepoint `format_sse` 断言成员资格。
- **证据**：
  - `taskvm/projection/app.py` 模块 docstring 路由矩阵（"REVISED by RFC-D1"）+
    `_http_status_for` typed 错误映射；
  - `taskvm/projection/events.py` `KERNEL_EVENT_SSE` / `RUNTIME_EVENT_SSE` /
    `TRANSPORT_EVENT_SSE` / `SSE_TYPE_VOCABULARY`；
  - `taskvm/projection/services/driver.py`（lifecycle 确定性返回值 +
    `execute_compensation` 回报 runtime disposition）；
  - `tests/projection/test_route_matrix.py`（"RFC-D1 §6" 语义断言：201/202/409/422/
    404 typed）、`tests/projection/test_events.py`（EventKind+RuntimeEventKind
    totality + chokepoint 拒绝未注册类型）、`tests/e2e_ui/test_runtime_e2e.py`
    （全弧：start → ACTION COMMITTED → checkpoint → rollback EXECUTED →
    disposition 经 SSE 可见）。
- **裁决（已采纳，2026-08-16）**：**选 (b) 文档对齐代码**。理由：
  1. 实现已被 135+ 测试锁定且语义更诚实（rollback 执行 + disposition 回报恰是审计
     要求的修复方向；把代码改回"fire-and-forget pending"是倒退）；
  2. 路径拼写属 transport 细节，§0 已声明合同冻结 OUTCOMES 而非拼写（"This
     contract freezes OUTCOMES, not CSS classes / DOM selectors / Flask
     internals"）——统一 `/governance/` 前缀不改变任何 OUTCOME；
  3. SSE 词汇 = 事件种类集合这一冻结原则**不变**，D-F3 恰是把原则执行得更严
     （totality + chokepoint 断言取代自由字符串）。
  projection.md §6/§7 已按此修订并标注 REVISED-by-RFC-D1。
- **不修改的冻结面**：§0-§5、§8-§13 全部不动；§6 的正常路径保证（0 意外 405、
  0 意外 500、结构化 4xx、未知 sid ⇒ 404、SSE content-type）不动；§7 的"transport
  replaceable / semantics frozen / 单调 id / 快照+revision 恢复 / 整页 reload 即
  违约"原则不动。
- **遗留（routed，非本 RFC 范围）**：`workspace_ui/composition.py` 作为组合根 import
  substrate（组合根是 §5 seam 的合法使用者，但 `gui_driver` 物理删除仍 PENDING）——
  登记于 `substrate.md` §8 Transitional Debt Register（T1，owner E/G）；SPA 静态前端
  接线后 `/sessions/<sid>` 才服务 HTML（当前诚实服务 JSON snapshot）。
- **状态**：**已裁决并落地**（文档已修订；代码以 E44 之后的 D 返工为准）。
