# TaskVM：把异质既有应用编译成可治理的可执行任务虚拟机

> **CHI 2027 full paper 开工对齐文档（一次成型版）**。项目代号 **TaskVM**（不再用 A2UI 作代号——`A2UI` 是 Google 2025 年的通用 agentic-UI 声明式协议名 `a2ui.org` v0.8/v0.9.1，Macaron-A2UI 是团队自家的该协议训练侧实现，均非本项目私有名）。实现中可选择性复用 A2UI v0.8 作 UI 渲染传输格式，与更名不冲突。
>
> 本文件是通读全部输入材料（`docs/oracle/` 5 份规划 txt + `docs/references/` 9 篇竞品/自家论文 tex）并完成全部对齐后形成的**唯一开工基础**。所有 novelty 引用均经独立逐字核对（标【核对】+ 文件:行号）。今天 2026-08-06；CHI 2027 full paper deadline ≈ 2026-09-10 AoE，无延期、约 5 周。

---

## 0. 一句话主线 + 锚点

**一句话主线：人治理任务（governance），Agent 自治应用（autonomy）。**

TaskVM 把多个正在运行的**既有应用**的实时状态，反向编译成一个**可双向操纵、可回退、可验证**的任务虚拟机界面；用户在界面上同时操纵"只读区"（多 app 实时核心状态的忠实投影）与"可读可写区"（进度推进、回退、checkpoint），就像在操纵**一个**统一的低认知负载的应用——但每一次操作都由 GUI Agent 落实到多个真实应用，并由独立 verifier 读 ground-truth 判定"改的发生、没改的不动、界面随后重新同步、可回退"。

### 锚点（VM-like 框架，四者同时存在才与所有竞品拉开差距）

> 旧版四锚点（existing applications / live state / executable binding / round-trip verification）只抓住了 VM 冰山露出海面的一角——"跨 app"。VM-like 真正意味着下面五点，缺一不可，且**没有任何现有工作同时做到**：

1. **bottom-up live projection（自底向上的实时投影）**：界面是**多个真实 app 实时状态**自底向上编译出来的忠实投影（GUI Agent 作编码器：读 app 状态→编码成 VM state；GenUI 模型作解码器：VM state→人可操纵界面）。**不是**自顶向下从 task/prompt 生成界面。投影会**随世界状态变化动态重投影**，不是用户重新触发。
2. **bidirectional executable binding（双向可执行绑定）**：用户操纵 VM 变量 ↔ 真实 app 对象双向忠实绑定，可读可写。一个 VM 变量可绑**多个**异质 app 对象 + 写回算子。JavaVM 类比：程序操作 JVM 对象，JVM 翻译成硬件内存读写；用户操作 VM 变量，TaskVM 翻译成真实 app 读写。
3. **substrate-independence（substrate 无关性，"JVM moment"）**：同一 VM 操作跨不同 app 栈（Stack A=Google Calendar+Jira+Docs+Gmail / Stack B=Outlook+Linear+Notion+Outlook Mail），界面稳定、操作相同、最终任务语义一致、底层轨迹完全不同。**这才是"VM"的核心**——它是一个抽象机，不绑定具体 app 实现。
4. **governance over autonomy（治理优先于自治）**：**核心词是 governance（人侧），不是 autonomous（agent 侧）**。人在任务起始时自顶向下设定 milestone/checkpoint（"这次要做到哪里"），自底向上系统动态收集实时状态；人**随时**可在界面里推进/回退/撤销**真实 app 变更**。Agent 在 governance 大前提下尽可能自治执行；安全性/边界**全部下放给自动 verifier**，**绝不需要**"人审核 Agent 结果后才能继续"那种 Sidekick 式人工中制介入（用户特别讨厌这种范式，"太老了"）。这与 AgentLens（侧重 autonomous 下何时介入监督）、ALLOY（人类监督工作流）**方向相反**——他们是"监督 agent"，我们是"治理世界状态"。
5. **round-trip verification + reversibility（验证与可逆）**：独立 verifier 读隐藏 canonical ground-truth，判 changed-happened + non-interference + reconciliation + **回退后真实 app 真的复原**。负对照（打坏 dispatcher）必须 ≤0.3。verifier **永不**来自生成 binding 的模型自评。这是 VM 的"内存安全/事务"层。

冻结 RQ：*Can an agent compile live, fragmented application state into a task virtual machine that users can govern — manipulating a bidirectional projection to drive, roll back, and verify cross-application effects across heterogeneous existing software?*

最强单句主张：*We turn application-centric interaction into task-native governance: the user governs a task virtual machine projected from live cross-application state, while the agent realizes and verifies that state in heterogeneous software — and the projection stays faithful, reversible, and substrate-independent.*

---

## 1. 核心构念

### 1.1 VM 类比（逐项对应，这是"VM"到底意味着什么）

| JavaVM | TaskVM |
|---|---|
| 异构硬件（x86/ARM/RISC-V） | 异构真实 app（Calendar/TaskBoard/Drive/Mail/...） |
| JVM 抽象机：程序操作 JVM-level 对象，不碰裸内存 | TaskVM 抽象机：用户操作 task-level 变量，不碰各 app 原生 UI |
| 程序写 JVM 对象 → JVM 翻译 → 硬件内存写 | 用户改 VM 变量 → TaskVM 编译 → GUI Agent 写回真实 app |
| JVM 对象读 ← 硬件内存读（faithful） | VM 变量读 ← 真实 app 状态投影（faithful, verified） |
| substrate-independence：同一 bytecode 跨硬件语义一致 | JVM moment：同一 VM 操作跨 Stack A/B，界面稳定、语义一致、轨迹不同 |
| 字节码 verifier + GC + 内存安全 | 独立 GT verifier + 非干涉硬门 + 负对照 + reconciliation |
| 事务/回滚 | **进度回退/撤销真实 app 变更**（compensation/saga）——VM 的事务性 |

### 1.2 编译器架构（encoder/decoder 核心）

- **GUI Agent = 执行器 + 编码器**：它**读**真实 app 状态（经 GUI 观测：screenshot/DOM/a11y）编码成中间 VM-state；它也**写**（把用户的 VM 变量 patch 执行回真实 app）。**两个模型角色独立**：Agentic-UI 生成模型（解码器）与 compute-use 执行模型（编码器+写回），即使同用一家厂商也是两次独立调用、不共享 context。
- **GenUI 模型 = 解码器**：把中间 VM-state 解码成**人可操纵的界面**（只读区 + 可读可写区）。
- **必须双向**：人在 GenUI 操纵 → 解码器逆 → VM-state patch → GUI Agent 写回 → 真实 app 变 → 编码器重读 → GenUI 重解码重投影。用户体验：像在操纵**一个**统一的低认知负载 app，而非 N 个异构 app。

### 1.3 界面架构（两区分离，这是 governance 的落点）

- **只读区**：多个 app 的实时核心状态的投影/绑定（"世界现在什么样"的 faithful 视图，自底向上重投影）。
- **可读可写区**：人控制进度、回退、撤销、设 checkpoint 的地方（"我要世界变成什么样"的控制台，映射回真实 app）。
- **自顶向下 + 自底向上**：人初始下放任务 + 若干 checkpoint（**不是** ALLOY 那种"事前 PBD 把整个工作流定死"）；系统同时动态收集实时 app 状态（**不是** Morae 那种"只在偏好歧义时被动暂停"）。
- **进度可回退/撤销**：VM 的事务性，撤销的是**真实 app 的变更**（compensation），不是内部 model 的 cheap rollback。

### 1.4 主闭环与八步往返

**主闭环**：`User (governance) ↔ GenUI (decoder) ↔ Shared VM-state ↔ GUI Agent (encoder/executor) ↔ 真实 apps`。Shared VM-state 保存目标/计划/已执行动作/真实结果/当前 app 状态/可回退事务日志——**不是聊天历史**。人位于 governance 侧（设 checkpoint + 随时回退），**不**在 autonomous 侧做"审核后继续"。

**八步往返**：Observe → Abstract → Project → Manipulate → Compile → Execute → Verify → Reconcile（+ Rollback 作为 governance 的可逆层）。

