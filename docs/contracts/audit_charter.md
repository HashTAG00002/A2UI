# Audit Agent Charter — 冻结（适用于所有 Agent / 层的审计）

> 状态：**冻结**（2026-08-15，E38 Evidence Protocol Amendment；取代 2026-08-14 E28 版）。任何审计 Agent——无论审 Agent A 的 Kernel、Agent B 的 Substrate、Agent C 的 Architect、Agent D 的 Projection、Agent E 的 Runtime/Verifier、Benchmark 还是集成——开工前必须读完本页 + [layered_ownership_protocol.md](layered_ownership_protocol.md) + **被审层的冻结合同 doc**（如 Kernel 审看 [kernel.md](kernel.md)）。
> 一句话：**审计只验"被审层的冻结合同是否真的成立 + 跨层是否真的干净 + 文档与代码是否一致"。不重设计、不跨层重证、不跨轮 drip-feed；对关键结论必须锚定 immutable commit 并取得足够强的 ground truth。**

## 0. 为什么有这份 Charter

过去几轮审计发生过三类失败。本页用来堵死它们；它们会在任何层的审计复发，不只 Kernel。

### 0.1 Treadmill

每轮审计"再发现 N 条"，因为不变量没有事先枚举冻结，审计一轮挖一层、永无止境；被审 Agent 被迫一轮轮补。

Agent A 的 Kernel 审计是触发本 Charter 的实例：v2 → v3 → v4 三轮 escalation。

### 0.2 Hostile-caller drift

审计把被审层当成对抗任意恶意第三方的防火墙，要求它重证下游/上游层本该自己保证的内容合法性。

但这些层是我们自己控制的模块，不是任意插件。

这会催生：

- 重复 `if` 防御；
- 跨层重复 validation；
- 测试把错误行为锁成正确；
- prototype 被无意义地推向 production-hardening。

矫正后的模型见 [layered_ownership_protocol.md](layered_ownership_protocol.md)：**内容合法性由唯一 producer / domain constructor 负责；时序合法性由 Kernel 负责。一个性质只有一个 owner，不在下一层重证。**

### 0.3 Evidence-channel drift

跨环境审计中，曾出现：

```text
branch/main 网页快照
        ↓
缓存 / crawler snapshot 过期
        ↓
看到旧代码

commit ledger / .mrules
        ↓
声称修复已经完成

exact Git object
        ↓
实际代码状态未知
```

如果审计 Agent 直接选择自己更相信的一边，就可能产生两种错误：

```text
旧网页误报 bug
→ 已修代码被重新返工
→ treadmill

ledger 自报"已修"
→ 未实际 landing 的代码被误判 PASS
→ false lock
```

因此从 E38 起增加一条通用证据原则：

> **审计对象必须先固定 immutable commit SHA。Branch name、GitHub main 页面、commit message、.mrules、handoff、CI 摘要都可以是 evidence，但都不能替代 exact-SHA implementation ground truth。**

本修订只改变取证纪律，不新增任何产品 invariant、不扩大任何 Agent 的历史使命。

## 1. 审计的唯一原则

审计不得要求被审层重证别的层拥有的内容合法性。审计只验：被审层自己的冻结合同是否真的成立、跨层边界是否真的干净、文档与代码是否一致。

被审层不是 hostile-caller firewall。任何：

> "X 层可能喂坏东西，所以 Y 层必须再检查一次"

的论证，一律 out-of-scope。那是 X 层自己的 contract test 应负责的事情。

同时：审计结论必须针对一个明确、不可变的代码对象。因此每轮审计除声明 layer/scope 外，还必须声明：

- Audited layer:
- Frozen contract:
- Audit target commit SHA:
- Repository identity:
- Bounded scope:

若用户指定了 SHA，则必须以该 SHA 为唯一审计对象。若用户要求"当前 main"，审计 Agent 应先解析并记录当时的具体 commit SHA，再开始取证。

## 2. 审计对象

本 Charter 适用于对任何 Agent / 层的审计：

- Agent A — Kernel / Domain（合同：[kernel.md](kernel.md)）
- Agent B — Substrate（合同：[substrate.md](substrate.md)）
- Agent C — State Compiler / Task Architect / Governance（合同：[architect.md](architect.md)）
- Agent D — Projection / Frontend（合同：**将来** projection.md —— 截至本修订尚未冻结；审计前必须先要求冻结，不得做开放式审计）
- Agent E — CUA Runtime / Verifier / Rollback（合同：[runtime.md](runtime.md)）
- Benchmark / 集成 / 跨设备（合同：各自冻结 doc）

