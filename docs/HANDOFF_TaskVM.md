# TaskVM 开工交接 prompt（给 coding agent）

> 你是 TaskVM 项目的 coding agent。本文件是上一轮对齐 agent 留给你的开工交接，读完即可直接进入 plan 模式产出 W1 代码方案。所有产品/研究决策**已全部锁死、零遗留待确认项**——你的工作是工程实现，不是再发散选题。遇到任何与此处或权威文档冲突的早期材料，以权威文档为准，不要重新打开已拍板的问题。

---

## 0. 你是谁、要做什么、时间线

- 你是 **TaskVM** 项目的 coding agent（多 agent 并行开发模式，非单人手写）。
- 这是一篇 **CHI 2027 full paper** 的工程实现，tech-heavy HCI 工作，train-free 主线。
- 今天 **2026-08-06**；deadline **≈ 2026-09-10 AoE**，无延期、**约 5 周**。
- **W1 已 PASS**（commit `1d8feee`，binding F1=1.0 + round-trip 1.0 + neg-control 0.3，见 §3）。你现在直接进 **W2**（见 §4、§8）。W2-W5 是建 VM 的其余性质（回退/两区/JVM-moment/reconciliation/OOD）——这些是 VM 之所以叫 VM 的证据，建不出来论文退回"可编辑任务面"（撞 ALLOY/Jelly）。

---

## 1. 唯一权威文档（开工前必读第一件事）

路径：`docs/A2UI_开工大纲_v0_心智模型对齐版.md`（**文件名仍带 v0 字样，但内容已是"VM 框架一次成型版"**）。

这是**唯一**开工基础。任何冲突以此文档为准：
- 早期材料 `docs/oracle/` 下的 5 份原始 txt 是**只读参考**，不是决策源。
- 9 篇竞品/自家论文 tex 在 `docs/references/`（ALLOY / Morae / AgentLen / Jelly / SaC / DuetUI / AOHP / Macaron-A2UI / sidekick）——每篇的逐字核对负样本对比见权威文档 §3。

---

## 2. 一句话心智模型 + VM 框架五性质（不能丢的先验 #1）

**人治理任务（governance），Agent 自治应用（autonomy）。** TaskVM 把多个正在运行的**既有应用**的实时状态，反向编译成一个**可双向操纵、可回退、可验证、substrate 无关**的任务虚拟机界面；用户在界面同时操纵只读区（多 app 实时核心状态投影）+ 可读可写区（进度推进/回退/checkpoint），像操纵一个统一低认知负载 app。GUI Agent=编码器+执行器（读 app→VM-state，写回 app），GenUI 模型=解码器（VM-state→界面），双向忠实。

**VM 框架五性质（同时存在才拉开差距；旧四锚点只抓冰山一角"跨app"）**：
1. **bottom-up live projection**（自底向上实时投影，随世界状态动态重投影，非用户触发）
2. **bidirectional executable binding**（一变量→多 app+写回，双向；GUI Agent 编码器 / GenUI 解码器）
3. **substrate-independence**（JVM moment：同操作跨 Stack A/B 界面稳定语义一致轨迹不同）
4. **governance over autonomy**（人设 checkpoint+随时回退，**非**"审核后继续"；安全性下放自动 verifier。**核心词 governance 人侧，不是 autonomous agent 侧**——与 AgentLens/ALLOY 方向相反）
5. **round-trip verification + reversibility**（独立 GT verifier + 非干涉硬门 + 负对照≤0.3 + reconciliation + **回退后真实 app 复原 compensation/saga**）

**9 篇竞品无一同时做到性质 2-5**（实证矩阵见权威文档 §4.2）。冻结 RQ：*Can an agent compile live, fragmented application state into a task virtual machine that users can govern — manipulating a bidirectional projection to drive, roll back, and verify cross-application effects across heterogeneous existing software?*

---

## 3. 你要搭什么（核心构念 + 架构骨架）

**一等贡献 = 可治理的可执行 VM 本身**（人治理 VM-state，Agent 在真实 app 中实现并验证该状态）。**不是**安全压缩前沿（SCF 已降级为衍生贡献，见 §6）。

