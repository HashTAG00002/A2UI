<!-- taskvm/skills/architect/SKILL.md — format template (RM1C-SKILLS skeleton).
     The prompt-assembly injection point lives in the frozen layers and is
     wired at the R2.5 stage; this file defines ONLY the three-section
     format + the anti-cheat policy. Distilled content lands here per the
     SKILL-LADDER (bench_design §17.2), sourced from development-split
     successful trajectories ONLY. -->

# 任务架构师 Skill

---
role: architect
version: 0.1.0
status: skeleton — format template only; no distilled content yet
distill_policy: development split 成功轨迹 only；held-out 变体永不参与蒸馏
---

## 触发条件

<!-- 何时注入本 skill（由 prompt 装载点评估；条件必须基于任务/输入的可见特征，
     禁止基于任何评测协议的内部状态）。 -->

- （示例格式）当输入包含「任务目标 + 已观察状态」且需要产出完整任务架构（变量/工作流/检查点）时注入。

## 通用领域与操作先验

<!-- 通用世界知识与操作先验。允许：任务图的通用建模惯例、动作语义先验。
     禁止：任何 frozen task 的种子数据、成功判定谓词、防篡改保护名单、
     检查点证据字段——完整禁词表以 tests/skills/ 的反泄露测试为准
     （模板自身不复述禁词，避免自指误报）。 -->

- （示例格式）导航型动作（如「打开某应用」）只改变位置、不写入任务变量——建模为无状态写入的步骤即可，不必强行绑定变量。
- （示例格式）查询型动作（如「查看账单」）的结果在运行时才可知——把「查询结果」建为变量、由后续动作消费，而不是在架构期臆造其值。

## 蒸馏少样本

<!-- 从 development split 成功轨迹蒸馏的少样本示例。每条注明来源档位
     （L0/L1/L2）。本节在骨架阶段为空占位。 -->

- （占位）待蒸馏 — R2.5 SKILL-LADDER 各档填充（L0 起步）。