审计开工第一步：

1. 指明本轮审哪个 Agent / 层；
2. 定位该层冻结合同；
3. 固定 exact commit SHA；
4. 枚举本轮 bounded scope。

没有冻结合同的层：先要求该 Agent 冻结合同，不得在合同未冻结时做开放式审计。否则又回到 treadmill。

## 3. In-scope

审计允许报告的只有以下五类。

### 3.1 被审层冻结合同违反

该层合同 doc 列出的 invariant / API / type，代码声称成立但实际不成立。

每条必须引用：

- contract clause
- file:line
- exact code path
- counterexample

### 3.2 Doc-vs-code lie

被审层文档声称某行为已经实现，但 exact audited commit 中代码没有实现；或者代码行为与冻结文档相反。

这是最尖锐、最欢迎的 finding。

注意：**branch webpage != exact audited commit**。不能因为一个可能过期的 `blob/main/...` 页面与合同不同，就直接形成 doc-vs-code finding。必须先核 exact SHA。

### 3.3 分层泄漏

包括但不限于：

- reverse import；
- 跨层 import concrete implementation；
- Domain 获得 mutable runtime state；
- 被审层残留它不该拥有的内容 validation；
- Runtime 获取 Evaluation-only capability；
- 上层知道具体 substrate implementation。

跨层边界优先由 architecture gate 机器强制，审计复核 gate 是否真的针对 exact audited commit 为绿。

### 3.4 次生 Regression

本轮 fix 或后续其他层提交，对已经 frozen/test-pinned 的行为造成了新的破坏。

必须证明：

```text
old frozen behavior
        ↓
specific later change
        ↓
reproducible regression
```

不能因为网页显示旧内容就推断 secondary regression。

### 3.5 Test oracle 本身错误

测试绿，但 assertion 锁定的是 buggy behavior。

在 scope，但必须同时给出：

1. 当前测试锁定什么；
2. 冻结合同要求什么；
3. 正确行为应该是什么；
4. 为什么当前 oracle 错。

不能仅因为审计 Agent 偏好另一种行为就改测试。

## 4. Out-of-scope

以下行为一旦出现，对应 finding 视为无效，不得阻塞冻结。

### 4.1 发明新 invariant category

审计只能验证已冻结合同里的 invariant。新 category（"还应该防 X"）必须走对应层 RFC（Kernel 见 [kernel_rfc_backlog.md](kernel_rfc_backlog.md)，Runtime 见 [runtime_rfc_backlog.md](runtime_rfc_backlog.md)，其他层走各自 RFC 队列）。它不是 audit finding，不阻塞冻结。

### 4.2 重审已 frozen + test-pinned 的 invariant

一项 invariant 已经 contract frozen + implementation green + corresponding test pinned，下一轮不得以同 scope 重开。

除非：有具体 secondary regression，或有正式批准的新 RFC。否则就是 treadmill。

### 4.3 Hostile-caller / firewall 框架

禁止："假设某层会恶意/任意喂坏东西，因此下一层必须再验证。"

这不是本学术 prototype 的系统模型。内容 validation 归 producer / domain constructor。

### 4.4 要求跨层重证

例如：

- 审 Kernel 时不得要求 Kernel 验 workflow shape；那是 C / TaskArchitecture 的职责。
- 审 C 时不得要求 C 验 observation freshness；那是 E / Runtime-Verifier 的职责。
- 审 B 时不得要求 B 验 contract desired consistency；那是 C 的职责。
- 审 D 时不得要求 Frontend 自己重新执行 CUA 来确认截图新鲜度。
- 审 E 时不得要求 Runtime 重新验证 Architect artifact 的静态 schema。

### 4.5 用行数 / scope 当 gate

验收是结构性 bar，不是行数。不得因为"文件太长 / 类太大 / 看起来还能再拆"重新开启减肥轮（先例：Agent A 的 kernel.py ≤600 行指标已由 RFC-001 裁决退役为 soft diagnostic）。除非 frozen contract 明确规定了结构约束。

### 4.6 重设计架构

架构已经冻结。审计只验证 conformance，不 redesign。"我更喜欢另一种设计"是 preference，out-of-scope。

### 4.7 开放式"再找更多 bug"

禁止。开工前先枚举 scope。枚举完整后即停。不得 round 1 → 3 findings、round 2 → 再想起 4 findings、round 3 → 再扩大 invariant。

### 4.8 把证据冲突伪装成实现 defect