**主闭环**：`User (governance) ↔ GenUI (decoder) ↔ Shared VM-state ↔ GUI Agent (encoder/executor) ↔ 真实 apps`。Shared VM-state 保存目标/计划/已执行动作/真实结果/当前 app 状态/可回退事务日志——**不是聊天历史**。人位于 governance 侧（设 checkpoint + 随时回退），**不**在 autonomous 侧做"审核后继续"。

**八步往返**：Observe → Abstract → Project → Manipulate → Compile → Execute → Verify → Reconcile（+ Rollback 作为 governance 可逆层）。

**架构骨架**（目录树，**W1 已实现粗体部分，W2-W5 建其余**）：
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
**W1 已实现并 PASS**（commit `1d8feee`，非 cherry-pick）：`apps/{calendar,taskboard}` + `harness/{state_adapter,replay_engine,trace_capture}` + `task_state/{representation,compiler,entity_binding,dependency_graph,projection_policy(rule stub)}` + `execution/{patch_compiler,action_dispatcher}` + `verifier/{canonical_state,round_trip_checks,non_interference}` + `workspace_ui/renderer` + `benchmark/{fixtures,model_client,cost_model,a2ui_spec}` + `evaluation/run_w1_killtest`。
**W1 实测**：2 任务×3 样本 binding F1=1.0（仅从 a11y/DOM 观测发现跨 app 绑定），round-trip 三检全 1.0，**neg-control=0.3**，**non-interference 硬门**（违反钳≤0.3），**no-leak 静态可强制**。跨 gpt-5.5/gpt-5.6-sol 可复现。
**三个承重不变量（违反任一即 void）**：① read-path-is-GUI/write-path-is-API split（compiler 读渲染 GUI 观测永不读 DB）；② no-leak canonical state（verifier-only GT）；③ negative-control（broken dispatcher 必须 ≤0.3）。

**仓库命名**：目录保留 `a2ui/`（迁移成本不值得），内部包 `taskvm/`，类名 TaskVM 前缀。**项目代号 = TaskVM**。

---

## 4. 你的第一个动作（W2，立即执行）

**W1 已 PASS，不重做。直接进 W2。** W2 目标：第 3 app（Drive）+ state adapter 泛化 + **两区 UI 分离**（只读区投影 + 可读可写区 governance）+ **回退骨架**（`execution/rollback` compensation/saga）+ live-mode 小规模。
**W2 gate**：两区可分别操纵；回退骨架能撤销单 app 单步。
**注意**：W2-W5 是建 VM 其余性质（回退/两区/JVM-moment/reconciliation/OOD）——这些是 VM 之所以叫 VM 的证据，建不出来论文退回"可编辑任务面"（撞 ALLOY/Jelly）。

---

## 6. 不能丢的先验 / 地雷（这些若丢了会返工）

> 以下每条都是已拍板决策或已核实事实，**不要重新打开**。

