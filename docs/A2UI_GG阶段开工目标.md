# TaskVM GG 阶段开工目标（终极版）：删除一切内部数据库暴露，交付面向开放场景真正可用的 TaskVM Harness

> **本文档是 GG 阶段的唯一权威交接文档**。coding agent 开工前必须通读 `.mrules` 全文（尤其 E7/E8/E10/E11/E16）+ `docs/A2UI_开工大纲_v0_心智模型对齐版.md`（下称"大纲"，本文件所有章节引用均锚定大纲）+ 本文件全文。
> **用户的原话目标（一字不改的宗旨）**：交付一个面向开放场景真正能用的 TaskVM APP 封装，让用户用得很爽。用户拿着真实手机（或浏览器），系统先初始化形成一个大致的 workflow；用户在 workflow 上设定"执行到哪一步"；系统自治执行、不停，直到用户强制暂停；暂停后用户拖拽进度条回退；系统用 generator 生成新的 subgoal instruction 供用户随意调整；调不回去的诚实呈现"不可回退"。线性（Sequential）、树状（Parallel）、环状（Loop）三种 workflow 全部支持上述闭环。**所有不为这个目的服务的东西，全部删掉。**
> **本阶段只有"真做完/没做完"两种状态，不存在"看起来做完了"**（大纲 §8 原话）。每个 GG.X 完成即落盘 `eval_results/*.json` 证据 + 立即 `git commit`（message 格式 `GG.X: <描述>`）。

---

## § 0. 唯一红线：内部信息零暴露判定标准（GG 阶段一切工作的最高判据）

大纲 §5.1 已有的判定标准是"写/回退必须过浏览器真实手势"。GG 阶段在其上再加一条**同等优先级、写死、任何审计/实现都必须遵守**的红线：

> **【GG 红线】一条信息，如果不能被"一个拿着真实设备、看着渲染出来的屏幕的真人用户"用肉眼直接看到，它就禁止进入任何模型的任何输入（观测、instruction、prompt、上下文），无论这条信息看起来多"方便定位"。**
>
> 判定口令（对每个往模型输入里放信息的地方逐条问）：*"这个字符串/字段/ID/坐标，真实用户能在屏幕上看到吗？"* 看不到 → 禁止。
>
> **推论 1**：`entity_id`（E1/T1/F1/M1/wxid_xxx/p_xxx/chat_id）是数据库主键，真实软件的屏幕上永远不会渲染它 → **禁止出现在任何模型输入里**。
> **推论 2**：`data-eid`/`data-task-id`/`data-post-id` 等 DOM 属性是把数据库主键贴到页面上 → 真实软件没有这种东西 → **从渲染模板里删除**。
> **推论 3**：`get_state()`/`read_canonical()`/`posts.json`/zustand store 是应用的内部状态 → 禁止被读出来翻译进 instruction 或观测 → **只能存在于 verifier/seed/rollback_log 等 harness 内部控制面，其产物永远不进模型**。
> **推论 4**：用内部 ID 拼深链 URL（`/edit/<entity_id>`、`openApp('/chat/{wxid}')`）直接跳转 → 真实用户做不到 → **执行路径里禁止，导航必须从 app 入口页靠点击完成**。
> **推论 5**：title/name/subject/可见正文/可见颜色/可见位置 → 真实用户能看到 → **允许**，且这是 GG 阶段唯一的合法定位方式。

**三层权限模型（GG 阶段必须刻进每个模块的 docstring）**：

| 层 | 允许看到什么 | 反例（禁止） |
|---|---|---|
| **模型可见层**（观测 + instruction + 一切 prompt） | 渲染像素、屏幕上肉眼可见的文本/颜色/位置 | entity_id、data-* 属性、get_state 产物、posts.json、operator 内部词汇表 |
| **Harness 控制层**（verifier / seed / inject / rollback_log / 重试决策） | canonical state（它是 hidden GT，大纲 §12.2 要求 verifier 必须读它） | 把 canonical 内容翻译进模型输入 |
| **翻译层**（GG 新增，见 GG.3） | 把 server 侧的 entity_id **翻译**成屏幕上可见的 title/name，翻译结果（可见文本）才允许进模型 | 把 entity_id 本身透传进模型 |

