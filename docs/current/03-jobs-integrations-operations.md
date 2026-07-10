# 03 后台任务、第三方集成与运维

## 1. Worker 类型

| Compose 服务 | WORKER_TYPE | 职责 |
|---|---|---|
| worker-bars-scheduler | `bars_scheduler` | 更新行情、聚合和触发盘后链路 |
| worker-strategy-scheduler | `strategy_scheduler` | DSA 兜底调度 |
| worker-calendar | `calendar_scheduler` | 更新交易日历 |
| worker-monitor | `monitor_scheduler` | 盘中自选股监控 |
| worker-strategy-batch | `strategy_batch` | 领取并执行 StrategyRun |
| worker-outbox | `outbox` | 扩张 Outbox 为 MessageDelivery |
| worker-delivery | `delivery` | 实际渠道投递、重试、最终状态 |
| worker-after-close | `after_close_orchestrator` | 盘后编排任务 |
| worker-watchdog | `watchdog` | 每 60s 清理 stale scheduler_job_runs 和僵尸 worker_heartbeats |
| worker-capture | capture service | 生成个股详情图片 |

统一 Worker 入口是 `backend/app/worker.py`。服务编排事实源是 `docker-compose.prod.yml`。

worker-strategy-batch 的 run 级总超时由 STRATEGY_RUN_TOTAL_TIMEOUT_SECONDS 环境变量控制（默认 7200 秒，与 after_close_orchestrator._DSA_POLL_TIMEOUT_SECONDS 对齐）。run 级总超时耗尽后剩余 pending 项标记 failed/run_timeout_budget_exhausted。历史 bars 不足标的（< 60 根日线）在 create_batch_run 时标记 skipped/insufficient_history，不进入计算循环。

## 2. 调度语义

- 日历刷新：约 02:00 Asia/Shanghai；
- 盘后行情：交易日约 16:00；
- DSA 兜底：交易日约 18:30；
- 盘中监控：09:30–11:30、13:00–15:00 按配置轮询；监控资格判定使用 `app.services.eligible_user_service.filter_monitor_eligible_recipients`，active admin 与 active member + 有效 subscription 进入监控，disabled admin 与无订阅普通用户排除；`monitor_batch_service` 拉取 1m 行情使用 `include_realtime=True` 并剔除最后一根未完成 1m，`source_bar_time` 始终来自最新已完成 1m bar；日线/15m **计算输入**使用 `include_realtime=False`（仅已完成 bar，watchlist_monitor 事件计算口径不得因截图实时性需求而变更）；飞书盘中截图展示默认 1d，实时性由 Capture Snapshot `1d + include_realtime=True` 的 partial daily 合成保证，与监控计算链路分离；
- Outbox/Delivery：短轮询；`delivery_worker.py` 对 `monitor_event`/`strategy_event`/`monitor_chart` 投递前再次调用 `is_user_eligible_for_monitor` 复核，与 monitor_batch/event_recipient/outbox_relay 口径一致；
- Worker 心跳：持续更新。

### 2.1 实时行情与 pytdx 接入

`/api/v1/instruments/{instrument_id}/quote` 仅在 `market_status_service.compute_market_session` 返回 `MORNING_SESSION` 或 `AFTERNOON_SESSION` 时尝试 pytdx 实时拉取；午休、盘前、盘后、非交易日均不尝试 pytdx，直接读 DB 日线 fallback。

- pytdx 使用模块级单例适配器 + 线程锁，防止多线程同时操作同步 socket；
- 连接异常时支持断线重连，超时可控；
- 使用 Redis 短缓存（10s TTL）削峰，缓存命中时直接返回缓存结果；
- 同步 pytdx 调用通过线程池提交到 async event loop，避免阻塞主循环；
- 日志必须区分 `pytdx 成功`、`pytdx 失败 fallback`、`非交易时段 fallback` 三种场景。

`MarketDataAggregationService` 在 `timeframe=1d && include_realtime=true && 交易时段` 时，用当日已完成 1m bar 合成一根 partial daily bar 追加到响应末尾，返回 `data_source=hybrid`、`is_partial=true`、`last_live_bar_time`；非交易时段、收盘后、`include_realtime=false` 时不合成。partial daily bar 不写库，仅用于前端盘中展示。

盘中监控与个股详情 K线实时是两条独立业务链路，共同依赖 MDAS 的 live 1m 拉取能力：
- `worker-monitor` → `monitor_batch_service.execute_monitor_cycle()` → `MarketDataAggregationService.get_bars(timeframe="1m", include_realtime=True)` → `pytdx_adapter.get_minute_bars`；监控触发不依赖 `StockDetailPage`，也不依赖前端 `/quote`；只处理最新已完成 1m bar，并剔除最后一根可能未完成的 bar；
- 个股详情 `/bars?timeframe=1d&include_realtime=true` 通过同一份 live 1m 数据合成 partial daily bar 供页面展示；
- 两条链路均要求 `start_time`/`end_time` 同为 `Asia/Shanghai` aware datetime，禁止 naive/aware 混用。

行情调度与盘后编排中的覆盖率检查统一复用 `BarsCoverageService`，禁止复制 SQL。`worker-bars-scheduler` 与 `worker-after-close` 均以 `shanghai_business_date()` 作为业务日期，避免服务器时区偏差。所有覆盖率门禁（`bars_scheduler` 自动触发 DSA、`dsa-only`、系统概览 `WAITING_DSA` 判定）均使用 `BarsCoverageService.compute_daily_coverage` 返回的 `coverage_raw` 原始值进行阈值判断，`coverage` 仅用于展示。`/admin/after-close-runs/dsa-only` 在当日无数据时 fallback 到最新可用交易日再校验覆盖率。

### 2.2 盘后 publish auto-trigger

当前系统使用 DSA 完成后自动触发盘后 publish 流水线，避免 `strategy_batch_worker` 完成 DSA run 后 `after_close_orchestrator` 未启动导致 publish 缺失。

触发链路：
- `worker.py` 在 `strategy_batch_worker` 完成 DSA run 后检查 `strategy_type == "dsa_selector"` 且 `trigger_source == "scheduled"` 且 `status == "completed"`；
- 满足条件时自动调用 `create_after_close_run(trade_date, run_id)`，触发 `after_close_orchestrator` 执行 publish；
- 仅对 `dsa_selector + scheduled + completed` 触发，其他策略类型、手动触发、非 completed 状态不触发；
- `create_after_close_run` 幂等：同 `trade_date` 已有 after_close 任务时返回已有任务，不重复创建；
- 触发失败不传播异常，仅记录日志（`logger.exception`），不影响 `strategy_batch_worker` 主流程；
- 非 DSA 策略（如 `watchlist_monitor`）不触发 auto-trigger；
- `trade_date` 缺失时不触发，记录 warning 日志。

### 2.3 盘后编排状态机与 feature_snapshot 步骤

