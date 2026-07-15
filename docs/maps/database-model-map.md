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
| `instruments` | 股票主数据；**市值字段（CHANGE-20260713-010，migration 063）**：新增 `total_share`（BIGINT NULL，总股本，单位：股）、`float_share`（BIGINT NULL，流通股本，单位：股）、`share_as_of`（DATE NULL，股本数据日期，与 `total_share`/`float_share` 同步写入）；每日 18:00（Asia/Shanghai）由 `instrument_share_capital_sync_service.sync_share_capitals` 通过 `pytdx.get_finance_info` 同步 SH/SZ 股本（BJ 跳过），批次 500，`asyncio.to_thread` 包装阻塞调用；只保留最新态不做历史回填；quote 端点从 DB 读取股本 + 当前价格计算 `total_market_cap`/`float_market_cap`，禁止用户请求时第三方联网；数据缺失返回 `market_cap_degraded_reason="market_cap_data_unavailable"` 不伪造 |
| `trading_calendar` | 交易日和开闭市 |
| `bars_daily`, `bars_15min`, `bars_60min`, `bars_minute` | 已完成正式 Bar |
| `market_boards` | qstock 板块目录（行业/概念），只存最新态；字段：`id`/`external_code`/`name`/`type`/`updated_at`；唯一约束 `uq_market_boards_code_type (external_code, type)`；索引 `ix_market_boards_type`；migration 062 |
| `market_board_memberships` | 板块成分股关系，只存最新态；复合主键 `(board_id, instrument_id)`；字段含 `updated_at`；FK `board_id`→`market_boards.id`（CASCADE）/`instrument_id`→`instruments.id`（CASCADE）；索引 `ix_market_board_memberships_instrument`；migration 062 |

周线/月线由日线聚合，不作为独立业务源。partial Bar 不写入完成 Bar 表。

`market_boards`/`market_board_memberships` 只保存最新关系，不增加历史日期维度，不存板块行情/资金流。`/market/stocks` 的 `industry`/`concept` 筛选通过 `filter_instruments_by_board()` 查询 `market_boards` 表实现；未同步板块数据时筛选返回空列表。

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
| `strategy_events` | 稳定事件；`idempotency_key` 格式 `symbol:source_run_id:algorithm_version`（每只股票每个 run 至多一个事件；旧格式 `symbol:trade_date:algorithm_version:hash(evidence)` 已废弃） |
| `event_recipients` | 事件与有效用户收件人关系 |
| `stock_feature_snapshots` | 盘后特征快照（结构/时序因子 + 前端列表用 summary）；唯一键 `(instrument_id, trade_date, primary_timeframe, secondary_timeframe, adj, schema_version)`；3 个 btree 索引；无 GIN 索引；`/watchlist/monitor-status` 的 metrics 唯一来源；**`source_run_id` FK → `stock_feature_snapshot_runs.id`**（migration 061，nullable，仅新数据填写，不全量回填；`feature_snapshot_service.upsert_snapshot` ON CONFLICT DO UPDATE **更新 `source_run_id`**；`GET /stocks/{symbol}/context` 按 `source_run_id == run.id` 精确查询）；**索引**：ORM 模型 `stock_feature_snapshot.py` 已删除冗余单列索引 `ix_feature_snapshot_source_run_id`，仅保留组合索引 `ix_feature_snapshot_run_instrument(source_run_id, instrument_id)`（最左前缀已覆盖纯 `source_run_id` 查询，减少磁盘占用） |
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

## 5.5 用户偏好

| 表 | 语义 |
|---|---|
| `user_table_view_presets` | 用户表格视图配置（保存筛选/排序/列设置 preset）；唯一键用两个 partial unique index 实现：`uq_user_table_view_preset_strategy_not_null (user_id, table_id, strategy_key, name) WHERE strategy_key IS NOT NULL` + `uq_user_table_view_preset_strategy_null (user_id, table_id, name) WHERE strategy_key IS NULL`（解决 PostgreSQL NULL!=NULL 问题）；索引 `(user_id, table_id, strategy_key)` 用于查询和 quota 检查；config 为 JSONB 仅允许 keyword/sort/filters/hiddenColumns/pageSize（禁止 selectedKeys/page/activeRunId/rows；filters 每项 dict 含 key/op/value 且 op 限制白名单 contains/eq/gt/gte/lt/lte/between/empty/not_empty；hiddenColumns 每项 string；sort.key 非空 string）；每 user+table_id+strategy_key 最多 20 个（应用层 quota）；is_default 同维度至多 1 个 true（应用层互斥更新）；user_id 由认证上下文注入不接受 body 传入；migration 059 创建 |

## 6. 任务与运行

| 表 | 语义 |
|---|---|
| `scheduler_job_runs` | 调度任务运行状态、租约、计数、错误 |
| `job_run_events` | 任务事件历史 |
| `worker_heartbeats` | Worker 实例、版本、最近心跳 |

生产审计发现 `worker_heartbeats` 可能残留 stale/running 僵尸记录，需要修复为可信状态源。
