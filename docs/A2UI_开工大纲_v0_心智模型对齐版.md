# TaskVM 开工大纲（单一权威文档·锁定版）

> 本文件是通读全部材料并完成 15+1 个开放问题（含 §15-G-Q16 新增项）**全部拍板、零遗留待确认项**后形成的**唯一开工基础**，取代此前的【心智模型对齐版】。
>
> **项目更名**：项目代号由 **A2UI 正式更名为 TaskVM**（见 §15-Q2 决策记录）。原因：`A2UI` 是 Google 于 2025 年发起的通用 agentic UI 声明式协议名（`a2ui.org`，v0.8，2025-09 创建 / 2025-11 更新），Macaron-A2UI（COLM 2026，Mind Lab 团队开源工作）只是该协议的一个训练侧实现（模型），与本项目及本团队均无归属关系、并非本项目前作，三者（Google 协议 / Macaron-A2UI / 本项目）都不是"我们的项目"，继续用 `A2UI` 做代号会让审稿人误以为是同一件事。**TaskVM 项目在实现中仍可能选择性复用 A2UI v0.8 协议作为 UI 渲染层的传输格式**（见 §7 与 §15-Q6），"更名"与"是否用该协议渲染界面"是两件独立的事，不冲突。
>
> 输入材料：5 份外部规划 txt（`docs/oracle/`）+ 4 篇竞品论文 tex 原文（`docs/references/`：DuetUI / SaC / Sidekick / Macaron-A2UI）+ AOHP 技术报告（`docs/references/AOHP-paper/`，已确认为相邻工作、非撞车）。
> 今天 2026-07-30；CHI 2027 full paper deadline ≈ 2026-09-10 AoE，无延期、只剩 6 周。全文【待对齐】标记已全部拍板清空，剩余的仅为落地时的工程细节，交由开工的 coding agent 在 W1 中自行决定并记录。

---

## 0. 一句话主线 + 四锚点

**人操作任务，Agent 操作应用。** Agent 把多个正在运行的**既有应用**的实时状态，反向编译成一个可编辑、可执行、可验证的任务界面；用户在界面上改一个任务变量，Agent 把改动可靠写回多个真实应用，独立 verifier 读取 ground-truth 状态判定"改的发生、没改的不动、界面随后重新同步"。

四锚点（HCI-UI 第 8 轮 line 10736–10743 锁定，**四者同时存在**才与 DuetUI 拉开距离）：
`existing applications` / `live state` / `executable binding` / `round-trip verification`。

冻结 RQ（line 11261）：*Can an agent compile live, fragmented application state into an executable task-specific interface that users can directly manipulate with verifiable cross-application effects?*
最强单句主张（line 11286）：*我们将 application-centric interaction 转变为 task-native interaction：用户操纵任务状态，Agent 负责在异质软件中实现并验证该状态。*

---

## 1. 核心构念

- **构念名**：可执行投影保真性（Executable Projection Fidelity）/ Round-trip Task-state Fidelity。任务界面必须压缩复杂性才有价值，但压缩 = 删除区别，被删除的区别可能改变真实任务结果——简化与保真之间的张力是全篇唯一的人本矛盾。
- **主闭环**：`UI Agent ↔ Shared Execution State ↔ 生成式 UI`，人位于循环之外、作为低频控制节点（授权/暂停/异常恢复），**不是** DuetUI 那种人-UI-Agent 高频来回。Shared Execution State 保存目标/计划/已执行动作/真实结果/当前应用状态/artifact/失败重试/风险/待确认操作——不是聊天历史。
- **八步往返**：Observe → Abstract → Project → Manipulate → Compile → Execute → Verify → Reconcile。
- **一条数据的依赖流**：环境产生可验证 state diff → 研究者定义透明操作化 → 自动生成实例 + 隐藏 canonical task graph → compiler 抽 task-state + binding → projection_policy 决定压缩 → 用户编辑 → semantic patch → hybrid actuator 落实 → 独立 verifier 用隐藏 canonical state 评分 → reconciliation 回写界面 → 用户研究验证操作化。
- **一等对象是 Task State，不是 trajectory**（trajectory 是 agent 内部概念，不暴露给用户）。

---

## 2. 一条任务讲清这台机器（concrete example）

用户对 Agent 说：*帮我准备下周五的项目发布：安排发布会议、整理材料、创建剩余任务、起草团队通知。*
真实状态分散在 Calendar（会议）/ TaskBoard（任务、负责人、截止）/ Drive（材料）/ Mail（通知草稿）。传统 GUI Agent 只给用户看"正在打开 Calendar…正在点击…"的轨迹，用户仍要理解几十步。

TaskVM 不展示轨迹，而是实时生成一个**活的任务状态**：
```
项目发布            状态：准备中
发布日期          8 月 14 日
发布会议          8/14 14:00–15:00
负责人            Alex
材料状态          3 / 4 已完成
剩余任务          - 最终检查演示文档
                  - 确认发布公告
通知              已起草，未发送
```
用户把"发布日期"从 8/14 拖到 8/18。系统自动算出：Calendar 会议要移、TaskBoard 中依赖发布日期的 deadline 要改、Docs 发布计划日期要更新、邮件草稿日期要改、已完成且不依赖发布日期的任务不动——然后 Agent 在线执行这些应用操作，独立 verifier 读真实状态判定"改的都改了、没改的没动、界面重新同步"。

这里的 `patch`（{field, old, new}）只是"用户改了什么"的结构化表达，不是大模型的主观标签；最终对错由各应用的**真实状态**判定。

---

## 3. Novelty——逐字核对的竞品边界【核对】

三个 verifier 独立对 tex 原文核实，全部 `found_verbatim=true`（一处拼接引用已标注精确化处理）。

### SaC（Software as Content，2026-03 预印本，最危险的范式近邻）
`6.2.Limitation.tex`：
- L17（逐字）：*"The current implementation does not perfectly support write-capable agent execution—the agent can retrieve and synthesise information from the environment, but cannot act on it: submitting forms, writing data, triggering external services, or modifying state in external systems."*
- L19（逐字）：*"Write-capable execution requires solving a set of interconnected backend problems: reliable agent control over external systems with heterogeneous APIs, transactional consistency when multi-step workflows partially fail, and frontend state synchronisation when agent-initiated writes change the ground truth that the agentic application displays."*
- L18（被我们省略号跳过的中间句，引用时需显式标注 elision）：*"This is not a gap that inference speed or prompt engineering will close."*
- intro L67（逐字）：*"The application itself becomes the interaction state: persistent, structured, and directly manipulable by both parties."*
- 6.2 L28（逐字）：*"…the evaluation … is an existence proof, not an empirical characterisation."*
- 6.2 L31（逐字，结尾 "as an interaction modality"）：*"…there is currently no established benchmark for evaluating agentic applications as an interaction modality."*

关键区分：SaC 的 generated application **本身就是 interaction state**（top-down 从 query 生成**新** app，app 即真理来源）；我们的界面是**多个既有应用实时状态的投影/缓存**（bottom-up 反向编译，real state 留在真实 app 里，surface **永不是** source of truth）。

### DuetUI（CHI'26）
- `5-Implementation.tex` L23（逐字）：*"The ServiceAgent executes calls to external services (simulated via LLMs, and are detailed within the attached materials) to fulfill data requirements defined in the task plan."*
- `8-Discussion.tex` sec:mock（逐字，3 段拼接，引用时按 3 个独立片段处理，`'trust gap'` 写成 LaTeX ``trust gap''）：*"…we adopted a simulation-based approach for external services…" / "…simulation alone does not resolve the ``trust gap''…" / "Consequently, we suggest that future deployments prioritize the development of interactive mechanisms for real-time source verification and iterative error repair to establish long-term user trust."*
- 技术评测：GPT-4 同源（persona 生成 + 数据集 + baseline + LLM-as-judge，双评 + 人工复核 + κ>0.95，同源偏差仍在）；Full Loop W-F1=0.508，Data F1=0.367，loop 提 Recall 降 Precision。用户研究 2 条件 vs Stitch，N=24，总任务时间**未显著**降低（188.38s vs 195.38s, p=.5545），收益只在沟通成本。
- DuetUI 自陈"proximity to a fixed Ground Truth is an insufficient proxy"——它**观察到**了安全压缩前沿问题，但只当 metric artifact 哀叹，没形式化为一等指标。→ 这正是我们 SCF 的切口。

