# Runtime RFC Backlog

> scope 外问题登记处（runtime.md §1：需要新跨层接口或修改冻结验收标准时，先在这里登记一页）。每条登记包含：问题、证据、建议、提出 wave。镜像 `kernel_rfc_backlog.md` 的体例。

## RFC-001：Runtime→Architect 跨层 Port（CUA 序列化器 / ModelPort / ModelCallLedger）的 import-gate 处置

- **提出**：Wave-A.6（Agent E runtime 交付）。architect.md §5 已路由："CUA 调用由 E 的 runtime 记账（同 ledger 接口；runtime→architect 的 import gate 由 E 走 RFC，不在本 wave）"。
- **问题**：Runtime（L2）需要三样住在 `taskvm.architect`（L4）里的东西：
  1. `ActionContractSerializer` —— 把 `ActionContract` / `CompensationEntry` 确定性序列化成 CUA goal 文本（architect.md §2、§6，0 模型调用）。
  2. `ModelPort` / `HttpModelPort` —— 真实 provider HTTP 调用（architect `port.py`）。
  3. `ModelCallLedger` / `ModelCallRecord` —— 调用计量，benchmark 必须能区分 compiler/architect/**cua** 三角色（architect.md §5）。
  但架构 gate（`tests/architecture/test_import_boundaries.py`）给 `taskvm/runtime` 的 `allowed_taskvm = {taskvm.domain, taskvm.kernel, taskvm.substrate}`，**不含 `taskvm.architect`**（architect 在 runtime 之上，L4 > L2；runtime 不得向上依赖 architect）。直接 `from taskvm.architect import ...` 会被 gate 拒绝。
- **证据**：
  - gate 规则实测：`_RULES["taskvm/runtime"].allowed_taskvm = ("taskvm.domain","taskvm.kernel","taskvm.substrate")`；architect 不在列。
  - architect `port.py` 的 `ModelCallRecord` 字段 = `role, purpose, model, ok, is_repair, prompt_tokens, completion_tokens, latency_ms, revision, error`；`ModelCallLedger.record()` 只做 `rec.role not in MODEL_ROLES` 字符串校验后 append——**duck-typed**：任何带同字段的对象都被接受。
  - architect `ActionContractSerializer` 的方法签名 `cua_goal(contract, labels=None, *, attempt=1) -> str` / `compensation_goal(entry, labels=None) -> str`——纯文本输出，无 architect 专有类型返回。
- **建议（已采纳，本 wave 实现）**：**不开 reverse import；用依赖注入 + Protocol Port 解决。** Runtime 在 `taskvm/runtime/ports.py` 定义：
  - `CUAGoalSerializer(Protocol)` —— 方法签名与 architect `ActionContractSerializer` 逐字兼容；组合层直接把 architect 的实例注入（结构兼容，无需适配）。
  - `CUAModel(Protocol)` —— `predict_action(*, goal, observation, labels, attempt, model) -> CUADecision`；`CUADecision` 是 runtime 自有 domain-ish 类型（`act`/`done`/`fail` + `GuiAction`）。architect 的 `ModelPort`+`_LedgeredPort` 经组合层（Agent G）适配成 `CUAModel`（负责 system prompt 组装 + observation→prompt + JSON→`CUADecision` 解析）。**适配器住在组合层，不在 runtime 也不在 architect**，因此不破坏任何 gate。
  - `ObservationExtractor(Protocol)` —— `extract(observation, variables) -> tuple[ObservedValue]`；确定性 fast path。组合层注入 architect `StateCompiler.extract_observed` 的包装。
  - `CallLedger(Protocol)` —— `record(rec) / records / counts_by_role / total`；runtime 自有 `ModelCallRecord`（字段与 architect **逐字段相同**）。组合层把**同一个** architect `ModelCallLedger` 实例同时注入 architect 与 runtime——`counts_by_role()` 天然合并三角色，benchmark 报告的 CUA call count = 真实 provider request count，无需跨 ledger 合并代码。
  - `Verifier(Protocol)` —— `verify(...) -> VerificationResult`；具体实现 `taskvm.verifier.VisibleVerifier`（E 自有，`taskvm/verifier/visible.py`），组合层/测试注入。Runtime 不 import `taskvm.verifier`（gate 不允许；verifier 经注入进入）。
- **不修改的冻结面**：不动 `taskvm.architect` / `taskvm.domain` / `taskvm.kernel` 任何已有公开类型；不放宽 runtime gate。`taskvm/runtime` 仍是 stdlib-only（无 `requests`/`flask`/`playwright`）。
- **未决（留给后续 wave，非阻塞）**：若日后要消除 runtime `ModelCallRecord` 与 architect `ModelCallRecord` 的双定义，最干净的是把纯数据的 `ModelCallRecord`（+ `MODEL_ROLE_*` 常量）上移到 `taskvm.domain`（domain 是 stdlib-only，纯数据 dataclass 合法），让 architect 与 runtime 引用同一类型。这需要 Agent A（domain owner）+ Agent C（architect owner）各动一行 re-export——属跨层改动，本 wave 不做，留作本 RFC 的 follow-up。当前 duck-typed 兼容已满足"统一报告 = 真实 provider request count"的验收。
- **状态**：**已裁决（本 wave 采纳 DI-Port 方案）**。Runtime 以此实现，测试以 fakes + 真实 architect `ActionContractSerializer` 注入证明端到端可组合。物理合并 `ModelCallRecord` 类型留 follow-up。

## RFC-002：kernel facade 缺少只读 `pending_compensation` 暴露

- **提出**：Wave-A.6 GLM 5.3 审计（Agent E runtime 交付审计 finding D3）。
- **问题**：runtime.md §4 要求"Runtime 的主循环检测到 pending compensation 状态 → 切换到 execute_compensation 模式"。但 kernel facade 的只读快照只有 `pending_recompose`（kernel.md §3），没有 `pending_compensation`。Runtime 唯一的探测途径是 `request_action` 抛出的 `ValidationError`——异常消息不是编程接口，解析字符串是脆后门。
- **证据**：`taskvm/kernel/kernel.py` introspection 区只有 `session_id` / `epoch` / `pending_recompose` 三个 property；`_require_executable_locked` 在当前 epoch 存在 pending 补偿计划时抛 `ValidationError("execution blocked: compensation plan {pid} is pending; ...")`。内核已持有 `_comp_status`（plan_id → pending/complete/partial/failed/discarded）与 `_comp_plans`，暴露只读 property 不改任何时序语义。
- **建议**：Agent A 在 kernel facade 加 `@property pending_compensation -> str | None`（当前 epoch 的 pending plan_id，无则 None），并在 kernel.md §3 只读快照清单补一行。只读、无 mutation、不违反"Kernel 不为下游重做内容"——它本来就是 Kernel 自己的时序状态。
- **当前处置（非阻塞）**：Runtime 以保守方案运行——`request_action` 抛 `ValidationError` 时 run() 返回 `BLOCKED`（安全停止，不热重试），composition 层在发起 rollback 后显式调用 `runtime.execute_compensation(plan)`。语义等价（forward autonomy 落地前不推进），只是无法从 run() 返回值区分 blocked 原因。owner：Agent A；落地后 Runtime 可一行切换为主动检测。
- **状态**：**待裁决**（non-blocking，Runtime 已有保守实现）。
