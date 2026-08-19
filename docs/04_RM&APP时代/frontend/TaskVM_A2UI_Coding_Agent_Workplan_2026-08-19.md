# TaskVM × A2UI 开工执行计划
**日期：2026-08-19**  
**面向：Coding Agent**  
**目标：把当前 TaskVM 做成一个真正由 live task state 驱动、用户可治理、面向开放任务而不是预写场景模板的交互原型；动态任务区使用正式 A2UI，固定治理区保证控制权和可解释性。**

---

## 0. 开工前结论：不要从“重写整个前端”开始

当前 TaskVM 已经有可复用的后半条链：

`LocalPatch / GoalPatch -> ActionContract -> Serializer -> CUA -> GUI gestures -> fresh observe -> verifier`

本轮主要补齐前半条链：

`VM-state -> GenUI Decoder -> A2UI -> Renderer -> 用户操作 -> A2UI action -> TaskVM governance`

**绝对不要**再增加“用户点控件 -> LLM 重新翻译成自然语言 -> CUA”的中间模型。A2UI action 应直接进入 TaskVM 现有 governance 语义层。

本轮采用“两层 UI”：

1. **固定 Governance Shell（非模型生成）**  
   Goal、Start/Pause/Resume/Stop、checkpoint、rollback、conflict、verifier/evidence、live substrate、执行进度等永远由系统控制。
2. **动态 A2UI Task Surface（模型生成）**  
   根据当前 Task VM state 决定任务相关控件、分组、卡片、表单与信息层级。

这样既有生成式 UI 的开放性，又不会让模型决定“用户还能不能暂停/回退/看证据”。

---

# 1. A2UI v0.9 目录审计结论

仓库当前目录：

`docs/A2UI-protocol-spec/v0_9`

### 1.1 可以保留的结论

- 本地 JSON 文件都能正常 JSON parse。
- `server_to_client.json`、`common_types.json`、Basic Catalog 等核心文件形状与官方 v0.9 主线一致，可以解释现有 benchmark/validator 为什么能工作。
- v0.9 的核心消息仍然是：
  - `createSurface`
  - `updateComponents`
  - `updateDataModel`
  - `deleteSurface`
- v0.9 的组件是 flat discriminator：
  - `{"id":"x","component":"Text",...}`
  - 不是 v1.0 keyed wrapper。

### 1.2 必须修正的地方

当前目录**不能视为“完整、当前、逐文件的官方镜像”**。

当前本地 `json/` 只有：

- `client_capabilities.json`
- `client_data_model.json`
- `client_to_server.json`
- `common_types.json`
- `sample.json`
- `server_capabilities.json`
- `server_to_client.json`

而当前官方 v0.9 JSON 目录还包含：

- `client_to_server_list.json`
- `client_to_server_list_wrapper.json`
- `server_to_client_list.json`
- `server_to_client_list_wrapper.json`

尤其本地 `sample.json` 已经引用了 `server_to_client_list.json`，但该文件本地缺失，所以这份 vendored spec 对“完整 schema resolution / sample validation”是不完备的。

另外，本地 `docs/a2ui_protocol.md` 比当前官方 living document 少了一条后续补充说明：Catalog JSON Schema 的 `$id` 与 A2UI 的 `catalogId` 应同时存在且使用同一个 URI。

### 1.3 版本策略

**本轮不要混用版本。**

为了尽快打通现有代码，建议：

- 本轮 runtime 先明确冻结为 **A2UI v0.9**；
- 把 `docs/A2UI-protocol-spec/v0_9` 修成一个**完整、可追溯、pin 到官方 commit 的镜像**；
- 在 `SOURCE.txt` 写：
  - upstream repo
  - upstream commit SHA
  - fetch date
  - protocol family
  - 每个 vendored 文件的 SHA-256
- 之后单独做一个 v0.9 -> v0.9.1 compatibility commit。