**.mrules 豁免条款**：`.mrules` 是历史纠错记录（E1-E20 的教训传承），**不是设计文档也不是运行代码，不许删**。要删的是"运行路径 + 观测路径 + 指令路径 + 设计文档里的内部-ID 依赖描述"。

---

## § 1. 全仓库泄露清单（2026-08-13 审计取证，file:line 级，GG.1-GG.4 逐条清除）

> 以下每一项都已用 grep/读码独立核实。coding agent 按此清单逐条删除/改写，**删一条验证一条**（L0 import 不破）。

### 1.1 渲染层（最根源：自建 app 把数据库主键渲染成了可见列 + data-* 属性）

真实软件（Google Calendar/Notion/微信）的屏幕上**没有**"数据库 ID"这一列。我们的自建 app 却把它渲染出来了——这是所有后续泄露的总源头（截图/a11y/DOM 观测全部因此被污染）。

| 文件:行 | 泄露内容 | 处置 |
|---|---|---|
| `taskvm/apps/calendar/templates/calendar.html:14` | `<tr class="event-row" data-event-id="{{ e.eid }}" data-date=...>` + 可见 EID 列 | 删 `data-event-id` 属性 + 删整个 EID 可见列 |
| `taskvm/apps/calendar/templates/event_detail.html:8`、`event_edit.html:8` | `data-event-id="{{ event.eid }}"` + `<dt>EID</dt>` 可见字段 | 删属性 + 删可见 EID 字段 |
| `taskvm/apps/taskboard/templates/taskboard.html:14-15` | `data-task-id="{{ t.tid }}" data-deadline data-status` + 可见 TID 列 | 同上 |
| `taskvm/apps/taskboard/templates/task_detail.html:8,11`、`task_edit.html:8` | `data-task-id` + 可见 TID 字段 | 同上 |
| `taskvm/apps/drive/templates/drive.html:14-15` | `data-file-id="{{ f.fid }}" data-parent data-owner` + 可见 FID 列 | 同上 |
| `taskvm/apps/drive/templates/file_detail.html:8,11`、`file_edit.html:8` | `data-file-id` + 可见 FID 字段 | 同上 |
| `taskvm/apps/mail/templates/mail.html:15`、`message_detail.html:11` | 可见 MID 列/字段（`<code>{{ m.mid }}</code>`） | 删可见 MID 列/字段 |
| `taskvm/apps/outlook_cal/templates/outlook_cal.html:15` | 可见 AID 列 | 删可见 AID 列 |
| `taskvm/apps/*/templates/base.html:17-18`（各 app） | `tr.xxx-row[data-xxx-id]:hover` CSS 选择器 | 改为不带属性的选择器（如 `tr.event-row:hover`） |
| `taskvm/apps/*/app.py` 各文件 docstring（如 `calendar/app.py:12` "data-event-id DOM"） | 设计文档级记录 | 改写 docstring，删除 data-id 依赖描述 |
| `taskvm/apps/drive/templates/file_edit.html:30`、`mail/templates/message_edit.html:37` | `data-var="publish_date"`/`data-var="send_date"` | 评估：`data-var` 是字段名不是实体 ID，但同样不是真实软件会有的属性 → 删 |
| `taskvm/workspace_ui/editable_components.py:150-180, 264-280` | saga 进度条/undo 界面/冲突标记直接把 `calendar.E1.date`、`taskboard.T2.deadline` 这种**内部路径字符串**显示给用户 | 改为显示"app 名 + 可见 title + 字段中文名"（harness 侧翻译，见 GG.3 翻译层） |

### 1.2 观测层（喂给 compiler 的 a11y/DOM 文本含内部 ID）

