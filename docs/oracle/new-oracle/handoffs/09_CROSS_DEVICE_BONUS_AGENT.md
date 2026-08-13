# Optional Coding Agent H：跨设备统一投影 Bonus（OSWorld + MobileGym）

## 启动条件

只有在单 substrate 最终 E2E、GoalPatch、rollback、frontend 和 final benchmark smoke 全部通过后才开始。此任务不能阻塞主论文 prototype，也不能通过引入跨设备特例破坏 substrate transparency。

---

## 目标

用同一个 TaskVM projection 和 workflow，同时调度两个真实独立 substrate session：

- Desktop：OSWorld；
- Mobile：MobileGym。

用户只看到一个任务状态、一个 milestone/checkpoint workflow 和多个 surface lane；无需理解哪一步属于哪台设备。

---

## Owned paths

```text
taskvm/substrate/composite/**  # 若确有必要；优先复用 registry/session group
tests/cross_device/**
taskvm/benchmark/tasks/cross_device/**
docs/CROSS_DEVICE_DEMO.md
```

不要修改各具体 substrate 内部实现；通过统一 port 组合。

---

## 最小 Demo

只选一个足够有说服力、可稳定复现的任务，例如：

> 在桌面端读取会议时间与材料状态，在移动端向指定联系人发送整理后的通知，并在统一 TaskVM 中验证桌面与手机两条 lane 达到同一个 checkpoint。

或：

> 在桌面端识别今天最高的三笔支出，在手机端将摘要发给指定联系人。

只需要 1–2 个高质量任务，不扩展成多设备平台。

---

## 架构要求

- Task Architect 输出一个 fan-out：desktop lane + mobile lane；
- 每条 lane 有独立 substrate session、epoch child token、screenshots 和 verification；
- TaskVM Kernel 保存统一 task variables 和 checkpoint；
- Projection UI 默认显示高层 lane，点击分别查看 desktop/mobile 最新截图；
- Barrier 只有两边 required result verified 后通过；
- 一端失败时保留另一端已验证结果并允许局部恢复；
- GoalPatch 可以只 invalidate 一台设备上的未来 lane；
- 不新增 `if mobile` 到 runtime/projection/kernel。

---

## 研究价值要验证，而不是只做炫技

至少比较：

- 跨设备 TaskVM vs 用户手工切换设备；
- 跨设备 TaskVM vs 两个互不共享状态的独立 CUA；
- task success；
- 跨设备切换/重复输入；
- 总模型调用与延迟；
- 统一 checkpoint 的恢复能力；
- 用户是否理解两个设备的协同进度。

---

## 边界

不做：

- 动态发现任意数量设备；
- 分布式数据库/CRDT；
- 多用户协作；
- 跨设备锁服务；
- 二十种 substrate；
- 为 demo 使用 hidden state 传递结果。

跨设备的信息传递必须通过 TaskVM 已观察并抽象出的任务状态，不得直接从 OSWorld oracle 填入 MobileGym action。

---

## 验收

1. 同一个 session 页面显示 desktop/mobile 两条 lane。
2. 两边 CUA 可独立执行并上报 screenshot。
3. 一个 TaskVariable 能关联跨设备 evidence。
4. fan-in checkpoint 真实等待两边验证。
5. 一边 GoalPatch 或失败不会重启另一边。
6. Runtime/projection/kernel 不 import `mobilegym` 或 `osworld` 具体模块。
7. 完整录屏和 event trace 不含 hidden oracle 信息。