### Macaron-A2UI【开源第三方工作，COLM 2026，Mind Lab 团队】
- `2-relatedworks.tex` L5（逐字）：*"In contrast to these lines of work, we focus on assistant-side Generative UI rather than action execution over an existing interface."*
- `3-problem.tex` L5：声明式协议（A2UI v0.8）+ 可信组件 catalog 渲染，**不**生成 HTML/JS/framework 代码。
- 这是 **CHI 工作天然占据的缝隙**：Macaron 明确把"对既有界面的动作执行"排除在外。CHI 工作 = Macaron 主动让出的那一半。
### AOHP（Android Open Harness Project，清华/北大/港大，2026-06 技术报告，已深读核对）【结论：不撞车，仅为相邻工作】
`main.tex` 摘要与正文核对：
- AOHP 是 **fork AOSP 的操作系统级 agent harness**，核心是让 agent 成为"OS 一等公民"：personalized service composition（把多个 App 聚合成"购物入口"等任务级入口）+ efficient agent interfaces（结构化 UI、后台虚拟屏幕并行、事件流）+ secure information flow（敏感数据 vault 化 + taint tracking）。
- 表面相似点：它的 "Generated Service Entrances"（§Personalized Service Composition）把多个 App 聚合为一个任务级入口，词汇上和我们的"任务界面聚合多应用状态"接近。
- 实质区别（三条，构成不撞车的理由）：(a) AOHP 改的是**操作系统内核/框架层**（AOSP fork），解决的是"agent 如何高效访问系统资源"，不是"如何把多个 App 的真实状态压缩为可编辑任务界面"；(b) AOHP **没有 round-trip verification 闭环**——它的评测指标是任务完成率（+21.12pp）、token 成本（-51.55%）、执行时延，全篇没有"用户编辑任务变量→写回验证→界面重新同步"这套机制；(c) AOHP 的 personalization 是"跨 App 复用用户偏好记忆"（如送货时间偏好从一个购物 App 迁移到另一个），不是"同一任务变量在多个 App 间的语义绑定与依赖传播"。
- 可借鉴的具体工程做法（非概念，纯方法论）：其 benchmark 用 **checkpoint-weighted completion rate**（30 个真实任务，按目标 checkpoint 打分，允许部分完成给部分分），比二元成功/失败更细粒度，值得在 TaskVM 的 verifier 打分设计中借鉴。
- **结论**：AOHP 作为 related work 的"系统层相邻工作"引用（同属"重新设计 agent 与 OS/App 交互基础设施"这个大方向），不构成 novelty 威胁，不需要额外的切割论证。

### Sidekick【UIST'26，2026-07-20 发布，今天新增】
- `5_user_study.tex` L20（逐字）：*"Sidekick is not an agent or CUA, but a prototype system that serves as a communication layer for users to multitask with CUAs. It does not perform actions on the computer, but only provides feedback to users."*
- `7_discussion.tex` L19（逐字）：*"…currently focuses on a single CUA performing one task within one window, whereas real-world settings may involve multiple agents operating across different workspaces…"*
- 它的"验证"是 `4_system.tex` L14 的 before/after 截图 VLM(gemini-2.5-flash) 错误检测（判 CUA 这一步有没有达成 subgoal），**不是** round-trip verification。
- 边界 (a)-(d) 全部确认：(a) 不绑 live app state（在隔离 macOS VM 里观察 CUA 的独立 overlay 窗口）；(b) 不写回、不执行（唯一能动的是 pause CUA）；(c) 无独立 ground-truth verifier（自指 goal-match）；(d) 无跨应用传播（单 CUA/单窗口，跨 agent 是 future work）。
- 一句话区分：**Sidekick 压缩的是 actions；我们压缩的是 applications。Sidekick 的界面是 Agent 的仪表盘；我们的界面是软件世界的控制台。**
- Sidekick 的 PT(外围文本) baseline 没打赢 chat-only，还制造"虚假安全感"——设计我们 4 条件研究时要吸取。

### 能 claim / 不能 claim
**能 claim**：首次把不受统一 contract 约束的既有软件，运行时接入同一个可执行任务投影，并用独立 verifier 证明 round-trip fidelity。
**不能 claim**：首次 task-centric computing（Activity-Centric 2006 已有）；首次 agent 生成任务 UI（DuetUI 已占）；首次 GUI+MCP hybrid（赛道拥挤）；首次动态 app 作交互媒介（SaC 已占）。

---

## 4. 三方撞车独立判断 + "一眼拉开差距"设计【新增·我自己的判断】

> 用户要求：靠我自己的判断（不受差异 doc 影响），评估 DuetUI/SaC/Sidekick 三篇与我们的正面撞车程度，并从 **demo 具体展示** 和 **顶层设计哲学** 两角度，设计让人"看之前觉得像、看完一眼就懂区分点"的呈现。

### 4.1 撞车程度的独立判断（按严重性排序）

**① SaC —— 撞车最重，但撞的是"大概念"不是"技术问题"。**
SaC 已经占了"动态生成的 app 是人-Agent 交互媒介"这个宏观主张，而且讲得比我们早、比我们透。如果我们还敢 claim"首次让动态 app 当交互层"，就是送死。**但** SaC 自己逐字承认它做不了写回、做不了事务一致性、做不了前端↔ground-truth 同步，且自评是"existence proof not empirical characterisation"、没有 benchmark。所以正确定位是：**SaC 是父范式（parent paradigm），不是竞品**——我们研究的是"当一个 SaC 式的 surface 必须忠实代表并写回多个真实既有 app 时，会发生什么"。源真理归属（source-of-truth）是那把救命的刀：SaC 里 app **就是** state；我们里 surface **只是投影**，real state 留在真实 app。这一刀把 SaC 从"撞车"变成"我们补它没解决的那一半"。

**② DuetUI —— 表面像、骨子里正交。**
表面都是"生成一个任务 UI + 人操作 + 人-Agent 循环"。但 DuetUI 是 **top-down**（prompt→任务分解→界面），服务是 **LLM 模拟**，人是**高频 co-generator**（每次操作都在重塑意图），UI 是"帮 Agent 理解人想做什么"。我们是 **bottom-up**（真实应用实时状态→反向编译），**真实执行**，人是**低频**（偶尔改个任务变量），UI 是"帮人理解 Agent 正在改变的世界"。DuetUI 连真实 app 都不碰。撞车只在"懒读摘要的审稿人"层面，不在实质。风险是 framing，不是技术。

**③ Sidekick —— 实质撞车最低，但 demo 混淆风险最高（且最新、审稿人记忆最新）。**
Sidekick 实质上是纯反馈/通信层：不执行、不写回、不绑 live state、不跨应用、无独立 verifier。它和我们在每个承重轴上都正交。**但** 它是 7 月 20 号刚发的，审稿人脑子里最新鲜；而且它的表面词汇（CUA + 侧边面板 + 多模态反馈 + 人间歇查看）和一个**偷懒的 TaskVM demo** 最像。如果我们的 demo 一开场是"Calendar Agent 80% / Jira Agent 60% / Docs 完成 / 发现冲突 / [暂停][查看日志][继续]"——那就是一个跨应用版 Sidekick，死路。所以 Sidekick 的威胁不在思想重叠，而在 **demo 纪律**：绝不能用"状态仪表盘"开场。

**一句话总结我的判断**：三篇不是一堵墙，而是各缺一个不同的锚点——DuetUI 缺 live state/executable binding（它是 UI-from-intent），SaC 缺 existing applications/round-trip（app-IS-state，不写回），Sidekick 缺 executable binding/live state/round-trip/cross-app（feedback-only）。**只有我们四个锚点同时在。** 防御策略因此各不相同：SaC=让出大概念、楔入 source-of-truth；DuetUI=锐化 top-down-vs-bottom-up + 模拟-vs-真实；Sidekick=demo 纪律（用"操纵+写回+verifier"开场，绝不用状态仪表盘开场）。

### 4.2 顶层设计哲学：一个判别问题让人一眼分清四者

核心判别问题（一句话，适用于所有四者）：**这个界面是"什么的投影"？操纵它会"改变什么、是否被验证"？**

| 工作 | 界面是什么的投影 | 操纵它改变什么 | 有无独立验证 |
|---|---|---|---|
| **DuetUI** | Agent 的任务分解 | 重塑人的意图（top-down 共生成） | 无（GPT-4 同源 judge） |
| **SaC** | 界面**就是** state（自洽生成 app） | 演化这个 app 本身 | 无（existence proof） |
| **Sidekick** | Agent 的动作 | 什么都不改（只观察/暂停） | 仅动作级错误检测 |
| **TaskVM（我们）** | **多个真实应用的实时状态** | **写回那些真实应用** | **独立 verifier 读 ground-truth** |

