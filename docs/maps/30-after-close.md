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
- 编排任务以 `SchedulerJobRun`（job_name="after_close_orchestrator"）记录，状态机：queued → refreshing_daily → syncing_boards → checking_coverage → computing_features → publishing → succeeded；异常 → failed；可被 watchdog 中断后自动 resume_queued。
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
| AC-03 本地完整手动调试 | `backend/app/api/admin_after_close.py` 创建/重试/恢复端点；`scripts/trigger_dsa_batch_small.py` | 已实现未运行核验 | API 路径存在；本地未启动 Worker 执行完整链路 |
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
- **P0（生产诊断 2026-07-28，CHANGE-20260728-005）**：生产服务器（GIT_SHA=37c9fa3）2026-07-27 和 2026-07-28 两次盘后 run 失败，根因有二：
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
- ✅ **市场聚合独立 job**：`market_factor_aggregation_service.run_market_factor_aggregation`，读取 stock_core pointer 后切 market_aggregation pointer，失败只重跑聚合
- ✅ **事件 outbox 模型支持**：`StockFeatureSnapshotRunItem.phase='event_outbox'` 已定义，实际事件写入由 `stock_state_event` 表（稳定唯一键幂等）承载

**[本轮仍待验证的项]**：

- PG 集成测试 6 项待 CI（`PURE_UNIT_TEST=1` 时 SKIP，需 CI 临时 PG 容器）
- 生产部署后真实 canary 验证待执行
- 全市场 history 回补待执行

详见 `docs/changes/2026/CHANGE-20260729-008-incremental-publish-full-closure.md`。

### 11.6 History 版本一致性审计结论（CHANGE-20260729-009）

**核验状态：已基于生产数据库只读审计确认（2026-07-29）**

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

`first_pyramid_service.py` 中两个 15m 门槛的权威定义：

| 常量 | 值 | 用途 | 使用位置 |
|---|---|---|---|
| `_CHIP_MIN_15M_BARS` | `500` | 批量服务最低门槛（degraded） | `after_close_chip_consensus_service` 批量计算 |
| `NODE_CLUSTER_LOW_BARS` | `4000` | Node Cluster 完整质量门槛（250日×16根/日） | 个股详情实时计算 `compute_chip_status_for_stock` |

- 000021 15m bars=354，不满足 500 最低门槛 → `chip_status=skipped + reason_code=M15_BARS_INSUFFICIENT + actual_bars=354 + required_bars=500`
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
- `heartbeat_at` 超时阈值默认 180 秒（6 个 heartbeat 周期）；超时后 watchdog 将 run 标记为 `interrupted`。

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
  - 断点续算：`get_pending_chip_instruments` 过滤已 `succeeded` 的 instrument，`resume_queued` 只重试未成功项；
  - 部分成功写 `metadata.chip_status=partial`，主 `status=succeeded`；
- **不新增常驻容器**：chip_consensus worker 在现有 after-close worker 容器内通过 `WORKER_TYPE` 分支执行；
- watchdog：`auto_resume_interrupted_after_close_runs`（`scheduler_job_run_recovery_service.py:L169`）同时扫描 `after_close_orchestrator` 和 `after_close_chip_consensus` 两类 `interrupted` 任务，最多恢复 3 次；
- chip 创建：`after_close_orchestrator.py:L2066` 在主 run `succeeded` 后调用 `create_after_close_chip_consensus_job`（软失败，创建失败不反改主 run）。

### 13.4 聚合依赖闭环：stock_core pointer → board aggregation

- `market_factor_aggregation_service.run_market_factor_aggregation`（`backend/app/services/market_factor_aggregation_service.py:L33`）：
  - 步骤 1 读取已发布 `stock_core` pointer（`get_publication`），无 pointer 抛 `ValueError`；
  - 步骤 2 校验 `source_core_run_id` 等于 `stock_core` pointer 的 `data_run_id`；
  - 步骤 3 调用 `publish_market_aggregation` 切换 `market_aggregation` pointer（含 source 校验）；
- `board_analysis_service` 输入门禁：必须存在已发布 `stock_core` pointer，否则拒绝计算（PRD §5 BA-01）；
- 聚合失败只重跑聚合，不影响已发布 `stock_core`；
- 依赖顺序：`stock_core published` → `market_aggregation` / `board_analysis` 可触发 → `review` 可触发；
- `after_close_orchestrator` 当前止于 stock_core + chip_consensus 创建，**market aggregation 和 board_analysis 不在主编排内自动触发**，需通过 CLI / admin API 单独触发（见 `docs/runbooks/after-close-production-run.md` §9）。

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
- `market_review_run.source_board_run_id` 必须指向当前已发布的 `market_aggregation`（board）pointer 的 `data_run_id`；
- 两者均必须为当前正式 pointer，不得指向已过期或被替换的旧 run。

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
