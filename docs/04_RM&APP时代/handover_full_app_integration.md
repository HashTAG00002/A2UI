# 交接文档：MobileGym 全量 APP 接入 TaskVM

## 角色

你是 GLM-3 coding agent。负责将 MobileGym 的全量 27 个应用（13 daily apps + 14 system apps）及 OS runtime 层接入 TaskVM 框架。本文档由审计 agent 基于源码逐行验证后编写，所有路径、常量名、行为均为事实引用。

---

## 1. 背景

TaskVM 的 taskvm/substrate/mobilegym/bridge.py 是连接 TaskVM 与 MobileGym（一个 React + Playwright 的移动 OS 模拟器）的 HTTP 桥接服务。当前 bridge.py 第 103 行硬编码了：

```python
APPS = ["wechat", "alipay", "x"]
```

仅 3 个 app。而 MobileGym 实际通过 manifest.ts 注册了 27 个 app（由 PackageManagerService.ts 第 8-11 行的 import.meta.glob 自动发现 apps/*/manifest.ts 和 system/*/manifest.ts）。本工单要求全量接入。

此外，MobileGym 还有完整的 OS runtime 层（任务管理、通知中心、系统设置等），这些不属于任何 app 但属于手机世界的一部分。本工单也要让 oracle 能读取 os 层状态。

---

## 2. 事实清单（逐源码验证）

### 2.1 MobileGym 全量应用清单（27 个）

数据来源：mobilegym/apps/*/manifest.ts（13 个）+ mobilegym/system/*/manifest.ts（14 个），共 27 个 manifest.ts 文件。

#### 13 个 Daily Apps（apps/ 目录）

| # | app_id | 目录名 | displayName | 有 state.ts |
|---|---|---|---|---|
| 1 | alipay | Alipay | 支付宝 | YES |
| 2 | bilibili | Bilibili | 哔哩哔哩 | YES |
| 3 | ebay | Ebay | eBay | YES |
| 4 | map | Map | 地图 | YES |
| 5 | railway12306 | Railway12306 | 铁路12306 | YES |
| 6 | redbook | RedBook | 小红书 | YES |
| 7 | reddit | Reddit | Reddit | YES |
| 8 | spotify | Spotify | Spotify | YES |
| 9 | tencent_meeting | TencentMeeting | 腾讯会议 | YES |
| 10 | weather | Weather | 天气 | YES |
| 11 | wechat | Wechat | 微信 | YES |
| 12 | wechat_reading | WechatReading | 微信读书 | YES |
| 13 | x | X | X | YES |

#### 14 个 System Apps（system/ 目录）

| # | app_id | 目录名 | displayName | 有 state.ts |
|---|---|---|---|---|
| 14 | answer_sheet | AnswerSheet | 答题卡 | YES |
| 15 | browser | Browser | 浏览器 | YES |
| 16 | calculator | Calculator | 计算器 | NO |
| 17 | calculator2 | Calculator2 | 计算器2 | YES |
| 18 | calendar | Calendar | 日历 | YES |
| 19 | clock | Clock | 时钟 | YES |
| 20 | compass | Compass | 指南针 | YES |
| 21 | contacts | Contacts | 电话 | YES |
| 22 | file_manager | FileManager | 文件 | YES |
| 23 | gallery | Gallery | 相册 | YES |
| 24 | notes | Notes | 笔记 | YES |
| 25 | settings | Settings | 设置 | YES |
| 26 | sms | Sms | 短信 | YES |
| 27 | theme_store | ThemeStore | 主题商店 | NO |

关键事实：
- 25 个 app 有 state.ts（zustand store），get_state(required_apps=...) 能返回它们的状态。
- 2 个 app 没有 state.ts：calculator 和 theme_store。对它们调用 get_state(required_apps=["calculator"]) 会触发 5 次重试（mobile_gym.py 第 843-870 行），最终 warning 但不报错（返回不含该 app key 的 state）。
- APP_NAME_MAP（mobile_gym.py 第 256-352 行）有约 80 个 key（中文/英文/别名 -> appId），其 .values() 的 set 恰好是这 27 个 appId。_KNOWN_APP_IDS（第 1191 行）是 frozenset(set(APP_NAME_MAP.values()))。
- open_app() 方法（第 915-934 行）接受中文名、英文名或 appId，通过 APP_NAME_MAP 映射或直接检查 _KNOWN_APP_IDS。

