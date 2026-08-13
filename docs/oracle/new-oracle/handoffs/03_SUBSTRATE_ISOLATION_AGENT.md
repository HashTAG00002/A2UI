# Coding Agent B：Substrate 完全隔离、GUI-only Port 与可见观察

## 你的唯一任务

让 Web built-in、MobileGym、OSWorld 的所有差异只存在于 `taskvm/substrate/`，并让上层只看到统一的观察与动作接口。彻底删除 TaskVM runtime 的 API executor、hidden database ID 和 `read_canonical()` 依赖。

依赖：Agent A 的 domain/kernel public contract 已合并。

先阅读：

- `00_README_MASTER_HANDOFF.md`
- `taskvm/substrate/base.py`
- `taskvm/harness/state_adapter.py`
- `taskvm/harness/browser_controller.py`
- `taskvm/harness/mobilegym_bridge.py`
- `taskvm/substrate/mobilegym/bridge.py`
- `taskvm/execution/gui_executor.py` 与 `gui_executor_async.py`，只为识别 substrate-specific 代码

---

## Owned paths

```text
taskvm/substrate/**
tests/substrate/**
```

可在获得接口 owner 同意后最小修改：

```text
taskvm/ports/**
taskvm/harness/**        # 仅删除/迁移 substrate-specific 文件
taskvm/apps/**           # 仅启动适配，不改业务 demo 内容
```

不要修改 projection、Task Architect、workflow/kernel 语义。

---

## 最终统一接口

实现一个 platform-neutral port，语义至少覆盖：

```text
SubstrateProvider
  create_session(config) -> SubstrateSession

SubstrateSession
  list_surfaces()
  observe(surface, previous_fingerprint?) -> Observation
  act(surface, GuiAction, epoch) -> ActionReceipt
  capture(surface) -> VisualArtifact
  close()
```

Observation 应包含：

- 最新截图或截图引用；
- 清洗后的可见文本/accessibility 信息；
- surface fingerprint；
- TaskVM-owned surface handle candidates；
- observation revision 与时间。

GuiAction 只允许现实动作，例如 click/tap/type/key/scroll/wait。不要提供“set field”“mutate database”“restore state”的底层捷径。

### SurfaceHandle

- 由 TaskVM runtime 创建并缓存；
- 可基于可见 label、角色、邻近文本、bbox、结构指纹；
- 失效后可重新绑定；
- 不得等同于 App DB primary key；
- DOM 中技术上存在但用户不可见的 `data-event-id`、`data-task-id`、`data-file-id` 不得进入 observation/model-facing metadata。

---

## Built-in Web 实现

把以下 Web/Playwright 特有代码迁入：

```text
taskvm/substrate/builtin_web/
  provider.py
  session.py
  browser.py
  observer.py
  actuator.py
  launcher.py
```

要求：

1. `BrowserController` 不再位于 generic `harness/`。
2. 删除硬编码的 `/mnt/dolphinfs/...` browser/lib 路径；从配置、环境变量或 Playwright 安装发现。
3. DOM/a11y 可以用于观察，但只输出可见/可访问内容和 TaskVM-owned handle。
4. 写入只能经 Playwright 真实鼠标、键盘、滚动和触摸等动作。
5. app URL、端口和启动方式只在 built-in provider 配置中出现。
6. 上层无需知道当前 App 是 Calendar/TaskBoard/Drive/Mail；surface metadata 可有用户可见 display name，但不能靠 app-specific operator dispatch。

---

## MobileGym 实现

合并当前两套 bridge：

```text
taskvm/harness/mobilegym_bridge.py
taskvm/substrate/mobilegym/bridge.py
```

只保留 `taskvm/substrate/mobilegym/`。

硬规则：

- `setState` / simulator store 注入只可由 Evaluation environment setup 使用，不能从 SubstrateSession runtime 方法暴露。
- runtime 写入只通过真实 tap/type/swipe/keypress。
- 如果某操作没有真实撤销 UI，返回明确的不可逆 capability/result，不允许 snapshot restore。
- MobileGym 特有 async loop、坐标规范化和 bridge 通信全部封装在目录内。

---

## OSWorld 实现

本任务只需建立可用 port skeleton + 最小真实 adapter，不要求大规模 task coverage。

最低交付：

- 能连接一个 OSWorld session；
- 列出至少一个 desktop surface；
- 捕获截图；
- 执行 click/type/key/scroll；
- 返回统一 Observation/ActionReceipt；
- 上层代码不出现 OSWorld import。

不得只留 `README_placeholder.py` 或 `NotImplementedError` 后宣称完成；如果当前环境无法实际跑 OSWorld，至少要有 contract tests + 一个可执行的 integration entrypoint，并在报告中明确未复现部分。

---

## 删除 API Executor

从 runtime 代码中彻底移除：

- `executor="api"` 默认或选项；
- `requests.post` 形式的 app mutation；
- API-mode rollback；
- API/GU​I 分支 factory；
- `StateAdapter.mutate()` 中的隐藏写回；
- `harness/state_adapter.py` compatibility shim。

环境启动/seed/oracle API 应迁到 Evaluation plane，不能作为 substrate runtime port。

### 需要提供静态 Gate

扫描 `taskvm/` 的非 evaluation/environment 目录，禁止：

```text
requests.post(... app mutation ...)
executor="api"
read_canonical
set_state / setState
restore_snapshot
```

不要用简单地改名绕过 gate。

---

## 配置与 Registry

唯一允许选择 substrate 的地方：

```text
bootstrap/config
SubstrateRegistry.create(name, config)
```

选择完成后，上层得到同一 Protocol。不要在 workspace UI、runtime scheduler、workflow executor 里再次分支。

---

## 测试

1. 通用 contract test 同时跑 built-in 和 fake substrate：相同 observe/act/capture 语义。
2. Web 可见性测试：隐藏 data attribute 不出现在 Observation。
3. API backdoor static test。
4. Handle cache：结构未变可复用；结构指纹变化后失效。
5. MobileGym 不可逆测试：没有 UI 撤销能力时明确失败，状态不被 hidden restore。
6. 浏览器路径可移植测试：仓库中无用户绝对路径。

---

## 明确不做

- 不做 workflow planning。
- 不做 projection UI。
- 不读取/更新 Projection Store 业务语义。
- 不把 app-specific locator/operator 暴露成跨层协议。
- 不为通过测试调用 benchmark fixture 的 hidden ID。

---

## 验收

```bash
pytest -q tests/substrate tests/architecture
python -m compileall taskvm/substrate
```

并确保：

```bash
grep -R "executor.*api\|read_canonical\|data-event-id\|data-task-id\|data-file-id" \
  taskvm/substrate taskvm/runtime taskvm/projection
```

不出现生产依赖；允许只在禁止性测试与 Evaluation adapter 中出现。
