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

`MarketDataAggregationService` 在 `timeframe=1d && include_realtime=true && 交易时段` 时，用当日已完成 1m bar 合成一根 partial daily bar 追加到响应末尾，返回 `data_source=hybrid`、`is_partial=true`、`last_live_bar_time`；非交易时段、收盘后、`include_realtime=false` 时不合成。partial daily bar 不写库，仅用于前端盘中展示。盘中监控（`monitor_batch_service`）与个股详情 K线均依赖 MDAS 的实时语义：前者用最新已完成 1m bar 作为算法输入，后者通过 1d partial bar 在页面展示盘中价格。

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
