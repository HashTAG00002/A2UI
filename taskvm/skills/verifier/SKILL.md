---
role: verifier
version: 1.0.0
status: distilled-v1 (L0 baseline lessons + general evidence priors)
distill_policy: development split 成功轨迹 only；held-out 变体永不参与蒸馏
---

<!-- taskvm/skills/verifier/SKILL.md — 界面验证员 skill v1（R2.5 SKILL-LADDER L0 蒸馏）。
     来源：development split 的 L0 基线轨迹分析（2026-08-20，gpt-5.6-sol，六个 demo
     goal）。L0 基线无端到端成功轨迹，v1 = 通用判定证据惯例 + 基线失败教训的
     匿名化提炼；held-out 变体永不参与蒸馏。禁词表以
     tests/skills/test_skills_antileak.py 为准。 -->

# 界面验证员 Skill

## 触发条件

- 当输入包含「验证意图 + 写入后观察」且需要给出三态判定（changed / not_yet / cannot_verify）时注入。

## 通用领域与操作先验

- **导航完成的判据**：判定「进入某应用」类目标时，屏幕出现该应用的标志性界面（顶部应用名、主导航 Tab、应用特有列表）即为已变化——即使被验证项的字面值从未在屏幕上逐字出现，也不据此判 not_yet。
- **消息已发送的判据**：消息气泡出现在会话消息流尾部、且输入框已清空；两者同时可见才算已变化。
- **输入是否生效的判据**：判定输入类动作时，屏幕可见文本出现所输入的内容才算生效；屏幕无变化判 not_yet 并指出焦点可能不在输入框，而不是猜测输入已提交。
- **证据优先于推断**：判定已变化必须引用屏幕当前可见的证据；观察与验证条件无法对应时倾向 cannot_verify，绝不编造证据或替界面找理由。

## 蒸馏少样本

- （L0 基线教训，匿名化）某验证面对「执行代理已进入聊天应用、屏幕可见聊天列表」仍对导航类写入判未完成，执行代理两次如实报完成被打回后升级——教训：导航完成以应用标志性界面可见为准，不苛求变量字面值上屏。
