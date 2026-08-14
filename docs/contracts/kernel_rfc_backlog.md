# Kernel RFC Backlog

> scope 外问题登记处（kernel.md 首页规则：需要新跨层接口或修改冻结验收标准时，先在这里登记一页）。每条登记包含：问题、证据、建议、提出 wave。

## RFC-001：kernel.py ≤ 600 行验收指标与冻结范围的总和不兼容

- **提出**：Wave-A.5（Agent A v5，kernel-slim-v5 分支）。
- **问题**：本 wave 验收要求 `wc -l taskvm/kernel/kernel.py ≤ 600`。按冻结范围实测，诚实底线约为 **800 行代码**（不含文档/空行）。两者冲突。
- **证据**（AST 实测，非估算）：
  - v4（f154b4c）1194 总行 = 888 代码 + 209 docstring + 46 注释 + 51 空行。
  - 本 wave 实际可下放的内容校验代码 ≈ **150 行**（`_validate_composition_locked` 65 + 补偿 value-match/extra-key 防御 ~25 + observation dup 扫描 ~10 + checkpoint 撞名 guard ~14 + store 双重校验 ~5 + 冗余 deepcopy ~2 + bool verify 改签净差 ~10）。spec 的 600 目标建立在对可下放量的 ~600 行估计上，实测只有 ~150 行。
  - 同 wave 新增且由 layered protocol §4 / prompt §7/§8 **强制要求**的时序代码 ≈ +110 行：pending-compensation gate、typed landing（attempt identity）、COMPLETE 截断、PARTIAL disposition、compensation landing helpers。
  - 净结果：888 − 150 + 110 ≈ **850 代码行**；docstring 精简后总行数 ≈ 1000。protocol §4 的 15 条时序不变量本身即需 ~750 行诚实 Python。
- **已达成的真实目标**：职责面积确实缩小——内容校验全部单一 owner 化（domain 构造器 + typed result），Kernel 不再做任何 producer 内容重证；无职责搬家（无 kernel helper 文件、无新增 kernel 模块、domain 未获得任何 mutable runtime state）。
- **建议**（三选一，需 oracle/治理裁决）：
  1. 将验收指标修订为"kernel.py 代码行（AST 实测）≤ 850 且无职责搬家"，或直接以职责面积审查替代行数代理指标；
  2. 接受 ~1000 总行作为 protocol §4 全部 15 条不变量 + §7/§8 新时序要求的真实底线，指标顺延到下一 wave 随进一步分层（如 C/E 落地后可能的简化）再议；
  3. 若 600 是硬性政治指标，唯一诚实路径是删减 protocol §4 已冻结的不变量（不推荐——那正是本协议要保护的东西）。
- **状态**：**已裁决（Oracle，2026-08-14 v5 rollback closure 评审）**：采纳建议 2 之实质——`kernel.py ≤ 600` 不再作为 blocker / 验收硬指标，降级为 **soft diagnostic**；真实判据 = 职责面积（Kernel 只拥有 STATE/TIME/HISTORY/TRANSITION，内容校验单一 owner 化，无职责搬家）。裁决原文要点："不要再为了 ≤600 对 Kernel 做第二轮强制减肥；把这个指标废止或者改成 soft diagnostic。"当前实现 ~1010 行（v5 rollback closure 后）。
