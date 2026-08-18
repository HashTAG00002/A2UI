# TaskVM

**Compile live state of multiple existing applications into an editable,
executable, verifiable task interface.**

人治理任务，Agent 自治应用。TaskVM 将分散在多个应用、设备与交互环境中的
任务相关状态，组织成一个持续存在、可操纵、可验证的任务层。用户直接治理任务
要达到的状态；Agent 在治理边界内自主完成具体的软件操作；独立 verifier 读
ground-truth 判定"改的发生、没改的不动、界面重新同步"。

五条结果性性质（权威定义见
[`docs/A2UI_开工大纲_v0_心智模型对齐版.md`](docs/A2UI_开工大纲_v0_心智模型对齐版.md) §3）：
自底向上的实时投影 · 双向可执行性 · substrate independence · governance over
autonomy · 独立验证与诚实可逆性。

---

## 架构

```text
taskvm/                        ← prototype（枪 + 靶场）
  domain/                      语义类型：Patch / Node / ObservedValue
  kernel/                      历史驱动执行引擎：event_index / compensation / checkpoint
  architect/                   State Compiler + Task Architect（意图→架构，一次模型调用）
  governance/                  GovernanceService：LocalPatch / GoalPatch / Rollback 路由
  runtime/                     AutonomyRuntime：bounded loop / verifier / compensation
  projection/                  Flask API + SSE + SPA 前端
  substrate/                   底座隔离 port（builtin_web / MobileGym / OSWorld）
  apps/                        五个自建 web 应用（calendar / taskboard / drive / mail / outlook_cal）
  thirdparty/                  外部底座适配器
  workspace_ui/                生产组合根 + 静态前端 + demo 入口

taskvm_bench/                  ← 计量（论文测量仪；与 prototype 物理隔离）
  benchmark/                   考场定义：12 Family × 5 Split × 15 任务
  evaluation/                  隐藏判卷 Oracle + harness + runner + statistics
  baselines/                   direct-CUA / planner-CUA 对照
  task_state/                  replay compiler（冻结输入绑定）
  harness/                     locator + replay engine
```

### 六层依赖方向（禁止逆流）

```
domain ← kernel ← architect ← governance ← runtime ← projection
                                      ↘ substrate port ↗
```

- `taskvm/` 零引用 `taskvm_bench/`（架构门 `_ALWAYS_BANNED` 锁定）。
- benchmark 层单向导入 prototype（仅 SUT 方向）。

---

## 快速启动

```bash
pip install -e .
pip install playwright && playwright install chromium   # 浏览器测试

./scripts/dev.sh       # 启动 5 个 builtin app + projection UI
# UI: http://127.0.0.1:3016
# stop: ./scripts/stop.sh
```

