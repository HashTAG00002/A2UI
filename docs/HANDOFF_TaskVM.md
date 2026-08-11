# TaskVM 全链路返工交接 prompt（GUI Agent 执行器 + GenUI 解码器，2026-08-11）

> 你是 TaskVM 项目的 coding agent。这是一份**任务量很大、允许你长时间自主连续执行**的交接——用户明确说"任务量可以很大，跑上我就去睡觉了"，意味着你不需要每完成一小步就等待确认，也不需要因为任务大而提前缩小范围。**但每一个任务包完成后，必须落盘可复核的证据，绝不接受纯文字汇报"已完成"**（这是本项目历史上反复踩过的坑，见 `.mrules` E8/E9）。开工前必须先完整读一遍 `.mrules`（尤其 E10）和 `docs/A2UI_开工大纲_v0_心智模型对齐版.md` 的 §5/§7/§8/§12/§13。

---

## 0. 你要解决的根本问题（不要跳过，这决定了你后面每一行代码怎么写）

`.mrules` E10 审计发现：TaskVM 项目从诞生第一天起，架构图里最核心的两个模型角色——**GUI Agent 执行器**（写/回退）和 **GenUI 解码器**——从未被真正实现过。现状：

- **写操作**（`taskvm/harness/state_adapter.py` 里每个 `*Adapter.mutate`）：全部是 `requests.post(内部 Flask JSON API)`，从未打开浏览器、从未渲染页面、从未模拟一次点击/输入。
- **回退操作**（`taskvm/execution/rollback.py`）：把 `before` 值塞回同一个 `operator` 重新调一次 `mutate`，本质仍是上面的后门写路径。
- **GenUI 解码器**（`taskvm/workspace_ui/renderer.py`）：纯 Python f-string 拼接，`_PAGE_TPL` 是写死的 Jinja2 模板，全仓库找不到任何一处真正调用生成式模型来决定界面长什么样。
- **读路径**（`taskvm/task_state/compiler.py`）：这是全系统唯一部分真实的环节——只读渲染文本（a11y/DOM），用前沿模型（`gpt-5.6-sol`）发现 binding，不 import GT fixtures。**这一段基本可以保留、复用**，但后续要在更丰富的真实 UI 上重新验证泛化性。

**唯一判定标准（写死，任何你写的代码都必须经得起这条复核）**：
> 如果一步写操作或回退操作，不需要打开浏览器、不需要渲染出页面、也不需要模拟一次真实的点击/输入/回车，就能让状态发生改变，那它就是后门，不合规——无论内部实现看起来多么"语义化"、多么像是在调用 app 自己的业务 API。

你的任务：把这个心智模型真正实现出来。

```
真实 app 渲染画面（截图 / DOM / a11y）
        │
        │ 【① 编码器 = GUI Agent 的"观测"能力】（基本已具备，见 §0 上文）
        ▼
   VM-state（结构化任务状态）
        │
        │ 【② 解码器 = 真正的 GenUI 模型调用】—— 本次必须新增，对应任务包 P4
        ▼
   人可操纵的两区界面（只读区投影 + 可读可写区 governance）
        │
        │ 【③ 人在界面上编辑，产生语义化"意图 patch"】
        ▼
   语义化 patch / 撤销意图
        │
        │ 【④ 执行器 = GUI Agent 的"动作"能力】—— 本次必须新增，对应任务包 P2/P3
        │   拿到当前渲染画面 + 这个意图，自己决定点哪里/输入什么/按什么键
        │   回退同理：重新观察画面，自主规划一次新的撤销手势序列；
        │   找不到能完成撤销意图的 UI 控件，诚实报告"做不到"，不得回退到调 API
        ▼
   真实 app 状态改变（通过浏览器自动化产生的真实点击/输入/回车）
        │
        │ 【⑤ 独立 verifier 重新读真实状态】（已具备，不需要改）
        ▼
   判定 changed-happened / non-interference / reconciliation / 是否可回退
```

---

## 1. 五个任务包总览（对应上面架构图的五个箭头，取代旧版"周计划"）

| 任务包 | 目标 | 优先级 |
|---|---|---|
| **P1** | 5 个自建 app（calendar/taskboard/drive/mail/outlook_cal）补齐真实可交互 GUI | 最先做，是 P2/P3 前提 |
| **P2** | GUI Agent 执行器：接入真实 grounding/agent 模型，驱动浏览器完成写操作 | P1 之后 |
| **P3** | GUI Agent 回退：重新观察画面、自主规划撤销手势；做不到就诚实报错 | P2 之后 |
| **P4** | GenUI 解码器：真实模型把 VM-state 解码成界面描述 | 可与 P1-P3 并行 |
| **P5** | 重新验证：用真实实现重跑全套 kill-test，产出新的可信 `eval_results/*.json` | 最后，P1-P4 完成后 |