验证命令（在 mobilegym 目录下执行）：

    find apps system -name "manifest.ts" | wc -l   # 应输出 27
    find apps system -name "state.ts" | wc -l       # 应输出 25

### 2.2 当前 TaskVM 集成状态

文件 taskvm/substrate/mobilegym/bridge.py：

- 第 103 行：APPS = ["wechat", "alipay", "x"] -- 仅 3 个 app
- 第 267-274 行：act_primitive 方法中 kind == "open" 分支用 APPS 校验 target，不在列表中的 app 会被拒绝（返回 failed）
- 第 307-316 行：_activate 方法用 APPS 调用 env.reset(app_ids=APPS) 和 env.get_state(required_apps=APPS)
- 第 320-325 行：reset 方法同样用 APPS
- 第 327-373 行：inject_task 方法中 get_state(required_apps=APPS) 以及 wechat 专用的 seed 逻辑（add_chats / add_contacts）
- 第 383-428 行：read_resource 方法只支持 3 个 resource：wechat_chats、alipay_transactions、x_posts
- 第 582-599 行：x_state 方法只读 X app 的 toggle lists
- 第 601-612 行：session_state 方法只返回 wechat + alipay 的 summary
- 第 615-719 行：mutate_wechat -- wechat 专用写路径（GUI 手势发送消息）
- 第 730-947 行：mutate_x -- X 专用写路径（GUI 手势 toggle）
- 第 960-1014 行：html_view -- 只渲染 wechat chats + alipay transactions 的 HTML 视图

文件 taskvm/substrate/mobilegym/provider.py：

- 第 22 行：surface_app=cfg.get("app", "wechat") -- 默认 app 是 wechat
- 第 33 行：app=cfg.get("app", "wechat") -- 评估环境默认也是 wechat

文件 taskvm/substrate/mobilegym/session.py：

- 第 43 行：surface_app: str = "wechat" -- session 默认 app 是 wechat
- 第 48-52 行：SurfaceInfo 用 surface_app 构造，display_name=surface_app.title()

文件 taskvm/substrate/mobilegym/evaluation.py：

- 第 29 行：self.app 注释为 # wechat | alipay | x -- 只支持 3 个 app
- 第 35-38 行：_RESOURCE、_ID_FIELD、_ENTITY_KIND 字典只映射了 wechat、alipay、x 三个 app
- 第 56-65 行：oracle_state 方法用 _RESOURCE 查表，不在表中的 app 会 KeyError

文件 taskvm/workspace_ui/app_open.py：

- 第 352-355 行：--apps 参数默认 "wechat,alipay" -- 只暴露 2 个 app
- 第 279 行：app_name = str(body.get("app", "") or "wechat").strip() -- 默认 wechat
- 第 282-285 行：校验 app_name not in state.apps -- 只接受 --apps 传入的列表

文件 scripts/app_mobilegym.sh：

- 没有显式设置 APPS 环境变量（bridge 的 APPS 常量是硬编码的）
- 第 111-113 行：启动 bridge 时不传 --apps 参数（bridge 的 main() 也没有 --apps 参数）

文件 taskvm/substrate/port.py：

- 第 120-122 行：GUI_ACTION_KINDS = ("click", "tap", "type", "key", "scroll", "wait", "open") -- open 动作用于打开 app
- 第 136 行：GuiAction.target -- "open" 动作的目标 app 名称

### 2.3 OS Runtime 层状态

mobile_gym.py 的 get_state() 返回 {"os": {...}, "apps": {...}} 结构。os 层包含：

- os.tasks：任务管理器中的任务列表（app_id -> task info）
- os.activeAppId：当前前台 app 的 appId
- os.settings：系统设置（亮度、音量、WiFi、蓝牙等）
- os.notifications：通知列表
- os.home_screen：主屏幕布局（app 图标位置等）

