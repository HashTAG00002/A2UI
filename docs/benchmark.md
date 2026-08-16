# TaskVM Final Benchmark — 运行手册与证据规范

> Owner: Agent FF (E-波次收尾). 层: `taskvm_bench/benchmark/`（考场定义）+ `taskvm_bench/evaluation/`（考官与判卷）.
> 本文档只描述**当前代码真实存在的行为**; 任何与代码不符的表述都是 bug.

## 1. 一句话

在确定性模拟考场（`BenchmarkWorld`）上, 以**能力对齐的确定性 fakes** 驱动同一 CUA 能力面, 对比四种执行条件（TaskVM / direct-CUA / planner-CUA / oracle 上界）+ 两个消融（no-verifier / no-replan）, 由隐藏判卷 Oracle 打分, 全程落盘可复核.

## 2. 运行

```bash
# 标准环境: /mnt/dolphinfs/.../conda/envs/taskvm/bin/python, PYTHONPATH=仓库根
PYTHONPATH=. python -m taskvm_bench.evaluation.cli list --what suites

# 冒烟 (15 任务 × 全部 6 条件 × 1 seed, 分钟级)
PYTHONPATH=. python -m taskvm_bench.evaluation.cli run --suite final --seeds 1 --budget smoke \
    --run-id ff-smoke --out eval_results

# 论文矩阵 (15 × 6 × 3 seeds, paper 预算放宽 loop/round 上限)
PYTHONPATH=. python -m taskvm_bench.evaluation.cli compare --config configs/paper_matrix.json

# 从落盘产物重新渲染论文表格 (不重跑)
PYTHONPATH=. python -m taskvm_bench.evaluation.cli report --input eval_results/<run-id> --format paper
```

产物: `eval_results/<run-id>/report.json`（机器可读 + per-trial `trials/*.json`）与 `report.md`. `eval_results/` 不进 git.

## 3. 公平合同 (所有条件共享)

| 轴 | 共享方式 |
|---|---|
| 任务 | 同一 `TaskSpec` (seed/goal/success/protected) |
| CUA 能力 | 同一 `TemplateCUA` 确定性模板 (可见文本 → 动作) |
| 模型预算 | 同一 `TrialBudget` (`BUDGET_PRESETS[\"paper\"]`) |
| 观测 | 同一 `WorldSubstrate` 协议 (可见文本, 无内部 ID) |
| 判卷 | 同一隐藏 `Oracle` (只读世界真值, 只在 trial 结束打分) |

## 4. 条件 (registry 冻结)

- `taskvm` — 完整栈: architect 一次构图 → kernel 门控 → runtime autonomy → verifier
- `direct-cua` — 无任务结构, CUA 直接对原始观测循环
- `planner-cua` — 先出静态计划再执行 (无重规划/无验证回环)
- `taskvm-oracle-upper-bound` — **诊断专用, 永不进 headline** (Verifier 拿隐藏真值, 衡量 verification 层天花板)
- `taskvm-no-verifier` — 消融: 去 completion 验证
- `taskvm-no-replan` — 消融: 去 governance 重规划路由

## 5. 任务分类 (12 Family × 5 Split × 15 任务)

Family 覆盖: SEQUENCE / FANOUT_FANIN / BOUNDED_LOOP / CROSS_APP / GOAL_PATCH / LOCAL_PATCH / INTERRUPTION / CONFLICT / ROLLBACK / UI_DRIFT / PARTIAL_FAILURE / IRREVERSIBLE.
Split 覆盖: ID / SURFACE_HOLDOUT (venues) / OPERATION_HOLDOUT (rsvp) / CROSS_PRODUCT / SEED_NOISE.
合同锁定在 `tests/benchmark/test_taxonomy.py` (含 holdout 诚实性: ID 任务零 venues/rsvp 泄漏).

## 6. 报告指标

per-condition: `successes/graded` + Wilson 95% CI, model calls (by role), GUI 手势数, 系统写入数, 触发注入数, failure taxonomy (`classify_failure`: cua/planner/oracle/timeout/…), harness crash 与 evaluation error **单列不吞**.
FF 波次新增: sync 心跳数与快路径占比, observed-plane 失配数, GoalPatch 复用率.

## 7. 诚实边界 (paper 必须写明)

1. **fakes 不是真模型**: CUA/模型是确定性能力模板, 数字回答的是"结构问题" (任务结构何时挽回 CUA 失误), 不是任何具体大模型的成绩.
2. **world 不是 MobileGym/OSWorld**: 确定性模拟考场, 换真实底座走 `substrate` 参数扩展, 本波未含.
3. **oracle 上界是诊断量**: 拿了隐藏真值的验证器只用于界定 verification 层潜力.
4. **stale-response 执行率未计量**: 该指标在确定性世界里恒为 0, 诚实标注为 unmeasured 而非 0.

## 8. 最终合同测试

`tests/evaluation/test_final_contract.py` 锁定六条: ①现代 runtime 平面 import graph 零 benchmark/evaluation (legacy 违规文件登记为只可收缩的债务表) ②Oracle 秘密零泄漏 (canary spec) ③同 seed 完全复现 ④故障注入只走公共 governance 缝 ⑤聚合不静默丢弃失败 trial ⑥W 期 gate-script 词汇从代码/README/pyproject/final docs 消失.
