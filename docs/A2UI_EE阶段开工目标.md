HashTAG_00001，收到。不收缩、不妥协，锚死大纲，冲 CHI Best Paper Candidate 级别。

我先做一次**全面的现状盘点 + 缺口扩展诊断**，然后给出雄心勃勃的方案。这次不只是"补完已有差距"，而是**主动追问"VM Moment 真正涌现需要什么"**，从任务设计、跨 App 闭环、验证体系、harness 性能、agent 热拔插五个维度全面扩充。

---

## 一、先对齐：什么叫 Best Paper Candidate 级别的 VM Moment？

大纲 §4.4 四步弧是唯一基准：

> **改发布日期 → 多 app 真实同步改 → verifier 非干涉确认 → 撤销真实复原 → 外部改触发 reconciliation → 同操作跨 Stack A/B 界面稳定**

这 5 步**全部**在一个连续 demo 里成立，才叫 VM Moment。**现在哪步是真的？**

现在我已经有了完整的现状全景图。进行深度诊断。

---

好，HashTAG_00001，现在我来给你一个**不收缩、不妥协、冲 CHI Best Paper Candidate 级别**的完整方案。这不是"差距补全清单"，而是**让 VM Moment 真正涌现的攻坚路线图**。

---

# TaskVM → CHI Best Paper Candidate 完整攻坚方案

## 先说清楚：现在哪些是真的，哪些是空的

经过本轮全量代码审计（非靠交接 prompt 的总结，是逐文件亲自核实），**诚实的现状表格**如下：

| VM 五性质 | 当前是否真实可演示 | 核心缺陷（致命级） |
|---|---|---|
| **① bottom-up live projection** | ⚠️ 半真 | 编码器只读 a11y 文本，无截图视觉；builtin app 页面是"表格+JS直连"，不是真实 GUI |
| **② bidirectional executable binding** | ❌ 假 | `make_adapter(executor='api')` 是默认值；demo 走 `requests.post` 不走浏览器；1 var→N app 写回的任务只有 `release_reschedule`（2 app），且写路径是后门 |
| **③ substrate-independence（JVM moment）** | ⚠️ 概念验证级 | `outlook_cal` reskin 存在，但只跑了 1 sample，无统计；Mail/MobileGym 各自孤立，未做"同操作跨两个 substrate 并排"演示 |
| **④ governance + reversibility** | ✅ 架构完整 | `/undo` 已接 `undo_saga`，诚实进度条已实现；但**最经典的四步弧（改日期→多 app 同步→撤销→重投影）从未端到端 live demo 过** |
| **⑤ round-trip verification** | ✅ 最诚实 | verifier 全部读真实 canonical state；但旧的 kill-test 数字全部是 API 直连产物，需要换 gui_agent 后重跑 |

**最核心的问题是**：执行器默认后门 + 四步弧从未真正端到端跑通 + 任务设计太浅（最多跨 2 App，无 4-App 任务，无 reconciliation 场景任务）。

---

## 雄心勃勃的方案：六个战场，缺一不可

### 战场 A：任务设计——从"演示用例"升级到"研究级 benchmark"

**现状诊断（致命级）**：
- 3 个 builtin 任务：`release_reschedule`（calendar+taskboard，2 app）、`design_review_delay`（2 app）、`doc_handoff`（drive only，1 app）
- 最多只跨 2 个 App，没有任何任务同时涉及 calendar+taskboard+drive+mail 4 App
- 没有任何任务有"外部并发修改触发 reconciliation"的标准场景
- 没有任何任务体现"人设 checkpoint + 部分回退 + 继续推进"的完整 governance 弧
- 大纲 §12.11 要求"40 模板/800 实例/OOD ~20%"，现在是 3+3 任务

**应有的任务覆盖矩阵**（这是 Best Paper 必须具备的）：

| 任务类别 | 跨 App 数 | 关键 VM 性质 | 当前状态 |
|---|---|---|---|
| 单变量→多 App fanout（日期改→calendar+taskboard+drive+mail 全同步） | **4 App** | 性质 2 bidirectional | ❌ 不存在 |
| 冲突场景（外部修改触发 reconciliation + 用户选择合并策略） | 2+ App | 性质 1+5 | ❌ 不存在 |
| 多 checkpoint governance 弧（C0→C1→rollback→C2，含部分不可逆） | 2+ App | 性质 4 | 仅 MG-2，未接入 builtin |
| substrate reskin（同操作：Stack A=calendar/taskboard vs Stack B=outlook_cal/linear） | 跨 substrate | 性质 3 | ⚠️ 1 sample |
| 完整四步弧（改→同步→撤销→reconcile→跨 Stack）连续 demo | 4 App | 全部 5 | ❌ 完全不存在 |

