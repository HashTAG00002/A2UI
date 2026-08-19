# TaskVM 双线总调度交接文档（agentRM × agentAPP）· 2026-08-19

> **本文档是 RM&APP 时代的唯一主调度文档。** 与其他任何历史文档冲突时，以本文档为准（`.mrules` 与 `docs/contracts/` 冻结合同除外）。
> 旧路径对照：本文档中的所有 docs 路径均为 2026-08-19 目录重组后的新路径（见 §8 文档地图）。

---

## 0. 你是谁、你读什么

| | **agentRM**（开放 bench 线） | **agentAPP**（A2UI 协议 + APP 线） |
|---|---|---|
| 使命 | 把 "harness 不制约模型" 变成可复现的数字（CHI 证据层） | 把 "模型生成动态任务界面 + 固定治理壳" 做成真产品（CHI 原型层） |
| 主文档（唯一） | 本文档 + `docs/04_RM&APP时代/bench/RM.1.C.bench_design.md` | 本文档 + `docs/04_RM&APP时代/frontend/TaskVM_A2UI_Coding_Agent_Workplan_2026-08-19.md` |
| 宪法（两线同） | `.mrules` + `docs/contracts/*` | 同左 |
| 坐标清单 | `docs/04_RM&APP时代/handover_dehardcode_generalization.md` §3.2 | 同左 + workplan §1–2 |

**两线共同的开发纪律（摘自 .mrules，违反即返工）：**
- GUI-only，无内部 ID 暴露；写/回退必须走真实 GUI 手势
- `taskvm/` 零引用 `taskvm_bench/`（架构门锁定）
- 冻结层（domain/kernel/architect/substrate/governance/runtime/projection）修改需 RFC——`handover_dehardcode_generalization.md` 本身就是 PURETY-GEN 的 RFC 授权
- 验证性结论必须附 `eval_results/*.json` 原始字段；发现失败必须诚实报告；Gate 判据用 mean/majority 不用 max
- commit 只 add 自己本会话改的文件，禁 `git add -A`；`eval_results/`、`*.png` 不进 staging；一个 commit 一个性质
- 运行环境：conda env `taskvm`（Python 3.10）；Node 22 已装在 env 内；Playwright 浏览器在 env 的 `opt/ms-playwright`；npm/pip 走代理 `http://10.70.16.106:3128` + 官方源

---

## 1. 现状一页纸

### 1.1 已完成的地基（不要再碰，只复用）

| 板块 | 状态 | 证据（git SHA / 路径） |
|---|---|---|
| 六层架构 + 冻结合同 | 全绿 | 597 passed，架构门锁定 |
| RM-0.A 正确性收口 | 完成 | `4ddaa5d` A-01 多 surface resolver（EvidenceSurfaceResolver 进 composition 主路径）+ A-02 stop 竞态（stale-on-arrival，stop 后 0 GUI writes） |
| RM-0.B 真模型基建 | 完成 | `83c891d` B-11 trial 完整性 + stage funnel；`d8cf544` B-06 真模型条件系列；B-09 反旁路（RM 主路径静态禁用 semantic mutate 路由） |
| real-full 编排链 | 打通 | `bootstrap_real_full`（`taskvm/workspace_ui/composition.py`）：NL goal → StateCompiler → TaskArchitect → Kernel → Runtime → CUA → GUI → Verifier |
| TaskVM APP 壳 + MobileGym 启动器 | 完成 | `9ca9d6b` `app_open.py` + `scripts/app_mobilegym.sh` |
| 模型调用档案器 | 完成 | `taskvm/workspace_ui/call_archive.py`（`TASKVM_CALL_ARCHIVE_DIR` 环境变量启用；18 次真实调用已归档） |
| 前端 island 环境 | 完成 | `taskvm/workspace_ui/a2ui_client/`：Node 22 + React 19 + @a2ui/react 0.10.2（`./v0_9` 入口）+ @a2ui/web_core 0.10.6 + motion + canvas-confetti + vitest，lockfile 已锁 |
| builtin_web 双链路 | 完成 | `taskvm/substrate/builtin_web/`（calendar，端口 3013）+ demo_open 双 substrate 启动器 |

### 1.2 唯一硬阻塞：架构师 schema 刚性（W0.2，今天就修）

**实测证据：6 个真实任务注入，编排层 0/6 通过，CUA 一次都没被调用。** 18 次真实 gpt-5.6-sol 调用的完整 prompt/response 逐字落盘在 `eval_results/taskvm_demo_run_20260819/`（15 个 call txt + 5 张截图 + INDEX + run_summary.json）。

三条刚性规则（拒绝点全部实测复现）：

1. **"每个 action 必须有非空 `sets`"**（`taskvm/architect/architect.py:542-547`）——命中 4/6。模型把导航型（打开支付宝）、信息获取型（查询最大支出，desired=null 运行时才可知）、触发型（点击发送，变量已被前序 action 写完）动作建模为 `sets:{}`，语义全部正确，全部被杀。
2. **"sequence 必须单链"**（`taskvm/domain/workflow.py:131-168` `_check_primitive_shapes`）——命中 2/6。带分叉/汇合的合法依赖图被判 fork。
3. **bounded repair 轮数不足**（`taskvm/architect/architect.py:201` `max_repairs=1` → 共 2 次尝试）——模型两轮都坚持正确语义，然后诚实失败（无 fallback，符合 "NO fallback — ever" 承诺）。

**为什么这导致 CUA 完全不被调用**：管线顺序是 goal → StateCompiler（模型调用①）→ TaskArchitect（模型调用②）→ schema 验证 → 失败则 repair（模型调用③）→ 仍失败则 `ArchitectOutputError`——计划是 CUA 的"施工工单"，没有工单 Runtime 永不启动，CUA 模型（工种④）连一张截图都看不到。18 次调用 = 6 state_compiler + 12 task_architect + **0 CUA**。定性：**瓶颈在 harness 刚性 schema，不在模型能力**（RFC-A01 已立案：`docs/contracts/architect_rfc_backlog.md`，git `49a2649`）。

