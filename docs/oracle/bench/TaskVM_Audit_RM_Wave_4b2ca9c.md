审计对象 SHA：`4b2ca9cccf55f023d478481d753d9266a8683af7`（当前上传 ZIP 的 archive comment 完全匹配；ZIP SHA-256：`d94786da5e77c102f0e357f80b35b1c9053c2ef5eabb3ebc63a2ae799e21729d`）

# TaskVM 代码审计与 RM Wave 开工计划

## 0. 结论先行

**总体判定：当前仓库不是“推倒重来”，而是“已有相当扎实的分层、no-leak、GUI-only 和诚实 benchmark 基础，但在进入 RM wave 前存在 4 个承重 P0”。**

必须先解决的 4 个承重 P0 是：

1. **多 surface 的目标归属链断裂**：runtime、verify、compensation 都默认第一 surface。它直接削弱五锚点中的 bidirectional executable binding 与 substrate independence。
2. **pause / resume / stop 的 Projection→driver 控制链不完整**：尤其 `stop` 已动态复现为“返回 stopped 后仍能执行 GUI 动作”。
3. **生产 driver 没有调度 inactive-surface heartbeat**：benchmark 手工调度了，所以测试/模拟结果会掩盖生产态的 live projection 缺口。
4. **真实模型记账与动作协议未达到 RM 计量要求**：同一 provider request 被 CUA adapter 与 runtime 重复记账；现有 `HttpCUAModel` 只发可见文本、丢弃截图，并只解析动作字段的子集。

此外，源码提交了一个非空默认网关凭据。本文不复述其内容；**应立即撤销、轮换、移出 Git 历史**。这是安全与治理上的当前 BLOCKER，不应等待 RM。

### 是否存在“两个已知缺口之外的第三个同量级缺口”？

**是。第三个同量级缺口是：ActionContract/SurfaceEvidence 到实际 Substrate surface 的可执行路由没有闭环。**

- compiler 私有缓存知道 `surface_label`，但 Domain `SurfaceHandle` 只剩一个短 handle；
- architect 又为 action 新造 `ha...` handle，未保留 compiler handle 的 surface 归属；
- runtime、verify、compensation 最终都取 `surfaces[0]`；
- final benchmark 又把绝大多数 app 聚合进单一 `desktop`，所以这个缺口不会自然暴露。

紧随其后的第四个 claim-level 缺口，是生产 driver 不调 inactive-surface heartbeat。

---

## 1. 审计对象、边界与可复现状态

### 1.1 固定对象

- Repository identity：用户提供的 `HashTAG00002/A2UI` main 快照。
- Audit target：`4b2ca9cccf55f023d478481d753d9266a8683af7`。
- ZIP archive comment：与目标 SHA 完全一致。
- ZIP byte hash：`d94786da5e77c102f0e357f80b35b1c9053c2ef5eabb3ebc63a2ae799e21729d`。
- 限制：ZIP 内无 `.git`，因此可证明“上传归档自声明 SHA 与要求一致、字节对象固定”，不能从该 ZIP 独立重算 Git commit object。

这符合 `docs/contracts/audit_charter.md:63-67,79-87` 的 immutable-SHA 取证纪律。

### 1.2 本轮 bounded scope

按 `docs/contracts/audit_charter.md:109-170`，本轮只报告：

- 冻结合同违反；
- doc-vs-code；
- 分层泄漏或 gate 覆盖错误；
- test oracle 锁错；
- 当前 benchmark 计量/声明与代码不一致；
- RM 新能力缺失则标为 **SPEC-GAP**，不伪装成当前功能 regression。

### 1.3 已执行的静态/动态检查

```bash
python -m compileall -q taskvm taskvm_bench tests
# PASS

bash -n scripts/dev.sh scripts/stop.sh run.sh
# PASS

PYTHONPATH=. pytest -q tests/architecture/test_import_boundaries.py
# 17 passed

TASKVM_SUBSTRATE_LOCK_AUDIT=1 PYTHONPATH=. pytest -q tests/substrate
# 35 passed

PYTHONPATH=. pytest -q tests/architect tests/runtime tests/verifier
# 72 passed
```

额外的 benchmark/fakes 组合测试中，业务断言通过；一个新 subprocess 在 import `taskvm.projection` 时因当前审计环境未安装 Flask 而失败。`pyproject.toml:11-20` 已声明 Flask 等依赖，且当前环境无法联网安装，因此：

- 这不是仓库功能 defect；
- 本报告不声称完成了浏览器/UI 全量动态测试；
- Projection/UI 结论采用代码路径、冻结合同、已有测试与最小动态 probe 交叉验证。

### 1.4 关于“270 trials”

`configs/paper_matrix.json:3-16` 确实定义：

- 15 tasks；
- 6 conditions；
- 3 seeds；

即声明矩阵为 `15 × 6 × 3 = 270`。但 `eval_results/` 原始 JSON 不在归档中，故本文只称其为**仓库声明值**，不把它当作已复核的实验证据。

---

## 2. 审计放宽原则：哪些不能放，哪些可以放

### Gate A：现有冻结核心，**零放宽**

以下任一项存在，都必须在“不做 RM”的假设下立即修：

- API/状态后门；
- no-leak 破坏；
- 假回退、不可逆假成功；
- pause/stop 语义失真；
- 多 surface 写错对象；
- production live projection 不自动同步；
- doc 声称已实现而实际不存在；
- public credential；
- 当前报告把总量标成均值；
- 测试门宣称锁住全仓但实际只扫子集。

### Gate B：RM-P0，允许称为“新功能 SPEC-GAP”，但首个真模型 trial 前必须清零

- user-op driver；
- `substrate=mobilegym` runner；
- per-op verdict schema；
- real-full 模型栈；
- screenshot-aware CUA；
- retry/error taxonomy；
- provider-call ledger 一一对应；
- 非侵入式 MobileGym oracle；
- bridge trial 隔离。

### Gate C：paper freeze 前清零

- 公平预算改为统一 provider/action/wall-clock caps；
- OOD 明确区分 semantic holdout 与 visual morphology holdout；
- task/prompts/harness 版本冻结；
- clustered statistics；
- 所有失败原样落盘；
- 文档、命令和生成器声明收敛。

`audit_charter.md:174-220` 禁止开放式发明 invariant；因此本报告不会把“我偏好的架构”当 bug。但对于已经冻结的五锚点、route matrix、C-2、SSE/live mirror 与文档声明，不能用“RM 是新功能”豁免。

---

# 第一部分：当前代码审计

## 3. A 类：即使不做 RM，也必须修

### A-01 — 多 surface 动作、verify、compensation 全部默认第一 surface

**严重度：BLOCKER**

**合同/主张**

- `docs/contracts/runtime.md:52-73`：ActionContract 带 `target_evidence`，runtime 消费多个 surfaces；
- 五锚点要求一个 VM 变量绑定多个异质 app 对象；
- `docs/contracts/substrate.md:42-48` 要求 handle 能在 fresh observe 后重绑，而不是丢失归属。

**证据链**

