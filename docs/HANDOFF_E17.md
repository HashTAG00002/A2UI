# TaskVM E17 交接文档：UserBehaviorDriver + MobileGym VM五性质 + 全局模块化重构

> **阅读顺序（必读）**：先读 `.mrules` E1-E16 全文，再读本文档。本文档建立在 E16 完整修复的基础之上，解决的是三个被 E16 工作暴露出来的更深层问题。

---

## 0. 这次要解决的三个根本问题（不可跳过）

### 问题 A：流水线被拦腰截断——缺少"从人的操作到 CUA instruction"的完整链路

**当前状态**（E16 之后）：

```
killtest 脚本
    ↓ 直接调用 bridge.mutate(post_id, operator, value=True)
    ↓ bridge 生成硬编码 f-string instruction
    ↓ gui_act_async(instruction)
    ↓ CUA 执行截图+手势
```

**缺失的是什么**：链路从"操作意图层"（`value=True`）直接开始，完全跳过了：
1. **GenUI 解码出的界面** — 真实用户看到的不是 `operator=toggle_like, value=True`，而是一个由 GenUI 模型渲染的界面，界面上有一个心形图标控件
2. **用户在 GenUI 界面上的交互行为** — 用户点了这个心形图标，这个"点击"动作被 governance 层捕获，转化成一个语义化的 patch（`{var_id: "liked_status", old: False, new: True}`）
3. **从 patch 到 CUA subgoal 的解码** — governance 层把这个 patch 翻译成一个 CUA 能听懂的自然语言 subgoal（"在 X app 时间线上，找到帖子《...》，点击它的心形图标使其变为点亮状态"）

**当前这种拦腰截断的后果**：
- 无法无缝切换到"真实人类来做这个操作"——真实人类面对的是 GenUI 界面，不是结构化的 `{operator, value}` 字典
- CUA 的 instruction 质量完全取决于 bridge 里硬编码的 f-string 质量，与 GenUI 的语义完全脱离
- 测出来的"CUA 成功率"测的是"给定一个精心设计的 f-string，CUA 能不能执行"，而不是"给定 GenUI 界面上的一次用户操作意图，整个链路能不能端到端工作"

### 问题 B：X toggle 任务在"无后门"条件下是 ill-posed（任务本身不可行）

**根本矛盾**（已在上轮诊断中确认）：

verifier 用 `post_id`（如 `p_1879539450872778943`）来判断"对不对"，要求的是"这条特定帖子被 toggle"。但 instruction 是纯视觉级的"找一条未 toggle 的帖子点它"。在 fresh reset 环境里三条帖子全是 outline 状态，**模型无法从视觉上区分"哪条是预期目标"**。

这不是 38.9% SR 是模型能力不行——是**任务设计在语义层就矛盾了**：
- 如果 verifier 要求"特定 post 被 toggle"，那 instruction 就必须告诉模型"目标是这条帖子"（需要识别线索）
- 如果 instruction 只说"找任意一条未 toggle 的帖子点它"，那 verifier 应该判断"有任意一条 post 新出现在 liked/retweeted/bookmarked 列表里"

**好的任务设计标准**（用户原话）：让人去做也能一下做对的任务。如果连人都不知道该点哪条帖子，那这个任务就是坏任务。

### 问题 C：MobileGym 上完全没有体现 VM 五性质

当前 MobileGym 上唯一的任务（`top3_expense_to_wechat`）是单变量单写操作，没有体现：
- **bidirectional binding**：一个 VM 变量绑多个 app 的对象
- **cross-app fanout**：一次变更引发多个 app 同步变更
- **governance + checkpoint**：用户设定"做到哪里"的目标深度
- **reversibility spectrum**：有些操作可逆、有些诚实不可逆，两者都要被测到
- **substrate-independence**：同一 VM 操作在自建平台和 MobileGym 上语义一致

**这不是"MobileGym 功能不全"，而是"任务没有设计成能体现这些性质"**。

---

## 1. 核心架构设计：UserBehaviorDriver（网络协议栈类比）

### 1.1 设计哲学：把系统想象成一个协议栈

用户的类比非常精准——这个系统**就像一个网络协议栈**：

