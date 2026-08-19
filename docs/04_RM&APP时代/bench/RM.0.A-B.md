我**不建议原样 approve**，但不是因为整体方向错。相反，这个本地 agent 的判断大约 **90% 是对的**，RM-0.A / RM-0.B / RM-1.0 的三段式命名也很好，可以直接沿用。

真正需要改的只有几处，而且都是会影响 coding agent 实际施工边界的地方，不是文字洁癖。

第一，**B-01 ledger 重复记账必须挪回 RM-0.A**。这是最明确的一处。上一轮 DSA 自己的 Gate A 明确把“ledger 一一对应”列为冻结核心，而且还专门说了“不能因为 RM 是扩展功能，就放过……重复 ledger”。fileciteturn1file0L165-L202 更关键的是，当前在线 UI 已经使用 `HttpCUAModel + shared ledger`，所以这不是“未来 RM 才会碰到”的问题；RM 只是会让它变得更致命。DSA 的动态反例就是 2 个 provider request → 4 条 ledger row。fileciteturn1file0L90-L104

第二，**A-01 的修法写得稍微过度指定了**。问题本身完全成立：compiler 知道 surface，architect 丢失它，runtime 又固定 `surfaces[0]`。fileciteturn1file0L29-L49 但 prompt 不应该诱导 coding agent 给冻结的 domain `SurfaceHandle` 塞 `surface_id` 或新建一个暴露 substrate token 的公开语义。应该要求它在现有合同下完成 handle→surface resolution，具体 owner 由 frozen contracts 决定。

第三，**A-06 不应该要求 agent 凭空给 `workspace_ui/apps` 发明完整依赖白名单**。仓库明确冻结的是六层依赖和 `taskvm/** → taskvm_bench` 零引用；`verifier` 也有明确合同。最小且正确的修法是把 repo-wide `taskvm_bench` ban 真正锁全，再补已有合同明确规定的 governance/verifier gate。否则是在 audit 修复里偷偷创造新 invariant。

第四，**RM-0.B 的收口 smoke 必须多一条非常关键的要求：`taskvm-real-full` 必须真的从自然语言 goal → fresh observation → StateCompiler → TaskArchitect → Kernel → Runtime 开始，不能继续借 `_make_kernel()` 的手拼 plan。** 这正是你刚才问“UI 是否已经任意指令”的核心差距。原 DSA 也明确要求 user-op 从顶层完整接口链下来，而不是绕过系统。fileciteturn0file0L31-L49 现在的 `demo.py:123-141` 是固定 goal + 手拼两节点；所以 RM-0.B 结束时必须至少有一条**现有 demo 场景的 real-full smoke**，不需要开始设计 RM-1.0 新任务。

第五，有两个小但会真实绊倒 coding agent 的地方：回归命令里的 `tests/benchmark` **不存在**，应该是 `taskvm_bench/tests`；另外 B-08 没必要为了 RM 主路径去删除/强关 legacy semantic routes，最小修复是 **RM runner 永远以无 `--cua-loop` 模式启动 bridge + evaluation 代码静态禁止调用 `/api/wechat/*`、`/api/x/*`**。这样更克制。

至于本地 agent 对 UI 的解释：**核心判断正确，但“mentor 四步弧没问题”说得过满。** 当前默认 `./scripts/dev.sh` 确实接真实 `HttpCUAModel`，不是 TemplateCUA；但 goal 固定、plan 手拼、只绑定一个 builtin app，而且 `demo.py` 自己已经承认 `VisibleLabelExtractor` 在 builtin 页面上可能抽不到 observed value，届时 verifier 会 honest-fail。所以它是“真 CUA 的 production-shape demo”，不是“任意自然语言 × 任意 builtin/MobileGym × full compiler/architect pipeline”。这点 RM-0.B 收口后才应该真正补齐。

所以我的裁决是：**不原样 approve；下面这个版本我 approve，可以直接给一个 coding agent 一口气做完 RM-0.A + RM-0.B。** 原 DSA 推荐的依赖顺序本来也是 surface/governance/heartbeat/ledger → user-op → MobileGym runner → real trial。fileciteturn1file0L361-L385

你是 TaskVM 的 **RM-0 修复 agent**。一个 agent 负责一次性完成 **RM-0.A + RM-0.B**。

工作目录：

`/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui`

基准 SHA：

`64d24e93ac2060ad15209e60329ebc9b9be4be60`

开工前先执行 `git rev-parse HEAD`。若 HEAD 不等于该 SHA，停止修改并在汇报第一行明确声明 SHA mismatch。

---

# 0. 本 wave 的边界

我们现在把开发阶段固定命名为：

* **RM-0.A = RM-free correctness closure**

  * 即使暂时不做 real-model evaluation，也已经违反冻结合同、当前 prototype 行为、计量真实性或文档真实性的问题。
* **RM-0.B = RM-specific infrastructure closure**

  * legacy deterministic benchmark 当前可以不依赖，但在首个真实模型 × MobileGym / builtin full-chain trial 之前必须完成的基础设施。
* **RM-1.0 = open-scenario task design**

  * 只有 RM-0.A + RM-0.B 全绿后才开始。
  * 本工单禁止设计新的开放场景任务、禁止开始正式 10-task RM suite、禁止跑 paper matrix。
  * RM-0.B 只允许使用已有 demo/fixture 做 plumbing smoke。

