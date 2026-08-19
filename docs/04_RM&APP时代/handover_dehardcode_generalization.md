# 交接工单：TaskVM 全层去硬编码 + 历史叙事清零（PURITY-GEN）

## 角色

你是 coding agent。本工单由 HashTAG_00001 于 2026-08-19 亲自下达，**本工单本身就是对冻结层修改的 RFC 授权**（Repository Contract §5 要求的 RFC 由此满足，执行时在 commit message 引用 `PURETY-GEN` / `RFC-PURITY-GEN`）。

---

## 0. 铁律：先读懂再动手

以下所有判断必须**先读代码再执行**，本文档给出的行号是 2026-08-19 快照，可能有 ±10 行漂移；以 grep 到的符号名为准。

---

## 1. 背景：为什么有这个工单

TaskVM 的定位是**面向开放场景的学术 prototype**（`docs/A2UI_开工大纲_v0_心智模型对齐版.md` §7：理想的 TaskVM 应能面对未见任务、未见界面和不同 substrate）。但当前仓库存在三类与该定位冲突的实现：

1. **历史叙事注释**：58 个 Python 文件、约 558 处注释包含 episode 编号（E-xx）、任务编号（TaskN）、agent 编号（Agent A/B/C/D/E）、审计编号（A-0x/B-0x/C-x/RM-x/D-Fx）、日期叙事（2026-08-xx）、"legacy/历史/此前/后来/现在改成"式演化故事。这些注释把"考古学"混进"说明书"，让读者无法判断哪句描述的是**现在的代码**。
2. **场景特异硬编码**：按 app / 按任务 / 按场景的枚举与分支，限制了开放场景能力（详见 §3 坐标清单）。
3. **规则式验证与治理**：写回验证、回退判定目前依赖确定性规则查表；owner 的立场已明确：**优先保障泛化性（model-based verification），在模型能力边界内诚实报告不可验证/不可逆，制约因素应是模型能力而非 harness 侧枚举**。真实开放场景下 100% 可审计性不存在（真人也做不到），学术 prototype 接受这一取舍。

**owner 原话（2026-08-19，不得弱化）**：
> "我的 taskvmAPP 必须面向开放场景！除了 substrate 用的是 mobilegym 而不是真机以外，必须在 taskVM 的每一层、每一个地方，都要有完完全全真真实实不做妥协的泛化性，禁止枚举，禁止分支！验证+回退不能全交给模型动态生成不是借口，让模型 verifier 去验证，不要用规则！优先保障泛化性（model-based verification），在此基础上尽量诚实审计不可回退，这样制约的就是模型能力而非 harness 侧。governance 的核心也是要灵活可变的，不能假定人只有这些意图，所以才要有模型在 agenticUI 侧理解人的意图。"

### 1.1 实测证据（同日全链路 demo，比任何论证都硬）

同日对 APP 做了 6 次真实任务注入（含 bench fixtures 的 demo goal 原文），**编排层 0/6 通过**，18 次真实 gpt-5.6-sol 调用的完整 prompt/response 已落盘：

- 档案：`eval_results/taskvm_demo_run_20260819/`（15 个 call txt + 截图 + INDEX + run_summary.json）
- 三类刚性拒绝点全部实测复现：
  1. **"每个 action 必须有非空 sets"**（命中 4/6）——模型把导航型（打开 app）、信息获取型（查询账单，金额变量 desired=null 运行时才可知）、触发型（点击发送，变量已被前序动作写完）动作建模为 sets:{}，语义全部正确，全部被杀；
  2. **"sequence 子节点必须单链"**（命中 2/6）——带汇合/分叉的依赖图被判 fork；
  3. **bounded repair=2 轮不够**——模型坚持正确语义，诚实失败（无 fallback，符合设计承诺）。
- 定性：**harness 侧刚性 schema 制约模型，而非模型能力不足**。本工单的 §4 必须优先修复此点，否则任何自然语言任务都无法进入执行阶段。

---

## 2. 任务 A：历史叙事注释清零

### 2.1 范围

`taskvm/` 与 `taskvm_bench/` 下全部 `*.py`（不含 `__pycache__`、不含 `docs/`、不含测试 fixtures 中**作为数据**的字符串）。