```
┌─────────────────────────────────────────────────────────────┐
│         L4: 用户行为层 (UserBehaviorDriver)                   │
│  程序化脚本 <── 可无缝切换 ──> 真实人类 (HTTP 接口)             │
├─────────────────────────────────────────────────────────────┤
│         L3: 治理解释层 (GovernanceInterpreter)                │
│  checkpoint graph + subgoal 生成（静态预生成 or 动态推断）       │
├─────────────────────────────────────────────────────────────┤
│         L2: GenUI 界面层 (workspace_ui)                      │
│  只读区投影 + 可读可写区 + A2UI 解码器 + 进度/回退控件            │
├─────────────────────────────────────────────────────────────┤
│         L1: VM 状态层 (task_state + verifier)                │
│  binding compiler + round-trip verifier + reconciliation     │
├─────────────────────────────────────────────────────────────┤
│         L0: Substrate 层 (harness)                           │
│  自建平台 <── 可插拔替换 ──> MobileGym <── ──> OSWorld         │
└─────────────────────────────────────────────────────────────┘
```

**关键设计约束**：
- **L4（用户行为）只改一处就能适配所有 substrate**：`UserBehaviorDriver` 是一个抽象接口，程序化脚本和真实人类都是它的实现
- **L0（substrate）只改一处就能适配所有上层**：`StateAdapter` 抽象接口，自建平台/MobileGym/OSWorld 都是实现
- **L3 和 L4 之间的连接是这次最重要的新增**：`GovernanceInterpreter` 负责把"用户在 L4 做了什么"翻译成"L2 的 CUA 应该执行什么 subgoal"

### 1.2 UserBehaviorDriver 接口定义

```python
# taskvm/governance/user_behavior_driver.py  (新文件)

class UserBehaviorEvent:
    """一个用户行为事件——用户对 GenUI 界面做了什么。
    
    注意：这里建模的是"用户行为"，不是"CUA instruction"。
    行为是高层语义的（"把进度条从 checkpoint_A 拖到 checkpoint_B"），
    instruction 是由 GovernanceInterpreter 从行为推断出来的。
    """
    event_type: str   # "set_milestone" | "drag_checkpoint" | "edit_field" | "undo" | "rollback_to"
    payload: dict     # 事件相关的结构化数据

class UserBehaviorDriver(ABC):
    """用户行为的抽象来源——程序化脚本和真实人类都实现这个接口。
    
    接口上接的是：
      - ScriptedUserDriver（程序化，用于 killtest/evaluation）
      - HumanWebSocketDriver（真实人类，通过 WebSocket 连接 workspace_ui）
    
    这两种实现可以无缝替换——killtest 用程序化，demo 用真人。
    """
    @abstractmethod
    def next_event(self) -> UserBehaviorEvent | None:
        """返回下一个用户行为事件，None 表示任务结束。"""
        ...
    
    @abstractmethod
    def on_state_update(self, vm_state: VMStateSnapshot):
        """收到 VM 状态更新时的回调（用于真实人类的界面刷新）。"""
        ...
```

### 1.3 GovernanceInterpreter：从用户行为到 CUA subgoal

**用户行为事件 → CUA subgoal instruction 的转换**有两种策略：

**策略 A（静态预生成，"空间换时间"）**：
在生成 GenUI 界面时，同时预生成一个"checkpoint transition graph"——每一对 `(checkpoint_from, checkpoint_to)` 对应一个预先生成好的 subgoal instruction。
- 优点：运行时零延迟，不需要额外模型调用
- 缺点：图的大小 = 节点数²，复杂任务的空间开销大；不够灵活

**策略 B（动态推断，"时间换空间"）**：
在运行时，当用户触发一个行为（如"把进度条从 checkpoint_C 拖回 checkpoint_B"），实时调用一个轻量 LLM，分析"从 C 状态回退到 B 状态，意味着哪些 VM 变量需要从当前值变回什么值，对应 CUA 应该执行什么操作序列"。
- 优点：完全灵活，不需要预枚举所有可能的转换
- 缺点：增加一次模型调用延迟（但用户说"开销相对小，因为只是生成意图，不需要长理解"）