**一等对象是 VM-state（task state），不是 trajectory**——trajectory 是 agent 内部概念，不暴露给用户。

---

## 2. 一条任务讲清这台机器（concrete example，详尽版）

用户对 TaskVM 说：*帮我准备下周五的项目发布：安排发布会议、整理材料、创建剩余任务、起草团队通知。本次做到"材料齐 + 发布会议定 + 通知草稿"这个 checkpoint 为止。*

**自顶向下**：用户设定 checkpoint（做到哪里），不下放具体操作步骤。
**自底向上**：TaskVM 的 GUI Agent 读真实状态——Calendar（会议）、TaskBoard（任务/负责人/截止）、Drive（材料）、Mail（通知草稿），编码成 VM-state。

TaskVM 不展示几十步轨迹，而是解码成一个**活的任务虚拟机界面**：

```
═══════════════════════════════════════════════════════════
  📋 项目发布 · VM-state（实时投影 · 自底向上）     [checkpoint: 材料齐+会议定+草稿]
═══════════════════════════════════════════════════════════
  ▣ 只读区（多 app 实时核心状态 · faithful projection）
    发布日期          8 月 14 日          ← calendar.E1.date + taskboard.{T1,T2}.deadline
    发布会议          8/14 14:00–15:00    ← calendar.E1
    负责人            Alex                ← taskboard.{T1,T2}.assignee
    材料状态          3 / 4 已完成        ← drive.docs.*.status
    通知草稿          已起草，未发送      ← mail.D1.status
    ⚠ 底层已变 (09:31): taskboard.T2.deadline 现 8/20（你投影的是 8/14）  ← reconciliation
  ─────────────────────────────────────────────────────────
  ✎ 可读可写区（governance · 双向绑定 · 可回退）
    [推进到 checkpoint]  [回退上一步↩]  [撤销本次变更↶]  [设新 checkpoint]
    发布日期   [2026-08-14] → [2026-08-18  ✎]
═══════════════════════════════════════════════════════════
```

**场景 1 — 双向写回 + 非干涉 + 重投影**：用户在可读可写区把"发布日期"从 8/14 改成 8/18。TaskVM 编译成 VM patch → GUI Agent 写回：Calendar 会议 E1 移到 8/18、TaskBoard 中**依赖**发布日期的 T1/T2 deadline 顺延到 8/18、Drive 发布计划文档日期更新、Mail 草稿日期更新——但**已完成且不依赖发布日期的 T3（会议纪要）纹丝不动**。独立 verifier 读真实 app 状态：✓ 4 个目标变更全发生、✓ 11 个无关对象零变更（非干涉硬门）、✓ 只读区重新同步到 8/18。负对照（打坏 dispatcher）得 0.3。

**场景 2 — 回退真实变更（VM 事务性，governance 核心）**：用户发现 8/18 与另一个重要会议冲突，点"撤销本次变更"。TaskVM 用 compensation/saga 把刚写的 4 个 app 变更**真实复原**（Calendar E1 回到 8/14、T1/T2 deadline 回 8/14、Docs 日期回滚、Mail 草稿日期回滚），verifier 确认复原忠实。**这是 Jelly 的"version revert"做不到的——Jelly 只回退内部 model，不回退真实 app。**

**场景 3 — reconciliation（随世界状态动态重投影）**：队友在外部把 TaskBoard T2 deadline 改成了 8/20。只读区 T2 字段变琥珀，显示"底层已变: 现 8/20（你投影的是 8/14）"，给出合并选项。**这正是 SaC 逐字交出的"frontend state synchronisation"未来工作（§3 SaC 核对）——他们承认做不到，我们做到了。**

**场景 4 — JVM moment（substrate 无关性）**：同一"发布日期 8/14→8/18"操作，跨 Stack A（Google Calendar+Jira+Docs+Gmail）与 Stack B（Outlook+Linear+Notion+Outlook Mail）：界面外观稳定、用户操作相同、最终任务语义一致、底层轨迹完全不同、用户无需重新学 app。**这是 VM 之所以叫 VM 的证据——没有任何竞品要求也不演示这个。**

这四步合起来，就是"治理一个跨 app 的任务虚拟机"：双向忠实 + 非干涉 + 可回退 + substrate 无关 + 独立验证。**9 篇竞品没有任何一篇能复现其中一步的"真实写回 + 验证 + 可逆"**（§3 逐篇核对）。

---

## 3. Novelty——逐字核对的竞品边界【核对】

> 本节对 `docs/references/` 下 9 篇逐篇做负样本对比，每篇核实"它投影的是什么 / 用户操纵什么 / 是否持久 / 有无回退真实变更 / 有无 checkpoint 事务 / 验证机制 / 是否随世界动态重投影 / 与 TaskVM 的本质区别"。所有引用均逐字核对 tex（标文件:行号）。这是给开工 coding agent 理解"VM-like 到底跟每一篇差在哪"的负样本库。

### 3.1 ALLOY — 投影 procedure，非 state；事前 PBD，非动态【核对】

- **venue/日期**：无 venue 标注的 manuscript preprint（`main.tex:2` `\documentclass[manuscript]{acmart}`），全篇无 `\acmConference`/月份。tex 文件日期约 2025-10。
- **投影什么**：用户**演示**推断出的 **procedure/workflow**（任务级子任务节点 DAG）。`3-System.tex:3` *"transforms user demonstrations into editable and reusable LLM workflows"*；`3-System.tex:61` *"each node represents a semantically meaningful sub-task rather than low-level browser operations"*。
- **用户操纵什么**：**workflow node**（节点/边/prompt）。`3-System.tex:63` (F3) *"add new nodes... delete nodes... reconnect edges... re-record individual nodes"*。
- **持久 vs 一次性**：持久（随演示实时更新 + 可存模板）。`3-System.tex:42`。
- **回退真实 app 变更**：**无**（grep rollback/undo/revert/reconcile/compensation 全 tex 零命中）。
- **checkpoint/事务性**：**无**（仅 `3-System.tex:100` SQLiteSession 持久化执行结果，非事务 checkpoint）。
- **验证机制**：用户研究自评（12 人 NASA-TLX + 7pt Likert + 任务完成率）。无独立 GT verifier。
- **随世界动态重投影**：**无**（演化靠用户继续演示，非外部状态变更）。
- **关键核实**：ALLOY workflow 是**用户演示 author**，不是从 GUI 状态 discover。`3-System.tex:32` *"Users demonstrate a task by performing it directly in the browser... ALLOY automatically generates a structured workflow"*；`3-System.tex:58` (F1) *"ALLOY captures user demonstrations as they naturally perform web tasks"*。
- **与 TaskVM 本质区别**：① **state vs procedure**——ALLOY 投影"先做哪步"（DAG），TaskVM 投影"世界现在什么状态"（VM-state）。这是本体论不对称：procedure DAG 只能问"跑没跑完"，没有 non-interference/reconciliation 概念（imperative DAG 不对没碰的 state 做 claim）；typed VM-state 才能做"flight_booked=true AND 无关状态不动"的可验证 claim。② **ex-ante PBD vs 动态投影**——ALLOY 的 workflow 靠用户演示 author + 用户编辑驱动演化；TaskVM 的投影靠真实世界状态变化驱动重投影。一个程序不会随世界变，一个投影会。③ **无回退真实变更、无 substrate-independence、无独立 verify**。
- **撞车风险（VM 框架下）**：**4/10**。占 VM 性质最多（real-app 执行 + 持久可编辑 + 多 site），但 state-vs-procedure + ex-ante-vs-动态 是真本体论差异。

### 3.2 Morae — 决策点一次性 choice overlay，非持久投影【核对】

