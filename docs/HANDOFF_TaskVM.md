# TaskVM 开工交接 prompt（给 coding agent）

> 你是 TaskVM 项目的 coding agent。本文件是上一轮对齐 agent 留给你的开工交接，读完即可直接进入 plan 模式产出 W1 代码方案。所有产品/研究决策**已全部锁死、零遗留待确认项**——你的工作是工程实现，不是再发散选题。遇到任何与此处或权威文档冲突的早期材料，以权威文档为准，不要重新打开已拍板的问题。

---

## 0. 你是谁、要做什么、时间线

- 你是 **TaskVM** 项目的 coding agent（多 agent 并行开发模式，非单人手写）。
- 这是一篇 **CHI 2027 full paper** 的工程实现，tech-heavy HCI 工作。
- 今天 **2026-07-30**；deadline **≈ 2026-09-10 AoE**，无延期、**只剩 6 周**。
- 你的第一周（W1, 7/30–8/6）**只做一个 kill test**（见 §4），不是搭全系统。W1 是真 gate，不是里程碑。

---

## 1. 唯一权威文档（开工前必读第一件事）

路径：`docs/A2UI_开工大纲_v0_心智模型对齐版.md`（**文件名仍带 v0 字样，但内容已是"锁定版"**，文内标题已改为"TaskVM 开工大纲（单一权威文档·锁定版）"）。

这是**唯一**开工基础。15+1 个开放问题已全部拍板。任何冲突以此文档为准：
- 早期材料 `docs/oracle/` 下的 5 份原始 txt（HCI-UI 总纲 / GenUI 调研 / 与 DuetUI 差异 / 与 SaC+Sidekick 差异 / CHI 工作计划）是**只读参考**，不是决策源。
- 4 篇竞品论文 tex 在 `docs/references/`（DuetUI / SaC / Sidekick / Macaron-A2UI）+ AOHP 技术报告。

---

## 2. 一句话心智模型 + 四锚点（不能丢的先验 #1）

**人操作任务，Agent 操作应用。** Agent 把多个正在运行的**既有应用**的实时状态，反向编译成一个可编辑、可执行、可验证的任务界面；用户改一个任务变量，Agent 把改动可靠写回多个真实应用，独立 verifier 读 ground-truth 判定"改的发生、没改的不动、界面重新同步"。

四锚点（**四者同时存在**才与 DuetUI 拉开距离，缺一不可）：
`existing applications` / `live state` / `executable binding` / `round-trip verification`。

冻结 RQ：*Can an agent compile live, fragmented application state into an executable task-specific interface that users can directly manipulate with verifiable cross-application effects?*

---

## 3. 你要搭什么（核心构念 + 架构骨架）

**一等贡献 = 可执行投影保真性（Executable Projection Fidelity）本身**——"任务界面是多个真实应用状态的忠实、可执行、可验证的投影"这件事。**不是**安全压缩前沿（SCF 已降级为衍生贡献，见 §6）。

**主闭环**：`UI Agent ↔ Shared Execution State ↔ 生成式 UI`，人位于循环之外、作为**任务起始时的主动 milestone/checkpoint 设定者**（见 §6 地雷）。Shared Execution State 保存目标/计划/已执行动作/真实结果/当前应用状态/artifact/失败重试/风险/待确认操作——**不是聊天历史**。

**八步往返**：Observe → Abstract → Project → Manipulate → Compile → Execute → Verify → Reconcile。

**架构骨架**（目录树，W1 只动加粗部分）：
```
taskvm/
├── apps/                  # 自建可重置 Web 应用（sqlite 后端，复用 SenseAct engine 模式）
│   ├── calendar/  taskboard/  drive/
│   │   └── engine/        # reward.py(判成功) / injector.py(初始状态+可选外部并发注入) / *_db.py
│   └── _heldout/          # held-out 黑盒 app（OOD；对模型黑盒，对 verifier 白盒 via state adapter）
├── harness/               # browser_controller(Playwright) / state_adapter(reset·seed·read-canonical) / trace_capture / replay_engine / shadow_txn
├── task_state/            # representation / compiler(Apps→TaskWorld) / entity_binding / dependency_graph / projection_policy(规则/启发式，不追求 Pareto)
├── execution/             # patch_compiler(编辑→semantic patch) / replanner / action_dispatcher(GUI/MCP/API hybrid)
├── verifier/              # app_state_checks / cross_app_checks / non_interference / round_trip_checks / reconciliation
├── workspace_ui/          # renderer / editable_components / live_sync（先结构化文本/表单，不追求花哨）
├── benchmark/             # task_templates(40) / initial_states(隐藏 canonical graph) / user_edits / ood_splits / live_runs；API 成本追踪复用 SenseAct cost_model.py
├── baselines/             # 规则/类型匹配·prompt-only·frontier+shadow·人工 binding 上界·规则+critic
├── user_study/            # 4 条件
└── evaluation/  docker-compose.yml  README.md
```
**W1 只动**：`apps/{calendar,taskboard}` + `harness/{state_adapter,replay_engine,trace_capture}` + `task_state/{representation,compiler}` + `verifier/round_trip_checks` + `workspace_ui/renderer`。**先 replay-mode**，跑通 compiler→UI→patch→执行→verifier 整条链。