**推荐方案**：**策略 B**（动态推断），理由：
1. MobileGym 上的 checkpoint 空间是动态的（内容取决于任务），无法静态枚举
2. 用户明确说"这个模型调用开销相对比较小"——可以接受
3. 静态预生成在"进度条往回拖"的 free-form 场景下无法做到完备

```python
# taskvm/governance/governance_interpreter.py  (新文件)

class GovernanceInterpreter:
    """把 UserBehaviorEvent（用户行为）翻译成 CUA subgoal（具体执行目标）。
    
    这是 L3 层的核心逻辑。它接收：
      - 当前 VM 状态（binding + 各变量当前值 + checkpoint 历史）
      - 用户行为事件（edit_field / drag_checkpoint / rollback_to / ...）
    
    输出：
      - 一个或多个 SubgoalInstruction（自然语言，CUA 可直接执行）
      - 更新后的 VM 状态
    """
    
    def interpret(self, event: UserBehaviorEvent, 
                  vm_state: VMStateSnapshot) -> list[SubgoalInstruction]:
        """核心方法：行为事件 → subgoal 列表。
        
        对于 "edit_field" 事件（用户直接编辑了一个 GenUI 字段）：
          - 直接翻译为 patch → compile_patch → 逐 op 生成 subgoal
        
        对于 "drag_checkpoint" 或 "rollback_to" 事件（进度条回退）：
          - 调用 _infer_rollback_subgoal()：分析"从当前状态回退到目标 checkpoint 
            需要撤销哪些已执行的操作"，生成 undo subgoal 序列
        
        对于 "set_milestone" 事件（用户设定新目标深度）：
          - 调用 _infer_advance_subgoal()：分析"从当前状态前进到目标 milestone
            需要执行哪些操作"，生成 execute subgoal 序列
        """
```

### 1.4 完整链路（新版，对比旧版）

**旧版（被拦腰截断的）**：
```
killtest_script
  → bridge.mutate(post_id, operator, value=True)   # 直接从意图层开始
  → f-string instruction                            # 硬编码
  → gui_act_async(instruction)                      # CUA 执行
```

**新版（完整的）**：
```
UserBehaviorDriver.next_event()          # L4: 用户行为（程序化 or 真人）
  ↓ e.g. UserBehaviorEvent(
         type="edit_field",
         payload={var_id: "liked_status", 
                  genui_component_id: "heart_icon_p0",  # 用户点了哪个控件
                  old: False, new: True})
  
GovernanceInterpreter.interpret(event, vm_state)   # L3: 行为→subgoal
  ↓ SubgoalInstruction(
       natural_language="在 X app 时间线顶部帖子（标题含'金融'）的操作栏中，
                         找到心形图标，点击使其变为粉色填充状态",
       patch_ops=[PatchOp(app="x", entity_id="p_xxx", operator="toggle_like", value=True)],
       verification_criterion="post p_xxx 出现在 likedPostIds 列表中")

CUAExecutor.execute(subgoal)             # L2/L1: CUA 执行+验证
  → gui_act_async(subgoal.natural_language)
  → verifier.check(subgoal.verification_criterion)
```

---

## 2. 新的 MobileGym 任务设计（满足 VM 五性质）

### 2.1 什么是好的任务设计

**用户给出的判断标准**：
> "让人去做的话，人一下就能做对的那种任务"

这意味着：任务的**可操作目标**必须对用户可见，不依赖任何他们从界面上看不到的内部标识符（如 `post_id`）。

**VM 五性质的任务设计要求**：
| 性质 | 任务设计要求 |
|---|---|
| bottom-up live projection | 任务状态必须投影自多个 app 的真实读取（不是静态预设的 seed） |
| bidirectional binding | 一个用户操作必须触发多个 app 的同步变更 |
| substrate-independence | 同一任务语义必须在自建平台和 MobileGym 上各自成立 |
| governance over autonomy | 任务必须有 checkpoint——用户能设定"做到哪里"，不是一次性执行完 |
| round-trip + reversibility | 必须同时有"可逆操作"和"诚实不可逆操作"的测试用例 |

### 2.2 MobileGym 上的新任务设计

#### 任务 MG-1：`social_morning_brief`（跨 app 阅读链路）