- **venue/日期**：UIST '25，September 28–October 1, 2025，Busan。`paper.tex:66-68`。
- **投影什么**：决策点的 **user choice / preference**（结构化选项）。`04_system.tex:59` *"Generative UI for Capturing User Choices"*。
- **用户操纵什么**：**a choice**（偏好选项）。`04_system.tex:64` *"Users can then optionally interact with the generated UI to clarify preferences, and their inputs guide the agent's subsequent actions"*。
- **持久 vs 一次性**：**一次性决策点 overlay**（pause→选→继续）。`00_abstract.tex:6` *"automatically identifies decision points during task execution and pauses so that users can make choices"*。
- **回退真实 app 变更**：**无**（`03_formative.tex:88` D4 "accept or undo" 是 formative 设计方向，System 章未实现）。
- **checkpoint/事务性**：**无**。
- **验证机制**：**self-ask-then-answer（self-verification）+ 人工标注 GT 评估 pause 准确率**。`04_system.tex:8` *"self-ask-then-answer verification strategy"*。**这是模型自评，正是 TaskVM 的最干净 foil。**
- **随世界动态重投影**：**无**。
- **关键核实**：Morae UI 是**决策点一次性 overlay**，非持久投影。`04_system.tex:60` *"the agent proactively pauses the process and prompts users"*。
- **与 TaskVM 本质区别**：Morae 占住了 load-bearing primitive"generated UI from live state → 用户选 → agent 继续"，但① 单 app / 单选择 / 一次性，**无跨 app 扇出、无持久投影、无双向绑定**；② "验证"是 self-ask 模型自评，做不了非干涉；③ 无回退、无 substrate-independence。本质是"模型按偏好依赖决定何时让人介入"的单线 reactive 工作流，与 VM 的持久双向 + governance + 事务性不是一个东西。**核心区别：Morae 让人做 choice（点一下选哪个），TaskVM 让人 govern 世界状态（操纵 + 回退 + 设 checkpoint）。**
- **撞车风险（VM 框架下）**：**3/10**。

### 3.3 AgentLens — GenUI 按设计非交互、decoupled，与"双向"正相反【核对】

- **venue/日期**：UIST '26，November 2–5 2026，Detroit；页眉标 Preprint。`main.tex:51`。**post-2026-01，"持久个人助理范式变革"屏蔽论不适用。**
- **投影什么**：Full/Partial UI 投影 **live app screen**；GenUI 投影 **agent 用 NL spec 生成的 HTML**。`01_Introduction.tex:15`。
- **用户操纵什么**：Full/Partial UI 下用户**通过 overlay 触摸坐标直接操纵 live app**；**GenUI 只看不可交互**。`04_Method.tex:83`；GenUI（`04_Method.tex:93` 注释行）*"our current implementation of GenUI does not support GUI interaction"*。
- **持久 vs 一次性**：**just-in-time overlay（关键决策点弹出）**，非持久。`01_Introduction.tex:26` *"just-in-time manner"*。
- **回退真实 app 变更**：**无**。
- **checkpoint/事务性**：**无**。
- **验证机制**：3 名人工 annotator 判断 LLM 模态选择是否对齐 + 21 人 PSSUQ。明确否定单一 GT：`05_Evaluation.tex:25` *"better understood as a preference-driven decision than as a single ground-truth prediction problem"*。
- **随世界动态重投影**：**部分**（Full/Partial UI 是 live app 镜像，touch 转发）；GenUI 本身 **decoupled、非 live**。
- **关键核实**：AgentLens GenUI **非交互、decoupled from live state**。`04_Method.tex:91` *"We deliberately decoupled the GUI generation process from the main agent because we found that when the GenUI Agent is exposed to the original app screen, it tends to reproduce the existing design"*；`04_Method.tex:93` *"does not support GUI interaction"*。
- **与 TaskVM 本质区别**：AgentLens 最像 TaskVM 的 modality（GenUI）**按设计就不是 live-state 投影、不是控制面**——它与 TaskVM 做了**相反的工程选择**（AgentLens 解耦以避免幻觉；TaskVM 绑定以保证保真）。这是真哲学分叉，不是 framing。① GenUI 非交互 = 与"双向"正相反；② 单 app / 单任务；③ 非持久；④ 无 verify。**核心区别：AgentLens 侧重 autonomous（何时介入监督），TaskVM 侧重 governance（治理世界状态）——方向相反。**
- **撞车风险（VM 框架下）**：**3/10**（S 级里最弱）。

### 3.4 Jelly — 内部 data model 是 state，非外部 app 投影；不碰真实 app【核对】

- **venue/日期**：CHI '25，April 26–May 1 2025，Yokohama。`main.tex:104`。
- **投影什么**：**task-driven data model**（object-relational schema + dependency graph）。`1-introduction.tex:19`、`:24`。
- **用户操纵什么**：**data model 字段 + 自然语言**。`1-introduction.tex:24` *"interactions translated to changes in the underlying model. Users can also directly inspect the model"*。
- **持久 vs 一次性**：**持久**（data model 持续演化 + traceable history）。`6-interface.tex:67`。
- **回退真实 app 变更**：**有 version revert，但仅针对内部 data model/UI 版本，不涉及真实 app 变更**（Jelly 不碰真实第三方 app）。`6-interface.tex:67` *"revert any changes if there are adjustments that do not meet their expectations"*。
- **checkpoint/事务性**：**无**。
- **验证机制**：2 名 human coder 评估生成 data model（schema/dependency 准确率 91.5%/96.9%）。无独立 GT verifier。
- **随世界动态重投影**：**无（明确 future work）**。`5-pipeline.tex:72` *"the current pipeline primarily relies on generated data"*；`9-discussion.tex:30`（§9.4 future work）将 MCP/RAG/LLM-generated API calls 列为 *"immediate next step"*。
- **关键核实**：Jelly **没有碰真实第三方 app**。`5-pipeline.tex:72`、`9-discussion.tex:30`。
- **与 TaskVM 本质区别**：Jelly 的内部 model **就是** state（source of truth），TaskVM 的 surface **只是**外部真实 app 的投影（绝非 source of truth）。Jelly §9.4 future work（MCP/RAG/AppWorld）公开承认它做不到 live external——**这不是 collision 风险，是 novelty 本身**（他们公开做不到，我们做到了）。Jelly 根本不在"GUI-agent-操作-真实-app"这个游戏里 → 没有 substrate → 不是 VM。**核心区别：Jelly 的回退是内部 model 的 cheap rollback，TaskVM 的回退是真实 app 变更的 compensation。**
- **撞车风险（VM 框架下）**：**3.5/10**（从旧版 6 下调——VM 框架下 Jelly 没有 substrate，根本不是 VM）。

### 3.5 SaC — app 就是 state，不写回，无 reconcile【核对】

- **venue/日期**：arXiv preprint，无月份。`main.tex:2`。
- **投影什么**：**agent 构建的信息架构（agentic application）**，状态 `s_t=(V_t, Φ_t^s, C_t)`。`1.intro.tex:54`。
- **用户操纵什么**：structured affordances（filters/selectors/buttons/sliders）。`1.intro.tex:59`。
- **持久 vs 一次性**：**持久**（persistent bidirectional medium）。`3.2.tex:71`；`1.intro.tex:67` *"The application itself becomes the interaction state"*。
- **回退真实 app 变更**：**无**。
- **checkpoint/事务性**：**无（事务一致性列为未来待解）**。`6.2.Limitation.tex:19` *"transactional consistency when multi-step workflows partially fail, and frontend state synchronisation... are a set of interconnected backend problems"*。
- **验证机制**：**无定量验证**（scenario-based existence proof）。`6.2.Limitation.tex:28` *"an existence proof, not an empirical characterisation"*。
- **随世界动态重投影**：**部分（on-access 刷新，非持续自动；且当前 agent 只读不写）**。`4.5.tex:30` *"updated, not evolved"*。
- **关键核实**：SaC **当前实现不支持写回**。`6.2.Limitation.tex:17` *"The current implementation does not perfectly support write-capable agent execution—the agent can retrieve and synthesise information from the environment, but cannot act on it: submitting forms, writing data, triggering external services, or modifying state in external systems"*。
- **与 TaskVM 本质区别**：SaC 是**父范式**（动态 app 作交互媒介），不是竞品。source-of-truth 那把刀救我们：SaC 里 app **就是** state，TaskVM 里 surface **只是投影**。SaC 逐字把 write-back + 事务一致性 + frontend↔ground-truth 同步列为未解决 future work = **正是 TaskVM 的核心**。**核心区别：SaC 的 app 是自洽生成的 state 本身，TaskVM 的 surface 是外部真实 app 的可验证投影。**
- **撞车风险（VM 框架下）**：**4/10**（大概念撞车最重，但技术问题没撞死；无 substrate → 不是 VM，因为 app=state 而非 app=被投影对象）。