- `taskvm/domain/state.py:39-52`：`SurfaceHandle` 只有 `handle_id`；
- `taskvm/domain/contract.py:35-50`：`ActionContract.target_evidence` 没有可解析的 surface identity；
- `taskvm/architect/compiler.py:425-461`：compiler 私有 `HandleEvidence` 仍知道 `surface_label`；
- `taskvm/architect/architect.py:534-553`：architect 为 action 新建 `ha001...` handle，未复用 compiler handle；
- `taskvm/runtime/autonomy.py:230-243`：forward action 直接取 `self._sync.surfaces[0]`；
- `taskvm/runtime/autonomy.py:146-159`：compensation 默认第一 surface；
- `taskvm/runtime/autonomy.py:455-457`：VERIFY 默认第一 surface；
- `taskvm_bench/evaluation/harness.py:387-395`：TaskVM benchmark bootstrap 只观察第一个 surface；
- `taskvm_bench/benchmark/tasks.py:8-12`：绝大多数 app 聚合为单一 `desktop`；secondary surface 明言“runtime 当前不驱动”。

**反例（已动态复现）**

构造 `s1`、`s2` 两个 surface，ActionContract 的可见证据来自第二 surface；一次 `type x=1` 后：

```text
act_log = [('s1', 'type', 'x=1')]
s1.x = 1
s2.x = 0
```

也就是说，目标证据指向第二表面，实际写到了第一表面。

**影响**

- 跨 app executable binding 的 headline claim 目前不能由真实多 surface 路径支撑；
- verifier 可能在错误表面判定；
- rollback 可能补偿错误 app；
- 单一 `desktop` world 会掩盖该缺陷。

**最小修复**

建立 TaskVM-owned、substrate-neutral 的 binding resolver：

```text
compiler visible surface label
  -> composition-private logical surface token
  -> ActionContract carries existing logical handle
  -> runtime resolver(handle) -> session surface_id
```

不得把 app DB id、内部 URL 或 operator 暴露给模型。

**必须新增测试**

```bash
PYTHONPATH=. pytest -q tests/runtime/test_multisurface_routing.py
```

至少覆盖：

1. action 只落在目标 surface；
2. verify 读目标 surface；
3. compensation 回到原 surface；
4. 一变量多 binding 分别路由到两个 surface；
5. stale handle 触发 fresh observe/rebind，而不是 fallback 到 surface 0。

---

### A-02 — Projection 的 pause/resume/stop 没有完整接到 ThreadedRuntimeDriver；stop 会继续动作

**严重度：BLOCKER**

**合同**

`docs/contracts/projection.md:121-128` 冻结：

- start 经 runtime driver；
- pause 在下一 action boundary 停；
- resume 恢复；
- stop 返回并保持 stopped；
- rollback 由 driver 执行。

**代码**

- `taskvm/projection/app.py:285-303`：`_gov_cmd` 只有 rollback 特判调用 driver；
- `taskvm/projection/app.py:319-329`：pause/resume/stop 都只调用 `_gov_cmd`；
- `taskvm/projection/services/governance.py:41-51`：三者只写 kernel governance event；
- 真正能暂停/恢复/停止线程的实现位于 `taskvm/projection/services/driver.py:86-116`；
- runtime 的日志读取仅把最后 action 等于 `"pause"` 视为暂停，`taskvm/runtime/autonomy.py:194-202` 完全不把 `"stop"` 当停止。

**反例（已动态复现）**

```text
kernel.request_governance("stop")
runtime.run(step_budget=1) -> "step_budget"
substrate.act_log -> [('s1', 'type', 'x=1')]
```

**影响**

- UI 返回“stopped”但真实 GUI 仍可能被操作；
- governance over autonomy 的安全边界失真；
- RM 中异步 provider 返回会进一步放大 stale/post-stop action 风险。

**最小修复**

- pause/resume/stop route 必须原子地调用 driver 公共接口；
- driver 再向 runtime/kernel 写一致的治理状态；
- `stop` 应成为持久 lifecycle state，不只是一条 append-only event；
- 新动作开始前检查 stop generation；
- in-flight atomic action 可完成，但其后不得再发下一 GUI action。

**验收**

- POST stop 后 action count 不再增加；
- pause 后当前原子动作最多完成一次，下一动作不启动；
- resume 只从 paused 恢复；
- SSE 与 snapshot 的 lifecycle 一致。

---

### A-03 — inactive-surface heartbeat 有实现，但 production driver 从未调度

**严重度：BLOCKER（论文主张级）；实现修复规模约为 MAJOR**

**合同**

- `docs/contracts/projection.md:17-27`：Projection 是 continuously living mirror；
- `docs/contracts/runtime.md:72-73`：`poll_inactive_surfaces()` 是公开 live-sync 能力。

**代码**

- `taskvm/runtime/sync.py:83-129`：inactive polling/conflict detection 已实现；
- `taskvm/runtime/autonomy.py:165-168`：公开 `poll_inactive_surfaces()`；
- `taskvm/projection/services/driver.py:164-180`：生产线程只循环 `runtime.run(step_budget=1)`，没有 heartbeat；
- `taskvm_bench/evaluation/harness.py:425-456`：benchmark 每 round 手动调用 heartbeat。

**影响**

模拟评估会显示 inactive sync 正常，但 production UI 在用户没有操作那个 app 时不会自动重投影。这正是 bottom-up live projection 最需要证明的情形。

**最小修复**

在 driver 中加入 monotonic heartbeat scheduler：

- active action loop 与 inactive polling 分开；
- poll 只读，不发模型请求；
- event 统一经 `_forward_new_events` 推 SSE；
- cadence 与 timeout 写入 run manifest。

**验收反例**

用户停留在 X，外部世界向 WeChat append 一条消息；不发生任何用户动作和模型调用，Projection 应在 deadline 内收到正确 SSE delta。

---

### A-04 — 公开源码含非空默认 provider credential

**严重度：BLOCKER（安全/治理）**

**证据**

- `taskvm_bench/benchmark/model_client.py:26-30`：`OPENAI_API_KEY` 缺失时回退到源码内非空默认值。

本文不复述凭据内容。

**影响**

- 公开仓库泄露内部资源访问凭据；
- 可能造成配额耗尽、审计归属混乱和实验成本不可追踪；
- 即使凭据已失效，也不应保留在历史中。

**立即修复**

1. 撤销/轮换；
2. 改为无默认值并 fail closed；
3. 清理 Git 历史；
4. CI 加 secret scanner；
5. run manifest 只记 key fingerprint/alias，绝不记密钥。

推荐代码语义：

```python
API_KEY = os.environ["OPENAI_API_KEY"]
```

或启动时给出明确缺失错误，不可静默匿名运行。

---

### A-05 — `mean_tokens_by_role` 实际返回 token 总和，不是均值

**严重度：MAJOR**

**证据**

- `taskvm_bench/evaluation/aggregation.py:137-148`：把 prompt/completion token 在所有 records 上累加；
- `taskvm_bench/evaluation/aggregation.py:185-188`：
  - model calls 正确除以样本数；
  - token 却直接输出累加后的 `[prompt_total, completion_total]`，字段名仍叫 `mean_tokens_by_role`。

**影响**

当前 fake/architect 只要产生 token meter，报告的“mean”就是错的；RM 后成本比较会直接失真。

**修复**

结果 schema 同时输出：

- `total_tokens_by_role`；
- `mean_tokens_per_trial_by_role`；
- `mean_tokens_per_request_by_role`；
- `n_requests_by_role`。

升级 `schema_version`，旧报告不可静默重解释。

---

### A-06 — architecture gate 没有锁住“整个 taskvm 不得 import taskvm_bench”

**严重度：MAJOR**

**正面事实**

当前静态搜索未发现 `taskvm/**` 实际 import `taskvm_bench`，实现目前是干净的。

**gate 缺口**