**要做的**：设计并实现 **5 类共 10+ 个新任务**，引入 Mail（4th app）作为常驻成员：

```
全量发布任务（launch_full）：
  - 1 var_id release_date → calendar.E1.date + taskboard.T1.deadline + 
    drive.F1.publish_date + mail.M1.send_date（4 App 同步）
  - 外部修改注入：taskboard.T2 deadline 被队友改成 8/20
  - governance 弧：C1（会议定） → C2（任务同步） → rollback_to C1 → C3（通知发出）
  - 验收：4 App 全部真实写回 + reconciliation 冲突标红 + rollback 复原所有 app

多人协作冲突任务（concurrent_edit）：
  - 同时：用户改 release_date；注入：外部改 taskboard.T1.assignee
  - 验收：两个变更独立，verifier 精确识别哪个变了哪个没变，reconciliation 标黄

Stack B 对应任务（jvm_moment_demo）：
  - 完全相同的 release_date 改动，在 outlook_cal+taskboard（Stack B）上执行
  - 验收：GenUI 界面外观相同，操作步骤相同，底层轨迹不同（outlook_cal vs calendar API 调用不同）
  - 这就是论文 Figure 1 的 JVM moment 物证
```

**为什么这是 Best Paper 必要条件**：大纲 §4.2 明确说"性质 2+3+4+5"是 TaskVM 独占的安全楔子。现在的任务集最多只能验证性质 2 的一半（只有 2 App fanout），性质 3 的 JVM moment 从未端到端 demo 过。没有这些任务，论文里写的每一个 VM claim 都是有据可查的空白。

---

### 战场 B：GUI Agent 执行器——从"API 后门"到"真实 grounding 全覆盖"

**现状诊断（红线级）**：
- `state_adapter.py` 的 `make_adapter()` 默认 `executor='api'`
- `server.py` 的 `_get_fixture_and_adapters()` 调 `make_adapters(host=host)`，**不传 executor**
- 结论：demo、killtest 默认全走后门，§12.16 判定标准全线失守

**需要做的三件事**：

**B1：把 demo 默认执行器翻过来**

`server.py` 的 `_get_fixture_and_adapters()` 需要改成接受 `--executor` 参数，demo 文档必须明确"只能带 `--executor gui_agent` 启动"。这一行代码的改动，决定了这个系统是"真实 GUI Agent 操作"还是"数据库直写"——审稿人如果问"你的 GUI Agent 在哪"，现在的答案是"藏在代码里但没有被用"。

**B2：GUI Agent 热拔插架构**

这是你提到的"harness+model 可热拔插"。当前 `gui_executor.py` 的 `_predict()` 把模型调用和 grounding 逻辑耦合在一起。Best Paper 级别需要：

```python
# 当前（不好）
class GuiExecutor:
    def _predict(self, ...):
        # 直接调 gpt-5.6-sol

# 应该是（支持热拔插）
class GroundingBackend(ABC):
    def predict_action(self, screenshot, instruction, history) -> dict: ...

class GPT56SolBackend(GroundingBackend): ...       # 当前
class UITarsBackend(GroundingBackend): ...          # OSWorld mm_agents uitars_agent.py
class AguvisBackend(GroundingBackend): ...          # OSWorld mm_agents aguvis_agent.py
class GLM5VBackend(GroundingBackend): ...           # glm-5v-turbo（已验证畅通）

class GuiExecutor:
    def __init__(self, backend: GroundingBackend): ...
```

**为什么这是 Best Paper 必要条件**：
- 可以报"我们用 4 个不同 grounding model 都跑通了 VM round-trip"——这比"只用了一个模型"强一个数量级
- 可以做 Table 2 的"model 对比实验"：GPT-5.6-sol vs UITars vs GLM-5v-turbo 在 GUI write 的 SR 对比
- 审稿人不会说"这个结果依赖特定模型，换个模型可能不行"——因为你有消融数据

**B3：max_steps 自适应 + 效率指标**

当前 `DEFAULT_MAX_STEPS=18` 是固定值。P5 实测 9 个写操作耗 144 次调用（平均 16 次/操作）。Best Paper 需要报告这个指标并对比：
- 每次写操作的平均 steps 分布
- 复杂任务（4-App fanout）vs 简单任务（1-App）的 steps 差异
- 这本身就是一个关于"governance abstraction 降低执行复杂度"的 RQ3 证据