### 3.6 DuetUI — top-down 意图共生成，服务 LLM 模拟，无真实执行【核对】

- **venue/日期**：CHI '26，April 13–17 2026，Barcelona。`chi26-175.tex:68`。
- **投影什么**：**task decomposition**（task/subtask/data 三层）+ 对应 InterfaceDescription。`5-Implementation.tex:18`。
- **用户操纵什么**：直接操纵 interface 元素，**隐式引导 agent**（重塑意图）。`4-System.tex:178`。
- **持久 vs 一次性**：**持久**（bidirectional context loop）。`5-Implementation.tex:32`。
- **回退真实 app 变更**：**无**。
- **checkpoint/事务性**：**部分（"commit" 指 Task Loop 写入 Context Layer，非事务）**。`5-Implementation.tex:32`。
- **验证机制**：混合——资深 UX 设计师标注 GT + GPT-4 LLM-as-judge（功能等价匹配）+ expert 评分。**GPT-4 同源**（persona 生成 + 数据集 + baseline + judge）。
- **随世界动态重投影**：**无外部 app 重投影**（外部服务 LLM 模拟）。`5-Implementation.tex:23` *"executes calls to external services (simulated via LLMs)"*。
- **关键核实**：ServiceAgent **LLM 模拟**。`5-Implementation.tex:23`。`8-Discussion.tex` sec:mock 把 real-time source verification + iterative error repair 列为 future work。
- **与 TaskVM 本质区别**：DuetUI 是 top-down（prompt→任务分解→界面）、服务模拟、人**高频 co-generator**、UI"帮 Agent 理解人"。TaskVM 是 bottom-up、真实执行、人**低频 governance**、UI"帮人理解 Agent 正在改变的世界"。**核心区别：DuetUI 连真实 app 都不碰。**
- **撞车风险（VM 框架下）**：**2.5/10**。

### 3.7 AOHP — OS 层 service composition，非 state 绑定，无 verify【核对】

- **venue/日期**：Technical Report / preprint（thuair 清华模板），无月份。`main.tex:1`。
- **投影什么**：**personalized service composition / generated service entrance**（聚合多 app 服务能力的任务级入口）。`main.tex:207`。
- **用户操纵什么**：**personalized service entrance**（任务级概念，如"shopping"）。`main.tex:128`。
- **持久 vs 一次性**：**持久**（generated entrance + system memory 跨 app）。`main.tex:219`。
- **回退真实 app 变更**：**无**。
- **checkpoint/事务性**：**仅评估指标**（`main.tex:300` checkpoint-weighted completion rate 是 benchmark 评分粒度，非运行时事务）。
- **验证机制**：benchmark 完成率（objective checkpoints）+ 安全案例研究。无独立 GT verifier。
- **随世界动态重投影**：**部分（Event Stream 让 agent 订阅实时事件，是 agent 侧感知，非用户界面重投影）**。`main.tex:248`。
- **与 TaskVM 本质区别**：AOHP 改 OS 内核/框架层（AOSP fork），解决"agent 如何高效访问系统资源"，不是"把多个 app 真实状态压缩为可编辑任务投影"。无 round-trip verifier、无任务变量绑定（service composition ≠ state binding）、personalization 是跨 app 偏好记忆。**核心区别：AOHP 是 agent-OS harness，TaskVM 是人可直接操纵的任务虚拟机。** 可借鉴其 checkpoint-weighted completion rate 评分方式。
- **撞车风险（VM 框架下）**：**2.5/10**。

### 3.8 Macaron-A2UI — 明确排除既有界面执行，是 TaskVM 的缝隙不是竞品【核对】

- **venue/日期**：mindlab 类，Date=May 2026，疑似 COLM 2026 投稿。`colm2026_conference.tex:46`。
- **投影什么**：**assistant 侧 A2UI 消息序列**（surfaceUpdate/dataModelUpdate/beginRendering/deleteSurface）。`3-problem.tex:7`。
- **用户操纵什么**：client 渲染的 UI 控件（declarative 协议 + trusted catalog）。`3-problem.tex:5`。
- **关键核实**：Macaron **明确拒绝执行现有界面**。`2-relatedworks.tex:5` *"we focus on assistant-side Generative UI rather than action execution over an existing interface"*。
- **与 TaskVM 本质区别**：Macaron 是**解码器**这一半（GenUI 模型），明确排除**编码器**（既有界面执行）。TaskVM 占据的正是 Macaron 让出的那一半。**可复用资产**（不作竞品）：A2UI-Bench（300 任务）、L1 自动 / L2·L3 LLM-judge(gpt-5.1) / V1-V3 VLM-judge、Flutter Web renderer + 23 组件 catalog + render-check gate、LoRA SFT→GRPO 配方、训练好的 Grande(235B,74.2)/Venti(GLM-5.1,75.6)。但**不部署其训练模型**（主线用前沿通用模型 + A2UI v0.8 spec 注入 prompt；不下载 → 不算复用训练产物 → 不 pin 死 v0.8）。
- **撞车风险（VM 框架下）**：**1.5/10**。

### 3.9 Sidekick — 纯反馈层，不执行不写回【核对】

- **venue/日期**：UIST '26，November 02–05 2026，Detroit。`main.tex:112`。
- **投影什么**：**CUA 的执行状态/动作/reasoning**（多模态反馈）。`1_intro.tex:22`。
- **用户操纵什么**：**只是看/监听**（feedback-only）；可在连续错误达 red 时让 CUA **pause**。`4_system.tex:15`。
- **回退真实 app 变更**：**无**。
- **checkpoint/事务性**：**无**。
- **验证机制**：VLM 自验证（错误检测，非任务完成 GT）。`4_system.tex:14`。
- **关键核实**：Sidekick **feedback-only、从不写回**。`1_intro.tex:38` *"providing feedback to support transparency, progress awareness, and context resumption"*。
- **与 TaskVM 本质区别**：Sidekick 压缩的是 **actions**，TaskVM 压缩的是 **applications**。Sidekick 的界面是 Agent 的仪表盘，TaskVM 的界面是软件世界的控制台。**核心区别：Sidekick 是 contrast case（不执行、不写回、不投影 state），不是竞品。** 设计 4 条件研究时吸取其 PT baseline 没打赢 chat-only + 制造"虚假安全感"的教训。
- **撞车风险（VM 框架下）**：**1/10**。

### 3.10 能 claim / 不能 claim（汇总）

**能 claim**：首次把异质既有软件运行时编译成一个**可治理的、双向忠实的、可回退的、substrate 无关的、独立验证的**任务虚拟机——人治理 VM-state，Agent 在真实 app 中实现并验证该状态。
**不能 claim**：首次 task-centric computing（Activity-Centric 2006）；首次 agent 生成任务 UI（DuetUI/Jelly）；首次 GUI+MCP hybrid；首次动态 app 作交互媒介（SaC）；首次 GenUI×GUI-agent 组合（Morae/AgentLens）；首次可编辑持久任务面（ALLOY）。

---

## 4. 三方撞车独立判断 + "一眼拉开差距"设计【核对·VM 框架重估】

### 4.1 VM 框架下的撞车 re-rank（0-10，诚实，不谄媚）

评估口径：假设 TaskVM 已按 VM 框架做出来（双向忠实 + substrate-independence + governance/可回退 + 独立 verify），哪篇最威胁**这个 VM 楔子本身**。