1. **VM 框架五性质同时存在**才拉开差距（§2）。少一个就被竞品吸收。**尤其：governance（人侧）≠ autonomous（agent 侧），不要搞混**——AgentLens 侧重 autonomous 下何时介入，ALLOY 人类监督工作流，TaskVM 是 governance 大前提下尽量自治，方向相反。
2. **verifier 永远来自环境状态**（sandbox 隐藏 canonical state），**绝不让生成 binding 的模型自评**。这是核心诚实性约束。
3. **不部署 Macaron 已训练模型（Grande/Venti）**。主线 = 前沿通用模型（GPT-5.6-sol / Claude-Sonnet-5）+ A2UI v0.8 协议 spec 注入 system prompt。不下载模型 → 不算复用其训练产物 → 不 pin 死在 A2UI v0.8（协议升级只换 prompt 文本）。
4. **两个模型角色独立**：(1) Agentic UI 生成模型（多 app 状态→任务界面）；(2) compute-use 执行模型（在真实 App 里操作）。即使同用一家厂商的模型，也是**两次独立调用、不共享 context**。
5. **SCF 已降级为衍生贡献**。一等贡献 = 可执行投影本身。`projection_policy` 模块**保留**在架构里但用**规则/启发式**实现，**不跑 Pareto 实验**；SCF 完整三轴测量与 Pareto 写入 Discussion / Future Work。**不要**为 SCF 搭策略学习模块挤占 W1 kill test 资源。
6. **demo 开场必须用"操纵+写回+verifier+回退+跨substrate"四步弧**（权威文档 §4.4：改日期→真实 app 同步改→verifier 非干涉→撤销真实复原→外部改触发 reconcile→跨 Stack 稳定）。**绝不用"状态仪表盘"开场**（"四 agent 进度条 + 暂停/查看日志/继续" = 跨应用版 Sidekick，死路）。
7. **人的角色 = "任务起始时主动设 checkpoint + 随时回退的 governance 者"**，**不是**"低频授权节点"或"审核者"。用户**特别讨厌**"人审核 Agent 结果、确认后才能继续"这种 Sidekick 式人工中制介入（原话："我特别讨厌这种人类中制介入的研究，因为它太老了"）。正确口径：人治理（设 checkpoint + 回退），**不审核**；安全性/边界检查全部下放给**自动 verifier**。论文措辞与 demo 设计都必须避免任何会被读成"人审核后才能继续"的句式。
8. **"统一信息实体"只是 `task_state` 数据结构的叙事包装**（"同一任务变量在不同 App 里的绑定关系集合"），**不是**独立于 task_state、本体论意义上更根本的新实体。**不建独立模块**，仅作 Discussion 叙事升华。否则会撞 SaC 的"the application itself becomes the interaction state"语言，与"real state 永远留在真实 App 里、surface 只是投影、绝非 source of truth"的核心防御论点矛盾。
9. **项目代号 = TaskVM**（不再用 A2UI 作代号）。`A2UI` 是 Google 2025 年发起的通用 agentic UI 声明式协议名（`a2ui.org` v0.8），Macaron-A2UI（COLM 2026）是 Mind Lab 团队的开源第三方工作，与本项目及本团队无归属关系、**不是本项目前作**。三者不要混淆。TaskVM 实现中仍可选择性复用 A2UI v0.8 协议作 UI 渲染层传输格式（见第 3 条），与更名不冲突。
10. **AOHP 是相邻工作、不撞车**（已全文核对，`docs/references/AOHP-paper/`）。AOHP 改 OS 内核/框架层、无 round-trip verification、personalization 是跨 App 偏好记忆而非任务变量绑定——三点均与我们正交。可借鉴其 **checkpoint-weighted completion rate** 评分方式（比二元成功/失败细粒度），用于 verifier 打分设计。
11. **benchmark 混合**：3 白盒自建（Calendar/TaskBoard/Drive，sqlite）+ 1 held-out 黑盒（对模型黑盒/对 verifier 白盒 via state adapter）。**held-out = 两者都要**：1 个真未见 app（验迁移）+ 已见 app 的 rename/reskin/schema 变体（验反捷径），分别报 OOD 指标。
12. **Mail app = out**（Drive 作第 3 个 app，Mail 永久 optional）。**benchmark 规模 = 40 模板 / 800 实例 / OOD 占 ~20%**。
13. **用户研究 = 4 条件**（后置非阻塞，自动评测是论文主体）：C0 原始多 app GUI / C1 静态只读聚合 dashboard / C2 chat agent + 全 app 工具访问（Claude/GPT + 3 app MCP 工具，真正 non-inferiority 对手）/ C3 我们的投影。**非劣性 margin = C3 成功率不低于 C2 的 5 个百分点**（实验前定死）。**N=18 within-subject**。IRB：若所在机构无正式 IRB，走轻量自述路径（自愿参与/知情同意/数据匿名化），W5 pilot + 后置正式，不阻塞投稿。
14. **reconciliation 机制 = re-read-on-action**（用户编辑/定时心跳触发重读）+ 冲突时**标红不静默覆盖**，给出"底层已变 / 你的编辑 / 合并选项"。**并发修改注入**：不建"协作者"角色，只在 `apps/<name>/engine/injector.py` 预留最简"benchmark 自己主动注入外部状态变更"接口，W3 用（governance kill-test 的 reconciliation 部分）。
15. **跨设备 = appendix showcase**，不当贡献。**wind-tunnel（CUA 模拟用户）= 推迟，本期不建**。
16. **训练 = 诚实 Go/No-Go**（W4 决策点）：seen app prompt≈92% 但 renamed≈71%、unseen≈54%、错误集中在同名异义/隐含依赖时才训轻量 QLoRA critic（3B–7B，标签来自 cloned sandbox 真实 state diff，**非 LLM 自评**）。否则 train-free。**绝不为了 tech-heavy 硬训**。
17. **回退/撤销 = compensation/saga 真实复原 app 变更**（不是内部 model cheap rollback——Jelly 那种）——这是 VM 事务性，W3 建，是 governance kill-test 的核心。

