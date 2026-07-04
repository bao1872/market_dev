# 02 数据、API、权限与安全契约

## 1. 核心数据实体

| 领域 | 核心实体 |
|---|---|
| 账户权限 | `users`, `roles`, `user_roles`, `plans`, `subscriptions`, `invite_codes`, `access_audit_logs` |
| 股票行情 | `instruments`, `trading_calendar`, `bars_daily`, `bars_15min`, `bars_60min`, `bars_minute` |
| 策略发布 | `strategy_definitions`, `strategy_versions`, `strategy_runs`, `strategy_run_items`, `strategy_results`, `strategy_result_metrics` |
| 自选监控 | `user_watchlist_items`, `monitor_states`, `monitor_evaluations`, `strategy_events`, `event_recipients` |
| 消息投递 | `notification_channels`, `notification_messages`, `outbox`, `message_deliveries`, `capture_jobs` |
| 任务运行 | `scheduler_job_runs`, `job_run_events`, `worker_heartbeats` |

partial 实时 Bar 不写入完成 Bar 表，只存在于请求快照或短缓存。

strategy_run_items.reason_code 标准编码：
- failed: timeout（单股超时）、runtime_error、data_error、run_timeout_budget_exhausted（run 级总超时预算耗尽）
- skipped: insufficient_data、insufficient_history（历史日线 < 60 根）、suspended、delisted、new_listing

## 2. 权限契约

| API 类型 | 有效会员 | 到期/无订阅 | Admin |
|---|---:|---:|---:|
| `/me/access`, `/plans`, 续期 | 是 | 是 | 是 |
| 历史消息只读 | 是 | 是 | 是 |
| 趋势结果 | 是 | 否，403 | 是 |
| Watchlist 读写和状态 | 是 | 否，403 | 是 |
| 个股详情和行情研究 | 是 | 否，403 | 是 |
| 管理 API | 否 | 否 | 是 |

后端权限不能只靠前端隐藏。所有私有资源从 JWT 获取 user_id。

## 3. API 契约概要

| 能力 | 端点/路由组 | 关键规则 |
|---|---|---|
| Auth | `/auth`, `/me`, `/plans` | 登录、注册、刷新、AccessContext |
| 行情 | `/instruments`, `/calendar`, `/market`, `/bars` | 数据新鲜度、partial/degraded 标识；`/instruments/{id}/bars` page_size 按 timeframe 限制：`15m` 最大 4000，`1h` 最大 1200，其他最大 1000；`/instruments/{id}/indicators` 的 `bars` 参数最大 4000 |
| 策略 | `/strategies`, `/strategy-runs` | 只读 released/published 结果；`/strategy-runs/{run_id}/results` 以 `strategy_run_items` 为主表 LEFT JOIN `strategy_results` + `instruments`，返回全量 universe（含 succeeded/skipped/failed），skipped/failed 行 `id`/`payload` 为 null；新增 `item_status`/`reason_code`/`error_message` 字段；默认无筛选时 `source_total = run.total_instruments`。JOIN 策略：因 `strategy_run_items.result_id` 当前未回填（ALIGN-033 P2），`strategy_results` 关联统一改用 `(run_id, instrument_id)`，包括批量加载、metric_filter 子查询、sort LEFT JOIN 三处 |
| 监控 | `/monitor-states`, `/strategy-events` | 只处理完成 Bar，按用户资格过滤 |
| 通知 | `/messages`, `/notification-channels` | 用户只能操作自己的消息和渠道 |
| 自选 | `/watchlist` | active subscription + monitor_limit |
| 个股详情分享 | `/stock-detail-feishu` | target_channel_id 支持手动指定渠道 |
| Capture | `/api/v1/capture/*` | 只接受 Capture Token |
| Admin | `/admin/*` | Admin 角色 + 审计；含 `GET /admin/worker-heartbeats` 只读心跳视图（health_state 后端计算：fresh<120s / stale 120-600s / stopped≥600s 或 status=stopped） |
| Metrics | `/metrics` | Prometheus 指标，无需认证 |

## 4. Capture Token 契约

Capture Token 是截图 worker 专用短期 JWT，与普通 Access Token 严格隔离。

- Capture Token 只能访问 `/api/v1/capture/*`；
- 普通 Access Token 不能访问 Capture API；
- Capture Token 不能访问普通 API；
- 前端使用独立 `CAPTURE_TOKEN_KEY` 和 `captureClient`；
- path `instrument_id` 必须与 token 中 `instrument_id` 一致；
- scope 必须是 `stock_detail_capture`。

## 5. 飞书渠道契约

- 唯一 adapter_type：`feishu_platform_app`；
- `feishu_webhook` 已永久删除；
- 每个用户最多一个 active `feishu_platform_app` 渠道；
- 管理员通知复用管理员自己的 active Platform App 渠道；
- 系统不维护独立管理员飞书 Webhook 或凭据。

## 6. 数据生命周期

- 发布批次不可变；
- released StrategyVersion 不可变；
- 历史 message/delivery/capture job 不覆盖；
- soft delete 不等于业务可用；
- 恢复数据时重新校验权限和额度；
- Alembic 是唯一 DDL 事实源，已执行历史 migration 不修改，只新增前向 migration。

## 7. 行情覆盖率口径

全市场 `bars_daily` 覆盖率计算统一由 `app.services.bars_coverage_service.BarsCoverageService` 提供，禁止在 Service/API/Worker 中复制 SQL。

- 分子：指定 `trade_date` 当日 `bars_daily` 中不同 `instrument_id` 数，JOIN `instruments` 并应用 `stock_symbol_sql_filter`，排除指数/基金/ETF 残留数据；
- 分母：`instruments` 中 `status='active'` 且为 A 股股票的标的数；
- 默认日期使用 `shanghai_business_date()`（Asia/Shanghai），不使用服务器本地 `date.today()`；
- 返回结构：`{trade_date, covered, total, coverage, coverage_raw, source}`，其中 `source='bars_daily'`；
  - `coverage`：`round(coverage_raw, 4)`，仅用于展示；
  - `coverage_raw`：`covered / total` 原始值，所有覆盖率门禁/阈值判断必须使用 `coverage_raw`，避免四舍五入边缘误判；
- `/admin/after-close-runs/dsa-only`、`bars_scheduler`、系统概览 `WAITING_DSA` 判定等覆盖率门禁统一使用 `coverage_raw`；
- `/admin/after-close-runs/dsa-only` 在请求日期当日无数据时，fallback 到最新已落盘交易日（`get_latest_trade_date`），覆盖率仍不足时返回 409 `DATA_COVERAGE_INSUFFICIENT`。

## 8. 时间展示与时区

- 数据库存储：UTC + TIMESTAMPTZ；
- 业务日期与调度判断：Asia/Shanghai；
- API、消息、日志展示：Asia/Shanghai，统一使用 `app.core.time.format_shanghai_datetime`；
- 飞书消息 `data_time` 与触发时间均显示 CST，不再出现 `+00:00` 或 UTC 时间。