| 排名 | 工作 | VM 框架分 | 旧版分 | venue | 变化理由 |
|---|---|---|---|---|---|
| 1 | **ALLOY** | 4 | 5 | preprint 2025-10 | 占 VM 性质最多（real-app+持久可编辑+多 site），但 state-vs-procedure + ex-ante-vs-动态 是本体论差异 |
| 2 | **SaC** | 4 | 5.5 | preprint 2026 | 大概念撞车最重，但无 substrate（app=state）→ 不是 VM；逐字交出 write-back/reconcile |
| 3 | **Jelly** | 3.5 | 6 | CHI'25 | **下调最多**——无 substrate（§9.4 自承不碰真实 app）→ 根本不是 VM；"future work"翻面成 novelty |
| 4 | **Morae** | 3 | 6 | UIST'25 | 占 load-bearing primitive，但单 app/单选择/一次性/self-ask 自评 |
| 5 | **AgentLens** | 3 | 4 | UIST'26 preprint | GenUI 按设计非交互=与"双向"正相反；单 app；post-2026-01 屏蔽不适用 |
| 6 | **DuetUI** | 2.5 | 3.5 | CHI'26 | top-down 意图、服务模拟、无真实执行 |
| 7 | **AOHP** | 2.5 | 3 | TR 2026 | OS 层 service composition，非 state 绑定 |
| 8 | **Macaron** | 1.5 | 2 | COLM'26 | 明确排除既有界面执行——是缝隙不是竞品 |
| 9 | **Sidekick** | 1 | 1.5 | UIST'26 | 不执行不写回——contrast case |

**全部下降**——不是因为谄媚，是评估对象变具体了：VM 框架要求双向忠实 + substrate-independence + governance/可回退 + 独立 verify 这一组，9 篇每篇最多占 1-2 个，没人凑齐。**Jelly 下调最多**（6→3.5）是关键修正：旧版把"§9.4 future work 指向我们"当 collision 风险，VM 框架下它是"他们公开做不到，我们做到了"= novelty 本身。

### 4.2 VM 性质占用矩阵（实证，非叙事）

| VM 性质 | 谁占了 | TaskVM 独占？ |
|---|---|---|
| bottom-up live projection | Morae（当前页）、AgentLens（Full/Partial UI 镜像）、SaC（app=state）、Jelly（内部 model=state）各占一半 | 不独占 |
| bidirectional executable binding（一变量→多 app+写回） | **无一篇** | **独占** ✓ |
| substrate-independence（JVM moment） | **无一篇** | **独占** ✓ |
| governance + reversibility（回退真实 app 变更） | **无一篇**（Jelly 仅回退内部 model） | **独占** ✓ |
| round-trip verification（独立 GT + 非干涉 + neg-control + reconcile） | **无一篇**（Morae=self-ask 自评） | **独占** ✓ |

**安全楔子完全落在性质 2+3+4+5**——bottom-up projection（性质 1）单独不能扛（Morae/AgentLens/SaC/Jelly 各占一半）。论文**必须**立在 2-5 这个组合上，不能立"首次组合 GenUI+GUI agent"/"首次可编辑任务面"/"首次 live-state 投影"（全死）。

### 4.3 顶层设计哲学：一个判别问题让人一眼分清九篇

核心判别问题：**这个界面是"什么的投影"？操纵它会"改变什么、能回退吗、是否被独立验证、跨 substrate 吗"？**

| 工作 | 界面投影 | 操纵改变 | 能回退真实变更 | 独立验证 | 跨 substrate |
|---|---|---|---|---|---|
| ALLOY | procedure DAG | 工作流节点 | 否 | 否 | 否（单浏览器） |
| Morae | 当前页 choice | 一个选择 | 否 | 否（self-ask） | 否（单 app） |
| AgentLens | GenUI 对话/Partial UI 镜像 | 回答/单点触 | 否 | 否 | 否（单 app） |
| Jelly | 内部 data model | model 字段 | 仅内部 model | 否 | 否（无真实 app） |
| SaC | 生成的 app=state | 演化这个 app | 否 | 否 | 否（app 即 state） |
| DuetUI | 任务分解 | 重塑意图 | 否 | GPT-4 同源 | 否（服务模拟） |
| AOHP | service entrance | 任务概念 | 否 | benchmark 完成率 | 否（OS 层） |
| Macaron | assistant GenUI | UI 控件 | 否 | LLM/VLM-judge | 否（排除执行） |
| Sidekick | agent 动作 | 只看 | 否 | VLM 错误检测 | 否 |
| **TaskVM** | **多真实 app 的 VM-state** | **治理世界状态** | **是（compensation）** | **是（独立 GT）** | **是（JVM moment）** |

哲学一句话：ALLOY 让人编辑程序；Morae 让人选选择；AgentLens 让人答对话；Jelly 让人改内部 model；SaC 让 app 成为 state 本身；Sidekick 让 UI 当 Agent 仪表盘；**TaskVM 让人治理一个跨真实 app 的任务虚拟机——你改变的是世界，不是 Agent 的计划，且每一次改变都可回退、被独立验证、跨 substrate 一致。**

### 4.4 demo 具体展示：四步弧（九篇都做不到）

见 §2 的场景 1-4。这是单一连续弧，把 VM 性质 2-5 全部演出来，且九篇**物理上无法复现任何一步的"真实写回 + 回退 + 验证 + 跨 substrate"**：
1. 改发布日期 → 多 app 真实同步改（ALLOY/Morae/AgentLens 单 app；Jelly/SaC/DuetUI 不写回；Macaron 排除执行；Sidekick 不执行）；
2. verifier 非干涉确认（九篇无 verifier）；
3. 撤销 → 真实 app 变更真实复原（Jelly 仅回退内部 model，其余无回退）；
4. 外部改 Jira → reconciliation 标红（SaC 逐字交出的 future work）；
5. 同操作跨 Stack A/B 界面稳定（JVM moment，九篇不要求也不演示）。

**写进 teaser figure + intro 第 1 页**，审稿人一眼分清。一句话锋利版：*Morae projects a choice. AgentLens projects a conversation. ALLOY projects a procedure. Jelly projects an internal model. SaC projects an app-as-state. TaskVM projects the live state of the software world — and lets you govern it: drive, roll back, and verify cross-application effects, substrate-independently.*

### 4.5 诚实代价（不报喜不报忧）

VM 框架**降低 collision 风险，但抬高执行门槛**：
- **Collision 风险 ↓**：楔子更锋利、更无人占（性质 2-5 全缺）。novelty 这仗现在能打。
- **Execution 风险 ↑**：W1 已实现 binding-discovery compiler + round-trip verifier（非干涉硬门 + 负对照，已 PASS）。但 VM 的其余性质——**回退/撤销真实变更（compensation）、只读区/可读写区分离、JVM moment、checkpoint+自底向上动态收集**——全是 W2-W5 要建的。5 周内建不完，"VM"就是 aspirational，审稿人会说"你叫 VM 但没回退没 substrate-invariance"。
- **净判断**：可投、有竞争力、非稳中。**主导不确定性从"够不够 novel"（已基本解决）转到"5 周建不建得完 VM 的其余性质 + OOD 泛化站不站"**——这是更好的位置，因为后者能靠 W2-W5 直接改善。**不需要训练模型，train-free 主线，尽力就好。**

---

## 5. 系统架构骨架（VM 框架，W1 已实现粗体部分）

```
taskvm/
├── apps/                  # 自建可重置 Web 应用（sqlite/内存后端，复用 SenseAct engine 模式）
│   ├── calendar/  taskboard/  drive/  _heldout/
│   │   └── engine/        # reward.py(判成功) / injector.py(初始状态+外部并发注入) / *_db.py
├── harness/               # browser_controller(Playwright) / state_adapter(reset·seed·read-canonical) / trace_capture / replay_engine / shadow_txn(★回退/compensation, W2-W3 建)
├── task_state/            # representation(VM-state schema) / compiler(GUI Agent=编码器→VM-state) / entity_binding(★双向绑定,一变量→多 app) / dependency_graph(effect 传播) / projection_policy(规则/启发式,不追求 Pareto)
├── execution/             # patch_compiler(VM 变量→semantic patch) / replanner / action_dispatcher(GUI/MCP/API hybrid) / rollback(★compensation/saga,W3 建)
├── verifier/              # app_state_checks / cross_app_checks / non_interference(硬门) / round_trip_checks / reconciliation(★随世界重投影,W3-W4) / rollback_verify(★回退后真实复原,W3)
├── workspace_ui/          # renderer / editable_components / live_sync  ★两区分离:只读区(投影)+可读可写区(governance),W2-W3 建
├── benchmark/             # task_templates / initial_states(隐藏 canonical graph) / user_edits / ood_splits / live_runs；API 成本追踪复用 SenseAct cost_model.py
├── baselines/             # 规则/类型匹配·prompt-only·frontier+shadow·人工 binding 上界·规则+critic
├── user_study/            # 4 条件
└── evaluation/  docker-compose.yml  README.md
```