---

## 7. 可复用工程模式（别重造轮子）

- **SenseAct 项目**（团队自家项目，**非本仓库**，路径 `/home/hadoop-mt-ocr/dolphinfs_ssd_hadoop-mt-ocr/zhangyuzhe09/SenseAct`；与本项目研究问题无直接关系，但工程结构高度可复用）：
  - `scenarios/<name>/engine/{reward,injector,*_db}.py` 模式 → 本项目 `apps/<name>/engine/`（每个自建 App 配一个判成功的 `reward.py` + 注入初始/外部状态的 `injector.py`）。
  - `senseact/cost_model.py` 真实 token 计量（不用启发式估算，每次调用记录真实 usage）→ benchmark 的 API 成本追踪。
  - `senseact/metrics.py`：SR + Success@Budget / cost-success AUC 组合 → "如何同时报告成功率与成本"的可执行范式。
- **AOHP**（`docs/references/AOHP-paper/`）：checkpoint-weighted completion rate 评分方式。
- **A2UI v0.8 协议 spec**（4 种消息类型）→ 注入 UI 生成模型 system prompt。
- **Macaron 的 A2UI-Bench 评测思路/judge prompts** → 可作参考（L1 自动 / L2·L3 LLM-judge / V1-V3 VLM-judge 架构），**但不部署其训练模型、不 pin 死 v0.8**。
- **API 调用量估算**（非精确，供预算）：单次完整 benchmark 跑测约 600–900 次调用；开发期迭代 10–20 轮累计约 6,000–18,000 次；探索性实验总量级 20,000–30,000 也合理。**建议参照 SenseAct `cost_model.py`，W1 跑几个 kill test 任务后拿真实数字校准**。

---

## 8. 5 周排期（VM 框架，W1 已 PASS，分布式 kill-test）

> 用户确认：W1 一下午 agent 就做完了，不用一周；不需要训练模型，尽力赶上 CHI。VM 框架下 kill-test 是分布式的，每个 VM 性质都有自己的 gate。

| 周 | 日期 | 目标 | 必须完成的纵向切片 + gate |
|---|---|---|---|
| **W2** | 8/7–8/13 | 第 3 app + 两区 UI + 回退骨架 | Drive app + state adapter 泛化；`workspace_ui` 两区分离（只读区投影 + 可读可写区 governance）；`execution/rollback`（compensation/saga）骨架；live-mode 小规模。**gate：两区可分别操纵，回退骨架能撤销单 app 单步** |
| **W3** | 8/14–8/20 | **governance kill-test（rollback + reconciliation）** + benchmark 雏形 | 跨 app 回退忠实（Rollback Fidelity）+ 外部并发注入触发 reconciliation 标红；`verifier/rollback_verify` + `verifier/reconciliation`；benchmark 40 模板/800 实例 + 隐藏 canonical graph + 各 baseline。**gate：W3 rollback kill-test + reconciliation kill-test 双 PASS（neg-control 仍 ≤0.3）** |
| **W4** | 8/21–8/27 | **JVM-moment + OOD kill-test（命门）** + 训练 Go/No-Go | Stack A/B（2 stack × 2-3 app）substrate-invariance；OOD split（rename/reskin/未见 app）；训练决策（大概率 train-free）；failure analysis。**gate：JVM-moment kill-test + OOD kill-test（命门，binding F1>0.6）** |
| **W5** | 8/28–9/3 | 用户研究 + 论文主体并行 | 若 IRB 就绪：~12-18 人 within-subject 4 条件（C0 raw-multi-app / C1 static-read-only / C2 chat-agent+tools non-inferiority / C3 ours），非劣性 margin=C3 不低于 C2 的 5pp，N=18；并行写 Intro/RW/System/Benchmark/Eval。IRB 无则走轻量自述路径，pilot + 后置正式，不阻塞投稿 |
| **W6** | 9/4–9/10 | 冻结 + 投稿 | 重跑终值 + 统计/可视化 + ablation + figure（teaser 用 §4.4 四步弧）+ demo 视频 + 匿名化 + 全文收敛 + 投稿 |