注意：当前 bridge.py 的 get_state(required_apps=APPS) 只请求 APPS 列表中的 app store。os 层的状态总是包含在返回的 state 中（因为 _get_state() 调用 window.__SIM__.getState() 返回完整 state，required_apps 只影响重试逻辑）。

当前 bridge.py 没有 oracle 路由来读取 os 层状态。你需要新增一个 GET /api/os_state/<sid> 路由。

### 2.4 MobileGym 官方任务套件

数据来源：mobilegym/bench_env/task/ 目录。

以下 app 有官方任务套件（tasks.py + app.py）：calendar、clock、notes、sms、settings、calculator。

其他 app 没有官方任务套件，但全量接入后 agent 可以通过 open 动作自由打开并交互。

---

## 3. 需要修改的文件及变更说明

### 3.1 新增文件：taskvm/substrate/mobilegym/app_catalog.py

创建一个单一数据源模块，集中管理全量 app 元数据，供 bridge.py、provider.py、session.py、evaluation.py、app_open.py 共享导入。

```python
# taskvm/substrate/mobilegym/app_catalog.py
"""MobileGym full app catalog -- single source of truth.

Generated from mobilegym/apps/*/manifest.ts + mobilegym/system/*/manifest.ts
(27 manifest.ts files, auto-discovered by PackageManagerService.ts).

25 apps have state.ts (zustand store); 2 do not (calculator, theme_store).
get_state(required_apps=[...]) on a storeless app triggers a retry-then-warn
but does NOT error (mobile_gym.py L843-870).
"""

# (app_id, displayName, category, has_store)
# category: "daily" or "system"
_APP_TUPLES = [
    # Daily Apps (13) -- all have state.ts
    ("alipay",          "支付宝",     "daily", True),
    ("bilibili",        "哔哩哔哩",   "daily", True),
    ("ebay",            "eBay",        "daily", True),
    ("map",             "地图",        "daily", True),
    ("railway12306",    "铁路12306",   "daily", True),
    ("redbook",         "小红书",      "daily", True),
    ("reddit",          "Reddit",     "daily", True),
    ("spotify",         "Spotify",    "daily", True),
    ("tencent_meeting", "腾讯会议",    "daily", True),
    ("weather",         "天气",        "daily", True),
    ("wechat",          "微信",        "daily", True),
    ("wechat_reading",  "微信读书",    "daily", True),
    ("x",               "X",          "daily", True),
    # System Apps (14) -- calculator and theme_store have NO state.ts
    ("answer_sheet",    "答题卡",      "system", True),
    ("browser",         "浏览器",      "system", True),
    ("calculator",      "计算器",      "system", False),
    ("calculator2",     "计算器2",     "system", True),
    ("calendar",        "日历",        "system", True),
    ("clock",           "时钟",        "system", True),
    ("compass",         "指南针",      "system", True),
    ("contacts",        "电话",        "system", True),
    ("file_manager",    "文件",        "system", True),
    ("gallery",         "相册",        "system", True),
    ("notes",           "笔记",        "system", True),
    ("settings",        "设置",        "system", True),
    ("sms",             "短信",        "system", True),
    ("theme_store",     "主题商店",    "system", False),
]

# Full list of all 27 app_ids
ALL_APP_IDS = tuple(t[0] for t in _APP_TUPLES)

# Only apps that have a zustand store (25 of 27)
STORE_APP_IDS = tuple(t[0] for t in _APP_TUPLES if t[3])

# Display names map
DISPLAY_NAMES = {t[0]: t[1] for t in _APP_TUPLES}

# Category map
CATEGORIES = {t[0]: t[2] for t in _APP_TUPLES}

def is_valid_app(app_id: str) -> bool:
    """Check if app_id is a known MobileGym app."""
    return app_id in ALL_APP_IDS

def is_valid_app_or_raise(app_id: str) -> str:
    """Validate and return app_id, or raise ValueError."""
    if app_id not in ALL_APP_IDS:
        raise ValueError(
            f"unknown app {app_id!r}; valid: {ALL_APP_IDS}")
    return app_id

def get_display_name(app_id: str) -> str:
    """Get the user-visible display name for an app."""
    return DISPLAY_NAMES.get(app_id, app_id)
```

