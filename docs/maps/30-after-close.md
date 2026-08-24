# 盘后任务 Map

核验状态：已基于代码审计更新（Phase 4）；Phase 5A 已修复 AC-04 日线 readiness 冲突并核验 P0 Redis 隔离
最后核验日期：2026-07-27
核验分支：dev
核验提交：72dcd6c074212c0935090ce86acc7e48ba619dcb（Phase 4）；Phase 5A 修复见 `docs/changes/2026/CHANGE-20260727-002-after-close-daily-readiness.md`
事实所有权：Scheduler、readiness、Orchestrator、Worker、run、校验和发布链路

> 本文件必须基于真实代码、数据、日志或运行结果填写。不得根据 PRD 推测实现已经存在。

## 1. 当前实现摘要

- 远程自动触发：bars_scheduler Worker 每日 16:00（上海时区）调用 `create_after_close_run`，交易日历判断后创建 SchedulerJobRun。
- 本地不自动调度：backend lifespan 不启动 Scheduler；Scheduler/Worker 必须显式设置 `WORKER_TYPE` 启动。
- 手动入口：`admin_after_close.py` 提供创建、查询、重试、恢复、force（含 `restart_from="daily_ready"` 从 DSA 阶段重算）API；`backend/scripts/trigger_dsa_batch_small.py` 为脚本入口。
- 编排任务以 `SchedulerJobRun`（job_name="after_close_orchestrator"）记录。顶层步骤统一经过 `execute_orchestrator_step`（统一步骤执行器，见 §13.5）：`refreshing_daily → syncing_boards → checking_coverage → computing_features → publishing → auction_anchor(可选) → computing_review → enqueue_chip_job(可选)`；主任务终态 `succeeded / partial_success / failed`，可被 watchdog 中断为 `interrupted` 后自动 `resume_queued`，可被管理员 `cancelled`。**`computing_review` 已抽为 `_execute_review_step` 业务体并经执行器包装（AC-02，2026-08-03）**；`watchlist_ready` 为**派生就绪指示器**（`has_succeeded_snapshot_run`），非可执行步骤，不作为执行器步骤（见 §13.5）。
- readiness：checking_coverage 步骤仅检查日线覆盖率 >= 0.9（Phase 5A 移除 15m 阻塞，符合 PRD30 AC-04）；日线不足则标记 failed，15m 缺失不再阻塞 after-close run。
- run 隔离：`create_after_close_run` 使用 run_key = `after_close_orchestrator:{trade_date}` 去重；同一 trade_date 同时只能有一个活跃（queued/running/resume_queued）任务。
- 计算与发布分离：DSA StrategyRun 完成后进入 publishing，调用 `StrategyBatchService.publish_run` 标记 published_at，再 finish snapshot run。
- 两阶段发布：阶段 1 `publish_run`（StrategyRun status=completed→published），阶段 2 `finish_snapshot_run`（snapshot run status=succeeded，写 published_at）。
- 幂等：create 去重；publish_run 对已 published 幂等返回；execute 按 last_completed_step 断点恢复。
- Worker claim/re-claim：`_after_close_poll_once` 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取 queued/resume_queued；领取时递增 lease_epoch；execute 通过 `_current_lease_epoch` ContextVar fencing。
- 状态：SchedulerJobRun 状态为 queued/running/succeeded/failed/skipped/interrupted/resume_queued；StrategyRun 状态为 queued/running/completed/partial_failed/published/failed。PRD 要求的 "pending" 由 queued 表达，"partial" 由 partial_failed 表达。
- 旧触发路径：worker.py 注释明确 `_maybe_trigger_after_close_orchestrator` 已删除。

## 2. PRD 实现映射

| PRD 条款 | 当前实现入口 | 状态 | 验证证据 |
|---|---|---|---|
| AC-01 远程自动运行 | `backend/app/worker.py:scheduled_bars_refresh`（bars_scheduler Worker，CronTrigger 16:00）→ `after_close_orchestrator.create_after_close_run` | 已实现并核验 | `worker.py:L428-L480`；`after_close_orchestrator.py:L275-L352` |
| AC-02 本地不自动调度 | `backend/app/main.py:lifespan` 不启动 Scheduler；Scheduler 仅在 `worker.py` 按 `WORKER_TYPE` 手动启动 | 已实现并核验 | Phase 2/3 本地启动日志；`config.py` 无自动 scheduler |
| AC-03 分平面调试与执行 | 本地 pure-unit/mock；远程验证栈运行 PG、Worker、migration 和完整链路 | 部分实现，远程待核验 | 本地不得启动真实盘后链；验证栈入口见 `scripts/ops/panji-verify-*` |
| AC-04 日线盘后计算 | `after_close_orchestrator.py` 步骤 refreshing_daily + computing_features；checking_coverage 仅检查日线覆盖率 >= 0.9（Phase 5A 已移除 15m 阻塞） | 已实现并核验 | `after_close_orchestrator.py:L1277-L1334`；测试 `test_after_close_orchestrator.py::test_ac04_daily_ready_15m_missing_allows_proceed` / `::test_ac04_daily_missing_blocks` / `::test_ac04_no_intraday_readiness_in_after_close_source` |
| AC-05 固定参数一次计算 | `dsa_selector.yaml` 参数 `allowed_scopes: [system]`；`create_batch_run` 使用 manifest 固定参数 | 已实现并核验 | `dsa_selector.yaml`；`strategy_batch_service.py` |
| AC-06 Readiness 门槛 | `after_close_orchestrator.py:checking_coverage` → `BarsCoverageService.compute_daily_coverage`（Phase 5A：仅日线，15m intraday 工具保留在 `BarsCoverageService` 供其他链路使用，after-close 不再调用） | 已实现并核验 | `after_close_orchestrator.py:L1277-L1334`；测试同 AC-04 |
| AC-07 Run 隔离 | `create_after_close_run` run_key 去重；`uq_scheduler_job_runs_active_run_key` 部分唯一索引 | 已实现并核验 | `after_close_orchestrator.py:L282-L314`；`scheduler_job_run.py:L42-L53` |
| AC-08 计算与发布分离 | `execute_after_close_run` computing_features → publishing；`StrategyBatchService.publish_run` 独立调用 | 已实现并核验 | `after_close_orchestrator.py:L1742-L1850` |
| AC-09 正式发布指针 | `StrategyRun.published_at` + `StockFeatureSnapshotRun.published_at`；API 查询 `published_at IS NOT NULL` | 已实现并核验 | `strategy_run.py:L164-L168`；`stock_context.py:L112` |
| AC-10 两阶段发布 | `publish_run`（阶段 1）→ `finish_snapshot_run`（阶段 2），独立 session，失败回滚 snapshot run | 已实现并核验 | `after_close_orchestrator.py:L1743-L1842` |
| AC-11 幂等与补跑 | create 去重；publish_run 幂等；execute 断点恢复；`retry_after_close_run` 支持重试 | 已实现并核验 | `after_close_orchestrator.py` / `strategy_batch_service.py:L1092-L1154` |
| AC-12 跨 Worker 领取 | `_after_close_poll_once` FOR UPDATE SKIP LOCKED + lease_epoch fencing + `_current_lease_epoch` | 已实现并核验 | `worker.py:L1314-L1436`；`scheduler_job_run_recovery_service.py` |
| AC-13 完成状态 | `AfterCloseRunStatus` 枚举 + `SchedulerJobRun.status`；`StrategyRun.status` 含 partial_failed | 部分实现 | 无显式 "pending"/"partial" 字段，语义由 queued/partial_failed 表达 |
| AC-14 部分失败 | `StrategyRun` 记录 succeeded_count/failed_count/skipped_count；publish_run 拒绝 partial_failed | 已实现并核验 | `strategy_run.py:L152-L162`；`strategy_batch_service.py:L1130-L1135` |
| AC-15 旧触发路径清理 | `worker.py:L383` 注释明确删除 `_maybe_trigger_after_close_orchestrator`；grep 未再发现该符号 | 已实现并核验 | `worker.py:L383-L393` |

## 3. 主要入口

### Feature Snapshot 批处理实现

`feature_snapshot_service.compute_for_trade_date` 按 `batch_size` 通过 `MarketDataAggregationService.get_bars_batch` 为每批预读 1d 与 15m 的 point-in-time qfq bars，将 canonical frame 传入单股计算，并在批内完成计算后集中 upsert/flush。MDAS 批入口复用单股 `get_bars` 的 bars、复权和诊断合同，按标的返回结果或异常；同一 `AsyncSession` 下有界顺序执行。批结果包含 `batch_count`、`mdas_batch_read_count`，每批完成后发送 progress callback；整日期 commit/rollback 与 published 快照保护仍由原事务边界负责。