| 文件:行 | 泄露内容 | 处置 |
|---|---|---|
| `taskvm/harness/replay_engine.py:146-153` `synthesize_a11y` | `parts = [f'[bid={eid}] {kind}']` —— **把 entity_id 直接写进喂给 compiler 的 a11y 文本** | 删 `[bid=...]`；实体在观测里只由可见字段表示（title/name/subject…） |
| `taskvm/harness/replay_engine.py:132-139` `_A11Y_FIELDS` | 含 `peer_wxid`（微信内部 ID） | 删 `peer_wxid`（保留 `peer_name`——那是可见的联系人名） |
| `taskvm/harness/replay_engine.py:125-131` docstring | "byte-identical regression guard" 注释里记录的字段依赖 | 改写；回归基线必须重跑重建（GG.6），旧 byte-identical 基线作废 |
| DOM 观测解析器（`parse_dom_entities*` 系列） | 若靠 `data-*-id` 属性切分实体 | 改为靠行/卡片结构切分，实体键用可见 title（harness 内部映射见 GG.3） |

### 1.3 指令层（喂给执行模型的 instruction 含内部 ID + 硬编码 if/elif）

| 文件:行 | 泄露内容 | 处置 |
|---|---|---|
| `taskvm/execution/gui_executor.py:68-103` `_build_instruction` | 第 84 行 `f"...the {entity_kind} with id '{entity_id}'..."` —— instruction 直接含数据库主键；整段 if/attempt 硬编码模板 | **整个函数删除**，由 GG.3 的 LLM SubgoalGenerator 取代 |
| `taskvm/governance/governance_interpreter.py:379-429` `_build_edit_nl` | 全部 operator if/elif 硬编码分支；`move_event`/`set_deadline` 等模板直接拼 `{eid}`；`send_message` 分支 `contact_name or f"chat {eid}"` 泄露 wxid；未见 operator 走 generic fallback（零泛化） | **整个函数删除**，由 GG.3 的 LLM SubgoalGenerator 取代 |
| `taskvm/governance/governance_interpreter.py:216-217` loop 模板 NL | `f"Loop template — for each entity in {values}..."`（values 是 entity_id 列表） | 删除，loop 指令同样走 LLM 生成（loop_values 的实体先翻译成可见 title 列表） |
| `taskvm/governance/governance_interpreter.py:239-240, 258-259, 269-270, 332-338` | set_milestone/rollback_to 模板 NL（含 checkpoint id + saga id 字符串） | checkpoint id（C0/C1）是 governance 层用户可见概念，可保留；saga_id 是内部事务 ID → 从 NL 里删除，只保留在 meta |
| `taskvm/substrate/mobilegym/bridge.py:~636-668`（inline f-string instruction 路径） | X toggle 的 inline f-string 模板（E16 已清空 content_hint，但模板本身仍是硬编码 f-string） | 与 GG.3 统一：bridge 内不再有自己的 instruction 模板，一律由 governance/SubgoalGenerator 经 `instruction_override` 传入；bridge 只保留"无 instruction 时拒绝执行并报错"的诚实路径 |
| `taskvm/substrate/mobilegym/bridge.py:~604-622` ablation 注释块 | E14-core/E16 的 ablation 历史注释（提及 posts.json/content_hint 后门史） | 删除代码里的历史注释（历史留在 .mrules，代码不留） |

### 1.4 执行层（动作路径里的内部-ID 后门）