原 Gate C（统一实验 caps、真正 visual OOD、clustered statistics、task/prompt/harness freeze、development/frozen 数据分离等）属于 **RM-1.0 freeze 阶段**，本 wave 不实现，除非下面明确要求只修失实文案。

---

# 1. 必读材料

按以下顺序阅读，不能跳过：

1. `.mrules`

   * 仓库最终规则；
   * 五锚点；
   * 六层依赖；
   * GUI-only / no internal ID；
   * taskvm 与 taskvm_bench 权限隔离；
   * frozen path；
   * git discipline。

2. `docs/contracts/audit_charter.md`

   * 本工单所有 finding 的审计纪律来源。
   * 注意：修 DEFECT，不借 RM 新功能无限扩 scope；新 invariant 不得偷偷写进修复。

3. `docs/oracle/new-oracle/TaskVM_Audit_RM_Wave_4b2ca9c.md`

   * DSA 审计报告。
   * 逐条核对 A/B finding 的原始 file:line、动态反例、风险说明。
   * 如果该文件是 untracked，本工单允许读取，但不要自动把它提交进 git。

4. 当前心智模型：

   * `docs/A2UI_开工大纲_v0_心智模型对齐版.md`

5. Frozen contracts：

   * `docs/contracts/layered_ownership_protocol.md`
   * `docs/contracts/kernel.md`
   * `docs/contracts/substrate.md`
   * `docs/contracts/architect.md`
   * `docs/contracts/runtime.md`
   * `docs/contracts/projection.md`
   * 各层 `*_rfc_backlog.md`，仅在 frozen contract 本身无法解释实现位置时查阅。

6. 历史施工 handoff，仅用于理解 owner，不用于推翻当前 frozen contract：

   * `docs/oracle/new-oracle/handoffs/00_README_MASTER_HANDOFF.md`
   * `02_LAYERED_KERNEL_REFACTOR_AGENT.md`
   * `03_SUBSTRATE_ISOLATION_AGENT.md`
   * `04_TASK_ARCHITECT_GOVERNANCE_AGENT.md`
   * `05_PROJECTION_FRONTEND_AGENT.md`
   * `06_CUA_EXECUTION_SYNC_ROLLBACK_AGENT.md`
   * `07_FINAL_BENCHMARK_AGENT.md`
   * `08_INTEGRATION_RELEASE_CLEANUP_AGENT.md`
   * `11_CURRENT_REPOSITORY_AUDIT.md`
   * `12_AGENT_F_SESSION_HANDOFF.md`（如存在）

7. 本工单会重点修改/索引的实现：

   * `taskvm/domain/state.py`
   * `taskvm/domain/workflow.py` / ActionContract 定义所在文件
   * `taskvm/architect/compiler.py`
   * `taskvm/architect/architect.py`
   * `taskvm/architect/http_port.py`
   * `taskvm/architect/ledger.py` 或 ModelCallLedger / ModelCallRecord 实际定义文件
   * `taskvm/governance/**`
   * `taskvm/runtime/autonomy.py`
   * `taskvm/runtime/sync.py`
   * `taskvm/runtime/ports.py`
   * `taskvm/runtime/bootstrap.py`
   * `taskvm/verifier/visible.py`
   * `taskvm/projection/app.py`
   * `taskvm/projection/store.py`
   * `taskvm/projection/services/governance.py`
   * `taskvm/projection/services/driver.py`
   * `taskvm/workspace_ui/composition.py`
   * `taskvm/workspace_ui/demo.py`
   * `taskvm/substrate/port.py`
   * `taskvm/substrate/builtin_web/**`
   * `taskvm/substrate/mobilegym/bridge.py`
   * `taskvm/substrate/mobilegym/session.py`
   * `taskvm/substrate/mobilegym/provider.py`
   * `taskvm/substrate/mobilegym/evaluation.py`
   * `taskvm_bench/evaluation/harness.py`
   * `taskvm_bench/evaluation/aggregation.py`
   * `taskvm_bench/evaluation/statistics.py`
   * `taskvm_bench/evaluation/cli.py`
   * `taskvm_bench/evaluation/runner.py`
   * `taskvm_bench/benchmark/model_client.py`
   * `taskvm_bench/benchmark/generator.py`
   * `taskvm_bench/benchmark/registry.py`
   * `taskvm_bench/benchmark/ood_fixtures.py`
   * `taskvm_bench/benchmark/mobilegym_fixtures.py`
   * `configs/paper_matrix.json`
   * `README.md`
   * `docs/benchmark.md`
   * `scripts/dev.sh`
   * `scripts/stop.sh`
   * `tests/architecture/**`
   * `tests/runtime/**`
   * `tests/projection/**`
   * `tests/substrate/**`
   * `tests/verifier/**`
   * `tests/governance/**`
   * `tests/architect/**`
   * `tests/integration/**`
   * `tests/e2e_ui/**`
   * `taskvm_bench/tests/**`

---

# 2. 不可违反的施工纪律

## 2.1 Frozen semantics

本 wave 的原则是：

> 实现追上已冻结合同，而不是借修 bug 重写合同。

修改 frozen path 是允许的，因为本工单就是 exact-SHA audit finding 的修复 wave；但必须保持 frozen contract 的公开语义。