**主链批读（AC-16，2026-08-03）**：`compute_review_core_with_run_items`（盘后编排实际调用的主链，单股×阶段检查点）现也在每 claim 批内通过 MDAS `get_bars_batch` 一次预读 1d point-in-time qfq bars，将 canonical frame 与诊断 hash（`source_bar_hash`/`adj_factor_hash`）经 `primary_bars`/`primary_source_bar_hash`/`primary_adj_factor_hash` 传入单股计算；批读失败降级为逐股 `_fetch_bars_from_db`（不抛）。仍保持每股独立事务提交（AC-08 检查点不因批读改变）。返回新增 `batch_count`/`mdas_batch_read_count` 低基数 metrics。


| 类型 | 路径 | 符号 | 职责 |
|---|---|---|---|
| 自动触发 | `backend/app/worker.py` | `scheduled_bars_refresh` | bars_scheduler Worker 每日 16:00 创建 after_close run |
| 手动运行 | `backend/app/api/admin_after_close.py` | `create_after_close_run_endpoint` / `force_after_close_run_endpoint`（含 `restart_from="daily_ready"`）/ `retry_after_close_run_endpoint` | 管理员创建/重试/从DSA阶段重算 |
| readiness | `backend/app/services/after_close_orchestrator.py` | `execute_after_close_run` 中 checking_coverage | 仅日线覆盖率检查（Phase 5A：15m intraday 工具保留在 `BarsCoverageService` 但 after-close 不再调用） |
| Orchestrator | `backend/app/services/after_close_orchestrator.py` | `execute_after_close_run` | 阶段编排与断点恢复 |
| Worker | `backend/app/worker.py` | `run_after_close_orchestrator_worker` / `_after_close_poll_once` | 任务领取与执行 |
| 发布 | `backend/app/services/strategy_batch_service.py` | `publish_run` | StrategyRun 标记 published |
| Snapshot 完成 | `backend/app/services/feature_snapshot_service.py` | `finish_snapshot_run` | snapshot run 标记 succeeded 并写 published_at |

## 4. 调用链

```text
bars_scheduler (16:00) / admin API / 脚本
→ create_after_close_run(run_key 去重)
→ SchedulerJobRun queued
→ after_close_orchestrator_worker _after_close_poll_once (FOR UPDATE SKIP LOCKED + lease_epoch)
→ execute_after_close_run
  → refreshing_daily (BarsSchedulerService.refresh_all_instruments)
  → syncing_boards (board_sync_service，软失败)
  → checking_coverage (daily >= 0.9，Phase 5A 移除 15m 阻塞)
  → computing_features (create_batch_run → DSA StrategyRun → Worker claim → 结果写入)
  → publishing (publish_run 阶段1 → finish_snapshot_run 阶段2)
  → succeeded
→ state_event_service.generate_events_for_run (事件生成，失败不影响主流程)
```

## 5. 状态机

SchedulerJobRun 状态：

```text
queued → running → succeeded/failed
running → interrupted (watchdog 检测 lease/heartbeat 超时)
interrupted → resume_queued (auto_resume_interrupted_after_close_runs)
resume_queued → running (Worker 领取，lease_epoch + attempt_no 递增)
```

AfterCloseRunStatus（metadata 层级）：

```text
queued → refreshing_daily → syncing_boards → checking_coverage → computing_features → publishing → succeeded
any → failed
```

StrategyRun 状态：

```text
queued → running → completed/partial_failed/failed
completed → published
```

## 6. 数据和状态

| 对象 | 权威存储 | 创建者 | 更新者 | 消费者 |
|---|---|---|---|---|
| after_close run | `scheduler_job_runs`（job_name="after_close_orchestrator"） | `create_after_close_run` | Orchestrator/Worker | admin API / pipeline service |
| DSA run | `strategy_runs` | `StrategyBatchService.create_batch_run` | Worker / publish_run | after_close_orchestrator / API |
| 子任务 | `strategy_run_items` | create_batch_run | Worker | batch service |
| 结果 | `strategy_results` | Worker | - | publish / API |
| snapshot run | `stock_feature_snapshot_run` | feature_snapshot_service | finish_snapshot_run | watchlist / stock_context |
| published_run_id | `strategy_runs.published_at` + `stock_feature_snapshot_run.published_at` | publish_run / finish_snapshot_run | - | API/前端/选股 |

## 7. 已知风险

- ~~P1：AC-04 与实现冲突~~ **[Phase 5A 已关闭并核验]** `checking_coverage` 已移除 15m 覆盖率检查（`intraday_result["ready"]`），仅保留日线覆盖率 >= 0.9。符合 PRD30 AC-04。测试证据：`test_after_close_orchestrator.py::test_ac04_daily_ready_15m_missing_allows_proceed` / `::test_ac04_daily_missing_blocks` / `::test_ac04_no_intraday_readiness_in_after_close_source`。
- ~~P0：本地调试误触正式发布风险~~ **[Phase 5A 已关闭并核验]** 所有 Redis 入口（`backend/app/core/redis_client.py`、`backend/app/db.py`、Worker、admin API）统一经过 `app.config.get_settings()`；development 环境下 `_validate_redis_url` 拒绝 DB0，`_resolve_redis_url` 缺失 REDIS_URL 抛 `MissingRequiredSettingError`，无 `localhost:6379/0` 默认回退。测试证据：`backend/tests/test_config_validation.py`（DB0 拒绝 / 隐式 DB0 拒绝 / DB15 通过 / 默认 localhost 拒绝 / 缺 URL 拒绝）。
- P1：子任务跨 Worker re-claim 在 StrategyBatch Worker 中通过 `FOR UPDATE SKIP LOCKED` + `lease_expires_at` 实现，但具体超时/重试策略需进一步核验。
- P2：AC-13 中 "pending" / "partial" 字段未显式存在，使用 queued / partial_failed 表达，文档和 API 消费者需注意语义映射。
- **P0（生产诊断 2026-07-28，CHANGE-20260728-005）**：远程开发运行服务器（GIT_SHA=37c9fa3）2026-07-27 和 2026-07-28 两次盘后 run 失败，根因有二：
  1. `compute_for_trade_date() got an unexpected keyword argument 'dsa_run_id'`（2026-07-27 16:00 run）。origin/main `37c9fa3` 已修复（PR #94），dev 未包含此修复，本轮规则禁止 merge/rebase。
  2. DSA StrategyRun 卡在 `running` 状态，`succeeded_count=0, failed_count=0`，feature snapshot 计算成功（`snapshot_count=5293`）但 `publish_run` 拒绝发布（要求 `completed`）。涉及 `after_close_orchestrator.py:L1735` 和 `strategy_batch_service.py:L1132` 的状态转换逻辑，需后续排查。
- **问财软失败语义**：生产 `BOARD_SYNC_ENABLED=true` 时问财为硬依赖，但 2026-07-27/28 三次运行均 `board_sync_result.status=succeeded`（raw=5542, resolved=5287, 行业=257, 概念=388），失败发生在 DSA 计算和发布步骤，与问财无关。

## 8. 验证入口

- 单股：未运行核验；
- 指定股票池：`force_after_close_run_endpoint` 支持 `restart_from="daily_ready"` + 指定 symbols（从 DSA 阶段重算，替代原 dsa-only 端点）；
- 全市场：默认 after_close_orchestrator 全市场；
- 重复执行：`create_after_close_run` run_key 去重 + `publish_run` 幂等；
- Worker 超时和重领：`recover_stale_scheduler_job_runs` + `auto_resume_interrupted_after_close_runs`；
- 部分失败：`StrategyRun.failed_count` / `partial_failed`；
- 发布前后 API 读取：`stock_context.py` 按 `published_at IS NOT NULL` 过滤；
- 远程交易日自动运行：远程 bars_scheduler Worker 已运行（Phase 3 远程只读审计）。

## 9. 更新触发条件

任何 Scheduler、任务阶段、状态、Worker、run、发布或补跑变化都必须更新本 Map。

## 10. Phase 5B-2 影响说明

**核验状态：未核验（Phase 5B-2）**

Phase 5B-2 的 PRD60 PA-01 capability 模型变化（`user_capabilities` 表、`require_capability`、前端 `CapabilityRoute`）不影响盘后链路：

- after-close orchestrator / Worker / admin API 路由保持不变；
- capability 检查不施加于 after-close 编排与发布路径（admin 豁免，且 after-close 走 admin API 或 scheduler 触发）；
- 本轮未对盘后链路做运行时核验，仅静态确认路由与依赖未改动。

如后续对 after-close admin API 增加 capability 守卫，需重新核验并更新本节。

## 11. 增量检查点与分层发布（CHANGE-20260729-006）

**核验状态：未运行核验（2026-07-29 新增）**

本轮引入"单股×阶段"为最小计算/事务/检查点粒度，新增 4 张表和 2 个服务。**未集成到现有 Worker**，本轮只提供 service 层，现有 Worker 保持 legacy 模式。