`after_close_orchestrator.execute_after_close_run` 状态机当前为：

```text
queued → refreshing_daily → checking_coverage → creating_dsa
  → waiting_dsa_worker → quality_gate → feature_snapshot → publishing → succeeded
任意步骤异常 → failed
```

`feature_snapshot` 步骤位于 `quality_gate` 与 `publishing` 之间，调用 `feature_snapshot_service.compute_for_trade_date` 为当日 active A 股全集生成 `stock_feature_snapshots` 行：

- 使用独立 `AsyncSessionLocal`，不依赖 HTTP 请求 session；
- 单股失败写 `degraded_reasons` 不阻断其他股票；
- 失败比例超过 `failure_threshold`（默认 0.3）抛 `RuntimeError`；
- **事务边界**：`compute_for_trade_date` 不内部 commit，只 upsert（flush）+ 检查阈值；caller（`after_close_orchestrator`）显式控制：
  - 成功（`failure_rate <= threshold`）→ `db.commit()`，进入 `publishing`；
  - `RuntimeError`（超阈值）→ 显式 `db.rollback()` 丢弃半成品行 → 异常向上传播 → orchestrator 写 `failed` 事件 → **不进入 publishing**；
- `feature_snapshot` 失败时 `last_completed_step` 不推进，重试从 `quality_gate` 之后重新进入；
- 完成后更新心跳与 `last_completed_step='feature_snapshot'`；
- **Heartbeat 保活（CHANGE-20260709-003）**：feature_snapshot 阶段启动后台 `_job_run_heartbeat_loop`（间隔 30s），并在 `compute_for_trade_date` 每批完成后通过 `_build_feature_snapshot_progress_callback` 刷新 `heartbeat_at`、`lease_expires_at` 与 `metadata.feature_snapshot_progress`，防止长计算被 watchdog/recovery 误判为 stale；进度事件按每 500 只股票采样写入 `job_run_events`，避免事件表膨胀；
- **Run lifecycle（Phase 8 新增）**：feature_snapshot 步骤前后写 `stock_feature_snapshot_runs`：
  - 开始时 `create_snapshot_run(trade_date, 'after_close')` 创建 `running` run（独立 session + commit），并立即把 `feature_snapshot_run_id` / `last_started_step=feature_snapshot` 写回 orchestrator metadata；
  - 成功时 `finish_snapshot_run(status='succeeded')` 写 `published_at`（独立 session + commit）；
  - 失败时 `finish_snapshot_run(status='failed')` 不写 `published_at`（独立 session + commit），再向上传播异常触发 orchestrator FAILED；
  - run 记录在独立 session 中提交，保证 snapshot session rollback 不影响 run 状态持久化。

断点恢复路径（`last_completed_step` → 已完成步骤集合）：

| `last_completed_step` | 已完成步骤集合 |
|---|---|
| `None` / `queued` | `{}` |
| `refreshing_daily` | `{refreshing_daily}` |
| `waiting_dsa_worker` | `{refreshing_daily, waiting_dsa_worker}` |
| `quality_gate` | `{refreshing_daily, waiting_dsa_worker, quality_gate}` |
| `feature_snapshot` | `{refreshing_daily, waiting_dsa_worker, quality_gate, feature_snapshot}` |
| `publishing` | `{refreshing_daily, waiting_dsa_worker, quality_gate, feature_snapshot, publishing}` |
| `succeeded` | 全部（直接返回） |

`feature_snapshot` 失败时 `last_completed_step` 不会推进到 `feature_snapshot`，重试会从 `quality_gate` 之后重新进入 `feature_snapshot`。

### 2.3.1 盘后流水线可视化面板（admin）

`backend/app/services/after_close_pipeline_service.py` 为管理员提供盘后流水线聚合状态视图，复用现有 after_close_orchestrator 状态机、job_run_events、stock_feature_snapshot_runs run gate，不引入新的状态定义。

**4 个 admin API 端点（prefix=/admin）**：

| 端点 | 方法 | 说明 |
|---|---|---|
| `/after-close/pipeline/latest` | GET | 查询最近交易日的流水线聚合状态（交易日始终返回 today，非交易日回退最近有记录的交易日） |
| `/after-close/pipeline?trade_date=YYYY-MM-DD` | GET | 查询指定交易日的流水线聚合状态 |
| `/after-close/pipeline/runs?limit=20` | GET | 查询最近 N 次 after_close_orchestrator + snapshot_run 混合列表 |
| `/after-close/pipeline/run` | POST | 触发当日 after_close 编排（幂等：同 trade_date 已有 queued/running/succeeded 返回 existing） |

**响应结构 `AfterClosePipelineResponse`**：
- `overall_status`：not_started / running / succeeded / failed / blocked / skipped（交易日收盘后超过 30 分钟阈值仍无 run → blocked；非交易日无 run 且无 backfill_full → skipped）；
- `latest` 策略：交易日（含今日）始终以 today 为目标 trade_date，即使无 run 也返回 today 的 not_started/blocked，不回退历史 run 掩盖"今天未执行"；非交易日回退到最近有记录的交易日；
- `feature_snapshot_run` 摘要优先返回 succeeded+published+full run（即 watchlist_ready 的实际数据源），若不存在再 fallback 到最新任意 run；
- `watchlist_ready`：严格复用 `feature_snapshot_service.has_succeeded_snapshot_run`（`status='succeeded' AND published_at IS NOT NULL AND metadata_.scope='full'`），sample backfill 不计入；"自选可用"是发布后的最终门禁，不作为执行步骤；
- `steps`：面向用户 **5 个真实阶段**（内部细粒度状态归并）：`market_prep`(行情准备=refreshing_daily+checking_coverage+creating_dsa) → `dsa_compute`(DSA计算=waiting_dsa_worker) → `quality_gate`(质量校验) → `feature_snapshot`(特征快照) → `publishing`(发布结果)；每步 status 为 pending/running/completed/failed/skipped，运行中阶段 `finished_at=null` 且耗时=now-started（不为负）；
- `after_close_run`：job_run 摘要（status/orchestrator_status/heartbeat/lease_expires/last_completed_step/error/`feature_snapshot_progress`/`feature_snapshot_stalled`）；
- `feature_snapshot_stalled`：顶层与 `after_close_run` 均暴露；编排处于 feature_snapshot 且心跳新鲜，但 `feature_snapshot_progress.updated_at` 距今 > 300s 时为 true，供前端提示"疑似停滞"（不替代心跳超时 blocked 判定）；
- `feature_snapshot_run`：snapshot_run 摘要（run_type/scope/snapshot_count/failed_count/published_at）；
- `data_freshness`：复用 `system_overview_service._compute_data_freshness`；
- `events`：最近 100 条 job_run_events（来自 `job_run_event_service.list_events`）；状态切换事件带 `payload.event_type="started"`，feature_snapshot 进度事件带 `payload.event_type="progress"`。