尤其禁止：

* 为方便 routing 把 app DB id / deep URL / internal operator 放进 domain 或模型 prompt；
* 给 domain `SurfaceHandle` 塞 concrete substrate locator；
* 为 benchmark 添加 runtime 后门；
* 为让测试绿而降低 verifier / rollback / no-leak 标准；
* 用 hidden state API 替代真实 GUI write/rollback；
* 给 RM harness 增加可以直接操纵 Kernel/Runtime 的“测试捷径”。

## 2.2 A-04 明确撤销

用户已经明确决定 **A-04 本 wave 不修**。

因此：

* 不修改 `OPENAI_API_KEY` 的现有读取约定；
* 不删除 `taskvm_bench/benchmark/model_client.py` 当前默认内部 key；
* 不修改该 key 的值；
* 不要求 secret rotation；
* 不清 git history；
* 不把 credential remediation 混入任何 commit。

注意：

这只豁免 credential finding。

**不豁免 `model_client.py` 中与 RM-0.B provider retry/error policy 有关的代码修改。**
可以改 retry/error/repair 行为，但不得顺手改 key/base-url 配置语义。

## 2.3 Git

* 每个独立性质一个 commit。
* 只 `git add` 本会话实际修改的文件。
* 禁止 `git add -A`。
* 禁止 `git add .`。
* 不 staging `eval_results/`、截图、PNG。
* 不 force push。
* 不自动提交原本就 untracked、且不是本会话生成的 DSA 报告或旧大纲。

## 2.4 Evidence

每个行为类修复都必须：

1. 有自动化 regression test；
2. 跑测试；
3. 把机器可读验证摘要落盘：

`eval_results/rm0/<finding-id>.json`

例如：

`eval_results/rm0/A-01.json`

这些文件不进 git。

JSON 至少包含：

* finding
* git_sha
* test_command
* exit_code
* passed
* failed
* relevant_assertions
* notes
* timestamp

不能用口头“看起来好了”替代。

---

# 3. RM-0.A — RM-free correctness closure

按以下顺序完成。

---

## A-01 Multi-surface binding / routing

### 当前问题

重点核对：

* `taskvm/architect/compiler.py` 中 compiler 已经从可见 observation 知道 `surface_label`；
* `taskvm/architect/architect.py` 生成 ActionContract target evidence 时重新造 handle，丢失原 binding provenance；
* `taskvm/runtime/autonomy.py` forward / verify / compensation 多处默认 `surfaces[0]`。

结果：

证据来自 surface B 的变量，动作可能落到 surface A。

### 修复目标

建立完整链：

`visible observation → compiler grounding → ActionContract target evidence → runtime surface resolution → act/verify/compensation on correct surface`

### 非常重要的边界

**不要为了修这个问题修改 frozen domain `SurfaceHandle` 的公开语义去携带 concrete `surface_id`、URL 或 substrate locator。**

domain handle 仍然必须保持 TaskVM-owned / substrate-independent。

使用 frozen contracts 允许的最窄 owner 实现：

* 保留/复用 compiler 已建立的 handle provenance；
* concrete handle→surface/session locator mapping 保持在 contract 允许的 private resolver/cache owner；
* runtime 通过 TaskVM-owned handle resolution 找到当前 session surface；
* stale handle 必须 fresh observe/rebind；
* rebind 失败必须 honest structure invalidation / failure；
* **绝不 fallback 到 `surfaces[0]`**。

模型输入中不能出现内部 surface token、DB id、内部 URL。

### Tests

新增：

`tests/runtime/test_multisurface_routing.py`

至少覆盖：

1. evidence/contract 指向第二 surface → GUI action 只落第二 surface；
2. verify 读取正确目标 surface；
3. compensation 回原 surface；
4. 一个 semantic variable 有多个 heterogeneous bindings 时分别路由到对应 surface；
5. stale handle → fresh observe + rebind；
6. rebind 失败 → honest invalidation/failure，绝不 fallback surface 0；
7. no-leak：新增 routing metadata 不进入模型 prompt。

---

## A-02 Governance lifecycle：pause / resume / stop

### 当前问题

重点核对：

* `taskvm/projection/app.py`
* `taskvm/projection/services/governance.py`
* `taskvm/projection/services/driver.py`
* `taskvm/runtime/autonomy.py`
* Kernel governance event/epoch path

当前 rollback 会进入 driver，但 pause/resume/stop 没有形成同样完整的 lifecycle control。

stop 已有动态反例：发 stop 后仍可发生后续 GUI action。

### 修复目标

Projection public governance API 的：

* start
* pause
* resume
* stop

必须真实控制 runtime driver 生命周期。

### 单一 owner 要求

**一个 HTTP lifecycle request 不得同时经两个独立路径重复写 governance event / 重复 bump epoch。**

不要做：

`KernelGovernancePort.pause()` 一次
再 `driver.pause() -> runtime.request_pause() -> kernel.request_governance("pause")` 又一次

必须选定 contract-compatible single owner，让：

`Projection route → lifecycle driver → runtime/kernel consistent governance state`

或等价的单一路径成立。

### Stop semantics

stop 必须是真正的 persistent lifecycle state：