### 1.3 已就绪、等接线的东西

- 后端已输出模型生成的 projection schema（`taskvm/projection/view_models.py:109` `projection_schema_view`），但前端**零消费**（grep "schema" 在 `static/js/taskvm.js` 零命中）——"GenUI 输出前端消费不了"的准确坐标
- A2UI v0.9 协议镜像缺 4 个 list/wrapper schema（`docs/A2UI-protocol-spec/v0_9/json/`），本地 `sample.json` 引用了不存在的 `server_to_client_list.json`；`a2ui_protocol.md` 缺 catalog `$id == catalogId` 澄清
- bench 侧两套 ontology 未接通：`taskvm_bench/benchmark/schema.py` 有 12 Family + 15 TaskSpec，但真正跑 MobileGym 的 `mobilegym_fixtures.py` 仍是老 `CanonicalTaskGraph`（仅 3 scenario）；`evaluation/user_ops.py:166-171` 的 `world_diff/protected_diff` 硬编码 None；`TrialRecord.finalize()` 的 "all ops applied => pass" 是假 verdict
- MobileGym bridge 白名单只有 3 app（`taskvm/substrate/mobilegym/bridge.py:103` `APPS = ["wechat", "alipay", "x"]`）；oracle 扁平化只支持这 3 个 app 的状态结构
- osworld substrate 有诚实实现骨架（`taskvm/substrate/osworld/`，无 endpoint 时 fail-close 不装假桌面）——25 天窗口内不进正式 suite

### 1.4 待办杂务（不挡路，但别拖）

工作树未提交产物（截至 2026-08-19）：`call_archive.py`、`app_open.py`(M，接线改动)、`a2ui_client/`（环境）、两份 handover 工单、本目录的 frontend/ 三份文档、本重组的 rename staging。按 git 纪律分批落盘（见 §7 所有权矩阵）。`eval_results/` 永不进 git。

---

## 2. 四份旧工单 → 两线合并宣告（"乱"的终结）

| 旧工单 | 去向 |
|---|---|
| `handover_dehardcode_generalization.md`（PURETY-GEN）任务 B-P0（schema 刚性） | → **W0.2 闸门**（agentRM 今天执行） |
| PURETY-GEN 任务 B 其余（bridge/provider/session 硬编码）+ 任务 C 通用 mutate | → **agentRM · R3 substrate 开放** |
| PURETY-GEN 任务 C ModelVerifier 三态 | → **agentRM · R4** |
| PURETY-GEN 任务 A 注释清零（58 文件/558 处历史叙事） | → **两 agent 各清各的目录**（纯注释 commit 先行，零代码行变化） |
| PURETY-GEN 任务 C governance 意图模型化 + 前端 schema 消费（P4） | → **agentAPP · A6**（P4 被 A2UI island 吸收，不再修旧静态页） |
| `handover_full_app_integration.md`（MG-FULL-APPS） | → **agentRM · R3**（整单并入） |
| `frontend/TaskVM_A2UI_Coding_Agent_Workplan_2026-08-19.md` | → **agentAPP 整条线**（A1–A8 的原文依据） |
| `bench/RM.1.C.bench_design.md` | → **agentRM 整条线**（R1–R9 的原文依据） |

从现在起，每个 agent 只需要读：**1 份主文档 + 1 份宪法 + 1 份坐标清单**（§0 表格）。

---

## 3. 总 DAG

```text
━━━ 第0层 · 今天 8/19 · 串行闸门 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 W0.1 归档提交（杂务，不挡路）
 W0.2 架构师 Schema 解放 [agentRM，半天]
      │  允许 sets:{} 观察型动作 · 允许 DAG 分叉汇合 · repair 3-4 轮+反例回喂
      ▼
 ▓ GATE-ARCH ▓ 6 个 demo goal 重跑，≥5/6 活过 architect（call_archive 复档对比）
━━━ 第1层 · 今天起并行（零依赖，不等 GATE-ARCH）━━━━━━━━━━━━━━━━━━━━━
 [APP] A1 协议镜像 P0 修复          [APP] A2 删假开放入口 P0.5
 [RM ] R1 G0 grader 闭环（fake-port 单测先行）
 [RM ] R3 substrate 开放（与 W0.2 不同文件，同日可开）
 [APP] A9.0 全链路延迟审计（只测不改：时间瀑布 + sim:3000 冷启动/残留 daemon/端口占用排查）
━━━ 第2层 · GATE-ARCH 后（≈8/20）━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [RM ] R1 真模型收口 ══► ▓ GATE-G0 ▓ RM-C04-01 无 LLM-judge 自动判分【硬门：
 │        跑通前禁止写第2条 benchmark task】
 ├──► [RM ] R2 四硬契约模板（F1改目标/F3回退/F7+8暂停停止/F9漂移）8/21-22
 ├──► [RM ] R2.5 SKILL-LADDER 螺旋：L0 简单轨迹→蒸馏skill→L1 单干预→再蒸馏→L2 十族 dev anchor 8/22-25
 ├──► [RM ] R4 ModelVerifier 三态（并行，≤8/26 必须落地）
 └──► [APP] A3 genui 生产层 ◄── A1
          └──► A4 真 GenUI Decoder（3个未见goal→3棵不同组件树，前端零改码）
━━━ 第3层 · ≈8/22-25 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [APP] A5 React island + transport ◄── A3
 [APP] A6 governance 接线 + 意图解析模型化 ◄── A4+A5
 [APP] A7 动效（蛇形进度/🎉/回退倒放）◄── A5
 [APP] A9.1/A9.2 首响应体验 + 多APP实时截图墙 ◄── A5 + A9.0
 →  ★ APP pilot-ready ≈8/22（A1-A6 最小切片 + A9.1 基础响应性——卡顿的仪器会毁掉用户研究）
 [RM ] R5 剩余族 anchors（F2/F5/F6/F10）+ app 覆盖扩散 ◄── R2.5+R3（按 L0→L2 阶梯先易后难，每族通过即蒸馏）
 [RM ] R8-pilot 用户研究预试（4-6人）8/23-27 ◄── ★ APP pilot-ready
━━━ 第4层 · 8/26-28 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [RM ] R6 30 formal tasks + freeze v1.0 ◄── R4+R5     ▓ FREEZE 8/28 ▓（此后只修 blocking bug）
 [APP] A8 open-world kill tests + e2e 回环 ◄── GATE-ARCH + A4-A6 + R3
━━━ 第5层 · 8/29-9/3 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [RM ] R7 正式自动评测（60→120 paired runs，6条件诊断矩阵+funnel）◄── FREEZE
 [RM ] R8-main 用户研究 N=24 ◄── FREEZE + APP 成品
━━━ 第6层 · 9/4-10 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [RM ] R9 数据锁 + 统计 + 质性编码（9/4-6）──► 论文/视频/复现包（9/7-10）
```