**前端页面**：
- `/admin/after-close` 详情页：顶部状态卡 + 5 阶段垂直时间线（行情准备/DSA计算/质量校验/特征快照/发布结果）+ 数据新鲜度卡 + 编排状态详情 + 最近 20 次运行列表 + 事件日志抽屉；特征快照阶段展示进度（processed/total、snapshot 成功/失败、速度、ETA）与"疑似停滞"横幅；
- `/admin/overview` 中的 `AfterClosePipelineCard` 改造为摘要卡（状态 pill + 编排阶段 + Worker 心跳 + 行情/选股发布至 + 进入详情链接）；
- 轮询策略：running 状态 10s，非 running 60s，页面不可见暂停。

**生产验收路径（PR #47 已部署验证）**：
- `/health` → 200；
- `/admin/after-close/pipeline/latest` → 200（交易日 today 无 run 返回 today 的 not_started/blocked，非交易日回退最近有记录的交易日）；
- `/admin/after-close/pipeline?trade_date=YYYY-MM-DD` → 200；
- `/admin/after-close/pipeline/runs?limit=20` → 200；
- `/admin/overview` 与 `/admin/after-close` → 200；
- backend/frontend 20m 日志无 5xx/502/timeout；
- 文案校验：盘前无 run → not_started；收盘后 30 分钟无 run → blocked；sample run 不显示为前台可读；full/published/succeeded → watchlist_ready=true；手动 backfill full 与正式 after_close succeeded 通过 has_backfill_full 区分。
- **中断后 UI 展示（CHANGE-20260709-003）**：`orchestrator_status='interrupted'` 且存在 `feature_snapshot_run.status='running'` 时，`steps[5]`（feature_snapshot）显示 `running`，并在 `after_close_run` 摘要中暴露 `feature_snapshot_run_id` / `feature_snapshot_progress`，页面提示“快照计算失联/待修复”。

### 2.3.2 feature_snapshot 心跳失联修复 Runbook

**触发场景**：`after_close_orchestrator` 因 Worker 重启、租约过期或心跳超时被标记为 `interrupted`，但同 trade_date 的 `stock_feature_snapshot_runs` 仍卡在 `running`。

**修复入口**：`app.services.after_close_orchestrator.repair_stale_after_close_snapshot_runs`。

**修复策略**：
- 仅当存在 `status='interrupted'/'failed'` 的 after_close job_run 且同 trade_date 存在 `run_type='after_close' + status='running'` 的 snapshot run 时才触发；
- 若 snapshot run 已运行时间超过 `stale_threshold_seconds`（默认 300s）则进入修复；
- 统计 `stock_feature_snapshots` 中该 trade_date 实际行数：
  - `actual_count >= expected_count * 0.95` → `finish_snapshot_run(status='succeeded')` 并写 `published_at`，允许 watchlist 读取；
  - 否则 → `finish_snapshot_run(status='failed')`，metadata 写入 `reason='orchestrator_interrupted_or_lease_expired'`；
- 修复后不会自动重试 after_close，需要管理员手动触发 retry 或调用 execute_after_close_run 断点恢复。

**生产操作步骤**：
1. 确认 `trading-worker-after-close` 已停止或已部署修复版本；
2. 在 backend 容器或管理脚本中调用 `repair_stale_after_close_snapshot_runs`；
3. 检查返回结果中 `action` 为 `succeeded` 或 `failed`；
4. 若 repair 为 `failed`，通过 admin 页面或 API 重试当日 after_close，利用 `last_completed_step='quality_gate'` 断点恢复，仅重跑 `feature_snapshot`；
5. 验证 `watchlist_ready=true` 且 `/admin/after-close` 特征快照阶段状态正确。

**禁止操作**：
- 不要直接删除 `stock_feature_snapshot_runs`；
- 不要手工修改 `published_at` 冒充成功；
- 不要清空 `stock_feature_snapshots`；
- 不要在修复前启动新的 research matrix 全量回补。

### 2.3.3 正式盘后任务 vs research matrix 回补优先级

- 正式 `after_close_orchestrator` 优先级高于 `research_matrix_backfill`；
- research 回补不会自动触发 after_close，两者仅共享 CPU/内存资源，不互相改状态；
- 若 after_close 处于 `running`/`interrupted` 且 research 回补仍在运行，应让 research 跑完当前月份后停止，不继续下个月，待盘后任务恢复正常后再决定是否继续 research 收尾。

### 2.4 Feature Snapshot 历史回补脚本

> **历史回补仍 BLOCKED**：PR #41 生产验证 full scope 耗时 126 分钟，超过 120 分钟阈值。下一步进入 `--profile-summary` 轻量性能诊断（不重构公式、不跑历史回补），定位瓶颈后再决定是否进入 compute-once-extract 优化。

`backend/scripts/feature_snapshot_backfill.py` 为历史交易日批量生成 `stock_feature_snapshots`。**核心计算逻辑在 `backend/app/services/feature_snapshot_service.py`，脚本只做 CLI 参数解析、dry-run 标记、resume 跳过、批量调用 service。**

**Instrument-first 架构（Phase 8 新增）**：脚本从 date-first 重构为 instrument-first，避免为每个 instrument/date 重复查询 bars：

```
for instrument_batch in active instruments:
    一次性拉该 batch 每只股票从 start 前足够 warmup 到 end 的 1d/15m bars
    for trade_date in trade_dates:
        在内存 slice 到 trade_date
        compute_feature_snapshot_for_date(primary_bars=..., secondary_bars=...)
        upsert
```

调用方式：

```bash
cd /root/web_dev/backend && .venv/bin/python -m scripts.feature_snapshot_backfill \
    --start 2026-06-01 --end latest --batch-size 20 --resume --dry-run
```

CLI 参数：

| 参数 | 默认 | 语义 |
|---|---|---|
| `--start` | 必填 | 起始日期 YYYY-MM-DD |
| `--end` | `latest` | 结束日期或 `latest`（解析为 `bars_daily` 表最新 trade_date） |
| `--batch-size` | 20 | 每批 instrument 数（保守内存） |
| `--resume` | False | 跳过已存在 snapshot 且所属日期有 `succeeded` run 的行 |
| `--dry-run` | False | 只打印计划与 missing 统计，不执行写入 |
| `--failure-threshold` | 0.3 | 单日失败比例阈值，超过则该日 run 标 `failed` |
| `--symbols` | None | 只处理指定股票代码（逗号分隔，如 `000100,603303`），用于小样本验证；触发 `scope='sample'` |
| `--limit-instruments` | None | 只处理前 N 只股票（整数），用于小样本验证；触发 `scope='sample'` |
| `--workers` | 1 | 并行进程数（1=单进程，>1 启用 multiprocessing）；建议生产先用 2 验证，再 4；超过 `os.cpu_count()` 自动 cap 并 warning |