---

### 战场 C：验证体系——从"测信息抽取"升级到"测 VM 五性质"

**现状诊断（论文威胁级）**：

大纲 §5.2 自己承认：`run_w1_killtest.py` 测的是"模型能否从 HTML 文本抽取四元组，且套进 REST API 后状态符合预期"——这是信息抽取，不是 GUI Agent 能力。换 gui_agent 后必须重跑，且需要新增大纲 §6 里承诺的全套指标：

| 大纲 §6 指标 | 当前是否有落盘的 JSON | 是否用真实 GUI 路径测过 |
|---|---|---|
| Projection Coverage | ❌ 没有独立 killtest | ❌ |
| Binding Accuracy | ✅ `binding_f1` | ✅（但测的是 API 路径） |
| Round-Trip Fidelity | ✅ `score/round_trip` | ⚠️ gui_agent 路径下 p5 PASS |
| **Non-Interference** | ✅ `non_interference` | ⚠️ 同上，API 路径 |
| Reconciliation Accuracy | ❌ 只有 `amber` 字段，无专用 killtest | ❌ 从未被系统测过 |
| **Rollback Fidelity** | ✅ `rollback_verify.py` | ⚠️ builtin 是值反转，不是 GUI 重规划 |
| **Substrate-Invariance** | ❌ 没有专用指标和 killtest | ❌ |
| OOD Generalization | ⚠️ 只有 `f1_varid_semantic`，样本极少 | ⚠️ |
| Interaction Compression | ❌ 完全没实现 | ❌ |

**最大的空洞是 Reconciliation + Substrate-Invariance + Interaction Compression**——这三个指标在大纲 §6 承诺了，但现在完全没有对应的 killtest。它们分别对应 VM 性质 1（重投影）、性质 3（JVM moment）、以及 RQ3 的核心主张。

**要建的新 killtest**：

```
run_reconciliation_killtest.py
  - 场景：inject 外部并发修改（/api/inject_task 路由已有）
  - 验收：(a) amber 冲突标红出现；(b) 用户选择 accept_theirs/keep_mine/merge；
           (c) verifier 确认最终状态符合选择；(d) non-interference 仍成立

run_substrate_invariance_killtest.py（JVM moment 的量化证据）
  - 场景：同一任务（release_date 改日期）在 Stack A（calendar+taskboard）和 
           Stack B（outlook_cal+taskboard）各跑 3 samples
  - 验收：(a) binding F1 在两个 substrate 上均 ≥ 0.8；
           (b) round_trip 均 ≥ 0.85；
           (c) GenUI 界面 semantic similarity ≥ 0.85（通过 LLM-judge 比较两个 surface 的 slot 结构）；
           (d) 底层轨迹不同（`apps_written` 字段 outlook_cal ≠ calendar）

run_interaction_compression_killtest.py
  - 对比：(a) TaskVM 路径：用户 1 次编辑（改 release_date）→ N app 同步，用户 actions = 1；
           (b) 基线路径：用户逐个打开 calendar/taskboard/drive/mail，逐个改，user actions = N×k；
  - 指标：Interaction Compression = 基线 actions / TaskVM actions（§6 定义）
  - 注：基线可以是 frontier_shadow.py 里已有的 shadow agent，让它在 API 模式下逐个操作
```

---

### 战场 D：harness 性能——挖到上限

**现状诊断**：

你说的"harness 性能挖掘到上限"，我理解为两个层面：

**D1：GUI Agent 执行效率**

E13/E15 发现平均 16-20 次调用/写操作，目标 8-10。根本问题是：
- 重试从头重走整个表单流（View→Edit→改字段→Review→Confirm）
- `prev_screenshot` 方向虽然加了但效果有限（E13）

**真正的优化方向**（未被实施过的）：
```
1. 表单预缓存：第一次访问详情页时截图+解析 "可编辑字段→坐标" 的映射表，
   后续重试直接用缓存坐标，不需要重新 grounding
   
2. 结构化动作 DSL 扩展：
   当前 DSL 只有 click/type/press/scroll/done/fail
   增加 fill_form(field_name, value) → 让 browser_controller 根据 
   data-var 属性直接定位 input 并填值（不走视觉 grounding）
   用于那些 "编辑表单已经打开" 的情况——visual grounding 只用于 "导航到表单"
   
3. 并行写回：4-App fanout 任务可以并行启动 4 个 BrowserController 实例
   当前是串行 (for op in ops: mutate)，4 App 串行 ≈ 4 × 30s = 120s
   并行可降到 40-50s——这对 demo 体验非常重要
```