**建议顺序**：先在 **Calendar** 一个 app 上把 P1→P2→P3→P5 全链路纵向打穿、验证方案可行，再横向推广到 TaskBoard/Drive/Mail/OutlookCal。P4（GenUI）相对独立，可以任何时候插入并行做。

---

## 2. P1：给 5 个自建 app 补齐真实可交互 GUI

### 2.1 现状问题
`taskvm/apps/calendar/templates/calendar.html`（其余 4 个 app 同理）目前只是一张只读表格 + 一段内嵌 JS 直接 `fetch(POST /api/...)`。这个"按钮"从设计上就是给人类在浏览器里手工点的 debug 工具，不是一个真实软件会有的完整交互层级（详情页、编辑表单、确认对话框）。GUI Agent 执行器要"自己决定点哪里、输入什么"，前提是页面上真的有值得点的东西。

### 2.2 具体要求（5 个 app 都要做，逐个过一遍）
对每个 app（`taskvm/apps/{calendar,taskboard,drive,mail,outlook_cal}/`），针对它目前支持的每一种 `operator`（在 `state_adapter.py` 对应 Adapter 的 `mutate` 里能看到，比如 Calendar 的 `move_event`，TaskBoard 的 `set_deadline`，Drive 的 `move_file`，Mail 的发送/编辑草稿，OutlookCal 同 Calendar），补出一条**纯 GUI 路径**：

- **详情页/详情视图**：点击一个实体（事件/任务/文件/邮件）应该导航到或展开一个显示其完整字段的视图，而不是所有信息都摊在一张表格里。
- **编辑表单**：修改字段（如日期、deadline、文件路径）应该通过一个真实的表单控件完成（`<input type="date">`、下拉选择、文本框等），提交时是表单的正常提交行为（可以仍然是 `fetch`，但要挂在真实的表单/按钮上，且要有视觉反馈，如提交后跳转/刷新/toast），不能是"页面加载时就绑定好、跳过任何中间态"的极简单击。
- **删除/危险操作确认对话框**：如果这个 app 的某个 operator 语义上是"删除/移除"，要有一个确认步骤（原生 `confirm()` 也可以，只要是通过真实点击触发）。
- **发送类操作**（Mail 的发草稿/发送）：要有输入框 + 发送按钮的组合，不能是一个隐藏参数直接 POST。

**参照标准**：可以参考 `taskvm/harness/mobilegym_bridge.py` 里对接的 MobileGym 微信页面交互结构（虽然那是别的项目代码，不能改，但可以看它的组件层级作为"什么叫真实可交互 GUI"的直觉参照）；也可以直接参考常见的 Google Calendar / Trello / Google Drive / Gmail 页面的基本交互层级（不需要照抄视觉设计，只需要交互层级完整）。

### 2.3 验收标准
对每个 app 的每个写操作，能否找到一条"不看任何内部 API 文档、只靠看页面就知道怎么点"的路径。建议做法：写完后**自己用浏览器手动走一遍每个 operator 的 GUI 路径**，确认可行，再进入 P2。

### 2.4 产出证据
每个 app 至少 1-2 张关键页面截图（详情页、编辑表单、确认对话框），存到 `eval_results/p1_ui_screenshots/<app_name>/`。

---

## 3. P2：GUI Agent 执行器（写路径）

### 3.1 架构改造点
`taskvm/harness/state_adapter.py` 里每个 `*Adapter.mutate(self, sid, entity_id, operator, value)` 目前直接 `requests.post`。改造后，它应该：
1. 打开（或复用已打开的）浏览器页面，导航到目标 app 的当前状态；
2. 把 `(entity_id, operator, value)` 转换成一句自然语言指令（如"把事件 E1 的日期改成 2026-08-18"）；
3. 调用一个新建模块（建议 `taskvm/execution/gui_executor.py`）里的 GUI Agent 执行器，传入**当前页面截图/DOM/a11y** + 这句指令；
4. 执行器内部循环：模型看截图 → 决定下一步动作（点击坐标/输入文本/按键）→ 通过 Playwright 真实执行 → 重新截图 → 直到判断任务完成或达到步数上限；
5. 执行完毕后，`mutate` 重新调用只读路径（`read_canonical` 或复用 compiler 的观测逻辑）确认状态是否真的改变，返回 before/after 给 rollback 用。