* stop 接受后，不得再启动新的 GUI atomic action；
* 若 stop 到达时一个真实 GUI primitive 已经进入不可分割执行，可允许该 primitive 完成；
* provider response 尚未落 GUI action 时 stop 到达，旧 response 不得再执行；
* stop 后不能因为下一次 driver tick 自动复活；
* start/resume 是否允许从 stopped 重启，严格按 projection/runtime contract 当前语义实现，不自行发明。

### Tests

至少：

1. POST pause → 当前 atomic action 边界后不再启动下一动作；
2. POST resume → 从 paused 恢复；
3. POST stop → stop 返回后 `substrate.act_log` 不再增长；
4. provider request in-flight → stop → stale response 返回后不得 `act()`；
5. lifecycle HTTP response、driver status、projection snapshot、SSE 一致；
6. 同一次 lifecycle request 只产生一次相应 governance transition，不双记。

---

## A-03 Production inactive-surface heartbeat

### 当前问题

`AutonomyRuntime.poll_inactive_surfaces()` 已存在。

benchmark 会手工调用。

production `ThreadedRuntimeDriver` 没有调度，因此当前 live projection claim 在真实 driver 路径下不闭环。

### 修复目标

给 production driver 增加 monotonic heartbeat scheduler：

* active action loop 与 inactive heartbeat cadence 分离；
* heartbeat 只读；
* unchanged fingerprint fast path；
* 0 provider request；
* 0 high-level compiler/architect call，除非 runtime 正式发布 StructureInvalidated 后由上层 slow path 处理；
* 新 runtime events 经现有 `_forward_new_events()` / SSE path 推送；
* 不创建第二套世界同步逻辑。

cadence / timeout 应能被配置并记录到 run manifest；不要硬编码 paper-specific 数字进 runtime contract。

### Test

集成测试：

* surface A 是 active；
* 用户无输入；
* agent 无新 GUI action；
* surface B 外部发生变化；
* provider request count 不增加；
* heartbeat poll 后 kernel observed plane 更新；
* projection/SSE 在设定 deadline 内收到正确 delta。

---

## A-04

**撤销，不做。**

---

## A-05 Token aggregation correctness

### 当前问题

`taskvm_bench/evaluation/aggregation.py`

当前 `mean_tokens_by_role` 实际输出 token 总和 tuple，而不是 mean。

### 修复目标

不要静默改变旧字段含义。

输出明确字段：

* `total_tokens_by_role`
* `mean_tokens_per_trial_by_role`
* `mean_tokens_per_request_by_role`
* `n_requests_by_role`

如果当前 persisted/report schema 有 versioning，升级 schema version；若没有统一 schema version，不要凭空制造一个全仓版本协议，只给本 report schema 增加明确版本字段并更新消费者测试。

要求：

* denominator 明确；
* missing token usage 保持 honest missing，不用 0 假装 provider 返回过 usage；
* old report 不得被新代码静默解释成新语义。

加测试覆盖手算 case。

---

## A-06 Architecture gate coverage

### 当前问题

`tests/architecture/test_import_boundaries.py`

当前 `_RULES` 没覆盖所有 `taskvm/**`，所以 `_ALWAYS_BANNED=("taskvm_bench",)` 并没有真的做到 repo-wide。

### 修复目标

做两件事：

1. 新增 **repo-wide AST gate**：

   * 对 `taskvm/**/*.py` 全部禁止 import `taskvm_bench`。
   * 包括 absolute 与 relative import。
   * 不依赖某目录是否出现在 `_RULES`。

2. 补齐**已有 frozen contract 明确规定**的层规则：

   * governance；
   * verifier；
   * 以及 frozen docs 已经明确写出的其他规则。

### 非常重要

**不要为了“覆盖完整”而凭空给 `workspace_ui/`、`apps/` 发明新的严格 import allowlist。**

它们若没有 frozen contract 的明确依赖表，只需要受到 repo-wide `taskvm_bench` ban 和已有明确红线约束。

Audit 修复不能创造新 invariant。

---

## A-07 README truthfulness

修正 `README.md` 中与当前代码不一致的 workflow：

当前 demo 必须诚实描述为：

* `scripts/dev.sh` 启动 builtin apps + projection UI；
* demo session 固定 goal；
* kernel variables / workflow plan 当前由 `workspace_ui/demo.py` 手工 fixture 组装；
* 默认在线模式 CUA 是真实 `HttpCUAModel`；
* offline 模式是 honest FAIL placeholder；
* 当前 demo 一次绑定一个 builtin app；
* 它不是 arbitrary natural-language full compiler→architect demo；
* 它不是 MobileGym demo；
* SSE endpoint 写真实路径；
* session API 写真实 method/path。

并明确：

`taskvm-real-full` 的自然语言 goal → compiler → architect → runtime full composition 将在 **RM-0.B** 完成。

RM-0.B 完成后，如果实现事实已经改变，再在对应 commit 更新 README 为新事实。

不要为了匹配旧 README 去实现半吊子 route。

---

## A-08 Budget wording only

当前只修 claim，不做 RM-1.0 budget redesign。

修改：

* `docs/benchmark.md`
* `taskvm_bench/benchmark/registry.py` 等相关 claim

把当前六条件称为：

**structurally aligned under a shared TrialBudget object**

或等价准确表达。

不要声称已经拥有统一：

* provider-request cap
* GUI-action cap
* wall-clock cap

这些属于 RM-1.0 freeze。

---

## A-09 OOD naming only