原因：官方现在把 **v0.9.1** 标成 current production，而 v0.9 是 previous stable；但仓库现有 benchmark、validator、prompt 和示例都按 `"version":"v0.9"` 工作。本轮最忌讳的是“一半 v0.9 schema + 一半 v0.9.1 payload”。

**Gate P0：任何 A2UI 功能代码开工前，先把协议来源锁死。**

---

# 2. 当前仓库还必须先清掉的两个“开放场景”反例

当前 `taskvm/workspace_ui/static/index.html` / `taskvm.js` 仍然有：

- 微信 / 支付宝 app 下拉框；
- 微信 / 支付宝特定示例 chips；
- `names = {wechat, alipay, x}`；
- `POST /api/app/goals` 强制要求一个 app；
- bootstrap 完成后前端自动调用 `/governance/start`。

这些都不符合我们要表达的产品心智：

> 用户提交的是任务目标，不是在选择“我要操纵哪个 app”；  
> TaskVM 应先编译任务世界，再让用户看清楚并决定何时开始自治。

必须改为：

- 首页只保留一个自然语言 Goal 输入；
- app/substrate 选择属于 runtime capability，不属于任务 UI；
- MobileGym 如仍需要一个技术上的 initial foreground app，由环境配置选择，不由任务语义写死；
- bootstrap 完成后进入 **Ready**，不要自动 Start；
- 用户先看到 task surface、计划/风险/权限，再明确点击 Start；
- demo 示例若保留，只能放在 demo fixture/config 中，不能写死进生产 HTML/JS。

**Gate P0.5：grep 生产 UI，不得再出现 `wechat`、`alipay`、某个业务任务的 semantic key 条件分支。**

---

# 3. 最终运行时架构

```text
                 ┌─────────────────────────────────────┐
                 │        Fixed Governance Shell       │
                 │ Goal / Start / Pause / Stop         │
                 │ Checkpoint / Rollback / Evidence    │
                 │ Conflict / Live substrate / Progress│
                 └────────────────┬────────────────────┘
                                  │
                                  │ trusted controls
                                  ▼
User ── edits ──>  Dynamic A2UI Task Surface
                  (model chooses layout/components)
                         │
                         │ structured A2UI action
                         ▼
                  TaskVM Action Router
                         │
                    policy validation
                         │
                ┌────────┴────────┐
                ▼                 ▼
           LocalPatch          GoalPatch
                │                 │
                └──────┬──────────┘
                       ▼
              existing TaskVM runtime
 ActionContract -> Serializer -> CUA -> GUI gestures
                       │
                       ▼
                 fresh observe
                       │
                       ▼
                    verifier
                       │
                       ▼
              deterministic mapper
                       │
                       ▼
             A2UI updateDataModel
```

### 关键原则

- **TaskArchitect 不生成 A2UI。**
- 新增独立 **GenUI Decoder**。
- GenUI Decoder 只负责“怎么把 VM-state 组织成人能操作的任务 UI”。
- 普通状态变化绝不重新调用 GenUI 模型。
- `updateDataModel` 是确定性更新。
- 只有结构性变化（首次 compose / GoalPatch / 可见变量集合或交互 affordance 大幅改变）才调用 GenUI Decoder。
- A2UI 不能直接调用 CUA。
- A2UI 不能输出 app-specific operation。
- Governance Shell 的存在、位置、Pause/Stop/Rollback 能力不能由模型控制。

---

# 4. 生产代码新增目录

新增：

```text
taskvm/genui/
├── __init__.py
├── protocol.py
├── context.py
├── decoder.py
├── schema.py
├── validator.py
├── policy.py
├── data_model.py
├── action_router.py
├── store.py
└── prompts/
    └── decoder_system.md
```

职责：

### `protocol.py`
唯一协议版本常量、catalog id、surface id 命名规则。禁止在多处散落 `"v0.9"`。

### `context.py`
定义传给 GenUI Decoder 的**公开语义上下文**，禁止直接把 Kernel 对象、隐藏 GT、数据库 ID、app 内部 primary key 塞给模型。

建议字段：