**最关键风险**：① **W4 OOD 是命门**（W1 只测近同构 date-move，泛化未证）——建议 W2 收尾就备 OOD fixture，W3 benchmark 嵌 OOD split 提前跑，别等 W4 才发现不泛化。② **W3 rollback/reconciliation 工程量**——compensation/saga 跨真实 app 是 VM 事务性硬骨头。**功能冻结**（W6）：禁止新增 OSWorld 全量/记忆/长期 history/UI 风格生成/多任务族/通用 Agent OS/训练（除非 W4 OOD gap）。

---

## 9. 用户研究品味画像（帮助你做设计取舍）

用户（CHI 作者）的研究品味五条锚点（权威文档附录 A.4）：
1. 把 Agent 当**有状态的中间运行时**，不是一次性对话工具。
2. 聚焦**中途可改、可撤回、可写回**的"非一次成型"协作结构。
3. 把**诚实性/可验证性**当硬约束——独立 verifier 读 ground-truth，拒绝模型自评。
4. 偏好**窄而锋利**的切面（先找一个秒懂痛点的具体场景再技术化，不喊宏大范式）。
5. 把**输入/操作 ↔ 真实世界状态的边界（commit boundary）**当核心物理量。

**当你面临设计选择，默认选**：(a) 加可验证运行时状态层；(b) 窄而锋利的具体场景；(c) 独立/ground-truth 验证而非模型自评；(d) 围绕 commit boundary 框架。**避免**：宏大范式框架、train-heavy/模型即 novelty、让 agent 自证正确、任何"人审核 Agent 结果后才能继续"的设计（那是 Sidekick 路线，用户特别讨厌）。

---

## 10. 三个 CHI 方向的上下文（仅供你理解用户品味，不影响 W1）

用户与 GPT 讨论过三个方向，**方向 1 TaskVM = 当前锁定主线**，方向 2/3 是下一论文候选：
- **方向 2 PromptPatch**（Agent 执行中改 Prompt，保留有效进度只重做受影响部分）→ 其核心需求（保存工具调用/依赖、增量重规划、断点续跑）应在 TaskVM 的 `execution/` 层**预留接口**，避免下一篇推倒重来。
- **方向 3 Beyond Submit**（提交前输入可逆、草稿可撤回）→ 其 commit-boundary 思想可作 TaskVM "用户编辑→semantic patch→执行"的设计灵感：编辑未"提交"前用 `shadow_txn` 影子执行做可逆预览，提交后才真正写回并验证（与 §3 架构的 `shadow_txn` 模块天然契合）。

---

## 11. 开工即守（checklist）

- [ ] 先读 `docs/A2UI_开工大纲_v0_心智模型对齐版.md`（唯一权威文档，VM 框架一次成型版）。
- [ ] 项目代号用 **TaskVM**（不用 A2UI 作代号）。
- [ ] **W1 已 PASS，直接进 W2**（建第3app + 两区UI + 回退骨架）。
- [ ] 这是**独立项目**，不拖入其他仓库（SenseAct 仅作工程模式参考，路径见 §7，不混入代码）。
- [ ] 自动评测为主体、用户研究后置不阻塞。
- [ ] 训练是 Go/No-Go 而非默认；不部署 Macaron 已训练模型。
- [ ] 主线用前沿通用模型 + A2UI v0.8 协议 spec 注入 prompt；两个模型角色独立（解码器=UI生成 vs 编码器+写回=compute-use）。
- [ ] demo 开场用 §4.4 四步弧（操纵+写回+verifier+回退+跨substrate），绝不用状态仪表盘开场。
- [ ] 人的角色 = governance 者（设 checkpoint + 随时回退），不是审核者；安全性下放给自动 verifier。
- [ ] 回退 = compensation/saga 真实复原 app 变更（不是内部 model cheap rollback）。
- [ ] VM 框架五性质同时存在才拉开差距；governance（人侧）≠ autonomous（agent 侧），不要搞混。