若 exact SHA 尚未取得，且 branch webpage 与 ledger 冲突，不得任选一边形成 defect finding。

正确状态是 **EVIDENCE HOLD**：先完成 exact-SHA 取证。证据不足不是代码 defect。

## 5. 审计自身纪律

### 5.1 开工第一步：声明 Scope

必须明确：

- Audited layer:
- Frozen contract:
- Audit target SHA:
- Repository:
- Contract clauses being checked:
- Known handoff obligations:
- Explicitly excluded scope:

不声明 scope 的 finding 不接受。

### 5.2 每条 Finding 必须 Forensic

每条至少包含：

- DEFECT / SPEC-GAP / PREFERENCE / EVIDENCE HOLD 分类；
- Owner；
- Contract clause；
- Exact audited SHA；
- file:line；
- code quotation / symbol；
- Counterexample sequence；
- Why contract is violated；
- Dynamic reproduction（行为类 claim 必须）；
- Minimal repair boundary；
- Expected gate transition。

禁止："这里看起来可能有问题。"

### 5.3 行为类 Claim 必须 Ground-truth

涉及 runtime behavior、model-call count、state transition、rollback、interrupt、fast path、verification 等行为时，必须跑 pytest、existing gate 或最小 probe。

诚实说"我没跑"是底线。但对于 load-bearing LOCK claim：只读代码而不实跑，不足以形成最终 PASS。

同样，"Coding Agent 说它跑绿了"属于 evidence，但不等于本轮独立 reproduction。

## 5A. Exact-SHA Evidence Protocol

本节从 E38 起冻结，适用于所有 Agent / 层。

### 5A.1 先固定不可变审计对象

所有代码级审计必须针对 full commit SHA，而不是只针对 main / HEAD / latest / 网页当前内容。

推荐记录：

```bash
git rev-parse HEAD
git remote get-url origin
git cat-file -t <SHA>
git log --oneline --decorate -N
```

目的不是增加工程流程，而是回答："我们到底在审哪一份代码？"

### 5A.2 证据等级

不同 evidence 强度不同。从高到低：

**Level 1 — Exact Git object / local clean checkout**

最强代码证据：

```bash
git show <SHA>:path/to/file
git ls-tree
git cat-file
git hash-object
```

如果当前 working tree 满足 `HEAD == audited SHA`、`git status --porcelain == empty`，且 `git hash-object working/file == git rev-parse <SHA>:working/file`，则当前工作区对该文件与 audited SHA 逐字节等价。在该工作区执行测试，可视为针对该 exact SHA 的行为 reproduction。

**Level 2 — SHA-addressed immutable remote content**

例如 `.../<owner>/<repo>/<FULL_SHA>/<path>`（SHA 寻址的 raw 内容）。可以作为强代码证据。重点是路径使用 immutable commit SHA，不是某个特定域名天然可信。网络代理、crawler、缓存层仍可能失败，因此若它与 Level 1 冲突，以 Git object 为准。

**Level 3 — Exact commit diff / tree page**

可以证明：这个 commit 改了哪些文件；parent 是谁；某文件是否在该 commit 被修改。但如果需要证明"最终 blob 中具体实现是什么"，最好继续读取 exact blob，而不是只看 commit message。

**Level 4 — Test / CI / evaluation output**

原始测试输出是强行为 evidence。必须区分"本轮 Audit Agent 独立复跑"和"Coding Agent / commit ledger 记录的历史测试结果"——前者证据更强。

**Level 5 — Commit message / .mrules / handoff / Agent report**

这些是 claim evidence，不是 implementation ground truth。它们非常有价值——可以告诉审计 Agent 应检查什么、谁声称改了什么、应复现哪个 gate——但不能单独证明代码已经 landing。

**Level 6 — Moving branch webpage**

例如 `blob/main/...`、`raw/.../main/...`。只能作为导航或辅助 evidence。因为 main 是可移动 ref + 网页/crawler/cache 可能过期，不得用它推翻 exact-SHA Git object。

### 5A.3 Branch 页面与 Exact SHA 冲突时怎么办

如果出现 `blob/main/compiler.py → 旧代码`、`commit ledger → 声称已修`、`exact SHA → 尚未直接读取`：

正确审计动作不是猜哪边对，而是：

1. 固定 audited SHA；
2. 读取 exact blob；
3. 核 parent / file history；
4. 必要时 hash cross-check；
5. 独立跑对应行为 gate。

在 1–5 完成前：`status = EVIDENCE HOLD`。不是 PASS，也不是 FAIL。

### 5A.4 EVIDENCE HOLD 的语义

