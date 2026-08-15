# Coding Agent D：Projection Runtime 与可用前端

## 你的唯一任务

把当前不可用、易 405、刷新式和模型重渲染式的 Workspace UI 重构成稳定的 TaskVM Projection UI。它必须展示任务层状态、workflow 与多 substrate 进度，并能按需展开最新真实截图。

你不负责 CUA 怎么点，也不负责设计 model prompt；只消费 kernel/runtime 公开的 snapshot 与 event stream。

依赖：Agent A 的 ProjectionSchema/Store/Event 类型；Agent C 的 TaskArchitecture schema；Agent E 的 runtime event/screenshot artifact contract。

---

## Owned paths

建议迁移为：

```text
taskvm/projection/
  app.py
  routes/
  services/
  presenters/
  templates/
  static/css/
  static/js/
  event_stream.py
  view_models.py

tests/projection/**
tests/e2e_ui/**
```

你可以删除/迁移：

```text
taskvm/workspace_ui/**
```

不要在 projection 中 import concrete substrate 或 benchmark fixture。

---

## 当前明确 Bug

### 405 Method Not Allowed

当前 `editable_components.py` 与 `server.py` 生成：

```html
<form method="post" action="edit">
<form method="post" action="undo/calendar">
<form method="post" action="checkpoint">
<form method="post" action="adopt_milestone">
<form method="post" action="resolve">
```

当当前 URL 为 `/<sid>` 而不是 `/<sid>/` 时，相对 URL `edit` 被解析为 `/edit`，POST 命中 GET-only 动态路由后产生 405。

必须：

- 所有 route 由 Flask `url_for` 或统一 API client 生成；
- 不手拼相对 action；
- 为每个按钮写 HTTP method + status contract tests；
- 页面错误显示业务可理解信息，不展示 Flask 默认 “Method Not Allowed” 页。

### God server

当前 `workspace_ui/server.py` 1300+ 行，同时做页面、模型、workflow、fixture seed、执行、rollback、SSE。必须拆分；projection server 不得自行规划或执行。

### GenUI 热循环

当前页面 render 会调用 `_genui_rw_zone_html()`/decoder。改为：

```text
ProjectionSchema：结构生成后持久化
ProjectionData：runtime 持续增量更新
Frontend：只应用 delta
```

普通数值、进度、截图变化 0 次模型调用。

---

## 用户界面信息架构

### 1. 顶部 Governance Bar

- 任务标题/当前 goal 摘要；
- autonomy 状态：running / safely pausing / paused / replanning / rolling back / complete / failed；
- Start/soft pause/emergency stop/resume；
- 模型调用与执行预算简洁提示；
- GoalPatch 入口。

### 2. Task State Projection

- 业务变量；
- current / desired / conflict 状态；
- 影响的 surface 数量；
- 已验证标记；
- editable control 由 ProjectionSchema 生成；
- LocalPatch 提交后立即显示 pending，不等整页 reload。

### 3. Workflow Map

必须动态可视化：

- Sequence：水平/垂直有向进度；
- Fan-out：一个语义节点展开多个 lane；
- Barrier/Fan-in：各 lane 验证后汇合；
- Bounded Loop：显示 iteration、termination condition、max；
- Milestone/checkpoint 是业务标题，不是 `step 1`；
- 节点状态：waiting/running/agent_done/verified/conflict/rolled_back/failed/irreversible。

前端只渲染后端 workflow view model，不在 JS 中自己猜拓扑。

### 4. Surface / App Cards

默认每张卡只显示：

- 用户可见 surface 名；
- 当前正在做的高层目标；
- 最新 observation 时间；
- lane/node 状态；
- verified/failed/conflict。

点击卡片：

- 立即读取 Runtime Artifact Store 中已有的最新 screenshot；
- 显示最近若干 CUA actions 与可见状态摘要；
- 不启动新模型、不重新执行、不主动刷新目标 App；
- 如果没有截图，明确显示“尚无观察”，不能报 500。

新 screenshot 到达时，已展开卡片通过 event stream 更新。

### 5. Checkpoint 与 Rollback

- 可视 timeline；
- 明确区分 committed/available/rolling back/partial failure；
- 不可逆影响显示锁图标和业务文案；
- rollback 后展示逐 surface 结果，而不是只 toast “success”。

---

## 前后端通信

建议：

- 命令：普通 JSON POST API；
- 状态：SSE 单向 event stream；
- screenshot：artifact endpoint 或静态受控 URL；
- 初始加载：一次 snapshot GET；
- JS 在本地维护 `last_event_id`/revision，乱序或重复 event 可丢弃。