**仓库命名**：目录建议 `taskvm/`；若迁移成本高，保留 `a2ui/` 作仓库目录但内部模块/类名统一 `TaskVM` 前缀。**项目代号已从 A2UI 更名为 TaskVM**（见 §6）。

---

## 4. W1 唯一目标：kill test（gate，不是里程碑）

```
2 个 Web 应用（Calendar + TaskBoard）
→ Agent 在线执行（frontier CUA API，不训练）
→ 实时任务界面（先结构化文本/简单表单，不追求花哨）
→ 用户修改一个任务变量（如：发布日期 8/14 → 8/18）
→ Agent 跨应用落实（Calendar 会议移动 + TaskBoard 依赖 deadline 同步）
→ 独立 verifier 用隐藏 canonical state 判定：
   ✓ 改的发生（两 app 都改对）  ✓ 没改的不动  ✓ 界面重新同步
```
**三个 sub-kill（任一触发即调整方向）**：
1. round-trip 跑不通 → 立即收缩到方向二（少量 typed cross-app operators），不加更多 UI/模型；
2. 规则系统在 tool/app OOD 上已和模型一样好 → 删除训练；
3. 只有"每个任务手写 React 页 + binding"才跑得通 → 停（那是定制 dashboard，不是 software compiler）。

**W1 硬约束**：不训练、不上 OSWorld、不接真实商业账户、不做花哨 UI、先 replay-mode 跑通整条链再小规模 live。

---

## 5. 你的第一个动作（立即执行）

进入 **plan 模式**，产出 W1 具体代码方案，覆盖这 6 项，方案给用户确认后再动手写代码：
1. **仓库骨架**：§3 目录树，至少初始化 W1 涉及的子目录 + `docker-compose.yml` + `README.md`。
2. **Calendar + TaskBoard 两个极简可重置 Web 应用接口**：sqlite 后端；复用 SenseAct `scenarios/<name>/engine/{reward,injector,*_db}.py` 模式；接口含 `reset()/seed()/read-canonical-state()`。
3. **canonical task graph 的隐藏与读取机制**：verifier 用隐藏 canonical state 作 GT；模型推理时只见 screenshot/DOM/a11y/tool schema/trajectory，**永不接触 DB**。
4. **replay 引擎**：用记录的 screenshot/DOM/action/state-diff 重放，开发期快速调试 + 大规模离线 benchmark。
5. **compiler/binding 的 frontier-API 调用契约**：调 GPT-5.6-sol / Claude-Sonnet-5 类前沿通用模型；把 **A2UI v0.8 协议完整 spec**（4 种消息类型 `surfaceUpdate`/`dataModelUpdate`/`beginRendering`/`deleteSurface` 的 schema，几千 token）直接注入 system prompt（这是 Macaron 论文自己对标的 full-prompt baseline 做法，**不需要 skill 机制、不下载 Macaron 模型**）；输入 = task/screenshot/DOM/a11y/trajectory/tool schema → 输出 = typed task-state graph + binding。
6. **verifier 的 round-trip 判定逻辑**：改的发生 / 没改的不动 / 界面重新同步。

---

## 6. 不能丢的先验 / 地雷（这些若丢了会返工）

> 以下每条都是已拍板决策或已核实事实，**不要重新打开**。