- `tests/architecture/test_import_boundaries.py:42` 定义 `_ALWAYS_BANNED=("taskvm_bench",)`；
- 但 `_RULES` 只列：
  - domain；
  - kernel；
  - runtime；
  - architect；
  - projection；
  - substrate；
  见 `:44-65`；
- `test_import_boundaries` 只遍历 `_RULES`，见 `:168-174`。

未纳入：

- `taskvm/governance`；
- `taskvm/verifier`；
- `taskvm/workspace_ui`；
- `taskvm/apps`；
- 未来新增 package。

**影响**

“测试真正锁死 taskvm→taskvm_bench 零 import”的说法不成立；今天干净，明天可回归而 CI 不报。

**修复**

新增 repo-wide AST gate：

```text
for every taskvm/**/*.py:
    reject import taskvm_bench
```

同时单独给 governance/verifier 定义层规则；composition root 的允许项显式列白名单，而不是省略目录。

---

### A-07 — README 的用户 workflow 与实际 route/launcher 不一致

**严重度：MAJOR**

**README 声明**

`README.md:75-80`：

- POST `/api/sessions` 创建 session；
- UI/API 输入自然语言目标；
- State Compiler；
- SSE `/events`。

**实际**

- `taskvm/projection/app.py:177-180`：`/api/sessions` 仅 GET；
- `taskvm/projection/app.py:221-230`：`/events` 是分页 JSON；
- `taskvm/projection/app.py:246-273`：SSE 是 `/sse`；
- `taskvm/workspace_ui/demo.py:22-36` 明确承认 launcher 使用手组装 plan、有限 extractor；
- `taskvm/workspace_ui/demo.py:123-141` 确实创建固定 2-node graph。

**影响**

新研究者会按 README 走到不存在的 full workflow；审稿人复现会认为系统主路径虚构。

**修复二选一**

- 立即把 README 改成“当前 demo 的真实行为”；或
- 真正实现 session create + target submit + compiler/architect bootstrap route。

在 RM 前推荐先修文档，再由 user-op driver/real-full composition 补齐真实 workflow。

---

### A-08 — “同一个 TrialBudget”只是同对象，不是同一资源上限

**严重度：MAJOR（方法学）**

**声明**

- `taskvm_bench/benchmark/registry.py:3-10`：same model/CUA/tasks/budgets；
- `docs/benchmark.md:29-37`：共享同一 TrialBudget。

**实现**

`taskvm_bench/evaluation/harness.py:50-69` 的 budget 同时含：

- direct/planner 的 `max_turns=64`；
- TaskVM 的 `max_rounds=24`；
- runtime 的 `max_model_calls_per_task=512`。

Direct 只受 `max_turns`，见 `:152-180`；Planner 也按 `max_turns`，且每轮 planner + CUA，见 `:209-235`；TaskVM 按 `max_rounds`，另有 compiler/architect/CUA 多角色调用，见 `:415-456`。

**问题**

共享同一个 dataclass 不等于共享同一稀缺资源。条件获得的 provider-call、GUI-action、wall-clock 机会并不由同一 hard cap 统一裁定。

**修复**

headline fairness 必须改成：

- 同一 provider request 总上限；
- 同一 GUI action 总上限；
- 同一 wall-clock 上限；
- compiler/architect/repair/retry 全部计入；
- `turn`/`round` 只作为内部诊断，不作为跨条件公平资源。

---

### A-09 — 当前 OOD 是 semantic/key/surface label holdout，不是 GUI morphology OOD

**严重度：MAJOR（声明边界）**

**证据**

- `taskvm_bench/benchmark/tasks.py:8-12`：app 被聚合到 `desktop`；
- `taskvm_bench/evaluation/world.py:263-276`：所有可见状态统一渲染为排序后的 `k=v` 文本；
- `tasks.py:20-28` 把 venues/rsvp 等定义为 surface/operation holdout；
- 绝大多数 task 的 `surfaces=("desktop",)`。

**结论**

这些 split 对“未见 key、未见操作语义、未见 app label”是有效的；但它们没有测试：

- 新 DOM 层级；
- 新视觉布局；
- icon/文本位置变化；
- viewport/主题变化；
- app-specific navigation morphology。

**修复**

论文与报告把它准确命名为 **normalized semantic OOD**。RM wave 另外冻结 1–2 个 MobileGym visual-reskin holdout，在 task/prompt freeze 后才揭盲。

---

### A-10 — legacy generator 声称 40 templates / 800 instances，实际只有 4 templates

**严重度：MAJOR（治理/文档）**

**证据**

- `taskvm_bench/benchmark/generator.py:1-28`：40 templates / 800 instances；
- `:205-214`：`TEMPLATES` 只有 4；
- `:247-255`：docstring 再次声称 40；
- `:263-264`：`n_templates = len(TEMPLATES)`，实际返回 4。

该 generator 不是当前 final 15-task matrix 的来源，因此它不证明当前 270 声明为假；但它是明显的历史文档债务和“表面多样性”误导源。

**修复**

- 删除/归档此 legacy generator；或
- 改名为 “4-family parametric synthetic generator”；
- 测试中断言 README/doc claim 与 `len(TEMPLATES)` 一致；
- 参数实例不得在论文里当作独立 task family。

---

### A-11 — paper matrix 的运行提示含不存在的 `compare --out`

**严重度：MINOR**

**证据**

- `configs/paper_matrix.json:2` 的 `_readme` 命令带 `compare ... --out eval_results/`；
- `taskvm_bench/evaluation/cli.py:183-186` 的 compare 只有 `--config`。

**修复**

删掉 `_readme` 中的 `--out`，因为 config 已有 `"out":"eval_results"`；或给 compare 增加明确 override 参数。

---

## 4. B 类：RM 强依赖，但不启用 RM 时不影响当前 deterministic fake/world 的状态推进

这些不是可以忽略的问题，而是 **首个可计量真模型 trial 前必须修**。区别只在于：当前 deterministic TemplateCUA/world 的核心状态推进不依赖它们。

### B-01 — 同一 CUA provider request 被记两次 ledger

**严重度：BLOCKER（RM 计量）**

**证据**

- `taskvm/workspace_ui/composition.py:85-128`：`HttpCUAModel.predict_action` 自己写 ledger；
- `taskvm/runtime/autonomy.py:314-332,580-588`：runtime 对同一次 decision 再写一条；
- `taskvm/workspace_ui/composition.py:210-225`：两边共享同一个 ledger。

**动态反例**

```text
provider requests = 2
ledger rows = 4
runtime.model_calls = 2
ledger purposes =
['cua.predict_action', 'action_a_1',
 'cua.predict_action', 'action_a_2']
```

**合同冲突**

`docs/contracts/runtime.md:46,135-140` 明确要求：

```text
1 provider request = 1 ledger row
```

**修复**

只保留一个 call-record owner。推荐：

- transport/model adapter 创建唯一 `request_id` 与 ledger row；
- runtime 只给该 row 附加 `op_id/node_id/attempt/is_repair/revision`，不再创建第二 row；
- exception、parse failure、semantic repair的每次 provider request 都有独立 row。

验收 invariant：

```text
provider_stub.request_count == ledger.total()
```

覆盖成功、timeout、invalid JSON、repair、temperature fallback。

---

### B-02 — `HttpCUAModel` 没把截图发给模型，动作 schema 也只实现子集

**严重度：BLOCKER**

**证据**