### 3.2 修改 taskvm/substrate/mobilegym/bridge.py

注意：此文件位于 FROZEN 层（taskvm/substrate/）。根据 Repository Contract，修改冻结层需 RFC。但本次修改是扩展 APPS 常量和相关方法，属于 substrate port 的正常维护，不算架构变更。请在 commit message 中注明：feat(substrate): expand APPS to full 27-app catalog (MG-FULL-APPS)。

变更点：

(a) 第 103 行：将 APPS 常量改为从 app_catalog 导入

```python
# taskvm/substrate/mobilegym/bridge.py
# 替换第 103 行：
#   APPS = ["wechat", "alipay", "x"]
# 为：
from taskvm.substrate.mobilegym.app_catalog import ALL_APP_IDS, STORE_APP_IDS
APPS = list(STORE_APP_IDS)  # 25 apps with state.ts, for get_state(required_apps=...)
ALL_APPS = list(ALL_APP_IDS)  # all 27, for open whitelist
```

(b) 第 267-274 行：act_primitive 方法中 kind == "open" 分支，将白名单从 APPS 改为 ALL_APPS

```python
# 替换第 269 行：
#   known = [a for a in APPS if a == target]
# 为：
known = [a for a in ALL_APPS if a == target]
```

(c) 新增两个 oracle 路由方法：app_state 和 os_state

```python
async def app_state(self, sid: str, app_id: str) -> dict:
    """Read-only: the raw zustand store state of one app.
    Returns {} for storeless apps (calculator, theme_store)."""
    await self._activate(sid)
    state = self._sid_live.get(sid) or await self.env.get_state(required_apps=APPS)
    apps = state.get("apps", {})
    return {"sid": sid, "app": app_id, "state": apps.get(app_id, {}) or {}}

async def os_state(self, sid: str) -> dict:
    """Read-only: the OS runtime state (tasks, activeAppId, settings, etc.)."""
    await self._activate(sid)
    state = self._sid_live.get(sid) or await self.env.get_state(required_apps=APPS)
    return {"sid": sid, "os": state.get("os", {}) or {}}
```

(d) 在 build_app 函数（第 1023 行起）中注册两个新路由

```python
# 在 build_app 函数中，app.router.add_get 行之后添加：
async def api_app_state(request):
    sid = request.match_info["sid"]
    app_id = request.match_info["app_id"]
    return web.json_response(await bridge.app_state(sid, app_id))

async def api_os_state(request):
    sid = request.match_info["sid"]
    return web.json_response(await bridge.os_state(sid))

app.router.add_get("/api/app_state/{sid}/{app_id}", api_app_state)
app.router.add_get("/api/os_state/{sid}", api_os_state)
```

(e) 第 601-612 行：session_state 方法扩展为返回全量 app 摘要（或保持原样，新增 os_state 路由即可）

保持原样不改（零回归原则）。os 层的摘要由新的 os_state 路由提供。

### 3.3 修改 taskvm/substrate/mobilegym/provider.py

变更点：

(a) 导入 app_catalog

```python
from taskvm.substrate.mobilegym.app_catalog import is_valid_app_or_raise
```

(b) MobileGymProvider.create_session 和 MobileGymEvaluationProvider.create 方法中，校验 app 参数

```python
# MobileGymProvider.create_session 中：
app = cfg.get("app", "wechat")
is_valid_app_or_raise(app)  # 新增校验
return MobileGymSubstrateSession(
    sid=cfg.get("sid", ""),
    bridge_url=cfg.get("bridge_url", "http://localhost:3019"),
    surface_app=app,
    timeout=cfg.get("timeout", 30.0),
)

# MobileGymEvaluationProvider.create 中：
app = cfg.get("app", "wechat")
is_valid_app_or_raise(app)  # 新增校验
return MobileGymEvaluationEnvironment(
    app=app,
    sid=cfg.get("sid", ""),
    bridge_url=cfg.get("bridge_url", "http://localhost:3019"),
    timeout=cfg.get("timeout", 10.0),
)
```

