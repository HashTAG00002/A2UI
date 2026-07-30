# A2UI 开工大纲（单一权威文档·心智模型对齐版）

> 本文件是我（Claude）通读全部材料后形成的**唯一开工基础**，合并并取代了此前的 `A2UI_介绍与开工大纲.md` 与 `A2UI_开工基础plan.md`（二者已删除，重要内容并入此处）。
> 输入材料：5 份外部规划 txt（现位于 `docs/oracle/`：HCI-UI 总纲 / GenerativeUI 调研 / 与 DuetUI 差异 / 与 SaC+Sidekick 差异 / CHI 工作计划）+ 4 篇论文 tex 原文（`docs/references/`：DuetUI / SaC / Sidekick / Macaron-A2UI）。
> 所有竞品逐字引用均经独立 verifier 对 tex 原文核对（标【核对】）。Sidekick/Macaron 边界、6 周排期、三方撞车独立判断、15 个开放问题的展开解释均为本次新增。
> 今天 2026-07-30；CHI 2027 full paper deadline ≈ 2026-09-10 AoE（用户确认无延期、只剩 6 周）。标【待对齐】=需你拍板、会影响我搭什么。

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

A2UI 不展示轨迹，而是实时生成一个**活的任务状态**：
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

### Macaron-A2UI【你们自家 COLM 2026 论文】
- `2-relatedworks.tex` L5（逐字）：*"In contrast to these lines of work, we focus on assistant-side Generative UI rather than action execution over an existing interface."*
- `3-problem.tex` L5：声明式协议（A2UI v0.8）+ 可信组件 catalog 渲染，**不**生成 HTML/JS/framework 代码。
- 这是 **CHI 工作天然占据的缝隙**：Macaron 明确把"对既有界面的动作执行"排除在外，且是同团队前作。CHI 工作 = Macaron 主动让出的那一半。
- 可复用资产：A2UI-Bench（300 任务 / atomic·depth·width + no_ui_chat 负类）、L1 自动 / L2·L3 LLM-judge(gpt-5.1) / V1-V3 VLM-judge 评测架构、Flutter Web renderer + 23 组件 catalog + render-check gate、LoRA SFT→GRPO 配方（reward 0.2/0.4/0.4）、minimal-prompt 评测 regime、训练好的 Grande(235B,74.2)/Venti(GLM-5.1,75.6) 模型。论文承诺开源模型+bench+协议。

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
Sidekick 实质上是纯反馈/通信层：不执行、不写回、不绑 live state、不跨应用、无独立 verifier。它和我们在每个承重轴上都正交。**但** 它是 7 月 20 号刚发的，审稿人脑子里最新鲜；而且它的表面词汇（CUA + 侧边面板 + 多模态反馈 + 人间歇查看）和一个**偷懒的 A2UI demo** 最像。如果我们的 demo 一开场是"Calendar Agent 80% / Jira Agent 60% / Docs 完成 / 发现冲突 / [暂停][查看日志][继续]"——那就是一个跨应用版 Sidekick，死路。所以 Sidekick 的威胁不在思想重叠，而在 **demo 纪律**：绝不能用"状态仪表盘"开场。

**一句话总结我的判断**：三篇不是一堵墙，而是各缺一个不同的锚点——DuetUI 缺 live state/executable binding（它是 UI-from-intent），SaC 缺 existing applications/round-trip（app-IS-state，不写回），Sidekick 缺 executable binding/live state/round-trip/cross-app（feedback-only）。**只有我们四个锚点同时在。** 防御策略因此各不相同：SaC=让出大概念、楔入 source-of-truth；DuetUI=锐化 top-down-vs-bottom-up + 模拟-vs-真实；Sidekick=demo 纪律（用"操纵+写回+verifier"开场，绝不用状态仪表盘开场）。

### 4.2 顶层设计哲学：一个判别问题让人一眼分清四者

核心判别问题（一句话，适用于所有四者）：**这个界面是"什么的投影"？操纵它会"改变什么、是否被验证"？**

| 工作 | 界面是什么的投影 | 操纵它改变什么 | 有无独立验证 |
|---|---|---|---|
| **DuetUI** | Agent 的任务分解 | 重塑人的意图（top-down 共生成） | 无（GPT-4 同源 judge） |
| **SaC** | 界面**就是** state（自洽生成 app） | 演化这个 app 本身 | 无（existence proof） |
| **Sidekick** | Agent 的动作 | 什么都不改（只观察/暂停） | 仅动作级错误检测 |
| **A2UI（我们）** | **多个真实应用的实时状态** | **写回那些真实应用** | **独立 verifier 读 ground-truth** |

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