EVIDENCE HOLD 表示：当前存在足以影响裁决的证据冲突或关键证据缺失，但尚未证明代码违反冻结合同。

因此：

- EVIDENCE HOLD != FAIL；
- EVIDENCE HOLD != REQUEST REWORK。

在 Hold 期间禁止：要求 Coding Agent 猜测式返工、新增防御代码、改 test oracle、扩大 audit scope。只做取证。

一旦 exact-SHA ground truth 补齐：证明确有违反 → FAIL / finding；证明代码正确 → PASS / 解除 HOLD。

### 5A.5 跨环境审计

若 Audit Agent 的环境能访问 web 但没有 repository checkout，而 Repository Host Agent 拥有 exact local checkout，则允许采用"宿主取证包"。最小取证包包括：

1. `git rev-parse HEAD`
2. `git remote get-url origin`
3. `git status --porcelain`
4. audited SHA object existence
5. target file exact blob content
6. target file blob hash
7. working-tree hash cross-check（若适用）
8. relevant git log / parent chain
9. exact gate command
10. raw test output + exit code

对于关键 LOCK blocker，建议同时提供代码级证据 + 行为级证据（例如：exact blob 显示 recoverable drift → fast path，加上 pytest 证明 recoverable drift → 0 model call）。这比任何网页截图或 Agent 自述更强。

### 5A.6 Exact-SHA Evidence 解决冲突后，不得继续借题扩审

例如：网页旧代码 → EVIDENCE HOLD → exact blob + pytest 证明实际已修。此时该问题应 **CLOSE**。

不得继续"既然都重新看了，不如再顺便检查五个新的 corner case"——除非它们属于开工时已经声明的 bounded scope。否则就是借 Evidence Hold 重启 treadmill。

### 5A.7 Evidence 错误与代码错误必须区分

审计 Agent 自己错误地：使用了过期 branch snapshot、错认 commit、没有跑测试却过度推断、把 ledger 当 ground truth——这些属于 **audit evidence defect**，不是 **product implementation defect**。

修正方式是：补齐证据 → 修正裁决。而不是要求 Coding Agent 改代码。

## 5B. Finding 分类

每条 finding 必须自标：

- **DEFECT**：代码违反 frozen contract。→ in-scope，可阻塞。
- **SPEC-GAP**：合同从未要求该行为。→ RFC，不阻塞。
- **PREFERENCE**：只是审计 Agent 的设计/美学偏好。→ out-of-scope。
- **EVIDENCE HOLD**：还不能确定代码究竟是否违反合同。→ 只取证，不返工，不阻塞成 FAIL。

把 SPEC-GAP / PREFERENCE / EVIDENCE HOLD 伪装成 DEFECT，都是违反本 Charter。

## 5C. One-owner Routing

每条 finding 必须标注 owner：A / B / C / D / E / F / G。

如果发现问题其实属于别层：路由到那个 owner 的 contract/gate。不能因为"最终表现发生在被审层"就要求被审层替别人修。

特别是 **Layer formal lock pending cross-layer debt** 必须区分 Owner-complete vs. Layer formally locked：若被审 Agent 自己已经完成全部 owner scope，但 formal layer lock 等待 D/E/G 清 legacy debt：

```text
OWNER-COMPLETE
CODE-FROZEN
LOCK PENDING <registered debts>
```

而不是把原 Agent 判回 FAIL。

## 5D. 一次性枚举 + 完整性承诺

开工即声明：本轮 bounded scope 的 finding 将一次性枚举完整；下一轮只允许检查这些修复造成的 secondary regression。

对漏报的非次生 bug 负责。不得用"再审一轮看看还有没有问题"作为默认流程。

## 5E. 诚实 Reporting

必须明确区分：

1. 我亲自执行并看到的；
2. 我直接读取 exact blob 得到的；
3. 历史 ledger 声称的；
4. 别的 Agent 提供的宿主取证；
5. 我尚未验证的。

禁止：没跑测试却写"tests pass"；没读 exact code 却写"implementation confirmed"；只有 commit message 却写"代码已 landing"；有 evidence conflict 却强行 PASS；有 evidence conflict 却强行 FAIL。

## 6. 验收 Bar

满足全部才算该层 formal frozen；缺一不可。具体 grep target / invariant / tests 由该层合同 doc 定义，本页只定义跨层通用结构。

### 6.1 Contract Gates

被审层冻结合同全部满足。每个 frozen invariant 至少有一个有效 test / gate 钉死。没有被错误 test oracle 锁住的合同违反。

### 6.2 Test Gates

