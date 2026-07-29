# 盘后任务 PRD

状态：已确认  
最后确认日期：2026-07-26  
对应 Map：`../maps/30-after-close.md`  
需求所有权：盘后触发、readiness、编排、计算、校验、发布和补跑

## 1. 目标

每个 A 股交易日完成所需数据准备、全市场日线因子和事件计算、结果校验和正式发布，并支持本地调试及远程补跑。

## 2. 已确认需求

### AC-01 远程自动运行

远程稳定运行位置应在交易日数据 ready 后自动启动盘后任务。

### AC-02 本地不自动调度

本地自动 Scheduler 默认关闭。

### AC-03 本地完整手动调试

本地必须能够手动运行和调试完整盘后链路，包括单股、指定股票池和全市场。

### AC-04 日线盘后计算

当前盘后编排不再以 15m 数据作为主计算要求，主要基于日线计算趋势、结构和动量。

### AC-05 固定参数一次计算

正式盘后 DSA 和相关因子每日以固定参数计算一次。页面只筛选已计算结果，不触发策略组合和重新计算。

### AC-06 Readiness 门槛

任务启动前必须检查与本次计算相关的数据 readiness，并给出不满足原因。

### AC-07 Run 隔离

每次完整运行具有明确 run 标识。局部调试、失败运行和未校验结果不得自动成为正式结果。

### AC-08 计算与发布分离

计算完成不等于发布。只有满足校验和发布条件的 run 才能成为正式结果。

### AC-09 正式发布指针

正式读取通过明确发布标识或 `published_run_id` 指向当前正式 run。

### AC-10 两阶段发布

发布应采用可重复、可恢复的两阶段语义：

1. 结果和状态准备完成；
2. 校验通过后切换正式发布指针。

不得把“同一数据库事务内一次完成所有长链路操作”作为唯一安全保证。

### AC-11 幂等与补跑

任务、子任务和发布过程应支持安全重试与补跑，避免重复记录、重复发布和状态倒退。

### AC-12 跨 Worker 领取

子任务领取、超时和重新领取必须具有明确规则，避免多 Worker 重复处理或永久丢失。

### AC-13 完成状态

至少区分：

- pending；
- running；
- partial；
- completed；
- failed；
- published。

具体字段可以不同，但语义必须明确。

### AC-14 部分失败

全市场任务部分失败时，不得直接标记整体成功。必须保留成功、失败、跳过和待重试范围。

### AC-15 旧触发路径清理

不再使用的盘后自动触发入口应删除，不长期保留重复编排路径。

### AC-16 统一盘后编排（CHANGE-20260728-008）

系统只允许 `job_name=after_close_orchestrator`、`run_type=full` 一种盘后任务类型。不得存在 `dsa_only` 独立端点、独立 `mode` 分支或独立 `run_type`。

"从 DSA 阶段重算"通过现有 `force` 端点 + `restart_from="daily_ready"` 参数实现，仍是同一 `after_close` 任务，不创建 `dsa_only` 类型，不跳过后续特征/快照/发布步骤。仅 admin 可用；必须先验证日线覆盖率 ≥ 90%。

状态链：`queued→running→refreshing_daily→syncing_boards→checking_coverage→computing_features→publishing→succeeded`；`StrategyRun` 状态链：`running→completed→published`，异常 → `failed`。不得在发布前伪造 `completed`。

对已有旧 `dsa_only` queued/running 记录只读识别；生产执行前通过正式 cancel/interrupted/retry 服务处理，禁止 DELETE 或直接改 metadata。

## 3. 验收标准

- 远程交易日任务可自动运行。
- 本地不会因 Scheduler 自动触发，但可完整手动运行。
- 单股、股票池和全市场使用同一核心链路。
- 未校验 run 不会成为正式发布结果。
- 重复执行不会产生无法解释的重复发布。
- 系统不存在 `dsa_only` 独立端点、独立 mode 分支或独立 run_type。
- `restart_from="daily_ready"` 从 DSA 阶段重算，仍执行完整后续链路。
- Map 对 AC-01 至 AC-16 给出实现状态、入口和验证证据。

## 4. 增量发布架构需求（CHANGE-20260729-006/007）

### AC-08：单股事务与检查点
- 计算/事务/检查点粒度为"单股×阶段"；batch 只控制吞吐和内存，不是完成或发布边界。
- 结果 commit 成功后才标记 item succeeded；单股失败只回滚该股票，不回滚其他已成功股票。
- 恢复只处理 pending、可重试 failed、lease 过期 running；成功且 hash/version 相同不重算。

### AC-09：分层发布指针
- `factor_publications` 按交易日+kind 维护发布指针，`data_run_id` 指向覆盖率门禁通过的不可变 run。
- `CORE_PUBLICATION_MIN_COVERAGE = 0.98`，低于门禁拒绝发布。
- 发布只做小事务原子切换指针，不复制结果数据；不得修改已发布 run。
- `trade_date` 必须为 NOT NULL（禁止普通唯一约束允许多 NULL 产生重复 pointer）。
- `publish_market_aggregation` 必须验证 `source_core_run_id` 等于该日期已发布 stock_core pointer。
- `publish_history_cross_section` 的 coverage 必须由 DB 统计，不接受调用方任意传值。
- pointer 不得倒退到旧 run。

### AC-10：读取端统一接入 pointer
- 用户市场列表、筛选排序、个股详情默认只读取 stock_core pointer 指向的 run。
- 无 pointer 时兼容回退到 `published_at IS NOT NULL`（旧数据）；有 pointer 后严禁混读不同 run。
- 个股暂存结果可在管理端查看；详情若展示未正式发布结果，必须返回 `is_provisional=true`、`data_run_id` 和 `calculation_status`。
- `is_stale` 真源为 `bars_daily.max(trade_date)`，不是 `StockFeatureSnapshot.max(trade_date)`。

### AC-14：独立任务与核心保护
- 市场聚合、事件、chip、通知为独立任务，失败只重试自身，不反改核心。
- 主编排在 core pointer 发布后即可标记 `core_published` 并允许复盘。
- 最终状态可为 `completed_with_errors`，但不得因 optional 失败反改 core。
- chip.core_run_id = snapshot_run_id（不指向 SchedulerJobRun.id）。
- chip 严格按 instrument_id + trade_date + snapshot_run_id + algorithm_version + status=succeeded 匹配。

