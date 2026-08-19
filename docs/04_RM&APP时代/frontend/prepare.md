对。我们现在已经到了一个很适合**停止继续讨论抽象架构，直接让 coding agent 开工**的节点。

先给你最重要的判断：

**`docs/A2UI-protocol-spec/v0_9` 的核心协议是对的，可以作为 v0.9 实现依据；但这个目录目前不是一份“完整且与今天官方内容完全同步”的 v0.9 镜像。开工前要先修一次。**

我检查到的具体问题有两个：

1. 你压缩包里的 `v0_9/json/` 缺少当前官方目录里的 4 个文件：
   `client_to_server_list.json`、`client_to_server_list_wrapper.json`、`server_to_client_list.json`、`server_to_client_list_wrapper.json`。当前官方目录明确包含它们。turn910529view0
   更关键的是，你们本地的 `sample.json` **已经引用 `server_to_client_list.json`，但这个文件本地并不存在**。所以日常 `server_to_client.json` validator 可以工作，但完整 schema/sample resolution 并不闭合。

2. 本地 `a2ui_protocol.md` 比今天官方 living document 少了后来补的一条说明：Catalog 的 JSON Schema `$id` 和 A2UI `catalogId` 应该同时存在，并设置成相同 URI。turn986659view0

另外，官方现在把 **v0.9.1 标记为 current production，v0.9 是 previous stable**。所以 v0.9 不是错的，更不是作废协议；但是不能把 v0.9、v0.9.1 混着用。turn657279search2turn657279search4

我的建议是：**这一轮先冻结 TaskVM runtime 为严格 v0.9，把 `v0_9` 修完整并 pin upstream commit；等真正的 A2UI round-trip 跑通以后，再做一个非常干净的 v0.9 → v0.9.1 compatibility commit。** 这样最不容易因为协议迁移把真正重要的 GenUI / governance 工作搅在一起。

另外我检查代码时又找到了两个必须优先处理的问题，而且它们和你说的“真正开放场景”直接相关：

* `static/index.html` / `taskvm.js` 的首页目前还写死了**微信 / 支付宝 selector 和具体任务 chips**；
* bootstrap 成功后 `taskvm.js` 会直接调用 `/governance/start`，也就是说用户还没看编译出来的任务世界，它就自动开始跑了。

这两件事我都建议在 A2UI 之前修。否则下面做得再 generative，第一屏依然像一个“微信/支付宝 demo”，而不是 TaskVM；而 autostart 又会削弱我们一直强调的 **governance over autonomy**。

我已经把完整实施计划整理成可以直接交给 coding agent 的 Markdown：
`@docs/oracle/frontend/TaskVM_A2UI_Coding_Agent_Workplan_2026-08-19.md`

# TaskVM × A2UI Coding Agent 开工指令

## 一、最终目标

不要重写 TaskVM 的执行后半链。

保留现有：

`LocalPatch / GoalPatch → ActionContract → Serializer → CUA → GUI gestures → fresh observe → verifier`

本轮实现：

`VM-state → GenUI Decoder → A2UI → Renderer → 用户操作 → A2UI action → TaskVM governance`

最终 UI 分成两个明确区域：

**固定 Governance Shell**

由系统控制，不允许模型决定其是否存在：

* Goal
* Ready / Running / Paused / Completed
* Start / Pause / Resume / Stop
* Checkpoint
* Rollback
* Conflict
* Evidence / verifier
* Live substrate
* verified execution progress

**动态 A2UI Task Surface**

由 GenUI 模型根据 Task VM state 动态决定：

* 卡片怎么分组
* 哪些变量重点展示
* 哪些控件适合当前任务
* 表单 / list / tabs / choice / datetime 等布局
* 信息层级

禁止 task-specific / app-specific UI 模板。

---

## 二、P0：先修 A2UI protocol mirror

在任何 GenUI 功能代码之前：