**W1 已实现**（commit `1d8feee`，已 PASS，非 cherry-pick）：`apps/{calendar,taskboard}` + `harness/{state_adapter,replay_engine,trace_capture}` + `task_state/{representation,compiler,entity_binding,dependency_graph,projection_policy(rule stub)}` + `execution/{patch_compiler,action_dispatcher}` + `verifier/{canonical_state,round_trip_checks,non_interference}` + `workspace_ui/renderer` + `benchmark/{fixtures,model_client,cost_model,a2ui_spec}` + `evaluation/run_w1_killtest`。

**W1 实测**：2 任务×3 样本，binding F1=1.0（模型仅从 a11y/DOM 观测发现 `release_date→calendar.E1.move_event+taskboard.T1/T2.set_deadline`），round-trip changed/untouched/resynced 全 1.0，**neg-control=0.3**（诚实，不报假阳性），**non-interference 硬门**（违反钳到 ≤0.3），**no-leak 静态可强制**（compiler 不 import fixtures/verifier）。跨 gpt-5.5/gpt-5.6-sol 可复现。

**三个承重不变量（违反任一即 void）**：① read-path-is-GUI/write-path-is-API split（compiler 读渲染 GUI 观测，永不读 DB）；② no-leak canonical state（verifier-only GT）；③ negative-control（broken dispatcher 必须 ≤0.3）。

---

## 6. 指标（GT 全部来自 sandbox 隐藏 canonical state，非模型自评）

七项自动指标 + 2 项 VM 专属：`Projection Coverage` / `Binding Accuracy` / `Round-Trip Fidelity` / `Non-Interference` / `Reconciliation Accuracy` / `Cross-App Consistency` / `Interaction Compression` + `OOD Generalization` + **`Rollback Fidelity`（回退后真实 app 复原忠实度，VM 专属）** + **`Substrate-Invariance`（同操作跨 Stack A/B 的界面/语义/轨迹一致性，VM 专属）**。
核心技术目标：在最大化 interaction compression 的同时维持 task projection fidelity + reversibility。
汇报三层：① Agent 仍能完成任务（E2E success/action数/latency/cost/non-inferiority vs C2 chat-agent）；② **VM 是否忠实可执行可回退**（coverage/binding/round-trip/non-interference/reconciliation/rollback/substrate-invariance/OOD）——技术主指标；③ 人是否真能 governance（识别跨应用错误正确率/修改耗时/回退成功率/低层操作数/最终是否符合用户治理意图）。

---

## 7. AI 侧：训什么 / 不训什么 + 协议

- **不训** UI 生成器（Macaron 已做到 30B/235B/754B + A2UI-Bench，且是自家工作）。**不训** GUI executor（UI-TARS/GUI-Owl/UI-Venus/Claude Computer Use 已在做，复用）。**不部署 Macaron 已训练模型**（Grande/Venti）。
- **主线 train-free**：前沿通用模型（GPT-5.6-sol / Claude-Sonnet-5）+ A2UI v0.8 spec 注入 system prompt（Macaron 自己对标的 full-prompt baseline 做法，不需要 skill 机制）。**两个模型角色独立**：Agentic-UI 生成模型（解码器）vs compute-use 执行模型（编码器+写回），两次独立调用不共享 context。
- **唯一值得学（W5 Go/No-Go）**：`Cross-App Binding Critic`——一个 VM 变量在异质 app、不同字段名、不同 GUI 表达中分别对应哪些真实状态与操作；改它后哪些关联状态必须同步（effect propagation）。标签来源：cloned sandbox 候选 binding → 读真实 before/after state diff → 与隐藏 expected diff 比 → 完全匹配正例、漏改/错改/多改 hard negative。**非 LLM 自评。** 仅当 seen app prompt≈92% 但 renamed≈71%、unseen≈54%、错误集中在同名异义/隐含依赖时才训（3B-7B QLoRA）。否则 train-free。**verifier 永远来自环境状态，绝不让生成 binding 的模型自评。**

---

## 8. 五周工作计划（VM 框架，W1 已 PASS，重新组织 W2-W6）

> 用户确认：W1 一下午 agent 就做完了，不用一周；不需要训练模型，尽力赶上 CHI。旧版 W1/W4 双 kill-test 已过时——VM 框架下 kill-test 是**分布式的**，每个 VM 性质都有自己的 gate。计划按"5 周建完 VM 其余性质 + OOD 站 + 用户研究"组织，工作 solid 优先、尽力赶 CHI。

### 核心锚点重述（驱动排期）
旧四锚点（跨 app 等）只抓 VM 冰山一角，导致 W1 只建了"binding-discovery + round-trip verify"而**忽视了回退/两区/JVM-moment/governance**。新计划围绕 **5 个 VM 性质**（§0 锚点）各设一个 gate：

| VM 性质 | gate 名 | 周次 | gate 判据 |
|---|---|---|---|
| bidirectional binding + round-trip | **W1 已 PASS** | W1（done） | binding F1=1.0 from GUI obs + round-trip 1.0 + neg-control ≤0.3 |
| governance + reversibility | **W3 rollback kill-test** | W3 | 撤销一次跨 app 变更 → 真实 app 复原忠实（Rollback Fidelity）+ 只读区/可读写区分离可用 |
| bottom-up dynamic re-projection | **W3 reconciliation kill-test** | W3 | 外部并发改 app → 只读区自动标红 + 合并选项（reconciliation） |
| substrate-independence | **W4 JVM-moment kill-test** | W4 | 同操作跨 Stack A/B 界面稳定 + 语义一致 + 轨迹不同（Substrate-Invariance） |
| OOD generalization | **W4 OOD kill-test（命门）** | W4 | rename/reskin/未见 app 上 binding F1 不崩（>0.6） |

### 排期

| 周 | 日期 | 目标 | 必须完成的纵向切片 + gate |
|---|---|---|---|
| **W2** | 8/7–8/13 | 第 3 app + 两区 UI + 回退骨架 | Drive app + state adapter 泛化；`workspace_ui` 两区分离（只读区投影 + 可读可写区 governance）；`execution/rollback`（compensation/saga）骨架；live-mode 小规模。**gate：两区可分别操纵，回退骨架能撤销单 app 单步** |
| **W3** | 8/14–8/20 | **governance kill-test（rollback + reconciliation）** + benchmark 雏形 | 跨 app 回退忠实（Rollback Fidelity）+ 外部并发注入触发 reconciliation 标红；`verifier/rollback_verify` + `verifier/reconciliation`；benchmark 40 模板/800 实例 + 隐藏 canonical graph + 各 baseline。**gate：W3 rollback kill-test + reconciliation kill-test 双 PASS（neg-control 仍 ≤0.3）** |
| **W4** | 8/21–8/27 | **JVM-moment + OOD kill-test（命门）** + 训练 Go/No-Go | Stack A/B（2 stack × 2-3 app）substrate-invariance；OOD split（rename/reskin/未见 app）；训练决策（大概率 train-free）；failure analysis。**gate：JVM-moment kill-test + OOD kill-test（命门，F1>0.6）** |
| **W5** | 8/28–9/3 | 用户研究 + 论文主体并行 | 若 IRB 就绪：~12-18 人 within-subject 4 条件（C0 raw-multi-app / C1 static-read-only / C2 chat-agent+tools non-inferiority / C3 ours），非劣性 margin=C3 不低于 C2 的 5pp，N=18；并行写 Intro/RW/System/Benchmark/Eval。IRB 无则走轻量自述路径，pilot + 后置正式，不阻塞投稿 |
| **W6** | 9/4–9/10 | 冻结 + 投稿 | 重跑终值 + 统计/可视化 + ablation + figure（teaser 用 §4.4 四步弧）+ demo 视频 + 匿名化 + 全文收敛 + 投稿 |