```text
goal
task_status
variables[]
  semantic_key
  display_label
  value_type
  observed
  desired
  mutability
  confidence
  visible_source_label
workflow/checkpoints[]
conflicts[]
allowed_surface_actions[]
```

### `decoder.py`
真实模型调用。输入 `TaskSurfaceContext`，输出 A2UI `updateComponents`。

### `schema.py`
使用官方 A2UI Agent SDK / vendored formal schemas 管理 schema/catalog。

### `validator.py`
两层校验：

1. A2UI protocol/catalog schema validation；
2. TaskVM semantic policy validation。

### `policy.py`
检查：

- 输入控件只能绑定 editable variable；
- 绑定路径只能指向当前 surface data model 的白名单路径；
- action name 必须在 allowlist；
- action context 里的 semantic key 必须存在；
- component 数量、树深、文本长度有上限；
- 禁止任意 URL / iframe / executable content；
- 不允许模型创建、隐藏或替换固定治理控件。

### `data_model.py`
纯函数：

`projection snapshot -> A2UI data model`

不得调用模型。

### `action_router.py`
解析 Renderer 回传 action -> 校验 -> 调现有 GovernanceService。

### `store.py`
保存每个 session 当前 A2UI surface generation、component message、data model revision。不要污染 Kernel semantic state。

---

# 5. Generative Boundary：让模型只生成“结构”，不要生成“事实”

建议让服务器确定性产生：

- `createSurface`
- `updateDataModel`
- surfaceId
- catalogId
- theme
- governance capability

让模型只产生：

- `updateComponents.components`

这是比“让模型输出整条协议”更安全的边界，同时依旧是真 A2UI。

模型可以看 observed/desired 值来决定 UI，但动态值应尽量通过：

```json
{"path":"/variables/release_date/desired"}
```

绑定，而不是把当前值复制成 literal。

这样：

```text
第一次：
GenUI call -> updateComponents
server -> updateDataModel

CUA 每次执行后：
0 次 GenUI call
server -> updateDataModel

LocalPatch：
通常 0 次 GenUI call
server -> updateDataModel

GoalPatch / structural change：
1 次 GenUI call
server -> updateComponents
server -> updateDataModel
```

---

# 6. 第一版不要做 TaskVM 自定义 Catalog

**第一版只用 A2UI Basic Catalog。**

先证明模型真的可以在未知任务下自由组合：

- Text
- Row
- Column
- Card
- List
- Tabs
- Divider
- Button
- TextField
- CheckBox
- ChoicePicker
- Slider
- DateTimeInput
- Image/Icon（必要时）

不要一开始造：

- `WechatEditor`
- `AlipayTransferCard`
- `CalendarReleaseDate`
- 任何具体 app / 具体 task component

甚至 `EvidenceBadge`、`CheckpointTimeline` 第一版也先放固定 shell 中。

只有当 Basic Catalog 做不到一个**跨任务、跨 app、跨 substrate**的交互语义时，第二版才允许添加 domain-neutral 组件。

---

# 7. Backend 实施步骤

## P0 — 协议镜像修复

1. 补齐官方 v0.9 缺失的 4 个 JSON schema。
2. 同步当前官方 v0.9 docs clarification。
3. `SOURCE.txt` 增加 upstream commit + file hashes。
4. 把 `agent_sdk_reference` 标记成“SDK reference，非 normative protocol spec”。
5. 新增测试：
   - 所有 JSON 可 parse；
   - 所有本地 `$ref` 可解析；
   - `sample.json` 可完成 schema resolution；
   - Basic Catalog `$id == catalogId`。
6. 不要先改 A2UI 业务代码。

**DoD：spec test 全绿。**

## P1 — 把 benchmark A2UI 代码提升到 production

当前：

`taskvm_bench/benchmark/a2ui_schema_manager.py`

不能被生产路径 import。

做法：

1. 在 `taskvm/genui/` 写 production implementation。
2. 优先采用官方 `a2ui-agent-sdk` 的 SchemaManager / BasicCatalog / validator 能力。
3. benchmark 如要复用，应 import production 公共 API，而不是反向。
4. 保留原 benchmark 行为的 regression tests。