**D2：reconciliation 实时检测**

当前 reconciliation 是 "on-action 触发"（用户做操作时才重读），大纲 §0 性质 1 要求"随世界状态变化动态重投影"——这意味着**应该有后台轮询**。最小化实现：

```python
# server.py 增加一个 /poll_conflicts 路由（SSE 或轮询）
# 每 5 秒读一次 canonical_snapshot 与 last_projection diff
# 有冲突时推送到前端（不阻塞用户，不需要操作触发）
```

这看起来很小，但它是 §0 性质 1 "投影会随世界状态变化**动态**重投影" 的直接证明——现在没有这个，"动态"这个词在当前实现里是假的。

---

### 战场 E：自建 App 的真实 GUI 交互层

**现状诊断**：

从代码审计看，情况比交接 prompt 描述的**好很多**：

- Calendar：有 `calendar.html`（主列表）、`event_detail.html`、`event_edit.html`（3 个模板）✅
- TaskBoard：有 `taskboard.html`、`task_detail.html`、`task_edit.html` ✅
- Drive：有 `drive.html`、`file_detail.html`、`file_edit.html` ✅
- Mail：有 `mail.html`、`message_detail.html`、`message_edit.html` ✅
- OutlookCal：有 `outlook_cal.html`、`appointment_detail.html`、`appointment_edit.html` ✅

这说明 P1（真实可交互 UI）**实际上已经比大纲 §5.2 描述的更完整**！每个 app 都有编辑表单。

**但问题是**：GUI executor 成功率差异很大——以 `release_reschedule` 为例，p5 killtest 里平均 16 步成功，但有时会重试 3 次（40+ 步）。**问题不是 GUI 本身，是 grounding 精度**。

需要做的是**确认每个 app 的 edit form 对 grounding 模型友好**：
- 表单字段有明确的 `<label>` + `data-var` 属性
- 关键按钮（Save/Confirm）有视觉上清晰可辨识的样式
- 字段名称与 VM-state 的 var_id 语义一致（模型读到字段名后知道这就是要改的字段）

具体来说：**在每个 app 的 edit 表单上加一轮 "grounding 友好性验证"**，方法：用 grounding 模型对 edit 页截图做一次 "找 release_date 字段" 的探针，测试 click accuracy。这比全量重新设计表单代价低得多，也能直接落盘成 `eval_results/grounding_probe_*.json`。

---

### 战场 F：打通完整四步弧——让 VM Moment 真正涌现

**这是 Best Paper 的核心**，其他一切都是服务于这一刻。

四步弧需要以下全部成立（同时在一个 live demo 里）：

```
Step 1：改 release_date 8/14 → 8/18
        → calendar.E1 真实 GUI 手势移动（Playwright 浏览器截图可见）
        → taskboard.T1/T2 真实 GUI 表单更新（截图可见）
        → drive.F1 publish_date 真实 GUI 更新（如果有 4-App 任务）
        → GenUI 界面（--genui 模式）由模型重新解码，两区同步显示 8/18
        → verifier 落盘 JSON：round_trip=1.0, non_interference=1.0, neg=0.3

Step 2：外部注入并发修改（taskboard.T2 被队友改成 8/20）
        → only_read_zone 出现琥珀色冲突标记（reconciliation 实时检测触发）
        → 用户选择 accept_theirs → verifier 确认 T2 now 8/20（符合选择）

Step 3：用户点撤销
        → saga undo：calendar.E1 真实 GUI 手势撤回 8/14（截图可见）
        → taskboard.T1 真实 GUI 撤回（T2 因冲突已手动决策，状态为 partial_failure）
        → 诚实进度条：T1 可拖回 ✓，T2 🔒 不可逆（已被外部更新）

Step 4：同一操作在 Stack B（outlook_cal + taskboard）上重演
        → 完全相同的 release_date → 8/18
        → outlook_cal 的 appointment_edit form 被真实 GUI 手势操作
        → GenUI 界面与 Stack A 外观相同（JVM moment 视觉证据）
        → verifier 对两个 substrate 报同样的 round_trip=1.0
```

**目前四步弧中，step 1 是⚠️半真（GUI 路径存在但不是默认），step 2 是✅架构完整但未被 demo 过，step 3 是✅后端完整但 GUI 路径不是默认，step 4 是❌从未端到端跑过。**

---

## 优先级排序（按"缺少会直接塌"的程度排序）

