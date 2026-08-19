<!-- taskvm/skills/compiler/SKILL.md — format template (RM1C-SKILLS skeleton).
     The prompt-assembly injection point lives in the frozen layers and is
     wired at the R2.5 stage; this file defines ONLY the three-section
     format + the anti-cheat policy. Distilled content lands here per the
     SKILL-LADDER (bench_design §17.2), sourced from development-split
     successful trajectories ONLY. -->

# 状态编译器 Skill

---
role: compiler
version: 0.1.0
status: skeleton — format template only; no distilled content yet
distill_policy: development split 成功轨迹 only；held-out 变体永不参与蒸馏
---

## 触发条件

<!-- 何时注入本 skill（由 prompt 装载点评估；条件必须基于任务/输入的可见特征，
     禁止基于任何评测协议的内部状态）。 -->

- （示例格式）当输入包含「观察到的可见表面」且需要从中提取任务变量与绑定证据时注入。

## 通用领域与操作先验

<!-- 通用世界知识与操作先验。允许：真实 app 的 UI 结构常识、跨任务通用的
     观察解读惯例。禁止：任何 frozen task 的种子数据、成功判定谓词、防篡改
     保护名单、检查点证据字段——完整禁词表以 tests/skills/ 的反泄露测试
     为准（模板自身不复述禁词，避免自指误报）。 -->

- （示例格式）微信的聊天列表按会话组织，每条会话显示联系人名称与最后一条消息预览；「变量」应绑定到这类屏幕可见的实体标签。
- （示例格式）支付宝的账单记录中金额带正负号，负数表示支出、正数表示收入——解读金额变量时按此惯例。

## 蒸馏少样本

<!-- 从 development split 成功轨迹蒸馏的少样本示例。每条注明来源档位
     （L0/L1/L2）。本节在骨架阶段为空占位。 -->

- （占位）待蒸馏 — R2.5 SKILL-LADDER 各档填充（L0 起步）。