**Instrument-first 优势**：
- 每只股票每周期（1d / 15m）只调用一次 `load_instrument_bars`，不为每个 trade_date 重复查询；
- bars 在内存中按 `trade_date` slice（`_truncate_bars_to_trade_date`），复用 `compute_feature_snapshot_for_date` 已有的 `primary_bars` / `secondary_bars` 入参；
- 单进程模式不并发，保证稳定和低内存；multiprocessing 见 2.4.1。

**事务边界（单进程 / 多进程 不同）**：
- 单进程（`--workers 1`，默认）：`backfill_instrument_first` 不内部 commit，由 `main` 在结束时统一 commit；
- 多进程（`--workers N`，N>1）：worker 内 per-date commit（详见 2.4.1），主进程创建/finalize run records；
- 失败比例超阈值的 trade_date 标 run.status='failed'（不抛 RuntimeError，不阻断其他日期）；
- 单股失败不阻断其他股票。

### 2.4.1 Multiprocessing 模式（`--workers N`，N>1）

`--workers > 1` 时启用 multiprocessing，主进程通过 `ProcessPoolExecutor` + `asyncio.gather(return_exceptions=True)` 分发 instrument chunks 到独立 worker 进程。

**worker 函数**：`_worker_process_instruments(chunk, trade_dates, db_url, ...)` 为 top-level 可 pickle 函数，每个 worker 进程独立创建 `async_engine` + `async_sessionmaker`。

**worker DB pool 配置（[Blocker Fix] 已收紧）**：
- `pool_size=1, max_overflow=0, pool_pre_ping=True`（每个 worker 只需 1 个 session，避免 4 workers × 15 = 60 连接打满 PG）；
- 不复用主进程 engine（子进程不能共享父进程的 event loop / connection pool）。

**worker 事务边界（per-date commit）**：
- 每个 `(instrument, trade_date)` 是独立事务：`upsert → db.commit() → success++`；
- 异常时 `await db.rollback()`，`failed++`，下一个 trade_date 继续用干净事务；
- `load_instrument_bars` 失败时该 instrument 所有 trade_dates 都标 `failed` 并 `continue`（不影响其他 instrument）；
- **[Blocker Fix] 严格语义**：`success++` 只在 `db.commit()` 成功后执行（commit 失败 → rollback → `failed++`，DB 写入与 stats 一致）。

**worker future 异常统计（[Blocker Fix]）**：
- 主进程用 `asyncio.gather(*futures, return_exceptions=True)` 收集结果，保留 chunk 顺序映射（替代 `as_completed`，因 Python 3.12 `as_completed` 返回 wrapper future 无法回溯原 chunk）；
- worker 抛 `BaseException`（含 `KeyboardInterrupt`/`SystemExit`）时，对该 chunk 的每个 instrument × 每个 trade_date 增加 `failed`，避免 worker 崩溃但 run 仍 finalized 为 `succeeded`；
- worker 正常返回时合并 `per_date_stats`（success/failed/skipped）。

**`--workers` 参数保护（[Blocker Fix]）**：
- `--workers < 1` 直接 `parser.error()` 抛 `SystemExit`（拒绝 0 / 负数）；
- `--workers > os.cpu_count()` 时 `warnings.warn()` + 自动 cap 到 `cpu_count`；
- 生产默认仍 1（不自动并发）；建议先 `--workers 2` 小样本验证，再 `--workers 4`。

**kill/resume 策略**：
- per-date commit 保证被 kill 时不丢已完成行；
- `--resume` 跳过已存在 snapshot 且所属 trade_date 有 `succeeded` run 的行（双重过滤）；
- 被中断后重启 `--workers N --resume` 即可续跑，已 commit 的行不会重复计算。

**Run gate（Phase 8 新增，[Blocker Fix] 增加 scope 区分）**：
- 每个 trade_date 开始时创建 `running` run（`run_type='backfill'`）；
- 成功时 `finish_snapshot_run(status='succeeded', metadata={'scope': scope})` 写 `published_at`；
- 失败时 `finish_snapshot_run(status='failed', metadata={'scope': scope})` 不写 `published_at`；
- `--resume` 跳过已存在 snapshot 且所属日期有 `succeeded` run 的行（双重过滤）；
- **[Blocker Fix] scope 区分 full / sample**：
  - `_resolve_run_scope(symbols, limit_instruments)` 决定 scope：任一过滤启用 → `sample`，都未启用 → `full`；
  - 普通 backfill（不带 `--symbols`/`--limit-instruments`）→ `metadata_.scope='full'`（watchlist 可读）；
  - 小样本 backfill（带 `--symbols` 或 `--limit-instruments`）→ `metadata_.scope='sample'`（watchlist 不可读，即使 succeeded + published_at 非空）；
  - `scope` 同时传入 `create_snapshot_run(scope=...)` 和 `finish_snapshot_run(metadata={'scope': ...})`，因 `finish_snapshot_run` 的 metadata 完全替换 create 时的 metadata；
  - `--dry-run` 不创建 run 记录。

**`--dry-run` 输出**：
- trade_dates 列表、active instruments、missing rows、预计 batch 数；
- 不写库。

**生产小样本验证流程**：
1. `--dry-run` 查看 missing rows 和预计批次数；
2. `--symbols 000100,603303` 或 `--limit-instruments 20` 跑最近 1 个交易日；
3. 验证 `stock_feature_snapshot_runs.status='succeeded'`、`metadata_.scope='sample'`、snapshot row count；
4. **[Blocker Fix] 小样本 run 不得发布到 watchlist**：因 `scope='sample'`，`/watchlist/monitor-status` 不读取该日 snapshot，`calculation_status` 保持 `WAITING_SNAPSHOT` / `NO_SNAPSHOT`，避免污染生产 watchlist SUCCEEDED 状态；
5. 禁止直接全量回补，需小样本验证后再逐步扩大。

约束：
- 不修改 DSA/BB/swing/temporal 数学公式；
- 复用 `feature_snapshot_service.compute_for_trade_date`；
- 单股失败记录到 `degraded_reasons`，不阻塞其他股票；
- upsert 幂等，可重复执行；
- `start > end` 直接 `sys.exit(1)`。

### 2.4.2 研究特征矩阵回补（research_feature_matrix_backfill）

`backend/scripts/research_feature_matrix_backfill.py` 是研究矩阵 CLI 入口，**DB 为主存储**（parquet 仅可选 debug 导出），与生产 `feature_snapshot_backfill` 严格分离：