1. 补齐 `v0_9/json/` 缺失的 4 个 list / wrapper schemas。
2. 同步当前官方 v0.9 living-document clarification。
3. `SOURCE.txt` 写入 upstream commit SHA。
4. 生成所有 vendored 文件 SHA-256 manifest。
5. 将 `agent_sdk_reference` 明确标成 SDK reference，而非 normative protocol。
6. 新增 protocol tests：

   * JSON 全部可 parse；
   * `$ref` 全可 resolve；
   * sample 可验证；
   * catalog `$id == catalogId`。

本轮 runtime 明确使用 `v0.9`，不要混入 `"version":"v0.9.1"`。

---

## 三、P0.5：先把假开放入口删掉

删除 production UI 中：

* 微信 selector
* 支付宝 selector
* `names = {wechat, alipay, x}`
* app-specific example chips
* goal API 的用户级必选 app 参数

首页改成：

`用户自然语言 goal → Compile Task`

app/substrate 是 runtime capability，不是用户创建 TaskVM 时必须选择的业务对象。

同时删除 bootstrap 后自动 `/governance/start`。

正确流程必须是：

`Goal → Compile → Ready → 用户检查 Task Surface / 权限 / 计划 → Start`

---

## 四、P1：新增生产级 `taskvm/genui`

新增：

`taskvm/genui/protocol.py`
`taskvm/genui/context.py`
`taskvm/genui/decoder.py`
`taskvm/genui/schema.py`
`taskvm/genui/validator.py`
`taskvm/genui/policy.py`
`taskvm/genui/data_model.py`
`taskvm/genui/action_router.py`
`taskvm/genui/store.py`

生产代码不得 import `taskvm_bench`。

优先使用正式 A2UI Agent SDK 完成 schema/catalog management 和 validation。

---

## 五、P2：定义 TaskSurfaceContext

GenUI 模型不得直接读取 Kernel 内部对象。

构造经过 scrub 的公开上下文：

* goal
* task status
* variables

  * semantic key
  * display label
  * value type
  * observed
  * desired
  * mutability
  * confidence
  * visible source label
* workflow/checkpoints
* conflicts
* allowed Task Surface actions

禁止输入：

* hidden canonical GT
* app database primary key
* benchmark oracle
* 内部 entity id
* 不可见后台信息

---

## 六、P3：真正的 GenUI Decoder

TaskArchitect **不生成 A2UI**。

新增独立模型调用：

`TaskSurfaceContext → GenUI Decoder → A2UI updateComponents`

服务器自己确定：

* createSurface
* surfaceId
* catalogId
* theme
* updateDataModel

模型主要决定：

`updateComponents.components`

这样模型控制 UI 结构，但不能控制身份、协议版本、数据真值或 governance capability。

第一版只允许 A2UI Basic Catalog。

禁止：

`WechatEditor`
`AlipayTransferCard`
`CalendarReleaseDate`
以及任何任务特异组件。

---

## 七、P4：严格分离结构变化与数据变化

首次任务：

`StateCompiler 1 次`
`TaskArchitect 1 次`
`GenUI Decoder 1 次`

正常 CUA 执行后：

`GenUI Decoder 0 次`
`updateDataModel`

LocalPatch：

`TaskArchitect 0 次`
`GenUI Decoder 通常 0 次`
`CUA 执行 → verifier → updateDataModel`

GoalPatch：

`TaskArchitect 1 次`
`GenUI Decoder 1 次`
`updateComponents + updateDataModel`

严禁每次 observed value 改变都重新问模型生成 UI。

---

## 八、P5：Backend A2UI API

新增：

`GET /api/sessions/<sid>/a2ui/bootstrap`

`GET /api/sessions/<sid>/a2ui/sse`

`POST /api/sessions/<sid>/a2ui/action`

A2UI endpoint 只能进入 TaskVM semantic/governance 层。

绝不能：

`A2UI action → substrate`

必须：

`A2UI → governance → ActionContract → Serializer → CUA`

---

## 九、P6：React A2UI Island

不要重写整个 dashboard。

新增：

`taskvm/workspace_ui/a2ui_client/`

使用 React + TypeScript + Vite。