### 3.2 参考实现：OSWorld mm_agents
路径：`/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/OSWorld/mm_agents/`

**不要照抄某个具体模型权重，是照抄这个 harness 的输入输出契约**。建议参考顺序：
1. `uitars_agent.py`（`class UITARSAgent`，核心方法 `predict(self, instruction, obs, last_action_after_obs=None) -> List`，`obs` 是 `{"screenshot": ..., "accessibility_tree": ...}` 结构）——理解它如何把"截图+a11y树+指令"变成一串结构化动作（`parse_action`/`parse_action_qwen2vl`），这是最直接可仿照的模式。
2. `aguvis_agent.py`、`qwen25vl_agent.py` 作为备选对照，理解不同模型family下 grounding 输出格式（坐标 vs 归一化坐标 vs 元素引用）的差异，选一种最适合公司网关可用模型（`glm-5v-turbo` 支持 vision，是现成可用的视觉模型；也可尝试其余支持 vision 的模型）的输出格式。
3. `prompts.py` 里能看到各家 agent 的 system prompt 设计范式，可直接借鉴其"要求模型输出结构化动作 JSON/DSL"的写法。

**你需要自己写的粘合层**（这是本次任务真正的工程量所在）：
- 一个薄的浏览器控制层（可以扩展 `taskvm/harness/browser_controller.py`，目前的 Playwright 封装），暴露"点击坐标"、"输入文本到当前 focus 元素"、"按键"、"截图"、"读 DOM/a11y" 这几个原子操作。
- 一个动作循环（`taskvm/execution/gui_executor.py`）：输入 `(page, instruction)`，循环调用模型 predict → 执行动作 → 判断是否 done（可以用一个简单的"模型输出 DONE/FINISH 动作"或"每步后重新读状态对比目标是否达成"的机制）→ 上限步数保护（防止死循环）。
- 模型客户端复用 `taskvm/benchmark/model_client.py` 已有的网关调用封装（附录 B 的限流/鉴权分层策略），不要重新造轮子。

### 3.3 承重不变量（不能违反）
- **不得绕过浏览器**：`mutate` 内部任何一步都不能有"如果 GUI 失败就退回 API"的分支——如果 GUI 路径做不到，应该让整个写操作失败并如实上抛，而不是静默降级到后门。
- **两个模型角色独立**：负责"发现 binding"的编码器调用和这里"执行动作"的执行器调用，必须是两次独立的模型调用，不共享 context/历史。
- **成本/超时保护**：GUI 动作循环要有步数上限和超时，避免一次写操作无限循环烧光 API 配额。

### 3.4 产出证据
每个 app 至少 1 个 operator 的完整执行录屏或分步截图（`open_app→定位元素→输入/点击→提交→重新读取确认`），存到 `eval_results/p2_visual_<timestamp>/<app_name>_<operator>/step_N_<action>.png`（命名习惯参照仓库里已有的 `eval_results/w2_visual_*/` 风格）。

---

## 4. P3：GUI Agent 回退（撤销）

### 4.1 架构改造点
`taskvm/execution/rollback.py` 目前的 `CompensationRecord` + `undo`/`undo_saga` 逻辑，改造后不再是"把 before 值传回 mutate"，而是：
1. 构造一句撤销意图的自然语言指令（如"撤销刚才把事件 E1 的日期改成 8/18 这个操作，恢复到 8/14"或更通用的"撤销上一步"）；
2. 调用同一个 P2 执行器模块，输入**当前画面** + 这个撤销意图；
3. 执行器自主判断：这个 app 的 UI 上是否存在能完成撤销的路径（如"编辑表单改回原值"、"撤回/删除消息按钮"、"版本历史回退"等）；
   - 如果存在，走真实手势完成，重新读状态确认恢复；
   - **如果不存在，必须诚实返回明确的失败信号**（例如 HTTP 409 或结构化的 `{"reversible": False, "reason": "..."}`），不允许回退到直接调用内部 API 强行把值改回去。