- 不接入 `watchlist_ready`，不修改 production snapshot；
- 不写 `stock_feature_snapshots`，只写专用 research 表（`research_feature_matrix_runs` + `research_feature_matrix_rows`）；
- 写入由 `backend/app/research/research_matrix_writer.py` 提供（三道硬阈值 + monthly run 生命周期 + 批量 upsert）；
- 计算由 `backend/app/research/feature_computer.py` 提供（per-bar full series，复用现有算法 SSOT：ATR/BB/SQZMOM/swing/DSA）；
- 字段因果口径由 `backend/app/research/feature_causality_registry.py` 统一登记，分 4 命名空间：`causal`（16）/ `confirmed_delay`（4）/ `hindsight`（6）/ `label`（7）= 33 字段；
- `hindsight.*` 与 `label.*` 禁止进入回测 feature；
- DSA 双轨：`causal.dsa_confirmed_*`（当时可知）vs `hindsight.dsa_finalized_*`（未来确认后回标注）；
- Node Cluster 只能是 `hindsight.node_cluster_*`，不得进入 causal；
- registry 仍保留 dotted key（`causal.atr`），写 DB 时映射成下划线列名（`causal_atr`）；
- 详见 `06-research-feature-matrix.md`。

#### 2.4.2.1 CLI 参数

| 参数 | 必填 | 语义 |
|---|---|---|
| `--month YYYY-MM` | 与 `--start` 互斥必填其一 | 单月回补（推荐用法） |
| `--start YYYY-MM-DD` | 与 `--month` 互斥必填其一 | 起始日期（与 `--end` 配合用于跨月 sample 验证） |
| `--end YYYY-MM-DD` / `latest` | 可选，默认 `latest` | 结束日期 |
| `--symbols` | 可选 | 只处理指定股票代码（逗号分隔，触发 sample scope） |
| `--limit-instruments N` | 可选 | 限制处理 instrument 数（触发 sample scope） |
| `--dry-run` | 可选 | 只打印计划与估算，不写 DB，不写文件 |
| `--resume` | 可选 | 续跑模式：已存在 run 复用，已存在 instrument/date 幂等 upsert |
| `--export-parquet PATH` | 可选 | 可选 debug 导出 parquet 路径（仅 sample scope，不作为主存储） |

#### 2.4.2.2 三道硬阈值

任一不通过即停止：

| 阈值 | 触发条件 | 行为 |
|---|---|---|
| 磁盘剩余 | `df -h /` 剩余 < 15GB | 停止（不创建 run） |
| 单月预估 | `estimate_month_size > 3GB`（rows × 2KB） | 停止（不创建 run） |
| 失败率 | [Blocker Fix] `failed_rows / expected_rows > 5%` | run 标 `failed`，不继续后续月份 |

设计原因：磁盘约 61GB 可用，数据库在 `/` 分区上，写数据库也会占用磁盘。

**失败率口径（Blocker Fix）**：
- `failed_rows` = 失败行数（一个股票失败对应多 trade_date 行失败）；
- `expected_rows` = `instruments_count × trade_dates_count`；
- `metadata_json.failed_instruments` = 失败股票数（仅查询用，不参与失败率计算）；
- 单只股票失败时 `_process_instrument` 返回 `(0, expected_rows)`，主流程累加 `total_failed_rows` + `total_failed_instruments`。

#### 2.4.2.3 进程锁（Blocker Fix）

防止同 `month/scope` 重复启动后台任务，使用双保险：

| 锁类型 | 实现 | 释放 |
|---|---|---|
| `pg_advisory_lock` | `acquire_run_lock(db, month, scope)` 调用 `pg_try_advisory_lock(namespace, key)`，namespace=`0x5245534D`（"RESM"），key=`sha1(month_scope)[:4]` 稳定 hash | session close 自动释放（session-level） |
| lock file | `acquire_lock_file(month, scope)` 用 `os.open(O_CREAT \| O_EXCL \| O_WRONLY)` 原子创建 `/tmp/research_matrix_backfill_{month}_{scope}.lock` | `release_lock_file(path)` 显式删除 |

**CLI 主流程约束**：
- dry-run 不获取锁（不写 DB）；
- 非 dry-run 必须先 `acquire_lock_file` 再 `acquire_run_lock`，任一失败打印 `[BLOCKED]` 退出；
- 主循环 + finalize 包在 `try`，`finally` 先 `await lock_session.close()` 再 `release_lock_file(lock_path)`；
- 同 `month/scope` 已有 running run 或 lock 存在时拒绝启动。

#### 2.4.2.4 分阶段验证

禁止直接全量跑到当前。必须按以下顺序逐阶段验证，每阶段验收通过后才进入下一阶段：

| 阶段 | 命令 | 验收点 |
|---|---|---|
| A. dry-run | `--month 2026-01 --dry-run` | 打印计划，`expected_rows` / `estimated_db_size` 合理 |
| B. 2 symbols | `--month 2026-01 --symbols 000001,600000` | run succeeded，rows 写入正确 |
| C. 100 stocks × 1 month | `--month 2026-01 --limit-instruments 100` | run succeeded，failed_rate < 5% |
| D. 全市场 2026-01 | `--month 2026-01` | run succeeded，磁盘占用合理 |
| E. 后台逐月回补到当前 | nohup 串行跑 `2026-02` 到当前 | 每月 run succeeded，磁盘监控 |

> 阶段 B/C/D 必须在 PR merge + migration 058 应用后才能执行。

**关键约束**：
- 阶段 A/B/C/D 必须前台执行（前台跑完才能跑下一阶段），不允许 nohup；
- **只有 D 阶段通过后，才允许启动后台逐月回补（阶段 E）**；
- 每阶段必须检查：`df -h /` / `rows_count` / `failed_rows` / `failed_rate` / `run.status` / 表大小 / 日志是否有 traceback。

#### 2.4.2.5 调用方式

```bash
# dry-run 查看计划
cd /root/web_dev/backend && python -m scripts.research_feature_matrix_backfill \
    --month 2026-01 --dry-run

# 2 symbols 验证
cd /root/web_dev/backend && python -m scripts.research_feature_matrix_backfill \
    --month 2026-01 --symbols 000001,600000

# 全市场 2026-01
cd /root/web_dev/backend && python -m scripts.research_feature_matrix_backfill \
    --month 2026-01

# --resume 续跑（幂等 upsert）
cd /root/web_dev/backend && python -m scripts.research_feature_matrix_backfill \
    --month 2026-01 --resume
```

#### 2.4.2.6 数据模型

由 `backend/alembic/versions/058_research_feature_matrix.py` 创建两张表：

- `research_feature_matrix_runs`：按月分批的 run 级元数据，唯一键 `run_key`（如 `2026-01_full`），记录状态机与统计摘要；`metadata_json` 只放小摘要（scope/notes/thresholds），不存完整 payload，不建 GIN 索引；[Blocker Fix] `failed_count` 列存 `failed_rows`，`metadata_json.failed_instruments` 存股票级失败数；
- `research_feature_matrix_rows`：扁平宽表 39 列（5 metadata + 33 feature + 1 created_at），唯一键 `(instrument_id, trade_date)` 跨 run 幂等 upsert；3 个 btree 索引（`trade_date` / `instrument_id` / `run_id`），不给单个 feature 列建索引。