当前 normalized world 的 holdout 统一改称：

**normalized semantic OOD**

不要称 visual morphology OOD / GUI morphology OOD。

真正 viewport / density / layout / visual reskin holdout 属于 RM-1.0。

修改 README、`docs/benchmark.md`、`ood_fixtures.py` 等所有实际 claim。

---

## A-10 Legacy generator truthfulness

`taskvm_bench/benchmark/generator.py`

当前实际 `TEMPLATES` 数与 “40 templates / 800 instances” 声明不一致。

修为真实含义，例如：

`4-family parametric synthetic generator`

并新增 regression test：

文档/暴露的 family count == `len(TEMPLATES)`。

不要为了维护“40”这个旧数字临时补 36 个垃圾模板。

---

## A-11 paper_matrix command typo

`configs/paper_matrix.json`

修掉不存在的：

`compare --out`

`compare` 只接受实际 CLI 支持的参数；输出路径由 config 的 `out` 字段承担。

---

## A-12 MobileGym fixture rollback wording

这是原审计的 B-07 doc debt，现在并入 RM-0.A 顺手收口。

`taskvm_bench/benchmark/mobilegym_fixtures.py`

把过期的：

`snapshot-based rollback`

描述改为当前真实行为：

* rollback 通过真实 GUI compensation 尝试；
* irreversible 时 honest 409/partial/locked；
* 没有 hidden `set_state` fallback；
* legacy semantic CUA route 无 CUA loop 时 honest 501；
* checkpoint 位于 irreversible action 前。

只改 truthfulness，不重写任务。

---

## A-13 Single-owner model-call ledger

这是原 prompt 的 B-01，**现在移动到 RM-0.A**。

原因：

C-2 是 frozen contract，不是 RM 新功能：

`1 provider request = 1 ledger row`

当前在线 `HttpCUAModel` production-shape path 已经受影响。

### 当前问题

重点核对：

* `taskvm/workspace_ui/composition.py`
* `taskvm/runtime/autonomy.py`
* architect/runtime `ModelCallRecord`
* shared `ModelCallLedger`

当前 adapter 会记录 provider request，runtime 又对同一 decision 建第二条 row。

### 修复目标

**transport/model adapter 是真实 provider request row 的唯一 owner。**

每一次真实 provider request：

* 创建唯一 request_id；
* 创建且只创建一条 ledger row；
* 捕获 model / usage / latency / ok / error；
* semantic retry/repair/temperature fallback 若真的再次请求 provider → 新 request_id、新 row。

Runtime：

* 不再为同一次 provider request创建第二 row；
* 只对已有 row 附加 execution context，例如：

  * node_id
  * op_id（若有）
  * attempt
  * is_repair
  * revision
  * purpose
* 若当前 ledger 类型不支持安全 annotate，设计最小 contract-compatible annotation API；不要靠“再 record 一条”模拟 metadata。

### Test invariant

必须覆盖：

* success
* transport timeout
* provider error
* invalid JSON
* semantic repair
* temperature downgrade（若该路径在对应 client 上存在）

核心断言：

`provider_stub.request_count == ledger.total_provider_requests()`

或当前 ledger API 的等价形式。

另断言：

同一个 request_id 绝不出现两条 provider rows。

---

# 4. RM-0.A Gate

RM-0.A 完成后先跑：

`PYTHONPATH=. pytest -q tests/domain tests/kernel tests/architect tests/governance tests/runtime tests/verifier tests/architecture tests/projection tests/substrate tests/integration taskvm_bench/tests`

再跑：

`PYTHONPATH=. python -m compileall -q taskvm taskvm_bench tests`

如果标准环境支持：

`PYTHONPATH=. pytest -q tests/e2e_ui`

有环境依赖导致无法运行时，诚实标 `environment_blocked`，不能冒充 PASS。

RM-0.A 未全绿不得进入 RM-0.B。

---

# 5. RM-0.B — real-model / MobileGym infrastructure closure

本阶段仍然**不设计新开放场景任务**。

目标：

让现有 demo/fixture 足够证明以下 plumbing 真正存在：

`natural-language goal`
→ fresh visible observation
→ StateCompiler
→ TaskArchitect
→ Kernel
→ Projection governance
→ Runtime
→ real CUA
→ real GUI
→ VisibleVerifier
→ Kernel
→ Projection/SSE

并能换 builtin / MobileGym substrate。

---

## B-01 Vision-capable HttpCUAModel + complete GuiAction schema

原 prompt B-02。

### 当前问题

`taskvm/workspace_ui/composition.py` 的 HttpCUAModel：

* 只发 visible_text；
* 没使用 `Observation.screenshot_ref` / image；
* action parser 只覆盖部分字段。

而 substrate GuiAction 已允许：

* click
* tap
* type
* key
* scroll
* wait
* open

以及：

* coordinate
* text
* key
* direction
* magnitude
* duration_ms
* target

### 修复目标

CUA 每一轮使用 fresh observation：

* fresh screenshot/image data URL（若 observation/capture 可提供）；
* scrubbed visible text；
* 用户可见 goal；
* 上一个 ActionReceipt 的用户可理解结果；
* verifier discrepancy / repair context；
* 当前 attempt。

不得输入：

* DB id
* entity_id
* hidden DOM attributes
* internal URL
* operator vocabulary
* fixture GT

