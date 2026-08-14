# Audit Agent Charter — 冻结（适用于所有 Agent / 层的审计）

> 状态：**冻结**（2026-08-14）。任何审计 Agent——无论审 Agent A 的 Kernel、Agent B 的 Substrate、Agent C 的 Architect、Agent D 的 Projection、Agent E 的 Runtime/Verifier、benchmark 还是集成——开工前必须读完本页 + [layered_ownership_protocol.md](layered_ownership_protocol.md) + **被审层的合同 doc**（如 Kernel 审看 [kernel.md](kernel.md)）。
> 一句话：**审计只验"被审层的冻结合同是否真的成立 + 跨层是否真的干净 + 文档与代码是否一致"，不重设计、不跨层重证、不跨轮 drip-feed。**

## 0. 为什么有这份 charter

过去几轮审计发生过两类失败，本页用来堵死它们（它们会**在任何层的审计**复发，不只 Kernel）：

1. **Treadmill**：每轮审计"再发现 N 条"，因为不变量没有事先枚举冻结，审计一轮挖一层、永无止境；被审 agent 被迫一轮轮补。（Agent A 的 Kernel 审计是触发本 charter 的实例：v2→v3→v4 三轮 escalation。）
2. **Hostile-caller drift**：审计把被审层当成对抗任意恶意第三方的防火墙，要求它重证**下游/上游层本该自己保证**的内容合法性。但这些层是我们自己控制的模块，不是任意插件。这催生大量重复 `if` 防御和"测试把错误行为当正确"的伪绿。

矫正后的模型（见 protocol §1）：**内容合法性由唯一 producer / domain constructor 负责；时序合法性由 Kernel 负责。一个性质只有一个 owner，不在下一层重证。** 审计角色随之收敛——对**任何层**都成立。

## 1. 审计的唯一原则（适用于任何层）

> 审计不得要求被审层重证**别的层**拥有的内容合法性；审计只验：被审层自己的冻结合同是否真的成立、跨层边界是否真的干净、文档与代码是否一致。

被审层不是 hostile-caller firewall。任何"X 层可能喂坏东西，所以 Y 层必须查"的论证，**一律 out-of-scope**——那是 X 层的 contract test 该管的。

## 2. 审计对象

本 charter 适用于对**任何 agent / 层**的审计：

- Agent A — Kernel / domain（合同：kernel.md）
- Agent B — Substrate（合同：将来 substrate.md）
- Agent C — State Compiler / Task Architect / Governance（合同：将来 architect.md）
- Agent D — Projection / Frontend（合同：将来 projection.md）
- Agent E — CUA Runtime / Verifier / Rollback（合同：将来 runtime.md）
- benchmark / 集成 / 跨设备（合同：各自 doc）

审计开工**第一步**：指明"本轮审哪个 agent / 层"，并定位该层的冻结合同 doc。没有冻结合同的层，先要求该 agent 冻结合同，**不得在合同未冻结时做开放式审计**（否则又回到 treadmill）。

## 3. In-scope（审计允许报的，仅这五类——对任何层相同）

1. **被审层冻结合同的违反**：该层合同 doc 列出的不变量 / API / 类型，代码声称成立但实际不成立。每条 cite file:line + 代码引用。
2. **Doc-vs-code 谎**：被审层文档声称某行为已实现，但代码没实现或相反。这是**最尖锐、最欢迎**的发现。
3. **分层泄漏**：reverse import、跨层 import concrete 实现、domain 获得 mutable runtime state、被审层残留它不该有的内容校验。（跨层边界由 architecture gate 机器强制，审计复核 gate 是否真绿。）
4. **次生 regression**：本轮 fix 引入的新破坏。
5. **测试把错误行为当正确**（test oracle 本身错）：测试绿但断言锁的是 buggy 行为。在 scope——但须给出"正确行为应是什么"。

## 4. Out-of-scope（审计禁止做的——违反即作废该 finding）