### 11.1 新增表（migration 073）

| 表 | 唯一键 | 职责 |
|---|---|---|
| `stock_feature_snapshot_run_items` | `(snapshot_run_id, instrument_id, phase)` | 单股×阶段检查点 |
| `first_pyramid_history_runs` | `id` | 历史回补 run 级追踪 |
| `first_pyramid_history_run_items` | `(history_run_id, instrument_id)` | 历史回补单股 item |
| `factor_publications` | `(scope_type, scope_key, trade_date, publication_kind)` | 分层发布指针 |

### 11.2 ID 合同统一

| ID | 含义 | 写入位置 |
|---|---|---|
| `orchestrator_job_run_id` | `SchedulerJobRun.id`（任务追踪） | `FirstPyramidHistoryRun.scheduler_job_run_id`（nullable metadata） |
| `snapshot_run_id` | `StockFeatureSnapshotRun.id`（当日核心数据版本） | `RunItem.snapshot_run_id` / `StockChipConsensusSnapshot.core_run_id` / `FactorPublication.data_run_id` |
| `history_run_id` | `FirstPyramidHistoryRun.id`（历史回补版本） | `FactorPublication.data_run_id`（kind=history_cross_section） |

**关键变更**：`after_close_orchestrator.py` 中 `create_after_close_chip_consensus_job(core_run_id=snapshot_run_id)` 不再传 `job_run_id`。

**[CHANGE-20260729-007 ID 合同修复]**：
- 071 migration FK 已修正：`stock_chip_consensus_snapshots.core_run_id` FK 从 `scheduler_job_runs.id` → `stock_feature_snapshot_runs.id`（与 orchestrator 传入值一致）
- 073 migration `factor_publications.trade_date` 已改为 NOT NULL（避免普通唯一约束允许多 NULL 产生重复 pointer）
- ORM 同步：`StockChipConsensusSnapshot.core_run_id` / `FactorPublication.trade_date`

### 11.3 新增服务

| 服务 | 模块 | 核心函数 |
|---|---|---|
| Run Item | `app.services.snapshot_run_item_service` | `create_run_items` / `claim_items`（UPDATE...RETURNING + FOR UPDATE SKIP LOCKED）/ `mark_item_succeeded/failed/skipped`（lease_epoch fencing）/ `get_run_progress` / `get_resume_items` / `recover_stale_running_items` |
| 分层发布 | `app.services.factor_publication_service` | `compute_coverage` / `publish_stock_core`（门禁 0.98 + on_conflict_do_update 原子切换）/ `publish_market_aggregation`（**[007]** 严格校验 source_core_run_id 匹配已发布 stock_core pointer）/ `publish_history_cross_section`（**[007]** coverage 由 `compute_history_coverage` 从 DB 统计）/ `get_publication` / `get_published_snapshot_run_id`（无 pointer 回退 published_at）/ `is_stale_snapshot`（**[007]** 真源改为 `bars_daily.max(trade_date)`） |

### 11.4 关键设计

1. **claim 原子性**：`UPDATE ... WHERE status IN ('pending','failed','running'+lease过期) ... FOR UPDATE SKIP LOCKED RETURNING`
2. **lease_epoch fencing**：`mark_item_*` 支持 `lease_epoch` 参数，旧 Worker 写入被拒绝
3. **coverage 门禁**：`CORE_PUBLICATION_MIN_COVERAGE = 0.98`，低于抛 `CoverageBelowThresholdError`
4. **原子指针切换**：`pg_insert(...).on_conflict_do_update(constraint="uq_factor_publications_scope_date_kind")`
5. **兼容回退**：`get_published_snapshot_run_id` 优先读 publication pointer，无 pointer 时回退 `published_at IS NOT NULL`
6. **[007] 读取端接入 pointer**：`stock_context.py` 的 `_find_latest_succeeded_run` / `_find_run_by_trade_date` 优先读 `factor_publications`（stock_core kind），无 pointer 时回退 `published_at IS NOT NULL`

### 11.5 当前限制（CHANGE-008 后状态）

**[CHANGE-20260729-008 代码闭环已完成的项]**：

- ✅ **Worker 已接入 run item**：`after_close_orchestrator` 主链切换到 `feature_snapshot_service.compute_review_core_with_run_items`，调用 `create_run_items` / `claim_items` / `mark_item_succeeded` / `mark_item_failed`，单股独立 AsyncSession + lease_epoch fencing
- ✅ **market_stocks LATERAL 已接入 pointer**：`_build_snap_lateral(snapshot_run_id=...)` 严格过滤 `source_run_id == pointer.data_run_id`；`get_market_stocks` 先读 publication pointer 再构建 LATERAL；无 pointer 时回退每股 latest（兼容历史数据）
- ✅ **历史回补已接入 run/item**：`backfill_history_with_run_items` + `create_history_run` / `claim_history_items` / `mark_history_item_*` / `finish_history_run`，单股独立事务，DB-only 取数
- ✅ **管理状态 API 已实现**：`app.api.admin_incremental_publish`，提供 `/status` / `/core/runs` / `/core/runs/{id}/progress` / `/history/runs` / `/history/runs/{id}/progress` / `/pointers`
- ✅ **历史回补 CLI 已实现**：`scripts/first_pyramid_history_backfill_cli.py`，支持 `--canary` / `--limit` / `--all` / `--symbols` / `--resume` / `--dry-run` / `--output-bars` / `--algorithm-version`
- ~~市场聚合独立 job：`market_factor_aggregation_service.run_market_factor_aggregation`~~（Historical/Non-Normative：该 service 已于 [Slice 4A10] 删除，见 70-review.md §0；`market_aggregation` pointer 当前来源见 granular restart / orchestrator）
- ✅ **事件 outbox 模型支持**：`StockFeatureSnapshotRunItem.phase='event_outbox'` 已定义，实际事件写入由 `stock_state_event` 表（稳定唯一键幂等）承载

**[本轮仍待验证的项]**：

- PG 集成测试 6 项待 CI（`PURE_UNIT_TEST=1` 时 SKIP，需 CI 临时 PG 容器）
- 远程开发部署后真实 canary 验证待执行
- 全市场 history 回补待执行

详见 `docs/changes/2026/CHANGE-20260729-008-incremental-publish-full-closure.md`。

### 11.6 History 版本一致性审计结论（CHANGE-20260729-009）

**核验状态：已基于共享开发业务数据库只读审计确认（2026-07-29）**

| 表 | 字段 | 实际值 | 期望值 | 结论 |
|---|---|---|---|---|
| `first_pyramid_history_runs` | `algorithm_version` | `1.0.0-core-split`（所有 run） | `1.0.0-core-split` | 一致 |
| `first_pyramid_history_daily_state` | `algorithm_version` | `1.0.0-core-split`（1289176 行） | `1.0.0-core-split` | 一致 |
| `factor_publications`（kind=history_cross_section） | `algorithm_version` | `1.0.0-core-split` | `1.0.0-core-split` | 一致 |

数据完整性：

- 5184 只股票数据完整，5118 只正好 250 行，66 只新股 <250 行（标记 `insufficient_history`，不算 failed）
- `trend_ready` / `structure_ready` = 100%，`momentum_ready` = 99.79%
- 000021 已有 250 行，无版本不一致

**结论**：本轮无需执行 History repair run，所有版本一致。已发布 pointer `5e222b38` 与最新 Core `a546defb` 匹配，无需切换。

### 11.7 15m 门槛澄清（CHANGE-20260729-009）

盘后 core 的 coverage 门禁已移除 15m，仅表示 stock_core/review core 不等待 15m；筹码共识仍消费 `bars_15min`。`after_close_chip_consensus_service` 在每只股票计算前调用 `refresh_15min_bars(count=4000)`，再由 MDAS 读取 canonical QFQ bars，并校验目标交易日 16 根、最后一根到 15:00、历史最少 500 根；刷新失败、日期陈旧、时段不完整和历史不足均写结构化 skipped，不反改 core。

`first_pyramid_service.py` 中两个 15m 门槛的权威定义：

| 常量 | 值 | 用途 | 使用位置 |
|---|---|---|---|
| `_CHIP_MIN_15M_BARS` | `500` | 批量服务最低门槛（degraded） | `after_close_chip_consensus_service` 批量计算 |
| `NODE_CLUSTER_LOW_BARS` | `4000` | Node Cluster 完整质量门槛（250日×16根/日） | 个股详情实时计算 `compute_chip_status_for_stock` |

- 000021 15m bars=354，不满足 500 最低门槛 → `chip_status=skipped + reason_code=M15_BARS_INSUFFICIENT + actual_bars=354 + required_bars=500`

