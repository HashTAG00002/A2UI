# Architect RFC Backlog

> scope 外问题登记处（同 kernel/projection/runtime backlog 约定：需要修改冻结层时，先在这里登记一页，附 exact-SHA 审计证据）。每条登记包含：问题、证据、建议、提出时间。

## RFC-A01：architect 系统提示词 / 装配器 / 修复引导语三方不一致——sequence 内步骤被判 fork（真实模型 4/4 失败）

- **提出**：2026-08-19（RM.c.1 前 APP 化端到端验证，CatPaw 会话）。
- **影响**：真实 gpt-5.6-sol 走 `bootstrap_real_full`（compiler+architect）在 MobileGym wechat 出厂世界上 **4/4 连续编排失败**，APP 无法进入执行阶段。这是"枪在靶场上卡壳"，不是 APP 壳的问题。
- **exact-SHA 审计证据**（HEAD `40fc5570a0117abacb3d5af189aedcc69afea39b`）：
  - `taskvm/architect/architect.py` blob `e7803381a5ead4e203568388a3b53da9b965ff2c`
  - `taskvm/architect/noleak.py` blob `015b495525aa8362108c5a6b1070629d5c6e10c5`
  - `taskvm/domain/workflow.py` blob `e1630ff361824bcd57b272a82f6c815b6783fae0`
  - 原始模型回复与探针输出：`eval_results/rm_c1_app_e2e_20260819.json`
- **问题**（三方不一致，全部在 FROZEN 层）：
  1. **系统提示词**（architect.py `_SYSTEM_PROMPT`）承诺 *"a sequence's steps run in your listed order"*——告诉模型 sequence 内顺序是**隐式的**（按列出顺序），`after` 只需表达跨容器等待。
  2. **domain 校验器**（domain/workflow.py L142-168）却要求 sequence 子节点之间形成**显式 depends_on 全序链**：任一时刻 ready 池必须恰好 1 个节点，否则 `found a fork`。
  3. **修复引导语**（noleak.py `_REPAIR_GUIDANCE`，fork 条目）说 *"inside a sequence, steps run in the listed order — exactly one step may be next at any point"*——**重申了提示词的隐式顺序语义**，没有告诉模型真正的修复动作（给容器内每个步骤补 `after` 链 / 把节点放进容器）。模型按引导"确认列出顺序无分叉"后原样重发 → 修复轮必然无效。
- **证据摘要**（4 次真实编排，全部失败）：
  - goal-1 `ArchitectOutputError: action '发送消息' needs a non-empty 'sets' mapping`（另类失败，同属提示词字段要求与模型理解偏差）；
  - goal-2/3/4 全部 `sequence 'n001' children must form a single ordered chain (depends_on within the sequence); found a fork`；
  - 探针抓到的**原始模型 JSON**（goal-4 两次尝试，逐字）：计划本身是完美线性链
    `打开黄勇的聊天 → 填写消息内容 → checkpoint(发送前确认, container=null) → 发送消息 → 核验消息已发送 → terminal`，
    唯一结构性差异：**checkpoint 的 `container` 是 `null`**（在 sequence 外）。"发送消息" 的 `after` 指向容器外的 checkpoint → 容器内视角出现两个 indeg=0 节点 → fork。修复轮模型输出与首轮**拓扑完全相同**（引导语无效的实证）。
  - **确定性复验（零模型调用）**：把同一份 JSON 的 checkpoint `container: null → "发送微信消息流程"`（单字段 delta）后，`TaskArchitect._assemble` + `TaskArchitecture` 校验**全部通过**（n002→n003→n004→n005→n006 线性链，单一 terminal）。
- **建议**（三选一或组合，需治理裁决；均为 FROZEN 层最小改动）：
  1. **装配归一（推荐）**：`_assemble_graph` 对 sequence 子节点在容器内依赖为空时按**列出顺序自动补链**——这正是提示词已经向模型承诺的语义，装配层兑现即可；同时把 `after` 指向容器外节点的情况视为已满足顺序（消除 phantom fork）。改动集中在 architect.py 装配段，约 +15 行，domain 校验器不动。
  2. **提示词+引导语对齐**：`_SYSTEM_PROMPT` 增加"sequence 内每个步骤必须在 after 里链接它的前驱（容器内标签）"；`_REPAIR_GUIDANCE` fork 条目改为指明该动作。模型自由度不变，但依赖模型遵循度（当前 4/4 未遵循，风险高）。
  3. **1+2 组合**：装配归一兜底 + 提示词讲清楚，双保险。
  - **不建议**：放宽 domain 校验器（sequence 全序链是 kernel 调度语义的根基，放宽会改变 FROZEN domain 语义）。
- **状态**：**待裁决**。RM.c.1 若要以真实 gpt-5.6-sol 跑 MobileGym 开放目标，此 RFC 是前置依赖。