action JSON schema 与 `GuiAction` frozen vocabulary 对齐。

解析缺字段/非法 action honest fail，不猜。

no-leak 测试覆盖：

* initial prompt
* retry prompt
* verifier repair prompt
* screenshot-associated message construction

---

## B-02 Provider retry / error taxonomy

原 prompt B-03。

重点：

`taskvm_bench/benchmark/model_client.py`

同时确认：

`taskvm/architect/http_port.py`

生产 port 目前已经遵守“一次 complete_json = 一次 provider request”，不要把隐藏 retry 加进去。

### 规则

* 401 / 402 / 403 → immediate fatal；
* 其他明确 non-retryable 4xx → fatal；
* 429 → bounded exponential backoff；
* 5xx → bounded exponential backoff；
* transport timeout / connection transient → bounded retry；
* unsupported temperature → 最多一次显式 downgrade；
* 每次真实 provider request 独立 request_id + ledger row；
* parse failure 不允许低层偷偷 re-request；
* semantic JSON repair 必须由明确的上层 repair orchestration 发起，并独立计数/记账。

不要改 OPENAI_API_KEY / base-url 约定。

---

## B-03 Non-invasive MobileGym evaluation oracle

原 prompt B-04。

### 当前问题

重点检查：

* `taskvm/substrate/mobilegym/bridge.py`
* `taskvm/substrate/mobilegym/evaluation.py`

oracle read 不得改变 agent 下一帧世界。

尤其 X oracle 读取当前会 activate/open app 的问题必须消除。

### 修复目标

evaluation observer 与 agent interaction context 分离。

优先级：

1. simulator/store 的 read-only observer；
2. 独立 browser/context；
3. 若在线期间无法做到 non-invasive，则在线控制环不读该 oracle，只在 trial 结束后从隔离副本判卷。

### 强制测试

oracle read 前后：

* `foreground_before == foreground_after`
* `screenshot_fingerprint_before == foreground/screenshot_after` 的等价稳定断言
* `agent_action_count` 不变
* runtime session active surface 不变
* projection latency clock 不被 oracle 自己的 UI navigation 污染

---

## B-04 ProjectionClient + UserOpDriver + per-op barrier

原 prompt B-05。

新增：

* `taskvm_bench/evaluation/projection_client.py`
* `taskvm_bench/evaluation/user_ops.py`

### UserOp

至少表达：

* start
* pause
* resume
* stop
* local_patch
* goal_patch
* checkpoint
* rollback

字段至少：

* op_id
* kind
* payload
* expected_http_class
* settle_policy

### 铁律

UserOpDriver：

* 只能调用 Projection 的 public HTTP/API；
* 不持有 Kernel；
* 不持有 Runtime；
* 不持有 CUAModel；
* 不调用 `GovernanceService.handle`；
* 不直接执行 substrate action；
* 不构造 agent trajectory；
* 不读 hidden oracle 决定下一步用户操作。

### Barrier

每次 user op 必须等待系统真正 settle。

优先复用现有公共信号：

* HTTP command accepted/returned；
* existing governance-applied SSE；
* verifier/runtime events；
* projection revision；
* observed-world revision。

**不要只为了 benchmark 新增一个 prototype-only hidden `/test/accepted(op_id)` API。**

如果需要 op correlation：

* 优先在 bench client 自己维护 `op_id ↔ request/response/SSE window`；
* 或复用已有公开 correlation field；
* 只有 frozen/public contract 确实缺少不可替代能力时，才做最小公开扩展，并在汇报里明确。

per-op timeline 至少记录：

* op issued
* HTTP accepted
* first GUI action
* last GUI action
* verifier completed
* first correct projection/SSE
* settled

---

## B-05 Per-op result schema

User operation 是 RM 评估的最小 verdict unit。

持久化结果至少包含：

* schema_version
* git_sha
* task_version
* harness_version
* model
* substrate
* environment_seed
* sample_index
* user_ops:

  * op_id
  * kind
  * verdict
  * world_diff
  * protected_diff
  * projection
  * rollback
  * ledger_request_ids
  * timeline
  * artifacts
* trial_verdict
* failure_class
* evaluation_error

目录：

`eval_results/<run-id>/`

* `manifest.json`
* `trials/`
* `artifacts/`
* `reports/`

不进 git。

注意：

**environment seed 和 stochastic sample_index 是两个概念，不要因为当前 CLI 叫 `--seeds` 就把真实模型 sample replicate 与世界初始化 seed 混为一谈。**

---

## B-06 Real-model condition definitions

新增/整理条件：

### Main

`taskvm-real-full`

必须真实包含：

* fresh substrate observation
* StateCompiler real ModelPort
* TaskArchitect real ModelPort
* Runtime real HttpCUAModel
* real GUI substrate
* VisibleVerifier
* shared single-owner ledger

禁止：

* TemplateModelPort
* TemplateCUA
* hand-written TaskVariable
* hand-written WorkflowGraph
* fixture plan fallback

### Diagnostic

`taskvm-real-cua-only`

* compiler/architect 可以 template；
* CUA 真实；
* condition id 必须明确，不得伪装 real-full。

### Baselines / controls

* `direct-cua-real`
* `planner-cua-real`
* `taskvm-template-control`

同一 condition 内：