**DoD：`taskvm/` 零 import `taskvm_bench/`。**

## P2 — TaskSurfaceContext + deterministic data model

建立：

```text
TaskSurfaceContextBuilder
TaskDataModelProjector
```

把公开 snapshot 转成：

```json
{
  "task": {...},
  "variables": {
    "release_date": {
      "label": "发布日期",
      "observed": "...",
      "desired": "...",
      "mutability": "editable",
      "status": "..."
    }
  },
  "workflow": {...},
  "conflicts": {...}
}
```

重要：

- semantic key 可以内部稳定，但 UI 必须显示 `display_label`；
- 不把 hidden canonical state / verifier GT 喂给 GenUI model；
- 不把 app 内部 DB id 喂给模型；
- artifact 只暴露安全 ref，不暴露真实文件路径。

**DoD：mapper 是纯函数；同一 snapshot 输入必得同一 data model。**

## P3 — 真 GenUI Decoder

输入：

- TaskSurfaceContext；
- A2UI formal schema；
- Basic Catalog；
- TaskVM UI rules；
- allowed action names。

输出：

- 一个或多个 `updateComponents` messages。

Generation policy：

- 结构由模型决定；
- 不允许输出 HTML/JS；
- 不允许发明组件；
- 所有可编辑控件必须 bind `/variables/<key>/desired`；
- readonly 变量不可用 input；
- 除 UI label/heading 外，动态任务值使用 data binding；
- 组件数量上限，例如 80；
- 树深上限，例如 8；
- 输出失败最多 repair 1 次，之后诚实 fallback。

**Fallback 不能是 task-specific 模板。**  
允许的 fallback 是一个完全通用的“变量列表 + 状态文本”Basic Catalog surface。

**DoD：3 个互不相干的 unseen goal 产生不同 component trees，前端零代码修改。**

## P4 — A2UI backend transport

建议新增：

```text
GET  /api/sessions/<sid>/a2ui/bootstrap
GET  /api/sessions/<sid>/a2ui/sse
POST /api/sessions/<sid>/a2ui/action
```

`bootstrap`：
- deterministic createSurface；
- 最新 updateComponents；
- 最新 updateDataModel。

`sse`：
- 只发送 A2UI 消息/必要 progress event；
- 保证有序；
- 可带 monotonically increasing revision。

`action`：
- 接 Renderer action；
- 经过 policy；
- 进入现有 GovernanceService。

不要让 A2UI endpoint 直接 import substrate driver。

**DoD：浏览器可以在不读取旧 variables DOM 模板的情况下仅靠 A2UI stream 渲染动态任务区。**

## P5 — 修复 bootstrap wiring

当前 `bootstrap_real_full()` 调了：

```text
kernel.init_task_state(arch.variables)
kernel.set_plan(arch.graph)
```

而 `set_plan()` 本身支持 schema 参数。

至少先修正为不丢失 Architect 结果中的 schema（如果该 schema 仍作为语义 hint 使用）。

但要明确：

> 旧 `ProjectionSchema` 不是 A2UI，它最多是 GenUI Decoder 的辅助语义信息，不能直接冒充 renderer tree。

首次 bootstrap 完成后：

1. kernel ready；
2. build TaskSurfaceContext；
3. GenUI Decoder 一次；
4. validate；
5. store surface；
6. UI 显示 Ready；
7. **等待用户按 Start**。

---

# 8. Frontend 实施步骤

当前前端没有 package manager，采用“React island”，不要一口气重写整个 dashboard。

新增：

```text
taskvm/workspace_ui/a2ui_client/
├── package.json
├── vite.config.ts
├── src/
│   ├── main.tsx
│   ├── TaskExperience.tsx
│   ├── a2ui/
│   │   ├── processor.ts
│   │   ├── catalog.ts
│   │   └── actionBridge.ts
│   ├── motion/
│   │   ├── ExecutionSnake.tsx
│   │   ├── Celebration.tsx
│   │   └── MotionProvider.tsx
│   ├── components/
│   │   ├── StatusPill.tsx
│   │   └── SurfaceFrame.tsx
│   └── theme/
│       └── tokens.css
└── tests/
```

