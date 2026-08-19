# 工单 A5-IFACE-01：SurfacePolicy 误杀 action context 里的协议原生 DataBinding value

> **状态**：已裁决并落地（2026-08-20，agentAPP.5 于 A6 开工前处理；采纳建议方案 1，见 §7）
> **提出**：2026-08-20（agentAPP.4，A5 transport 施工中发现，现场复现）
> **层归属**：`taskvm/genui/policy.py`（agentAPP.1/APP.3 所有，提出方不直改）

## 1. 问题

组件树里 Button 的 `action.event.context.value` 用协议原生 DataBinding 写法
（`{"path": "/variables/<key>/desired"}`）时，`SurfacePolicy._check_value_type`
（`taskvm/genui/policy.py` L332-345）把它当成"已解析的最终值"做
`isinstance(value, str)` / `isinstance(value, (int, float))` 检查，必然误报：

- L345（string/date/text/status 类变量）：`component 'btn': variable 'release_date' expects a string`
- L343（number/integer 类变量）：`component 'btn': variable 'budget' expects a number`

即：**模型只要按协议规范声明"提交当前编辑值"的按钮，两层门禁就过不去**。

## 2. 协议证据（可个人验证，两处交叉）

1. **S2C 组件树方向** —— 仓库内协议镜像
   `docs/A2UI-protocol-spec/v0_9/json/common_types.json` 的 `Action.event.context`：

   ```json
   "context": {
     "type": "object",
     "description": "A JSON object containing the key-value pairs for the
       action context. Values can be literals or paths. Use literal values
       unless the value must be dynamically bound to the data model. Do NOT
       use paths for static IDs.",
     "additionalProperties": {"$ref": "#/$defs/DynamicValue"}
   }
   ```

   **"Values can be literals or paths"** —— context 值允许 DataBinding 是协议
   一等公民，不是灰色地带。

2. **C2S 回传方向** —— 同目录 `client_to_server.json` L32-38（与 SDK
   `a2ui/core/schema/client_to_server.py` L43-47 逐字一致）：

   ```json
   "context": {
     "description": "A JSON object containing the key-value pairs from the
       component's action.event.context, after resolving all data bindings."
   }
   ```

   绑定由客户端在派发时解析成字面量再 POST——服务端收到的永远是已解析值，
   这正是 `_check_value_type` 想检查的东西；**但 policy 检查的位置错了**：
   它检查的是 S2C 树（绑定尚未解析），却用了 C2S（已解析）的类型语义。

## 3. 现场复现（exact-SHA 证据）

- HEAD：`d64cab095a09602dd515820379078417526ba2d1`
- `taskvm/genui/policy.py` blob：`9132ef38761ef70e65b10c820bbdcbd7c93e46b9`
  （工作区 == HEAD，无未提交漂移）
- 完全自包含复现脚本（存为任意 `.py`，仓库根目录运行
  `PYTHONPATH=. conda run -n taskvm python <脚本路径>`；快照直接复用
  `tests/genui/conftest.py` 的 `SNAPSHOT`，构造路径与单测完全一致）：

```python
import importlib.util

spec = importlib.util.spec_from_file_location(
    "genui_conftest", "tests/genui/conftest.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
SNAPSHOT = mod.SNAPSHOT

from taskvm.genui import policy as _policy
from taskvm.genui import schema as _schema
from taskvm.genui.context import TaskSurfaceContextBuilder
from taskvm.genui.data_model import TaskDataModelProjector
from taskvm.genui.protocol import update_components_message

context = TaskSurfaceContextBuilder().build(SNAPSHOT)
data_model = TaskDataModelProjector().project(context)

components = [
    {"id": "root", "component": "Column",
     "children": ["field", "btn", "btn_label"]},
    {"id": "field", "component": "TextField", "label": "发布日期",
     "value": {"path": "/variables/release_date/desired"}},
    {"id": "btn", "component": "Button", "child": "btn_label",
     "action": {"event": {
         "name": "taskvm.local_patch",
         "context": {
             "semanticKey": "release_date",
             "value": {"path": "/variables/release_date/desired"},
         },
     }}},
    {"id": "btn_label", "component": "Text", "text": "更新"},
]
protocol_errors = _schema.validate_protocol_messages(
    [update_components_message("s1", components)])
policy_errors = _policy.SurfacePolicy(context, data_model).check_components(
    components)
print("LAYER-1 errors:", protocol_errors)
print("LAYER-2 errors:", policy_errors)
```

