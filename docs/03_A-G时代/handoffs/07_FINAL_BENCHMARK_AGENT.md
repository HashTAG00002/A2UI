# Coding Agent F：最终 Benchmark、客观指标与历史 Kill-test 清理

## 你的唯一任务

把当前阶段性 evaluation scripts 重构成一套论文可用、可复现、权限隔离的 final benchmark。删除所有 `run_*killtest.py` 和 W1/W2/W3/GG/EE 叙事，但保留有科学价值的任务、指标和负对照思想，并纳入统一 runner。

依赖：核心 runtime 的公开接口已经稳定。

---

## Owned paths

```text
taskvm/benchmark/**
taskvm/evaluation/**
taskvm/baselines/**
tests/benchmark/**
tests/evaluation/**
docs/benchmark.md
```

不得为了跑分修改 runtime 行为或在生产代码中加 evaluator hooks。

---

## 必须删除的历史入口

当前 `taskvm/evaluation/` 中以下文件全部删除，不重命名保留旧逻辑入口：

```text
run_full_loop_killtest.py
run_mg_vm_killtest.py
run_mobilegym_killtest.py
run_open_world_killtest.py
run_reconciliation_killtest.py
run_substrate_invariance_killtest.py
run_w1_killtest.py
run_w2_killtest.py
run_w3_killtest.py
run_x_toggle_killtest.py
```

`run_four_step_arc.py` 若只是中间 demo 也删除；有用场景迁移为 final scenario fixture。

`run_interaction_compression.py`、`run_model_ablation.py` 的思想可以保留，但必须并入统一 CLI/report，不再各自输出互不兼容结果。

生产 dispatcher 中的 wrong-target/no-op 分支由 Runtime Agent 删除；benchmark 需要负对照时用独立 baseline/fault injector，不污染 production dispatcher。

---

## 最终目录建议

```text
taskvm/benchmark/
  tasks/
  generators/
  splits/
  environments/
  oracles/
  metrics/
  reports/
  schema.py

taskvm/evaluation/
  runner.py
  cli.py
  matrix.py
  aggregation.py
  statistics.py

taskvm/baselines/
  direct_cua.py
  planner_cua.py
  taskvm_oracle_binding.py
```

统一运行方式示例：

```bash
python -m taskvm.evaluation.cli run --suite final --system taskvm --substrate builtin_web
python -m taskvm.evaluation.cli compare --config configs/paper_matrix.yaml
python -m taskvm.evaluation.cli report --input eval_results/... --format paper
```

不要在文件名中出现 phase/gate/killtest。

---

## 权限隔离

### Runtime process

只能使用 visible observation + GUI actions。

### Environment controller

可以：

- reset/seed；
- 启动 App/设备；
- 注入实验事件（外部冲突、UI drift、用户 GoalPatch）；
- teardown。

### Oracle process

可以读取 hidden ground truth，仅用于评分。

要求：

- 三者使用不同对象/接口，最好不同进程或至少不同 dependency boundary；
- runtime 没有 oracle reference；
- model prompt capture 测试证明 hidden state 未泄漏；
- oracle failure 不能改变 runtime 行为，只使该 trial 判为 evaluation error。

---

## 系统条件与 Ablation

至少：

1. Direct CUA；
2. Planner + CUA；
3. TaskVM full；
4. TaskVM + oracle binding（仅 upper bound，不是主系统）；
5. TaskVM – live projection；
6. TaskVM – independent runtime verifier；
7. TaskVM – goal patch/recovery；
8. TaskVM – persistent GenUI projection。

最重要的公平性：同一任务、同一 CUA backend、相同最大预算，只改变 harness。

API executor 不能作为系统条件。可以有 deterministic environment sanity test，但不能作为 paper execution score。

---

## Task 与 Split

当前 generator 的结构模板太少，不能用随机参数数量冒充任务多样性。最终至少覆盖多个结构族：

- create/edit/delete/move/copy；
- search/filter/compare/aggregate；
- schedule/send/draft；
- cross-app dependency；
- sequence；
- fan-out/fan-in；
- bounded loop；
- conflict；
- partial failure；
- irreversible action；
- permission/availability failure；
- target UI drift；
- user LocalPatch 与 GoalPatch。

Split 独立报告：

```text
ID task
unseen task composition
unseen app/surface
unseen operation/field semantics
cross-product holdout
UI reskin/layout drift
cross-substrate
```

App holdout 的 runtime 不得拥有该 App 的 operator/selector/adapter；Evaluator 可以有 oracle。

---

## 指标

### 最终结果

- task success；
- goal predicate satisfaction；
- field/entity level non-interference；
- round-trip projection correctness；
- verified completion vs false done。

### 效率

- model calls 按角色；
- vision calls；
- input/output tokens；
- GUI actions；
- wall-clock p50/p90/p95；
- repairs/replans；
- interaction compression：必须用实测 trace，理论下界单独标注。

### 治理与恢复

- pause latency / actions after pause；
- stale response execution rate（应为 0）；
- GoalPatch success、已完成工作复用率、invalidated node 数；
- conflict detection/overwrite rate；
- rollback success/fidelity；
- irreversible truthfulness；
- partial branch preservation。

### 泛化

- 各 split success；
- ID→OOD gap；
- binding/state compiler quality；
- substrate invariance：任务语义一致、底层轨迹不同。

### 同步成本

- heartbeat 总数；
- fast-path proportion；
- incremental compiler calls；
- active-surface redundant polls；
- projection staleness。

---

## 自动干预 Scenario

统一 event injector：

1. CUA 运行中外部改变目标字段；
2. CUA request 在途时注入 LocalPatch；
3. 注入 GoalPatch 改终结目标；
4. rollback 到随机 committed checkpoint；
5. UI label/layout 改变；
6. fan-out 一条 lane 失败；
7. inactive mobile/desktop surface 外部变化；
8. 不可逆动作边界。

所有注入带确定时间/event revision，便于复现。

---

## 统计与报告

- 每个随机条件多次运行；
- 报告置信区间，不用单个最好 sample；
- 预先冻结 success predicate；
- trial-level JSON 保留原始事件和调用账本；
- 汇总报告不得覆盖或弱化原始 verdict；
- failure taxonomy 至少区分 observation/compiler/architect/CUA/verifier/recovery/budget/environment。

统一 report schema，不让每个 runner 自己造字段。

---

## 最终测试

1. Runtime import graph 无 benchmark/evaluation。
2. Oracle no-leak：captured prompts/events 不含 hidden state。
3. Seed/reset 可复现。
4. Fault injector 不修改 production dispatcher。
5. Report aggregation 对失败 trial 不静默丢弃。
6. 所有历史 `killtest` 字符串从代码/README/pyproject/final docs 消失，允许只在 git history。

---

## 明确不做

- 不为了凑 800 个实例只重复四个模板。
- 不把 API execution 作为成功率主表。
- 不用模型自评代替 oracle。
- 不把 oracle state 用于 runtime rollback/sync。
- 不在 production code 加 `broken/noop/wrong_target` mode。

---

## 验收

```bash
pytest -q tests/benchmark tests/evaluation tests/architecture
python -m taskvm.evaluation.cli --help
```

提交一份最小 smoke report 和一份完整 paper-matrix 配置；真实大规模模型结果可以后跑，但 runner、权限隔离和统计必须交付完整。
