# Database Model Map

## 1. 账户、权限、订阅

| 表 | 语义 |
|---|---|
| `users` | 用户身份和 active/disabled 状态 |
| `roles`, `user_roles` | admin/member 基础角色 |
| `plans` | 套餐和功能机器事实源 |
| `subscriptions` | 普通会员当前套餐、有效期、状态、权益快照 |
| `invite_codes`, `invite_redemptions` | 邀请码和兑换审计 |
| `access_audit_logs` | 管理员操作审计 |

## 2. 股票与行情

| 表 | 语义 |
|---|---|
| `instruments` | 股票主数据 |
| `trading_calendar` | 交易日和开闭市 |
| `bars_daily`, `bars_15min`, `bars_60min`, `bars_minute` | 已完成正式 Bar |

周线/月线由日线聚合，不作为独立业务源。partial Bar 不写入完成 Bar 表。

## 3. 策略与发布

| 表 | 语义 |
|---|---|
| `strategy_definitions` | 稳定策略身份 |
| `strategy_versions` | 不可变策略版本 |
| `strategy_runs` | 某策略版本某业务日期的一次运行 |
| `strategy_run_items` | 每股票运行 item，区分 succeeded/skipped/failed |
| `strategy_results` | 策略结果快照 |
| `strategy_result_metrics` | 可筛选/可排序指标 |

published run 不可变。partial_failed 不得自动发布。

## 4. 自选、监控、事件

| 表 | 语义 |
|---|---|
| `user_watchlist_items` | 用户自选和 active/软删除 |
| `monitor_states` | 当前监控状态快照 |
| `monitor_evaluations` | 策略版本、股票、源 Bar 的唯一评估 |
| `strategy_events` | 稳定事件 |
| `event_recipients` | 事件与有效用户收件人关系 |
| `stock_feature_snapshots` | 盘后特征快照（结构/时序因子 + 前端列表用 summary）；唯一键 `(instrument_id, trade_date, primary_timeframe, secondary_timeframe, adj, schema_version)`；3 个 btree 索引；无 GIN 索引；`/watchlist/monitor-status` 的 metrics 唯一来源 |
| `stock_feature_snapshot_runs` | snapshot 计算 run 级成功标记；唯一键 `(trade_date, schema_version, primary_timeframe, secondary_timeframe, adj, run_type) WHERE status='running'`（partial unique index）；3 个 btree 索引；watchlist 只读 `status='succeeded'` 的 run 对应日期 snapshot |
| `research_feature_matrix_runs` | 研究特征矩阵按月分批 run 级元数据；唯一键 `run_key`（如 `2026-01_full`）；2 个 btree 索引（`month`/`status`）；状态机 `running` → `succeeded`/`failed`；`metadata_json` 只放小摘要，不存完整 payload，不建 GIN 索引；与生产 snapshot 严格分离，不接入 watchlist |
| `research_feature_matrix_rows` | 研究特征矩阵扁平宽表，一只股票一个交易日的 33 个 feature 值；唯一键 `(instrument_id, trade_date)` 跨 run 幂等 upsert；3 个 btree 索引（`trade_date`/`instrument_id`/`run_id`）；不存 JSON payload，不建 GIN 索引；33 feature 列与 `feature_causality_registry.db_column()` 1:1 对应；总列数 39（5 metadata + 33 feature + 1 created_at） |

## 5. 消息、投递、截图

| 表 | 语义 |
|---|---|
| `notification_channels` | 用户通知渠道，Platform App only |
| `notification_messages` | 站内消息或外部通知内容实体 |
| `outbox` | 与业务事务一起写入的待处理事件 |
| `message_deliveries` | 某消息向某渠道的一次投递 |
| `capture_jobs` | 生成个股详情截图任务 |

文字和图片通过 message_group 关联，状态独立。

## 6. 任务与运行

| 表 | 语义 |
|---|---|
| `scheduler_job_runs` | 调度任务运行状态、租约、计数、错误 |
| `job_run_events` | 任务事件历史 |
| `worker_heartbeats` | Worker 实例、版本、最近心跳 |

生产审计发现 `worker_heartbeats` 可能残留 stale/running 僵尸记录，需要修复为可信状态源。