1. **发明新 invariant category**：审计只能验**已冻结**合同里的不变量。新 category（"还应该防 X"）→ 走 [kernel_rfc_backlog.md](kernel_rfc_backlog.md)（或对应层的 RFC 队列）RFC，**不算 audit finding**，不阻塞冻结。
2. **重审已 frozen + test-pinned 的不变量**：该层合同一旦绿且有对应测试钉死，下一轮不得以同 scope 重开。重开 = treadmill。
3. **Hostile-caller / firewall 框架**：禁止以"假设某层会喂坏东西"为由要求另一层加内容校验。内容校验归 producer / domain constructor。
4. **要求跨层重证**：不得要求被审层替上下游实现/校验内容。例：审 Kernel 时不得要求 Kernel 验 workflow shape（那是 C 的 TaskArchitecture）；审 C 时不得要求 C 验 observation freshness（那是 E 的 verifier）；审 B 时不得要求 B 验 contract desired 一致性（那是 C 的）。
5. **用行数 / scope 当 gate**：验收是**结构性** bar（§6），不是行数。不得以"X 文件太长"重开减肥轮（Agent A 的 kernel.py ≤600 已退役，见 RFC-001）。
6. **重设计架构**：架构已冻结。审计只验 conformance，不 redesign。"我偏好另一种结构"是 preference，out-of-scope。
7. **开放式"再找更多 bug"**：禁止。开工先枚举 scope，枚举完即停；不得跨轮 drip-feed。

## 5. 审计自身纪律（怎么做才算可信——对任何层相同）

1. **开工第一步：声明 scope**。写明"本轮审哪个层 / 该层合同的哪几条 / 哪些 doc-vs-code 点"，bounded。不声明 scope 的 finding 不接受。
2. **每条 finding 必须 forensic**：`file:line + 代码引用 + 反例调用序列`。禁止"看起来可能有问题"式抽象断言。
3. **行为类 claim 必须 ground-truth**：涉及运行时行为的，跑 `pytest` 或写 probe 实证；不得谎称"测试绿"或"代码会 X"而不读实际路径。诚实声明"我没跑"是底线；对 load-bearing 行为 claim 必须实跑。
4. **区分三类，每条自标**：
   - **defect**（代码违反冻结合同）→ in-scope，可阻塞。
   - **spec-gap**（合同从没要求过）→ 走 RFC，不阻塞。
   - **preference**（审计个人审美）→ out-of-scope。
   把 spec-gap/preference 伪装成 defect 是违规。
5. **one-owner routing**：每条 finding 标注它属于哪个 owner（A/B/C/D/E）。若发现某问题其实是**别层**的内容职责，路由到该 agent 的 contract test，**不要求被审层实现**。
6. **一次性枚举 + 完整性承诺**：开工即声明"本轮枚举完整，下一轮只做次生 regression 检查"。对漏报的非次生 bug 负责。这把"跨轮 drip-feed"从激励变成问责。
7. **诚实 reporting**：测试 fail 就说 fail；没跑就说没跑；修了的就不写"complete"。禁止 overclaim。

## 6. 验收 bar（机械可判定——"该层 frozen" 的充要条件）

满足全部才算该层 frozen；缺一不可。**具体 grep 目标 / 不变量清单 / 测试清单由该层合同 doc 给出**（如 Kernel 见 kernel.md §4），本页只给跨层通用的结构：

- `pytest` 对应层测试全绿（该层合同每条不变量 ≥1 对应绿测试）。
- `python -m compileall` 对应层 exit 0。
- **分层 gate 绿**（domain 仅 stdlib / kernel 仅 domain+stdlib / substrate 仅 domain 反向 gate / runtime 只到 substrate PORT / 各 agent 不越界）。
- 该层**不该有的内容校验零残留**（具体 grep 目标在该层合同 doc）。
- 无新 invariant category 未经 RFC。

bar 是**结构性**的，不是行数。

## 7. "frozen" 之后

某层 bar 满足 → 该层合同 frozen → 该 agent 退出主开发路径。此后审计**不得以同 scope 重开**该层。后续只允许：

- 次生 regression 检查（某 fix 引入新破坏）；
- 新 RFC 批准后新增的不变量。

重启某层（新 feature、新合同条目）需新 charter 或 RFC 授权，不得复用旧审计的开放式授权。

## 8. 审计的角色边界（一句话总结）

> 审计是**冻结契约的守门人**，不是**架构师**，不是**跨层防火墙设计师**。
> 它验"已冻结的是否真的成立"，不发明"还没冻结的应该存在"。
> 它为**漏报**负责，不为"没多防一层"负责。

违反本 charter 的 finding 视为越权，作废，不阻塞冻结。