哲学一句话：DuetUI 让 UI 帮 Agent 理解人；SaC 让 app 成为 state 本身；Sidekick 让 UI 当 Agent 的仪表盘；**我们让 UI 当软件世界的控制台——一个忠实、可操纵、可验证的真实应用投影；你改变的是世界，不是 Agent 的计划。**

### 4.3 demo 具体展示：一个三篇都做不到的开场弧

签名 demo 动作（单一连续弧，三篇**任何一篇都做不到**，看完即分清）：

```
1. 用户在任务面把"发布日期"8/14 拖到 8/18
   → 任务面更新（DuetUI 也能更新界面，但它是重新生成，不是来自真实状态）

2. 旁边的真实 Calendar 会议真的移到 8/18；真实 TaskBoard 依赖 deadline 真的改了；
   真实 Docs 日期真的更新了——任务面与三个真实 app 并排实时同步显示
   （DuetUI 做不到：模拟；SaC 做不到：不写回；Sidekick 做不到：从不执行）

3. 无关的会议/任务纹丝不动——verifier 弹出"非干涉确认"：
   4 个目标变更全部发生 / 11 个无关对象零变更
   （三篇都没有 verifier）

4. 这时一个同事在外部把 Jira deadline 改成了 8/20
   → 任务面那个字段变琥珀/红，显示"底层已变: Jira deadline 现 8/20（你设的是 8/18）"，
   给出合并选项
   （这是 reconciliation——正是 SaC 逐字交出的"frontend state synchronisation"未来工作；
    DuetUI/Sidekick 压根没有这层）
```

这段弧同时把四个锚点全部演出来，且三篇竞品**物理上无法复现任何一步的"真实写回+验证"**。这就是"看完一眼就明白"的时刻。

**第二个签名实验（"JVM moment"·应用替换不变性）**：同一 task edit（release_date=9/8）跨 Stack A（Google Calendar+Jira+Docs+Gmail）与 Stack B（Outlook+Linear+Notion+Outlook Mail）——任务面外观稳定、用户操作相同、最终任务语义一致、底层轨迹完全不同、用户无需重新学 app、无关状态不动。这证明任务交互与应用实现解耦——SaC/DuetUI/Sidekick 都不要求也不演示这个。CHI 2027 内做"小版"（2 stack × 2-3 app）当一个 figure，全量 Stack A/B/C 放未来。

> 把这两段写进 teaser figure + intro 的"我们做什么"框，审稿人从第 1 页就能把四者分开。

---

## 5. 二等（衍生）贡献：安全压缩前沿（Safe Compression Frontier）【已拍板：降级，见 §15-Q4】

> **地位锁定**：SCF **不是一等贡献**，一等贡献是 §1 的核心构念本身——**可执行投影保真性（Executable Projection Fidelity）**，即"任务界面是多个真实应用状态的忠实、可执行、可验证的投影"这件事本身。SCF 是在这个投影已经成立的前提下，一个重要但衍生的次级问题——"投影在简化时如何不丢失关键信息"。降级理由：①"新交互范式"这个一等主张本身已经和 SaC 的语言（"the application itself becomes the interaction state"）非常接近，容易被不细读的审稿人认为撞车，因此论文的第一道防线必须是**保真投影 + round-trip verification**这一条独一无二的技术主张（三篇竞品都做不到，见 §4.3 demo 弧），而不是压缩策略；②SCF 若强行升格为一等，需要额外搭一个 `projection_policy` 策略模块并跑 Pareto 实验，在 6 周窗口内会挤占 W1 kill test 的开发资源，直接威胁唯一的真实 gate。
>
> **落地含义**：`projection_policy` 模块**保留**在架构里（§8），但降级为"当前版本用规则/简单启发式实现，不追求 Pareto 前沿实验"；SCF 的完整三轴测量（coverage×round-trip-fidelity×interaction-compression）与 Pareto 前沿实验，写入 Discussion / Future Work，作为"我们的框架为下一步研究这个问题提供了必要的度量基础设施"，而不是本文的核心实验章节。

- **人本矛盾（保留，作为 Discussion 里的次级问题陈述）**：界面必须压缩才有价值 ∩ 必须保留会改变结果的区别 ∩ Agent 不应替用户决定什么区别重要。简化=删除区别=价值判断。模型更强不能消除。
- **研究问题（保留，但不作为本文一等实验）**：给定任务 + live state，系统能否自动找出**最小但足够**的人可见可操作自由度集，使得 (a) coverage（每个需要的状态改变可表达）(b) round-trip fidelity / non-interference（每个会改变结果的区别被显形）(c) compression（无关的东西不塞进来）。
- **"可玩"由此涌现**（不是硬造，仍是叙事资产，但不需要专门模块支撑）：近单调执行任务 → 进度轨；有真实 trade-off → 少量控制旋钮/分支 gallery；不可压缩探索/认知 → 透镜（聚合、定位冲突、导航，不替人操纵）。**W1-W4 用规则/启发式决定用哪一层即可，不需要学出一个策略模块。**
- **为什么不是 GenUI/DuetUI**：不学"生成什么 widget"（Macaron 已商品化），不学"用 UI 共塑模糊意图"（DuetUI 已占）；学的是**哪些维度可被安全编译掉、哪些必须作为人可见可操作自由度保留**——fidelity-governed projection，不是 UI 生成。这句话仍然成立，只是现在挂在"投影保真性"这个一等构念下面作为其中一个属性，而不是独立的一等贡献。

---

## 6. 四项锁定决策（已全部拍板，无待确认项）

1. **Benchmark = 混合**：3 白盒自建（Calendar/TaskBoard/Drive，sqlite 后端）+ 1 held-out 黑盒（对模型黑盒/对 verifier 白盒 via state adapter）。benchmark 持有隐藏 canonical task graph + DB 映射作 verifier GT；模型推理时只见 screenshot/DOM/a11y/tool schema/trajectory，**永不接触 DB**。held-out 单元采用**两者都要**（见 §15-Q7）：1 个真未见 app（验迁移）+ 已见 app 的 rename/reskin/schema 变体（验反捷径），分别报 OOD 指标。
2. **一等贡献是可执行投影本身（见 §1/§5），而非 SCF**。SCF 降级为衍生贡献，详见 §5。
3. **训练 = 诚实 Go/No-Go**：OOD 先设计得足够难以真实区分规则/prompt/学习；有 gap 才训轻量 QLoRA critic，无 gap 则 train-free。**绝不为了 tech-heavy 硬训。verifier 永远来自环境状态，绝不让生成 binding 的模型自评。**
4. **用户研究 = 4 条件**（已锁定）：C0 原始多 app GUI / C1 静态只读聚合 dashboard / C2 chat agent + 全 app 工具访问（Claude/GPT + 3 app MCP 工具，真正的 non-inferiority 对手）/ C3 我们的投影。非劣性 margin = C3 成功率不低于 C2 的5个百分点；N=18 within-subject（见 §15-Q10）。

---

## 7. AI 侧：训什么 / 不训什么 + 协议与 Macaron 关系（已全部拍板，见 §15-Q6）

- **不训** UI 生成器，**不部署 Macaron 的已训练模型**（Grande/Venti）。主线直接调用前沿通用模型（GPT-5.6-sol / Claude-Sonnet-5 类）生成 UI，并将 A2UI v0.8 的完整协议规范（`surfaceUpdate`/`dataModelUpdate`/`beginRendering`/`deleteSurface` 四种消息类型的 schema 定义，几千 token）直接注入 system prompt，不需要 skill 机制，这正是 Macaron 论文自己对标的 full-prompt baseline 做法。**不下载不部署 Macaron 模型 → 不算复用其训练产物，也就不 pin 死在 A2UI v0.8**（协议升级只需换 prompt 里的 spec 文本）。若后期因成本考虑改用本地部署的轻量开源模型走 schema-light 路线，届时才真正涉及下载 Macaron checkpoint，但非 W1-W4 主线。
- **不训** GUI executor（UI-TARS/GUI-Owl/UI-Venus/Claude Computer Use 已在做，复用）。**明确两个模型角色分工**：(1) Agentic UI 生成模型——把多 app 状态编译成任务界面；(2) Compute-use 执行模型——在真实 App 里操作。两者上下文独立，即使都用同一个厂商的模型（如同时用 GPT-5.6），也是两次独立调用，不共享 context。
- **唯一值得学**：`Cross-App Binding Critic / Task World Compiler`——一个任务变量在异质应用、不同字段名、不同 GUI 表达中分别对应哪些真实状态与操作；改它后哪些关联状态必须同步（effect propagation）。规则写不死：同名异义、异名同义、同字段不同任务语义、跨应用 effect 传播。
  - 标签来源：cloned sandbox 候选 binding → 读真实 before/after state diff → 与隐藏 expected diff 比 → 完全匹配正例、漏改/错改/多改 hard negative。**非 LLM 自评，非研究者逐条标。**
  - 反捷径：同工具不同参数→相反判断；不同工具同 effect→相同判断；重命名/换肤后仍有效；`tool-name-only baseline` 必须在重命名/反标签上显著下降。
  - 形态：3B–7B 多模态/语言 critic，QLoRA/轻量 SFT。
