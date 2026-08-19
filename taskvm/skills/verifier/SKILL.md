<!-- taskvm/skills/verifier/SKILL.md — format template (RM1C-SKILLS skeleton).
     The prompt-assembly injection point lives in the frozen layers and is
     wired at the R2.5 stage; this file defines ONLY the three-section
     format + the anti-cheat policy. Distilled content lands here per the
     SKILL-LADDER (bench_design §17.2), sourced from development-split
     successful trajectories ONLY. -->

# 界面验证员 Skill

---
role: verifier
version: 0.1.0
status: skeleton — format template only; no distilled content yet
distill_policy: development split 成功轨迹 only；held-out 变体永不参与蒸馏
---

## 触发条件

<!-- 何时注入本 skill（由 prompt 装载点评估；条件必须基于任务/输入的可见特征，
     禁止基于任何评测协议的内部状态）。 -->

- （示例格式）当输入包含「验证意图 + 写入后观察」且需要给出三态判定（changed / not_yet / cannot_verify）时注入。

## 通用领域与操作先验

<!-- 通用世界知识与操作先验。允许：判定证据的通用惯例、常见界面的状态呈现
     常识。禁止：任何 frozen task 的种子数据、成功判定谓词、防篡改保护名单、
     检查点证据字段——完整禁词表以 tests/skills/ 的反泄露测试为准
     （模板自身不复述禁词，避免自指误报）。 -->

- （示例格式）判定 changed 必须引用屏幕上当前可见的证据（如刚发送的消息气泡、已点亮的点赞图标）；截图与可见文本不一致时倾向 cannot_verify，不要编造证据。
- （示例格式）「已发送」的通用可见标志：消息气泡出现在会话尾部且输入框已清空——不是猜测网络层是否成功。

## 蒸馏少样本

<!-- 从 development split 成功轨迹蒸馏的少样本示例。每条注明来源档位
     （L0/L1/L2）。本节在骨架阶段为空占位。 -->

- （占位）待蒸馏 — R2.5 SKILL-LADDER 各档填充（L0 起步）。