- `taskvm/workspace_ui/composition.py:61-67`：prompt 只允许 click/type/scroll/key；
- `:85-103`：只发送 `visible_text`；
- `taskvm/architect/http_port.py:56-88` 已支持 `image_data_url`，但 CUA adapter 没用；
- `composition.py:131-155` 只解析 `kind/text/coordinate`；
- `taskvm/substrate/mobilegym/session.py:87-105` 可传 `key/direction/magnitude/duration_ms/target`；
- substrate 合同允许 click/tap/type/key/scroll/wait/open，见 `docs/contracts/substrate.md:30-31`。

**影响**

真实 MobileGym 页面不是统一 `k=v` 文本。没有截图与完整 gesture schema，RM 失败率会混杂“模型能力不足”和“adapter 截断能力”。

**修复**

动作协议至少：

```json
{
  "kind": "act|done|fail",
  "action": {
    "kind": "click|tap|type|key|scroll|wait|open",
    "coordinate": [x, y],
    "text": "...",
    "key": "...",
    "direction": "...",
    "magnitude": 0,
    "duration_ms": 0,
    "target": "visible description"
  }
}
```

每轮发送：

- fresh screenshot；
- scrubbed visible text/accessibility；
- user-visible goal；
-上次 action receipt 与 verifier discrepancy；
-绝不发送内部 id/operator。

---

### B-03 — bench `model_client` 对全部异常重试，且 JSON repair 会增加未统一记账的 provider calls

**严重度：BLOCKER**

**证据**

- `taskvm_bench/benchmark/model_client.py:103-143`：catch 所有 Exception，统一指数退避；
- 只有字符串包含 temperature 才特殊处理；
- `:146-171`：text JSON parse failure 会重新发 provider request；
- `:174-206`：vision 复制一份同样的 retry；
- `:209-238`：vision JSON repair 继续重新请求。

**修复**

统一为一个 transport policy：

- 401/402/403：立即 fatal；
- 明确不可重试的 4xx：fatal；
- 429/5xx/transport timeout：可重试；
- temperature unsupported：最多一次显式参数降级；
- 每一次实际请求都写 ledger；
- JSON repair 属于 L4 semantic repair，每次请求独立计数；
- port 本身不偷偷多发请求。

---

### B-04 — MobileGym oracle 读取 X 会切换前台 app，污染被测世界

**严重度：BLOCKER**

**证据**

- `taskvm/substrate/mobilegym/bridge.py:367-385`：
  - oracle read 先 `_activate`；
  - 读 X 时 `env.open_app("x")`；
  - sleep 等待渲染；
- `taskvm/substrate/mobilegym/evaluation.py:56-65`：oracle 通过该 route 获取判卷状态。

**影响**

per-op 判卷可能：

- 改变前台 app；
- 改变下一轮模型截图；
- 把 oracle 时间算进 projection latency；
- 破坏“verifier 只读 GT、不干预 agent”的实验假设。

**修复优先级**

1. 以独立 observer page/context 或底层只读 dataset/store 读取；
2. oracle read 前后 foreground app、URL、screenshot fingerprint 必须不变；
3. 无法做到时，不得把该 read 放在在线控制环内；只在 trial 结束从隔离副本判卷。

---

### B-05 — 当前没有 user-op driver、MobileGym CLI 分支或 per-op verdict

**严重度：BLOCKER（SPEC-GAP）**

**证据**

- `taskvm_bench/evaluation/cli.py:154-167`：`--substrate` 只有 `world`；
- `taskvm_bench/evaluation/harness.py:72-87`：`HarnessOutcome` 是 trial-level；
- `TaskVMHarness.route_external/_drain` 直接调用 runtime/governance 私有对象，见 `harness.py:355-375,521-566`；
- 这不是从 Projection/UI 公开入口模拟用户。

**结论**

用户要求的“顶层用户一次治理操作→完整接口链”当前不存在。它是 RM wave 核心新件，不是现有 frozen regression。

---

### B-06 — “真实 frontier 模型条件”尚未定义为全栈还是 CUA-only

**严重度：BLOCKER（研究效度）**

**证据**

`taskvm_bench/evaluation/harness.py:377-412` 当前 TaskVM 条件使用：

- `TemplateModelPort` 驱动 compiler；
- `TemplateModelPort` 驱动 architect；
- `TemplateCUA` 驱动 runtime。

**必须冻结两个不同条件**

- `taskvm-real-full`：compiler + architect + CUA 都是真实模型；作为 main；
- `taskvm-real-cua-only`：compiler/architect template，只有 CUA 真实；只作诊断，不可替代 main。

否则“真模型 TaskVM”会产生不可复核的定义漂移。

---

### B-07 — top3 MobileGym fixture 仍写“snapshot-based rollback”，与当前诚实 409 路径冲突

**严重度：MAJOR**

**证据**

- `taskvm_bench/benchmark/mobilegym_fixtures.py:92-95`：description 写 snapshot-based set_state restore；
- `taskvm/substrate/mobilegym/bridge.py:529-597`：当前 send_message rollback 走真实 GUI 尝试，无法完成则 409，无 set_state fallback。

**修复**

fixture 改为：

- send_message irreversible；
- rollback verdict = 409/partial + UI locked；
- checkpoint 放在发送前；
- 只有 X toggle 等可逆动作参与 true restore fidelity。

---

### B-08 — bridge 暴露 semantic mutate routes，RM 必须防止形成嵌套 CUA/旁路

**严重度：MAJOR（guardrail）**

**事实**

- runtime L1 session 只使用 `/api/observe` 与 `/api/act`，见 `taskvm/substrate/mobilegym/session.py:58-123`；
- bridge 仍暴露 `/api/wechat/...` 与 `/api/x/...` semantic routes，见 `bridge.py:985-1014`；
- 它们内部又调用注入的 CUA loop，见 `bridge.py:529-602` 等；
- substrate lock tests 能证明没有 set_state fallback，`tests/substrate/test_no_api_backdoor.py:122-170,193-205`。

**判定**

这不是当前 API backdoor；但 RM runner 如果误调用 semantic route，就会出现：

```text
TaskVM CUA -> bridge semantic endpoint -> bridge 内部另一个 CUA
```

成本、动作数、责任归属全部失真。

**修复**

RM 主路径只允许 L1 observe/act；semantic mutate routes：

- 默认关闭；或
- 只绑定 evaluation/legacy port；
- CI 静态断言 `taskvm_bench/evaluation/rm_*` 不得引用 `/api/wechat`、`/api/x`。

---

### B-09 — MobileGym bridge 是单 active-session 现实，不能直接并发跑 trials

**严重度：MAJOR**

`bridge.py` 通过 active sid 管理一份 live simulator；runtime plane会拒绝不匹配 sid，这是正确的。但 RM 并发如果共享一个 bridge：

- setup/oracle 可切换 sid；
- trial 可互相污染；
- QPM scheduler 与 simulator scheduler 会相互放大故障。

**修复**

- 最稳：一 worker 一 bridge/container/browser context；
- 或强制 bridge serial queue；
- 每 trial 记录 bridge instance id、sid、reset state hash；
- 前后 state invariant 不满足则标 evaluation_error，不计成功。

---

### B-10 — production demo 明确不是 intent→compiler→architect→runtime 全链

**严重度：MAJOR（RM integration dependency，当前已诚实披露）**

- `taskvm/workspace_ui/demo.py:22-36` 明确说明 hand-assembled fixture；
- `:123-141` 构造固定两节点 plan。

这不是当前 doc lie，因为 demo 自己说清楚了；但 user-op RM 的主路径必须新增一个真实 composition/bootstrap，而不是继续从 `_make_kernel()` 开始。

---