**目标差距（2026-08-06）**：当前实现仍在逐股计算前调用 `refresh_15min_bars`。PRD31 PC-20 与 PRD30 AC-18 要求改成“运行级有界 refresh → 冻结 cutoff/readiness → MDAS 批读 → 逐股计算”，因此该项不得标记为闭环。`15m` 必须继续更新，差距是刷新编排与批读方式，不是删除该周期。
- 错误消息明确两个门槛的用途，避免混淆
- 不修改门槛值，只统一文档与文案

### 11.8 板块分析 V1（CHANGE-20260730-011）

**核验状态：已基于代码核验（2026-07-30）**

#### 数据模型

- 新增表 `board_analysis_snapshots`（migration `074_board_analysis_v1`）
- 单表设计：每条记录既是 run 又是 snapshot，含 `status/started_at/finished_at` 字段
- 唯一键 `(trade_date, board_id, algorithm_version)` 保证幂等
- 复用 `factor_publications` 表发布指针：`publication_kind=market_aggregation`、`scope_type=board`、`scope_key=board_id::text`、`data_run_id=board_analysis_snapshot.id`

#### 关键代码入口

- ORM：`backend/app/models/board_analysis_snapshot.py`
- Schema：`backend/app/schemas/board_analysis.py`（BoardAnalysisSnapshotDTO / ListResponse / DetailResponse）
- Service：`backend/app/services/board_analysis_service.py`
  - 常量：`BOARD_ANALYSIS_ALGORITHM_VERSION="board-v1-20260730"` / `BOARD_ANALYSIS_MIN_COVERAGE=0.95`
  - 纯函数：`compute_board_payload(flat_list)` 计算分布指标
  - 入口：`compute_board_analysis(session, board_id, trade_date, ...)` 单板块计算
  - 批量：`compute_all_boards(session, trade_date, ...)` 行业+概念批量
  - 发布：`publish_board_analysis(session, snapshot)` 写入 factor_publications 指针
  - 查询：`list_board_analyses` / `get_board_analysis_detail` / `compute_is_stale` / `check_is_published`
- API：`backend/app/api/board_analysis.py`
  - 用户路由 `GET /api/v1/boards/analysis` 列表分页
  - 用户路由 `GET /api/v1/boards/{board_id}/analysis` 单板块详情
  - 管理路由 `POST /api/v1/admin/boards/{board_id}/analysis/compute` 触发单板块
  - 管理路由 `POST /api/v1/admin/boards/analysis/compute-all` 批量触发（canary/全量）
- CLI：`backend/scripts/board_analysis_cli.py`（`--canary` / `--all` / `--type` / `--limit` / `--trade-date` / `--publish` / `--no-publish` / `--dry-run`）

#### 输入门禁（严格遵循 PRD §6 板块分析 V1）

1. 必须存在已发布 `stock_core` pointer（否则拒绝计算）
2. `source_core_run_id = factor_publications.data_run_id`（kind=stock_core）
3. 从 `summary_payload.first_pyramid_flat` 提取 99 个 fp_ 字段
4. 退市股（`Instrument.status != 'active'`）不参与聚合，不进入 eligible_count/missing_count
5. 行业与概念分开计算，成员和股票因子必须同一 trade_date，禁止使用未来数据

#### 指标 payload（7 大维度）

- `trend_dist`：up/down/neutral 计数
- `trend_strength`：avg/p25/p50/p75
- `vwap_dev_pct`：avg/p25/p50/p75
- `structure`：swing/alignment 分布 + avg_active_ob_count
- `structure_events`：BOS/CHoCH/OB/EQH/EQL 计数与事件率（rate = 有事件数/ready_members）
- `momentum`：positive/negative/neutral + squeeze/released/normal + enhancing/fading/flat + avg_sqzmom
- `volume`：high/low/normal/unknown + avg_volume_ratio20/200 + 20/200 分位分布
- `total_members` / `ready_members` / `missing_members`：成员汇总

#### 发布门禁

- `coverage_ratio = ready_count / eligible_count`（eligible = active 股票数，含数据不足）
- `coverage_ratio >= 0.95` 才写入 `factor_publications` 指针
- 不足时保存 `partial` 结果但不切 pointer（可重复计算，幂等）

#### 前端入口

- 路由 `/boards`（列表）+ `/boards/:boardId`（详情）：`frontend/src/pages/BoardAnalysisPage.tsx`
- API 客户端：`frontend/src/api/endpoints.ts` 中的 `getBoardAnalysisList` / `getBoardAnalysisDetail` / `triggerComputeBoard` / `triggerComputeAllBoards`
- Hooks：`useBoardAnalysisList` / `useBoardAnalysisDetail` / `useTriggerComputeBoard` / `useTriggerComputeAllBoards`
- Admin 可触发 Canary（每类型 5 个）和全量计算
- 列表显示覆盖率徽标、状态、过期、已发布
- 详情显示 4 维分布（趋势/结构/动量/量能）+ 结构事件率 + payload JSON

## 12. 盘后任务中断恢复机制（CHANGE-20260730-014）

**核验状态：代码已合入 main (SHA 54fe3a2)；review run timeline API 与 SIGTERM drain 已落代码，待生产 SSH 可达后核验**

### 12.1 顶层 run heartbeat（30s fenced UPDATE）

- `SchedulerJobRun` / `market_review_runs` 在 `running` 状态下，worker 每 30 秒执行一次 fenced UPDATE 刷新 `heartbeat_at`：
  ```sql
  UPDATE scheduler_job_runs
  SET heartbeat_at = NOW()
  WHERE id = :run_id
    AND lease_epoch = :current_lease_epoch
  RETURNING id;
  ```
- `lease_epoch` fencing：仅持有当前 `lease_epoch` 的 worker 才能成功刷新 heartbeat；旧 worker（已被 re-claim 抢占）的 UPDATE 影响 0 行，立即退出。
- `after_close_chip_consensus` 使用共享 `fenced_job_run_service` 每约 30 秒刷新 heartbeat 和 lease；刷新、snapshot 写入与终态写入统一匹配 job id、`status=running`、worker instance、`lease_epoch`。
- scheduler watchdog 使用 90 秒 heartbeat 健康阈值，但只有 `lease_expires_at` 已过期且 heartbeat 同时不健康时才把 run 标记为 `interrupted`，不会接管仍有健康 heartbeat 的长任务。

### 12.2 item lease（14400s）+ fencing_epoch

- 每个 `market_review_run_items` / `stock_feature_snapshot_run_items` 持有独立 lease：
  - `lease_expires_at = NOW() + INTERVAL '14400 seconds'`（4 小时，覆盖单 scope 最长计算时间）；
  - `lease_epoch` 在每次 `claim_items` 时递增；
  - `mark_item_succeeded` / `mark_item_failed` / `mark_item_skipped` 必须携带 `lease_epoch`，旧 worker 写入被拒绝。
- 超过 `lease_expires_at` 的 `running` item 可被下一个 worker `claim_items` 抢占，`lease_epoch` 递增；旧 worker 后续 `mark_item_*` 因 epoch 不匹配被静默拒绝。
- `recover_stale_running_items`（位于 `snapshot_run_item_service`）扫描 `lease_expires_at < NOW()` 的 running items，重置为 `pending`；watchdog **当前不调用** 该函数（已知缺口，见 §12.7）。

### 12.3 watchdog 恢复同一 run（最多 3 次）

- `auto_resume_interrupted_after_close_runs`（位于 `scheduler_job_run_recovery_service`）扫描 `status=interrupted` 的 after_close run：
  - 检查 `attempt_no`（metadata.heartbeat_recovery_count）；
  - `attempt_no < 3` 时将 run 状态从 `interrupted` 切换为 `resume_queued`，等待 worker 领取；
  - `attempt_no >= 3` 时不再自动恢复，标记为 `failed` 并通知 admin。
- review orchestrator 使用相同模式：`market_review_runs.status=interrupted` 时由 watchdog 切换为 `resume_queued`，最多 3 次。
- 恢复后 worker 重新领取 run，递增 `lease_epoch` 和 `attempt_no`，按 last_completed_step 断点恢复。

### 12.4 SIGTERM drain

worker 收到 SIGTERM 信号时的 drain 流程：

1. **停止领取新 item**：worker 立即停止 `_after_close_poll_once` / `claim_items` 调用，不再领取新的 run 或 item；
2. **完成当前 run 内正在执行的 item**：当前 item 继续执行直至 `mark_item_succeeded` / `mark_item_failed`，但不会超过 SIGTERM 后的 grace period；
3. **刷新 run heartbeat 一次**：drain 完成后刷新最后一次 heartbeat，便于 watchdog 判断 run 是否需要恢复；
4. **优雅退出**：worker 进程退出码 0；run 状态保持 `running`（heartbeat 未续期后由 watchdog 切换为 `interrupted` → `resume_queued`）。

实现入口：

- `backend/app/worker.py`：SIGTERM 信号处理器 + drain 标志；
- `backend/app/services/after_close_orchestrator_service.py`：drain 标志检查点嵌入到主循环；
- `backend/app/services/review_orchestrator_service.py`：review worker 共用同一 drain 模式。