详见 `06-research-feature-matrix.md` 第 7 节。

#### 2.4.2.7 后台逐月回补 runbook

**前置条件**：阶段 D（全市场 2026-01）前台验收通过。

**启动方式**（nohup 串行，不并行）：

```bash
cat > /tmp/run_research_matrix_backfill.sh <<'EOF'
set -e
MONTHS="2026-02 2026-03 2026-04 2026-05 2026-06 2026-07"
for m in $MONTHS; do
  free=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
  if [ "$free" -lt 15 ]; then echo "STOP disk free ${free}GB"; exit 1; fi
  echo "RUN $m $(date)"
  docker exec trading-backend python -m scripts.research_feature_matrix_backfill --month "$m" --resume
  echo "DONE $m $(date)"
done
EOF

nohup bash /tmp/run_research_matrix_backfill.sh > /tmp/research_matrix_backfill.log 2>&1 &
echo $! > /tmp/research_matrix_backfill.pid
```

**停止条件**（任一触发即停止）：
- `df -h /` 剩余 < 15GB（脚本内联检查 + 手动检查）；
- 单月 run 标 `failed`（CLI 自动停止后续月份）；
- `failed_rate > 5%`（CLI 自动标 failed）；
- 日志出现 traceback（人工检查）。

**Phase 1 实际运行结果（2026-07-09）**：
- 后台串行 6 个月（2026-02 到 2026-07）全部 succeeded；
- 全量 7 个月（含 2026-01 前台 D）共写入 621,769 行，覆盖 2026-01-05 到 2026-07-08；
- 表大小 223 MB，磁盘占用可控；
- 最高 failed_rate 为 2026-02 的 4.11%（主要由北交所/新股 bars 不足导致），未超过 5% 阈值。

**监控命令**：
```bash
# 查看后台进度
tail -f /tmp/research_matrix_backfill.log
ps -p $(cat /tmp/research_matrix_backfill.pid) -o pid,etime,cmd --no-headers 2>/dev/null || echo "exited"

# 查看当前月份 run 状态
docker compose --env-file /etc/market-dev/market.env -f docker-compose.prod.yml exec -T postgres psql -U bz -d bz_stock -c "
select run_key,status,rows_count,failed_count,duration_seconds
from research_feature_matrix_runs
order by created_at desc limit 10;
"

# 查看表大小
docker compose --env-file /etc/market-dev/market.env -f docker-compose.prod.yml exec -T postgres psql -U bz -d bz_stock -c "
SELECT relname, pg_size_pretty(pg_total_relation_size(relid))
FROM pg_catalog.pg_statio_user_tables
WHERE relname LIKE 'research_feature_matrix%'
ORDER BY pg_total_relation_size(relid) DESC;
"
```

**停止后台任务**：
```bash
kill $(cat /tmp/research_matrix_backfill.pid)
# 清理 lock file（如异常退出残留）
rm -f /tmp/research_matrix_backfill_*.lock
```

**清理临时文件**：
```bash
rm -f /tmp/research_matrix_backfill_*.lock
rm -f /tmp/research_matrix_backfill.pid
# 保留日志最后 300 行
tail -300 /tmp/research_matrix_backfill.log > /tmp/research_matrix_backfill.final.log
mv /tmp/research_matrix_backfill.final.log /tmp/research_matrix_backfill.log
```

**禁止项**：
- 不要并行多月回补（每月串行，前一个月完成才跑下一个月）；
- 不要写 parquet/export（仅 sample scope 可选，full 回补不允许）；
- 不要跑 production `stock_feature_snapshots` 历史回补（仍 BLOCKED）；
- 不要生成 coverage/截图/DB 备份/大日志；
- 不要删除数据库卷或运行中镜像。

## 2.5 监控图片通知链路

盘中监控触发后，文字通知与图片通知是**两段独立链路**。文字通知成功不代表图片通知一定成功。

### 2.5.1 文字/卡片链路

```text
worker-monitor → monitor_batch_service.execute_monitor_cycle()
  → StrategyEvent / MonitorEvaluation
  → EventRecipient
  → NotificationMessage (source_type=monitor_event / strategy_event)
  → Outbox(notification.message.created)
  → outbox_relay
  → eligible_user_service 资格过滤
  → MessageDelivery(delivery_type=text/card)
  → delivery_worker
  → FeishuPlatformAppAdapter.send()
```

### 2.5.2 图片链路

```text
worker-monitor → monitor_batch_service._send_chart_images_via_outbox()
  → 生成短期 capture token
  → 调用 worker-capture HTTP /capture
  → capture_jobs (status=succeeded/failed)
  → NotificationMessage (source_type=monitor_chart)
  → Outbox(notification.message.created) payload:
       { delivery_type: "image", image_url: "...", message_group_id: "..." }
  → outbox_relay
  → MessageDelivery(delivery_type=image)
  → delivery_worker
  → FeishuPlatformAppAdapter 上传/发送图片
```

图片链路通过 `message_group_id` 与文字/卡片链路关联，形成同一事件的图文消息组。

### 2.5.3 Capture Token 字段要求

调用 `worker-capture` 时必须使用 `app.core.security.create_capture_token` 生成短期 token，且必须携带以下字段：

| 字段 | 要求 | 说明 |
|---|---|---|
| `type` | 固定为 `"capture"` | 由 `create_capture_token` 内部写入 |
| `scope` | 必须为 `"stock_detail_capture"` | 常量见 `app.constants.capture.CAPTURE_SCOPE_STOCK_DETAIL` |
| `user_id` | 必填 | 接收图片通知的用户 ID |
| `instrument_id` | 必填 | 截图标的 ID，必须与请求路径中的 `instrument_id` 一致 |
| `event_id` | 必填 | 关联的 StrategyEvent / MonitorEvaluation ID |
| `exp` | 短期有效 | 默认使用 `jwt_capture_ttl_seconds` |

`/api/v1/capture/stocks/{instrument_id}/snapshot` 通过 `get_capture_token_payload` 校验：
- `type == "capture"`；
- `scope == "stock_detail_capture"`；
- `user_id`、`instrument_id`、`event_id` 均存在；
- 否则返回 401/403。

capture worker 在截图时还会校验 token 中的 `instrument_id` 与路径参数 `instrument_id` 一致。字段缺失会导致截图请求 401/403，`image_url` 为空，后续图片 Outbox / MessageDelivery 不会生成，但**不影响文字通知**。

### 2.5.4 截图失败不阻塞文字通知