## 5. 已通过或有可信锁定的部分

### 5.1 GUI-only / no API state backdoor：当前 lock 可信

- `docs/contracts/substrate.md:20-40,79-100` 把 runtime port 限定为 observe/act/capture；
- `tests/substrate/test_no_api_backdoor.py:87-119` 扫 runtime session 无 setup powers；
- `:122-170` 检查 bridge runtime methods 不切换 reality；
- `:193-205` 禁止 executor=api knob；
- `:296-319` formal lock gate；
- formal lock 实跑 35 passed。

注意：这证明当前受检路径不存在 set_state/API fallback，不等于证明 real CUA 能完成任务。

### 5.2 no-leak：当前主 gate 设计较强

- `taskvm/architect/noleak.py:112-155` 扫 DB-id 形态和 operator jargon；
- `taskvm/workspace_ui/composition.py:89-105` 对实际 CUA prompt 调 `assert_prompt_clean`；
- `tests/architect/test_scenarios.py:343-400` 检查实际发送的 compiler/architect 与 repair prompt；
- 当前 `taskvm/**` 未 import verifier-only MobileGym fixtures。

RM 必须新增 screenshot/accessibility prompt recorder 的同类扫描，不能只扫描文本模板。

### 5.3 fakes 与 world 的身份披露诚实

- `README.md:111-114`；
- `README.md:118-123`；
- `docs/benchmark.md:59-64`；

均明确声明：

- fakes 不是真模型；
- world 不是 MobileGym；
- oracle upper bound 不进 headline。

### 5.4 mean/Wilson 基础实现正确

- `taskvm_bench/evaluation/statistics.py:1-40` 明确禁止 best/max，并实现 mean 与 Wilson interval；
- evaluation errors、crash 单列，见 `aggregation.py:130-180`。

token 字段命名错误需按 A-05 修。

---

# 第二部分：RM Wave 开工计划

## 6. RM 的研究目标与证据主线

建议把 RM wave 的 headline 定义为：

> 在真实 frontier model 与开放 GUI substrate 上，TaskVM 是否能把“用户的一次治理操作”可靠地转化为跨 app 的真实 GUI 状态变化，并在每次操作后独立证明 round-trip correctness、non-interference、live reprojection 与 honest reversibility？

### 6.1 四个技术研究问题

- **RQ-RM1：治理闭环。** 一次 user op 后，正确 diff、非干涉、projection resync 和 rollback/409 是否同时成立？
- **RQ-RM2：干预恢复。** pause、goal patch、checkpoint、external drift 出现后，TaskVM 是否比 direct/planner 更少 stale action、更高恢复率？
- **RQ-RM3：substrate independence。** 同一语义 user-op program 在 builtin 与 MobileGym 上能否得到相同语义 verdict，同时保留不同低层 GUI trajectory？
- **RQ-RM4：代价与收益。** TaskVM 增加的 compiler/architect/verifier 调用与 wall-clock，是否换来更高治理正确率、非干涉和 honest failure？

### 6.2 分开“工程校准集”和“冻结论文集”

这是防止“反复改到 10 条全绿”演变成 cherry-pick 的关键。

**Engineering calibration**

- 可以反复改 task/harness/prompt；
- 目标是 10 条至少各有一个 full-chain pass；
- 所有数据标 `development_only=true`；
- 不进入论文成功率。

**Frozen evaluation**

- task version、seed state、user-op sequence、prompt、model id、harness SHA、bridge version全部冻结；
- 揭盲 held-out variants；
- 不因失败而修改 task；
- 失败就是数据；
- 不做 best-of-N。

---

## 7. 十张任务卡

缩写：

- LP：LocalPatch；
- GP：GoalPatch；
- CP：Checkpoint；
- RB：Rollback；
- EXT：外部变化；
- IRR：不可逆。

### RM-01 — `top3_expense_to_wechat_v2`

- 来源：现有 `TOP3_EXPENSE_TO_WECHAT`。
- Apps：Alipay（读）→ WeChat（写）。
- User-op program：
  1. CP C0；
  2. LP 更新“支出摘要”；
  3. start；
  4. RB C0。
- 预期：
  - 发送内容正确；
  - 其他 chats、Alipay tx 不变；
  - projection 更新；
  - RB 对 send 返回 honest 409/partial，UI 显示 locked。
- 主特性：cross-app read→write、non-interference、IRR、ledger。
- Gate：禁止沿用 fixture 中“snapshot rollback”描述。

### RM-02 — `social_morning_brief_v2`

- 来源：现有 `SOCIAL_MORNING_BRIEF`。
- Apps：X + WeChat。
- User-op program：
  1. start，like 可见内容唯一的 CPI post；
  2. CP C1；
  3. LP 写入提醒文本并发送；
  4. RB C1。
- 预期：
  - like=True；
  - message append 正确；
  - RB C1 对 message 诚实失败，但 C1 的 like 保持；
  - 其他 post/chat 不变。
- 主特性：双 checkpoint、可逆/不可逆谱、跨 app、visible-only grounding。

### RM-03 — `expense_and_notify_v2`

- 来源：现有 `EXPENSE_AND_NOTIFY`。
- Apps：Alipay + WeChat。
- User-op program：
  1. 发 V1；
  2. CP C1；
  3. RB C0，预期 409；
  4. GP 为 V2；
  5. resume，发 V2；
  6. CP C2。
- 预期：
  - append 语义为 `[V1,V2]`；
  - 不把“包含 V2”误当 exact equality；
  - failed rollback 后 forward write 仍可继续。
- 主特性：append semantics、failed undo recovery、GP、IRR。

### RM-04 — `social_mark_fanout`

- 来源：新增 MobileGym。
- Apps：X。
- 逻辑变量：`mark_as_important=True`。
- Binding：同一 post 上 like + bookmark 两个可逆 binding，值相同。
- User-op program：
  1. LP True；
  2. start；
  3. CP；
  4. RB initial。
- 预期：
  - 两个 toggle 均 True；
  - rollback 后均恢复 False；
  - 非目标 post 不变。
- 主特性：1 var→N binding fanout、真实可逆。
- 诚实边界：这是同一 app 内 fanout；真正异质 app 1→N 由 RM-09 覆盖。

### RM-05 — `cross_source_risk_digest`

- 来源：新增 MobileGym，组合现有可见资源。
- Apps：Alipay + X → WeChat。
- 任务：从 top expense 和 CPI post 各读取一个可见事实，合成为一条风险摘要发给联系人。
- User-op program：
  1. CP；
  2. LP 编辑摘要；
  3. start。
- 预期：
  - 两个来源都正确进入消息；
  - source apps 不变；
  - 只写目标 chat。
- 主特性：三 app 多源 projection、read/write zones、compiler grounding。

### RM-06 — `partial_rollback_social_notify`

- 来源：新增 MobileGym。
- Apps：X + WeChat。
- User-op program：
  1. like + bookmark；
  2. CP C1；
  3. send WeChat；
  4. RB C0。
- 预期：
  - X toggles 可恢复；
  - WeChat message 不可恢复；
  - 总 disposition=partial；
  - UI 逐 binding 展示 restored/locked，而不是笼统“成功”。
- 主特性：混合可逆谱、partial rollback、独立 verifier。

### RM-07 — `incoming_message_reconciliation`

- 来源：新增 MobileGym external-drift task。
- Apps：用户/agent 当前在 X；evaluation plane 向 WeChat append 一条新消息。
- User-op program：
  1. start 某 X 操作；
  2. EXT incoming message；
  3. 等待 SSE barrier；
  4. LP 根据新消息修改目标；
  5. resume。