| 文件:行 | 泄露内容 | 处置 |
|---|---|---|
| `taskvm/substrate/base.py:131` `resume_url = self._edit_form_url(sid, entity_id)` + `:157` `_edit_form_url` 方法 + `taskvm/execution/gui_executor.py:220,228,235,239-242,344,357,373` 的 `resume_url` 参数链 | 用内部 ID 拼出编辑表单 URL 直接 `goto`，绕过 UI 导航——真实用户做不到 | **全链删除**。重试改为"不跳转，从当前卡住页面继续"（`prev_screenshot` 保留——它是观测截图不是内部状态，合法） |
| `taskvm/substrate/mobilegym/bridge.py:~776-835` `_send_message` 深链 | `window.__OS__?.openApp?.('wechat', '/chat/{chat_id}')` 用 wxid 深链进聊天 | 删除深链；导航改为真实手势序列（打开微信 → 在聊天列表里**按联系人可见名字**点击目标聊天）。找不到就诚实 fail |
| `taskvm/execution/gui_executor_async.py`（MobileGym 侧） | 若存在同样的 entity_id instruction/深链逻辑 | 同步清除（与 sync 版同标准） |

### 1.5 Harness 控制层（**保留**，但必须加 docstring 划界，防未来再被误用进模型）

以下**不删**——它们是 verifier/seed/rollback 的合法 GT 通道（大纲 §12.2："verifier 永远来自环境状态"），但必须在每个入口处加一行 `# CONTROL-PLANE ONLY: 产物禁止进入任何模型输入（GG 红线 §0）`：

- `taskvm/substrate/base.py:104,133` `_mutate_via_gui` 手势前后 `read_canonical`（写后验证+重试决策——harness 控制流，合法）
- `taskvm/substrate/mobilegym/bridge.py` 全部 `env.get_state()`（verifier 等价物）
- `taskvm/harness/replay_engine.py::capture_obs`（截图 + DOM 文本——但产出的 a11y 必须过 1.2 的清洗）
- `rollback_log`、`seed`/`inject_task`、`check_round_trip` 全部 verifier 侧代码

---

## § 2. 目标架构（GG 阶段完成后的唯一合法形态，锚定大纲 §5.1 主链路）

大纲 §5.1 的五箭头主链路不变，GG 阶段把每一环的"内部信息依赖"全部切除：

```
真实 app 渲染画面（截图 + 清洗后的可见文本观测）          ← ① 编码器（compile_binding，已是真模型，保留）
        │  观测里只有肉眼可见内容，无任何内部 ID
        ▼
   VM-state（var_id → app.entity.field via operator）     ← entity_id 只活在 server 侧，永不进模型
        │
        ▼
   ② 解码器 GenUI（genui_decoder，已是真模型，保留）  →  人可操纵两区界面
        │                                                     （provenance 显示"app+可见标题"，不显示内部路径）
        ▼
   ③ 人操作（编辑字段 / 拖进度条 / 设 checkpoint / 强制暂停）
        ▼
   ④a 翻译层【GG 新增核心】：entity_id → 可见定位符（visible locator）
        harness 用 canonical state 查 title/name（合法，因为 title 渲染在屏幕上），
        产出"标题为'项目发布会议'的会议"这种肉眼可验证的定位描述
        ▼
   ④b SubgoalGenerator【GG 新增核心，LLM 调用】：
        输入 = 用户意图 patch + visible locator + app 名 + 目标值 + （TTS：可选一轮自我精化）
        输出 = 目标级自然语言 subgoal instruction（零内部 ID、零 operator 黑话、零硬编码模板）
        ▼
   ④c GUI 执行器（gui_executor/gui_executor_async，保留 grounding 循环）
        输入 = 截图 + ④b 的 NL instruction；导航/定位/点击全靠视觉
        找不到 → 诚实 fail；不可回退 → 诚实 409（E7/E9 原则不变）
        ▼
   ⑤ 独立 verifier（保留，原样）读 canonical state 判定 + 负对照 ≤0.3
```

**这个架构对开放场景的泛化逻辑**：换一个从未见过的 app/substrate 时，① 编码器从渲染观测发现 binding（模型能力，已验证有泛化性）；④a 翻译层查到的 title 天然是该 app 屏幕上渲染的东西；④b 的 LLM 生成不依赖任何预写模板；④c 的 grounding 是纯视觉。整条链没有任何一环需要"提前见过这个 app"。

---

