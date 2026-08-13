# Coding Agent G：最终集成、启动可靠性、文档与历史包袱清理

## 你的唯一任务

在各模块 agent 合并后，完成冲突解决、端到端验收、可移植启动、文档权威收敛和旧阶段产物删除。你不是来添加新研究功能，而是确保最终 prototype 真正可启动、可操作、可演示、可复现。

---

## 合并顺序

1. Agent A：domain/kernel/contracts
2. Agent B：substrate
3. Agent C：architect/governance
4. Agent E：runtime/verification/rollback
5. Agent D：projection/frontend
6. Agent F：benchmark/evaluation
7. Optional Agent H：cross-device bonus

每一步合并后运行该模块测试和 architecture gate，不要等全部合完一次性 debug。

---

## Owned paths

```text
README.md
run.sh / scripts/**
pyproject.toml
docker-compose.yml
.mrules 或 AGENTS.md
docs/** 中权威与运行文档
tests/integration/**
tests/smoke/**
configs/**
```

可为集成修复最小修改各模块，但必须通知对应 owner；不重新打破边界。

---

## 权威文档清理

### 替换心智模型

将 `01_MENTAL_MODEL_ALIGNMENT_REPLACEMENT.md` 的正文替换：

```text
docs/A2UI_开工大纲_v0_心智模型对齐版.md
```

保留用户要求的结果性原则，不加入当前代码字段、接口或临时实现。

### 删除阶段/历史 handoff

完成信息迁移后删除：

```text
docs/A2UI_EE阶段开工目标.md
docs/A2UI_GG阶段开工目标.md
docs/DEMO_RUNBOOK_MobileGym.md
docs/HANDOFF_E17.md
docs/HANDOFF_TaskVM.md
```

若其中有仍然必要的启动说明，只迁移最终事实到一个短 `docs/RUNBOOK.md`，不保留阶段叙事。

### 重写 `.mrules`

当前 `.mrules` 是上千行“心智模型演化史”，又规定所有 agent 必读并持续追加，已经成为上下文污染源。

把它替换为不超过约 150 行的最终 repository contract，内容仅包括：

- 五个结果性原则；
- 六层依赖方向；
- runtime/evaluation 权限隔离；
- GUI-only、无 hidden IDs；
- owned paths/不要越层；
- 测试与诚实汇报要求；
- 不再追加 episode、phase、gate、killtest 历史。

也可改为根目录 `AGENTS.md`，但必须确认本地 coding agent 会读取哪个文件；不要同时保留两个互相冲突的权威规则。

### README/pyproject

删除：

- `version = "0.1.0-w1"`；
- `W1 kill-test` 描述；
- read GUI/write API；
- API executor 用法；
- hidden canonical 作为 runtime 能力；
- 阶段性 gate 数字。

README 只写：概念、最终架构、快速启动、用户 workflow、final benchmark、局限。

---

## 可移植启动

当前 `run.sh` 硬编码个人 conda、仓库和 browser/lib 绝对路径，并以 Flask `--debug` 启动。重写为：

- 从脚本自身解析 repo root；
- 使用当前激活 Python 或可配置 `TASKVM_PYTHON`；
- 依赖由 `pyproject.toml`/安装命令管理；
- Playwright 路径由标准安装发现；
- 无 `/mnt/dolphinfs/...`；
- 默认不启用 debug reloader；
- 每个服务写 PID/log，并可优雅 stop；
- 启动前检查端口，不粗暴 kill 所有进程；
- health check 等待服务可用；
- 失败时输出具体服务与日志路径；
- 提供 `scripts/dev.sh` 和 `scripts/stop.sh`，必要时提供 Docker Compose。

最终单命令应能：

```bash
./scripts/dev.sh
```

启动 built-in environment、TaskVM server 和所需 worker。

---

## 完整 Smoke Journey

自动化集成测试必须覆盖：

1. 服务启动；
2. 用户输入任意 goal，而不是 task_id；
3. 初始 visible observation；
4. State Compiler + Task Architect 形成 task projection；
5. 前端显示 workflow；
6. start 后 CUA 自主推进；
7. surface card 显示实时已有截图；
8. fan-out/fan-in 达到 verified checkpoint；
9. 用户在运行中 LocalPatch；
10. 用户在 CUA response 在途时 GoalPatch，旧 response 不执行；
11. inactive surface 外部变化产生 conflict；
12. rollback 经 GUI compensation；
13. 不可逆动作诚实显示；
14. event stream 断线重连；
15. 全程无 405/500/browser console error。

如果真实模型不稳定，提供 deterministic fake model 的 CI smoke 和真实模型的手动 E2E；不得用 fake 的成功声称真实 CUA 已通过。

---

## 前端专项验收

用自动 crawler 收集页面中的：

- form action；
- fetch URL；
- button command；
- SSE endpoint；
- screenshot URL。

逐一发请求验证不出现 405/500。尤其验证旧问题中的：edit、undo、checkpoint、adopt/goal patch、resolve、start、pause、rollback。

检查：

- CSS/JS 文件 200；
- 无重复 debug server；
- screenshot artifact MIME 正确；
- SSE reconnect 不重复执行 command；
- session URL 编码正确。

---

## 清理旧代码

合并后删除已无引用的：

- `taskvm/_shim/`；
- `harness/state_adapter.py`；
- replay/scripted driver 中间实现；
- legacy `workspace_ui/renderer.py`；
- static f-string editable fallback；
- old API adapter branches；
- `--no-genui`、`--executor api`；
- `replanner.py` stub；
- 所有 phase/killtest 注释与用户可见文案；
- 未使用的 duplicated mobilegym bridges。

运行 dead-code/import scan，不要因为“可能以后有用”保留两个真源。

---

## Final Gates

```bash
pytest -q
python -m compileall taskvm
python -m taskvm.evaluation.cli --help
./scripts/dev.sh
```

再运行：

- architecture import gate；
- API/hidden-ID backdoor scan；
- route/method crawler；
- UI Playwright smoke；
- deterministic full-loop；
- 至少一个真实 GUI/CUA end-to-end；
- git grep 确认不存在 `killtest`、`W1`、`executor=api`、`read_canonical` production path。

---

## 发布报告

最终报告必须包含：

- 最终目录树；
- 一张主架构图；
- 一条完整 runtime trace；
- 一份所有 route 的测试表；
- 一份模型调用账本；
- 一份 final benchmark smoke report；
- 已知局限（尤其真实模型、OSWorld、不可逆动作）；
- 从干净环境启动的复现步骤。

不要再产出新的阶段性“开工目标”文档。后续 issue 进入正常 issue tracker，而不是继续扩写心智模型或 `.mrules` 历史。