* model id/version 固定；
* 不能失败后静默换模型；
* model fallback 若存在必须改变 condition metadata / 明确记录。

---

## B-07 Real-full composition bootstrap

这是本工单必须显式补的一项。

当前：

`taskvm/workspace_ui/demo.py::_make_kernel()`

是固定 goal + 手拼 TaskVariable / WorkflowGraph。

RM-0.B 必须新增一个**真正的 real-full bootstrap/composition path**，供 evaluation/smoke 使用：

1. 接收自然语言 goal；
2. substrate `list_surfaces()`；
3. 对相关 surface fresh `observe()`；
4. 转换成 `CompilerObservationView`；
5. StateCompiler；
6. TaskArchitect；
7. 初始化 TaskVMKernel；
8. 注入 shared ledger；
9. compose AutonomyRuntime；
10. register Projection session；
11. driver start 后走正常 projection/governance/runtime path。

必须尽量复用已有：

* `GovernanceService.bootstrap`
* `StateCompiler`
* `TaskArchitect`
* `compose_task_runtime`
* `ProjectionSessionStore`

不要复制第二套 compiler/architect orchestration。

### 关键验收

使用**现有 demo goal**即可：

`把日历事件「产品发布」改期到 2026-08-18`

这是 plumbing smoke，不是 RM-1.0 新任务设计。

测试必须证明：

* 没调用 `_make_kernel()`；
* 没手工 `init_task_state([...])`；
* 没手工 `set_plan(WorkflowGraph(...))`；
* compiler provider 至少有真实 request；
* architect provider 至少有真实 request；
* CUA provider 至少有真实 request（有 provider 环境时）；
* 三类 request 全进同一 ledger，且 1:1；
* 最终动作仍走真实 GUI。

如果 provider credential 不可用：

* 允许测试 real-full composition 的 fake-port contract wiring；
* 真 provider smoke 标 `environment_blocked`；
* 禁止把 fake-port wiring 声称为 real-model PASS。

---

## B-08 MobileGym runner / factory / CLI

原 prompt B-07。

新增：

`taskvm_bench/evaluation/mobilegym_factory.py`

职责：

* 启动或连接 MobileGym bridge；
* health；
* reset；
* seed；
* 构造 `MobileGymSubstrateSession`；
* 构造隔离 evaluation observer；
* 构造 real-full TaskVM session；
* 注册 Projection session；
* 启 driver；
* trial close；
* state integrity check。

CLI 增加 MobileGym substrate。

示意：

`python -m taskvm_bench.evaluation.cli run --suite rm-smoke --substrate mobilegym --condition taskvm-real-full --model gpt-5.6-sol --samples 1`

如果当前 CLI 使用 `--seeds`：

* 保持 backwards compatibility；
* 明确 environment seed 与 real-model samples 的语义；
* 不允许破坏现有 deterministic matrix CLI。

---

## B-09 Bridge semantic-route anti-bypass

原 prompt B-08。

MobileGym bridge 当前仍有 legacy semantic routes，例如：

* `/api/wechat/...`
* `/api/x/...`

这些不能成为 RM 主路径。

### 最小修复原则

**不要求为了 RM 删除 legacy route。**

RM runner 必须：

* 启动 bridge 时不注入 legacy nested CUA loop；
* 主路径只使用 L1 `observe` / `act`；
* semantic mutate routes 在 RM configuration 下不可被 harness 调用；
* 若无 CUA loop，legacy route 保持 honest 501 即可。

增加 CI/static test：

`taskvm_bench/evaluation/**`

不得引用：

* `/api/wechat/`
* `/api/x/`
* bridge semantic mutate helper
* hidden `set_state` write path

evaluation setup 的 reset/seed/oracle 除外，但它们必须只存在 EvaluationEnvironment，不得进入 runtime/user-op driver。

这样锁死：

`TaskVM CUA → MobileGym L1 GUI act`

禁止：

`TaskVM CUA → bridge semantic route → nested CUA`

---

## B-10 MobileGym trial isolation

原 prompt B-09。

当前 bridge/session 不能假装可安全并发。

要求：

* 默认 serial execution，或一 worker 一 bridge instance；
* 不允许两个 active trial 共享同一 mutable foreground/session；
* 每 trial manifest 记录：

  * bridge instance id
  * sid
  * environment seed
  * reset state hash
  * initial state fingerprint
  * final integrity status

若 reset/state invariant 不成立：

`evaluation_error = true`

不得算 system failure，也不得算成功。

---

# 6. RM-0.B 收口 smoke

本阶段**不设计新的开放任务**。

只用已有 demo / fixture 做 plumbing validation。

---

## Smoke 1 — builtin real-full bootstrap

用现有 calendar demo seed + goal：

`把日历事件「产品发布」改期到 2026-08-18`

完整链必须是：

`natural-language goal`
→ fresh builtin observation
→ StateCompiler
→ TaskArchitect
→ Kernel
→ Projection session
→ UserOpDriver public API
→ Runtime
→ real HttpCUAModel
→ real GUI gesture
→ VisibleVerifier
→ Kernel
→ SSE/projection settle

这里绝对不能使用 `demo.py::_make_kernel()` 的 hand-built plan。

有可用 provider 时跑真模型。

没有 provider 时：

* fake-port wiring 测试单独 PASS；
* real-provider smoke 标 environment_blocked；
* 不冒充 real-full provider PASS。