## § 3. GG 任务包（按依赖顺序，每个包独立验收、独立 commit）

### GG.1 — 渲染层清洗（§1.1 清单全清）

按 §1.1 表格逐条删除 5 个自建 app 模板里的内部 ID 可见列 + data-* 属性 + 相关 CSS/docstring。
**同步必须做**：`taskvm/verifier/` 和 adapter 的 DOM 解析若依赖 data-id 属性切分实体，改为结构解析（行/卡片）+ 可见 title 键。**这是本包最大的隐藏工作量，必须先读 `replay_engine.py` 的 `parse_dom_entities*` 确认解析逻辑再动手。**
**验收**：用 Playwright 打开 5 个 app 各自截图，肉眼检查截图里没有任何 `E1/T1/F1/M1/A1/wxid_*` 字样；`grep -rn "data-eid\|data-task-id\|data-file-id\|data-event-id\|data-post-id\|data-var" taskvm/apps/` 返回 0 行；L0 `pytest tests/test_imports.py` 全绿。落盘 `eval_results/gg1_render_purge_<ts>/`（5 张截图 + grep 结果 txt）。

### GG.2 — 观测层清洗（§1.2 清单全清）

`synthesize_a11y` 删 `[bid=...]`；`_A11Y_FIELDS` 删 `peer_wxid`；解析器去 data-id 依赖。
**验收**：对 `release_reschedule` 跑一次 `capture_obs`，把喂给 compiler 的完整 a11y 文本落盘，人工逐行确认无内部 ID；`grep -rn "bid=\|peer_wxid" taskvm/harness/` 返回 0 行。落盘 `eval_results/gg2_observation_purge_<ts>.json`（含完整观测文本原文，供任何人复核）。

### GG.3 — 翻译层 + LLM SubgoalGenerator（本阶段技术核心，废除全部 if/elif 模板）

1. 新建可见定位翻译：给定 `(app, entity_id)`，harness 从 canonical state（控制层，合法）读出该实体的可见字段（title/name/subject/peer_name，按 app 的 kind 选优先级），产出 `visible_locator: str`（如 `标题为"项目发布会议"的会议`）。**读不出可见字段时诚实返回 None，下游生成"无法定位"的诚实 fail，禁止回退到拼 entity_id。**
2. 删除 `_build_edit_nl`（governance_interpreter.py:379-429）和 `_build_instruction`（gui_executor.py:68-103）**两个完整函数及其全部 if/elif 分支**。
3. 新建 `taskvm/governance/subgoal_generator.py`：`generate_subgoal(intent_patch, visible_locator, app, field_display, target_value, *, n_candidates=2) -> str`，LLM 调用（gpt-5.6-sol，`complete_json`，非 vision）。TTS 轻量版：生成 2 个候选 → 用一次轻量 LLM 调用或规则（更长、含 visible_locator 原文、含目标值原文）选更具体的那个。**禁止 roll 超过 2-3 次**（用户原话约束）。
4. 接线：`GovernanceInterpreter._interpret_edit_field`/`_interpret_loop_field` 改调 SubgoalGenerator；`gui_write`/`gui_write_async` 的 instruction 参数改为**必须由调用方传入**（删除函数内自建 instruction 的能力——从类型签名上封死旧路径）；MobileGym bridge 删除 inline f-string 路径，无 `instruction_override` 时诚实报错。
5. **no-leak 单元测试**：对每个任务跑一次生成，断言产出 NL 不含任何 `entity_id` 正则匹配（`E\d+|T\d+|F\d+|M\d+|A\d+|wxid_\w+|p_\d+`）、不含 operator 黑话（`move_event|set_deadline|toggle_like|...`）。
**验收**：6 个任务 × 每个任务全部 PatchOp 生成 subgoal，落盘 `eval_results/gg3_subgoal_samples_<ts>.json`（含 draft/refined 两版 + 选用理由 + no-leak 断言结果），人工可读确认"每条指令拿给一个不知道任何内部 ID 的人看，他能在页面上找到目标"。