## 5. 一等贡献：安全压缩前沿（Safe Compression Frontier）【待对齐·地位】

- **人本矛盾**：界面必须压缩才有价值 ∩ 必须保留会改变结果的区别 ∩ Agent 不应替用户决定什么区别重要。简化=删除区别=价值判断。模型更强不能消除。
- **研究问题**：给定任务 + live state，系统能否自动找出**最小但足够**的人可见可操作自由度集，使得 (a) coverage（每个需要的状态改变可表达）(b) round-trip fidelity / non-interference（每个会改变结果的区别被显形）(c) compression（无关的东西不塞进来）。
- **操作化**：三轴联合测量 `coverage × round-trip-fidelity × interaction-compression`，核心技术目标 = **最大化 compression 同时维持 projection fidelity** 的 Pareto 前沿。
- **"可玩"由此涌现**（不是硬造）：近单调执行任务 → 进度轨；有真实 trade-off → 少量控制旋钮/分支 gallery；不可压缩探索/认知 → 透镜（聚合、定位冲突、导航，不替人操纵）。系统按任务选压缩到哪一层。
- **为什么不是 GenUI/DuetUI**：不学"生成什么 widget"（Macaron 已商品化），不学"用 UI 共塑模糊意图"（DuetUI 已占）；学的是**哪些维度可被安全编译掉、哪些必须作为人可见可操作自由度保留**——fidelity-governed projection，不是 UI 生成。
- **【待对齐】**：HCI-UI 大纲只把 SCF "seed" 成研究问题（line 7758）；`开工基础plan` 提为一等贡献；`CHI 工作计划` doc **完全没提** compression。三份不一致。我倾向按锁定版当一等贡献（它直接驱动架构里的 `projection_policy` 模块 + 一个 Pareto 实验），但这决定了我搭的框架是否含一个"按任务决定 DOF/控件"的策略模块——需你拍板。

---

## 6. 四项锁定决策（+ 冲突标注）

1. **Benchmark = 混合**：3 白盒自建（Calendar/TaskBoard/Drive，sqlite 后端）+ 1 held-out 黑盒（对模型黑盒/对 verifier 白盒 via state adapter）。benchmark 持有隐藏 canonical task graph + DB 映射作 verifier GT；模型推理时只见 screenshot/DOM/a11y/tool schema/trajectory，**永不接触 DB**。
   - 【冲突/待对齐】`CHI 工作计划` doc 只说"一个 app 皮肤/schema/任务组合做 OOD"+"held-out schema"，比锁定版弱且含糊。需澄清：held-out 单元到底是"第 4 个模型未见过的真实 app"，还是"3 个已见 app 的改名/换肤/schema 变体"，还是两者都要？这俩 OOD 强度差很多。
2. **UI/可玩任务面 = 一等贡献**（SCF，见 §5）。
3. **训练 = 诚实 Go/No-Go**：OOD 先设计得足够难以真实区分规则/prompt/学习；有 gap 才训轻量 QLoRA critic，无 gap 则 train-free。**绝不为了 tech-heavy 硬训。verifier 永远来自环境状态，绝不让生成 binding 的模型自评。**
4. **用户研究 = 4 条件**：C0 原始多 app GUI / C1 静态只读聚合 dashboard / C2 chat agent + 全 app 工具访问（Claude/GPT + 3 app MCP 工具，真正的 non-inferiority 对手）/ C3 我们的投影。
   - 【冲突】`CHI 工作计划` doc 是 **3 条件**（chat-only / 可读轨迹 / Task-Workspace），缺 raw-multi-app baseline。→ **以锁定 4 条件为准**，但需你确认。

---

## 7. AI 侧：训什么 / 不训什么 + Macaron 复用

- **不训** UI 生成器（Macaron 已做到 30B/235B/754B + A2UI-Bench，且是自家工作）。
- **不训** GUI executor（UI-TARS/GUI-Owl/UI-Venus/Claude Computer Use 已在做，复用）。
- **唯一值得学**：`Cross-App Binding Critic / Task World Compiler`——一个任务变量在异质应用、不同字段名、不同 GUI 表达中分别对应哪些真实状态与操作；改它后哪些关联状态必须同步（effect propagation）。规则写不死：同名异义、异名同义、同字段不同任务语义、跨应用 effect 传播。
  - 标签来源：cloned sandbox 候选 binding → 读真实 before/after state diff → 与隐藏 expected diff 比 → 完全匹配正例、漏改/错改/多改 hard negative。**非 LLM 自评，非研究者逐条标。**
  - 反捷径：同工具不同参数→相反判断；不同工具同 effect→相同判断；重命名/换肤后仍有效；`tool-name-only baseline` 必须在重命名/反标签上显著下降。
  - 形态：3B–7B 多模态/语言 critic，QLoRA/轻量 SFT。