build 输出复制到：

`taskvm/workspace_ui/static/a2ui/`

原 `index.html` 增加：

```html
<div id="task-experience-root"></div>
```

第一版让 React island 负责：

- 动态 A2UI Task Surface；
- 新的 snake progress；
- completion celebration。

保留原生 JS shell 负责：

- Start/Pause/Resume/Stop；
- checkpoint/rollback；
- live phone；
- conflicts；
- evidence。

第二版再考虑逐步 React 化 shell。

---

# 9. Open-world 入口必须一起改

## 删除

```text
<select id="hero-app">
微信
支付宝
</select>
```

删除 app-name mapping 和 app-specific demo chips。

## 新入口

```text
[ TaskVM ]

What do you want to accomplish?

[ 用自然语言描述一个任务……………………………… ]

[ Compile task ]
```

下面只显示系统能力：

```text
Connected world: MobileGym / OSWorld / Builtin Web
Available surfaces discovered: N
```

“available surfaces”可以由 substrate 报告，**不是**用户先选 app 才能创建 task。

`POST /api/app/goals` 改成主要接受：

```json
{"goal":"..."}
```

如底层 demo 环境技术上必须指定初始 foreground surface，把它放到 server config，不放在任务 prompt 和用户 UI。

---

# 10. A2UI action policy

第一版动态 surface 只允许非常小的 action surface：

```text
taskvm.local_patch
```

全局治理动作继续留在固定 shell：

```text
start
pause
resume
stop
checkpoint
rollback
goal_patch/recompose
resolve_conflict
```

理由：

- 模型不能决定是否给用户 Pause/Stop/Rollback；
- 模型不能“忘了”显示治理能力；
- model-generated Task Surface 只负责 task-specific manipulation；
- governance capability 属于 trusted chrome。

`taskvm.local_patch` action 处理流程：

1. 校验 action.name；
2. 校验 semanticKey 存在；
3. 校验 variable.mutability == editable；
4. 校验 value type；
5. 调现有 `GovernanceService.local_patch`；
6. 让 runtime 继续走现有 Serializer/CUA；
7. fresh observe；
8. verifier；
9. deterministic `updateDataModel`。

未知 action / 未知 key / readonly 编辑一律 4xx，不能 best-effort 猜。

---

# 11. “多邻国感”视觉原则：学感觉，不复制资产

不要下载或复刻 Duolingo 吉祥物、插画、音效、商标或专有素材。

借鉴的是交互语言：

- 大圆角卡片；
- 轻微 3D/pressed button；
- 明确的成功反馈；
- springy 微动效；
- 大而清楚的状态；
- 很少让用户读后台日志；
- 每一次“系统真的完成了”都有奖励感；
- 出错时仍然友好，但绝不拿动画掩盖失败。

建议：

- card radius：16–22px；
- button 有 3–4px “底沿”，按下时 `translateY(2px)`；
- hover/tap scale 约 0.98–1.02；
- 页面结构变化用 spring，而不是线性 fade；
- 不要让整个页面一直动，只有状态变化时动。

---

# 12. 动画规格

## 12.1 完成 🎉

触发条件必须是：

```text
task status == completed
AND final verifier == pass
```

不是“CUA 说完成了”就触发。

动画：

1. success card spring scale `0.88 -> 1.04 -> 1.0`；
2. `🎉` / check 图标小幅 rotate + bounce；
3. canvas confetti 约 0.8–1.5s；
4. 只触发一次；
5. `prefers-reduced-motion` 下无 confetti，只显示静态成功态。

checkpoint 通过可以小 burst；整个任务完成才大 celebration。

## 12.2 “贪吃蛇”进度条

不要做传统 0–100% 百分比，因为 agent 任务未必能预知真实剩余工作。