`monitor_batch_service._send_chart_images_via_outbox()` 对每只股票单独捕获：
- capture worker 返回 401/403/异常或无 `image_url` 时，写 `capture_jobs` 记录（`status=failed`，`error_code=CAPTURE_REQUEST_FAILED` / `NO_IMAGE_URL`），然后 `continue`；
- 文字通知链路在此之前已经写入 Outbox，因此截图失败不会回滚或阻塞文字通知；
- 同一 `message_group_id` 下可能出现文字已发送但图片缺失的情况，整体状态由 delivery_worker 根据文字/图片结果标记为 `partial_failed`。

## 3. 任务状态与可观察性

重要任务必须记录：

```text
run_key
business_date
status
scheduled/started/finished
heartbeat
lease
instance
Git SHA
succeeded_count / failed_count
error_code / error_message
```

管理员和运维必须能回答：运行中的 Worker、Git SHA、心跳、next run、当前任务、股票计数、失败阶段、重试状态、发布完整性、文字状态、图片状态和数据新鲜度。

admin 可通过 `GET /admin/worker-heartbeats` 查看 `worker_heartbeats` 表的 raw 记录，附加后端计算的 `heartbeat_age_seconds` 和 `health_state`（fresh<120s / stale 120-600s / stopped≥600s 或 status=stopped）。AdminJobsPage "Worker 心跳" Tab 展示该数据，10 秒轮询，watchdog 服务在表中可见。

生产只读审计发现：`worker_heartbeats` 存在 stale/running 僵尸记录，导致 Worker 状态可信度不足。代码修复已由 PR #4 实现：`_recovery_watchdog_loop` 每 60 秒调用 `mark_stale_worker_heartbeats`，将 `status='running'` 且 `heartbeat_at` 超过 600 秒的记录标记为 `stopped`。PR #4 部署后该 loop 因 `WORKER_TYPE` 启动条件未匹配任何生产 worker 而从未运行；PR #7 新增独立 `worker-watchdog` 生产服务（`WORKER_TYPE=watchdog`）使其在生产运行。生产部署 67105c2 后验证：watchdog 启动即标记 38 条僵尸心跳为 stopped，stale running 已清零（ALIGN-023 已关闭）。

## 4. 飞书 Platform App

当前唯一飞书接入方式是 Platform App。

```text
Business Event / Manual Share
→ NotificationMessage
→ Outbox
→ Outbox Relay
→ MessageDelivery
→ Delivery Worker
→ FeishuPlatformAppAdapter
```

管理员内测申请通知走专用 `beta_application.admin_notification.created` Outbox 事件，查询 active admin 用户的 active `feishu_platform_app` 渠道，不走普通 eligible_user_service。

普通自动通知仍需要 active member + active subscription 过滤。手动指定 `target_channel_id` 的用户主动通知跳过资格过滤，但只能投递到指定 active channel。

## 5. Capture 与图文投递

Capture Worker 使用短期 Capture Token 访问 `/capture/stock/:symbol`。截图页面不经过普通 ProtectedLayout，不污染普通 Access Token。

截图页面渲染就绪判定：
- `page.goto` 使用 `wait_until="load"`（历史根因：`networkidle` 在前端存在长连接/持续轮询时永远不会触发，导致 30s 超时返回 502）。
- 页面 load 后通过 `wait_for_selector('[data-testid="stock-detail-capture"][data-render-ready="true"]')` 等待 bars + indicators 就绪，再执行截图。

文字和图片分开投递，状态分别记录。状态必须可查询，支持仅重试图片。

失败阶段包括：

```text
snapshot
capture
image_outbox
image_upload
image_delivery
card
text_outbox
```

### 5.1 飞书盘中截图实时链路（高清 + 不复用旧图/旧指标）

飞书盘中监控截图必须满足三件事：高清、不复用上一轮旧图/旧指标、K线标题显示股票名称。

- **业务默认周期 = 1d（日线）**：`stock_detail_feishu_service`（手动飞书分享）与 `monitor_batch_service._send_chart_images_via_outbox`（自动盘中监控截图）向 capture worker 发送的 `capture_payload["timeframe"]` 固定为 `1d`（常量 `FEISHU_CAPTURE_TIMEFRAME`，见 `app/constants/capture.py`）；实时性由 Capture Snapshot `1d + include_realtime=True` 的 partial daily 合成保证；Capture API 支持 15m/1h 等多周期是**能力**，`15m` 只用于显式请求 / 调试 / 未来策略声明，不得成为飞书业务默认周期。截图修复（高清/缓存/清晰度）**不得改变 `watchlist_monitor` 事件计算口径**。
- **高清截图**：capture worker 浏览器上下文使用 `viewport=1920x1200` + `device_scale_factor=2`（env `CAPTURE_VIEWPORT_WIDTH/HEIGHT` / `CAPTURE_DEVICE_SCALE_FACTOR`，默认 1920/1200/2，严禁 4 倍），提升 PNG 清晰度；截图为单张、不落库、不存 base64。
- **不复用旧图/旧指标**：
  - 截图缓存 key 维度扩展为 `event_id + instrument_id + chart_version + timeframe + source_bar_time + capture_run_id + device_scale_factor`，不同时间点/周期天然区分；
  - `disable_cache=True` 跳过读文件缓存但允许写最新缓存（飞书实时截图默认 True）；
  - `MonitorSnapshotService.get_snapshot(force_refresh=True)` 跳过内存缓存但写回最新；indicator 链路 `force_refresh=1&capture=1` 跳过 Redis 读缓存但写最新；
  - `source_bar_time` 优先取最新 `MonitorEvaluation.source_bar_time`（SUCCEEDED），兜底 `now_shanghai()` 分钟级；
  - Capture Snapshot 端点 `include_realtime=True` 且周期透传，盘中 K线为当前实时数据。
- **范围与爆炸半径**：不引入 DB schema/migration、不重启 postgres/redis、不批量删除 captures/cache；部署仅重建 capture-worker / backend / frontend；仅单次飞书实测，不批量。

### 5.2 K线实时状态展示（前端 CaptureStockPage）

`CaptureStockPage` 按 URL `timeframe` 初始化（无则 1d），截图模式不锁定日线；请求 snapshot 携带 `force_refresh=1&source_bar_time=...`；状态栏展示 `data_source` / `is_partial` / `last_live_bar_time` / quote status；K线主标题优先显示股票名称 `名称（代码）`，URL 仍用 symbol。

## 6. 部署与健康检查

生产服务：postgres、redis、backend、frontend、多个 worker、capture worker。

部署顺序：

```text
确认 main + 工作区干净
→ 备份数据库（本次任务暂不部署，不执行）
→ 构建 backend/frontend/capture
→ postgres/redis healthy
→ Alembic upgrade head
→ 启动 backend/frontend/workers
→ 验证版本、健康、心跳、任务、行情、发布和投递
```

部署前性能基线采集（物理单机）：

```text
docker stats --no-stream
free -h
df -h
uptime
```

实时行情部署验证项：