React island 第一版负责：

* A2UI Task Surface
* Execution Snake
* Completion celebration

原有可信 shell 暂时继续负责：

* Start/Pause/Stop
* checkpoint/rollback
* conflict
* live phone
* verifier/evidence

等完整 round-trip 稳定后，再决定是否进一步 React 化。

---

## 十、P7：A2UI action governance

第一版 model-generated Task Surface 只开放：

`taskvm.local_patch`

Start / Pause / Resume / Stop / Checkpoint / Rollback / GoalPatch / ResolveConflict 保持 trusted shell。

LocalPatch：

`A2UI action`
→ validate action
→ validate semantic key
→ validate editable
→ validate type
→ GovernanceService
→ ActionContract
→ Serializer
→ CUA
→ fresh observe
→ verifier
→ updateDataModel

任何未知 action、readonly 修改、未知 semantic key 都必须拒绝，禁止猜。

---

## 十一、P8：动画

### Verified Snake Progress

不是假 0–100%。

把真实 checkpoint / verified milestone 做成 pellet：

`○──●━━●━━◉····○`

* 实心 tail = 已 verifier pass
* 发光 head = 当前执行 milestone
* head 可以在当前 segment 内游动
* verifier pass 后才允许吃掉下一颗 pellet
* pause = 原地呼吸
* rollback = 真实倒退
* conflict = amber pulse
* irreversible boundary = lock barrier

用 SVG + Motion 实现。

### 🎉 Completion

只有：

`task completed AND final independent verifier PASS`

才能：

* success card spring
* 🎉 bounce
* confetti
* final completion state

CUA 自报完成不能触发。

Checkpoint 可以小 burst；final success 才大 celebration。

必须支持 `prefers-reduced-motion`。

---

## 十二、P9：Open-world Kill Test

至少 3–5 个代码中从未出现的任务。

要求：

* 不修改 frontend；
* 不添加新 component；
* 不添加新 semantic-key branch；
* 不添加 app-specific condition；
* GenUI 仍能生成不同 Task Surface；
* 用户仍能修改；
* 修改仍进入真实 CUA；
* verifier 仍能闭环。

同一个 semantic task 至少跑两个 substrate/stack：

* Task Surface 语义稳定；
* 用户操作相同；
* CUA trajectory 可以不同。

Production grep 出现：

`wechat / alipay / 某业务 semantic key`

都必须人工审计。

---

## 十三、完成标准

只有以下全部成立，才叫真正完成：

* [ ] 正式 A2UI message 被浏览器 Renderer 消费。
* [ ] 动态组件树由 GenUI 模型决定。
* [ ] 普通状态更新 0 次 GenUI call。
* [ ] 用户 A2UI 操作产生结构化 TaskVM patch。
* [ ] Task Surface 不直接接触 CUA。
* [ ] Start/Pause/Stop/Rollback 永远存在且不受模型控制。
* [ ] bootstrap 默认 Ready 而不是 autostart。
* [ ] production UI 没有微信/支付宝任务入口硬编码。
* [ ] unseen task 不需要改前端。
* [ ] readonly/unknown action 全部 server-side reject。
* [ ] GUI write-back 仍然是真实 gestures。
* [ ] fresh observe + independent verifier 决定成功。
* [ ] final verifier PASS 才 🎉。
* [ ] snake 只推进 verified progress。
* [ ] rollback 时进度和真实 app 状态一起倒退。
* [ ] 至少两个 substrate 使用同一套 Task Surface architecture。

### 你现在应该下载什么

我会分成“现在就装”和“以后想更炫再装”。

**现在就装：**