### 12.5 deploy.sh drain_after_close_worker()

部署脚本 `deploy.sh` 在重启 worker 容器前调用 `drain_after_close_worker()`：

1. 检查当前是否有活跃的 after_close 或 review run（`status IN ('queued', 'running', 'resume_queued')`）；
2. **有活跃 run**：
   - 向 worker 容器发送 SIGTERM（`docker kill -s TERM trading-worker`）；
   - 等待 run 完成（最长 30 分钟，每 30 秒轮询一次 `status`）；
   - run 完成或 watchdog 切换为 `interrupted` 后，允许重启容器；
   - 超时未完成则**拒绝重启**，输出诊断信息（run_id、当前 step、heartbeat_at），由 admin 手工决策。
3. **无活跃 run**：直接重启容器。
4. 重启后由 watchdog 自动 resume `interrupted` 的 run（见 §12.3）。

### 12.6 admin timeline API

新增 `GET /api/v1/admin/review/runs/{run_id}/timeline`，用于诊断 review run 的执行时间线：

返回结构（按时间排序的事件列表）：

```json
{
  "run_id": "uuid",
  "trade_date": "2026-07-29",
  "algorithm_version": "review-1.1.0",
  "status": "published",
  "events": [
    {
      "event_id": "uuid",
      "event_type": "run_created | run_started | scope_item_claimed | scope_item_succeeded | scope_item_failed | signal_emitted | publish_attempted | publish_succeeded | heartbeat_timeout | watchdog_resumed | sigterm_drain_started | sigterm_drain_completed",
      "scope_type": "market | major_index | style | industry_l1 | null",
      "scope_key": "string | null",
      "phase": "metrics | signals | attribution | tracking | null",
      "lease_epoch": 1,
      "attempt_no": 0,
      "occurred_at": "ISO8601",
      "metadata": {}
    }
  ]
}
```

实现入口：

- 路由：`backend/app/api/admin_review.py`
- 服务：`backend/app/services/review_orchestrator_service.py:get_run_timeline`
- 权限：`require_admin`
- 用途：诊断 heartbeat 超时、lease 抢占、watchdog 恢复、SIGTERM drain、scope item 失败模式等。

### 12.7 已知缺口

- **docker `stop_grace_period` 未配置**：`docker-compose.yml` 中 worker 容器未设置 `stop_grace_period`，默认 10 秒后 docker 发送 SIGKILL；SIGTERM drain 流程在 10 秒内无法完成长 item，导致 item 被强制中断。建议在 `docker-compose.yml` 中为 `trading-worker` 设置 `stop_grace_period: 1800s`（30 分钟，覆盖单 scope 最长计算时间）。
- **watchdog 不调用 `recover_stale_running_items`**：`auto_resume_interrupted_after_close_runs` 在恢复 `interrupted` run 时只重置 run 级状态，不调用 `snapshot_run_item_service.recover_stale_running_items`；若 run 恢复时仍有 `running` 状态的 item（lease 未过期但 worker 已死），这些 item 会卡在 `running` 直到 lease 过期。建议在 watchdog 恢复 run 时同步调用 `recover_stale_running_items(run_id)`。
- **review run 与 after_close run 的 watchdog 共用同一恢复服务**：当前 `auto_resume_interrupted_after_close_runs` 只扫描 `job_name=after_close_orchestrator` 的 run；review run 的 watchdog 恢复由独立的 `review_orchestrator_service` 实现，未来可能需要统一为通用 `run_recovery_service`，避免两套恢复逻辑。

## 13. 盘后阶段依赖与发布闭环实现（2026-07-30 核验）

**核验状态：已基于代码核验（2026-07-30）**

对应 PRD：`../prd/30-after-close.md` §6（AC-17 / AC-18 / AC-19）

### 13.1 after_close_orchestrator publishing 阶段调用 publish_stock_core

- `after_close_orchestrator.execute_after_close_run` 的 publishing 阶段在 snapshot run `succeeded` 后，显式调用 `factor_publication_service.publish_stock_core` 切换 `stock_core` publication pointer；
- 实现位置：`backend/app/services/after_close_orchestrator.py:L1850-L1929`；
- 流程：
  1. `get_publication` 读取已有 pointer；
  2. 已有 pointer 的 `data_run_id == snapshot_run_id` → 幂等复用，记录 info；
  3. 已有 pointer 的 `data_run_id != snapshot_run_id` → 不覆盖，记录 warning + event；
  4. 无 pointer → `compute_coverage` + `publish_stock_core`（`FIRST_PYRAMID_CORE_ALGORITHM_VERSION`），`CoverageBelowThresholdError` 时记录 error + event；
- pointer 写入成功后才写 publishing checkpoint；
- 调用使用独立 `AsyncSessionLocal()`（`pub_db`），与主 run session 隔离。

### 13.2 dsa_recovery_service.py（失败 DSA 恢复）

- 模块：`backend/app/services/dsa_recovery_service.py`；
- 公开函数：`recover_failed_dsa_run(db, job_run_id, *, strategy_key="dsa_selector", run_type="scheduled") -> tuple[StrategyRun, bool]`；
- 行为：
  - DSA run 为 `completed`/`published` → 直接复用，返回 `(run, False)`；
  - DSA run 为 `running` 且 lease 未过期 → 拒绝恢复；
  - DSA run 为 `failed`/`partial_failed` → 通过 `create_batch_run` 创建新 run（自动递增 `attempt_no`），原子更新 orchestrator metadata 的 `dsa_run_id`，返回 `(new_run, True)`；
  - 恢复次数上限 `_MAX_DSA_RECOVERY_COUNT = 5`，超过抛 `DSARecoveryError`；
- 约束：原失败 run 保留审计，禁止直接改回 `queued`；管理 API/CLI 只能调用该 service，禁止裸 SQL；
- 测试：`backend/tests/test_dsa_recovery_service.py`（10+ 用例覆盖复用/创建/lease 拒绝/超上限等）；
- **CLI / admin API 尚未实现**：当前需通过 service 调用，待新增 `backend/scripts/dsa_recovery_cli.py` 或 admin 端点。

### 13.3 worker.py chip_consensus 分支

- 模块：`backend/app/worker.py::_chip_consensus_poll_once`（L1529-L1620）；
- 行为：
  - 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取 `job_name='after_close_chip_consensus'` 且 `status IN ('queued', 'resume_queued')` 的任务；
  - 领取后更新 `status='running'` + `worker_instance_id` + `heartbeat_at` + `lease_expires_at` + `lease_epoch`（fencing）；
  - 调用 `execute_after_close_chip_consensus`（含断点续算）；
  - 断点续算：`get_pending_chip_instruments` 过滤已 `succeeded` 和合法 `skipped` 的 instrument，`resume_queued` 只重试 pending/failed/真正失租项；
  - 执行中由 `fenced_job_run_service.FencedJobHeartbeat` 每 30 秒续租，所有 snapshot upsert 在同一事务内先锁定并验证当前 job owner；
  - 终态由 `finalize_job_run` fenced 写入 `finished_at`、计数、结构化原因并释放 lease；全成功/部分成功/全 skipped/系统性失败分别形成 `succeeded/succeeded`、`succeeded/partial`、`succeeded/skipped`、`failed/failed`；
  - heartbeat、成功、失败、取消和失租路径均清理后台 heartbeat task；失租 worker 不执行 auction anchor 回调；
- **不新增常驻容器**：chip_consensus worker 在现有 after-close worker 容器内通过 `WORKER_TYPE` 分支执行；
- watchdog：`auto_resume_interrupted_after_close_runs`（`scheduler_job_run_recovery_service.py:L169`）同时扫描 `after_close_orchestrator` 和 `after_close_chip_consensus` 两类 `interrupted` 任务，最多恢复 3 次；
- chip 入队：`after_close_orchestrator.py` 的正式步骤 `enqueue_chip_job`（`_enqueue_chip_job_step`）在**主任务终态提交之前**调用 `create_after_close_chip_consensus_job`（只入队，不 await chip 计算，chip 由独立 Worker 异步执行）。入队失败计入 `partial_success` 判定，metadata 记录 `chip_enqueue_status / chip_job_id`；chip.core_run_id 指向 `snapshot_run_id`（数据版本）。

### 13.4 聚合依赖闭环：stock_core pointer → board aggregation（Historical/Non-Normative：legacy V1 链路，Board 非当前 Review 前置）

- `market_factor_aggregation_service.run_market_factor_aggregation`（`backend/app/services/market_factor_aggregation_service.py:L33`）：
  - 步骤 1 读取已发布 `stock_core` pointer（`get_publication`），无 pointer 抛 `ValueError`；
  - 步骤 2 校验 `source_core_run_id` 等于 `stock_core` pointer 的 `data_run_id`；
  - 步骤 3 调用 `publish_market_aggregation` 切换 `market_aggregation` pointer（含 source 校验）；