**三条跨线依赖（全图仅此三处，记住就不会乱）：**
1. `GATE-ARCH → R1 真模型收口 / A8`（架构师闸门是公共前置）
2. `R3 substrate 开放 → A8`（APP 的 kill test 需要开放靶场）
3. `APP pilot-ready(≈8/22) → R8 pilot`；`FREEZE + APP 成品 → R8-main`（**用户研究的仪器就是 APP，这是最紧的跨线约束**：APP 若滑到 8/24，pilot 最多滑到 8/25，主实验起点不能动）

---

## 4. agentRM 线（面向开放 bench）· milestone 卡

**身份**：把 "TaskVM harness 不制约模型" 这个主张变成可复现的数字。主文档：`bench/RM.1.C.bench_design.md`。

> **⚠️ 术语钉死（owner 2026-08-19 要求强调）**：「10 条」= 10 个 development anchor = **每族 1 个完整运行轨迹**（1 个 Task Instance：初始执行 episode + 全部注入干预 episode，每个 episode 内含多个 user op，每个 op 内含多个 GUI action/checkpoint）——**不是** 10 个孤立任务变体、**不是** 10 个事件、**不是** 10 个用户动作、**不是** 10 个 GUI 动作。依据：bench_design §六评估层级（Benchmark Run→Task Instance→Governance Episode→User Operation→GUI Action）、§七"每族先做一个 development-only 样本"、§十一 Development anchors 原文"每个族一条，共约十条……目标是至少出现稳定的 full-chain success"。10 条 dev anchor 全过后才扩 10 族×3=30 formal tasks。**十锚点自动迭代的主场 = R2.5（L2 档）衔接 R5。**

