<!-- taskvm/genui/skills/SKILL.md — the GenUI decoder's distillation slot
     (A4 step4, workplan §20.3). Format + anti-cheat boundary aligned with
     bench_design §17.2 / taskvm/skills/: three frozen sections, general
     priors only, zero frozen-task ground truth. The FIRST distilled
     priors below come from the 2026-08-20 A4 real run (APP-line
     development trajectories: three unseen goals decoded source=model
     on the first call each). Loader wiring for the genui_decoder role
     is a later ladder step (taskvm/skills/loader.py's role list is the
     RM line's territory); this file is the SLOT plus its starter
     content. -->

# GenUI 解码器 Skill（A2UI 组件树生成）

---
role: genui_decoder
version: 0.1.0
status: starter — first distilled priors from the 2026-08-20 A4 real run
distill_policy: APP 线 development split 成功轨迹 only；held-out 变体永不参与蒸馏
---

## 触发条件

- 当输入是「TaskSurfaceContext（任务目标 + 变量面板 + 工作流概要）」且需要产出 `updateComponents.components` 组件树时注入本 skill。

## 通用领域与操作先验

以下每条都对应一次真实验证过的生成行为（2026-08-20 A4 真模型全绿轨迹 + 首轮失败教训）：

- **结构适配任务形态**——同一套 18 个 Basic 组件，不同任务长出不同树：
  - 「时间/数字可编辑」型任务（闹钟类）→ 分组 Row + DateTimeInput/TextField 输入，每组配一个确认 Button；
  - 「只读摘要 + 少量开关」型任务（天气提醒类）→ Text 展示 observed 平面 + CheckBox 单开关，无需输入组；
  - 「纯文本输入 + 提交」型任务（发消息类）→ 扁平 Column + longText TextField + 提交 Button。
- **输出契约铁律**：只输出裸 JSON 数组（或 `{"components": [...]}` 包装），零 prose、零 markdown fence——任何包装或解释都会浪费一轮有界修复。
- **绑定纪律**：动态值一律 `{"path": ...}`；可编辑变量的输入控件走写通道 `/variables/<key>/desired`，只读展示走展示通道（observed/label）——字面值写死在树里等于把世界状态固化进结构，值一变就要重新生成。
- **动作词汇**：Button 的 action 用 `taskvm.local_patch` + `{"semanticKey": <key>}`——TaskVM 面上唯一合法的本地写动作。
- **单引用形态合法**：Button/Card 的 `child`、Modal 的 `trigger`/`content`、Tabs 的 `tabs[].child` 都是不挂 children 数组的官方挂法（可达性校验认这种边）；Button 的标签 Text 只挂 `child` 即可，无需双重挂载。
- **组件数克制**：18 个 Basic 组件足够表达大多数任务面；超过约 30 个组件的树通常是「把每个标签和值都铺成独立节点」的浪费形态——首轮真实失败即此形态，收敛为「可编辑变量才有专属输入组，只读变量合并展示」。

## 蒸馏少样本

- （L0 起步，2026-08-20 A4 real run）三条全绿轨迹的形态摘要——每 goal 恰好 1 次模型调用即通过两层校验：闹钟 35 组件分组表单树 / 天气 18 组件摘要卡片树 / 群消息 13 组件扁平表单树。逐字档案（prompt/reply/ledger）在运行机的 `eval_results/a4_decoder_20260820/`（按仓库契约不进 git）。
- （占位）待蒸馏 — SKILL-LADDER 后续档位从 APP 线 development split 的成功轨迹继续填充。