做成 **verified waypoint trail**：

```text
○──●━━●━━◉····○
         ↑
      active head
```

或一条轻微弯曲的 SVG path：

- 每个 checkpoint / verified workflow milestone 是 pellet；
- 已验证完成的部分 = 实心尾巴；
- 当前 executing = 发光蛇头；
- 未开始 = 浅色 pellet；
- head 只可以在当前 segment 内“游动”，**verifier PASS 后才跨过下一颗 pellet**；
- pause：蛇头原地轻微呼吸，不前进；
- rollback：蛇头真实向后退；
- conflict：当前 pellet amber pulse；
- irreversible boundary：画一条 lock/barrier，不能用“可回退”的动画暗示。

实现建议：

- SVG `<path>` + Motion；
- 使用 `pathLength` 或根据 milestone index 算进度；
- active head 用 `motion.circle`；
- 可加 2–4 个淡化 tail dots，形成 snake 感；
- 不需要 GIF/视频素材。

## 12.3 运行中的反馈

- card update：短 spring；
- verifier pass：绿色/成功状态轻弹；
- verifier fail：小幅 horizontal wobble + amber/red status；
- conflict 到来：卡片 border pulse；
- pause/resume：按钮状态 morph；
- rollback：进度 trail reverse，而不是“重新刷新页面”。

---

# 13. 需要下载/安装的东西

## 必须

### 系统
- Node.js：满足当前 Vite 要求；建议 Node 22 LTS。
- npm（Node 自带即可）。

### Python 后端

```bash
pip install "a2ui-agent-sdk==0.4.0"
```

然后把它写入 `pyproject.toml`，不要只安装在开发机。

### 前端 scaffold

```bash
cd taskvm/workspace_ui
npm create vite@latest a2ui_client -- --template react-ts
cd a2ui_client
```

### A2UI renderer

```bash
npm install @a2ui/react @a2ui/web_core
```

使用 versioned v0.9 import path。

### 动画

```bash
npm install motion
```

### 完成庆祝

```bash
npm install canvas-confetti
npm install -D @types/canvas-confetti
```

仓库当前 `static/confetti.min.js` 是一个“API-compatible local reimplementation”，不是官方 `canvas-confetti` 分发包。引入 bundler 后建议删掉这份自制替代，直接 bundle 官方 npm 包。

### Icons

```bash
npm install lucide-react
```

### 前端测试

```bash
npm install -D vitest @testing-library/react @testing-library/jest-dom
```

安装完**必须提交 lockfile**，不要依赖未来的 `latest`。

## 可选：第二阶段才装

如果以后真的想做一个 TaskVM 自有的“会呼吸/会庆祝的小角色”或复杂矢量插画：

```bash
npm install @rive-app/react-canvas
```

或使用 Rive 的 WebGL2 React runtime。

但**第一版不要装**。Motion + SVG + canvas-confetti 已足够完成：
- snake progress；
- spring cards；
- check/bounce；
- confetti；
- rollback reverse；
- status pulse。

Rive 会明显增加 runtime 体积和素材生产工作量，当前不是研究贡献的瓶颈。

---

# 14. 不需要下载的东西

第一版不需要：

- Duolingo 素材包；
- 任何 Duolingo mascot；
- Lottie 动画包；
- GIF progress animation；
- UI 图片素材库；
- Macaron 已训练模型；
- 一个 task-specific component marketplace。

我们需要的核心不是素材，而是：

```text
A2UI schema + catalog
A2UI MessageProcessor/Renderer
GenUI decoder
TaskVM governance policy
Motion/SVG
Confetti
Icons
```

---

# 15. 测试计划：不能再以“字段出现了”当完成

## Unit

### Protocol
- vendored `$ref` 全解析；
- official sample validate；
- catalog id invariant。

### GenUI
- valid A2UI accepted；
- malformed rejected；
- unknown component rejected；
- unknown action rejected；
- readonly input rejected；
- unknown semantic key rejected；
- excessive component tree rejected。