SSE 事件至少：

```text
projection_delta
workflow_delta
runtime_status
surface_observation
screenshot_available
verification_result
checkpoint_committed
conflict
error
```

不要把每 5 秒整页 reload 当同步。断线后自动重连并从 snapshot/revision 恢复。

---

## Route Contract

建议统一：

```text
GET  /sessions/<sid>
GET  /api/sessions/<sid>/snapshot
GET  /api/sessions/<sid>/events
GET  /api/sessions/<sid>/artifacts/<artifact_id>
POST /api/sessions/<sid>/start
POST /api/sessions/<sid>/pause
POST /api/sessions/<sid>/stop
POST /api/sessions/<sid>/local-patches
POST /api/sessions/<sid>/goal-patches
POST /api/sessions/<sid>/rollback
POST /api/sessions/<sid>/conflicts/<id>/resolve
```

命令成功返回明确 JSON 和 revision；异步操作返回 accepted 状态。采用 Post/Redirect/Get 或纯 fetch API，不能一部分表单 reload、一部分 JSON、一部分相对 URL。

---

## 动画与视觉

当前 `style.css`、`timeline.js`、`workflow_anim.js` 有不少类和动画，但集成不完整。不要机械保留旧 CSS；以行为为准：

- 节点状态变化有轻量过渡；
- fan-out lane 在启动时展开、barrier 在验证后汇合；
- loop iteration 更新清晰但不过度闪烁；
- checkpoint 成功有克制的成就反馈；
- conflict/irreversible 不使用庆祝动画；
- 支持 `prefers-reduced-motion`；
- 小屏可用。

不要把动画当完成标准的替代品。

---

## 测试

### Flask route tests

遍历页面中所有 form/button/API action，断言：

- URL 包含正确 sid；
- method 正确；
- 不出现 405；
- invalid input 返回结构化 4xx；
- unknown sid 返回可理解 404。

### UI E2E

用 Playwright 测试 TaskVM 自己的前端：

1. 打开 session；
2. Start；
3. 收到 workflow delta；
4. 展开 surface 卡，看到已有 screenshot；
5. 提交 LocalPatch；
6. pause/resume；
7. GoalPatch 进入 replanning；
8. rollback；
9. SSE 断线重连；
10. 全程无 console error、page error、405/500。

### Model-call regression

以 fake Task Architect 计数：连续 20 个 projection data delta 不增加 architect/GenUI call count。

---

## 明确不做

- 不从 frontend 直接调用 substrate。
- 不在 route 内运行长时间 CUA；只发布 command。
- 不在 render 中调用模型。
- 不泄露 entity ID/operator/saga ID。
- 不保留 static f-string editable fallback 或 `--no-genui` production path。

---

## T2 债务拆除（Oracle audit B-F1 指派给 D；2026-08-16 追加）

`docs/contracts/substrate.md` §8 Transitional Debt Register 的 **T2** 归你：
`taskvm/workspace_ui/server.py::_make_anchor_lookup` 当前让 runtime decision chain
间接消费 `env.oracle_state(sid)`（hidden entity_id → visible title → GUI 目标
anchor），违反 substrate 合同"runtime 不得消费 oracle"。拆除路径：

1. 删除 `_make_anchor_lookup` 及其注入点（含 rollback path 上的注入）；
2. runtime 寻址改为：`SubstrateSession.observe()` → visible evidence →
   State Compiler（fast path: `extract_observed`/`rebind`；slow path:
   `compile`）→ `SurfaceHandle`——注意 C-F1 修复后 `needs_slow_path` 的阶梯
   语义（指纹变化但 handle 可恢复 = 0 model call）；
3. 与 Agent E 协调 `workspace_ui` 的 runtime bootstrap（E 删除
   `execution/gui_driver.py` 时会移除 `mobilegym_bridge_url` 过渡 helper，
   你需要改为从 provider config / bootstrap 显式接线，不要重新发明
   facade helper）；
4. 完成后同步收缩 `tests/substrate/test_no_api_backdoor.py` 的
   `TRANSITIONAL_DEBT_REGISTER`（T2 条目删除）并跑
   `pytest tests/substrate -q`（默认 CI 不得出现 NEW violation）。

不要顺手重写 substrate 或 runtime（one-owner）；你只拆 UI 侧的 oracle 依赖。

---

## 验收

```bash
pytest -q tests/projection tests/e2e_ui
```

另提供一段 2–3 分钟可复现 screen recording 脚本说明：初始化 → autonomy → fan-out → 展开截图 → LocalPatch → GoalPatch → rollback。录屏本身不进 git。