- **决策点（W4）**：seen app prompt≈92% 但 renamed≈71%、unseen≈54%、错误集中在同名异义/隐含依赖时才训。否则 train-free。
- **【新增·待对齐】Macaron 关系**：我倾向 **differentiate-while-reusing**——CHI 工作明确占据 Macaron 让出的"对既有界面的动作执行"缝隙（不撞），同时复用 A2UI-Bench/judge prompts/Flutter renderer 作基础设施（降 benchmark 与模型准备的险）。但这会把 CHI 工作与 Macaron 耦合，且需决定 pin A2UI v0.8。需你确认是 build-on / differentiate / independent。

---

## 8. 系统架构骨架（我要搭的代码框架）

```
a2ui/
├── apps/                  # 自建可重置 Web 应用（sqlite 后端）
│   ├── calendar/  taskboard/  drive/
│   └── _heldout/          # held-out 黑盒 app（OOD；对模型黑盒，对 verifier 白盒 via state adapter）
├── harness/               # browser_controller(Playwright) / state_adapter(reset·seed·read-canonical) / trace_capture / replay_engine / shadow_txn(copy-on-write 影子执行)
├── task_state/            # representation / compiler(Apps→TaskWorld) / entity_binding / dependency_graph / projection_policy(★SCF:决定哪些维度留作人 DOF)
├── execution/             # patch_compiler(编辑→semantic patch) / replanner / action_dispatcher(GUI/MCP/API hybrid)
├── verifier/              # app_state_checks / cross_app_checks / non_interference / round_trip_checks / reconciliation
├── workspace_ui/          # renderer / editable_components / live_sync（先结构化文本/表单，不追求花哨）
├── benchmark/             # task_templates(30-50) / initial_states(隐藏 canonical graph) / user_edits / ood_splits / live_runs
├── baselines/             # 规则/类型匹配·prompt-only·frontier+shadow·人工 binding 上界·规则+critic
├── user_study/            # 4 条件
└── evaluation/  docker-compose.yml  README.md
```
**开工第一周我只动**：`apps/{calendar,taskboard}`(极简可重置) + `harness/{state_adapter,replay_engine,trace_capture}` + `task_state/{representation,compiler(frontier API)}` + `verifier/round_trip_checks` + `workspace_ui/renderer`(结构化文本/表单)。**先 replay-mode**，跑通 compiler→UI→patch→执行→verifier。

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
- ❌ 动态生成任意 UI、研究"该用列表还是看板"（与 DuetUI/SaC/A2UI 正面重合）。
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

## 15. 待对齐的开放问题（15 个，展开解释版）【待你拍板】

> 这 15 个是**会改变我搭什么**的分歧点。下面每个都展开成"这是什么问题 / 为什么重要 / 我的推荐默认"。你逐条"同意"或纠正即可。A 组（1-3）与 B 组（4-6）决定框架形态，最优先。

### A. 时间与范围（最优先）
1. **deadline 真实性 + 6 周现实**。你已确认无延期、只剩 6 周。剩下要定的只是：6 周压缩版（见 §11）是否可行？还是你有更准的日期/想转 UIST 2027？
   - 为什么重要：决定我按 6 周还是更短/更长排代码。
   - **推荐：跑 §11 的 6 周压缩版，用户研究后置非阻塞。**
2. **命名 canonical**。三份材料用了三个名字：①Task-Native Interaction / Task Capsule（HCI-UI 第 8 轮终版）②TaskVM / Playable Task Surface（与 SaC 差异 doc）③A2UI（仓库名 + plan，且 plan 明说"TaskVM/Task Capsule 暂不启用"）。
   - 为什么重要：论文里用哪个名字、teaser 怎么写、related work 怎么自我称呼，全看这个。且"virtualization"叙事（TaskVM）容易和 SaC 的"生成 app"撞，"projection"叙事更安全。
   - **推荐：论文用 Task-Native Interaction / Task Capsule（HCI-UI 终版，且不撞 SaC）；仓库/代码继续 a2ui。**