- `board_analysis_service` 输入门禁：必须存在已发布 `stock_core` pointer，否则拒绝计算（PRD §5 BA-01）；
- 聚合失败只重跑聚合，不影响已发布 `stock_core`；
- 依赖顺序：`stock_core published` → `market_aggregation` / `board_analysis` 可触发 → `review` 可触发；
- `after_close_orchestrator` 当前止于 stock_core + chip_consensus 创建，**market aggregation 和 board_analysis 不在主编排内自动触发**，需通过 CLI / admin API 单独触发（见 `docs/runbooks/after-close-remote-development-run.md` §9）。

### 13.5 统一步骤执行器 / watchdog / reconcile（Phase 0 收口，2026-08-03 核验）

统一步骤执行器 `execute_orchestrator_step`（`after_close_orchestrator.py:L116`）：
- 参数：`timeout_seconds / optional / heartbeat / progress / cancellation_check / attempt / retry_count / poll_interval`；返回 `(result, summary)`。
- **唯一周期循环 `_tick_loop`**：每 `_HEARTBEAT_INTERVAL_SECONDS`(10s) 刷新 `summary["elapsed_seconds"] / heartbeat_at / last_progress_at` 并调用 `progress`，使 watchdog 能实时判定 `step_timed_out`（`running + elapsed_seconds > timeout`），而非仅"结束后诊断"。
- **heartbeat 为单次 touch**：`_make_step_heartbeat` 构造单次 `touch_job_run_heartbeat`（fenced UPDATE，检查 lease_epoch + status='running'）；执行器不再把无限循环 `_job_run_heartbeat_loop` 当作回调传入。
- **运行中取消**：`_run_with_cancellation` 把 operation 建为独立 task，周期调用 `cancellation_check`，命中时 `op_task.cancel()` + `await` 终止业务协程；`_StepCancelledError` 转 `cancelled` summary 不炸穿 Worker。
- 步骤终态集合：`{succeeded, skipped, unavailable, failed, timed_out, cancelled, interrupted}`；非可选步骤超时/异常会 `raise`，可选步骤降级不抛。

顶层步骤（经执行器）顺序：`refreshing_daily → syncing_boards → checking_coverage → computing_features → publishing → auction_anchor(可选) → computing_review → enqueue_chip_job(可选)`。

**`computing_review`（AC-02，2026-08-03 收口）**：复盘业务体抽为模块级协程 `_execute_review_step(...)`，由 `execute_orchestrator_step("computing_review", lambda: _execute_review_step(...), optional=True, ...)` 包装，满足 AC-02「所有顶层步骤必须通过统一步骤执行器」。`_execute_review_step` 内部保留既有幂等 create_run / compute_run / resume_run / publish_run 语义与 publication pointer 唯一事实源，软失败（gate_blocked/计算失败）不抛异常，仅返回 `result["failed"]=True`；调用方将业务软失败如实映射到 step summary（`REVIEW_SOFT_FAILURE`）并 `_persist_step_summary`，并据此把主任务收为 `partial_success`（core 已发布）。检查点语义不变：失败时 `_execute_review_step` 内部传 `None` 不推进 `last_completed_step`（见 §12.1）。

**`watchlist_ready`（非执行器步骤）**：是**派生就绪指示器**而非可执行工作步骤——无 operation、无 timeout/heartbeat/cancellation，由 `feature_snapshot_service.has_succeeded_snapshot_run`（succeeded + published + full scope）推导，供 admin 流水线可视化渲染为终态展示步骤（`after_close_pipeline_service._PIPELINE_STEPS` 含 `"watchlist_ready"`）。强制塞进 `execute_orchestrator_step` 会造出空 operation，违反最小必要修改原则；此处如实标注：`watchlist_ready` 不经过统一执行器。

syncing_boards 软失败：`_execute_syncing_boards` 返回业务 `{status}`（succeeded/skipped/failed），执行器外层将业务 failed/skipped 如实映射到 step summary 并 `_persist_step_summary`，避免"业务 failed / 步骤 succeeded"矛盾。

watchdog / 状态查询（`get_after_close_run_status`）：
- `heartbeat_stale`（> `_HEARTBEAT_STALE_SECONDS`=60）+ 步骤级 `step_timed_out` 合并为 `stale`；
- 暴露 `step_summary / running_steps / step_timed_out / stale / partial_success`，API 完整透传（`AfterCloseRunStatusResponse`），管理后台可见真实 watchdog 字段。

cancel / reconcile（`cancel_after_close_run` / `reconcile_after_close_run`）：
- cancel：记录 `actor / request_id`，递增 `lease_epoch` fence 旧 Worker；
- reconcile：接入 `request_id`（metadata 写 `reconcile_request_id`）；running→interrupted 时写 `finished_at`、释放 `lease_expires_at`、递增 `lease_epoch`、把仍 running 的 step_summary 收敛为 interrupted；`_inspect_run_artifacts` 只读核验 `factor_publications` 表 `stock_core / market_aggregation` 真实 pointer，记录 `reconcile_artifacts / reconcile_contradictions`；reconcile 事件 payload 含 `actor / request_id / artifacts / contradictions / new_lease_epoch`。

Review 检查点：`_update_heartbeat_and_step` 的 `last_completed_step` 为 `str | None`，`None`=仅刷新心跳/租约、不推进检查点；Review 失败时传 `None`，避免下次 resume 跳过失败的 Review（详见 §12.1 检查点语义）。

管理后台两页（OPS-06，2026-08-03）：
- `AdminAfterClosePipelinePage`（`/admin/after-close`）为盘后流水线专用诊断页，承载四类操作（终止/对账/从此处续跑/完整强制重跑）与 7 步时间线 + watchlist_ready + 部分成功 + stale 警告。
- `AdminJobsPage`（`/admin/jobs`）为通用任务 + Worker 心跳监控页，不复制盘后四类操作（避免两页按钮语义分歧）；对 `after_close_orchestrator` 任务，任务详情抽屉新增「盘后详情（四类操作）」链接跳转 `/admin/after-close?tradeDate=<business_date>`，**携带被点击 run 的业务日期**，使专用页直接定位到该历史任务而非默认最新（CHANGE-20260804-001）；`AdminAfterClosePipelinePage` 通过 `useSearchParams` 读取 `tradeDate` 初始化 `selectedDate`，刷新/返回保持同一任务。通用页与专用页共享同一操作/状态事实源。

> 数据操作：以上为本地纯单元验证（PURE_UNIT_TEST=1），未部署、未连接共享库、未修改业务数据。

### 13.6 管理 API 统一错误合同（2026-08-04 收口）

管理后台所有错误响应统一经 `app.api.admin_errors.admin_error` 构造（唯一事实源），禁止端点在
`detail` 里手工拼多套字典：

- 统一稳定字段：`detail / message / error_code / severity / retryable / resumable / recommended_action / request_id`；
- `message` 恒等于 `detail`（兼容旧前端 `detail.message` 解析）；
- 业务上下文字段（`after_close_run_id / trade_date / started_at / heartbeat_at / last_completed_step / conflicting_run_id / daily_coverage / reason / threshold` 等）经 `**extra` 透传，不丢失；
- 便捷别名：`admin_conflict`(409) / `admin_not_found`(404) / `admin_bad_request`(400)。

`admin_after_close.py` 全部端点（create/force/resume/retry/cancel/reconcile/status/events）
已改用统一构造器；cancel/reconcile 的 404 错误透传 `request_id`。测试：`backend/tests/test_admin_errors.py`
（纯单元 8 项，覆盖稳定字段/extra 透传/request_id/状态码映射/源码不再手工 raise HTTPException）。

## 复盘 pointer 与 run 关系

**核验状态：待实现（复盘模块尚未开发）**
对应 PRD：`../prd/30-after-close.md` §复盘编排 + `../prd/70-review.md` §11

> 复盘发布链路尚未实现。以下为 PRD 定义的目标合同，当前 `after_close_orchestrator` 编排止于 board_analysis 发布，不包含 review 步骤。

### pointer 存储

`factor_publications` 表承载复盘发布指针：

| 字段 | 值 |
|---|---|
| `publication_kind` | `review` |
| `scope_type` | `market` |
| `scope_key` | `trade_date`（如 `2026-07-29`） |
| `data_run_id` | `market_review_run.id` |

### run 与输入 pointer 关系

- `market_review_run.source_core_run_id` 必须指向当前已发布的 `stock_core` pointer 的 `data_run_id`；
- `market_review_run.source_board_run_id`【Historical/Non-Normative：legacy lineage，非当前 Review 发布门禁】历史上指向 `market_aggregation`（board）pointer 的 `data_run_id`；当前 Review 不再以 `source_board_run_id` 为前置（见 70-review.md §0、review_publication_service 标注「Slice 4A5 Board-independent」）。
- 上述 `source_core_run_id` 必须为当前正式 pointer，不得指向已过期或被替换的旧 run。