`pytest <对应层测试>` 全绿。允许存在的 skip 必须：是合同明确登记的 pending、有明确 owner、不能伪装成 PASS。

### 6.3 Compile Gate

`python -m compileall <对应层>` exit 0。

### 6.4 Layering Gate

例如：Domain 只依赖允许的底层；Kernel 不 reverse-import Runtime；Runtime 只看到 Substrate PORT；Runtime 看不到 EvaluationEnvironment；上层不 import concrete substrate；Agent 不跨 ownership 边界。具体以各层冻结 contract 为准。

### 6.5 Forbidden Residue Gate

被审层不应拥有的：hidden ID、API mutation、duplicate validation、legacy fallback、direct concrete imports、old model role，应按该层合同要求清零。

### 6.6 RFC Gate

不存在未经 RFC 批准但被审计强行新增的新 invariant category。

### 6.7 Evidence Gate

正式 LOCK 前，必须明确：

- Audited exact SHA = ?
- 对 load-bearing 最终 blocker / closure：代码行为已由 exact-SHA 证据确认；行为 claim 已由对应 test/probe ground-truth；
- 若使用跨环境宿主证据，身份、blob、测试输出能够闭环；
- 不存在未解决的 EVIDENCE HOLD；
- Moving branch webpage 单独不能满足本 gate。

## 7. "Frozen"之后

某层 Bar 全部满足：PASS / FORMALLY LOCKED / FROZEN。该 Agent 退出主开发路径。此后审计不得以同 scope 重开该层。只允许：

### 7.1 Secondary Regression

后续某个具体 change 引入了可复现的新破坏。必须给出 regressing commit/path + reproduction。

### 7.2 Approved RFC

正式批准的新 feature / invariant。

### 7.3 不允许因为 stale evidence 重开

例如：已 frozen layer + 后来某个 `blob/main` 网页显示旧代码 → 不得直接重开。先按 §5A 固定 exact SHA。如果 exact object 与 frozen evidence 一致：CLOSE。这不是 regression。

重启某层（新 feature、新合同条目）需新 charter 或 RFC 授权，不得复用旧审计的开放式授权。

## 8. 推荐的审计输出模板

每轮最终回复建议严格使用以下结构：

```text
AUDIT TARGET
============
Layer:
Owner:
Frozen contract:
Exact commit SHA:
Scope:

EVIDENCE
========
Exact code inspected:
Behavior gates independently run:
Host-provided evidence:
Evidence limitations:

VERDICT
=======
PASS / CONDITIONAL PASS / FAIL / EVIDENCE HOLD

FINDINGS
========
F1 ...
  Classification:
  Owner:
  Contract:
  SHA:
  file:line:
  Counterexample:
  Reproduction:
  Minimal fix:
  Gate transition:

LOCK STATUS
===========
OWNER-COMPLETE?
CODE-FROZEN?
FORMALLY LOCKED?
Registered external debts?

NEXT ACTION
===========
Only the minimum action required by the frozen contract.
```

如果没有合法 finding：明确写"本轮 bounded scope 内没有发现新的 contract defect"。不要为了填满报告制造 finding。

## 9. 审计的角色边界

审计是冻结契约的守门人，不是架构师，不是跨层防火墙设计师，也不是"永远还能再找一个 bug"的红队。

它验证：已经冻结的东西是否真的成立。它不发明：还没冻结的东西应该存在。

同时：审计必须对自己的证据链负责。严格审计意味着：

```text
真实 bug 不放过
+
不存在的 bug 不制造
+
证据不足时不猜
```

而不是"越防御越严格、越多 finding 越专业"。

## 10. 最终元原则

整个项目的审计遵循以下顺序：

```text
Frozen Contract
       ↓
Single Owner
       ↓
Immutable Audit SHA
       ↓
Exact Implementation Evidence
       ↓
Independent Behavioral Evidence
       ↓
Claim-to-Code Consistency
       ↓
PASS / FAIL
```

若中间缺失关键证据：**EVIDENCE HOLD**，不是猜测。

若发现新设计问题但合同未要求：**RFC**，不是 blocker。

若被审层自己已经完成，只剩别层登记债务：**OWNER-COMPLETE / CODE-FROZEN / LOCK PENDING**，不是把原 Agent 拉回来返工。

审计为漏掉冻结合同中的真实 defect 负责，也为凭错误证据制造不存在的 defect 负责；不为"没有把学术 prototype 防御到 production-grade"负责。

违反本 Charter 的 finding 视为越权，作废，不阻塞冻结。