3. **AOHP 深读**。AOHP（agent-native OS，2026-06 tech report）被标"比 SaC 更接近、可能吸收 bottom-up 框架"，但 repo 里没有它的 tex，我还没深读。
   - 为什么重要：如果 AOHP 已经做了"从 live GUI 提任务状态 + 跨 app 执行 + app 可替换性"中的任何一块，我们的 related work 就有硬伤。它是唯一一个我没核实过的"近邻"。
   - **推荐：开工前我并行找 AOHP 论文做正式 diff，再写 related work。**

### B. 贡献栈（决定我搭什么）
4. **Safe Compression Frontier 的地位**。它到底是真"一等贡献"（驱动架构里的 `projection_policy` 模块 + 一个 Pareto 实验），还是只当评测 lens？三份材料不一致：HCI-UI 只 seed、`开工基础plan` 提为一等、`CHI 工作计划` 完全没提。
   - 为什么重要：若一等，我必须搭一个"按任务决定哪些维度留作人 DOF、哪些编译成控件"的策略模块，并跑 coverage×round-trip×compression 的 Pareto 实验；若只 lens，这个模块可以不做，省一周。
   - **推荐：一等贡献，按锁定版。它正好把你的"灵动/可玩"诉求和 tech-heavy 核心统一，且 DuetUI 自己都承认"固定 GT 不是好 proxy"——这是我们的切口。**
5. **签名实验"application-substitution invariance"（JVM moment）**。同一 task edit 跨 Stack A（Google Calendar+Jira+Docs+Gmail）/ B（Outlook+Linear+Notion）/ C（桌面+移动+Web），验任务面稳定+操作相同+语义一致+轨迹不同+无关不动。它在 SaC 差异 doc 里被锁为签名实验，但 `开工基础plan` 只有 3 app+1 held-out、没这个。
   - 为什么重要：这是**唯一**能证明"任务交互与应用实现解耦"的实验，也是 §4.3 demo 的第二招。但全量 3 stack × 4 app 在 6 周内太重。
   - **推荐：CHI 2027 内做"小版"（2 stack × 2-3 app）当一个 figure；全量 Stack A/B/C 放未来工作。**
6. **Macaron 关系**。CHI 工作与你们自家的 Macaron-A2UI（COLM 2026）是 build-on（复用 A2UI-Bench/judge/Flutter renderer）/ differentiate / independent？
   - 为什么重要：复用资产能省 benchmark + 模型准备的险，但会把 CHI 工作耦合到 Macaron 的 A2UI v0.8 协议；独立则要自建评测。
   - **推荐：differentiate-while-reusing——明确占据 Macaron 让出的"对既有界面动作执行"缝隙，同时复用 A2UI-Bench/judge/renderer 作基础设施，pin A2UI v0.8。**

### C. Benchmark 与 OOD
7. **held-out OOD 到底是什么**。锁定版说"1 个对模型黑盒、对 verifier 白盒的真实未见 app"；`CHI 工作计划` doc 说"held-out schema/皮肤/任务组合"。这俩 OOD 强度差很多：真未见 app 验迁移，已见 app 改名换肤验反捷径。
   - 为什么重要：决定 benchmark 里 held-out 那一块怎么造、verifier 的 state adapter 怎么写、以及"OOD Generalization"指标报什么。
   - **推荐：两者都要，分别报——1 个真未见 app（验迁移）+ 已见 app 的 rename/reskin/schema 变体（验反捷径）。**
8. **Mail app in/out**。材料里 Mail 一直是"可选第 4 个"。
   - 为什么重要：4 个 app 比 3 个 app 的跨应用复杂度（effect 传播、冲突）高一个量级，6 周内可能拖垮。
   - **推荐：out。Drive 作第 3 个，Mail 永久 optional。**
9. **benchmark 规模终值**。30-50 模板 / 500-1500 实例是个范围。
   - 为什么重要：W3 要 overnight 跑完，规模定死才知道 API 成本和时间。
   - **推荐：40 模板 / 800 实例 / OOD 占 ~20%。**