### Data model
- snapshot -> data model deterministic；
- observed value changes只改 data model；
- normal update 不增加 GenUI model call counter。

## Integration

### Bootstrap
- goal -> compiler -> architect -> genui decoder；
- UI ready；
- **没有 autostart**；
- 用户点 Start 后才进入 autonomy。

### LocalPatch
- 输入控件本地更新 desired；
- action -> governance local patch；
- serializer -> CUA；
- fresh observe；
- verifier；
- A2UI data model 更新。

### GoalPatch
- committed history 保留；
- future recompose；
- GenUI decoder 恰好重新调用一次；
- component tree 可以改变。

### Rollback
- fixed shell rollback；
- real app compensation；
- verifier；
- snake progress reverse。

---

# 16. Open-world Kill Tests

这是本轮最重要的验收，不要只跑熟悉 demo。

至少准备：

### Set A — 未见任务
3–5 个之前代码里从未出现的 goal，类型差异要大，例如：
- 跨 app 信息整理；
- 日期/时间调整；
- 布尔/枚举偏好；
- 文本编辑；
- 需要冲突处理的任务。

### Set B — UI 泛化
要求：
- 不改 frontend；
- 不加 component；
- 不新增 `if semantic_key == ...`；
- 模型仍能生成可操作 surface。

### Set C — substrate
同一 semantic task 在至少两个 substrate/stack 上跑：
- UI task surface 基本稳定；
- 用户操作语义相同；
- 底层 CUA trajectory 可以不同。

### Static gate
生产目录：

```bash
grep -R "wechat\|alipay\|release_date\|calendar.*if\|taskboard.*if" taskvm/workspace_ui taskvm/genui
```

任何命中都要人工解释；业务模板条件分支直接失败。

---

# 17. 视觉验收

原型达到以下状态才算“足够炫酷但仍可信”：

- 首屏不是工程 dashboard，而是 task-native experience；
- Ready 状态清楚告诉用户“已编译，尚未执行”；
- Start 是明确动作；
- dynamic surface 因任务不同而明显变化；
- progress 是一条有生命感的 verified snake trail；
- checkpoint 通过有小奖励；
- final verifier pass 有 🎉；
- rollback 能看到进度向后移动；
- pause 不再假装有进展；
- conflict / verification failure 不使用庆祝动画；
- reduced-motion 可用；
- live phone/evidence 仍可随时打开核对真实世界。

---

# 18. 推荐提交顺序

每个阶段独立 commit，任何阶段失败都可以回滚：

1. `chore(a2ui): pin and complete v0.9 specification mirror`
2. `refactor(open-world): remove app-specific hero and autostart`
3. `feat(genui): add production schema/policy/data-model layer`
4. `feat(genui): add real A2UI decoder`
5. `feat(api): add a2ui bootstrap/sse/action transport`
6. `feat(ui): add React A2UI island`
7. `feat(governance): wire A2UI local_patch to existing service`
8. `feat(motion): add verified snake progress and celebrations`
9. `test(open-world): add unseen-task and no-hardcoding gates`
10. `test(e2e): add round-trip CUA + verifier + rollback journey`

每个 commit：
- 先跑相关 unit；
- 再跑 integration；
- 不通过不要进入下一阶段。

---

# 19. 最终 Definition of Done

只有同时满足以下条件，才可说“TaskVM 已经有真正的 generative governance UI prototype”：