1. **四锚点同时存在**才与 DuetUI 拉开距离——`existing applications` / `live state` / `executable binding` / `round-trip verification`。少一个就被竞品吸收。
2. **verifier 永远来自环境状态**（sandbox 隐藏 canonical state），**绝不让生成 binding 的模型自评**。这是核心诚实性约束。
3. **不部署 Macaron 已训练模型（Grande/Venti）**。主线 = 前沿通用模型（GPT-5.6-sol / Claude-Sonnet-5）+ A2UI v0.8 协议 spec 注入 system prompt。不下载模型 → 不算复用其训练产物 → 不 pin 死在 A2UI v0.8（协议升级只换 prompt 文本）。
4. **两个模型角色独立**：(1) Agentic UI 生成模型（多 app 状态→任务界面）；(2) compute-use 执行模型（在真实 App 里操作）。即使同用一家厂商的模型，也是**两次独立调用、不共享 context**。
5. **SCF 已降级为衍生贡献**。一等贡献 = 可执行投影本身。`projection_policy` 模块**保留**在架构里但用**规则/启发式**实现，**不跑 Pareto 实验**；SCF 完整三轴测量与 Pareto 写入 Discussion / Future Work。**不要**为 SCF 搭策略学习模块挤占 W1 kill test 资源。
6. **demo 开场必须用"操纵+写回+verifier"弧**（权威文档 §4.3 的 4 步：拖日期→真实 app 同步改→verifier 非干涉确认→外部改 Jira 触发 reconciliation 标红）。**绝不用"状态仪表盘"开场**（"四 agent 进度条 + 暂停/查看日志/继续" = 跨应用版 Sidekick，死路）。
7. **人的角色 = "任务起始时的主动 milestone/checkpoint 设定者"**，**不是**"低频授权节点"。用户**特别讨厌**"人审核 Agent 结果、确认后才能继续"这种 Sidekick 式人工中制介入（原话："我特别讨厌这种人类中制介入的研究，因为它太老了"）。正确口径：人控制"要做到哪里"（事前设定目标深度/checkpoint），**不控制"怎么验证安全"**——安全性/边界检查全部下放给**自动 verifier**，不需要人在提交前做最终审核。论文措辞与 demo 设计都必须避免任何会被读成"人审核后才能继续"的句式。
8. **"统一信息实体"只是 `task_state` 数据结构的叙事包装**（"同一任务变量在不同 App 里的绑定关系集合"），**不是**独立于 task_state、本体论意义上更根本的新实体。**不建独立模块**，仅作 Discussion 叙事升华。否则会撞 SaC 的"the application itself becomes the interaction state"语言，与"real state 永远留在真实 App 里、surface 只是投影、绝非 source of truth"的核心防御论点矛盾。
9. **项目代号 = TaskVM**（不再用 A2UI 作代号）。`A2UI` 是 Google 2025 年发起的通用 agentic UI 声明式协议名（`a2ui.org` v0.8），Macaron-A2UI（COLM 2026）是 Mind Lab 团队的开源第三方工作，与本项目及本团队无归属关系、**不是本项目前作**。三者不要混淆。TaskVM 实现中仍可选择性复用 A2UI v0.8 协议作 UI 渲染层传输格式（见第 3 条），与更名不冲突。
10. **AOHP 是相邻工作、不撞车**（已全文核对，`docs/references/AOHP-paper/`）。AOHP 改 OS 内核/框架层、无 round-trip verification、personalization 是跨 App 偏好记忆而非任务变量绑定——三点均与我们正交。可借鉴其 **checkpoint-weighted completion rate** 评分方式（比二元成功/失败细粒度），用于 verifier 打分设计。
11. **benchmark 混合**：3 白盒自建（Calendar/TaskBoard/Drive，sqlite）+ 1 held-out 黑盒（对模型黑盒/对 verifier 白盒 via state adapter）。**held-out = 两者都要**：1 个真未见 app（验迁移）+ 已见 app 的 rename/reskin/schema 变体（验反捷径），分别报 OOD 指标。
12. **Mail app = out**（Drive 作第 3 个 app，Mail 永久 optional）。**benchmark 规模 = 40 模板 / 800 实例 / OOD 占 ~20%**。
13. **用户研究 = 4 条件**（后置非阻塞，自动评测是论文主体）：C0 原始多 app GUI / C1 静态只读聚合 dashboard / C2 chat agent + 全 app 工具访问（Claude/GPT + 3 app MCP 工具，真正 non-inferiority 对手）/ C3 我们的投影。**非劣性 margin = C3 成功率不低于 C2 的 5 个百分点**（实验前定死）。**N=18 within-subject**。IRB：若所在机构无正式 IRB，走轻量自述路径（自愿参与/知情同意/数据匿名化），W5 pilot + 后置正式，不阻塞投稿。
14. **reconciliation 机制 = re-read-on-action**（用户编辑/定时心跳触发重读）+ 冲突时**标红不静默覆盖**，给出"底层已变 / 你的编辑 / 合并选项"。**并发修改注入**：不建"协作者"角色，只在 `apps/<name>/engine/injector.py` 预留最简"benchmark 自己主动注入外部状态变更"接口（定时脚本或手动 trigger 直接向 DB insert 一条冲突事件），列为 **W4+ 可选加分项，非 W1-W3 核心路径**。
15. **跨设备 = appendix showcase**，不当贡献。**wind-tunnel（CUA 模拟用户）= 推迟，本期不建**。
16. **训练 = 诚实 Go/No-Go**（W4 决策点）：seen app prompt≈92% 但 renamed≈71%、unseen≈54%、错误集中在同名异义/隐含依赖时才训轻量 QLoRA critic（3B–7B，标签来自 cloned sandbox 真实 state diff，**非 LLM 自评**）。否则 train-free。**绝不为了 tech-heavy 硬训**。

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