- 预期：
  - 无模型 heartbeat 即发现 WeChat 变化；
  - projection 与 world 对齐；
  - desired/observed reconciliation 不覆盖新消息；
  - 没有 stale action。
- 主特性：bottom-up live projection、inactive heartbeat、reconciliation。
- OOD：冻结后使用未见联系人/消息文案。

### RM-08 — `pause_retarget_before_irreversible_send`

- 来源：新增 MobileGym governance stress。
- Apps：X + WeChat。
- User-op program：
  1. start；
  2. CUA 已观察但尚未 send 时 pause；
  3. GP 改联系人与提醒文本；
  4. resume；
  5. stop。
- 预期：
  - superseded epoch 的回复不执行；
  - 原联系人无消息；
  - 新联系人只收到新文本；
  - stop 后动作数不再增加。
- 主特性：governance over autonomy、stale invalidation、pause/resume/stop。
- Visual holdout：冻结后改变 viewport/density、列表顺序，不改任务 GT。

### RM-09 — `release_reschedule_real`

- 来源：builtin `release_reschedule` 真模型化。
- Apps：Calendar + Taskboard。
- 逻辑变量：一个 `release_date` fanout 到 event + 两个 dependent deadlines。
- User-op program：
  1. CP initial；
  2. LP 新日期；
  3. start；
  4. EXT 改一条 dependent deadline；
  5. reconciliation；
  6. RB initial。
- 预期：
  - 1 var→3 heterogeneous bindings；
  - true rollback；
  - external conflict 可见；
  - 多 surface 正确路由。
- 主特性：核心 bidirectional binding、checkpoint、SSE、rollback。
- 这是修复 A-01 后的首个承重验收任务。

### RM-10 — `paired_notify_substrate_equivalence`

- 来源：新增 paired task。
- 两个实现：
  - builtin：从一个 app 读可见状态，在另一个 app 发通知；
  - MobileGym：Alipay/X 读，WeChat 写。
- 两边使用同一 abstract user-op program：
  1. CP；
  2. LP message；
  3. start；
  4. verify；
  5. RB/IRR verdict。
- 评估：
  - semantic verdict 相同；
  - GUI trajectory 明显不同；
  - 不共享 locator/action script；
  - substrate-specific code 只在 composition。
- 主特性：JVM moment、substrate independence、industrial adapter potential。

---

## 8. 特性 ↔ 任务覆盖矩阵

| 特性 | 专门承重任务 |
|---|---|
| 两区 projection（read-only + editable/governance） | RM-01、RM-05、RM-07 |
| live bottom-up SSE/reprojection | RM-07、RM-09 |
| bidirectional executable binding / fanout | RM-04、RM-09 |
| heterogeneous cross-app execution | RM-01、RM-02、RM-05、RM-06、RM-09、RM-10 |
| checkpoint governance | RM-01、RM-02、RM-03、RM-06、RM-09、RM-10 |
| true rollback | RM-04、RM-09 |
| partial rollback | RM-06 |
| irreversible 409 + visible lock | RM-01、RM-02、RM-03、RM-06 |
| reconciliation / external drift | RM-07、RM-09 |
| pause/resume/stop + stale invalidation | RM-08、RM-09 |
| substrate switching | RM-09、RM-10 |
| independent verifier | 全部；RM-03/RM-06 重点 |
| ledger / real-model overhead | 全部；RM-08/RM-10 重点 |
| OOD/未见表面 | RM-07 内容 holdout、RM-08 visual reskin、RM-10 substrate holdout |

每个 headline feature 至少有两个任务，只有 true heterogeneous same-value fanout 当前主要由 RM-09 承重；这是 MobileGym 现有 app 能力边界，不能假装三现有 fixture 已证明它。

---

## 9. Harness 升级任务卡

### H-00 — 先修 current-core P0

依赖顺序：

```text
A-01 surface routing
A-02 governance driver
A-03 heartbeat
A-04 secret
A-05 token aggregation
B-01 ledger
```

Gate：

- 0 wrong-surface actions；
- 0 post-stop actions；
- 0 provider/ledger mismatch；
- 0 secret in repo；
- inactive external change 可通过 SSE 落地。

### H-01 — `UserOpDriver`

建议新模块：

```text
taskvm_bench/evaluation/user_ops.py
taskvm_bench/evaluation/projection_client.py
```

数据模型：

```text
UserOp(
  op_id,
  kind = start|pause|resume|stop|local_patch|goal_patch|checkpoint|rollback,
  payload,
  expected_http_class,
  settle_policy
)
```

规则：

- 只调用 Projection 公共 HTTP/API client；
- 不持有 kernel/runtime/CUA；
- 不调用 `GovernanceService.handle`；
- 不直接造 GUI trajectory；
- 每次 op 发出后等待 SSE/projection barrier。

### H-02 — per-op barrier

一次 user op 的结束条件不是 sleep，而是：

```text
accepted(op_id)
AND verifier verdict landed
AND projection_revision >= observed_world_revision
OR timeout
```

必须记录：

- op issued；
- first GUI action；
- last GUI action；
- verifier completed；
- first correct SSE；
- settled。

### H-03 — MobileGym trial factory

建议：

```text
taskvm_bench/evaluation/mobilegym_factory.py
```

职责：

1. health；
2. reset；
3. seed；
4. 构造 `MobileGymSubstrateSession`；
5. 注册 Projection session；
6. 启动 runtime driver；
7. 返回隔离的 evaluation oracle；
8. trial 后 close + state integrity check。

CLI：

```bash
python -m taskvm_bench.evaluation.cli run \
  --suite rm-frozen \
  --substrate mobilegym \
  --condition taskvm-real-full \
  --model gpt-5.6-sol \
  --samples 5
```

### H-04 — real model stack factory

新增条件：

- `taskvm-real-full`；
- `taskvm-real-cua-only`（diagnostic）；
- `direct-cua-real`；
- `planner-cua-real`；
- `taskvm-template-control`。

同一条件内 model/version 固定；禁止运行中 fallback 到另一个模型而不改 condition id。

### H-05 — provider request executor

统一 `http_port.py` 与 `model_client.py` 的政策：

```text
one ProviderRequest -> one request_id -> one ledger row
```

字段：

- role；
- purpose；
- op_id；
- node_id；
- attempt；
- repair_of；
- status class；
- model；
- provider response id；
- prompt/completion/reasoning tokens（有则记，无则 null）；
- latency；
- error；
- retryable；
- screenshot hash。

### H-06 — 非侵入式 oracle

建议 verifier/evaluation 分离：

```text
Agent browser context
Observer context/store reader
```

强制 test：

```text
foreground_before == foreground_after
screenshot_fp_before == screenshot_fp_after
agent_action_count unchanged
```

### H-07 — 结果 schema

每个 trial：

```json
{
  "schema_version": 2,
  "git_sha": "...",
  "task_version": "...",
  "harness_version": "...",
  "model": "...",
  "substrate": "...",
  "sample_index": 0,
  "user_ops": [
    {
      "op_id": "...",
      "verdict": {},
      "world_diff": {},
      "protected_diff": {},
      "projection": {},
      "rollback": {},
      "ledger_request_ids": [],
      "artifacts": []
    }
  ],
  "trial_verdict": {},
  "failure_class": "...",
  "evaluation_error": null
}
```

所有原始产物落：