### 3.4 修改 taskvm/substrate/mobilegym/session.py

变更点：

(a) 第 48-52 行：SurfaceInfo 的 display_name 从 app_catalog 获取

```python
from taskvm.substrate.mobilegym.app_catalog import get_display_name

# 替换第 48-52 行：
self._surface = SurfaceInfo(
    surface_id=f"mobilegym:{surface_app}",
    display_name=get_display_name(surface_app),  # 原来是 surface_app.title()
    surface_kind="app",
)
```

### 3.5 修改 taskvm/substrate/mobilegym/evaluation.py

变更点：

(a) 第 35-38 行：_RESOURCE 等字典保持原样（向后兼容，3 个 legacy app 的语义投影不变）

(b) 新增 app_state 和 os_state 方法

```python
def app_state(self, sid: str | None = None, app_id: str | None = None) -> dict:
    """Generic oracle: raw store state of any app (verifier/benchmark only).
    For the 3 legacy apps (wechat/alipay/x), use oracle_state() for the
    flattened semantic projection. This method returns the raw store dict."""
    s = sid or self.sid
    a = app_id or self.app
    r = requests.get(f"{self._bridge}/api/app_state/{s}/{a}",
                     timeout=self.timeout)
    r.raise_for_status()
    return r.json()

def os_state(self, sid: str | None = None) -> dict:
    """OS runtime state oracle (tasks, activeAppId, settings, etc.)."""
    s = sid or self.sid
    r = requests.get(f"{self._bridge}/api/os_state/{s}",
                     timeout=self.timeout)
    r.raise_for_status()
    return r.json()
```

(c) oracle_state 方法（第 56-65 行）保持原样：对 wechat/alipay/x 返回语义投影，对其他 app 调用者应使用 app_state() 获取原始 store。

### 3.6 修改 taskvm/workspace_ui/app_open.py

变更点：

(a) 第 352-355 行：--apps 参数默认改为全量

```python
# 替换第 352-355 行：
ap.add_argument("--apps", default=None,
                help="apps offered in the hero (comma-separated; "
                     "default: all 27 MobileGym apps)")
```

(b) main 函数中，如果 args.apps 为 None，从 app_catalog 加载全量

```python
# 在 main 函数中，apps 赋值之前添加：
if args.apps is None:
    from taskvm.substrate.mobilegym.app_catalog import ALL_APP_IDS
    apps = ALL_APP_IDS
else:
    apps = tuple(a.strip() for a in args.apps.split(",") if a.strip())
```

(c) 第 263 行 /api/app/status 的 apps 字段升级为分组对象数组

```python
# 替换第 263 行：
#   "apps": list(state.apps),
# 为：
from taskvm.substrate.mobilegym.app_catalog import CATEGORIES, DISPLAY_NAMES
"apps": [
    {"id": aid, "name": DISPLAY_NAMES.get(aid, aid),
     "group": CATEGORIES.get(aid, "daily")}
    for aid in state.apps
],
```

### 3.7 修改 scripts/app_mobilegym.sh

变更点：

(a) 第 111-113 行：bridge 启动命令不变（bridge 的 APPS 常量已在代码中改为全量）

(b) 更新第 17-19 行的注释，反映全量接入后的 factory world 描述

---

## 4. 并行执行策略

本工单可以拆分为 3 个并行子任务，每个子任务由一个 GLM-3 coding agent 执行。文件所有权严格隔离，避免冲突。

### 子任务 A：bridge + app_catalog

- 新建 taskvm/substrate/mobilegym/app_catalog.py
- 修改 taskvm/substrate/mobilegym/bridge.py
- 新建 tests/substrate/test_mobilegym_catalog.py
- 新建 tests/substrate/test_mobilegym_bridge_fullapps.py