- **决策点（W4）**：seen app prompt≈92% 但 renamed≈71%、unseen≈54%、错误集中在同名异义/隐含依赖时才训。否则 train-free。
- **Macaron 关系最终锁定**：**differentiate + 选择性复用基础设施**——CHI 工作明确占据 Macaron 让出的"对既有界面的动作执行"缝隙（不撞，Macaron-A2UI 是与本项目无归属关系的开源第三方工作，非本项目前作）；可选择性复用其公开的 A2UI-Bench 评测思路/judge prompts 作参考，但**不复用其训练模型、不 pin 死在 A2UI v0.8**（理由同上）。

---

## 8. 系统架构骨架（我要搭的代码框架）

```
taskvm/
├── apps/                  # 自建可重置 Web 应用（sqlite 后端，目录结构参考 SenseAct 的 scenarios/<name>/engine/{reward,injector,*_db}.py 模式）
│   ├── calendar/  taskboard/  drive/
│   │   └── engine/        # reward.py(判定成功) / injector.py(注入初始状态+可选的外部并发修改注入，见 §15-E-Q11) / *_db.py
│   └── _heldout/          # held-out 黑盒 app（OOD；对模型黑盒，对 verifier 白盒 via state adapter）
├── harness/               # browser_controller(Playwright) / state_adapter(reset·seed·read-canonical) / trace_capture / replay_engine / shadow_txn(copy-on-write 影子执行)
├── task_state/            # representation / compiler(Apps→TaskWorld) / entity_binding / dependency_graph / projection_policy(规则/启发式实现，不追求 Pareto，见 §5)
├── execution/             # patch_compiler(编辑→semantic patch) / replanner / action_dispatcher(GUI/MCP/API hybrid)
├── verifier/              # app_state_checks / cross_app_checks / non_interference / round_trip_checks / reconciliation
├── workspace_ui/          # renderer / editable_components / live_sync（先结构化文本/表单，不追求花哨）
├── benchmark/             # task_templates(40) / initial_states(隐藏 canonical graph) / user_edits / ood_splits / live_runs；参考 SenseAct 的 cost_model.py 真实 token 计量方式做 API 成本追踪
├── baselines/             # 规则/类型匹配·prompt-only·frontier+shadow·人工 binding 上界·规则+critic
├── user_study/            # 4 条件
└── evaluation/  docker-compose.yml  README.md
```
**开工第一周只动**：`apps/{calendar,taskboard}`(极简可重置) + `harness/{state_adapter,replay_engine,trace_capture}` + `task_state/{representation,compiler(frontier API)}` + `verifier/round_trip_checks` + `workspace_ui/renderer`(结构化文本/表单)。**先 replay-mode**，跑通 compiler→UI→patch→执行→verifier。

> **仓库/代码命名**：目录名建议由 `a2ui` 迁往 `taskvm`；若迁移成本过高，允许保留 `a2ui/` 作为仓库目录但内部模块/类名统一用 `TaskVM` 前缀，不影响开工进度，由 coding agent 自行决定。

---

## 9. 指标（GT 全部来自 sandbox 隐藏 canonical state，非模型自评）

七项自动指标 + OOD：`Projection Coverage` / `Binding Accuracy` / `Round-Trip Fidelity` / `Non-Interference` / `Reconciliation Accuracy` / `Cross-App Consistency` / `Interaction Compression` + `OOD Generalization`。
核心技术目标：最大化 interaction compression 同时维持 task projection fidelity（§5 Pareto 前沿）。
汇报三层：①Agent 仍能完成任务（E2E success/action数/latency/cost/non-inferiority vs C2 chat-agent）；②**任务界面是否忠实可执行**（coverage/binding/round-trip/non-interference/reconciliation/OOD）——技术主指标；③人是否真受益（识别跨应用错误正确率/修改耗时/低层操作数/额外文字解释次数/最终是否符合用户修改）。

四层评测对应闭环不同阶段：(1) 任务状态编译、(2) 在线同步、(3) 用户修改→执行结果、(4) 完整 round-trip。

---

## 10. W1 kill test（方向是否成立的判据，开工第一周只做这个）

```
2 个 Web 应用（Calendar + TaskBoard）
→ Agent 在线执行（frontier CUA API，不训练）
→ 实时任务界面（先结构化文本/简单表单，不追求花哨）
→ 用户修改一个任务变量（如：发布日期 8/14 → 8/18）
→ Agent 跨应用落实（Calendar 会议移动 + TaskBoard 依赖 deadline 同步）
→ 独立 verifier 用隐藏 canonical state 判定：
   ✓ 改的发生（两 app 都改对）  ✓ 没改的不动  ✓ 界面重新同步
```
**三个 sub-kill**：① round-trip 跑不通 → 立即收缩到方向二（少量 typed cross-app operators），不加更多 UI/模型；② 规则系统在 tool/app OOD 上已和模型一样好 → 删除训练；③ 只有"每个任务手写 React 页 + binding"才跑得通 → 停（那是定制 dashboard，不是 software compiler）。
**W1 不训练、不上 OSWorld、不接真实商业账户、不做花哨 UI。** 先 replay-mode 跑通整条链，再小规模 live。

---

## 11. 排期——6 周压缩版（API-first + 自动评测为脊柱 + 末尾人类验证）【按用户确认修订】

用户确认：只剩 6 周、无延期；工作主要依赖 API 调用，人类验证只在末尾，前面全自动评测。这是一篇 tech-heavy HCI 工作，自动评测是主体、用户研究是确认。**所以 6 周可行**，前提是：①W1 kill test 通过（无方向危机）；②默认 train-free（无训练弯路）；③用户研究后置非阻塞（自动评测是论文主体）；④范围死冻（3 app、1 任务族、无 OSWorld/跨设备/训练）。

| 周 | 日期 | 目标 | 必须完成的纵向切片 |
|---|---|---|---|
| **W1** | 7/30–8/6 | **Kill test**（唯一 gate） | 2 app（Calendar+TaskBoard）replay-mode + frontier CUA API；跑通 compiler→UI→patch→执行→verifier 整条链。不训练。跑不通即收缩方向二 |
| **W2** | 8/7–8/13 | 第 3 app + live 小规模 + 投影 UI | Drive + state adapter 泛化 + live-mode 小规模 + 结构化投影 UI（不花哨）+ stale-state 检测 |
| **W3** | 8/14–8/20 | **Benchmark + 基线 + 自动主实验**（论文主表来源） | 30-50 模板→500-1500 实例 + 隐藏 canonical graph + 各 baseline（规则/prompt/人工 binding）+ 7 指标 overnight API 跑完 |
| **W4** | 8/21–8/27 | OOD + 签名实验 + 训练 Go/No-Go + 失败分析 | rename/reskin/unseen-app OOD split + app-substitution 不变性小版 + reconciliation demo + 训练决策（大概率 train-free）+ failure analysis |
| **W5** | 8/28–9/3 | 用户研究 + 论文主体并行 | 若 IRB 就绪：~12-18 人 within-subject 4 条件；并行写 Intro/RW/System/Benchmark/Eval |
| **W6** | 9/4–9/10 | 冻结 + 投稿 | 重跑终值 + 统计/可视化 + ablation + figure + demo 视频 + 匿名化 + 全文收敛 + 投稿 |

**最关键风险**：W1/W4 的 round-trip 可靠性。若 compiler 不能可靠把任务变量绑到正确的真实 app 对象、actuator 不能可靠写回——无论时间够不够，论文核心都站不住。所以 W1 是真 kill test，不是里程碑。**W8 风格的功能冻结**：禁止新增 OSWorld 全量/记忆/长期 history/UI 风格生成/多任务族/通用 Agent OS/训练（除非 W4 看到 OOD gap）。

