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
- 手动入口：`admin_after_close.py` 提供创建、DSA-only 创建、查询、重试、恢复 API；`backend/scripts/trigger_dsa_batch_small.py` 为脚本入口。
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
| 手动运行 | `backend/app/api/admin_after_close.py` | `create_after_close_run_endpoint` / `create_dsa_only_run_endpoint` / `retry_after_close_run_endpoint` | 管理员创建/重试 |
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
- 指定股票池：`create_dsa_only_run_endpoint` 支持指定 symbols；
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