| 优先级 | 战场 | 具体任务 | 不补会塌哪个 claim | 工作量估计 |
|---|---|---|---|---|
| **P0** | **B1** | 把 demo 执行器默认改成 gui_agent | §12.16 红线，审稿人问到即坍塌 | 极小（几行 + 文档） |
| **P0** | **A1** | 实现 `launch_full`（4-App 任务：calendar+taskboard+drive+mail 全同步） | 性质 2 bidirectional 的核心证据 | 中（新 fixture + 验证） |
| **P0** | **F** | 四步弧端到端 demo 脚本（`demo/run_demo.py`，固定种子，可复现） | §4.4 是 teaser figure 的唯一来源 | 中大 |
| **P1** | **C1** | `run_substrate_invariance_killtest.py`（Stack A vs B 并排对比） | 性质 3 substrate-independence，VM 之所以叫 VM | 中 |
| **P1** | **C2** | `run_reconciliation_killtest.py`（并发注入+合并策略测试） | 性质 1+5 reconciliation，SaC 逐字交出的 future work | 中 |
| **P1** | **B2** | GUI Agent GroundingBackend 热拔插架构 | Model ablation，防止"依赖特定模型"攻击 | 中 |
| **P1** | **D2** | `/poll_conflicts` 后台轮询（SSE） | 性质 1 "动态"重投影是否真实 | 小 |
| **P2** | **C3** | `run_interaction_compression_killtest.py` | RQ3 的量化证据 | 中 |
| **P2** | **B3** | `fill_form` DSL + 并行写回 | 执行效率，demo 体验 | 中 |
| **P2** | **A2** | 冲突场景任务 + governance 弧任务 | 完整 benchmark，§12.11 规模要求 | 大 |
| **P3** | **A3** | 40 模板 + OOD ~20%（大纲 §12.11 全量） | benchmark 规模，统计置信度 | 大 |
| **P3** | **①-1** | 编码器接入截图视觉（`complete_vision_json`） | §7.1 GUI 场景理解声称的完整性 | 中 |

---

## 为什么现在距离 Best Paper 还有距离（一句话总结每个战场）

1. **任务太浅**：3+3 个任务，最多 2 App，没有 4-App 任务，没有 reconciliation 场景任务——论文里写了 4-App、JVM moment、reconciliation，但验证数字指向的是 2-App 任务，这是 novelty claim 和实验证据之间最大的裂缝。

2. **执行器后门仍在默认路径**：demo 和大多数 killtest 走 `requests.post`，不走浏览器。这一件事本身就能让审稿人判 reject。

3. **四步弧从未端到端跑通过**：论文里的 §2 具体例子（发布日期改动，4 App 联动）是整个 contribution 的核心叙事，但没有任何 `eval_results/` 文件能证明它真的发生过。

4. **JVM moment（substrate-independence）缺乏量化**：这是 TaskVM 独占的 VM 性质，大纲 §4.2 说得很清楚"无一篇竞品做到"——但当前只有 1 个 OOD sample，不是专门的 substrate-invariance killtest。

5. **Reconciliation + Interaction Compression 两个指标完全没有 killtest**：大纲 §6 承诺了 9 个指标，其中至少 3 个（reconciliation accuracy、substrate-invariance、interaction compression）没有任何落盘 JSON 支撑。

6. **grounding 模型不可热拔插**：无法做 model ablation，无法防御"这个结果只在 gpt-5.6-sol 上成立"的攻击。

---

## 一句话给 coding agent 的最高优先级指令

> **第一件事**：把 `server.py::_get_fixture_and_adapters()` 里的 `make_adapters(host=host)` 改成 `make_adapters(host=host, executor=executor)`，在 `main()` 加 `--executor` 参数（默认 `gui_agent`），让 demo 默认走真实 GUI 手势。这一行代码改动，是所有其余工作的地基——没有它，你建了再多的东西都站在沙子上。
>
> **第二件事**：在 `fixtures.py` 里加 `launch_full` 任务（4 App：calendar+taskboard+drive+mail，1 var_id `release_date` → 4 个 binding，非干涉集覆盖全部无关对象），配套 `run_launch_full_killtest.py`，证明"1 次编辑 → 4 App 真实同步"。
>
> **第三件事**：写 `run_substrate_invariance_killtest.py`，在 `release_reschedule`（Stack A=calendar+taskboard）和 `outlook_release_reschedule`（Stack B=outlook_cal+taskboard）上各跑 3 samples，并排报 binding_f1 + round_trip + GenUI semantic similarity。这就是 JVM moment 的量化证据，论文 Table 1 的核心一行。

这三件事做完，VM Moment 就开始真正涌现了。其余的是增益，不是生死线。