> 注：原 8 周 plan 的 W2-W8 纵向切片纪律仍参考，但按 6 周压缩——W1+W2 合并环境与 kill test，W3 自动评测提前成脊柱，W5 用户研究与写作并行，W6 冻结投稿。

---

## 12. 三条落地形态 + 3 RQs

**落地 = 同时是三样东西**：
- **产品 Demo**：用户交给 Agent 一个跨应用任务，任务逐渐生长成一个持续存在的小型应用；用户直接操作这个任务应用，Agent 在后台操作真实软件。
- **研究系统**：读 GUI/tool 执行 → 维护任务状态 → 生成可编辑界面 → 把用户修改传播到多应用 → 自动检测不同步与错误。
- **研究评价平台**：独立回答任务 UI 是否正确、用户修改是否真执行、跨应用是否一致、Agent 成功率损失多少、用户是否更高效准确。

**三个研究问题**：
- **RQ1（系统正确性）**：能否从在线 GUI/tool 轨迹中准确维护一个实时任务状态？
- **RQ2（可执行控制）**：用户对任务状态的修改能否被正确传播到多个应用，并保持最终状态一致？
- **RQ3（人类价值 + 自动化代价）**：与聊天和轨迹式控制相比，任务界面能否提升理解/错误发现/修改效率，同时保持 Agent 任务成功率与自动化收益？

---

## 13. 负样本边界（不做什么）

- ❌ 漂亮 Agent Dashboard，但不能写回真实应用（只是可视化）。
- ❌ 把每步轨迹变成可编辑卡片（仍是 trajectory supervision，与 Magentic-UI 增量）。
- ❌ 动态生成任意 UI、研究"该用列表还是看板"（与 DuetUI/SaC 正面重合）。
- ❌ 通用 Agent OS / 所有软件所有任务长期记忆多设备（不可验证）。
- ❌ 只做用户研究、没有自动保真证明（不符合技术定位）。
- ❌ 为 tech-heavy 强行训练 text-to-UI 模型（训练对象不是瓶颈）。
- ❌ 手工为每个任务写 React 页面和 binding（那是定制 dashboard，不是 software compiler）。
- ❌ 信任/同意/技能漂移/来源/未来复用/策略编辑等同在一篇（概念膨胀，无主线）。
- ❌ 同一个模型生成 binding 又判断 binding 对错——最终 verifier 必须来自环境状态。
- ❌ 每个 app 手写全部 binding——会杀死"动态编译既有软件"的 claim，核心 binding 必须至少部分自动发现。
- ❌ demo 开场像"四 agent 进度条 + 暂停/查看日志/继续"——那是跨应用版 Sidekick，死路（见 §4.3）。

---

## 14. 范式上限八条（更强模型也消不掉的天花板，SCF 的 justification）

1. **压缩 vs 保真 trilemma（|U|<<|W|）**：界面把真实状态集 W 映射到可见状态集 U，|U|<<|W|，多个不同真实态塌缩成一个界面态。三难：高度简化 ∩ 保留潜在重要细节 ∩ 不让 Agent 决定什么细节重要——近乎不可同时。简化=删区别=价值判断。
2. **任务非一维/单调**：很多任务是 Pareto trade-off（成本↔舒适↔时间↔风险），没有唯一"向右"。进度条只适合单调任务；分支/地图/时间轴/多旋钮适合多目标。
3. **无 canonical task skeleton**：同一组软件状态可对应完全不同的任务理解（进度恢复/团队健康/客户关系）。界面选择哪些对象，本身在定义问题。
4. **过程即价值**：阅读/创作/谈判/诊断等任务的价值就在过程中，删除过程=改变任务，不是优化任务。
5. **跨设备 ≠ 跨权限**：不同设备/应用属不同主体，强模型不能替合法主体消除冲突。
6. **无原子跨应用事务**：独立真实系统不共享事务协议，存在部分失败/过时/不可逆，必须承认"部分实现/等待同步/需要补偿"状态。
7. **错误爆炸半径**：抽象层压缩操作数 = 压缩错误传播距离；交互压缩率越高，单个操作因果范围越大，用户越需要结果可见性。
8. **视觉可变、交互语法不可变**：配色/动画/密度可个性化，但"什么表示未执行/执行中/已完成、什么会产生外部效果、如何撤销、异常如何显示"必须稳定，否则无法积累肌肉记忆与可迁移技能。

**诚实上限**（不是"所有任务变成进度条"）：把机器产生的偶然复杂性吸收掉，把人类真正需要决定的复杂性重新显形。简单任务=一根进度条；复杂任务=一间驾驶舱；不可压缩任务=一副更好的透镜。真正的研究高度：不是研究 agent 能生成多炫的 UI，而是研究**一个任务哪些维度可以被安全编译掉、哪些必须作为人可见可操作的自由度保留**。

---

## 15. 15 个开放问题的最终拍板记录（已全部完成，供后续 coding agent 直接执行）

> 本节是 15 个开放问题（含 §G 新增第 16 项）经多轮讨论后的最终决策记录，**全部已拍板、无遗留待确认项**，coding agent 可直接按此开工。

### A. 时间与命名（已锁定）
1. **6 周现实**。**已锁定**：跑 §11 的 6 周压缩版，无延期、不转 UIST。用户补充：项目采用多 coding agent 并行开发模式（非单人手写），6 周窗口下内容量可行性高于传统人工开发估计，不需要因此额外压缩范围。
2. **命名：A2UI → TaskVM**。**已锁定并完成**（见文档开头说明）。决策依据：`A2UI` 是 Google 2025 年发起的通用 agentic UI 声明式协议名（`a2ui.org` v0.8，可在企业内部资料交叉验证，包括行业跟进文档对 Google 2025 年底发布 A2UI 标准的多次独立记载），Macaron-A2UI（COLM 2026）是 Mind Lab 团队的开源第三方工作，与本项目及本团队无归属关系、不是本项目前作。三者（`A2UI` 协议 / Macaron-A2UI / 本项目）都不是本项目私有名字，继续用 `A2UI` 作项目代号会造成三方混淆，故项目代号改为 **TaskVM**，论文内部术语也统一用 TaskVM / Task-Native Interaction / Task Capsule 这一系统词汇（具体用哪个写论文标题由写作阶段再定，不影响开发）。TaskVM 项目在实现中仍可选择性复用 A2UI v0.8 协议作为 UI 渲染层的传输格式，二者不冲突（见 §7/Q6）。
3. **AOHP 深读**。**已完成**（已加入 `docs/references/AOHP-paper/`，已全文核对）。结论：**不撞车**，仅为相邻工作。详见 §3 新增小节。AOHP 改 OS 内核/框架层，无 round-trip verification，personalization 是跨 App 偏好记忆而非任务变量绑定，三点均与我们正交。可借鉴其 checkpoint-weighted completion rate 评分方式。

### B. 贡献栈（已锁定）
4. **Safe Compression Frontier 的地位。已锁定：降级为衍生贡献**，**一等贡献是可执行投影本身（Executable Projection Fidelity）**。用户确认的重定位理由：“新交互范式”这个提法本身容易与 SaC 语言撞车，而安全压缩虽重要但本质是保真投影这个一等命题下的次级衍生问题，而非原生问题。SCF 不建独立的 `projection_policy` 策略学习模块与 Pareto 实验，该部分写入 Discussion/Future Work。详见 §5。
5. **签名实验"application-substitution invariance"（JVM moment）**。**已锁定**：CHI 2027 内做"小版"（2 stack × 2-3 app）当一个 figure；全量 Stack A/B/C 放未来工作。用户无异议，直接按此执行。
6. **协议与 Macaron 关系。已锁定**：**differentiate + 选择性复用基础设施**。关键确认：Macaron-A2UI 是 Mind Lab 团队的开源第三方工作，与本项目及本团队无归属关系、不是本项目前作，不存在"建在其之上"的关系；`A2UI` 协议也非本项目私有。主线技术路线：直接调用前沿通用模型（GPT-5.6-sol/Claude-Sonnet-5）+ 完整 A2UI v0.8 协议 spec 注入 system prompt（不需要 skill 机制，这是 Macaron 论文自己对标的 full-prompt baseline 做法），**不下载不部署 Macaron 训练好的模型**（Grande/Venti）——因此不算复用其训练产物，也就不 pin 死在 A2UI v0.8（协议升级只需换 prompt 文本）。若未来因成本考虑改用本地部署的轻量开源模型走 schema-light 路线，届时才真正涉及 Macaron checkpoint 复用，但非主线。另需明确：**两个模型角色独立**（Agentic UI 生成 vs. compute-use 执行），即使同用一个厂商模型也是两次独立调用、不共享 context。