**最关键风险**：① **W4 OOD 是命门**（W1 只测近同构 date-move，泛化未证）——建议 W2 收尾就备 OOD fixture，W3 benchmark 嵌 OOD split 提前跑，别等 W4 才发现不泛化。② **W3 rollback/reconciliation 的工程量**——compensation/saga 跨真实 app 不简单，是 VM 事务性的硬骨头。**功能冻结**（W6）：禁止新增 OSWorld 全量/记忆/长期 history/UI 风格生成/多任务族/通用 Agent OS/训练（除非 W4 OOD gap）。

---

## 9. 三条落地形态 + 3 RQs

**落地 = 同时是三样东西**：① 产品 Demo（跨 app 任务生长成一个可治理的 VM，用户在界面治理、Agent 在后台操作真实软件）；② 研究系统（读 GUI/tool 执行→维护 VM-state→生成两区界面→把用户治理操作传播到多 app→回退→自动检测不同步与错误→独立验证）；③ 研究评价平台（独立回答 VM 是否忠实、用户治理是否真执行、跨 app 是否一致、回退是否复原、substrate 是否无关、Agent 成功率损失多少、用户是否更高效）。

**三个研究问题**：
- **RQ1（VM 正确性）**：能否从在线 GUI/tool 轨迹中准确维护一个实时 VM-state，并随世界状态动态重投影？
- **RQ2（可治理控制）**：用户对 VM-state 的操纵（推进/回退/checkpoint）能否被正确传播到多个应用、可回退、并保持最终状态一致？
- **RQ3（governance 价值 + 自动化代价）**：与聊天和轨迹式监督相比，VM 界面能否提升理解/错误发现/修改/回退效率，同时保持 Agent 任务成功率与自动化收益？

---

## 10. 负样本边界（不做什么）

- ❌ 漂亮 Agent Dashboard 但不能写回真实应用（只是可视化）。
- ❌ 把每步轨迹变成可编辑卡片（仍是 trajectory supervision，与 Magentic-UI 增量）。
- ❌ 动态生成任意 UI、研究"该用列表还是看板"（与 DuetUI/SaC 正面重合）。
- ❌ 通用 Agent OS / 所有软件所有任务长期记忆多设备（不可验证）。
- ❌ 只做用户研究、没有自动保真证明（不符合技术定位）。
- ❌ 为 tech-heavy 强行训练 text-to-UI 模型（训练对象不是瓶颈）。
- ❌ 手工为每个任务写 React 页面和 binding（那是定制 dashboard，不是 software compiler）。
- ❌ 信任/同意/技能漂移/来源/未来复用/策略编辑等同在一篇（概念膨胀，无主线）。
- ❌ 同一个模型生成 binding 又判断 binding 对错——最终 verifier 必须来自环境状态。
- ❌ 每个 app 手写全部 binding——会杀死"动态编译既有软件"的 claim，核心 binding 必须至少部分自动发现。
- ❌ **"人审核 Agent 结果、确认后才能继续"的设计/措辞**（Sidekick 式人工中制，用户特别讨厌，"太老了"）——人治理（设 checkpoint + 回退），不审核。
- ❌ demo 开场像"四 agent 进度条 + 暂停/查看日志/继续"（跨应用版 Sidekick，死路）。
- ❌ 把"统一信息实体"建成独立模块（仅作 task_state 叙事包装，否则撞 SaC "app becomes interaction state"）。
- ❌ 把"范式变革"（Codex/Claude Code/OpenClaw 持久个人助理）当 novelty 防线（只能当 intro motivation；ALLOY/Morae/Jelly 不是关于跨会话记忆的，范式变革技术上没 supersede 它们）。

---

## 11. 范式上限八条（更强模型也消不掉的天花板，Discussion 用）

1. 压缩 vs 保真 trilemma（|U|<<|W|）：简化=删区别=价值判断，模型更强不能消除。
2. 任务非一维/单调：多目标任务是 Pareto trade-off，无唯一"向右"。
3. 无 canonical task skeleton：同一组软件状态可对应完全不同任务理解，界面选择本身在定义问题。
4. 过程即价值：阅读/创作/谈判/诊断等价值在过程中，删除过程=改变任务。
5. 跨设备 ≠ 跨权限：不同设备/应用属不同主体，强模型不能替合法主体消除冲突。
6. 无原子跨应用事务：独立真实系统不共享事务协议，必须承认"部分实现/等待同步/需要补偿"状态——**这正是 governance + 回退（compensation）存在的理由**。
7. 错误爆炸半径：抽象层压缩操作数 = 压缩错误传播距离，交互压缩率越高用户越需要结果可见性。
8. 视觉可变、交互语法不可变：配色/动画可个性化，但"什么表示未执行/执行中/已完成、什么会产生外部效果、如何撤销、异常如何显示"必须稳定。

诚实上限：把机器产生的偶然复杂性吸收掉，把人类真正需要决定的复杂性重新显形。简单任务=进度条；复杂任务=驾驶舱；不可压缩任务=透镜。研究高度：一个任务哪些维度可被安全编译掉、哪些必须作为人可见可操作自由度保留——这是"可治理 VM"的设计约束。

---

## 12. 不能丢的先验 / 地雷（coding agent 必读）

1. **VM 框架五性质同时存在**才拉开差距（§0）。少一个就被竞品吸收。**尤其：governance（人侧）≠ autonomous（agent 侧），不要搞混**——AgentLens 侧重 autonomous 下何时介入，ALLOY 人类监督工作流，TaskVM 是 governance 大前提下尽量自治，方向相反。
2. **verifier 永远来自环境状态**（sandbox 隐藏 canonical state），**绝不让生成 binding 的模型自评**。
3. **不部署 Macaron 已训练模型**。主线 = 前沿通用模型 + A2UI v0.8 spec 注入 prompt。不下载模型 → 不算复用训练产物 → 不 pin 死 v0.8（协议升级只换 prompt 文本）。
4. **两个模型角色独立**（解码器=UI 生成 vs 编码器+写回=compute-use），两次独立调用不共享 context。
5. **SCF 降级为衍生贡献**。一等贡献 = 可治理的可执行 VM 本身。`projection_policy` 用规则/启发式，不跑 Pareto；SCF 完整三轴测量写 Discussion/Future Work。
6. **demo 开场必须用 §4.4 四步弧**（改日期→真实同步改→verifier 非干涉→撤销真实复原→外部改触发 reconcile→跨 Stack 稳定）。**绝不用状态仪表盘开场**。
7. **人的角色 = "任务起始时主动设 checkpoint + 随时回退的 governance 者"**，**不是**"低频授权节点"或"审核者"。安全性全部下放给自动 verifier。
8. **"统一信息实体"仅作 task_state 叙事包装**，不建独立模块（否则撞 SaC）。
9. **项目代号 = TaskVM**（不用 A2UI）。A2UI 协议可作渲染层传输格式，与更名不冲突。
10. **AOHP 是相邻工作、不撞车**（OS 层 service composition，无 verify，无 state 绑定）。可借鉴 checkpoint-weighted completion rate。
11. **benchmark 混合**：3 白盒（Calendar/TaskBoard/Drive）+ 1 held-out 黑盒（对模型黑盒/对 verifier 白盒）。held-out = 两者都要（1 个真未见 app + 已见 app 的 rename/reskin/schema 变体），分别报 OOD。Mail = out。规模 40 模板/800 实例/OOD ~20%。
12. **用户研究 = 4 条件**（后置非阻塞，自动评测是主体）。非劣性 margin = C3 不低于 C2 的 5pp，N=18 within-subject。IRB 无则走轻量自述路径。
13. **reconciliation = re-read-on-action + 冲突标红不静默覆盖**；并发修改注入 = `injector.py` 预留最简接口（W3 用）。
14. **跨设备 = appendix showcase**；wind-tunnel = 推迟。
15. **回退/撤销 = compensation/saga 真实复原 app 变更**（不是内部 model cheap rollback）——这是 VM 事务性，W3 建。

---

## 13. 开工指引（面向 coding agent）