---

## Smoke 2 — builtin governance op

至少通过 UserOpDriver 公共 Projection API 跑：

* start
* pause
* resume
* stop

证明：

* lifecycle 真控制 driver；
* stop 后零新 act；
* per-op barrier 能 settle；
* SSE 与 snapshot 一致。

---

## Smoke 3 — MobileGym L1 plumbing

不做新任务设计。

只验证：

* bridge start/health
* reset
* seed
* observe
* one primitive real GUI act
* fresh observe
* evaluation observer non-invasive
* close
* integrity check

然后用已有 fixture 中最简单的一条/一个 op 做一次 UserOpDriver plumbing smoke 即止。

不能把该结果写成正式 RM task success。

manifest 明确：

`development_only: true`

---

## Smoke 4 — ledger invariant

在 builtin real-full smoke 和 MobileGym smoke 中都检查：

`provider request count == provider ledger rows`

并分别给出：

* state_compiler
* task_architect
* cua

角色计数。

不得再出现：

`2 provider requests → 4 ledger rows`

---

# 7. Full regression

最终至少执行：

`PYTHONPATH=. pytest -q tests/domain tests/kernel tests/architect tests/governance tests/runtime tests/verifier tests/architecture tests/projection tests/substrate tests/integration taskvm_bench/tests`

环境允许时：

`PYTHONPATH=. pytest -q tests/e2e_ui`

以及：

`PYTHONPATH=. python -m compileall -q taskvm taskvm_bench tests`

注意：

仓库里是：

`taskvm_bench/tests`

**不是 `tests/benchmark`。**

不要运行不存在的测试目录。

如果有额外测试目录，先 `find tests taskvm_bench/tests -maxdepth 2 -type f` 核实再加。

---

# 8. Completion gate

只有全部满足才宣布 RM-0 完成：

## RM-0.A

* multi-surface route correct；
* pause/resume/stop lifecycle correct；
* stop stale action blocked；
* production inactive heartbeat exists；
* token aggregation truthful；
* architecture repo-wide bench ban locked；
* README/demo claims truthful；
* current budget/OOD claims corrected；
* generator/matrix/fixture docs corrected；
* C-2 ledger 1:1 fixed。

## RM-0.B

* visual CUA path；
* full GuiAction schema；
* provider error/retry taxonomy；
* non-invasive MobileGym oracle；
* ProjectionClient；
* UserOpDriver；
* per-op barrier/result schema；
* real-model conditions；
* genuine `taskvm-real-full` bootstrap；
* MobileGym runner/factory；
* semantic-route anti-bypass；
* trial isolation；
* builtin + MobileGym development smoke。

---

# 9. 最终汇报格式

最后不要只说“all fixed”。

按编号逐条报告：

### 1. Baseline

* start SHA
* end SHA
* environment
* unavailable dependencies

### 2. RM-0.A findings

每条：

* finding id
* root cause
* files changed
* behavior changed
* tests
* eval_results JSON path
* commit SHA
* status：PASS / FAIL / ENVIRONMENT_BLOCKED

### 3. RM-0.B findings

同样格式。

### 4. Real-full proof

明确回答：

* natural-language goal 是否真的进 StateCompiler？
* TaskArchitect 是否真的调用 provider？
* CUA 是否真的调用 provider？
* 是否存在 hand-built fallback？
* 是否全部共享单一 ledger？
* provider request 与 ledger row 是否 1:1？
* GUI action 是否只走 SubstrateSession.act？
* verifier 是否 fresh visible observation？
* UserOpDriver 是否只走 Projection public API？
* MobileGym RM path 是否完全没调用 semantic mutate route？

### 5. Regression

列出真实命令和 pass/fail 数。

### 6. Remaining limitations

只列真实剩余限制。

尤其诚实说明：

* RM-1.0 新开放任务尚未设计；
* paper freeze caps / visual OOD / clustered statistics 尚未做；
* 未运行的真实 provider/MobileGym 环境测试不得冒充通过。

### 7. RM-1.0 readiness verdict

最后只允许三个 verdict：

* `READY_FOR_RM-1.0`
* `NOT_READY_FOR_RM-1.0`
* `READY_WITH_ENVIRONMENT_BLOCKERS`

只有 RM-0.A 与 RM-0.B 的代码 gate 全绿，且剩余问题纯属真实外部环境不可用时，才允许第三种。

---

# 10. 最后一条原则

不要为了让 RM-0 快速结束而阉割五锚点，也不要借 RM-0 无限扩展论文功能。

本 wave 的目标非常窄：

> **把当前已承诺的 prototype 修正确，把真实模型 / MobileGym / user-op 的“路”铺通；但不提前开始 RM-1.0 的开放任务设计和正式实验。**

完成 RM-0 后停止。

不要设计新的十条任务，不要 freeze paper matrix，不要跑正式主实验。

这个版本我会给 **APPROVE**。它保留了你本地 agent 绝大多数方案，只把会真正导致施工跑偏的地方修正了：**ledger 归类、surface owner 边界、architecture gate scope、real-full bootstrap、bridge 防旁路、测试路径和 lifecycle single-owner**。等这个 agent 最终给出 `READY_FOR_RM-1.0`，再让任务设计 agent 进入开放场景设计，阶段边界会非常干净。