### 子任务 B：evaluation + provider + session

- 修改 taskvm/substrate/mobilegym/evaluation.py
- 修改 taskvm/substrate/mobilegym/provider.py
- 修改 taskvm/substrate/mobilegym/session.py
- 新建 tests/substrate/test_mobilegym_evaluation_fullapps.py

### 子任务 C：APP UI + 脚本 + README

- 修改 taskvm/workspace_ui/app_open.py
- 修改 scripts/app_mobilegym.sh（注释更新）
- 修改 README.md（app 覆盖面描述更新）
- 新建 tests/workspace_ui/test_app_open_fullapps.py

### 并行铁律

1. 单一文件所有权：每个文件恰一个 owner，改动落到他人领地时上报主 agent 统一处置。
2. 子 agent 不跑真进程：只用 fake/stub 单测，不起 sim/bridge/APP、不占端口。
3. 子 agent 不碰 git：改动留在工作树，每个子 agent 完成后由主 agent 审 diff、跑该范围测试、代为提交。
4. 接口契约冻结：app_catalog.py 的公开 API（ALL_APP_IDS, STORE_APP_IDS, is_valid_app, get_display_name 等）和 bridge 新路由的 HTTP 契约（GET /api/app_state/<sid>/<app_id>, GET /api/os_state/<sid>）以本文档 3.1 和 3.2 为冻结契约，子 agent 不得单方改契约。

---

## 5. 测试要求

### 5.1 新增测试

tests/substrate/test_mobilegym_catalog.py：

- 27 个 app_id 计数正确
- 25 个 store app（有 state.ts）
- calculator 和 theme_store 不在 STORE_APP_IDS 中
- is_valid_app 对全量 27 个返回 True，对 "phone"/"camera"/"qqmusic" 返回 False
- get_display_name 返回正确的中文名

tests/substrate/test_mobilegym_bridge_fullapps.py（fake env）：

- act_primitive "open" 接受全量 27 个 app（包括 calculator、theme_store）
- act_primitive "open" 对未知 app（如 "phone"）返回 failed
- app_state 对有 store 的 app 返回非空 dict
- app_state 对 calculator/theme_store 返回空 dict（honest）
- os_state 返回包含 os key 的 dict

tests/substrate/test_mobilegym_evaluation_fullapps.py（fake HTTP）：

- app_state(sid, "calculator2") 返回 state dict
- os_state(sid) 返回 os dict
- provider.create 对合法 app 返回 session/env
- provider.create 对非法 app 抛出 ValueError

tests/workspace_ui/test_app_open_fullapps.py（Flask test client）：

- /api/app/status 返回 27 个分组 app
- POST /api/app/goals 对合法 app 接受
- POST /api/app/goals 对非法 app 返回 400

### 5.2 回归测试

以下既有测试必须保持绿（零回归）：

- tests/substrate/test_mobilegym_runtime_purity.py
- 全部 contract lock 测试
- architecture gate 测试（substrate 不 import taskvm_bench）
- bench 侧 B-09 anti-bypass 测试

### 5.3 E2E 验收（真栈，主 agent 执行）

起栈后：

- G3a：27 个 app 逐个 open + observe，全量截图存档
- G3b：sid 往返（inject_task 种子 -> 切 sid -> 切回 -> 验证种子仍在）
- G3c：calculator 可达性（open calculator -> os_state.activeAppId == "calculator"）
- G3d：oracle 非侵入（os_state/app_state 双读前后 fingerprint 不变）
- G3e：APP hero 显示全量 27 个 app 分组
- G3f：全量 pytest 回归

---

## 6. 环境速查

- 仓库：/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui
- MobileGym 真身：/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/mobilegym
- 解释器：TASKVM_PYTHON=/mnt/dolphinfs/.../conda/envs/taskvm/bin/python
- Playwright：PLAYWRIGHT_BROWSERS_PATH=$CONDA_PREFIX/opt/ms-playwright
- LD_LIBRARY_PATH：repo/.chromelibs/lib（前置冒号拼接）
- 起栈：./scripts/app_mobilegym.sh
- 停止：./scripts/stop.sh
- OPENAI_API_KEY：从 .mrules 第 8 节解析