### pointer 切换语义

- 切换 review pointer 是原子操作（`on_conflict_do_update`），不复制结果数据；
- 旧 review run 保留可查询，新 pointer 只切换读取入口；
- pointer 不得倒退到旧 run。

### 覆盖率门禁

- market 范围必须 ready 才能发布 review pointer；
- 发布前检查整套 Review 门禁（见 PRD §11.1）。

### 当前实现状态

- `after_close_orchestrator` 不包含 review 编排步骤；
- 无 `review_orchestrator` 服务；
- `factor_publications` 当前无 `publication_kind=review` 记录；
- 待 Phase 1-2 实现后更新本节核验状态。

## 11. Auction Anchor 接入（[CHANGE-20260730-018]）

### 编排顺序

`after_close_orchestrator` 在 stock_core 发布后、market_aggregation 之前插入 auction_anchor 生成：

```
stock_core 发布 → chip_consensus 结果 → auction_anchor 生成发布 → market_aggregation → review
```

### 统一入口

盘后编排、Admin、恢复入口统一调用 `generate_and_publish_auction_anchors`（`backend/app/services/auction_anchor_service.py`），在一个事务边界内完成锚点生成+校验+publication 切换。**禁止盘后只 generate 不 publish**。

### Chip 软失败语义（[P0-2]）

| chip 状态 | auction_anchor 行为 |
|---|---|
| succeeded/partial | status=succeeded，生成完整锚点（structure+chip+composite） |
| failed/timeout/未完成 | status=structure_only，只生成结构锚点 |
| chip 后来恢复成功 | 在 chip worker 完成回调中重新调用 `generate_and_publish_auction_anchors` 重建完整锚点，`publish_auction_anchors` 通过 `on_conflict_do_update` 原子切换 publication 指针到新 snapshot |

失败不影响 core，标记为 `optional_failure`。

### Scheduler 接入（[P0-3]）

接入现有 after_close_orchestrator Worker（`WORKER_TYPE=auction_scheduler`），**不新建容器**：

| 时间（Asia/Shanghai） | 任务 | run_key |
|---|---|---|
| 09:25:05 | `auction_final:{date}` — 扫描最终竞价 | `auction_final:{date}` |
| 10:00:00 | `auction_open_confirmation:{date}` — 开盘后验证事件生命周期 | `auction_open_confirmation:{date}` |

使用 SchedulerJobRun、run_key、heartbeat、lease、fencing、retry 和恢复机制（同 after_close_orchestrator 模式）。

实现位置：`backend/app/services/auction_scheduler_service.py`，由 `worker.py` 在现有 poll loop 中按 WORKER_TYPE 分发。

### 当前实现状态

- `after_close_orchestrator.py` 已接入 `generate_and_publish_auction_anchors` 调用
- `after_close_chip_consensus_service.py` 在 chip 完成回调中触发锚点重建
- `auction_scheduler_service.py` 提供 09:25/10:00 任务创建与执行
- 详见 `docs/maps/75-auction-analysis.md`

## 12. review 阶段接入 + 时间线负耗时修复（2026-08-01 核验，CHANGE-20260801-001）

### 12.1 盘后正式链（已核验代码）

**真实执行链**（`after_close_orchestrator.py:publishing` 之后的 `computing_review` 阶段）：

```python
# 步骤 1：从 factor_publications 拿 stock_core 正式 pointer（board_analysis 已退役，见 70-review.md §0）
stock_core_pub   = get_current_pointer(scope='stock_core')
board_analysis_pub = get_current_pointer(scope='board_analysis')

# 步骤 2：review_orchestrator_service.create_run() (幂等: 同输入返回同 run)
review_run_id = review_orchestrator_service.create_run(
    trade_date              = trade_date,
    source_stock_core_run_id = stock_core_pub.run_id,
    source_board_run_id      = board_analysis_pub.run_id,  # Historical/Non-Normative: legacy Review 输入，当前 source_board_run_id 恒为 NULL（Slice 4A5 Board-independent）
    algorithm_version       = CURRENT_ALGO_VERSION,
    # + metadata: bootstrap_required, scope 列表, 等
)

# 步骤 3：review_orchestrator_service.compute_run(review_run_id)
#   - 内部按 scope 逐项计算; 失败记录 reason; 支持幂等
compute_status = review_orchestrator_service.compute_run(review_run_id)
if compute_status != 'succeeded':
    raise AfterCloseReviewError(review_run_id, compute_status)

# 步骤 4：review_orchestrator_service.publish_review(review_run_id)
#   - 切换 review_publications.published_run_id; 写入 publishing factor 元数据
pub_status = review_orchestrator_service.publish_review(review_run_id)

# 步骤 5：主任务 only after 上面四步都 succeeded → 主任务 SUCCEEDED
# 若任一步失败: 主任务 FAILED, metadata.review_run_id / review_status / review_reason 回写
```

位置：`backend/app/services/after_close_orchestrator.py` 的 `computing_review` 阶段（在 publishing 后，watchlist_ready 前）。

**检查点语义（Phase 0 收口）**：Review 失败/质量门阻塞时 `_review_failed=True`，主任务收 `partial_success`，但通过 `_update_heartbeat_and_step(db, job_run, None, worker_id)` 传 `None` 仅刷新心跳、**不推进 `last_completed_step`**；只有 Review 真正成功才推进 `computing_review` 检查点，避免下次 resume 跳过失败的 Review。

### 12.2 7 步状态机 & 时间线映射（后端）

文件：`backend/app/services/after_close_pipeline_service.py`

```text
_PIPELINE_STEPS（7步展示序列）：
  refreshing_daily (0)
    → syncing_boards (1)
    → checking_coverage (2)
    → computing_features (3)  # absorbing legacy 4-steps (creating_dsa/waiting_dsa_worker/quality_gate/feature_snapshot)
    → publishing (4)
    → computing_review (5)  # [NEW] 本 CHANGE 新增
    → watchlist_ready (6)
```

- `_COMPLETED_STEP_INDEX[COMPUTING_REVIEW] = 5`, `_COMPLETED_STEP_INDEX[SUCCEEDED] = 6`
- `StepStatus` 聚合：`SUCCEEDED` 仅当 review_publications.published pointer 存在且 review_run.status=succeeded。

### 12.3 时间线负耗时根因 & 修复（后端核心）

**根因（修复前）**：
- `_aggregate_step_events` 按创建时间 降序（新→旧）处理事件 → 对同一 attempt 中的同一 transfer 先拿到 finished（新） 再拿 started（旧），导致 `start=finished_event.created_at`、`finish=started_event.created_at`，出现负 duration。
- 跨 attempt 事件（如 queued→queued 两次之间的 START）未被隔离，旧 attempt 的 finish 与新 attempt 的 start 混用。
- 混用 naive/aware datetime，未统一时区。

**修复（代码核验）**：
1. **统一时区 `_normalize_to_shanghai`**：所有 datetime 转为 Asia/Shanghai aware。naive datetime 按 UTC 再转上海；该函数在 `__main__` 自测中覆盖 UTC→上海正确转换。
2. **按事件升序（旧→新）**：事件排序 `sorted(events, key=lambda e: e.created_at)`。
3. **attempt 隔离**：通过边界事件（status=queued/manual_resume 或包含 START 关键字的 payload.step ）切开 attempts；每 attempt 独立计算 step 转移。
4. **step 正确配对**：每个 attempt 内从 step_prev → step_next 转移时，step_prev.finish = transfer_event.created_at，step_next.start = transfer_event.created_at；终端事件（status=failed/succeeded）作为当前 running_step.finish。
5. **异常告警写入 `warnings`**：若出现 `start > finish` 或 `duration ≤ 0`，不填负数，不填 `max(0, duration)`，而是：
   - `duration_seconds = None`
   - `warnings.append("invalid_order_or_zero_duration")`
   - （前端用黄底展示"未知"）

### 12.4 时间线前端展示映射（已核验代码）

文件：
- `frontend/src/pages/adminAfterClosePipelineHelpers.ts:formatDurationSeconds()`
- `frontend/src/pages/AdminAfterClosePipelinePage.tsx → PipelineTimeline`
- `frontend/src/styles/global.scss:.timeline-meta-warn`

规则：
| status | duration | warnings | 显示 | 样式 |
|---|---|---|---|---|
| running | null / ≤0 | — | "进行中" | 正常灰字 |
| completed / succeeded / failed | 正数 且无 invalid_order warning | — | `Xm Ys` 或 `X.Xs` | 正常 |
| 任何 | null / ≤0 | `includes('invalid_order_or_zero_duration')` | "未知" | `.timeline-meta-warn` 黄底，⚠ 前缀 + title 诊断 |
| 任何 | 正数 | `includes('invalid_order_or_zero_duration')` | "未知" | 同上（警告优先于数值展示） |