### 4.2 参考案例（同类问题已有先例，直接抄这个模式）
`taskvm/harness/mobilegym_bridge.py` 处理微信"发消息不可撤回"的方式是一个好的参照：调研确认微信没有删除/撤回 UI，`msg:` 前缀的撤销请求直接抛 `HTTPConflict`，不假装成功。把这个诚实失败的模式复制到 P1 新建的 5 个 app 的 GUI 上——先老老实实调研每个 app、每个 operator 是否真的有对应的撤销 UI 路径（比如"删除刚创建的任务"这种可能有删除按钮，"把日期从 8/14 改到 8/18 再改回 8/14"这种可以走"再编辑一次"的路径，但要让 GUI Agent 自己判断和执行，不能你替它写死"这个操作可以撤销"）。

### 4.3 新构念：以诚实为本的回退在 UI 上的呈现（用户明确要求的新设计点，价值很高，请认真做）
用户的原话类比：**进度条模型**——能回退的时候真的回退，不能回退的时候，要给用户一个诚实的、可见的呈现（"进度条拖到这里就拖不动了"），而不是让一个后端布尔字段（`SagaResult.partial_failure`）自己知道就完了。

具体要做：
1. **先修一个接线问题**：`taskvm/workspace_ui/server.py` 里的 `/undo` 路由目前调用的是 `rollback_log.undo_last`（旧的单 app 单步 undo），而不是支持 `partial_failure` 语义的 `undo_saga`。改成调用 `undo_saga`，否则下面第 2 步做的 UI 永远收不到这个字段。
2. **在只读区/governance 面板上新增一个"不可逆标记"的可视化组件**（`taskvm/workspace_ui/editable_components.py` + `live_sync.py`）：当某次撤销的结果里 `partial_failure=True` 时，明确展示"这一步操作已发生且当前无法通过 UI 撤销"，可以做成进度条卡在某个刻度、该刻度之前可拖动、该刻度本身标红/锁死的视觉隐喻，与开工大纲里 reconciliation 的琥珀色（amber）标记视觉语言保持一致的设计体系（都是"诚实呈现异常状态"的同一套语言，颜色可以用红色/警示色区分于 amber 的"外部变化"语义）。
3. 这个组件要能被 P5 的验证/演示直接用到——即回退接口返回 `partial_failure` 后，前端页面上确实能看到这个视觉变化，不是只存在于 JSON 里。

### 4.4 产出证据
- 一次"可回退成功"的完整截图/录屏序列。
- 一次"诚实报告不可逆"的完整截图/录屏序列，包括前端展示的锁死/标红效果。
- 存到 `eval_results/p3_visual_<timestamp>/`。

---

## 5. P4：GenUI 解码器（可与 P1-P3 并行）

### 5.1 架构改造点
`taskvm/workspace_ui/renderer.py` 的 `render(binding, values)` 目前是纯字符串拼接。改造后：
1. 新建一个模块（建议 `taskvm/workspace_ui/genui_decoder.py`），输入 VM-state（`TaskBinding` 结构），调用一个前沿通用模型（如 `gpt-5.6-sol`，与执行器/编码器**分开、独立**调用），在 system prompt 中注入 A2UI **v0.9** 协议 spec（`taskvm/benchmark/a2ui_spec.py::A2UI_V09_SPEC` 已经就绪，直接复用；官方源文件见 `docs/references/A2UI-protocol-spec/v0_9/`），要求模型输出结构化 UI 描述（`createSurface`/`updateComponents`/`updateDataModel`/`deleteSurface` 等 v0.9 A2UI 消息——注意 v0.9 的组件是扁平判别式 `{"component":"Text",...}`，不是 v0.8 的键包裹 `{"Text":{...}}`，两者不兼容，不要混用旧示例）。
2. `renderer.py` 保留的唯一逻辑是"把模型输出的结构化描述转成最终 HTML"的**薄**渲染层——即渲染层不再包含任何"决定界面该长什么样"的判断（比如"这个字段用什么控件展示"这种决策应该来自模型输出，不是渲染层里的 if-else）。
3. 两区语义（只读区投影 + 可读可写区 governance）必须保留，这是承重的 governance UI 设计，模型输出的结构里应该能表达这个区分（比如给每个字段标注 `editable: true/false`，渲染层据此决定是否套用可编辑区的样式）。

### 5.2 验收标准
- 同一个 VM-state，模型在不同调用间给出的界面结构可以有差异（这是"生成式"的体现），但功能语义（哪些字段可编辑、只读区展示什么）必须一致、正确。
- `renderer.py` 代码 review 时，找不到任何硬编码"某个字段/某种数据类型固定用什么控件"的判断逻辑。