1. **Node.js 22 LTS**。当前 Vite 要求 Node.js `20.19+` 或 `22.12+`，所以直接用 Node 22 是最省心的。
2. **React + TypeScript + Vite**，用 `npm create vite@latest a2ui_client -- --template react-ts` 建 island。
3. **`@a2ui/react` + `@a2ui/web_core`**。官方 React renderer 自己就是用 `MessageProcessor + A2uiSurface + basicCatalog` 这条路径，并明确提供 `v0_9` import；不要自己重新实现 A2UI message processor。1
4. **`motion`**。这个就是我们做 spring、snake head、卡片进出、rollback reverse 的主力。Motion 官方支持 React 18.2+，Vite 不需要特殊配置。
5. **`canvas-confetti`**。专门负责你要的 🎉；而且它原生提供 reduced-motion 选项。
   顺便说一句：你压缩包现在的 `static/confetti.min.js` **不是官方 canvas-confetti dist，而是仓库自己写的 API-compatible reimplementation**。既然这次要引入 bundler，我建议把它换成真正的 npm package。
6. **`lucide-react`**。做 Pause、Play、Undo、Checkpoint、Shield、Lock、Evidence 之类图标，不需要维护一堆 SVG 文件。官方包就是 `npm install lucide-react`。search1
7. 后端安装 **`a2ui-agent-sdk`**。当前官方 Python SDK 提供 `A2uiSchemaManager`、validator、BasicCatalog 和 parsing/schema utilities；PyPI 当前发布版是 0.4.0。

可以直接交给 coding agent：

```bash
# backend
pip install "a2ui-agent-sdk==0.4.0"

# frontend
cd taskvm/workspace_ui
npm create vite@latest a2ui_client -- --template react-ts
cd a2ui_client

npm install @a2ui/react @a2ui/web_core
npm install motion
npm install canvas-confetti
npm install lucide-react

npm install -D @types/canvas-confetti \
  vitest \
  @testing-library/react \
  @testing-library/jest-dom
```

然后**提交 `package-lock.json`**。包版本更新很快，因此不要让 coding agent 以后每次装 `latest` 得到不同环境。

### Rive 要不要下？

**第一轮我建议不要。**

Rive 很适合以后做一个 TaskVM 自己的小角色——比如运行时眨眼、checkpoint 时跳一下、完成时庆祝。官方 React runtime 有 Canvas 和 WebGL2 版本。

但是 Rive runtime 明显比我们现在需要的重；官方列出的 runtime size 也说明它不是一个“为了做一条进度动画随手加上”的小依赖。

你的三个需求：

* 🎉 完成庆祝
* 贪吃蛇进度
* 多邻国式弹性 UI

**Motion + SVG + canvas-confetti 已经完全够用。**

等整个 research prototype 跑通以后，如果我们真的决定做一个“TaskVM 小生命”，再装：

```bash
npm install @rive-app/react-canvas
```

就行。

### 我特别想把“贪吃蛇”做成什么样

我不建议做一个单纯“绿色条不断跑”的 loading bar。那会有一个研究上很危险的问题：**它可能让用户误以为 agent 已经取得了真实进展。**

我想把它做成 **Verified Snake**：

```text
       ✓        ✓
   ●━━━━●━━━━━◉····○····○
              ↑
           正在执行
```

蛇头可以在当前一段里有生命感地游动，但只有 verifier 真正确认 milestone 完成，它才能“吃掉”下一颗节点。

* 执行中：蛇头小幅弹跳、尾巴流动；
* verifier pass：`pop!` 吃掉下一颗；
* checkpoint：小 🎊；
* Pause：蛇头停下来轻微呼吸；
* conflict：节点变成警告态；
* rollback：蛇**真的倒着游回来**；
* final verifier pass：最后一颗变成大 ✓，然后 🎉 confetti。

这其实特别适合 TaskVM：**动画本身也表达 governance 和 truthfulness，而不是单纯装饰。**

至于“多邻国感”，我会学它的**粗圆、弹性、奖励反馈、pressed button、低认知负担**，而不会下载或模仿 Duolingo 的 mascot、插画、音效或品牌素材。我们应该让人第一眼觉得“这个东西活着、愿意让我操作”，但第二眼又能清楚看到：**现在世界是什么状态、它准备做什么、我什么时候能停、哪里能回退、什么已经被真正验证。**

这才会是“炫酷”同时又真正属于 TaskVM 的 UI。