| ID | 做什么 | 依赖 | 索引文件 | 验收 |
|---|---|---|---|---|
| **W0.2** | 架构师 schema 解放：允许 `sets:{}` 观察型/触发型 action 节点；允许 sequence 内 DAG 分叉汇合（或引导模型用 fan-out+barrier 表达）；repair 提到 3-4 轮并把具体拒绝原因作为反例回喂。注意：原设计意图是"每个 action 有治理把手（知道改了什么变量才能回退）"——修复应把检查从**节点级**放松到**任务级**（整个任务至少一个写变量的 action），不是删掉治理语义 | — | `taskvm/domain/architecture.py`、`taskvm/architect/architect.py`（验证器+prompt+repair guidance）、`docs/contracts/architect_rfc_backlog.md`（修复后关闭 RFC-A01）、`handover_dehardcode_generalization.md` §1.1/§3.2、`eval_results/taskvm_demo_run_20260819/`（baseline） | 6 demo goal 重跑 ≥5/6 活过 architect；call_archive 复档落 `eval_results/`；architect 合同测试同步更新 |
| **R1** | G0 grader 闭环：EvidenceBundle（oracle_seed、每次 intervention 前后 oracle 快照、oracle_final、public projection snapshots、runtime/event/write trace、checkpoint snapshots、injected events）+ 唯一判分入口 `grade_task(task_spec, evidence_bundle) -> ContractVerdict`（world_contract/governance_contract/projection_consistency/progress/failure_codes 五字段）+ 填 `world_diff/protected_diff` + 废除 "applied=pass" 假 verdict + 薄 adapter 让 MobileGym runner 消费 TaskSpec（废弃第二套 ontology）+ RM-C04-01 anchor（X app：找"核心CPI下降"帖点赞+收藏→checkpoint→rollback→判分，bench_design §九完整定义） | W0.2（真模型部分；fake-port 单测可先行） | `taskvm_bench/benchmark/schema.py`、`mobilegym_fixtures.py`、`evaluation/user_ops.py:166-171`、`results.py`、`funnel.py`、bench_design §九/§十一 | **无 LLM judge** 自动判分，所有 verdict 指回 `eval_results/<run-id>/` raw evidence；stage survival funnel 落盘 |
| **R2** | 四硬契约 predicate template：LOCAL_PATCH / ROLLBACK / PAUSE_RESUME+STOP / EXTERNAL_FIELD_CHANGE——按**干预类型**写通用模板（不是 per-task evaluator），GT 运行时自动生成（如 pause 的 GT = pause ack 时间点后零 TaskVM-caused writes） | R1 | `schema.py` ExternalEventKind、bench_design §三（GT 运行时自动生成的六个例子） | evaluator 单测全过；四族 dev anchor 并入 R2.5 L2 档执行 |
| **R2.5** | **SKILL-LADDER 难度阶梯 + skill 蒸馏螺旋**（owner 2026-08-19 指令：不要一上来满配迭代 10 条，先易后难逐档爬升，档间蒸馏）。**L0** 简单轨迹（无干预、单 app、1-3 步，纯 plumbing；复用已有 demo goal/fixture，不新写 benchmark task，与 GATE-G0 铁律相容）→ 蒸馏 skill v1 → **L1** 单一治理干预轨迹（一个 episode、一种干预类型，development_only TaskSpec）→ 蒸馏 skill v2 → **L2** 十族满配 dev anchor（多 episode、混合干预、跨 app/surface——"10 条"的主场，衔接 R5）→ 蒸馏终版 skill，随 freeze 锁死。**skill 机制**：每角色一个子目录 `taskvm/skills/{compiler,architect,cua,verifier}/`（GenUI decoder 的在 `taskvm/genui/skills/`，归 agentAPP）；格式=markdown（触发条件 + 通用领域/操作先验 + 从真实成功轨迹蒸馏的少样本）。owner 立场：**skill 是 harness 性能提升的关键一步，不是作弊**，设计期演化、最终在 bench 绑定锁死。**反作弊硬规则**：skill 只含通用世界知识/操作先验（如"支付宝账单入口在底部 Tab『我的』"），禁止含任何 frozen task 的 seed/success 谓词/protected 集/witness；蒸馏源仅 development split（held-out 变体永不参与——bench_design §十一防 cherry-picking 协议的天然延伸）；skill 集版本+内容 hash 写入 frozen manifest；论文如实披露（先例：AppAgent knowledge-based 操作模式）。**为什么必要**：纯 CUA 能完成的轨迹可能因治理不忠实被本 bench 判 FAIL——要求严于普通 CUA bench，模型角色多、上下文重，必须以 harness 先验（skill）逐步补位。**分级模型**：L0/L1 档迭代可用指定便宜模型（manifest 记 model id + development_only）；L2 档 sign-off 与正式 suite 必须 pinned 主模型。装载点在冻结层 prompt 组装（architect/compiler/cua/verifier），**本卡即 RFC 授权**（模式同 PURETY-GEN） | GATE-ARCH（L0 起步）；L1/L2 ← R2（predicate 就绪） | 新目录 `taskvm/skills/`、bench_design §17（2026-08-19 追加节）、各角色 prompt 组装点 | 每档真实执行 anchor + skill commit；蒸馏后同档重跑成功率提升有 ledger 证据；skill 文件 anti-leak grep 零 GT 字段；L2 档十族 dev anchor 全过 |
| **R3** | substrate 开放（合并 MG-FULL-APPS + PURETY-GEN B/C）：全量 app catalog（含系统级 app）、`GET /api/app_state/<sid>/<app_id>` 通用读、通用摘要、**通用 mutate 路由**（`POST /api/mutate/<sid>`，NL intent 替代 operator 枚举）、动态 app 发现、provider/session 去 wechat 默认值 | — | `handover_full_app_integration.md`（整单）、`bridge.py:103/383/601/615/730/960`、`evaluation.py:35-38`、`provider.py`、`session.py:43`、`handover_dehardcode_generalization.md` §3.2/§4.1 | 任意 app（含 calculator 这类无 store 的）零 app 特异分支；反旁路测试不放松；旧 per-app 路由 302 兼容后删 |
| **R4** | ModelVerifier 三态（changed/not_yet/cannot_verify）：VLM 读 fresh screenshot + 验证意图；规则检查降级为 cheap pre-filter（fingerprint 未变 short-circuit 返回 not_yet），不得作为最终判定否决模型结论；走 ModelPort 记账（role=`model_verifier`，在 `port.py MODEL_ROLES` 注册——协议常量追加不是场景枚举） | — | `taskvm/verifier/visible.py`、`taskvm/substrate/port.py`、`handover_dehardcode_generalization.md` §4.2 | 三态各有路径；fake model port 单测；**≤8/26 落地**（赶 R6 freeze） |
| **R5** | 剩余族 anchors：F2 fan-out（一个意图多 effect 全完成）/ F5 混合回退（可逆恢复+不可逆保留）/ F6 goal patch+replan（committed work reuse 率）/ F10 跨 surface（MobileGym+builtin_web 双靶场，action/verify/rollback 路由正确性）；app 覆盖向系统 app 扩散。**执行顺序按 R2.5 阶梯 L2 档：先易后难，每族通过即蒸馏 skill** | R2.5 R3 | `schema.py` Family、`tasks.py`（15→N TaskSpec）、`taskvm/substrate/builtin_web/`（calendar:3013） | 每族 ≥1 真实执行 anchor |
| **R6** | 扩 30 formal tasks（10 族×3），manifest/seed/hash/**skill set hash** 固定，freeze suite v1.0——skill 终版随 freeze 锁死，此后改动=新版本号+相关数据重跑 | R4 R5 | bench_design §十二/§十四/§17、`registry.py`、`configs/` | 8/28 后不因模型失败删 task；skill set 版本入 manifest |
| **R7** | 正式自动评测：TaskVM-full vs direct-cua-real vs planner-cua-real vs taskvm-template-control（11 conditions 见 `d8cf544`）+ taskvm-real-cua-only 诊断；60 paired runs 先行再补 120；三个 headline 指标 + funnel + failure analysis | R6 | bench_design §十、`evaluation/cli.py`、`funnel.py` | 机器可读结果全落 `eval_results/<run-id>/` |
| **R8** | 用户研究：pilot 4-6 人（8/23-27）→ main N=24 within-subject（8/28-9/3），TaskVM vs Direct-CUA；intervention success / state-understanding accuracy / NASA-TLX / UMUX-LITE / perceived control / trust calibration + 开放自由玩（qualitative） | ★APP pilot-ready / R6 | bench_design §六（三层证据）/§十三 | controlled + free-play 一次完成；统计按 participant 聚类，不拿 user op 伪装大 N |
| **R9** | 数据锁 + 统计 + 质性编码 → 论文实验章节（5.1-5.5 结构见 bench_design §十六） | R7 R8 | bench_design §十三/§十四 | 主图主表固定；9/7 后禁止加功能 |

---

## 5. agentAPP 线（A2UI 协议 + 前端 APP）· milestone 卡

请注意，A2UI 官方 github 仓库见 `/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui_code`
**身份**：把 "模型生成的动态任务界面 + 固定治理壳" 做成真产品。主文档：`frontend/TaskVM_A2UI_Coding_Agent_Workplan_2026-08-19.md`。

| ID | 做什么 | 依赖 | 索引文件 | 验收 |
|---|---|---|---|---|
| **A1** | 协议镜像 P0：补齐 v0_9 缺的 4 个 list/wrapper schema（`client_to_server_list[_wrapper]` / `server_to_client_list[_wrapper]`）、同步官方 v0.9 living doc 澄清（catalog `$id == catalogId`）、`SOURCE.txt` pin upstream commit + 全文件 SHA-256、`agent_sdk_reference` 标记为 SDK reference 非 normative | — | `docs/A2UI-protocol-spec/v0_9/`、workplan §1/§7-P0 | spec test 全绿（JSON parse / $ref resolve / sample validate / catalog $id==catalogId）；本轮 runtime 冻结 v0.9 不混 v0.9.1 |
| **A2** | 删假开放入口：微信/支付宝 selector、`names={wechat,alipay,x}`、示例 chips、goal API 用户级必选 app 参数、**bootstrap 后自动 /governance/start**；首页只留 NL goal → Compile → Ready → 用户检查 Task Surface/权限/计划 → Start | — | `static/index.html`、`static/js/taskvm.js`、`app_open.py`、`server.py`、workplan §2 | grep 生产 UI 零 `wechat/alipay/业务 key` 条件分支（Gate P0.5） |
| **A3** | genui 生产层：新增 `taskvm/genui/`（protocol/context/schema/validator/policy/data_model/store）；优先用官方 a2ui-agent-sdk（已装 0.4.0，import 名 `a2ui`）的 SchemaManager/BasicCatalog/validator；TaskSurfaceContextBuilder 只喂公开语义（goal/变量 display_label/observed/desired/mutability），禁喂内部 ID/GT/DB key | A1 | workplan §4/§7-P1/P2、`taskvm_bench/benchmark/a2ui_schema_manager.py`（只参考，反向 import 禁止）、`taskvm/domain/projection.py`（语义组件树）、`taskvm/projection/view_models.py:109` | mapper 纯函数（同 snapshot 同 data model）；`taskvm/` 零 import `taskvm_bench/` |
| **A4** | 真 GenUI Decoder：模型生成 `updateComponents`（**只生成结构，不生成事实**；动态值用 `{"path":"/variables/<key>/desired"}` 绑定）；两层校验（A2UI schema + TaskVM policy：editable 白名单/action allowlist/组件数≤80/树深≤8/禁 URL-iframe-executable/不得触碰固定治理控件）；fallback 是通用"变量列表"非 task 模板；decoder 可选小快模型（role→model 路由，workplan §20.2）；蒸馏经验落 `taskvm/genui/skills/`（workplan §20.3） | A3 | workplan §5/§6/§7-P3、`taskvm/genui/prompts/decoder_system.md` | **3 个互不相干 unseen goal → 3 棵不同组件树，前端零改码**；普通状态更新 0 次 GenUI call |
| **A5** | React island + transport：a2ui_client 用 @a2ui/react `./v0_9` MessageProcessor/Renderer 消费真 A2UI 消息（createSurface/updateComponents/updateDataModel）；bootstrap/SSE/action 回传 transport；旧六面板静态页降级为 fallback 或删除 | A3 | `taskvm/workspace_ui/a2ui_client/`（环境就绪）、workplan §3 | 浏览器真渲染 A2UI 消息；value-only 更新只走 updateDataModel |
| **A6** | governance 接线 + 意图解析模型化：A2UI action → `action_router` 校验 → LocalPatch/GoalPatch 直进现有 GovernanceService（**不翻译回自然语言再过 CUA**）；用户自由文本意图 → 轻量模型解析为 GoalPatch/Patch/RollbackIntent（治理核心灵活可变，不假定人只有枚举的几种意图）；意图解析与"结构化 action→自然语言呈现"润色走**小快模型**（Qwen 级，role→model 路由——只润色呈现、不改语义，ledger 记 model id，见 workplan §20.2） | A4 A5 | `taskvm/genui/action_router.py`、`taskvm/projection/services/governance.py`、`handover_dehardcode_generalization.md` §3.2 governance 节 | 治理壳（Start/Pause/Stop/Checkpoint/Rollback/Evidence）永不由模型生成或隐藏 |
| **A7** | 动效：verified snake progress（只跨 verified milestone）、checkpoint 小奖励、final verifier PASS 才 🎉、rollback 倒放、pause 不假装有进展、`prefers-reduced-motion` | A5 | workplan §17、motion/canvas-confetti（已装） | 视觉验收清单逐项过；conflict/verification failure 不用庆祝动画 |
| **A8** | open-world kill tests：Set A 未见任务×3-5（类型差异大）/ Set B UI 泛化（零前端改码、零 `if semantic_key==`）/ Set C 同一任务双 substrate（UI 稳定、CUA 轨迹可不同）；static gate grep；e2e 回环（CUA+verifier+rollback journey） | GATE-ARCH + A4-A6 + R3 | workplan §16/§19、`tests/e2e_ui/`、`handover_dehardcode_generalization.md` §5（未见任务 E2E） | workplan §19 DoD 清单全勾；证据落 `eval_results/` |
| **A9** | **响应性与实时投影**（owner 2026-08-19 现场反馈："太卡太慢、进 session 点 start 没反应、图片经常丢失、3000 的 mobilegym 起不来"）。三段：**A9.0 全链路延迟审计**（只测不改——时间瀑布：UI 点击→HTTP→治理→driver tick→模型 TTFB/总时长→截图采集→act→verify→SSE 推送→首帧渲染，每段标注 server/transport/client 归属；数据源=ledger latency 字段+call_archive 档案+SSE 时间戳+前端 performance marks；含 sim:3000 冷启动计时、`.run/` 残留 daemon 与端口占用排查、Flask 线程模型核查；产物 `eval_results/<run-id>/latency_waterfall.json`）；**A9.1 首响应体验**（铁律：任何用户操作 <100ms 内有可见回执——乐观确认+失败回滚；治理命令受理零模型调用必须本地即时，"点 start 没反应"按 bug 处理；阶段化进度时间线由现有状态信号驱动："编译任务世界 ✓3.2s → 生成计划 ✓11.8s → 执行 2/5 步（18s…）"带活计时器，把黑盒等待变成白盒过程；**渐进式任务面**（owner 2026-08-19 追加）：goal 提交瞬间即渲染通用骨架——goal 卡 + 单点 workflow 脉冲节点（"正在编译任务世界…"）+ 治理壳 Ready 禁用态，StateCompiler 返回→变量骨架落位（label 已知、值 pending），TaskArchitect 返回→单点 morph 成真实 DAG（节点逐个落位、verify/checkpoint 着色），GenUI decoder 返回→替换为 A2UI 组件树；骨架必须通用、不得伪造计划内容，morph 用 motion layout 动画（workplan §20.1）；SWR 快照渲染：进 session 永远先渲染上次快照+"同步中"角标，禁止空白加载页；缩略图管线：≤240px WebP + fingerprint 去重（屏未变=零字节）+ SSE 突发合并 150ms + 慢网络自适应降级；止血层与完整层——止血=现有静态页按钮 pending 态/错误浮出/图片重试占位（与 A2 同文件顺手），完整=进 A5 island）；**A9.2 多 APP 实时截图墙**（每 surface 一张实时缩略图卡，多 surface 并发时 fan-out/fan-in 泳道脉冲动画显示"开工中"；前台走现有 live-screenshot 通道，后台 surface 复用 A-03 heartbeat 的 fresh observe 通道；点击任意卡放大该 APP 当前截图不论前台后台，全图懒加载；动画由 kernel events/workflow 节点状态驱动，纯前端） | A9.0 无前置（8/19 立即）；止血层← A2 同期；A9.1/A9.2 完整层 ← A5 + A9.0 | `taskvm/workspace_ui/**`（唯一可改区，含 a2ui_client）、`call_archive.py`（只读）、ledger/funnel 产物（只读）、workplan §17 | click-to-feedback <100ms 全操作覆盖；模拟慢网络首屏壳 <1s；多 surface 并发截图更新零丢失；A9.0 审计报告落盘。**边界铁律**：projection snapshot 若缺 per-surface screenshot_ref 字段→提 projection RFC backlog，禁直改冻结层；bridge 截图采集节奏改动属 agentRM（R3 协调）；sim/launcher 修复落点 `scripts/`（移交给对应 owner，APP 线不碰 substrate） |

---

## 6. 并行/串行速查

### 6.1 今天（8/19）就能同时开工的 6 件事（零依赖冲突）

1. **W0.2 架构师 schema 解放**（agentRM，最高优先，半天）——只碰 `taskvm/domain/` + `taskvm/architect/`
2. **A1 协议镜像修复**（agentAPP）——只碰 `docs/A2UI-protocol-spec/` + 新 spec tests
3. **A2 删假开放入口**（agentAPP）——只碰 `taskvm/workspace_ui/static/` + `app_open.py`
4. **R1 G0 grader 的 fake-port 单测部分**（agentRM 副线可先行）——只碰 `taskvm_bench/evaluation/` 新文件
5. **A9.0 延迟审计**（agentAPP）——instrumentation 只落 `taskvm/workspace_ui/` + 产物落 `eval_results/`；诊断 sim:3000/launcher 问题但只报告不移手不改（`scripts/` 与 substrate 属对应 owner）
6. **W0.1 杂务归档提交**（任何 agent 或 owner 指令后执行，分批 commit）

### 6.2 绝对不能并行的依赖对（等前置完成再动）

| 后置 | 必须等 | 原因 |
|---|---|---|
| R1 真模型收口 | W0.2 + GATE-ARCH | 否则 trial 全死在 architect，白烧模型调用费 |
| 第 2 条及以后的 benchmark task | R1 GATE-G0 | bench_design 铁律："第一条 rollback task 自动判分跑通之前，不准写第 2 条 benchmark task" |
| A4 GenUI Decoder | A3 + A1 | 无生产 schema 层则 decoder 输出无处校验 |
| A6 治理接线 | A4 + A5 | 无 renderer 则 action 无回传路径 |
| A9.1/A9.2 响应性与截图墙 | A5 + A9.0 | 无 transport 则无截图投递管线；无审计数据则优化靠猜 |
| R5 满配族 anchors | R2.5 | 阶梯未爬完就打满配：白烧模型费，成功率数据无诊断意义 |
| A8 kill tests | GATE-ARCH + A4-A6 + R3 | 未见任务需要架构师放行 + 开放靶场 + 完整 UI 链 |
| R6 freeze | R4 + R5 | freeze 后 ModelVerifier 变更会使数据不可比 |
| R7 正式评测 | R6 FREEZE | freeze 前跑的数据不能进论文主表 |
| R8-main 用户研究 | FREEZE + APP 成品 | 仪器（APP）和被测系统都必须锁版 |

### 6.3 两个硬 gate + 一个 freeze

- **GATE-ARCH**（W0.2 后，8/19-20）：6 demo goal 重跑 ≥5/6 活过 architect，call_archive 复档对比
- **GATE-G0**（R1 后，8/20）：RM-C04-01 无 LLM judge 自动判分，verdict 指回 raw evidence
- **FREEZE 8/28**：suite v1.0 冻结；此后**只修 blocking bug，不开发 feature，不因模型失败删 task**

### 6.4 冲突面控制（多 agent 并行时）

R2-R5 的各族工单都会追加 `tasks.py` 和 predicate 模板——**R1 落地时就把 predicate 按干预类型分文件**（`predicates/rollback.py`、`predicates/local_patch.py`…），各族只写自己的文件 + 自己的 TaskSpec，`tasks.py` 用 append-only 约定避免 merge 冲突。`bridge.py` 在 R3 期间对 APP 线只读。

---

## 7. 文件所有权矩阵（防冲突铁律）

| 路径 | Owner | 其他线权限 |
|---|---|---|
| `taskvm/domain/`、`taskvm/architect/` | agentRM（W0.2 短期独占） | W0.2 冻结后两线只读；修改需 RFC |
| `taskvm_bench/**` | agentRM | agentAPP 禁碰 |
| `taskvm/substrate/mobilegym/**` | agentRM（R3） | APP 线只读 |
| `taskvm/verifier/**` | agentRM（R4） | APP 线只读 |
| `taskvm/substrate/builtin_web/**` | agentRM（R5 的 F10 用） | APP 线只读 |
| `taskvm/genui/`（新目录） | agentAPP | RM 线禁碰 |
| `taskvm/skills/`（compiler/architect/cua/verifier 子目录，新） | agentRM（R2.5；冻结层 prompt 装载改动以 R2.5 卡为 RFC 授权） | agentAPP 只读 |
| `taskvm/genui/skills/`（新） | agentAPP | RM 线只读 |
| `taskvm/workspace_ui/**`（server/app_open/static/a2ui_client） | agentAPP | RM 线禁碰（call_archive.py 除外：RM 线判分要读它的档案） |
| `taskvm/projection/**` | **FROZEN** | 两线只读；APP 线消费 `view_models.py` 但不改它；修改需 RFC |
| `taskvm/kernel/`、`taskvm/governance/`、`taskvm/runtime/` | **FROZEN** | 两线只读；修改需 RFC（A6 的意图解析模型化若需动 governance 接口，走 RFC backlog） |
| `docs/A2UI-protocol-spec/**` | agentAPP（A1） | RM 线只读 |
| `docs/contracts/**` | 双方均只读；RFC 走各自 `*_rfc_backlog.md` | — |
| `eval_results/<run-id>/` | 各写各的 run-id 子目录 | 互不覆盖；永不进 git |
| `scripts/` | 修改者拥有对应 commit | — |

**注释清零（PURETY-GEN 任务 A）分工**：agentRM 清 `taskvm/`（domain/architect/substrate/verifier/runtime/kernel/governance/projection）+ `taskvm_bench/`；agentAPP 清 `taskvm/workspace_ui/` + `taskvm/genui/`。纯注释 commit 零代码行变化，单独提交，验证命令见 `handover_dehardcode_generalization.md` §2.3。

---

## 8. 文档地图（2026-08-19 重组后）

```text
docs/
├── A2UI_开工大纲_v0_心智模型对齐版.md      ← 心智模型总纲（.mrules 冻结指针，活跃）
├── A2UI_开工大纲_v0_心智模型对齐版_老版本.md ← 历史版（.mrules 第94行引用其 API key 章节，保持原位）
├── contracts/            ← 冻结合同（kernel/architect/runtime/projection/substrate/治理 + RFC backlogs + audit_charter）【活跃法律，代码引用，永不移动】
├── A2UI-protocol-spec/   ← 官方协议 vendored 镜像（v0_8/v0_9/v1_0）【活跃资产，agentAPP A1 的工作对象】
├── our_tex/              ← 论文 LaTeX（Overleaf subtree 同步）【活跃资产】
├── 01_早期立意立项/
│   ├── compare/          ← 立项期竞品调研 chat exports（DuetUI/SaC/Sidekick/CHI 计划/GenerativeUI 调研等）
│   └── references/       ← 相关工作论文源码（AgentLen/ALLOY/AOHP/DuetUI/Jelly/Macaron/Morae/SaC/sidekick）（gitignored，仅本地）
├── 02_legacy时代/
│   └── chatgpt-export_[A2UI] Prototype工作流优化建议.txt  ← E-xx episode 时代的驱动文档（P0 清单出处）
├── 03_A-G时代/
│   ├── INDEX.md
│   └── handoffs/         ← agent A-G 并行施工的 12 份交接工单（00-12）
└── 04_RM&APP时代/
    ├── 00_MASTER_HANDOVER_两线总调度.md   ← 本文档（唯一主调度）
    ├── bench/            ← agentRM 主文档群
    │   ├── RM.1.C.bench_design.md        ← bench 设计总纲（3295 行，含 23 天计划）
    │   ├── RM.0.A-B.md                   ← RM-0 工单（历史，已完成）
    │   └── TaskVM_Audit_RM_Wave_4b2ca9c.md ← DSA 审计报告（历史）
    ├── frontend/         ← agentAPP 主文档群
    │   ├── TaskVM_A2UI_Coding_Agent_Workplan_2026-08-19.md ← APP 线施工总纲（1010 行）
    │   ├── prepare.md                    ← A2UI v0.9 镜像审计结论（A1 的依据）
    │   └── chatgpt-export_A2UI搭配GPT实现流程.txt
    ├── handover_dehardcode_generalization.md  ← PURETY-GEN 工单（去硬编码+注释清零，两线共同的坐标清单）
    └── handover_full_app_integration.md       ← MG-FULL-APPS 工单（已并入 R3）
```

**路径引用安全性**：`.mrules` 引用的两份开工大纲、代码 docstring 引用的 `docs/contracts/*` 均未移动，冻结指针全部有效。历史工单（RM.0.A-B.md 等）内部引用的旧路径（如 `docs/oracle/new-oracle/...`）在重组前就已失效，属历史文档内部事务，不修改（纪律：不动文件内容）。

---

## 9. 核心诉求落点表（owner 原话 → 哪个 milestone 兑现）

| Owner 诉求（2026-08-19 原话摘录） | 落点 |
|---|---|
| "taskvmAPP 必须面向开放场景，每一层每个地方完全真实不做妥协的泛化性" | W0.2（架构师放行）+ R3（substrate 开放）+ A8（kill test 验证） |
| "禁止枚举，禁止分支"（准确边界：禁场景特异分支，不禁动作协议字母表） | R3 + `handover_dehardcode_generalization.md` §3.1 试金石 |
| "让模型 verifier 去验证，不要用规则；优先泛化性（model-based verification）" | R4 ModelVerifier 三态（cannot_verify 是诚实输出不是失败） |
| "governance 灵活可变，不能假定人只有这些意图" | A6 意图解析模型化（NL 意图 → GoalPatch/Patch/RollbackIntent） |
| "GenUI 输出的东西前端消费不了" | A3+A4+A5（生成→校验→真渲染闭环） |
| "注释不要再写历史遗留问题" | PURETY-GEN 任务 A（两线各清各目录，纯注释 commit） |
| "事务锁定/新任务就报废" | W0.2 + R3 通用 mutate（NL intent 替代 operator 枚举） |
| "CHI award candidate 级别实验" | R1-R9（bench_design §十六的 5.1-5.5 结构） |
| "先 10 条任务族反复迭代，每条突出不同特性" | R1 GATE-G0 → R2 → R5（10 族 = bench_design §七 F1-F10） |
| "built-in 做跨平台跨设备联动" | R5 的 F10（MobileGym + builtin_web 双 surface） |
| "太卡太慢，进 session 点 start 没反应，图片经常丢失，3000 的 mobilegym 起不来，debug 各项时间开销" | A9.0（审计落盘）→ A9.1（首响应体验） |
| "首 token 响应优化，不能一直卡着" | A9.1（乐观确认 + 事件驱动阶段进度 + 活计时器——把黑盒等待变成白盒过程；APP 侧可选分级模型提速意图解析） |
| "多 APP 状态改变的当前截图实时渲染，fan-in/fan-out 显示开工中，点击看任意 APP 截图（含后台）" | A9.2（截图墙；后台 surface 复用 A-03 heartbeat 的 fresh observe 通道） |
| "前端先初始化 workflow 为单点，随 Compile→Ready→Start 的编译返回动态切换，让用户体感立即开始处理" | A9.1 渐进式任务面（workplan §20.1） |
| "『10 条』= 10 个完整运行轨迹（dev anchor），不是任务/事件/用户动作/GUI 动作——必须强调" | §4 术语钉死（依据 bench_design §六/§七/§十一）+ R2.5 |
| "不要一上来满配 10 条：先简单轨迹→蒸馏 skill→harness 提升→再上难度，循环提高先验" | R2.5 SKILL-LADDER（L0→L1→L2，档间蒸馏） |
| "每个角色都要 skill（compiler/architect/cua/verifier/genui）；skill 是 harness 性能关键一步不是作弊，最终在 bench 绑定锁死" | R2.5 + R6（manifest 锁 skill set hash）+ `taskvm/skills/` 目录 |
| "不必处处 GPT-5.6-sol：简单场景用小模型（Qwen 级），结构化信息润色成自然语言再送 CUA" | A6/A4 分级模型路由（workplan §20.2）；bench 侧 L0/L1 迭代可用便宜模型（manifest 标记），正式 suite pinned 不变 |

---

## 10. 时间线到截稿（对齐 bench_design §十二，23 天）

| 日期 | 唯一目标 | Exit criterion |
|---|---|---|
| 8/19 | W0.2 + A1 + A2 + R1 fake-port 单测 + A9.0 延迟审计启动 | GATE-ARCH 复跑 ≥5/6；latency_waterfall.json 落盘 |
| 8/20 | R1 真模型收口 | GATE-G0：rollback anchor 自动判分 |
| 8/21-22 | R2 四硬契约 + A3/A4 + R2.5 L0/L1 起步 | evaluator 单测全过；3 unseen goal 3 组件树；首批 skill 蒸馏 |
| 8/23-25 | R5 剩余族 anchors（按 R2.5 阶梯）+ A5/A6/A7 + A9.1/A9.2 | ★ APP pilot-ready（8/22，含基础响应性）→ R8-pilot 启动（8/23） |
| 8/26-27 | R6 扩 30 formal tasks + R4 收口 + A8 | 每 family ≥1 真实 anchor；manifest/seed/hash 固定 |
| **8/28** | **FREEZE suite v1.0** | 此后只修 blocking bug |
| 8/29-9/1 | R7 正式自动评测 | 先 60 paired runs 再补至 120 |
| 8/28-9/3 | R8-main 用户研究 N=24 | controlled + free-play 一次完成 |
| 9/4-6 | R9 数据锁 + 统计 + 质性编码 | 主图主表固定 |
| 9/7-10 | 论文 / 视频 / supplementary / 复现包 | 禁止再加功能 |

**两条 critical path（不可被 coding agent 压缩的）**：① deterministic GT 闭环（R1）——不通则后续所有 online run 白跑；② 用户研究招募与实验周期（R8）——今天就要开始准备 pilot 招募。

---

*本文档由审计 agent 于 2026-08-19 生成，作为 docs 时代重组的一部分。执行中若发现与代码现实冲突，以代码 + eval_results 实证为准，并在本文件追加勘误记录。*