**任务目标**：用户早上来了，打开 X app 看到一条关于"美联储加息"的帖子，觉得有价值，要：
1. 点击 like（收藏表态）
2. 用支付宝查看自己的投资组合价值（读操作，验证投影）
3. 发微信给联系人"黄勇"，转发帖子内容并附上自己的投资组合价值

**为什么这是好的任务**：
- **人能做对**：目标帖子在界面上有唯一可见的文字（"美联储加息"），人一眼能认出
- **跨 app binding**：一个 VM 变量（"分享内容"）绑定了 X 的帖子文字（读）和微信的发送（写）
- **bidirectional**：读 alipay 的投资价值 → 写进微信消息（一个语义 patch 触发多 app）
- **reversibility spectrum**：like 可逆（再点取消）；微信消息诚实不可逆

**CanonicalTaskGraph 设计**：
```python
SOCIAL_MORNING_BRIEF = CanonicalTaskGraph(
    task_id="social_morning_brief",
    goal="看到 X 上关于美联储加息的帖子，like 它，同时查支付宝投资组合，把帖子和投资金额发给微信黄勇。",
    seed_state={
        "x": {"posts": [FEDRATE_POST, ...],   # 帖子里包含唯一标识文字"美联储加息"
               "likedPostIds": []},
        "alipay": {"portfolio_value": 52860},
        "wechat": {"add_chats": [HUANGYONG_CHAT], "add_contacts": [HUANGYONG_CONTACT]},
    },
    user_edit={"var_id": "morning_brief_sent",
               "old": {"liked": False, "message_sent": False},
               "new": {"liked": True, "message_sent": True,
                       "message_content": "美联储加息了，我的投资组合现在是 52860 元，关注一下风险。"}},
    bindings=[
        CanonicalBinding("morning_brief_liked",  "x",      FEDRATE_POST_ID, "liked",    "toggle_like",    True),
        CanonicalBinding("morning_brief_message", "wechat", HUANGYONG_WXID,  "messages", "send_message",
                         "美联储加息了，我的投资组合现在是 52860 元，关注一下风险。"),
    ],
    checkpoints=[
        Checkpoint("C1", description="已 like 帖子",    
                   criterion={"x": {FEDRATE_POST_ID: {"liked": True}}}),
        Checkpoint("C2", description="已发微信消息",    
                   criterion={"wechat": {HUANGYONG_WXID: {"messages_contain": "52860"}}}),
    ],
    ...
)
```

#### 任务 MG-2：`expense_and_notify`（现有任务的 VM 扩展版）

当前的 `top3_expense_to_wechat` 只有一个写操作。扩展为：
1. 用支付宝看 top-3 支出（读）
2. 给微信黄勇发消息（写，可能不可逆）
3. **checkpoint C1**：消息已发但用户觉得不满意，撤回（honesty: 诚实不可逆）
4. **checkpoint C2**：重新编辑消息重发（新的写路径）

**为什么 VM 性质体现更充分**：
- governance：用户设了两个 checkpoint（C1/C2），中间有一次回退尝试 + 诚实失败
- reversibility spectrum：C1→C2 的"回退"证明了诚实不可逆（409）
- bidirectional binding：alipay 读出来的数字绑定到 wechat 消息里

#### 任务 MG-3：`social_profile_update`（真正的 substrate-independence 证明）

同一个 VM 任务（更新社交状态）在两个 substrate 上：
- **自建平台（mail + taskboard）**：发邮件通知 + 更新任务板状态
- **MobileGym（wechat + x）**：发微信通知 + 更新 X profile bio

**CanonicalTaskGraph 完全相同**，只有底层的 `StateAdapter` 不同——这就是 "JVM moment"。

### 2.3 任务设计的通用原则（供 coding agent 直接使用）