### C. Benchmark 与 OOD（已锁定，无异议）
7. **held-out OOD**。**已锁定：两者都要**——1 个真未见 app（验迁移）+ 已见 app 的 rename/reskin/schema 变体（验反捷径），分别报 OOD 指标。
8. **Mail app**。**已锁定：out**。Drive 作第 3 个 app，Mail 永久 optional。
9. **benchmark 规模**。**已锁定：40 模板 / 800 实例 / OOD 占 ~20%**。用户补充了 API 成本评估需求（见下方新增"API 调用量估算"）。

### D. 用户研究（已锁定）
10. **4 条件 + 非劣性 margin + N + IRB**。**已锁定**：4 条件（C0/C1/C2/C3）；非劣性 margin = C3 不低于 C2 的 5 个百分点；N=18 within-subject。IRB 实质：美欧高校强制要求的人类受试者研究伦理审查机制，风险低的研究（如本项目的可用性测试）多数机构有 expedited/exempt 通道（1-2 周可批）；若所在机构无正式 IRB，工业界背景 HCI 论文的常见做法是在文中自述遵循伦理准则（自愿参与、知情同意书、数据匿名化），不强制要求正式批文号即可投稿。**推荐**：若无正式 IRB 走轻量自述路径，W5 pilot + 后置正式，不阻塞投稿（自动评测仍为论文主体）。

### E. 机制设计
11. **reconciliation 机制**。**已锁定**：re-read-on-action(用户编辑/定时心跳触发重读)+ 冲突时标红不静默覆盖。**并发修改注入机制——已拍板：采纳折中方案**。不建"协作者"角色(benchmark 环境下无法真实模拟多人协作、测不出实质效果)，只在 `apps/<name>/engine/injector.py` 预留一个最简单的"benchmark 自己主动注入外部状态变更"接口(定时脚本或手动 trigger 直接向 DB insert 一条冲突事件，参考 SenseAct 的 `injector.py` 模式)，实现成本接近零。此接口列为 **W4+ 可选加分项，非 W1 核心路径**，不阻塞开工；W1-W3 reconciliation 先仅靠历史心跳重读验证，W4 视时间富余再接入该接口测试外部并发场景，测出的效果作为 §4.3 demo 第 4 步与 SaC 未来工作缺口的直接对照证据。
12. **跨设备**。**已锁定：appendix showcase**，不当贡献。
13. **wind-tunnel（CUA 模拟用户）基础设施**。**已锁定：推迟，本期不建**。

### F. 心智模型（已锁定，含重要激发性修正）
14. **"滑块愿景"重定位 + 人的决策口径。已锁定，但经用户重要修正**：用户明确接受重定位（不同任务获得它能诚实承受的最简单界面，不需要硬拗滑块），并补充了滑块当初的真正目的：**激发玩乐心理、降低认知负载**，不是字面意义上的单一控件。**关键修正（重要）**：关于人的角色，用户明确拒绝了"低频授权节点"这个提法因为它听起来太像 Sidekick 的人工审核/监控范式（用户原话："我特别讨厌这种人类中制介入的研究，因为它太老了"）。正确口径是：**人在任务开始时主动设定这次要做到哪个 milestone/checkpoint**（而非模型自行判断边界并自动停下等人审核）；安全性/边界检查等后续环节全部下放给自动化 verifier，不需要人在提交前做最终审核。这个区别很关键：**人控制的是"要做到哪里"，不控制"怎么验证安全"**。
15. **人的角色口径。已锁定**：因 Q14 修正而重新确定为"**任务起始时的主动 milestone 设定者**"，而非"低频授权节点"。论文措辞应避免任何会被读成"人审核 Agent 结果、确认后才能继续"的句式（那是 Sidekick 路线），而应强调"人在事前设定目标深度/checkpoint，模型自主完成到该深度为止，安全性由自动 verifier 全程接管"。

### G. 心智模型泛化(用户新提，非原 15 之一，单独记录)
16. **"统一信息实体"代表用户心智模型——已拍板：限定为 task_state 的叙事包装**。用户原意是把故事讲得更形而上一点——底层存在一个"统一信息实体"，代表用户心智模型、统领跨 App 协调诉求，并通过 task 得到体现(参考自家 SenseAct 项目在 web bench 设计上"底层有一个用户画像"的思路，但 SenseAct 本身研究主动感知，与本项目话题关系不大，仅供设计思路借鉴)。**风险(已确认并采纳规避)**：该表述若不加限定，容易与 SaC intro 的语言("the application itself becomes the interaction state...converging toward personalized software")产生比降级前 SCF 更严重的撞车观感——因为它暗示存在一个独立于具体 App 的"更根本的真理来源"，与本项目"real state 永远留在真实 App 里、surface 只是投影，绝非 source of truth"的核心防御论点直接矛盾。**最终决定**："统一信息实体"仅限定为 `task_state` 数据结构本身的哲学化包装(即"同一任务变量在不同 App 里的绑定关系集合"这个已有技术含义)，**不**是独立于 task_state、本体论意义上更根本的新实体；**不建独立模块**，仅作为 Discussion 里的叙事升华措辞，不影响 §8 架构骨架。

---

## 17. 新增参考资料与工程借鉴（本轮新增）

- **AOHP 技术报告**（`docs/references/AOHP-paper/`）：已全文核对，结论为相邻工作、不撞车，可借鉴其 checkpoint-weighted completion rate 评分方式（详见 §3、§15-Q3）。
- **SenseAct 项目**（团队自家项目，路径 `/home/hadoop-mt-ocr/dolphinfs_ssd_hadoop-mt-ocr/zhangyuzhe09/SenseAct`，非本仓库内）：一个聚焦 agent 主动感知能力的独立 web-agent benchmark，与本项目在研究问题上无直接关系，但其工程结构高度可复用：
  - 目录模式 `scenarios/<name>/engine/{reward,injector,*_db}.py`（每个自建 App 配一个判成功的 `reward.py` + 注入初始/外部状态的 `injector.py`）——直接对应本项目 `apps/<name>/engine/` 的设计（已写入 §8 架构骨架）。
  - `senseact/cost_model.py` 的真实 token 计量方式（不用启发式估算，每次调用记录真实 usage）——用于本项目 benchmark 的 API 成本追踪（已写入 §8）。
  - `senseact/metrics.py` 的 SR + Success@Budget/cost-success AUC 组合，为"如何同时报告成功率与成本"提供了可执行范式。
- **API 调用量估算**（用户要求，非精确值，供预算参考）：以 40 模板/800 实例的 benchmark 全量跑一遍估算，单次完整跑测约 600-900 次 API 调用（Agentic UI 生成 60-90 次 + compute-use 执行 450-750 次 + round-trip 验证 60 次）；开发阶段（W1-W3）需反复跑同批任务调试 prompt/verifier，保守估计迭代 10-20 轮，累计约 6,000-18,000 次调用；若算上多方案探索性实验（如并行测试不同 UI 压缩策略），总量级达 20,000-30,000 次也属合理区间。**此估算精度有限**（未知具体 prompt 长度、图片分辨率等参数），建议参照 SenseAct 的 `cost_model.py` 做法，实际跑几个 W1 kill test 任务后拿到真实数字用于校准，不需要现在纠结准确度。

---

## 18. 开工指引(面向执行的 coding agent，全部拍板已完成，零待确认事项)

1. **可直接开工**，全文 15+1 个开放问题(含 G 组新增的第 16 项)均已拍板锁定，**无任何遗留待确认项**。
2. 据 §8 骨架进 **plan 模式**，过一遍 W1 的具体代码方案(仓库骨架、Calendar/TaskBoard 极简可重置应用接口、canonical task graph 的隐藏与读取、replay 引擎、compiler/binding 的 frontier-API 调用契约、verifier 的 round-trip 判定逻辑)。
3. 开工即守：项目代号为 **TaskVM**(不再用 A2UI 作代号)；这是**独立项目**，不拖入其他仓库；自动评测为主体、用户研究后置不阻塞；训练是 Go/No-Go 而非默认；不部署 Macaron 已训练模型，主线用前沿通用模型 + A2UI v0.8 协议 spec 注入 prompt；demo 开场用"操纵+写回+verifier"弧(§4.3)，绝不用状态仪表盘开场。
4. **两个收尾细节均已拍板**(见 §15-E-Q11、§15-G-Q16)：(a) `injector.py` 预留最简外部并发注入接口，列为 W4+ 可选加分项，不阻塞 W1-W3；(b) "统一信息实体"仅作为 `task_state` 的叙事化包装写入 Discussion，不建独立模块、不进 W1-W2 架构。coding agent 可按本文档直接落地，无需再向用户确认任何决策点。

