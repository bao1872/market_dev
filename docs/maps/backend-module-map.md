# Backend Module Map

> 目的：让新 agent 知道真实后端代码在哪里。本文是实现地图，不重复产品规则。

## 1. 应用入口

| 职责 | 文件 |
|---|---|
| FastAPI app | `backend/app/main.py` |
| Lifespan seed/recovery | `backend/app/main.py` |
| DB session | `backend/app/db.py` |
| API deps | `backend/app/core/deps.py` |
| Security/JWT | `backend/app/core/security.py` |
| Settings | `backend/app/config.py` / `backend/app/config.production.py` |

## 2. 模块映射

| 模块 | API | Service | Repository/Model | 测试/备注 |
|---|---|---|---|---|
| access/auth | `api/auth.py`, `api/me.py`, `api/plans.py` | `access_control_service.py`, `plan_service.py`, `subscription_service.py` | `models/user.py`, `models/subscription.py`, `models/plan.py` | 权限修改必须覆盖 active/expired/admin |
| market_data | `api/bars.py`, `api/market.py`, `api/calendar.py`, `api/instruments.py` | `market_data_aggregation_service.py`, `bars_coverage_service.py`, `calendar_seed.py`, `market_status_service.py` | bar/instrument/calendar models & repositories | 页面、指标、截图必须同源；覆盖率统一由 `BarsCoverageService` 计算；quote 实时性统一由 `market_status_service.compute_market_session` 判断，仅 `MORNING_SESSION`/`AFTERNOON_SESSION` 触发 pytdx；`market_data_aggregation_service.py` 负责 1d partial daily bar 合成（交易时段 `include_realtime=true` 时 `data_source=hybrid`/`is_partial=true`/`last_live_bar_time` 非空，收盘后/非交易时段 `is_partial=false`）；同时服务 `/bars` 实时展示与 `worker-monitor` 的 live 1m 输入，两条业务链路分离，但均要求 `start_time`/`end_time` 同为 `Asia/Shanghai` aware datetime |
| screening | `api/strategies.py`, `api/strategy_runs.py` | `strategy_batch_service.py`, strategy runtime | `strategy_*` models | 发布门禁关键模块；`strategy_batch_service.py` 新增 _classify_computable_universe 方法（_DSA_MIN_HISTORY_BARS=60），run 级总超时从 STRATEGY_RUN_TOTAL_TIMEOUT_SECONDS 环境变量读取（默认 7200），execute_run 保留预置 skipped_count |
| watchlist | `api/watchlist.py` | `feature_snapshot_service.compute_feature_snapshot_for_date` / `upsert_snapshot` / `compute_for_trade_date` / `build_summary_payload` / `_truncate_bars_to_trade_date` / `_resolve_expected_snapshot_trade_date`（async） | `user_watchlist_items`, `stock_feature_snapshots`, `monitor_evaluations`, `strategy_events` models | 到期权限和额度检查；`GET /watchlist/monitor-status` 响应 `metrics` 唯一来自 `stock_feature_snapshots.summary_payload`（`_source='feature_snapshot'`），不再走 `MonitorSnapshotService` 实时计算或 `MonitorState.payload` fallback；`MonitorEvaluation` 仅用于展示评估状态字段（evaluation_status/retry_count/error_code/source_bar_time）；新增 `calculation_status` 三态（SUCCEEDED/WAITING_SNAPSHOT/NO_SNAPSHOT）；`_resolve_expected_snapshot_trade_date` 复用 `calendar_service.get_previous_trading_day_async` / `get_most_recent_trading_day_async`，禁止硬编码周末 |
| monitoring | `api/monitor_states.py`, `api/strategy_events.py` | monitor scheduler/services, `eligible_user_service.py` (`filter_monitor_eligible_recipients`/`is_user_eligible_for_monitor`) | monitor/evaluation/event models | 只处理 completed 1m Bar；监控资格：active admin 放行，active member + 有效 subscription 放行，disabled admin / 无订阅普通用户排除；`monitor_batch_service`/`event_recipient_service`/`outbox_relay`/`delivery_worker` 四处统一使用该口径；`monitor_batch_service` 1m 输入 `include_realtime=True` 并剔除最后一根未完成 bar，日线/15m 输入 `include_realtime=False` |
| feature_snapshot | - | `services/feature_snapshot_service.py`（含 `create_snapshot_run` / `finish_snapshot_run` run lifecycle）, `services/after_close_orchestrator.py` (步骤 3.5), `services/calendar_service.py` (`get_previous_trading_day_async` / `get_most_recent_trading_day_async`) | `models/stock_feature_snapshot.py`, `models/stock_feature_snapshot_run.py`, migration `056_stock_feature_snapshots` + `057_stock_feature_snapshot_runs` | 复用 `_compute_all_factors_for_bars` / `_compute_relation` / `_compute_daily_context` / `_compute_m15_response` / `_compute_derived_relation` / `bollinger()` 不复制公式；point-in-time 截断 `index.date <= trade_date`；upsert 幂等；单股失败写 `degraded_reasons` 不阻断批次；失败比例超 30% 抛 `RuntimeError`；盘后 orchestrator 在 `quality_gate` 与 `publishing` 之间执行；`compute_for_trade_date` 不内部 commit，由 caller 决定 commit/rollback；run lifecycle（Phase 8 新增）：`create_snapshot_run(scope=...)` 创建 `running` run（独立 session + commit），`finish_snapshot_run(metadata={'scope': ...})` 终态为 `succeeded`（写 `published_at`）/ `failed`（不写）；watchlist 通过 `_has_succeeded_snapshot_run` 严格判断 `status='succeeded' + published_at IS NOT NULL + metadata_['scope']='full'`；**[Blocker Fix] scope 必传**：after_close 固定 `scope='full'`；普通 backfill 不带过滤条件时 `scope='full'`；小样本 backfill 带 `--symbols`/`--limit-instruments` 时 `scope='sample'`（watchlist 不可读） |
| feature_snapshot_backfill | - | `scripts/feature_snapshot_backfill.py`（CLI 调用层，无业务计算） | - | 历史交易日批量回补；**instrument-first 架构（Phase 8 新增）**：每只股票每周期只调用一次 `load_instrument_bars`，内存中按 `trade_date` slice；核心计算复用 `feature_snapshot_service.compute_feature_snapshot_for_date`；支持 `--start/--end=latest/--batch-size/--resume/--dry-run/--failure-threshold/--symbols/--limit-instruments/--workers`；`--resume` 跳过已存在 snapshot 且所属日期有 `succeeded` run 的行（双重过滤）；run gate：每个 trade_date 创建 `succeeded`/`failed` run；**[Blocker Fix] scope 区分**：`_resolve_run_scope(symbols, limit_instruments)` 决定 scope（任一过滤启用 → `sample`，都未启用 → `full`），传播到 `create_snapshot_run(scope=...)` + `finish_snapshot_run(metadata={'scope': ...})`；`--dry-run` 不创建 run；与 `backend/scripts/backfill_all_periods.py` 同目录同调用方式；**multiprocessing（CHANGE-049 + Blocker Fix v2）**：`--workers N`（N>1）启用并行模式，新增 top-level 可 pickle worker 函数 `_worker_process_instruments(chunk, trade_dates, db_url, ...)`（独立 `async_engine` + `async_sessionmaker`，pool_size=1/max_overflow=0/pool_pre_ping=True），主进程通过 `backfill_instrument_first_parallel()` 编排（`ProcessPoolExecutor` + `asyncio.gather(return_exceptions=True)`，按 chunk 顺序映射 results，worker 抛 `BaseException` 时整个 chunk 计 failed）；worker 内 **per-date commit**（`upsert → db.commit() → success++`，异常 `rollback + failed++`，下一 date 继续用干净事务，commit 失败不计 success）；`parse_args()` 对 `--workers < 1` 调 `parser.error()` 抛 `SystemExit`，`--workers > os.cpu_count()` 时 `warnings.warn()` + 自动 cap |
| notifications | `api/notifications.py`, `api/stock_detail_feishu.py` | `outbox_relay.py`, `delivery_worker.py`, `stock_detail_feishu_service.py`, `channel_adapter.py`, `feishu_card_builder.py`, `message_builder.py` | notification/outbox/delivery models | 飞书、图文、重试；消息时间使用 `format_shanghai_datetime`；`delivery_worker.py` 对 `monitor_event`/`strategy_event`/`monitor_chart` 投递前用 `is_user_eligible_for_monitor` 复核 |
| coverage | - | `bars_coverage_service.py` | `bars_daily`, `instruments` | 统一 A 股覆盖率口径，返回 `coverage`（展示）与 `coverage_raw`（阈值判断），供 scheduler/orchestrator/overview 使用 |
| capture | `api/capture.py` | `stock_capture_service.py` | `capture_jobs` | Capture Token 隔离 |
| jobs/admin | `api/admin_after_close.py`, admin APIs | scheduler recovery, job services | `scheduler_job_runs`, `worker_heartbeats` | 管理任务与可观察性 |
| beta/admin | `api/public_beta.py`, `api/admin_beta_applications.py` | `beta_application_service.py`, `beta_application_notifier.py` | beta application models | 管理员通知特殊路径 |

## 3. 高风险热点

| 文件 | 风险 | 处理原则 |
|---|---|---|
| `backend/app/worker.py` | 多 worker 类型集中，修改容易影响生产调度 | 先补测试和 maps，再小步拆 |
| `outbox_relay.py` + `delivery_worker.py` | 影响站内/飞书投递 | 任何修改必须覆盖 target_channel_id、admin、expired |
| `market_data_aggregation_service.py` | 影响页面、指标、截图、监控 | 不允许页面自建第二套行情语义 |
| `strategy_batch_service.py` | 影响发布批次 | 不得放宽完整性门禁 |

## 4. AI 修改规则

1. API 不复制 Service 规则；
2. Repository 不判断产品权限；
3. Strategy Runtime 不读取用户资格；
4. Delivery 不重算事件；
5. Monitoring 只生成事件，不直接决定飞书格式；
6. Capture 使用专用 token，不读普通登录态。