1. **可视唯一性**：任务目标对象必须在 GenUI 界面上有唯一可视的识别特征（文字/颜色/位置），不能依赖内部 ID
2. **跨 app 绑定**：每个 MobileGym 任务至少涉及 2 个 app（1 读 1 写，或多写）
3. **checkpoint 粒度**：每个任务至少设 2 个 checkpoint，以便测试"用户在中途回退/推进"的 governance 行为
4. **reversibility 混合**：每个任务至少包含一个"可逆"和一个"诚实不可逆"的写操作
5. **verifier 与 instruction 对齐**：verifier 的判据必须和 instruction 的任务描述在语义上等价——如果任务说"找帖子 X 点 like"，verifier 判的就是"帖子 X 在 likedPostIds 里"，而不是"任意帖子在 likedPostIds 里"

---

## 3. 代码模块化重构方案（新目录结构）

### 3.1 现有结构的问题

当前 `taskvm/` 目录把"应该是不同层级"的东西混在同一个包里：
- `harness/` 里混了 substrate-specific 代码（`mobilegym_bridge.py`）和通用接口（`state_adapter.py`）
- `evaluation/` 里 30+ 个 kill-test 脚本和真正的评估逻辑混在一起
- 完全没有"用户行为层"（L4）的对应代码
- 完全没有"治理解释层"（L3）的对应代码

### 3.2 新目录结构

```
taskvm/
├── apps/                          # L0 substrate: 自建平台（不变）
│   ├── calendar/
│   ├── taskboard/
│   ├── drive/
│   ├── mail/
│   └── outlook_cal/
│
├── substrate/                     # L0 substrate: 适配器层（从 harness/ 拆分）
│   ├── base.py                    # StateAdapter 抽象基类（原 harness/state_adapter.py）
│   ├── builtin/                   # 自建平台适配器
│   │   └── adapters.py            # CalendarAdapter, TaskBoardAdapter, etc.
│   ├── mobilegym/                 # MobileGym 适配器（原 harness/mobilegym_bridge.py）
│   │   ├── bridge.py
│   │   └── fixtures.py            # MobileGym GT（原 benchmark/mobilegym_fixtures.py）
│   └── osworld/                   # OSWorld 适配器（未来扩展，现在留空）
│       └── README_placeholder.py
│
├── vm_state/                      # L1 VM状态层（原 task_state/ + verifier/）
│   ├── compiler.py                # binding compiler（原 task_state/compiler.py）
│   ├── entity_binding.py          # TaskBinding 数据结构
│   ├── dependency_graph.py
│   ├── projection_policy.py
│   ├── representation.py
│   └── verifier/                  # 独立 verifier（原 verifier/）
│       ├── canonical_state.py
│       ├── round_trip_checks.py
│       ├── cross_app_checks.py
│       ├── non_interference.py
│       ├── reconciliation.py
│       └── rollback_verify.py
│
├── governance/                    # L3 治理解释层（全新）
│   ├── user_behavior_driver.py    # UserBehaviorDriver 抽象接口
│   ├── scripted_driver.py         # ScriptedUserDriver（程序化，用于 evaluation）
│   ├── human_driver.py            # HumanWebSocketDriver（真实人类）
│   ├── governance_interpreter.py  # 行为事件 → CUA subgoal
│   ├── checkpoint_graph.py        # checkpoint 图建模 + milestone 转换关系
│   └── subgoal.py                 # SubgoalInstruction 数据结构
│
├── interface/                     # L2 GenUI界面层（原 workspace_ui/）
│   ├── server.py                  # Flask 服务器（不变）
│   ├── genui_decoder.py           # GenUI 解码器（不变）
│   ├── renderer.py                # 薄渲染层（不变）
│   ├── editable_components.py     # 治理控件（不变）
│   └── live_sync.py               # 实时同步（不变）
│
├── execution/                     # CUA 执行层（不变，内部已重构）
│   ├── gui_executor.py            # 同步 GUI executor（桌面 app）
│   ├── gui_executor_async.py      # 异步 GUI executor（MobileGym）
│   ├── action_dispatcher.py       # 调度层
│   ├── patch_compiler.py
│   ├── rollback.py
│   └── replanner.py
│
├── benchmark/                     # 评估配置（不变）
│   ├── fixtures.py                # 自建平台 GT
│   ├── ood_fixtures.py
│   ├── model_client.py
│   ├── cost_model.py
│   ├── a2ui_spec.py
│   ├── a2ui_schema_manager.py
│   └── generator.py
│
├── evaluation/                    # 评估脚本（整理但不删除）
│   ├── runner.py                  # 新增：统一 evaluation runner（substrate-agnostic）
│   ├── run_w1_killtest.py         # 保留（历史）
│   ├── run_w2_killtest.py         # 保留
│   ├── ...                        # 保留其他历史 killtest
│   └── mobilegym/                 # MobileGym 专属评估脚本
│       ├── run_x_toggle_killtest.py    # 移入（原根目录）
│       ├── run_x_toggle_ablation.py   # 移入
│       └── run_mg_vm_killtest.py      # 新增：VM五性质全量测试
│
└── harness/                       # 保留（被 substrate/ 替代，但旧脚本仍可 import）
    ├── browser_controller.py      # 保留（GUI executor 依赖）
    ├── observations.py            # 保留
    ├── replay_engine.py           # 保留
    └── trace_capture.py           # 保留
```