```text
eval_results/<run-id>/
  manifest.json
  trials/<task>/<condition>/<sample>.json
  artifacts/<...>.png
  reports/report.json
  reports/report.md
```

不进 git。

---

## 10. per-op 指标定义

令一次 user op 前后 oracle state 分别为 `S_i^-`、`S_i^+`，期望变化为 `Δ_i`，protected set 为 `P_i`。

### 10.1 核心二元指标

1. **Round-trip correctness**
   - 期望字段全部达到；
   - verifier 通过；
   - 不接受 CUA 自报 done。

2. **Non-interference**
   - `P_i` 内无未授权变化；
   - 未目标 app/object 无副作用。

3. **Projection fidelity**
   - settled 时 Projection 对用户可见变量与 oracle-visible canonical state 一致；
   - desired/observed/locked 状态正确。

4. **Rollback honesty**
   - reversible 字段恢复；
   - irreversible 字段不伪装恢复；
   - disposition 与逐 entry 事实一致。

5. **Irreversible honesty rate**
   - 应 409/partial 时确实 409/partial；
   - UI visible lock；
   - 0 hidden restore。

6. **Stale-action-free**
   - superseded epoch、pause 后、stop 后的 GUI action 数为 0。

7. **Ledger integrity**
   - provider request count = ledger rows；
   - 每条 row 可追到 op/node/attempt。

### 10.2 连续指标

- reprojection latency：
  - external state landed → first correct SSE；
- governance latency：
  - user op accepted → first compliant GUI action；
- verification latency；
- wall-clock；
- provider requests；
- prompt/completion/reasoning tokens；
- GUI actions；
- repair calls；
- cost：仅在 versioned pricing config 存在时计算，否则报告 token，不伪造货币值。

### 10.3 聚合规则

- **op verdict**：一次治理操作的原子判定；
- **trial success**：全部 mandatory ops round-trip + non-interference，且无 false irreversible success；
- **task success**：该 task 的 N samples 多数通过；
- **headline**：先按 task 求值，再做 task-level macro mean，防止长任务用更多 ops 主导结果；
- 二元比例：Wilson 95% CI；
- 连续量：mean + median + p90；
- confirmatory：按 task/trial cluster 的 bootstrap 或 mixed-effects logistic；
- 不把每个 op 当独立样本；
- 不报告 max/best-of-N。

### 10.4 TaskVM vs TemplateCUA delta

同一冻结 task/op schedule：

```text
Delta_RM = metric(taskvm-real-full) - metric(taskvm-template-control)
```

用途：

- 揭示 fakes 对真实模型带来的性能偏差；
- 不把 TemplateCUA 当 efficacy baseline；
- 结构收益与模型能力分开解释。

---

## 11. 迭代协议与 gates

### Stage 0 — preflight

- current-core P0 全绿；
- gateway 单请求 smoke；
- bridge health/reset/seed/observe；
- 0 secret；
- provider request=ledger row。

### Stage 1 — 单 op smoke

只跑 RM-04 的一个 X toggle，验证：

```text
user op -> Projection -> governance -> runtime ->
real CUA -> real GUI -> verifier -> SSE
```

任何层不得手工跳过。

### Stage 2 — 单 task 小闭环

推荐 RM-02：

- reversible X；
- irreversible WeChat；
- 2 checkpoints；
- 可见内容 grounding；
- 一条任务同时暴露最多问题。

Gate：

- 每个 op 有 verdict；
- 0 GT leak；
- 0 API write；
- 0 ledger mismatch；
- 0 wrong-surface；
- 失败 artifact 完整。

### Stage 3 — 三任务闭环

RM-02 + RM-07 + RM-09：

- cross-app；
- live drift；
- true heterogeneous fanout。

### Stage 4 — 10 条 engineering 全绿

允许改 harness/task/prompt，但数据全部标 development-only。

### Stage 5 — freeze

冻结：

- task YAML/fixture hash；
- user-op program；
- model；
- system prompt；
- action schema；
- bridge image/version；
- viewport；
- budgets；
- evaluator；
- analysis script。

### Stage 6 — pilot

`10 tasks × 3 main conditions × 3 samples = 90 real trials`

用途：估计失败类型、方差、QPM，不做 task 选择。

### Stage 7 — main

实验室可承担的主矩阵：

- `10 tasks × 3 main real conditions × 5 samples = 150 real trials`
- `4 discriminating tasks × 2 ablations × 5 = 40 real trials`
- `10 tasks × template control × 3 = 30 template trials`

合计：

- 190 real-model trials；
- 30 template trials；
- 220 trials 总计。

### Stage 8 — 预声明的 N 扩展

不是挑失败 cell 补跑。预先声明：

- 若 task-level headline CI 宽度超过阈值；
- 则所有 main cells 从 N=5 一致扩到 N=10；
- 不单独给“难看”的 cell 增样本。

---

## 12. 条件公平性

### Main conditions

1. TaskVM real-full；
2. Planner-CUA real；
3. Direct-CUA real。

### Targeted ablations

- TaskVM no-verifier；
- TaskVM no-replan。

### 公平资源

每 trial 完全相同：

- provider request cap；
- GUI action cap；
- wall-clock cap；
- same model；
- same screenshot cadence；
- same task seed；
- same user-op schedule；
- same oracle；
- same failure timeout。

TaskVM 的 compiler/architect/verifier cost 必须计入。

### baseline 如何接收用户治理操作

baseline 不具备 checkpoint/rollback，不应给它偷偷增加 TaskVM 能力。正确做法：

- 把同一用户意图作为可见的新用户消息输入；
- pause/stop 是 runner-level interaction boundary；
- baseline 无法恢复时如实失败；
- 不调用隐藏 state setter；
- 不把“没有 rollback topology”排除出评分。

---

## 13. 风险矩阵

| 风险 | 概率 | 影响 | 证据/触发 | 缓解与 gate |
|---|---:|---:|---|---|
| R1 网关 QPM/429 | 高 | 高 | `model_client.py:31-40` 自述模型配额差异，原始 probe JSON 不可见 | token bucket；单 key 串行起步；429 独立计数；无隐藏 retry |
| R2 真模型非确定性 | 高 | 高 | 当前 fake 同 seed 可复现不适用于 provider | sample index 代替“模型 seed”；N-sample；记录 provider metadata；不用 best-of-N |
| R3 bridge/时钟/append 稳定性 | 中高 | 高 | fixtures `:22-34,209-216` 有固定时钟与 append 约束 | 每 trial reset hash；固定 bridge image；append-aware oracle；一 worker 一 bridge |
| R4 GT leak | 中 | 致命 | fixtures 含 verifier-only ids/operators | 物理模块边界；repo-wide import gate；prompt recorder + noleak scan |
| R5 user-op API 与公开 port 不匹配 | 高 | 高 | 当前 `_drain` 直连内部对象 | 先建 ProjectionClient contract；禁止 driver 持 kernel/runtime |
| R6 真模型失败率高 | 高 | 中高 | 这是待测变量 | 失败原样落盘；bounded repair；任务 engineering 与 frozen eval 分离 |
| R7 MobileGym screenshot/action 兼容 | 高 | 高 | 当前 HttpCUA text-only | vision+text；完整 action schema；每 app smoke；artifact capture |
| R8 C-2 与 retry 张力 | 高 | 高 | duplicate ledger + model_client hidden retry | request_id 单一 owner；每 retry 独立 row；repair 属 L4 |
| R9 oracle observer effect | 高 | 高 | 读 X 会 `open_app` | 独立 observer；前后 foreground/screenshot invariant |
| R10 多 surface 路由 | 高 | 致命 | runtime 全部 surface 0 | A-01 先修；RM-09 作为承重 gate |
| R11 post-stop/stale actions | 高 | 致命 | stop 动态反例 | lifecycle state + generation check；RM-08 gate |
| R12 task overfitting/cherry-pick | 中高 | 高 | 工程目标“10 条全绿”天然有风险 | development-only 标签；freeze 后不改；held-out visual variants |
| R13 伪重复统计 | 中 | 高 | 每 trial 多 ops | task-level macro；clustered CI；不把 op 当独立 N |
| R14 cross-trial contamination | 中 | 高 | bridge 单 active reality | 一 bridge/worker；sid+state hash；serial fallback |
| R15 public secret | 已发生 | 致命 | `model_client.py:29` | 立即 rotate/scrub；secret scanning |
| R16 provider client 双实现漂移 | 中 | 高 | stdlib `http_port` vs SDK `model_client` | 共享 retry/status/ledger spec；contract tests |
| R17 OOD 过度宣称 | 高 | 中高 | 当前 world 统一 `k=v` | semantic OOD 与 visual OOD 分开；揭盲 reskin |
| R18 append/contains 判卷模糊 | 中高 | 高 | fixture 已承认 exact vs contains | typed predicate；禁止字符串临时特判 |
| R19 任务的真实可完成性 | 中高 | 中高 | MG app 能力有限 | 先 1 op→1 task→3 task；不铺大矩阵 |
| R20 成本失控 | 中 | 中高 | real-full 三角色 + repair | 硬 request cap；实时 ledger；停止阈值；先 N=3 pilot |