### GG.4 — 执行层去深链（§1.4 清单全清）

删 `_edit_form_url`/`resume_url` 全链；wechat 深链改真实手势导航；重试改为原地继续。
**验收**：`grep -rn "resume_url\|_edit_form_url\|openApp" taskvm/` 返回 0 行（`open_app` 手势本身保留——那是合法 UI 操作）；跑 GG.6 的最小 killtest 确认重试路径仍工作。落盘 grep 结果 + killtest JSON。

### GG.5 — 工作流运行时闭环（用户本次的核心新需求：初始化→自治→强制暂停→拖拽回退→诚实卡死）

这是"用户拿真实手机用"的完整体验闭环，三种 workflow（Sequential/Parallel/Loop，FF.4 已有执行器）全部接入：

1. **初始化**：seed 时 LLM 建议 checkpoint（FF.3 已有）+ 按任务生成 WorkflowPlan（FF.4 已有）→ 前端渲染对应的工作流动画（FF.5 已有三种渲染器）。
2. **自治执行**：新增"开始执行"按钮 → server 驱动 WorkflowExecutor 自动推进各节点，SSE 实时推 `workflow_progress`（FF.5 已有通道），**中途不停**。
3. **强制暂停**：新增 `POST /<sid>/pause` + 前端暂停按钮 → 执行循环在当前节点完成后停下（不杀进行中的单个 GUI 手势——诚实边界：手势不可半截取消），状态落 `sess.paused`。
4. **拖拽回退**：timeline 进度条拖到某 checkpoint（timeline.js 已有）→ `rollback_to` 事件 → `undo_saga`（已有）→ 每个 saga step 真实走 GUI 回退手势；找不到撤销路径的操作 → 诚实 409 → 进度条该刻度锁死标红 + shake（FF.1 G 动画已有）。
5. **回退后再生成**：回退完成后，用户可以在新状态上继续编辑 → 走 GG.3 的 SubgoalGenerator 生成新 instruction → 继续自治执行。
6. **诚实卡死呈现**：`partial_failure=True` 的 saga step 在前端显示为"🔒 此步不可回退"（editable_components 已有骨架），且文案改为用户语言（"这条微信消息已发出，无法撤回"），不显示内部路径。
**验收**：三种 workflow 各跑一次完整闭环 live demo（gui_agent 模式），每种的 `eval_results/gg5_workflow_loop_<type>_<ts>.json` 含：初始化 checkpoint 列表 / 自治执行每节点时间戳 / 暂停响应 / 回退的逐步 saga 结果 / 锁死刻度的 UI 截图（PNG）。**loop 工作流的每次迭代独立 verify（E11 原则，FF.4 已实现，GG 阶段在 live 模式下复核）。**

### GG.6 — 开放场景泛化 killtest（本阶段的终极验收，证明"未见场景能用"）

这是回答"还是不是我们最终的原型实现"的唯一证据：

1. **新 killtest `taskvm/evaluation/run_open_world_killtest.py`**：对一个**harness 从未硬编码过的任务**跑完整链路——观测→binding 发现→workflow 初始化→subgoal 生成→GUI 执行→verifier。
   - 测试任务设计原则：operator 词汇表里**故意用一个新的 operator 名**（如 `reschedule_appointment` 之外再 invent 一个 `update_rsvp`），fixture 对该 operator 没有任何预写模板——GG.3 之后系统应该照样工作（因为指令是 LLM 现场生成的）。
   - 可用 `outlook_cal` 的 reskin 变体或 MobileGym 上一个未接入过的 app（如 Reddit——它有 deleteChatMessage，可顺便测"真实可逆"路径）。