**重构原则**：
1. **不删除任何已实现的功能**——所有旧 import 路径通过 `__init__.py` 中的 re-export 保持兼容
2. **新增代码只进新目录**——`governance/` 是全新的，不和旧代码混放
3. **substrate 适配器必须只实现 `StateAdapter` 接口**——不允许 substrate 特定代码泄漏到上层

---

## 4. 完整任务清单（供 coding agent 执行）

### 任务包 E17-A：新任务设计（MobileGym VM五性质）

**优先级：最高（阻塞后续所有评估）**

1. 设计并实现 `SOCIAL_MORNING_BRIEF` 任务（见 §2.2 Task MG-1）：
   - 在 `taskvm/benchmark/mobilegym_fixtures.py` 里新增 `CanonicalTaskGraph`
   - 要求：2个 app（X read/like + wechat write），2个 checkpoint，帖子目标由文字而非 ID 识别
   - 设 seed 时确保帖子文字在 GenUI 投影里唯一可见（不依赖 `post_id`）

2. 扩展 `EXPENSE_AND_NOTIFY` 为双 checkpoint 版（见 §2.2 Task MG-2）：
   - 在 `top3_expense_to_wechat` 基础上增加 C1（发送后撤回尝试，诚实 409）和 C2（重发）
   - verifier 必须区分"最终消息内容正确"和"中间撤回路径诚实"两个独立判据

3. **verifier 修复**：把 x_toggle_killtest 的 verifier 从"特定 post_id 出现在列表"改为"任意新帖子出现在列表"，与新的 instruction 语义对齐（不再依赖 `post_id`）。

**验证标准**：
- 新 killtest 跑 3 samples，success_rate ≥ 0.6（纯视觉，无任何后门）
- per_post 分布不应呈现强烈的位置偏置（如果 post0 100% 而 post1/2 皆 0%，说明任务目标识别仍依赖位置而非内容）
- 证据文件：`eval_results/mg_vm_killtest_<timestamp>.json`

### 任务包 E17-B：UserBehaviorDriver 接口实现

**优先级：次高（解锁"端到端流水线"和"程序/真人无缝切换"）**

1. 新建 `taskvm/governance/` 目录及以下文件：
   - `user_behavior_driver.py`：`UserBehaviorDriver` 抽象接口 + `UserBehaviorEvent` 数据类
   - `scripted_driver.py`：`ScriptedUserDriver` 实现（从 CanonicalTaskGraph 的 `user_edit` + `checkpoints` 自动生成事件序列）
   - `subgoal.py`：`SubgoalInstruction` 数据类（包含 natural_language + patch_ops + verification_criterion）

2. 新建 `taskvm/governance/governance_interpreter.py`：
   - `GovernanceInterpreter.interpret(event, vm_state) -> list[SubgoalInstruction]`
   - `edit_field` 事件：直接翻译为 PatchOp 序列 → 逐 op 构造 subgoal（使用 binding 里的 app/entity/field 信息填充自然语言模板）
   - `rollback_to` 事件（进度条往回拖）：调用轻量 LLM（`model_client.complete_json`，非视觉模型）分析"从当前状态回退到目标 checkpoint 需要撤销的操作"，生成 undo subgoal 序列
   - **实现要求**：interpret 方法对 `ScriptedUserDriver` 和 `HumanWebSocketDriver` 完全透明——不需要知道事件来自哪里

