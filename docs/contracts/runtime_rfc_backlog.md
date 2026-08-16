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

## RFC-003：`completion_condition` 的最小确定性 visible-criterion 合同

- **提出**：Wave-A.7 Agent E bounded repair（P0-2）。runtime.md §6 冻结：
  `passed` iff 对 `contract.desired_state` 每个 key 的 fresh `after_observed[key] == desired_state[key]`
  **且 `completion_condition` 的可见判据满足**；default verifier 确定性、0 模型调用、
  visible-only。但 `ActionContract.completion_condition: str`（domain）是自由文本语义
  谓词描述，而 `VisibleVerifier._verify_action` pre-fix 只查 `desired_state`——
  `completion_condition` 从未被检查（一个 doc-vs-code lie：模块 docstring 声称
  "completion_condition / desired_state evaluated"）。
- **问题**：要诚实、确定性、0 模型调用地检查 `completion_condition`，又不发明 NLP
  parser，需要一个**最小机制**（runtime.md §6 / 本 prompt：minimal mechanism，不是通用
  predicate language）。自由文本形式（`"inbox visibly shows sent"`、`"收件箱显示已发送"`、
  `"{node_id} visibly done"`）异构，无法在不引入 NLP 的前提下确定性地 ground 到 observed
  值。
- **建议（已采纳，本 wave 实现）**：澄清 `completion_condition` 的最小确定性合同：
  1. **空串** = 无额外可见判据（satisfied）——`desired_state` match 即足够（例如纯导航点击）。
     这不是"忽略"，是合同明确：空条件 = 无额外判据。
  2. **`<semantic_key> == <value>`**（单子句，复用 runtime.md §11 已冻结的 loop
     `termination_predicate` 同一 minimal parse 形式，不引入新 parser）：verifier 对 fresh
     `after_observed[key]` 求 `str(value) == <value>`（值带引号可选 strip）。
  3. **非空且不符合 minimal form** = verifier **无法**确定性地建立 visible criterion →
     `passed = False`（诚实 fail-closed，detail 指出 non-conforming 条件）。**绝不静默 satisfied**
     （runtime.md §6 "completion_condition 已查" + 零暴露原则：看不到的判据不得假装满足）。
  4. 引用 **未 observed** 的 key（after_observed 无该 key）= criterion 无法 ground → `passed=False`。
- **不修改的冻结面**：不动 `ActionContract.completion_condition: str` 类型（domain，Agent A）；
  不放宽 runtime gate；default verifier 仍 deterministic、0 模型调用、visible-only、不读
  hidden DB / oracle / fixture / entity_id / internal API。这是 E 自有 verifier 内容所有权的
  minimal 合同澄清，不是新 invariant category，不阻塞 runtime.md 冻结面。
- **约束**：composition/Architect(C) 生产 ActionContract 时，`completion_condition` 应使用
  minimal form 或留空；非 minimal 的自由文本条件在 runtime verifier 处会诚实 fail（这是
  §6 的正确行为：不可验即不通过，而非发明 NLP 假装判过）。legacy architect/governance 测试
  fixture 中的 NL `completion_condition` 字符串不经过 runtime verifier（它们测各自层），
  不受影响。
- **状态**：**已裁决（本 wave 采纳 minimal-form 合同）**。`VisibleVerifier` 以此实现，
  negative-control test 覆盖（desired 全 match BUT completion 不满足 ⇒ `passed=False`）。
  follow-up（非阻塞）：若日后 Architect 要生成复合判据，走新 RFC 扩展 minimal form（仍是
  deterministic、0 模型调用），不回到 NLP。

## RFC-004：emergency-stop 语义对齐（soft pause + epoch invalidation 已 subsume）

- **提出**：Wave-A.7 Agent E bounded repair（P1 spec-gap）。handoff-06 §"Execution Epoch 与热中断"
  列出 `emergency stop：不再启动下一 action，并将 session 标为需要重新观察`，作为与 soft pause
  并列的语义。但 **frozen runtime.md §4（Hot Governance）只冻结了**：soft pause（当前 atomic action
  完成后阻止下一 action）、stale-discard（epoch race via `start_action` gate）、irreversible
  act-前 epoch + 治理态复检、pending-compensation 阻断 forward。§4 **未**冻结一个独立的
  "emergency stop" facade 操作或 "needs reobserve" 状态字段。
- **问题**：handoff-06（Level 5 evidence——handoff，非冻结合同）提到 emergency stop，但 frozen
  runtime.md §4 未将其作为独立操作冻结。按 audit charter §4.1，未冻结合同要求的操作不是 DEFECT，
  是 SPEC-GAP；不得为它发明新 invariant category 或 commercial cancellation framework
  （本 prompt 明确："不要设计 commercial cancellation framework"）。