---

# 附录 A：三个 CHI 方向候选 + 用户画像（用户补充，2026-07-30）

> 这是用户与 GPT 反复讨论后筛选出的三个"想做、且觉得有意思"的 CHI 方向。**方向 1 TaskVM = 我们已锁定的工作**（= 正文 §0 的 executable task projection over existing apps，即 HCI-UI 第 8 轮的 Task-Native Interaction / Task Capsule）。方向 2、3 是方向二/方向三的近亲，是**备选与下一论文候选**，不替换当前锁定主线——但它们揭示了一个重要的心智模型一致性，见附录 A.4。
> 三个方向共享一个底层直觉：**GUI Agent 时代，应该有一个介于"人"与"异质真实软件"之间的、可验证的中间运行时层**，把一次性 Prompt-Response 变成有状态、可增量改、可撤回、可写回的任务虚拟机。

## A.1 方向 1：TaskVM（= 当前锁定主线）

**TL;DR**：让 GUI Agent 把多个真实应用"编译"成一个临时任务界面，用户在新界面中的每次操作，都能写回原软件的真实状态。
GUI Agent 能否基于多个现有应用的运行状态，动态生成一个统一的 agentic UI，让用户不必等待 Agent 黑盒执行，而是可以直接查看、修改和推进任务？

`真实 App 状态 → Agent 理解任务与环境 → 生成统一任务界面 → 用户直接交互 → Agent 将操作翻译回原应用 → 环境状态更新`

重点：把 GUI Agent 和 agentic UI 变成类似 Java 虚拟机的跨软件中间层——底层应用不同，但用户始终在一个面向任务、可交互、可写回的界面中工作。
**最相似竞品**：Software as Content / Sidekick / DuetUI（= 正文 §4 已独立判定撞车程度与差异化设计）。

> 与正文的对应：方向 1 = §0 主线 = 四锚点 existing applications / live state / executable binding / round-trip verification。"TaskVM"现已正式定为项目代号（见 §15-A-Q2），本附录的叫法与正文一致，无需再对齐。**注意**：方向 1 的 TL;DR 与正文 §0 完全一致（真实状态→编译→界面→写回→更新），只是措辞用了"虚拟机中间层"比喻。

## A.2 方向 2：PromptPatch（下一论文候选 / 近方向二"可执行的平行世界"的运行时侧）

**TL;DR**：用户可以在 Agent 执行过程中修改 Prompt，系统自动保留仍然有效的工作，只重做受新要求影响的部分。
当用户在耗时、高成本的 Agent 任务执行过程中，才发现原始 Prompt 遗漏或写错了条件，系统能否将新指令可靠地写入正在运行的任务，而不是停止后从头执行？

`初始 Prompt → Agent 规划并执行 → 保存工具调用、中间结果和依赖关系 → 用户发送 Patch → 判断影响范围 → 保留有效进度 → 局部回滚或重规划 → 继续执行`

重点：让 Agent 从一次性的 Prompt–Response 系统，变成支持热更新、断点续跑和增量计算的任务虚拟机，避免用户只能在"继续等待错误结果"和"停止并浪费已有成本"之间选择。
**最相似竞品**：Cocoa / Interactive Debugging and Steering of Multi-Agent AI Systems / ChatGPT Interrupt·Update / Cursor Immediate Interrupt / Windsurf Cascade / Devin Queued Messages。

> 与正文的关系：这是 HCI-UI 方向二"可执行的平行软件世界"在"运行时增量改"侧的一个收敛变体——不 fork 平行分支，而是在单条轨迹上做"增量 patch + 影响范围判定 + 局部回滚"。它要求的核心基础设施（保存工具调用/中间结果/依赖、增量重规划、断点续跑）与 TaskVM 的 `execution/` 层（patch_compiler / replanner / shadow_txn）高度重叠——是 TaskVM 跑通后自然的下一篇。

## A.3 方向 3：Beyond Submit（早期被放弃的"commitment boundary"方向的精炼复活版）

**TL;DR**：Agent 可以利用用户提交前的输入过程提前理解任务，但用户删除或放弃的内容必须能够从 Agent 的状态中真正撤回。
当 Agent 能够在用户按下 Send 前持续观察输入、删除、停顿和改写时，系统能否利用这些过程信息提前理解和准备任务，同时保证未提交草稿仍属于用户的私人空间？

`输入轨迹 → Harness 选择性暴露信息 → Agent 进行可逆的提前推理 → 用户修改或删除 → 回滚相关计划与状态 → 用户正式提交 → Agent 才能持久化并执行外部动作`

重点：Send 不只是输入按钮，而是从"私人构思"到"正式表达"的 commit boundary。Harness 应成为输入事务管理器，支持 pre-commit、commit 和 rollback，避免已删除的草稿继续影响 Agent 的计划、记忆和工具调用，形成 draft residue。
**最相似竞品**：Can You See Me Think? / ExPerT / Incremental Dialogue Systems / I-BOX。

> 与正文的关系：这正是 HCI-UI Round 1-2 的"Preserving User Commitment Boundaries"方向，后被 novelty audit 放弃（commitment boundary 与 2026 LLM-reasoning 撞名、VeriSafe/EffectGuard 等已覆盖）。方向 3 把它**精炼**到"输入事务/草稿撤回"这个更窄、更新颖的切面（draft residue 是个有辨识度的构念）。它不进当前 6 周主线，但作为下一论文候选保留。

## A.4 三个方向揭示的心智模型一致性（重要——解释为什么用户喜欢这三个）

这三个方向表面不同，底层共享同一个研究品味，可作为"用户画像"锚点：

1. **都把 Agent 当成"有状态的中间运行时"，而不是"一次性的对话工具"。** TaskVM=跨软件任务虚拟机；PromptPatch=支持热更新/断点续跑的任务虚拟机；Beyond Submit=输入事务管理器。三者都是"给 Agent 加一层可验证的运行时状态层"。
2. **都聚焦"中途可改、可撤回、可写回"这种"非一次成型"的人机协作结构。** 不研究"Agent 怎么更好地一次完成"，而研究"Agent 执行到一半、或还没执行时，人如何低成本、可逆地介入与修正"。
3. **都把"诚实性/可验证性"当硬约束。** TaskVM=独立 verifier 读 ground-truth；PromptPatch=影响范围判定+保留有效进度（不能假装没改）；Beyond Submit=draft 必须真正撤回（不能留 residue）。三者都拒绝"让模型自己说自己对"。
4. **都选"窄而锋利"的切面，而非宏大范式。** 三个都先找一个"审稿人能秒懂痛点的具体场景"（写回/改 prompt/撤回草稿），再技术化，而不是一上来喊"新交互范式"。
5. **都把"输入/操作 ↔ 真实世界状态"的边界（commit boundary）作为核心物理量。** TaskVM 的 round-trip、PromptPatch 的影响范围、Beyond Submit 的 commit/rollback，本质都是在精确定义"什么时候一次输入真正对外生效、什么时候还没"。

**对正文的影响**：方向 1（TaskVM）已是主线，无变化。方向 2、3 不进 6 周主线，但：
- 方向 2 的核心需求（保存工具调用/依赖、增量重规划、断点续跑）应在 TaskVM 的 `execution/` 层**预留接口**，避免下一篇推倒重来。
- 方向 3 的 commit-boundary 思想可作为 TaskVM 中"用户编辑 → semantic patch → 执行"这一步的设计灵感：编辑未"提交"前是可逆的预览（shadow_txn 影子执行），提交后才真正写回并验证——这与正文 §8 的 `shadow_txn` 模块天然契合。

> 即：用户喜欢的这三个方向，本质是同一套"可验证的 Agent 运行时状态层"在不同生命周期阶段的实例化。TaskVM（跨软件写回）是其中最完整、最 tech-heavy、最适合作为 CHI 2027 主线的那一个。

---

# 附录 B：公司内部 API 调用方式（Friday/aigc 网关）与通用 OpenAI key+url 的核心区别