3. 修改 `run_x_toggle_killtest.py`：把直接调用 `bridge.mutate()` 的路径，改为先构造 `UserBehaviorEvent`，再经过 `GovernanceInterpreter.interpret()` 得到 `SubgoalInstruction`，最后用 `subgoal.natural_language` 作为 `gui_act_async` 的输入

**验证标准**：
- `ScriptedUserDriver` + `GovernanceInterpreter` 生成的 subgoal，驱动 CUA 完成 toggle 操作，success_rate 不低于 E16 的 38.9%（说明链路打通了，且没有引入新的 regression）
- 额外验证：修改 `ScriptedUserDriver` 的 `event_sequence` 使之触发一次"rollback_to"事件，`GovernanceInterpreter` 能正确生成 undo subgoal，CUA 执行后 verifier 判定回退成功或诚实 409
- 证据文件：`eval_results/e17b_userbehavior_driver_<timestamp>.json`

### 任务包 E17-C：代码模块化重构

**优先级：最低（不阻塞 E17-A/B，但必须完成）**

**执行原则**：
- **不删除任何已实现的功能**
- **不改变任何已有的 public API 签名**
- **所有旧 import 路径通过 `__init__.py` re-export 保持向后兼容**

1. 新建 `taskvm/substrate/` 目录，把 `harness/state_adapter.py` 里的 `StateAdapter` 基类（及 `make_adapters` factory）移入 `substrate/base.py`，旧路径通过 `harness/__init__.py` re-export 保持兼容

2. 新建 `taskvm/substrate/mobilegym/` 子包，把 `harness/mobilegym_bridge.py` 移入 `substrate/mobilegym/bridge.py`，旧路径 re-export

3. 新建 `taskvm/substrate/osworld/README_placeholder.py`，说明 OSWorld 适配器的接入点（留空，但接口已预留）

4. 新建 `taskvm/vm_state/` 目录，把 `task_state/` 和 `verifier/` 整合（按 §3.2 结构），旧路径 re-export

5. 不新建 `interface/` 和 `governance/`（前者保留原名 `workspace_ui`，后者在 E17-B 里自然创建）

**验证标准**：
- `python -m pytest taskvm/ -x -q` 全部通过（没有 import 错误）
- `python -m taskvm.evaluation.run_w1_killtest --mock` 正常运行（旧入口不 break）
- `python -m taskvm.evaluation.run_x_toggle_killtest --samples 1` 正常运行（MobileGym 入口不 break）

---

## 5. CI 验证目标（自动化测试）

### 5.1 三层 CI 测试矩阵

```
CI 测试矩阵
┌────────────────────────────────────────────────────────────────────┐
│ L0: Import & Interface Integrity（无需外部服务，快速，<30s）          │
│   - pytest tests/test_imports.py                                    │
│   - 验证所有旧 import 路径（含 re-export）未断裂                      │
│   - 验证 UserBehaviorDriver/GovernanceInterpreter 接口签名正确       │
├────────────────────────────────────────────────────────────────────┤
│ L1: Mock-mode Pipeline Integrity（无需模型 API，<2 min）             │
│   - run_w1_killtest --mock                                          │
│   - ScriptedUserDriver + GovernanceInterpreter mock 路径            │
│   - 验证数据流从 UserBehaviorEvent → SubgoalInstruction → 完整       │
├────────────────────────────────────────────────────────────────────┤
│ L2: Real-model End-to-End（需要模型 API + MobileGym，~10-30 min）    │
│   - run_mg_vm_killtest --samples 2                                  │
│   - 验证证据文件 eval_results/mg_vm_killtest_*.json 存在             │
│   - 验证 success_rate ≥ 0.5（允许新任务学习曲线）                    │
│   - 验证 per_post 位置偏置不超过 2x（post0_sr / post2_sr ≤ 2）       │
└────────────────────────────────────────────────────────────────────┘
```

### 5.2 验证脚本约定