### 2.2 判别口令

对每一处注释问一句：**"删掉这段叙事之后，剩下的文字能否让一个第一次读代码的人准确理解现在这段代码做什么？"**

- 能 → 只删叙事部分，保留功能性描述。
- 注释整段只剩叙事（如"（E10 审计发现此前……）"）→ 整段删除。
- 注释里既有叙事又有规格（如"折返验证：读 fresh observation（B-1 引入，2026-08-11 重写，此前是……）"）→ 改写为"折返验证：读 fresh observation"。

### 2.3 必须清除的叙事模式（grep 目标）

```
E[0-9]{1,2}[.．]?\s       Task[0-9]      Agent [A-E]
B-0[0-9]                  A-0[0-9]       C-[0-9]
RM[-.][0-9A-Za-z.]+       D-F[0-9]       2026-08-[0-9]{2}
legacy                    此前|后来|曾经|历史遗留|老版本|旧版
" Wave-[0-9]              kill-?test 编号叙事
```

执行后验证命令（应输出 0）：

```bash
grep -rn "E[0-9][0-9]*\|Task[0-9]\|B-0[0-9]\|A-0[0-9]\|2026-08" \
    --include="*.py" taskvm/ taskvm_bench/ | grep -v __pycache__ | wc -l
```

**注意**：docstring 里引用**现行合同文档**的条款号（如 `contract §4`、`substrate.md §2`）是合法的——那是规格引用，不是历史叙事，保留。`.mrules` / `.mrules.log` / `docs/` 不在清理范围（已冻结的历史档案）。

### 2.4 测试文件中的叙事

测试名与断言里的编号（如 `test_b09_antibypass`）可以保留**函数名**（改名会破坏 CI 引用），但 docstring 改写为描述"本测试锁什么不变量"。

---

## 3. 任务 B：消灭场景特异硬编码（能力债清零）

### 3.1 判别试金石（唯一标准）

> **这个枚举/分支/白名单，限制了"模型或用户能对世界做什么"吗？**

- 限制了 → **删掉或泛化**。
- 它只是防泄漏/防后门（阻止模型看到内部词汇、阻止绕过 GUI）→ **保留**（那是 Governance 锚点，不是能力限制）。
- 它是动作协议词汇表（click/type/scroll/key/wait/open 七种 GuiAction kind）→ **保留**（这是协议的字母表，等价于"鼠标只能点击移动"，不是场景特异；任何 app 的任何操作都由这七个原语组合表达）。

**"禁止枚举禁止分支"的准确含义**：禁止 `if app == "wechat"`、禁止 per-app 路由表、禁止固定 operator 白名单（`send_message`/`toggle_like` 这种）、禁止"任务模板库"。**不**意味着删除动作协议或错误码分支。

### 3.2 坐标清单（已审计，按文件）

#### taskvm/domain/architecture.py（TaskArchitecture 验证构造器）——**P0，实测阻断点**

| 位置 | 现状 | 处置 |
|---|---|---|
| action 节点非空 sets 强制 | 误杀导航型/信息获取型/触发型动作（实测 0/6 编排通过，见 §1.1） | 允许 sets 为空的 action（观察/触发型）；改为"含至少一个写变量的 action 的任务才合法"这种任务级检查，而非节点级 |
| sequence 单链强制 | 带分叉/汇合的自然依赖图被判 fork | 允许 sequence 内分叉与汇合（DAG），或引导模型用 fan-out+barrier 表达（两轮 repair 内给足反例） |
| bounded repair = 2 轮 | 实测不够 | 提高到 3-4 轮并把具体拒绝原因作为反例回喂（已有机制，确认拒绝信息足够具体） |

#### taskvm/substrate/mobilegym/bridge.py