环境变量：
- `TASKVM_PYTHON`：解释器路径（默认 `python3`）
- `TASKVM_UI_PORT`：projection UI 端口（默认 3016）
- `TASKVM_DEMO_APP`：demo 会话使用的 builtin app（默认 `calendar`）
- `TASKVM_DEMO_OFFLINE`：设为非空 → 使用确定性 placeholder CUA（不调用 provider，诚实 FAIL）
- `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `TASKVM_MODEL`：真实 CUA provider 配置

---

## 用户 Workflow（当前 demo 的真实行为）

1. **启动**：`./scripts/dev.sh` → 5 个 builtin app + projection UI 就绪；demo
   会话由 composition（`taskvm/workspace_ui/demo.py` → `compose_task_runtime`）
   在启动时组装注册，一次绑定**一个** builtin app（`TASKVM_DEMO_APP`，默认
   `calendar`；可选 calendar/taskboard/drive/mail/outlook_cal）
2. **会话查询**：GET `/api/sessions` 仅列出已注册会话（无 POST 创建路由；
   session 注册只发生在 composition 启动阶段）
3. **任务目标与计划**：demo 的 goal 与 kernel plan 是手工 fixture（固定目标
   「把日历事件『产品发布』改期到 2026-08-18」、任务变量 `event_date`
   2026-08-14→2026-08-18、固定 2 节点 plan），**不经** State Compiler /
   Task Architect。自然语言 goal → fresh observation → StateCompiler →
   TaskArchitect → Kernel → Runtime 的 full composition 已在 RM-0.B 落地
   （`taskvm/workspace_ui/composition.py::bootstrap_real_full`），入口为
   `taskvm/workspace_ui/demo_open.py`（`--goal` 必填；`--substrate
   {mobilegym,builtin_web}`，默认 `mobilegym`；`--start-bridge` 可托管拉起
   bridge）——dev.sh 的 demo 会话本身仍走手工 fixture
4. **CUA 模型**：默认在线模式为真实 `HttpCUAModel`（`OPENAI_BASE_URL` /
   `OPENAI_API_KEY` / `TASKVM_MODEL`；一次 predict = 一次 provider 请求 = 一条
   ledger 记录）；`TASKVM_DEMO_OFFLINE` / `--offline` 切换为确定性 placeholder，
   诚实 FAIL，不声称自治完成
5. **自治推进**：POST `/api/sessions/<sid>/governance/start` → runtime bounded loop
6. **实时投影**：SSE 流是 GET `/api/sessions/<sid>/sse`；GET
   `/api/sessions/<sid>/events` 是分页 JSON 事件日志（`offset`/`limit`），不是 SSE
7. **治理操作**：
   - LocalPatch：修改局部目标
   - GoalPatch：重构任务终点（旧 in-flight response 自动失效）
   - Checkpoint：标记可回退点
   - Rollback：经 GUI compensation 回退到 checkpoint
8. **独立验证**：verifier 从 fresh visible observation 判定完成（不自证）
9. **诚实边界**：不可逆动作诚实标记 PARTIAL / IRREVERSIBLE

---

## Final Benchmark

```bash
# 列出可用 suite
python -m taskvm_bench.evaluation.cli list --what suites

# 冒烟（15 任务 × 6 条件 × 1 seed）
python -m taskvm_bench.evaluation.cli run --suite final --seeds 1 \
    --budget smoke --run-id smoke --out eval_results

# 论文矩阵（15 × 6 × 3 seeds）
python -m taskvm_bench.evaluation.cli compare --config configs/paper_matrix.json

# 从落盘产物重新渲染报告
python -m taskvm_bench.evaluation.cli report --input eval_results/<run-id> --format paper
```

条件矩阵：`taskvm`（完整栈）/ `direct-cua`（无任务结构）/ `planner-cua`（静态计划）/
`taskvm-oracle-upper-bound`（诊断专用）/ `taskvm-no-verifier`（消融）/ `taskvm-no-replan`（消融）。

诚实边界（详见 [`docs/benchmark.md`](docs/benchmark.md)）：
- fakes 不是真模型——数字回答的是"结构问题"，不是任何具体大模型的成绩。
- world 是确定性模拟考场，不是 MobileGym/OSWorld。
- oracle 上界是诊断量，永不进 headline。
- open-world/holdout split 是 **normalized semantic OOD（归一化语义 OOD）**：只换
  未见 key / 未见操作语义 / 未见 surface label，world 对所有 surface 统一渲染
  `k=v` 文本；不测试新 DOM 层级 / 视觉布局 / viewport / 主题变化——真正
  visual-reskin holdout 属于后续 RM-1.0。

---

## 局限

- **真模型**：benchmark 使用确定性 fakes；真 provider 全弧未在 CI 验证。
- **SPA 前端**：当前是 honest JSON snapshot，非完整 SPA。
- **真实底座**：MobileGym/OSWorld 底座接入已有 substrate port，但 benchmark 未覆盖。
- **不可逆动作**：系统诚实标记 PARTIAL/IRREVERSIBLE，不假装所有动作可撤销。

---

## 文档

- [心智模型总纲](docs/A2UI_开工大纲_v0_心智模型对齐版.md)
- [Benchmark 运行手册](docs/benchmark.md)
- [合同文档](docs/contracts/)：kernel / architect / runtime / projection / substrate / governance
- [审计章程](docs/contracts/audit_charter.md)