> 背景：`benchmark/model_client.py` 等模块最初按"通用 OpenAI-compatible：`base_url` + `api_key` 直连"心智模型来写，这在公司网关下**基本能跑但有几个关键差异点会踩坑**。本节参考团队既有实现（`/mnt/dolphinfs/.../yangwenkui03/wm/lds/internal_mesh_client.py`、`config.yaml`、`config_loader.py` + SenseAct 的 `build/synthesize/_client.py` / `scripts/run_eval.py`）梳理，并用 `2026-08-05` 当天对网关做的真实连通性 probe（API key `1925796454518841403`）交叉验证，所有结论均可复现核实。

## B.1 表面像 OpenAI SDK，但有 3 个不能偷懒的差异

1. **`base_url` 固定为公司网关，不是 `api.openai.com`**：`https://aigc.sankuai.com/v1/openai/native`（OpenAI SDK 用法上完全兼容，`OpenAI(api_key=..., base_url=...)` 直接可用；`chat.completions.create(...)` 接口不变）。这一点两份参考实现（`internal_mesh_client.py` 注释 + SenseAct `_client.py`）完全一致，可互相印证。
2. **`api_key` 不是"个人 OpenAI key"，而是"Friday 平台业务应用 token"，且与调用配额强绑定**：每个 App（业务方申请的 token）在**每个模型上分别有独立的 QPM 限流**，不是"一个 key 全模型共享一个总配额"。这是最容易踩的坑——本项目现在用的 key `1925796454518841403` 对 `gpt-5.5` / `claude-sonnet-5` / `claude-opus-5` / `gpt-5.6-terra` / `gpt-5.6-luna` / `kimi-k2.7-code` 这几个"热门旗舰模型"限流极紧（连续 8 秒间隔重试仍 429），但对 `gpt-5.6-sol`、`gemini-3-flash-preview`、`gemini-3.6-flash`、`glm-5.2`、`glm-5v-turbo`、`aws.claude-sonnet-4.6`、`aws.claude-opus-4.8`、`deepseek-v4-pro/flash`、`kimi-k3`、`MiniMax-M3`、`LongCat-2.0` 完全畅通（首次调用即 200）。**这不是"key 失效"，而是"这个 App 在这些模型上的配额几乎为 0"**——错误体里会明确写 `App:**1403在模型:xxx每分钟请求次数超过限制`，一看就知道是限流不是鉴权失败，不要误判成 key 有问题。
3. **鉴权/欠费/限流有明确分层，不能一概按"重试"处理**：`internal_mesh_client.py` 的分层策略是本项目应直接照抄的最佳实践——
   - `401/403` → 鉴权失败（token 无效/无权限）→ **立即抛致命错误，不重试**；
   - `402` 或响应体命中"欠费/配额"关键词 → **立即抛致命错误，不重试**（避免烧空额度还傻等）；
   - `429` 或 `5xx` → 限流/瞬时故障 → **指数退避重试**（`min(2**attempt, cap)`）；
   - 其余 `4xx` → 不重试，直接上抛给调用方判断。
   本项目 `taskvm/benchmark/model_client.py` 目前的实现若还没有这个分层（尤其是"401/403/402 立即致命，不要浪费重试次数"），应参照 `internal_mesh_client.FatalAPIError` 的做法补上。

## B.2 模型命名规律（2026-08-05 probe 结果，供 `TASKVM_DEFAULT_MODEL` 选型参考）

Probe 方法：用文档给定 key 逐个模型发一次最小 `chat.completions` 请求，记录 HTTP 状态码与错误体；对 429 的模型额外做了 8 秒间隔重试排除"瞬时抖动"的可能。

| 模型名 | 结果 | 说明 |
|---|---|---|
| `gpt-5.6-sol` | ✅ 200，畅通 | **W1 建议默认模型**（比原计划的 `gpt-5.5` 更可靠，且已经是大纲原定的目标模型之一） |
| `gemini-3-flash-preview` / `gemini-3.6-flash` | ✅ 200，畅通 | 网关内部路由为 `google/gemini-...` 前缀，价格通常远低于旗舰模型，适合"UI-gen 之外"的批量/调试调用 |
| `glm-5.2` / `glm-5v-turbo` | ✅ 200，畅通；`glm-5v-turbo` 已验证支持 vision（`image_url` + `data:image/...;base64,...` 标准 OpenAI 格式可用） | `glm-5v-turbo` 可作为 compiler 读取 screenshot 这一步的**备选/兜底视觉模型** |
| `aws.claude-sonnet-4.6` / `aws.claude-opus-4.8` | ✅ 200，畅通 | **关键命名规律**：网关上 AWS Bedrock 托管的 Anthropic 模型必须带 `aws.` 前缀，不带前缀会报 `400 不支持的模型类型`（如 `claude-opus-4.8`、`claude-opus-4.7`、`claude-opus-4.6` 均 400，加上 `aws.` 前缀后 `aws.claude-opus-4.8` 立即 200） |
| `deepseek-v4-pro` / `deepseek-v4-flash` / `kimi-k3` / `MiniMax-M3` / `LongCat-2.0` | ✅ 200，畅通 | 全部一次性成功，可作平价备选 |
| `gpt-5.5` | ⚠️ 429（限流，非失效） | 大纲/W1 方案原定的默认模型；当前 key 在此模型上配额很紧，高并发场景（如 N≥3 samples 并发跑）大概率会被限流卡住，建议改用 `gpt-5.6-sol` 或加大重试间隔 |
| `claude-sonnet-5` / `claude-opus-5` | ⚠️ 429（限流，非失效） | 大纲 §7 提到的目标模型之一；命名本身合法（不是 400，说明模型存在），只是配额几乎耗尽/未开通，**不要**尝试加 `aws.` 前缀（探测过 `aws.claude-sonnet-5`/`aws.claude-opus-5` 均返回 400，说明这两个模型不走 AWS 通道，命名不需要改） |
| `gpt-5.6-terra` / `gpt-5.6-luna` / `kimi-k2.7-code` | ⚠️ 429（限流，非失效） | 同上，配额紧张，非命名或鉴权问题 |
| `claude-opus-4.8` / `4.7` / `4.6`（不带 `aws.` 前缀） | ❌ 400 不支持的模型类型 | 网关不认这个裸模型名；必须用 `aws.claude-opus-4.x` 形式 |

**结论对 W1 落地的直接影响**：`taskvm/benchmark/model_client.py` 里 `TASKVM_DEFAULT_MODEL` 的默认值建议从 `gpt-5.5` 改为 **`gpt-5.6-sol`**（本身就在大纲候选名单里，且当前 key 下畅通、无限流风险，不需要额外申请配额）；若后续要接入 Anthropic 系模型做对比实验，记得非 `aws.` 前缀的 `claude-sonnet-5`/`claude-opus-5` 目前受限流卡着，短期内更稳妥的替代是 `aws.claude-sonnet-4.6` / `aws.claude-opus-4.8`。

## B.3 配置管理方式的可借鉴模式

`wm/lds/config.yaml` + `config_loader.py` 展示了一个比"硬编码在 `.py` 里"更工程化的模式，值得 TaskVM 的 `benchmark/model_client.py` 参考：
- `base_url` / `api_key` / `model_name` 集中放在 yaml 的 `vlm.api` 节，而不是散落在各脚本里；`api_key` 留空则**自动回退读环境变量**（`INTERNAL_API_KEY`），本地开发和 CI 环境切换不需要改代码。
- `provider: local | api` 的双模式开关（本地 vllm/sglang 起的 OpenAI 兼容端点 vs. 远程公司网关），方便 W1 阶段先用小模型本地联调 prompt/schema，再切到远程前沿模型做正式 kill test，不需要改调用代码只需要切 yaml 一行。
- 这与本项目 §7 已经确定的"`OPENAI_API_KEY` 环境变量 + 可覆盖"的做法是同一思路，无需改架构，只是提醒 coding agent **把 model 名的默认值和限流应对策略也纳入可配置项**，不要硬编码成单一模型不可切换。

## B.4 与本项目既有决策的关系（不改变任何已锁定决策，仅补充工程实现细节）

本节纯属"如何正确调用公司内部 API"的工程细节补充，**不涉及、不重新打开**大纲正文任何一个已拍板的产品/研究决策（不改变"前沿通用模型 + A2UI v0.8 spec 注入"的主线、不改变"两个模型角色独立"的设计、不改变 W1 kill test 的范围）。唯一的具体行动项：`taskvm/benchmark/model_client.py` 的默认模型名与限流/致命错误分层策略应按 B.1、B.2 调整，避免 W1 跑 kill test 时把"限流"误诊断为"模型不可用"或"compiler 出错"，污染 sub-kill 的判断依据。