## 8. 6 周排期（你按此推进）

| 周 | 日期 | 目标 | 必须完成的纵向切片 |
|---|---|---|---|
| **W1** | 7/30–8/6 | **Kill test**（唯一 gate） | 2 app（Calendar+TaskBoard）replay-mode + frontier CUA API；跑通 compiler→UI→patch→执行→verifier 整条链。不训练。跑不通即收缩方向二 |
| **W2** | 8/7–8/13 | 第 3 app + live 小规模 + 投影 UI | Drive + state adapter 泛化 + live-mode 小规模 + 结构化投影 UI（不花哨）+ stale-state 检测 |
| **W3** | 8/14–8/20 | **Benchmark + 基线 + 自动主实验**（论文主表来源） | 40 模板→800 实例 + 隐藏 canonical graph + 各 baseline（规则/prompt/人工 binding）+ 7 指标 overnight API 跑完 |
| **W4** | 8/21–8/27 | OOD + 签名实验 + 训练 Go/No-Go + 失败分析 | rename/reskin/unseen-app OOD split + app-substitution 不变性小版（2 stack × 2-3 app 当一个 figure）+ reconciliation demo + 训练决策（大概率 train-free）+ failure analysis |
| **W5** | 8/28–9/3 | 用户研究 + 论文主体并行 | 若 IRB 就绪：~12-18 人 within-subject 4 条件；并行写 Intro/RW/System/Benchmark/Eval |
| **W6** | 9/4–9/10 | 冻结 + 投稿 | 重跑终值 + 统计/可视化 + ablation + figure + demo 视频 + 匿名化 + 全文收敛 + 投稿 |

**最关键风险**：W1/W4 的 round-trip 可靠性。若 compiler 不能可靠把任务变量绑到正确的真实 app 对象、actuator 不能可靠写回——无论时间够不够，论文核心都站不住。**W6 风格功能冻结**：禁止新增 OSWorld 全量/记忆/长期 history/UI 风格生成/多任务族/通用 Agent OS/训练（除非 W4 看到 OOD gap）。

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

- [ ] 先读 `docs/A2UI_开工大纲_v0_心智模型对齐版.md`（唯一权威文档，锁定版）。
- [ ] 项目代号用 **TaskVM**（不用 A2UI 作代号）。
- [ ] 这是**独立项目**，不拖入其他仓库（SenseAct 仅作工程模式参考，路径见 §7，不混入代码）。
- [ ] 自动评测为主体、用户研究后置不阻塞。
- [ ] 训练是 Go/No-Go 而非默认；不部署 Macaron 已训练模型。
- [ ] 主线用前沿通用模型 + A2UI v0.8 协议 spec 注入 prompt；两个模型角色独立。
- [ ] demo 开场用"操纵+写回+verifier"弧（§4.3），绝不用状态仪表盘开场。
- [ ] 人的角色 = 任务起始时的主动 milestone 设定者，不是低频授权节点；安全性下放给自动 verifier。
- [ ] 进入 plan 模式，产出 W1 代码方案（§5 的 6 项），给用户确认后再动手写代码。