2. **no-leak 静态门**：killtest 内嵌断言——整个运行过程中所有进模型的字符串（观测文本 + 全部 instruction）过内部-ID 正则扫描，发现一处即 FAIL 并落盘证据。
3. **全量回归**：`run_w1_killtest --mock`（L1）+ gui_agent 模式 4 任务 × 2 samples（继承 FF.7 标准）+ GG.5 三种 workflow 闭环。
**验收**：`eval_results/open_world_killtest_<ts>.json` 含 round_trip/binding_f1/no_leak_gate/neg_control 全字段；**verdict 必须是真实结果——FAIL 也如实落盘**（E3/E11 原则）。

---

## § 4. Harness 性能优化（写进 GG 规划，与清洗并行做）

1. **GUI 调用成本**：E13 实测 ~16 calls/op 的根因是重试重走全流程。GG.4 删掉深链 resume 后，重试改为"原地继续 + prev_screenshot"，预期 16→8-10 calls/op（E13 Task2 的诚实结论保留：若模型 form-confirm 仍不可靠导致超标，如实记录，不粉饰）。
2. **SubgoalGenerator 的 TTS 成本**：每个 PatchOp 多 1-2 次文本 LLM 调用（非 vision，便宜）。但**同一次用户编辑产生的多 app fanout ops 可以合并成一次 LLM 调用批量生成**（一次调用返回 N 条 instruction 的 JSON 数组），把成本从 O(ops×2) 降到 O(2)。
3. **Parallel 工作流的真实并行**：FF.4 已记录 gui_agent 模式下单浏览器 serialize。GG 阶段：为 PARALLEL 节点按 app 起多个 BrowserController 实例（每 app 一个 resident 页面），实现真并行；内存成本可接受。**若 Playwright 多实例在本机环境不稳定，诚实降级回 serialize 并记录原因。**
4. **GenUI 渲染缓存**：EE.7 遗留——每次 render 都调 GenUI 模型。GG 阶段加 VM-state 哈希缓存：state 未变则复用上次 decode 结果，SSE conflict 触发时才重新 decode。

---

## § 5. 开工前基线（每次新会话必须先跑，全绿才开工）

```bash
PYTHON=/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/conda/envs/taskvm/bin/python
cd /mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui
$PYTHON -m pytest tests/test_imports.py -v          # L0: 当前 30+ passed
$PYTHON -m taskvm.evaluation.run_w1_killtest --mock # L1: 4/4 PASS
```

分支：从 `ee-phase` 切 `gg-phase`（`git checkout -b gg-phase`），每个 GG.X 一个 commit。**注意工作区当前有未提交的删除（`run_ood_recon.py` 等 4 个 evaluation 脚本被删）——开工前先 `git status` 核实这些删除是否有意，无意则恢复，有意则单独 commit 说明原因。**

---

## § 6. 执行纪律（继承 .mrules E1-E20，GG 阶段逐条有效）

1. **每个数字必须落盘 `eval_results/*.json`**，口头汇报不算数（E8）。
2. **发现 FAIL 如实落盘 FAIL**，不用 mock/简化路径的数字掩盖（E11）。
3. **每完成一个 GG.X 立即 commit**，不攒大 commit；commit 前 `git status` 确认没把 `eval_results/`/截图 staged（强制规则 2/4）。
4. **多修复必须分阶段验证**，禁止打包后只报最终数字（E14-core 教训：消融或逐包落盘）。
5. **往模型输入里放任何信息前，先问 §0 判定口令**（E16 规则的 GG 升级版）。
6. **verifier 永远读 canonical state，绝不让生成方自评**（大纲 §12.2）。
7. **写/回退必须过浏览器真实手势**（大纲 §5.1 判定标准，E10）。
8. **被问到 A 时主动检查同类问题是否也在 B/C/D**（E10 元原则）——本轮内部 ID 问题就是这句话的又一次应验：E16 清了 MobileGym 的 posts.json，但没人主动查自建 app 的 `data-eid` 和 instruction 层。
9. **GG 阶段结束时必须把结果写回 `.mrules`（新增 E22）**，含：删了什么、验证证据 JSON 路径、已知残留局限。
