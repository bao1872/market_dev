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
| market_data | `api/bars.py`, `api/market.py`, `api/calendar.py`, `api/instruments.py` | `market_data_aggregation_service.py`, `bars_coverage_service.py`, `calendar_seed.py` | bar/instrument/calendar models & repositories | 页面、指标、截图必须同源；覆盖率统一由 `BarsCoverageService` 计算 |
| screening | `api/strategies.py`, `api/strategy_runs.py` | `strategy_batch_service.py`, strategy runtime | `strategy_*` models | 发布门禁关键模块；`StrategyLoader._registry` 仅注册 `dsa_selector` 与 `watchlist_monitor` |
| indicators | `api/indicators.py` | `indicator_service.compute_all_indicators` | `StrategyLoader`, `StrategyRuntime` | `watchlist_monitor` 内部委托 `BollingerMonitor`/`VolumeNodeMonitor`；旧 `bb_monitor`/`volume_node_monitor` 不再作为独立策略 key 注册 |
| watchlist | `api/watchlist.py` | watchlist service / limit logic / `MonitorSnapshotService` fallback | `user_watchlist_items` model | 到期权限和额度检查；无 MonitorState 或 payload 无效时 fallback，单只失败单行降级 |
| monitoring | `api/monitor_states.py`, `api/strategy_events.py` | monitor scheduler/services, eligible user | monitor/evaluation/event models | 只处理 completed 1m Bar |
| notifications | `api/notifications.py`, `api/stock_detail_feishu.py` | `outbox_relay.py`, `delivery_worker.py`, `stock_detail_feishu_service.py`, `channel_adapter.py`, `feishu_card_builder.py`, `message_builder.py` | notification/outbox/delivery models | 飞书、图文、重试；消息时间使用 `format_shanghai_datetime` |
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