| 位置 | 现状 | 处置 |
|---|---|---|
| L103 `APPS = ["wechat","alipay","x"]` | open 白名单只有 3 app | 改为全量 27 app 目录（见 `docs/handover_full_app_integration.md` §3.1-3.2 的 `app_catalog.py` 方案——那份工单的子任务 A 与本工单合并执行） |
| L383-428 `read_resource` | 只支持 wechat_chats/alipay_transactions/x_posts 三个 resource | 泛化为 `GET /api/app_state/<sid>/<app_id>`（任意 app 的原始 store 读），旧 resource 路由保留为兼容别名 |
| L601-612 `session_state` | 摘要硬编码 wechat/alipay 字段 | 改为遍历 catalog 生成的通用摘要（每 app 的 top-level list 长度计数），不再逐 app 写死字段名 |
| L615-719 `mutate_wechat` | operator 枚举 `send_message`，wechat 专用 | **重构为通用 mutate**（见 §4） |
| L730-947 `mutate_x` | operator 枚举 toggle_like/retweet/bookmark，x 专用 | 并入通用 mutate |
| L960-1014 `html_view` | 只渲染 wechat/alipay 两视图 | 通用化：由 app_state + catalog 元数据渲染任意 app |

#### taskvm/substrate/mobilegym/evaluation.py

| 位置 | 现状 | 处置 |
|---|---|---|
| L35-38 `_RESOURCE/_ID_FIELD/_ENTITY_KIND` | 只映射 3 个 app | 保留为 legacy 语义投影（向后兼容），新增 `app_state(sid, app_id)` / `os_state(sid)` 通用 oracle（full_app_integration 工单 §3.5） |

#### taskvm/substrate/mobilegym/provider.py / session.py

| 位置 | 现状 | 处置 |
|---|---|---|
| `cfg.get("app", "wechat")` 默认值 | 默认 app 写死 wechat | 默认值改为 catalog 首个 app 或必填校验（`is_valid_app_or_raise`），display_name 用 catalog 的中文名 |
| session.py L43 `surface_app: str = "wechat"` | 同上 | 同上 |

#### taskvm/workspace_ui/app_open.py

| 位置 | 现状 | 处置 |
|---|---|---|
| `--apps` 默认 `"wechat,alipay"` | hero 只展示 2 app | 默认全量（full_app_integration 工单 §3.6），按 catalog 分组渲染 |

#### taskvm/workspace_ui/static/js/taskvm.js + index.html

| 位置 | 现状 | 处置 |
|---|---|---|
| `renderAll()` 固定六面板；grep "schema" 零命中 | **后端模型生成的 projection schema（view_models.py `projection_schema_view` 已输出）前端根本没消费** | **P4 收尾**：新增 schema-driven 渲染函数，按模型输出的组件树（root/children/type/binds/editable）渲染主体区；固定面板降级为 schema 缺失时的 fallback。前端不得新增任何"按 app/按任务"的分支 |

#### taskvm/verifier/（现状 `visible.py` VisibleVerifier）

| 现状 | 处置 |
|---|---|
| 纯规则式可见性验证 | **新增 ModelVerifier**：VLM 读 fresh screenshot + 验证意图 → 判定 changed-happened / not-yet / cannot-verify（诚实三态）。规则检查降级为 cheap pre-filter（如 fingerprint 未变可直接 short-circuit 返回 not-yet，省一次模型调用），**不得**作为最终判定否决模型结论 |

#### taskvm/governance/

| 现状 | 处置 |
|---|---|
| GoalPatch/rollback 语义路径固定 | 保留结构（这是 VM 的控制流原语），但**意图解析模型化**：用户在 GenUI 的自由文本输入 → 轻量模型解析为 GoalPatch/Patch/RollbackIntent（现有 `local_patch` 只接受结构化 updates 的路径保留为程序化 API） |

#### 保留不动的（Governance 锚点，误删即倒退）

- `taskvm/architect/noleak.py` 禁词表——它防的是内部词汇进 prompt（GUI-only 判定口令的执行者），不限制模型能力。
- `bridge.py` 的 `_require_active`/`_activate` 会话隔离——防的是运行时偷换现实（反旁路），不是能力枚举。
- `port.py` `GUI_ACTION_KINDS` 七原语——动作协议字母表。
- 反旁路测试（`test_mobilegym_antibypass.py` 等）——它们锁的就是"不许走后门"，与泛化同向。

### 3.3 参考既有工单

`docs/handover_full_app_integration.md`（MG-FULL-APPS）已覆盖上表前四项的详细方案与测试要求，本工单不重复；执行顺序建议：先 MG-FULL-APPS 子任务 A（catalog+bridge），再做本工单的 verifier/governance/前端 schema 消费。

---