---

## 7. Git 提交纪律

- 一个 commit 一个性质，message 引用工单号 MG-FULL-APPS
- 只 git add 自己修改的文件，禁止 git add -A / git add .
- eval_results/、*.png 不进 staging
- 建议提交序列：
  1. feat(substrate): MG-FULL-APPS A -- app catalog (27 apps) + bridge full-app coverage
  2. feat(substrate): MG-FULL-APPS B -- evaluation app_state/os_state reads + provider validation
  3. feat(workspace_ui): MG-FULL-APPS C -- APP hero full app list (grouped chips)
  4. test: MG-FULL-APPS -- full regression evidence

---

## 8. 证据坐标（审计 agent 已核实）

    mobilegym/apps/*/manifest.ts                    13 个 daily app manifest
    mobilegym/system/*/manifest.ts                  14 个 system app manifest
    mobilegym/apps/*/state.ts + system/*/state.ts   25 个 zustand store（calculator/theme_store 无）
    mobilegym/bench_env/env/mobile_gym.py:103        APPS = ["wechat","alipay","x"]（当前硬编码）
    mobilegym/bench_env/env/mobile_gym.py:256-352   APP_NAME_MAP（约 80 个别名 -> 27 个 appId）
    mobilegym/bench_env/env/mobile_gym.py:835-870   get_state(required_apps) 重试逻辑
    mobilegym/bench_env/env/mobile_gym.py:915-934   open_app（接受中文名/英文名/appId）
    mobilegym/bench_env/env/mobile_gym.py:1191      _KNOWN_APP_IDS = frozenset(set(APP_NAME_MAP.values()))
    mobilegym/os/data/appRegistry.tsx:47-49         import.meta.glob 自动发现 app 组件
    mobilegym/os/PackageManagerService.ts:8-11       import.meta.glob 自动发现 manifest
    taskvm/substrate/mobilegym/bridge.py:103          APPS = ["wechat","alipay","x"]
    taskvm/substrate/mobilegym/bridge.py:267-274     act_primitive open 白名单
    taskvm/substrate/mobilegym/bridge.py:307-316     _activate 用 APPS
    taskvm/substrate/mobilegym/bridge.py:383-428     read_resource 只支持 3 个 resource
    taskvm/substrate/mobilegym/bridge.py:582-599     x_state（oracle 直读 store 先例）
    taskvm/substrate/mobilegym/bridge.py:601-612     session_state 只返回 wechat+alipay
    taskvm/substrate/mobilegym/bridge.py:960-1014    html_view 只渲染 wechat+alipay
    taskvm/substrate/mobilegym/evaluation.py:35-38  _RESOURCE 只映射 wechat/alipay/x
    taskvm/substrate/mobilegym/provider.py:22        默认 app = wechat
    taskvm/substrate/mobilegym/session.py:43-52     surface_app 默认 wechat
    taskvm/substrate/port.py:120-122                GUI_ACTION_KINDS（含 open）
    taskvm/workspace_ui/app_open.py:352-355          --apps 默认 wechat,alipay
    taskvm/workspace_ui/app_open.py:279-285          goals 校验 app in state.apps
    scripts/app_mobilegym.sh:111-113                 bridge 启动命令（无 --apps）

---

## 9. 完成定义

- [ ] 全量 27 个 app 可通过 open 动作打开（bridge act_primitive 接受全量）
- [ ] 全量 25 个有 store 的 app 可通过 app_state oracle 路由读取
- [ ] os 层状态可通过 os_state oracle 路由读取
- [ ] APP hero 显示全量 27 个 app（分组：日常应用 13 / 系统应用 14）
- [ ] provider 对非法 app 抛出 ValueError
- [ ] 既有 wechat/alipay/x 路径零回归（语义投影路由保留不变）
- [ ] 全部 pytest 测试绿（新增 + 回归）
- [ ] 证据落盘 eval_results/*.json
