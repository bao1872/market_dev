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
- 盘中监控：09:30–11:30、13:00–15:00 按配置轮询；监控资格判定使用 `app.services.eligible_user_service.filter_monitor_eligible_recipients`，active admin 与 active member + 有效 subscription 进入监控，disabled admin 与无订阅普通用户排除；`monitor_batch_service` 拉取 1m 行情使用 `include_realtime=True` 并剔除最后一根未完成 1m，日线/15m 输入使用 `include_realtime=False`；
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
- **Run lifecycle（Phase 8 新增）**：feature_snapshot 步骤前后写 `stock_feature_snapshot_runs`：
  - 开始时 `create_snapshot_run(trade_date, 'after_close')` 创建 `running` run（独立 session + commit）；
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

### 2.4 Feature Snapshot 历史回补脚本

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