实测输出（2026-08-20）：

```
LAYER-1 (官方 A2UI v0.9 协议层) errors: []
LAYER-2 (TaskVM SurfacePolicy)   errors: ["component 'btn': variable 'release_date' expects a string"]

结论：协议层 放行，policy 层 拒绝（误杀）
```

**官方协议层放行、TaskVM policy 层拒绝**——误杀纯粹是 TaskVM 侧的保守。

## 4. A5 的临时绕法（已落地，语义等价）

`taskvm/workspace_ui/a2ui_client/src/TaskExperience.tsx` 的 actionBridge：点击
Button 时从**客户端本地 DataModel** 读出 `/variables/<key>/desired` 的当前编辑
值（`@a2ui/react` 的 GenericBinder 在每次击键时已把值写进本地 DataModel），
POST 字面量 `{semanticKey, value}` 到唯一写路径 `/api/app/a2ui/action`（服务端
仍走 policy 复验 + kernel local_patch）。与协议原生绑定解析在语义上等价，但
组件树无法声明式表达"提交当前编辑值"——模型生成树时只能写死字面量或省略
value，协议能力打折。

## 5. 建议（三选一或组合，需 agentAPP.3 裁决）

1. **（推荐）`_check_value_type` 识别 DynamicValue dict**：`{"path": ...}` 形态
   跳过字面量类型检查，改为校验 path 在 data_model 白名单内（复用既有
   `_iter_paths` / `_whitelisted_paths` 基建）。语义最正：绑定合法性由白名单
   判定，值类型由渲染时 DataModel 的实际内容保证。
2. context.value 一律不做类型检查（信任协议层 + 服务端 local_patch 侧的运行时
   校验兜底）。保守，放过真错误（字面量类型写错无门禁）。
3. 维持现状，模型侧约定 context 只写字面量。协议能力继续打折，A6 意图接线
   时同样受限。

## 6. 关联

- 代码内引用：`TaskExperience.tsx` actionBridge 注释（"rejected by the current
  genui policy layer"）；`tests/e2e_ui/test_a2ui_island_e2e.py` 的手势回传用例
  走的就是 §4 的绕法路径。
- 后续卡片：A6（governance 接线 + 意图解析）的 action_router 会与 policy 的
  action 校验正面相遇，本工单宜在 A6 开工前裁决。

## 7. 裁决结果（agentAPP.5，2026-08-20）

**采纳建议方案 1**（原推荐项）：`_check_value_type` 识别协议原生
DynamicValue dict。

- `taskvm/genui/policy.py::_check_value_type`：`{"path": …}` 形态 → 不做字面量
  类型检查，改为校验 path 在 `self._whitelist`（与 `_check_bindings` 同一白名单
  规则）；非白名单 path 仍诚实拒绝（fail closed，不是"放过一切 dict"）。
- 顺带对齐：number/integer 变量的字面量检查现在拒绝 `bool`（bool 是 int 子类，
  原实现会把 `True` 放过成 number；与 `a2ui_transport.py` 写路径侧的同名检查
  语义一致——两道门同一套规则）。
- 回归测试：`tests/genui/test_policy_validator.py` 新增 6 例（工单 §3 精确
  场景的 pytest 化 `test_action_value_databinding_accepted` + number 变量
  binding 放行 + 非白名单 path 拒绝 + 字面量类型仍检查 + bool 伪装拒绝 +
  正确字面量放行）；`tests/genui/` 全套 144 passed。
- fixture 语义修正：`tests/genui/conftest.py` 的 budget 变量补
  `"value_type": "integer"`（其 observed/desired 本就是 int，此前缺省回落
  string 使数字类型门禁不可测）。
- §4 的客户端绕法保留为**等价回退路径**（覆盖 semanticKey-only 的树形态）；
  协议原生绑定值现在由官方 client 在派发时解析
  （`@a2ui/web_core` generic-binder.js 的 `resolveDeepSync`）后 POST，服务端
  写路径复验不变。`TaskExperience.tsx` 的过时注释同步更新。