### D. 用户研究
10. **4 条件确认 + 非劣性 margin + N + IRB 时间**。C0 raw-multi-app / C1 static-read-only / C2 chat-agent+tools / C3 ours 是否最终确认？非劣性 margin（C3 成功率不能比 C2 低多少）取多少？N 多少？6 周内 IRB 来得及吗？
    - 为什么重要：4 条件是锁定决策，但 `CHI 工作计划` doc 是 3 条件，需你一锤定音；margin 必须实验前定死（否则事后改 margin 是学术不端）；IRB 若不及则用户研究只能 pilot + 后置。
    - **推荐：4 条件；非劣性 margin = C3 成功率不低于 C2 的 5 个百分点；N=18 within-subject；IRB 立即提交，若不及则 W5 pilot + 后置正式（自动评测仍为论文主体，不阻塞投稿）。**

### E. 机制设计（可默认，有异议再改）
11. **reconciliation 机制**。底层 app 被外部改了之后，surface 怎么发现、怎么处理冲突？poll（轮询）/ subscribe（订阅事件）/ re-read-on-action（用户操作时重读）？冲突时是静默覆盖还是标红给选项？
    - 为什么重要：这是 §4.3 demo 第 4 步、也是 SaC 逐字交出的"frontend state synchronisation"未来工作——我们的卖点之一。机制没定，verifier 的 reconciliation 检查就没法写。
    - **推荐：re-read-on-action（用户编辑/定时心跳触发重读）+ 冲突时标红不静默覆盖，给出"底层已变 / 你的编辑 / 合并选项"。**
12. **跨设备**。drop / appendix showcase？
    - 为什么重要：上限第 5 条说"跨设备≠跨权限"，硬做会撞权限/事务的墙。
    - **推荐：appendix showcase，不当贡献。**
13. **wind-tunnel（CUA 模拟用户）基础设施**。本期建 / 推迟？
    - 为什么重要：它能自动生成用户编辑轨迹压测 reconciliation，但 HCI-UI 明确"模拟用户是风洞不是乘客"——不能当 novelty，且 6 周内建它风险高。
    - **推荐：推迟，本期不建。**

### F. 心智模型校验（确认我理解对）
14. **"滑块愿景"是否接受重定位**。你早期激动的"拖一下进度条"已被 Round 6/7 结构性批判（非单调多目标任务不可行）。你是否接受重定位的诚实上限（自动找最低安全压缩维度 → 进度条/驾驶舱/透镜三层）？
    - 为什么重要：若你仍想主推"一个滑块搞定一切"，那 SCF 的故事要重写，且会撞上限第 2 条的批判。
    - **推荐：接受重定位。这反而让 SCF 更可发表——"每个任务获得它能诚实承受的最简单界面"比"一个滑块"深刻得多，也防住"过度压缩"的审稿质疑。**
15. **人的角色口径**。是"低频授权节点"（与 SaC 差异 doc 的 Turn 5 框架）还是"任务变量编辑者、Agent 传播其编辑"（锁定主线）？两者不矛盾但口径不同，影响贡献 claim 措辞。
    - 为什么重要：若强调"授权节点"，容易被读成 Sidekick 式的监督/反馈；若强调"任务变量编辑者"，更突出"人操纵任务、Agent 操纵应用"的主线。
    - **推荐：统一为"低频任务变量编辑者"——人编辑任务变量（低频），Agent 把编辑可靠传播并验证；授权/暂停只是其中一种低频介入。**

---

## 16. 我接下来的动作

1. 等你对 §15 的问题拍板（尤其 A 组 1-3 与 B 组 4-6，决定框架形态）。
2. 拍板后更新本文件为锁定版（去掉【待对齐】），据 §8 骨架进 **plan 模式**，过一遍 W1 的具体代码方案（仓库骨架、Calendar/TaskBoard 极简可重置应用接口、canonical task graph 的隐藏与读取、replay 引擎、compiler/binding 的 frontier-API 调用契约、verifier 的 round-trip 判定逻辑），你确认后再动手写代码。
3. 开工即守：这是**独立的 a2ui 项目**，不拖入其他仓库；自动评测为主体、用户研究后置不阻塞；训练是 Go/No-Go 而非默认；demo 开场用"操纵+写回+verifier"弧（§4.3），绝不用状态仪表盘开场。

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

> 与正文的对应：方向 1 = §0 主线 = 四锚点 existing applications / live state / executable binding / round-trip verification。"TaskVM"是本附录里的叫法，与正文 §15-Q2 的命名待对齐（候选：Task-Native Interaction / Task Capsule / TaskVM）。**注意**：方向 1 的 TL;DR 与正文 §0 完全一致（真实状态→编译→界面→写回→更新），只是措辞用了"虚拟机中间层"比喻。

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