### 12.5 测试覆盖（已核验）

后端：
- `__main__` 自测：7 步、computing_review 顺序、succeeded index=6、时区转换 通过
- `backend/tests/test_admin_after_close_pipeline.py`（CI PG 集成测试：CI 真实执行）
- `backend/tests/test_after_close_status_detail.py`（CI PG 集成测试）

前端 node 合同（local 33/33 pass）：
- `frontend/src/pages/__tests__/adminAfterClosePipeline.test.ts`
  - `7步断言` + computing_review 标签/位置
  - `formatDurationSeconds` 9 项覆盖 running/未知/正数 + warnings invalid_order 情形

## 13. V2.1 开发链 Commit D–J（2026-08-05 基线 2267d43）

> 本节记录 Commit D–I 的**真实实现**（Commit J 为本文档/Change/Acceptance Matrix/Runbook
> 收口）。当前为代码开发阶段，未部署、未 apply Migration、未跑 PG 集成、未做真实数据验收。

### 13.1 进入点（entry functions）

- **chip 正式发布指针**：`factor_publication_service.publish_chip_consensus`
  （见 maps/20-quant-model.md §13.1 与 `backend/app/services/factor_publication_service.py`）。
  在 `ChipConsensusRun` 达到可发布终态（`succeeded`/`partial`）后原子写入
  `PUBLICATION_KIND_CHIP_CONSENSUS` 发布指针，并强制 lineage。
- **ProductReadiness 聚合/闭包**：`ProductReadinessService.evaluate_for_trade_date` /
  `collect_states`（`backend/app/services/product_readiness_service.py`）。
- **治理报告**：`evaluate_governance`（同文件，纯函数）。
- **Admin readiness API**：`GET /v1/admin/readiness/{trade_date}`
  （`backend/app/api/admin_readiness.py`，`require_roles("admin")`）。
- **Admin 盘后工作台前端**：`frontend/src/features/product-readiness/AdminReadinessWorkbench.tsx`，
  挂载于 `AdminDataProductionPage`「数据生产中心」总览。

### 13.2 九节点产品与分类（Commit G）

| 产品 | 分类 | readiness 数据源 |
|---|---|---|
| daily_facts | mandatory | `history_cross_section` publication pointer |
| board_facts | mandatory | `board_facts` publication pointer（pointer 指向 `reused_previous` → ready_reused） |
| stock_core | mandatory | `stock_core` publication pointer |
| board_aggregation | mandatory | `market_aggregation` publication pointer |
| review | mandatory | 正式发布指针（`MarketReviewRun.published_at` 非空）；run 自称 published 但 `published_at` 为空 → `degraded + REVIEW_NOT_PUBLISHED` |
| dsa_projection | enhancement（派生投影） | 真实产物核验：当日 `stock_feature_snapshots` 行数 > 0；无产物 → `NO_PROJECTION` |
| chip | enhancement | `chip_consensus` publication pointer；run succeeded 但无 pointer → `degraded + CHIP_PUBLICATION_MISSING` |
| state_events | enhancement（派生投影） | 真实产物核验：当日 `StockStateEvent` 按 `event_type` 计数 > 0；无事件 → `NO_STATE_EVENTS` |
| auction_anchor | enhancement | `auction_anchor` publication pointer；`structure_only` → `degraded + stale + AUCTION_STRUCTURE_ONLY`（等待 chip 升级） |

> [Corrective-3 §三] 修改前 `dsa_projection` / `state_events` 随 stock_core 自动 ready、
> `review` 仅看 run.status、`chip` succeeded 即 ready、`auction structure_only` 与
> succeeded 同样呈现 fresh/ready；这四处均会掩盖真实缺口，已按真实产物/指针核验修正。

闭包状态语义（`evaluate_closure`）：`pending` / `blocked` / `core_ready` /
`degraded_ready` / `fully_ready`。关键判定顺序：blocked（mandatory 任一 unavailable）→
pending（stock_core 未形成）→ core_ready（stock_core 就绪但其余 mandatory 未完成）→
mandatory 全部就绪后分 fully_ready / degraded_ready。

**目标差距（2026-08-06）**：PRD31 PC-51 新增 `mandatory_ready_enhancing`，用于 mandatory 已 ready 但增强任务仍 active/stale、尚未完成对账的阶段。当前 `evaluate_closure` 尚无该状态，容易过早落入 `degraded_ready`，需在后续代码修复及 DTO/前端合同中统一补齐。

### 13.3 chip 发布与血统（Commit D，[Corrective-3] 重写）

**关键历史事实**：在 Corrective-3 之前，**没有任何生产路径向 `chip_consensus_runs`
写入过数据**（`after_close_chip_consensus_service` 只写
`StockChipConsensusSnapshot`）。同时 worker 用错误签名 + `chip_run_id=None`
调用发布函数并把返回的 ORM 当 dict 读，因此 chip pointer 从未真正发布过，
且被 `except Exception: warning` 静默吞掉。

#### 13.3.1 ChipConsensusRun 生命周期

入口：`backend/app/services/chip_consensus_run_lifecycle.py`

- `resolve_or_create_chip_run(...)`：worker 领取 chip job 后创建或解析**唯一**领域 run。
  解析优先级：job metadata `chip_run_id` → 同
  `(trade_date, source_core_run_id, algorithm_version)` 未终结 run → 新建。
  retry/resume 复用同一 run，已完成进度不清零。
- `chip_run_id` 通过 `fenced_job_run_service.merge_job_run_metadata` 固定进
  `SchedulerJobRun.metadata_json`。
- `finalize_chip_run(...)`：计算结束写终态，`coverage_ratio` 由真实计数推导。

#### 13.3.2 编排顺序（强制）

```text
chip snapshots 完成
  → ChipConsensusRun 终态（finalize_chip_run）
  → publish_chip_consensus（真实 chip_run_id + algorithm_version）
  → commit publication pointer
  → generate_and_publish_auction_anchors
```

由 `publish_chip_and_upgrade_auction` 编排（依赖可注入，便于不连库测试）。
**auction 升级只在 chip pointer 成功发布之后执行**；发布失败禁止触发 composite upgrade。

#### 13.3.3 发布校验链与治理

- `publish_chip_consensus` 校验链：chip_run 存在 → trade_date 匹配 → status 为
  `succeeded`/`partial` → 当日已发布 `stock_core` pointer 存在 →
  `chip_run.source_core_run_id == 已发布 stock_core pointer.data_run_id`。
- coverage 由 DB 统计（`chip_run.coverage_ratio`），不接受调用方任意传值。
- 重复发布走 `on_conflict_do_update` 幂等；失败只重试指针，不重算 DSA/SMC/momentum。
- **软失败可治理**：写入 SchedulerJobRun metadata
  `chip_publication_status` / `chip_publication_error_code` /
  `chip_publication_error_message` / `chip_publication_retryable` / `chip_publication_id`。
  ProductReadiness 据此显示 `CHIP_PUBLICATION_MISSING` + `retry_chip_publication`。
- **lease fencing**：发布前与 auction 前各校验一次租约；失去租约则跳过全部写入。
- 测试：`test_chip_publication_unit.py`（发布函数）、
  `test_chip_worker_orchestration.py`（编排顺序 / 治理 metadata / retry 复用 / lease）。

### 13.4 Review 依赖与血统（Commit F）

- Review 只依赖 `stock_core` + `market_aggregation` 两个正式 publication pointer；
  不等待 chip、不等待 auction。
- 创建阶段只查询这两类 kind，禁止额外查询 chip/auction/state_event 等 kind。
- exact lineage：board run 的 `source_core_run_id` 必须与 stock_core pointer 同源、同日、succeeded。
- consumer 只读发布结果（publication pointer 指向的 run），不读临时表。
- 测试：`test_review_v21_dependency_contract.py`（mock AsyncSession）。

### 13.5 Migration 085 与 PG deferred 状态

- `backend/alembic/versions/085_board_definition_identity_contract.py` 已存在
  （Corrective-2，`board_definition_versions.identity_contract_version`）。
- 本轮未新增 Migration；`085` 未 apply。
- PG 集成测试（`test_v21_synthetic_e2e_pg.py` 等）标记
  `status = authored_not_executed`、`reason = pg_gate_deferred_during_development`，
  不阻塞开发。

### 13.6 当前实现状态

- 代码：已实现（Commit D–I 已提交并 push origin/dev）。
- 远程静态/纯单元验证：在授权范围内执行（Ruff、改动文件 Mypy、PURE_UNIT_TEST）。
- PG 集成 / Migration apply / 部署 / 真实数据验收 / 浏览器验收：未执行（PG gate deferred）。