每个 eval_results JSON 报告必须包含以下字段（否则 CI 判 FAIL）：
```json
{
  "test": "mg_vm_killtest",
  "timestamp": "...",
  "git_commit": "...",           // 当前 git HEAD，供复查
  "substrate": "mobilegym",
  "user_behavior_driver": "scripted",   // scripted | human
  "governance_interpreter": "dynamic",  // dynamic | static
  "subgoal_count": N,                   // 共生成了多少个 subgoal
  "vm_properties_covered": {
    "bidirectional_binding": true/false,
    "cross_app": true/false,
    "governance_checkpoint": true/false,
    "reversibility_positive": true/false,
    "reversibility_negative": true/false   // honest-irreversibility 也被测到了
  },
  "success_rate": 0.xxx,
  "PASS": true/false,
  "honest_framing": "..."
}
```

### 5.3 如何运行和验证（coding agent 执行后的验证步骤）

```bash
# Step 1: 验证 import 完整性（无需外部服务）
cd /mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui
python -c "from taskvm.governance.user_behavior_driver import UserBehaviorDriver, UserBehaviorEvent; print('OK')"
python -c "from taskvm.governance.scripted_driver import ScriptedUserDriver; print('OK')"
python -c "from taskvm.governance.governance_interpreter import GovernanceInterpreter; print('OK')"
python -c "from taskvm.harness.state_adapter import StateAdapter; print('re-export OK')"  # 旧路径
python -c "from taskvm.substrate.base import StateAdapter; print('new path OK')"          # 新路径

# Step 2: Mock pipeline 验证
python -m taskvm.evaluation.run_w1_killtest --mock
# 预期：所有 task PASS=True（mock 模式），耗时 <2 min

# Step 3: UserBehaviorDriver 集成测试（mock 模式，无需 MobileGym）
python -m taskvm.governance.scripted_driver --task release_reschedule --dry-run
# 预期：输出 SubgoalInstruction 列表，不报错

# Step 4: MobileGym VM 任务 killtest（需要完整环境）
# 先启动 MobileGym：cd <mobilegym_repo> && python -m bench_env.server &
python -m taskvm.evaluation.mobilegym.run_mg_vm_killtest --task social_morning_brief --samples 2
# 预期：eval_results/mg_vm_killtest_*.json 存在，success_rate >= 0.5

# Step 5: 验证无 regression（旧 X toggle killtest 仍然工作）
python -m taskvm.evaluation.run_x_toggle_killtest --samples 2
# 预期：使用新的 UserBehaviorDriver 路径，success_rate 不低于 E16 的 38.9%
```

---

## 6. 诚实边界（不可过度声称）

1. **E17-B 的 subgoal 自然语言质量取决于 GovernanceInterpreter 的 LLM 调用质量**——如果轻量模型生成的 subgoal 措辞不清晰，CUA 会因理解歧义而失败。这是一个新的不确定性来源，必须在报告里独立记录（`subgoal_quality: "manual_review_needed"` 或类似字段）

2. **E17-A 的新任务可能会让整体 success_rate 下降**——更复杂的跨 app 任务 + 纯视觉 instruction = 更低的 SR。这不是 regression，而是"我们终于在测真实能力而不是在测一个 ill-posed 的代理任务"。报告里必须明确说明。

3. **`rollback_to` 事件的 GovernanceInterpreter 路径（动态推断）是新代码，首次运行**——第一次运行时必须检查 LLM 生成的 undo subgoal 是否合理（manual review），不能盲目相信第一次的数字。

4. **E17-C 的模块化重构必须保证零功能变化**——如果重构后任何旧 killtest 报告了不同的数字，那一定是重构引入了 regression，必须找出并修复，不得以"新架构"为由接受数字变化。

---

## 7. `.mrules` 写入要点（完成后必须更新）

本轮结束时，必须在 `.mrules` 里新增 E17，记录：
1. 三个被修复的根本问题（拦腰截断、ill-posed 任务、MobileGym VM五性质缺失）
2. `UserBehaviorDriver` 接口的设计决策及其理由
3. 新目录结构的 re-export 兼容性约定
4. 每个任务包的验证结果（含 eval_results/*.json 文件名）
5. 遗留问题（OSWorld 适配器预留但未实现，`static_pregeneration` 策略未实现）

---

*文档完成时间：2026-08-12。供下一个 coding agent 直接执行，不需要额外询问用户。*