- **建议（已采纳，本 wave 对齐）**：澄清 frozen §4 的 **soft pause + epoch invalidation 已经
  subsume** handoff-06 的 emergency-stop 语义：
  - "不再启动下一 action" = §4 soft pause（`request_pause` → 当前 atomic action 完成后下一 action
    被 `_paused` gate + epoch bump 阻止）；CUA response 在 governance epoch bump 后被
    `start_action` gate DISCARD（`ACTION_DISCARDED`），绝不 `substrate.act`。
  - "session needs reobserve" = 每个新 contract attempt在第一次 gesture 前 fresh observe →
    extract → fold（runtime.md §3 + RFC-P0-1 / E40a 的 fresh-before fold），所以 stop 后下次
    run() 的 `before` 来自 fresh visible world，而非 stale cache。runtime 已满足。
  - 不新增独立 facade method、不新增 "needs_reobserve" 状态字段（那会是新 invariant category，
    out-of-scope）。`_stop_reason` / `_paused` 已表达停止原因。
- **不修改的冻结面**：runtime.md §4 冻结语义不动；不新增 emergency-stop 操作。这是 handoff ↔
  frozen contract 的 minimal 对齐（SPEC-GAP，非 DEFECT），不是新合同条目。
- **状态**：**已裁决（Option B：soft-pause + epoch-invalidation subsumes emergency-stop）**。
  regression test 覆盖：pause/stop 后下次 contract 的 before 来自 fresh observation
  （`tests/runtime/test_active_sync.py::test_fresh_before_observation_not_stale_kernel_cache`
  已证 fresh-before；soft-pause 的 next-action 阻止由 `tests/runtime/test_hot_governance.py::
  test_soft_pause_blocks_the_next_atomic_action` 锁定）。

## RFC-005：`requires_confirmation` 三方语义对齐（epoch + 治理态复检 = frozen §3/§4）

- **提出**：Wave-A.7 Agent E bounded repair（P1）。runtime.md §3 "如果 `contract.requires_confirmation`
  （IRREVERSIBLE）：再次检查 Kernel epoch + 治理态"；§4 "不可逆/高风险动作 … `act()` 前再次检查
  Kernel epoch + 治理态"；§6 "IRREVERSIBLE contract 在 `act()` 前被 `requires_confirmation` 拦截"。
- **问题**：确认 frozen 合同文字（§3/§4/§6）、当前代码、tests 三方语义一致。
- **证据（exact-SHA e36e1caa）**：
  - domain `ActionContract.requires_confirmation` (contract.py) = `reversibility is IRREVERSIBLE`
    （一个 property flag，标不可逆；不是 runtime human-in-the-loop approval boundary——
    "confirmed upstream" = UI 可见锁定 / 治理边界，contract.py docstring + 心智模型 §3.5）。
  - runtime `autonomy.py::_run_contract_once`：在 `substrate.act` 前对 irreversible contract 做
    `if contract.requires_confirmation and self._kernel.epoch != request_epoch: return "stopped"`
    （act-前 epoch 复检）；loop 顶 `if self._paused: return stopped` 覆盖 soft pause（治理态）；
    `_governance_says_paused()` 读 kernel 事件日志（只读 facade）。
  - tests：`test_irreversible_contract_rechecks_epoch_before_acting`（pause mid-flight → 不落地）
    + `test_irreversible_contract_executes_when_epoch_stable`（稳定 → 正常落地）锁定该 gate。
- **裁决**：三方**ALIGNED**。frozen §3/§4 "再次检查 Kernel epoch + 治理态" 的 minimal 实现是：
  (a) irreversible-specific act-前 epoch 复检（governance 介入 bump epoch → 拦截）+ (b) loop 顶
  soft-pause/`_governance_says_paused` 检查。所有改道（retargeting）类 governance 动作（pause/
  GoalPatch/LocalPatch/compensation）都 bump epoch（event-only 的 record_conflict/resolve_conflict
  除外——它们不改可执行状态，不触发 act-前拦截的需求），故 epoch 复检 IS the governance-state
  signal——无需新增独立 "治理态" 字段（那会是冗余/新 invariant）。`requires_confirmation` 是 reversibility flag（非 human
  approval gate）；runtime 不实现 human-in-the-loop confirmation boundary（合同未要求）。
- **不修改的冻结面**：runtime.md §3/§4/§6 不动；不新增 human approval system；不新增治理态字段。
- **状态**：**已裁决（ALIGNED，无代码变更）**。RFC 记录三方一致性，防未来 drift。