1. **W1 已 PASS**（§5），不重做。直接进 W2。
2. 据 §5 骨架 + §8 排期，W2 先建：第 3 app（Drive）+ 两区 UI 分离 + rollback 骨架。
3. **W3/W4 的 kill-test 是命门**（governance rollback/reconcile + JVM-moment + OOD）——这些是 VM 框架下"VM 之所以叫 VM"的证据，建不出来论文就退回"可编辑任务面"（撞 ALLOY/Jelly）。
4. 开工即守：项目代号 TaskVM；独立项目不拖入其他仓库（SenseAct 仅作工程模式参考）；自动评测为主体、用户研究后置不阻塞；训练是 Go/No-Go 而非默认；不部署 Macaron 模型；两个模型角色独立；demo 用 §4.4 四步弧；人 = governance 者；回退 = compensation 真实复原。

---

# 附录 A：三个 CHI 方向候选 + 用户画像（用户补充，2026-07-30）

> 这是用户与 GPT 反复讨论后筛选出的三个"想做、且觉得有意思"的 CHI 方向。**方向 1 TaskVM = 我们已锁定的工作**。方向 2、3 是**备选与下一论文候选**，不替换当前锁定主线——但它们揭示了一个重要的心智模型一致性，见附录 A.4。
> 三个方向共享一个底层直觉：**GUI Agent 时代，应该有一个介于"人"与"异质真实软件"之间的、可验证的中间运行时层**，把一次性 Prompt-Response 变成有状态、可增量改、可撤回、可写回的任务虚拟机。

## A.1 方向 1：TaskVM（= 当前锁定主线）

**TL;DR**：让 GUI Agent 把多个真实应用"编译"成一个临时任务界面，用户在新界面中的每次操作，都能写回原软件的真实状态。
GUI Agent 能否基于多个现有应用的运行状态，动态生成一个统一的 agentic UI，让用户不必等待 Agent 黑盒执行，而是可以直接查看、修改和推进任务？

`真实 App 状态 → Agent 理解任务与环境 → 生成统一任务界面 → 用户直接交互 → Agent 将操作翻译回原应用 → 环境状态更新`

重点：把 GUI Agent 和 agentic UI 变成类似 Java 虚拟机的跨软件中间层——底层应用不同，但用户始终在一个面向任务、可交互、可写回的界面中工作。
**最相似竞品**：Software as Content / Sidekick / DuetUI（= 正文 §3-§4 已逐篇核对判定撞车程度与差异化设计）。

> 与正文的对应：方向 1 = §0 主线 = VM 框架五性质。"TaskVM"已正式定为项目代号。本附录的"虚拟机中间层"比喻与正文 §1.1 VM 类比一致。

## A.2 方向 2：PromptPatch（下一论文候选 / 近方向二"可执行的平行世界"的运行时侧）

**TL;DR**：用户可以在 Agent 执行过程中修改 Prompt，系统自动保留仍然有效的工作，只重做受新要求影响的部分。
当用户在耗时、高成本的 Agent 任务执行过程中，才发现原始 Prompt 遗漏或写错了条件，系统能否将新指令可靠地写入正在运行的任务，而不是停止后从头执行？

`初始 Prompt → Agent 规划并执行 → 保存工具调用、中间结果和依赖关系 → 用户发送 Patch → 判断影响范围 → 保留有效进度 → 局部回滚或重规划 → 继续执行`

重点：让 Agent 从一次性的 Prompt–Response 系统，变成支持热更新、断点续跑和增量计算的任务虚拟机，避免用户只能在"继续等待错误结果"和"停止并浪费已有成本"之间选择。
**最相似竞品**：Cocoa / Interactive Debugging and Steering of Multi-Agent AI Systems / ChatGPT Interrupt·Update / Cursor Immediate Interrupt / Windsurf Cascade / Devin Queued Messages。

> 与正文的关系：这是 HCI-UI 方向二"可执行的平行软件世界"在"运行时增量改"侧的一个收敛变体——不 fork 平行分支，而是在单条轨迹上做"增量 patch + 影响范围判定 + 局部回滚"。它要求的核心基础设施（保存工具调用/中间结果/依赖、增量重规划、断点续跑）与 TaskVM 的 `execution/` 层（patch_compiler / replanner / shadow_txn / rollback）高度重叠——是 TaskVM 跑通后自然的下一篇。

## A.3 方向 3：Beyond Submit（早期被放弃的"commitment boundary"方向的精炼复活版）

**TL;DR**：Agent 可以利用用户提交前的输入过程提前理解任务，但用户删除或放弃的内容必须能够从 Agent 的状态中真正撤回。
当 Agent 能够在用户按下 Send 前持续观察输入、删除、停顿和改写时，系统能否利用这些过程信息提前理解和准备任务，同时保证未提交草稿仍属于用户的私人空间？

`输入轨迹 → Harness 选择性暴露信息 → Agent 进行可逆的提前推理 → 用户修改或删除 → 回滚相关计划与状态 → 用户正式提交 → Agent 才能持久化并执行外部动作`

重点：Send 不只是输入按钮，而是从"私人构思"到"正式表达"的 commit boundary。Harness 应成为输入事务管理器，支持 pre-commit、commit 和 rollback，避免已删除的草稿继续影响 Agent 的计划、记忆和工具调用，形成 draft residue。
**最相似竞品**：Can You See Me Think? / ExPerT / Incremental Dialogue Systems / I-BOX。

> 与正文的关系：这正是 HCI-UI Round 1-2 的"Preserving User Commitment Boundaries"方向，后被 novelty audit 放弃（commitment boundary 与 2026 LLM-reasoning 撞名、VeriSafe/EffectGuard 等已覆盖）。方向 3 把它**精炼**到"输入事务/草稿撤回"这个更窄、更新颖的切面（draft residue 是个有辨识度的构念）。它不进当前 5 周主线，但作为下一论文候选保留。

## A.4 三个方向揭示的心智模型一致性（重要——解释为什么用户喜欢这三个）

这三个方向表面不同，底层共享同一个研究品味，可作为"用户画像"锚点：

1. **都把 Agent 当成"有状态的中间运行时"，而不是"一次性的对话工具"。** TaskVM=跨软件任务虚拟机；PromptPatch=支持热更新/断点续跑的任务虚拟机；Beyond Submit=输入事务管理器。三者都是"给 Agent 加一层可验证的运行时状态层"。
2. **都聚焦"中途可改、可撤回、可写回"这种"非一次成型"的人机协作结构。** 不研究"Agent 怎么更好地一次完成"，而研究"Agent 执行到一半、或还没执行时，人如何低成本、可逆地介入与修正"。
3. **都把"诚实性/可验证性"当硬约束。** TaskVM=独立 verifier 读 ground-truth；PromptPatch=影响范围判定+保留有效进度（不能假装没改）；Beyond Submit=draft 必须真正撤回（不能留 residue）。三者都拒绝"让模型自己说自己对"。
4. **都选"窄而锋利"的切面，而非宏大范式。** 三个都先找一个"审稿人能秒懂痛点的具体场景"（写回/改 prompt/撤回草稿），再技术化，而不是一上来喊"新交互范式"。
5. **都把"输入/操作 ↔ 真实世界状态"的边界（commit boundary）作为核心物理量。** TaskVM 的 round-trip、PromptPatch 的影响范围、Beyond Submit 的 commit/rollback，本质都是在精确定义"什么时候一次输入真正对外生效、什么时候还没"。

**对正文的影响**：方向 1（TaskVM）已是主线，无变化。方向 2、3 不进 5 周主线，但：
- 方向 2 的核心需求（保存工具调用/依赖、增量重规划、断点续跑）应在 TaskVM 的 `execution/` 层**预留接口**，避免下一篇推倒重来。
- 方向 3 的 commit-boundary 思想可作为 TaskVM 中"用户编辑 → semantic patch → 执行"这一步的设计灵感：编辑未"提交"前是可逆的预览（shadow_txn 影子执行），提交后才真正写回并验证——这与正文 §5 的 `shadow_txn` 模块天然契合。

> 即：用户喜欢的这三个方向，本质是同一套"可验证的 Agent 运行时状态层"在不同生命周期阶段的实例化。TaskVM（跨软件写回 + 可治理 VM）是其中最完整、最 tech-heavy、最适合作为 CHI 2027 主线的那一个。

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