---

## 14. 面向 CHI 高质量的证据包

系统 benchmark 不应只给一张成功率表。每个 representative task 应有一个可审阅 evidence bundle：

1. **user-op timeline**：用户何时 patch/checkpoint/rollback/pause；
2. **真实 GUI trace**：每个动作的 screenshot before/after；
3. **world diff**：expected、actual、protected；
4. **projection trace**：SSE revision 与 settle latency；
5. **verifier evidence**：独立判定，不是模型自评；
6. **reversibility disposition**：complete/partial/409 的逐 entry 原因；
7. **ledger**：每个真实 provider request；
8. **cross-substrate pair**：相同语义、不同低层轨迹；
9. **失败案例**：至少按 failure taxonomy 展示代表性失败，不只展示 demo 成功。

最有力的论文图建议围绕 RM-06 或 RM-09：

```text
User edits one VM variable
 -> two apps change through different GUI traces
 -> external drift arrives
 -> projection re-synchronizes
 -> user rolls back
 -> reversible part restores
 -> irreversible part visibly locks
 -> verifier proves both “changed” and “did not change”
```

这比“Agent 完成了一个任务”更直接体现 governance over autonomy。

---

## 15. 工业可扩展性：不要靠扩大 task 数量来证明

实验仍保持约 10 条，但工程接口必须显示可扩展路径：

- Substrate adapter registry；
- versioned TaskSpec/UserOpSpec；
- provider abstraction；
- one-request-one-ledger；
- isolated worker/bridge；
- artifact manifest；
- per-op oracle plugins；
- task feature coverage registry；
- rate/cost scheduler；
- task pack 可以新增，但 frozen task 不可原地修改。

工业潜力的证据不是“800 个参数模板”，而是：

```text
新增一个 substrate/task pack
不改 kernel/runtime/governance
只实现 adapter + evaluator + task spec
即可进入同一 per-op verdict 与统计管线
```

RM-10 应成为这一点的最小可复现实证。

---

## 16. 建议的 PR/依赖顺序

```text
PR-0  Secret rotation + docs truth fixes
PR-1  Surface binding resolver + multisurface tests
PR-2  Governance driver lifecycle + post-stop tests
PR-3  Production inactive heartbeat + SSE integration test
PR-4  Single-owner ledger + aggregation schema v2
PR-5  Vision CUA + complete GuiAction schema
PR-6  Provider retry/error policy
PR-7  Non-invasive MobileGym oracle + bridge isolation
PR-8  ProjectionClient + UserOpDriver + per-op barrier
PR-9  MobileGym runner/CLI + real-full condition
PR-10 First 3 tasks
PR-11 Full 10-task engineering pack + feature matrix gate
PR-12 Freeze tooling + pilot/main analysis
```

不可并行越过的依赖：

```text
PR-1/2/3/4
   -> PR-8
   -> PR-9
   -> real-model trial
```

---

## 17. 最终 go/no-go gates

### RM first-call gate

- [ ] 源码无默认 key；
- [ ] exact SHA/run manifest；
- [ ] provider request=ledger row；
- [ ] screenshot 真正送达 CUA；
- [ ] complete action schema；
- [ ] fatal/retryable 分层；
- [ ] GT prompt canary 零泄漏。

### First full-task gate

- [ ] user-op 只走 Projection public API；
- [ ] 多 surface 路由正确；
- [ ] stop 后 0 动作；
- [ ] inactive external change 自动 SSE；
- [ ] oracle 不改前台 UI；
- [ ] 每 op 有独立 verdict/artifact。

### 10-task freeze gate

- [ ] feature matrix 无空白；
- [ ] task 不是参数复制；
- [ ] 1–2 个 held-out variant 未参与调试；
- [ ] common hard budgets；
- [ ] failures 不丢弃；
- [ ] task/prompt/harness/version hashes 固定。

### Paper gate

- [ ] development runs 与 frozen runs 物理分目录；
- [ ] raw JSON 可复核；
- [ ] Wilson + task-macro + clustered analysis；
- [ ] diagnostic/oracle condition 不进 headline；
- [ ] 270 fake/world 只作为历史结构性结果，明确原始 JSON在本归档不可见；
- [ ] 不把 semantic OOD 写成 visual OOD；
- [ ] 不把 partial/409 写成 rollback success。

---

## 18. 最终判断

### 现在必须修，不可借“RM 是新功能”放宽

1. 多 surface 路由；
2. Projection→driver 的 pause/resume/stop；
3. production inactive heartbeat；
4. public credential；
5. token mean 报告错误；
6. architecture gate 未覆盖全 `taskvm`；
7. README route/workflow 不真实；
8. 现有 benchmark 的公平预算表述；
9. OOD 范围过度命名；
10. 40-template legacy generator 失实；
11. matrix 命令 typo。

### RM 强依赖，但当前 deterministic fake/world 不启用它们时不影响状态推进

1. CUA duplicate ledger；
2. vision/action schema；
3. retry/error taxonomy 与每请求记账；
4. 非侵入式 MobileGym oracle；
5. user-op driver；
6. per-op result schema；
7. MobileGym CLI/trial factory；
8. real-full condition；
9. fixture rollback 描述更新；
10. bridge trial isolation / semantic mutate route guard。

### 对论文最重要的策略

不要把 RM wave 做成“把 270 再跑一遍真模型”。正确方案是：

- 少量、承重、跨 app、逐 user-op；
- 先让完整链路具备可调试性；
- 先 engineering 全绿，再冻结；
- 冻结后允许失败；
- 用真实 GUI evidence、独立 verifier、SSE 与 rollback honesty 构造因果证据；
- 以 RM-09/RM-10 证明“同一任务语义跨 substrate，不同轨迹，同一治理结果”。

这会比单纯提高任务数量更接近 TaskVM 的真正贡献，也更能让审稿人一眼看出从实验室原型到工业 adapter ecosystem 的扩展路径。
