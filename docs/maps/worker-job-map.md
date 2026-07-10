# Worker & Job Map

## 1. Worker 服务

| Compose 服务 | WORKER_TYPE | 主要表 | 关键风险 |
|---|---|---|---|
| worker-bars-scheduler | bars_scheduler | bars*, strategy_runs, scheduler_job_runs | 行情覆盖不足影响 DSA；复用 `BarsCoverageService` 统一口径，业务日期使用 `shanghai_business_date()` |
| worker-strategy-scheduler | strategy_scheduler | strategy_runs | 重复创建 run |
| worker-calendar | calendar_scheduler | trading_calendar | 交易日错误导致调度错误 |
| worker-monitor | monitor_scheduler | watchlist, monitor_evaluations, strategy_events, outbox, capture_jobs | 未完成 Bar 触发正式事件；链路为 `worker-monitor` → `monitor_batch_service.execute_monitor_cycle()` → `MarketDataAggregationService.get_bars(timeframe="1m", include_realtime=True)` → `pytdx_adapter.get_minute_bars`（仅最新已完成 1m bar，剔除最后未完成 bar，`source_bar_time` 来自 1m）；daily/15m 计算输入 `include_realtime=False`（watchlist_monitor 口径不被截图实时性污染）；**图片链路**：`monitor_batch_service._send_chart_images_via_outbox()` 生成 capture token → HTTP 调用 `worker-capture` /capture（`capture_payload["timeframe"]` 业务默认 `1d`，实时性由 Capture Snapshot 1d + include_realtime=True 的 partial daily 合成）→ 写 `capture_jobs` → 图片 Outbox（`delivery_type=image`，共享 `message_group_id`）；截图失败不阻塞文字通知；通知时间使用 `format_shanghai_datetime` |
| worker-strategy-batch | strategy_batch | strategy_runs, strategy_results | 发布残缺结果；run 级总超时 7200s（STRATEGY_RUN_TOTAL_TIMEOUT_SECONDS 可配置），历史不足标的标记 skipped/insufficient_history |
| worker-outbox | outbox | outbox, message_deliveries | 资格过滤或渠道扩张错误 |
| worker-delivery | delivery | message_deliveries, notification_channels | 假成功、吞错误 |
| worker-after-close | after_close_orchestrator | scheduler_job_runs, stock_feature_snapshots, stock_feature_snapshot_runs | 盘后链路断点恢复；状态机新增 `feature_snapshot` 步骤（`quality_gate → feature_snapshot → publishing`），调用 `feature_snapshot_service.compute_records_for_trade_date` 为当日 active A 股全集生成 `stock_feature_snapshots` 行；**两阶段 compute/write（CHANGE-20260711-001）**：compute 阶段只读（DB-only 批量加载 + 纯函数计算 → records，不 upsert/flush/commit snapshot）；write 阶段 `bulk_upsert_records`（每批 100 flush）+ `finish_snapshot_run(published_at)` 同一 `db` session 仅一次 commit；单股失败写 `degraded_reasons` 不阻断其他股票；失败比例超 30% 或 RSS 超 1800MB 抛 `RuntimeError` 标记 `failed`；**原子性**：write session 任一步失败整体 rollback（旧 `published` run 下新旧不混合），失败路径独立短 session 标记 run `failed`（published_at 保持 None）再向上传播；loader DB-only 批量查询（常数次，不逐股 pytdx/MDAS）；生产默认单进程、无持久缓存/新表/索引/migration；**Run lifecycle（Phase 8 新增 + [Blocker Fix] scope 必传）**：`feature_snapshot` 步骤前后写 `stock_feature_snapshot_runs`（`create_snapshot_run(scope='full')` 创建 `running` → `finish_snapshot_run(metadata={'scope': 'full'})` 终态 `succeeded`/`failed`），run 记录在独立 session 中提交保证 snapshot rollback 不影响；after_close 固定 `scope='full'`；覆盖率检查复用 `BarsCoverageService`；dsa-only 支持 fallback 到最新交易日 |
| scripts.feature_snapshot_backfill | - | stock_feature_snapshots, stock_feature_snapshot_runs | 历史交易日批量回补；**instrument-first 架构（Phase 8 新增）**：每只股票每周期只调用一次 `load_instrument_bars`，内存中按 `trade_date` slice；支持 `--symbols`/`--limit-instruments` 小样本验证；`--resume` 跳过已存在 + succeeded run 的行；run gate：每个 trade_date 创建 `succeeded`/`failed` run；失败比例超阈值标 `failed`（不抛 RuntimeError）；**[Blocker Fix] scope 区分 full/sample**：`_resolve_run_scope(symbols, limit_instruments)` 决定 scope，`scope` 同时传入 `create_snapshot_run` + `finish_snapshot_run`；sample run 不被 watchlist 读取（即使 succeeded + published_at 非空）；`--dry-run` 不创建 run；禁止直接全量回补；**multiprocessing（CHANGE-049 + Blocker Fix v2）**：`--workers N`（N>1）启用并行模式，主进程 `backfill_instrument_first_parallel()` 创建/finalize run records，通过 `ProcessPoolExecutor` + `asyncio.gather(return_exceptions=True)` 分发 instrument chunks 到独立 worker 进程；worker 函数 `_worker_process_instruments` 为 top-level 可 pickle，独立 `async_engine`（pool_size=1/max_overflow=0/pool_pre_ping=True）+ `async_sessionmaker`；**per-date commit 事务边界**：每个 `(instrument, trade_date)` 独立事务（`upsert → db.commit() → success++`，异常 `rollback + failed++`，下一 date 继续用干净事务，commit 失败不计 success）；**worker future 异常统计**：worker 抛 `BaseException` 时整个 chunk（每个 instrument × 每个 trade_date）计 `failed`，避免 worker 崩溃但 run 仍 finalized 为 `succeeded`；**`--workers` 参数保护**：`< 1` 直接 `parser.error()` 抛 `SystemExit`，`> os.cpu_count()` 时 `warnings.warn()` + 自动 cap；生产默认 1（不自动并发），建议先用 `--workers 2` 小样本验证，再 `--workers 4`；**kill/resume**：per-date commit 保证被 kill 不丢已完成行，`--workers N --resume` 续跑不重复计算已 commit 行 |
| worker-watchdog | watchdog | scheduler_job_runs, worker_heartbeats | 看门狗未运行导致僵尸残留 |
| worker-capture | capture service | capture_jobs, notification_messages | 截图失败但状态不可见；**调用方**：`monitor_batch_service._send_chart_images_via_outbox()`、`notification_service.test_channel_latest_event()`、`stock_detail_feishu_service`；**capture token 必须含** `scope=stock_detail_capture` / `user_id` / `instrument_id` / `event_id`，否则 `/api/v1/capture/stocks/{instrument_id}/snapshot` 返回 401/403；`page.goto` 使用 `wait_until="load"`（修复 `networkidle` 在前端长连接下 30s 超时返回 502 的问题），并通过 `data-render-ready="true"` 等待 bars+indicators 就绪；截图成功返回 `image_url`，失败由调用方写 `capture_jobs.status=failed` |
| scripts.research_feature_matrix_backfill | - | `research_feature_matrix_runs`, `research_feature_matrix_rows`（DB 主存储） | 研究特征矩阵 CLI 入口；**DB 为主存储**（parquet 仅可选 debug 导出）；与生产 `feature_snapshot_backfill` 严格分离：不接入 `watchlist_ready`，不写 `stock_feature_snapshots`；写入由 `app/research/research_matrix_writer.py` 提供（三道硬阈值 + monthly run 生命周期 + 批量 upsert）；计算由 `app/research/feature_computer.py` 提供（per-bar full series，复用 ATR/BB/SQZMOM/swing/DSA SSOT）；字段因果口径由 `app/research/feature_causality_registry.py` 统一登记（4 命名空间：causal 16 / confirmed_delay 4 / hindsight 6 / label 7 = 33 字段）；registry 保留 dotted key，写 DB 时映射成下划线列名（`causal.atr` → `causal_atr`）；`--month YYYY-MM` 单月回补，`--start/--end` 跨月 sample；`--symbols`/`--limit-instruments` 触发 sample scope；`--dry-run` 只打印计划；`--resume` 续跑幂等 upsert（`ON CONFLICT (instrument_id, trade_date) DO UPDATE`）；`--export-parquet` 仅 sample scope 可选 debug 导出；三道硬阈值：磁盘 < 15GB / 单月 > 3GB / 失败率 > 5% 停止；分阶段验证：dry-run → 2 symbols → 100 stocks × 1 month → 全市场 2026-01 → 逐月回补；instrument-first 架构（每只股票 load bars 1次 → compute_all_features → 按月份 trade_date 切片 → upsert）；tqdm 进度条；每 100 只 instrument commit 一次；详见 `current/06-research-feature-matrix.md` |