### 5.3 产出证据
- 至少 3 个不同 VM-state 输入下，模型生成的界面截图 + 对应的原始模型输出（JSON/文本），存到 `eval_results/p4_genui_samples/`。

---

## 6. P5：重新验证（P1-P4 全部完成后做）

### 6.1 为什么必须重做
`taskvm/evaluation/run_w1_killtest.py` 等脚本此前的 `binding_f1`/`round_trip` 等数字，衡量的是"模型能否从渲染 HTML 文本正确抽取四元组，且套进硬编码 REST API 后状态按预期改变"——这是信息抽取任务的成功率，跟"GUI Agent 真的能在渲染页面上点对地方"完全是两件事。P2/P3 落地后，这些数字必须重新跑。

### 6.2 具体任务
1. 复用 `run_w1_killtest.py` 的编排模式（读 fixture → 跑 binding discovery → 用新执行器 dispatch → verifier 判定 → 落盘 JSON），但把 dispatch 环节换成 P2/P3 的真实 GUI 执行器。
2. 至少在 Calendar 上重新测：binding F1、round-trip fidelity（changed/untouched/resynced）、non-interference、neg-control（≤0.3，负对照仍然要过，注意 broken-dispatcher 分支不要走真实 GUI，直接注入错误保持负对照语义）、rollback fidelity（含"诚实不可逆"分支的判定）。
3. 产出 `eval_results/p5_full_killtest_<timestamp>.json`，格式尽量与旧版一致方便对比，但必须新增一个顶层字段清楚标注 `"execution_mode": "gui_agent"`（区别于旧数据的 API 直连模式），防止未来审计混淆两批数据。
4. 在这份 JSON 报告的摘要里，如实记录新旧数字的差异和原因（例如：GUI 执行路径引入的失败率、耗时变化等），不要因为数字比旧版差就选择性隐瞒。

### 6.3 不接受的做法
- 不接受"先跑一遍 API 直连版本蒙混过关，再补一个 GUI 版本的 demo 截图"这种两张皮做法——`eval_results/p5_*.json` 里的每一条数据必须真的经过 P2/P3 的执行器落地。
- 不接受把结果只写进 AI 助手的 memory 而不落盘到仓库里的 JSON 文件（`.mrules` E8/E9 明确点破过这个漏洞）。

---

## 7. 不能丢的先验（与开工大纲 §12 一致，此处重申关键项）

1. **后门判定标准**（§0）是唯一标准，任何"看起来合规"的绕路写法都不接受。
2. **两个模型角色独立**：GenUI 解码器（P4）与 GUI Agent 编码器/执行器（读+P2/P3），任何情况下都是独立调用，不共享 context。
3. **人 = governance 者，不是审核者**：P3 的撤销交互不能做成"必须人工确认才能继续"的样子。
4. **verifier 永远来自环境状态**：P5 的判定绝不能让生成 binding/执行动作的同一个模型自评。
5. **诚实优先于完成度**：任何一个任务包如果卡住、做不到，如实报告卡在哪、为什么，不要弱化验收标准或用 mock/占位符冒充完成。
6. **每个任务包完成后立刻落盘可复核证据**（截图/录屏/JSON），这是本次任务对你的硬性要求，不是可选项。
7. 不要修改 `taskvm/thirdparty/` 或任何第三方代码库（OSWorld、MobileGym 等）内部代码，只作参考，通过 TaskVM 自己的模块封装接入。
8. 项目代号 TaskVM，不引入前端框架（React/Vue），保持 Flask 渲染 + 原生 CSS/JS 的简单架构（P4 的 GenUI 输出仍然渲染成这套技术栈下的 HTML）。

---

## 8. 开工顺序建议（内部自主排期参考，不是硬性周计划）

1. **P1 Calendar** → **P2 Calendar**（跑通一个 operator 的完整 GUI 写路径）→ **P3 Calendar**（跑通一次可回退 + 一次诚实不可逆）→ 用这条链路验证整体方案可行。
2. **P4 并行插入**（不依赖 P1-P3 结果，可以随时做）。
3. **横向推广 P1→P2→P3 到 TaskBoard/Drive/Mail/OutlookCal**。
4. **P5 全量重新验证**，产出最终报告。
5. 全部完成后，检查 `eval_results/` 下是否所有关键产物都已落盘（截图目录 + JSON 报告），自查一遍 `.mrules` E8/E9 的"落盘"要求是否真的满足，而不是自我感觉满足。

祝顺利——这是一次决定 TaskVM 论文核心技术主张能否站得住脚的返工，值得投入认真做。