- `GET /api/v1/instruments/{instrument_id}/quote` 返回字段包含 `source`/`is_realtime`/`update_time`/`freshness_seconds`/`degraded`/`degraded_reason`；
- 交易时段 pytdx 成功时 `source="pytdx"`、`is_realtime=true`、`degraded=false`；
- 交易时段 pytdx 失败时 `source="daily_fallback"`、`degraded=true`；
- 非交易时段 fallback 时 `source="daily_fallback"`、`degraded=false`；
- `GET /api/v1/instruments/{instrument_id}/bars` 返回 `data_source`/`as_of`/`is_partial`/`degraded`/`degraded_reason`；
- StockDetailPage 状态徽章显示“实时行情 / 日线回退 / 数据延迟 / 行情降级”之一，不再固定显示“实时行情”；
- 容器日志可见 `pytdx 成功`、`pytdx 失败 fallback`、`非交易时段 fallback` 区分日志。

`CORE_ONLY=1` 只用于受控恢复。需要完整业务能力时必须运行对应 worker：趋势选股需要 strategy_batch/scheduler，飞书图片需要 capture/outbox/delivery。

## 7. Secret 与日志

- Secret 不提交 Git；
- 文档不记录真实 Secret；
- 部署脚本不回显完整连接串或飞书密钥；
- 日志保留 service、git_sha、run_id、run_key、instrument、source_bar_time、error_code、request_id；
- 发现泄露先轮换，再处理历史。

## 8. after_close_orchestrator 类型债务治理

`after_close_orchestrator.py` 原有 22 个 mypy baseline 错误，根因是 `db.get(SchedulerJobRun, ...)` 返回 `SchedulerJobRun | None`，但调用方直接传给 `_update_orchestrator_status` / `_update_heartbeat_and_step`（参数要求非 Optional）。

当前治理方式：

- 新增 `_get_job_run_or_raise(db, job_run_id) -> SchedulerJobRun` 和 `_get_strategy_run_or_raise(db, run_id) -> StrategyRun` 类型收窄 helper，不存在时显式 raise；
- `execute_after_close_run` 中所有 `db.get(SchedulerJobRun, job_run_id)` 替换为 `_get_job_run_or_raise`，不再依赖 Optional 传递；
- `_get_or_create_job_run` 在 `acquire_job_run_lock` 返回 `is_new=True` 后显式校验 `job_run is not None`；
- `quality_gate` 阶段的 `dsa_run` 赋值使用 `_get_strategy_run_or_raise`，消除 `StrategyRun | None` 赋值冲突；
- 不使用 `cast` / `type: ignore` 掩盖 None；所有 None 分支显式 raise；
- 不改变 heartbeat、lease、repair、feature_snapshot 业务流程；只把"本来假设一定存在"的对象变成显式校验。

## 9. API 路由类型债务治理

`app/api/*` 和 `app/capture_main.py` 原有 20 个 mypy baseline 错误，根因是 `router.routes` 类型为 `list[BaseRoute]`，`BaseRoute` 没有 `path`/`methods` 属性，直接访问 `r.path` 触发 `[attr-defined]` 错误。

当前治理方式：

- 新增 `app/core/route_utils.py`，提供 `iter_api_routes(routes) -> Iterator[APIRoute]` 和 `get_route_paths(routes) -> list[str]` 类型收窄 helper；
- 所有 `app/api/*` 和 `capture_main.py` 中的 `[r.path for r in router.routes]` 替换为 `get_route_paths(router.routes)`；
- 需要同时访问 `r.path` 和 `r.methods` 的位置使用 `iter_api_routes` 收窄后迭代，并显式过滤 `r.methods is not None`；
- 不使用 `type: ignore` / `cast`；不改变 API 行为。

## 10. 债务治理工具通道规则

- mypy 使用 `MYPY_CACHE_DIR=/tmp/mypy_debt_cache` 单独检查目标文件，跑完删除 cache，不全仓库反复生成；
- 长命令（mypy 冷启动、大批量 pytest）使用 `nohup` + `/tmp/<name>.log` + `/tmp/<name>.pid` 后台执行，用 `ps`/`tail` 轮询，不依赖 Trae 交互式长连接；
- 单 PR 只处理一类债务，不混入业务逻辑修改。

## 11. Ruff baseline 债务治理

Ruff 债务清理属于纯样式修复，不需要构建镜像、部署或重启任何服务：

- 不构建 Docker、不重启 backend / worker、不启动 research backfill、不动生产调度逻辑；
- 长命令（ruff 全量检查、pytest 回归）写入 `/tmp/<name>.log`，结束后清理 `/tmp` 下的日志和 cache，不进入仓库；
- 磁盘可用空间 < 15GB 时立即停止债务治理工作，优先保障生产服务运行；
- 临时日志和 cache（`/tmp/ruff_*.log`、`/tmp/mypy_ruff_cleanup`、`.ruff_cache` 等）只允许放在 `/tmp` 或已 `.gitignore` 的位置，不得 commit；
- C408（`dict()` → `{}`）可用 `ruff --fix --unsafe-fixes` 自动修复，修后必须人工 diff 确认不改变语义；
- N806 仅允许两种处理方式：普通局部变量安全重命名为 snake_case；算法对齐变量（TradingView/PineScript/SMC 原命名）使用最小范围 `# noqa: N806` 并在旁注释 "kept to match upstream algorithm naming"；
- `strategy_assets` 虽是算法资产，但存在生产 import，不得从质量门禁中整体排除；
- 禁止全文件无说明的 blanket ignore；若使用 per-file ignore，必须在文件头注释说明原因，并在本节登记。

## 12. tests mypy 债务治理

`backend/tests/` mypy 债务清理属于纯类型修复，不需要构建镜像、部署或重启任何服务：

- 不构建 Docker、不重启 backend / worker、不启动 research backfill、不动生产调度逻辑；
- 不跑 coverage、不生成截图/DB 备份/大日志；
- mypy 使用 `MYPY_CACHE_DIR=/tmp/mypy_tests_cache` 单独检查 `tests/` 目录，跑完删除 cache，不全仓库反复生成；
- 长命令（mypy tests 冷启动、大批量 pytest）使用 `nohup` + `/tmp/<name>.log` + `/tmp/<name>.pid` 后台执行，用 `ps`/`tail` 轮询，不依赖 Trae 交互式长连接；
- 磁盘可用空间 < 15GB 时立即停止 tests mypy 治理工作，优先保障生产服务运行；
- 临时日志和 cache（`/tmp/mypy_tests_*.log`、`/tmp/pytest_*.log` 等）只允许放在 `/tmp`，不得 commit；
- 修复原则：typed fixtures（Protocol/dataclass/TypedDict）、显式 Optional 收窄（`assert x is not None`）、不大面积 Any/cast/type:ignore；
- 单 PR 只处理 tests mypy 债务，不混入业务逻辑修改或 app 行为变化。