## 2. 任务状态

所有重要任务必须可从数据库回答：

```text
谁在跑
跑哪个 Git SHA
什么时候 heartbeat
业务日期是什么
run_key 是什么
成功/失败多少
失败原因是什么
是否可重试
```

## 3. Stale 处理

已有恢复逻辑会处理 stale scheduler_job_runs。生产审计发现：worker_heartbeats 中 status=running 但 heartbeat_at 过旧的记录曾不会自动清理（PR #4 的 `_recovery_watchdog_loop` 因 `WORKER_TYPE` 启动条件未匹配生产 worker 而从未运行）。已新增独立 `worker-watchdog` 生产服务（`WORKER_TYPE=watchdog`）让看门狗在生产运行：

```text
running + heartbeat_at < now - threshold → stopped/stale
```

不得删除历史记录，不得影响 fresh heartbeat。

## 3.1 Admin 可观察性入口

`GET /admin/worker-heartbeats`（admin 只读）返回 `worker_heartbeats` 表 raw 记录，附加后端计算的 `heartbeat_age_seconds` 和 `health_state`：
- fresh：running + age<120s（同 `system_overview_service.WORKER_HEALTH_WINDOW`）
- stale：running + 120s≤age<600s（同 `worker.STALE_HEARTBEAT_THRESHOLD_SECONDS`）
- stopped：status=stopped 或 age≥600s

AdminJobsPage "Worker 心跳" Tab 展示该数据，10 秒轮询。watchdog 服务在 `worker_heartbeats` 表中可见。

## 4. 修改 worker.py 原则

- 不做大拆分；
- 先补测试再移动代码；
- 每次只改一种 WORKER_TYPE 或一个横切能力；
- 保持 WORKER_TYPE、compose 服务名、调度时间、run_key、幂等逻辑不变。

## 5. `_notify_monitor_status` 直接发送路径

`worker.py:1087-1191` 的 `_notify_monitor_status` 用于监控启动/异常通知，**直接调用 `adapter.send()` 绕过 Outbox/Delivery Worker 管道**。代码 TODO 已标记，待产品决策是否保留作为降级路径（监控服务异常时 Outbox/Delivery Worker 可能也不可用）。该路径缺少重试、幂等（除启动通知 Redis 幂等）、静默时段规避，且无单元测试覆盖（ALIGN-025）。
