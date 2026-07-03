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
| 行情 | `/instruments`, `/calendar`, `/market`, `/bars` | 数据新鲜度、partial/degraded 标识 |
| 策略 | `/strategies`, `/strategy-runs` | 只读 released/published 结果 |
| 监控 | `/monitor-states`, `/strategy-events` | 只处理完成 Bar，按用户资格过滤 |
| 通知 | `/messages`, `/notification-channels` | 用户只能操作自己的消息和渠道 |
| 自选 | `/watchlist` | active subscription + monitor_limit |
| 个股详情分享 | `/stock-detail-feishu` | target_channel_id 支持手动指定渠道 |
| Capture | `/api/v1/capture/*` | 只接受 Capture Token |
| Admin | `/admin/*` | Admin 角色 + 审计 |
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
