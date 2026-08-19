<!-- taskvm/skills/cua/SKILL.md — format template (RM1C-SKILLS skeleton).
     The prompt-assembly injection point lives in the frozen layers and is
     wired at the R2.5 stage; this file defines ONLY the three-section
     format + the anti-cheat policy. Distilled content lands here per the
     SKILL-LADDER (bench_design §17.2), sourced from development-split
     successful trajectories ONLY. -->

# GUI 操作代理 Skill

---
role: cua
version: 0.1.0
status: skeleton — format template only; no distilled content yet
distill_policy: development split 成功轨迹 only；held-out 变体永不参与蒸馏
---

## 触发条件

<!-- 何时注入本 skill（由 prompt 装载点评估；条件必须基于任务/输入的可见特征，
     禁止基于任何评测协议的内部状态）。 -->

- （示例格式）当操作目标指向某类应用（如支付、社交、内容社区）且需要在其中定位入口时注入对应先验。

## 通用领域与操作先验

<!-- 通用世界知识与操作先验。允许：真实 app 的 UI 结构常识、跨任务通用的
     操作惯例（bench_design §17.2 原例：「支付宝账单入口在底部 Tab『我的』」）。
     禁止：任何 frozen task 的种子数据、成功判定谓词、防篡改保护名单、
     检查点证据字段——完整禁词表以 tests/skills/ 的反泄露测试为准
     （模板自身不复述禁词，避免自指误报）。 -->

- （示例格式）支付宝的账单入口在底部 Tab「我的」。
- （示例格式）聊天类应用发送消息的通用序列：点开目标会话 → 点底部输入框唤起键盘 → 输入文本 → 点发送键；长列表找目标时优先用搜索框而不是逐屏滚动。

## 蒸馏少样本

<!-- 从 development split 成功轨迹蒸馏的少样本示例。每条注明来源档位
     （L0/L1/L2）。本节在骨架阶段为空占位。 -->

- （占位）待蒸馏 — R2.5 SKILL-LADDER 各档填充（L0 起步）。