## 4. 任务 C：写回与验证的模型化（通用 mutate + ModelVerifier）

### 4.1 通用 mutate 路由

替换 `POST /api/wechat/<sid>/<eid>` 与 `POST /api/x/<sid>/<eid>` 为：

```
POST /api/mutate/<sid>
{"app": "<app_id>", "entity_ref": "<用户可见的实体标识，如聊天名/帖子标题>",
 "intent": "<自然语言意图，如 '发送文本：今晚开会' / '点赞' / '把标题改成X'>"}
```

实现要求：
- 内部复用现有 CUA grounding loop（`gui_write_async` / `gui_act_async` 已是模型驱动的通用循环，无 app 分支）；
- 执行后调用 ModelVerifier（§4.2）读 fresh observation 判定意图是否达成，达成才返回 `{"status":"ok", "evidence": ...}`；
- undo：`{"intent": "...", "undo": true}` → 同一循环，模型现场规划撤销手势；判定不可达 → 诚实 `409 irreversible`（这条语义保留——它不是枚举，是诚实边界）；
- wechat/alipay/x 旧路由 302 到新路由（兼容期一个 commit 长度，之后删除）。

### 4.2 ModelVerifier 契约

```
输入：验证意图（业务语言）+ fresh Observation（截图 + 可见文本）
输出：{"verdict": "changed" | "not_yet" | "cannot_verify", "evidence": "<引用屏幕可见证据>"}
```

- 三态里的 `cannot_verify` 是**诚实输出**，不是失败——它就是"模型能力边界"的显式呈现（owner 立场：制约在模型侧，不在 harness 侧）。
- 走 ModelPort + ModelCallLedger 记账（role 扩展 `model_verifier`，需在 `port.py MODEL_ROLES` 注册——这是协议常量追加，不是场景枚举）。

### 4.3 验收

新增测试（fake model port，不起真栈）：
- 通用 mutate 对任意 app（含 calculator 这类无 store 的）不发 app 特异分支；
- ModelVerifier 三态各有路径；
- 旧路由兼容 302；
- noleak：mutate 路由的 prompt 不含内部词汇。

---

## 5. 测试与回归（零回归铁律）

- 既有全量 pytest 必须绿：`tests/`、`taskvm_bench/tests/`。
- 架构门（substrate 不 import taskvm_bench 等）不放松。
- 注释清理不得改变任何行为：`git diff` 中代码行（非注释/docstring 行）应为零变化——**逐文件 review，纯注释 commit 单独提**。
- E2E 验收（真栈，主 agent 执行）：`./scripts/app_mobilegym.sh --reset` 后用一个**未见过的任务**（不使用 fixtures 里的 demo goal，例如"打开日历看本周日程，把最重要的一件事用微信发给黄勇"）全链路跑通，证据落 `eval_results/*.json`。

---

## 6. Git 纪律

- 一个 commit 一个性质，引用工单号 `PURETY-GEN`：
  1. `docs(substrate): PURETY-GEN RFC — de-hardcode + generalization spec`（本文档）
  2. `chore(comments): PURETY-GEN — strip historical narrative from comments (58 files)`（纯注释）
  3. `feat(substrate): PURETY-GEN — full-app catalog + generic app_state/os_state routes`（合并 MG-FULL-APPS-A）
  4. `feat(substrate): PURETY-GEN — generic model-driven mutate route`
  5. `feat(verifier): PURETY-GEN — ModelVerifier (three-verdict, model-based)`
  6. `feat(workspace_ui): PURETY-GEN — schema-driven projection rendering (P4 close-out)`
  7. `test: PURETY-GEN — regression + OOD evidence`
- 只 add 自己改的文件；`eval_results/`、`*.png` 不进 staging。

---

## 7. 完成定义

- [ ] §2.3 验证命令输出 0
- [ ] §3.2 坐标清单逐项处置完毕（删/泛化/标注保留理由）
- [ ] 通用 mutate + ModelVerifier 上线，旧 per-app 路由删除
- [ ] 前端消费 projection schema（P4 收尾）
- [ ] OOD 任务（未见 goal）E2E 跑通，证据落盘
- [ ] 全量 pytest 绿
- [ ] 每个 commit 纯净（注释 commit 零代码变化）