- [ ] A2UI v0.9 vendor mirror 完整、pin 来源、hash 可追踪。
- [ ] 生产后端使用正式 A2UI schema/catalog validation。
- [ ] GenUI Decoder 是真实独立模型调用。
- [ ] 浏览器实际用 A2UI MessageProcessor/Renderer 消费消息。
- [ ] 模型决定 dynamic task surface 的结构。
- [ ] 普通状态更新只发 `updateDataModel`，0 次 GenUI call。
- [ ] A2UI action 直接进入 TaskVM governance，不重新翻译成自由文本。
- [ ] Start/Pause/Stop/Checkpoint/Rollback 不由模型生成。
- [ ] bootstrap 后默认 Ready，不自动运行。
- [ ] 生产 UI 不含微信/支付宝/某 task 的硬编码入口。
- [ ] unseen goal 不需要改前端代码。
- [ ] readonly / unknown action / unknown binding 会被服务端拒绝。
- [ ] LocalPatch 最终走真实 CUA GUI gestures。
- [ ] fresh observe + independent verifier 决定完成，不信 agent 自报。
- [ ] final verifier PASS 才触发 🎉。
- [ ] snake progress 只跨越 verified milestone。
- [ ] rollback 后 snake 与真实 app 状态一起向后恢复。
- [ ] 同一 Catalog/Task Surface 架构可运行在至少两个 substrate 上。
- [ ] `prefers-reduced-motion` 被尊重。
- [ ] 全部新增依赖锁进 lockfile / pyproject。

---

# 20. 2026-08-19 owner 追加：渐进式任务面、分级模型路由、GenUI skills

> 本节为 owner 追加指令，与前文冲突处以本节为准（§0 的"中间模型"禁令按 20.2 的口径修订）。

## 20.1 渐进式任务面（A9.1 扩展）：先让用户看见"已经开始"

goal 提交的**瞬间**就渲染通用骨架，随编译链返回逐级 morph，不等初始化完成：

```text
T0 提交瞬间    ：goal 卡 + 单点 workflow 脉冲节点（"正在编译任务世界…"）+ 治理壳 Ready 禁用态
T1 compiler 返回：变量骨架落位（display_label 已知、值 pending 占位）
T2 architect 返回：单点 morph 成真实 DAG（节点逐个落位动画；verify/checkpoint 节点类型着色）
T3 decoder 返回  ：骨架替换为正式 A2UI 组件树（A5 落地后）
```

- 骨架必须是**通用**的（goal 文本 + 单点 + 阶段标签），**不得伪造计划内容**——未编译完成前不得显示任何假节点/假变量值（诚实 UI 铁律，与 §2 的 Ready 不自动 Start 兼容：骨架期间 Start 保持禁用）；
- morph 用 motion 的 layout animation；T0 本身 <100ms，T0→T2 体感 <1s。

## 20.2 分级模型路由：不处处用 GPT-5.6-sol

`workspace_ui/composition.py` 增加 role→model 配置路由（workspace_ui 权限内，无需 RFC）：

| 角色 | 默认 | 说明 |
|---|---|---|
| compiler / architect / cua（主链） | GPT-5.6-sol | 不降（与 bench 对齐） |
| intent_parser（A6 意图解析） | 小快模型（Qwen 级） | 自然语言意图 → GoalPatch/Patch/RollbackIntent |
| nl_polisher（结构化 action → 自然语言呈现） | 小快模型，可选开关 | 关闭时走确定性模板 |
| genui_decoder（A4 结构生成） | GPT-5.6-sol | 可降小快模型 |

**对 §0 禁令的修订口径**：原文"绝对不要再增加'用户点控件→LLM 重新翻译成自然语言→CUA'的中间模型"针对的是**语义改写**（有损往返，治理语义必须结构化直达 governance 层——这条不变）；本节允许的是**呈现润色**——小模型只做"结构化→自然语言描述"的渲染，**不得增删改任何语义字段**；所有调用走 ModelPort + ledger（记录 model id，诚实记账），可一键关闭。依据（owner 2026-08-19）：GPT 类 CUA 对自然语言分布的适配优于结构化描述；润色用小模型成本可忽略，且改善首 token 体感。

## 20.3 GenUI decoder 的 skills

`taskvm/genui/skills/`：GenUI decoder 的蒸馏经验目录。格式、反作弊边界、freeze 锁死协议与 bench_design §17.2 对齐（蒸馏源=APP 线 development split 的成功轨迹；禁止含 frozen task GT）。owner 立场：skill 是 harness 性能提升的关键一步，不是作弊——**所有角色（compiler/architect/cua/verifier/genui）都应有各自 skill**。
