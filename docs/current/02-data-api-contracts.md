# 02 数据、API、权限与安全契约

## 1. 核心数据实体

| 领域 | 核心实体 |
|---|---|
| 账户权限 | `users`, `roles`, `user_roles`, `plans`, `subscriptions`, `invite_codes`, `access_audit_logs` |
| 股票行情 | `instruments`, `trading_calendar`, `bars_daily`, `bars_15min`, `bars_60min`, `bars_minute` |
| 策略发布 | `strategy_definitions`, `strategy_versions`, `strategy_runs`, `strategy_run_items`, `strategy_results`, `strategy_result_metrics` |
| 自选监控 | `user_watchlist_items`, `monitor_states`, `monitor_evaluations`, `strategy_events`, `event_recipients`, `stock_feature_snapshots`, `stock_feature_snapshot_runs` |
| 消息投递 | `notification_channels`, `notification_messages`, `outbox`, `message_deliveries`, `capture_jobs` |
| 任务运行 | `scheduler_job_runs`, `job_run_events`, `worker_heartbeats` |
| 用户偏好 | `user_table_view_presets` |

partial 实时 Bar 不写入完成 Bar 表，只存在于请求快照或短缓存。

### 个股详情 K线实时契约

- `/quote` 实时 **只代表顶部行情卡片实时**，不等价于 K线实时；
- `/bars?timeframe=1d&include_realtime=true` 是个股详情 1d K线实时的唯一后端契约；
- 交易时段（`MORNING_SESSION`/`AFTERNOON_SESSION`）内，`/bars?timeframe=1d&include_realtime=true` 必须返回今日 partial daily bar：
  - `data_source=hybrid`
  - `is_partial=true`
  - `last_live_bar_time` 非空
  - 最后一根 bar 日期为今日
  - close 来自最新已完成 1m bar；
- `MarketDataAggregationService` 调用 `pytdx_adapter.get_minute_bars` 拉取 live 1m 时，`start_time` 与 `end_time` 必须同为 `Asia/Shanghai` aware datetime，禁止 naive/aware 混用；`pytdx_adapter.get_minute_bars` 内部将 aware 输入按 `Asia/Shanghai` 解释后转为 naive，再与 pytdx 返回的 naive `datetime` 列比较过滤；该 live 1m 能力是 `/bars` 实时展示与 `worker-monitor` 的共同依赖，但各自业务链路保持分离；
- 1d partial daily bar 仅作为页面响应快照，不写入完成 Bar 库表；
- 收盘后或非交易时段，`/bars?timeframe=1d` 不得伪装实时：
  - `is_partial=false`
  - 最后一根为完整日线
  - `/quote` 可为 `daily_fallback`；
- 前端 `mergeRealtimeQuoteIntoBars()` 只能作为兜底视觉增强：当且仅当后端 `/bars` 未返回 `is_partial=true` 时才允许合并 quote；**后端已返回 partial bar 时，前端不得用 quote 覆盖**；不参与指标计算，不写入库。

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
| 表格视图配置（`/me/table-view-presets`） | 是 | 否，403 | 是 |
| 管理 API | 否 | 否 | 是 |

后端权限不能只靠前端隐藏。所有私有资源从 JWT 获取 user_id。

## 3. API 契约概要

| 能力 | 端点/路由组 | 关键规则 |
|---|---|---|
| Auth | `/auth`, `/me`, `/plans` | 登录、注册、刷新、AccessContext |
| 行情 | `/instruments`, `/calendar`, `/market`, `/bars` | 数据新鲜度、partial/degraded 标识；`/instruments/{id}/bars` page_size 按 timeframe 限制：`15m` 最大 4000，`1h` 最大 1200，其他最大 1000；`/instruments/{id}/indicators` 的 `bars` 参数最大 4000；`indicators` 响应含 `sqzmom_lb` 全局技术指标数据，后端逐行复刻 Pine 代码，前端只渲染不计算；**`GET /market/stocks` 行业/概念筛选**（PRD §7.5 qstock 同步后）：`industry`/`concept` 参数按板块名称筛选，通过 `filter_instruments_by_board()` 查询 `market_boards` 表实现；未同步板块数据时返回空列表（不报 422）；`industry` + `concept` 同时传时取交集（AND 语义）；**`GET /market/boards?type=industry\|concept`** 返回 `MarketBoardsResponse`：`items`（板块目录列表）+ `available: bool`（items 非空时 true）+ `reason_code: str \| null`（无数据时为 `"board_provider_unavailable"`）+ `updated_at`；前端依据 `available` 决定筛选输入是否禁用 |
| 结构状态因子 | `/instruments/{id}/structural-factors` | 双周期（1d+15m）5 组结构因子（DSA 段/Swing/成本节点/动量波动/成交参与）；前端只渲染后端 DTO，禁止重新计算；无认证要求（与 indicators API 一致）；250-500 bar lookback，15m 仅已完成 bar，Swing 仅已确认 pivot（无未来函数） |
| 策略 | `/strategies`, `/strategy-runs` | 只读 released/published 结果；`/strategy-runs/{run_id}/results` 以 `strategy_run_items` 为主表 LEFT JOIN `strategy_results` + `instruments`，返回全量 universe（含 succeeded/skipped/failed），skipped/failed 行 `id`/`payload` 为 null；新增 `item_status`/`reason_code`/`error_message` 字段；默认无筛选时 `source_total = run.total_instruments`。JOIN 策略：因 `strategy_run_items.result_id` 当前未回填（ALIGN-033 P2），`strategy_results` 关联统一改用 `(run_id, instrument_id)`，包括批量加载、metric_filter 子查询、sort LEFT JOIN 三处。**keyword 搜索（CHANGE-20260713-005）**：`strategy_result_repository.query_results` 的 `keyword` 参数（非空时）ILIKE 同时匹配 `Instrument.symbol`/`Instrument.name`/`Instrument.pinyin_initials`（3 处 or_ 分支同步，支持股票代码/中文名称/拼音首字母）；前端不做全量过滤，不增加新表；`total` 字段为该 keyword + filters 下的真实总数（不是 items.length）。**industry/concept 筛选（CHANGE-20260713-006）**：`/strategy-runs/{run_id}/results` 支持 `industry`（str \| None, Query）和 `concept`（str \| None, Query）参数，按行业/概念板块名称筛选，通过共享 `backend/app/repositories/board_filter_helper.py::build_board_filter_conditions` 构造 EXISTS 子查询（`MarketBoardMembership` JOIN `MarketBoard`，`type='industry'`/`'concept'`，`name` 匹配）；industry+concept 同时提供时为 AND 语义；`items`/`filtered_total`/`source_total` 三处同步应用相同条件 |
| 监控 | `/monitor-states`, `/strategy-events` | 只处理完成 Bar，按用户资格过滤；monitor_event 在 `delivery_worker.py` 投递前再次用 `is_user_eligible_for_monitor` 复核，active admin 放行，disabled admin / 无订阅普通用户排除 |
| 个股上下文 | `/stocks/{symbol}/context` | 用户面个股状态与事件聚合；Evidence DTO 从 ORM `event.evidence` 映射；时区 `Asia/Shanghai`；历史事件截止为次日 00:00 exclusive；run 查询按 `trade_date, published_at, finished_at` DESC 确定排序；`strategy_events.idempotency_key` 格式 `symbol:source_run_id:algorithm_version`（每只股票每个 run 至多一个事件） |
| 通知 | `/messages`, `/notification-channels` | 用户只能操作自己的消息和渠道 |
| 自选 | `/watchlist` | active subscription + monitor_limit |
| 表格视图配置 | `/me/table-view-presets` | 用户表格视图配置 CRUD；JWT user_id 隔离；active subscription + trend_selection feature（admin 豁免）；config 仅允许 keyword/sort/filters/hiddenColumns/pageSize；每 user+table_id+strategy_key 最多 20 个；`(user_id, table_id, strategy_key, name)` 唯一约束用两个 partial unique index 实现（strategy_key IS NOT NULL / IS NULL 分离，解决 NULL!=NULL 问题）；is_default 同维度互斥；**写操作（POST/PATCH/DELETE）必须在返回前 `await db.commit()`，异常分支 `rollback` 后 re-raise，禁止吞异常；写后读跨请求必须可见** |
| 个股详情分享 | `/stock-detail-feishu` | target_channel_id 支持手动指定渠道 |
| Capture | `/api/v1/capture/*` | 只接受 Capture Token |
| Admin | `/admin/*` | Admin 角色 + 审计；含 `GET /admin/worker-heartbeats` 只读心跳视图（health_state 后端计算：fresh<120s / stale 120-600s / stopped≥600s 或 status=stopped）；新增盘后流水线聚合状态端点 `/admin/after-close/pipeline/latest`、`/admin/after-close/pipeline?trade_date=`、`/admin/after-close/pipeline/runs?limit=`、`POST /admin/after-close/pipeline/run`（admin，幂等），响应 `AfterClosePipelineResponse` 含 8 步骤时间线 + watchlist_ready 严格判定 + data_freshness + 最近 100 条 events |
| Metrics | `/metrics` | Prometheus 指标，无需认证 |

## 4. Capture Token 契约

Capture Token 是截图 worker 专用短期 JWT，与普通 Access Token 严格隔离。

- Capture Token 只能访问 `/api/v1/capture/*`；
- 普通 Access Token 不能访问 Capture API；
- Capture Token 不能访问普通 API；
- 前端使用独立 `CAPTURE_TOKEN_KEY` 和 `captureClient`；
- path `instrument_id` 必须与 token 中 `instrument_id` 一致；
- scope 必须是 `stock_detail_capture`。

### 4.1 Capture Snapshot 端点与截图请求契约

截图 worker 通过 `POST /capture`（capture worker）发起截图，最终由前端 `CaptureStockPage` 调用后端 `GET /api/v1/capture/stocks/{instrument_id}/snapshot` 取数（Capture 专用链路，仅供截图 worker 通过 Capture Token 访问）。

#### 4.1.1 Capture Snapshot 端点（`GET /api/v1/capture/stocks/{instrument_id}/snapshot`）

- 认证：Capture Token（`get_capture_token_payload`），path `instrument_id` 必须与 token 中 `instrument_id` 一致，否则 403；
- 查询参数：
  - `timeframe`：截图 K线周期（1d|15m|1h|1w|1mo），默认 1d；
  - `source_bar_time`：实时 bar 时间（仅用于日志/cache key，防旧图）；
  - `force_refresh` / `capture`：跳过 indicator Redis 读缓存但写回最新（等价实时计算，截图链路默认 True）；
- 始终 `include_realtime=True`，保证盘中 K线为当前实时数据；`bars_limit` 按 `indicator_contract.INDICATOR_BARS[timeframe]` 对齐（1d=250、15m=4000、1h=1200、1w=260、1mo=120）；
- 硬规则：URL `timeframe` 必须原样透传给 `get_bars(timeframe=...)`、`_df_to_responses(df, instrument_id, timeframe)`、`compute_all_indicators(timeframe=..., bars=INDICATOR_BARS[timeframe])` 三处，禁止内部回退常量 `_CAPTURE_TIMEFRAME`（1d）；`include_realtime=True` 为截图链路不可绕过的硬编码硬规则（非可配置开关），禁止改为 `False`；
- 业务边界（CHANGE-20260710-002）：Capture Snapshot API 支持多周期是**能力**，不等于飞书业务默认多周期；业务调用方（手动飞书分享 `stock_detail_feishu_service`、自动盘中监控 `_send_chart_images_via_outbox`）默认传 `timeframe=1d`；`15m` 只用于显式请求 / 调试 / 未来策略声明，不得成为 watchlist_monitor 飞书业务默认周期；截图清晰度/缓存修复不得改变 `watchlist_monitor` 事件计算口径。
- 响应一次返回 `instrument` / `bars` / `indicators` / `events` / `snapshot_time`，其中 `last_live_bar_time` / `is_partial` / `data_source` 只存在于 `bars`（与后端 `BarListResponse` schema 一致），前端实时状态必须从 `snapshot.bars` 读取。

#### 4.1.2 截图请求/响应字段（capture worker `POST /capture`）

- `CaptureRequest` 新增透传字段：`timeframe`、`source_bar_time`、`capture_run_id`、`disable_cache`、`viewport_width`、`viewport_height`、`device_scale_factor`；
- `CaptureResponse` 新增元数据：`width`、`height`、`device_scale_factor`、`cache_hit`；
- `disable_cache=True` 跳过读文件缓存但允许写最新缓存（飞书实时截图默认 True），确保不复用上一轮旧图/旧指标。

### 4.2 高清截图渲染参数

capture worker 浏览器上下文使用 `viewport=1920x1200` + `device_scale_factor=2`（env `CAPTURE_VIEWPORT_WIDTH/HEIGHT` / `CAPTURE_DEVICE_SCALE_FACTOR`，默认 1920/1200/2，严禁 4 倍），提升 PNG 清晰度。截图为单张、不落库、不存 base64。

## 5. 飞书渠道契约

- 唯一 adapter_type：`feishu_platform_app`；
- `feishu_webhook` 已永久删除；
- 每个用户最多一个 active `feishu_platform_app` 渠道；
- 管理员通知复用管理员自己的 active Platform App 渠道；
- 系统不维护独立管理员飞书 Webhook 或凭据；
- `receive_id_type` 由前端表单选择后原样透传给飞书接口，支持 `user_id`/`open_id`/`chat_id`/`union_id`；
- `POST /notification-channels/{channel_id}/test-latest-event` 仅管理员可用，普通用户调用返回 403，detail 提示「最近事件实测仅管理员可用，普通用户请使用发送测试消息」；
- `POST /notification-channels/{id}/test` 对所有用户可用，发送测试消息并返回 `delivery.success`/`error_code`/`error_message`；测试成功后渠道状态变为 `active`。

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

## 9. SQZMOM_LB 技术指标契约

`/api/v1/instruments/{instrument_id}/indicators` 在全局技术指标区返回 `sqzmom_lb`（Squeeze Momentum Indicator [LazyBear]），由后端 `app.strategy_assets.algorithms.features.sqzmom_lb.compute_sqzmom_lb` 逐行复刻 TradingView Pine 代码，前端只消费后端 DTO，不重新计算。

### 9.1 Pine 等价约束

- `source = close`；
- `basis = sma(source, length)`；
- `dev = multKC * stdev(source, length)`：保持 Pine 原脚本逻辑，不修正为 `mult * stdev`；
- `ma = sma(source, lengthKC)`；
- `range = useTrueRange ? tr : high - low`；
- `rangema = sma(range, lengthKC)`；
- `upperKC = ma + rangema * multKC`，`lowerKC = ma - rangema * multKC`；
- `sqzOn = lowerBB > lowerKC and upperBB < upperKC`；
- `sqzOff = lowerBB < lowerKC and upperBB > upperKC`；
- `noSqz = not sqzOn and not sqzOff`；
- `val = linreg(source - avg(avg(highest(high, lengthKC), lowest(low, lengthKC)), sma(close, lengthKC)), lengthKC, 0)`；
- `sma` 为简单移动平均，`stdev` 使用 Pine 默认 `ddof=0`（有偏标准差）；
- `tr` 第一根按 `high - low` 处理，不导致整列异常；
- `linreg offset=0` 返回窗口内当前 bar 的回归值，即 `intercept + slope * (length - 1)`。

### 9.2 默认参数

| 参数 | 值 |
|---|---|
| length | 20 |
| mult | 2.0 |
| lengthKC | 20 |
| multKC | 1.5 |
| useTrueRange | true |

### 9.3 输出字段

`data.sqzmom_lb` 包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `val` | `list[float \| None]` | 动量柱状值 |
| `sqzOn` | `list[bool]` | Squeeze 开启 |
| `sqzOff` | `list[bool]` | Squeeze 释放 |
| `noSqz` | `list[bool]` | 无 Squeeze |
| `bcolor` | `list[str]` | 柱状颜色；`val > 0` 时 `val > nz(val[1])` 为 `lime` 否则 `green`，`val < 0` 时 `val < nz(val[1])` 为 `red` 否则 `maroon`；前值为 `na` 时按 0 处理 |
| `scolor` | `list[str]` | 零轴 squeeze marker 颜色；`noSqz` 为 `blue`，`sqzOn` 为 `black`，否则 `gray` |
| `time` | `list[str]` | 与当前 timeframe bar 对齐的 ISO 时间序列 |
| `params` | `dict` | 当前实际参数：`length`、`mult`、`lengthKC`、`multKC`、`useTrueRange`、`bb_dev_uses` |

`layers` 中新增 `strategy_id=sqzmom_lb`、`renderer=sqzmom`、`pane=sqzmom` 的图层定义，默认不加入任何策略的 `defaultLayers`，由用户手动开启。

### 9.4 限制

- SQZMOM_LB 不接入选股、监控、飞书、消息中心、事件系统；
- 不新增数据库表；
- 不改 DSA VWAP、筹码峰、K 线实时行情合并逻辑。

## 10. 结构状态因子 API 契约 V1.8

`GET /api/v1/instruments/{instrument_id}/structural-factors` 返回双周期（默认 1d + 15m）5 组结构状态因子（V1.8 约 50 字段），由后端 `app.services.structural_factor_service.compute_structural_factors` 统一计算，前端只消费 DTO，不重新计算。无认证要求（与 indicators API 一致）。

### 10.1 查询参数

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `primary_timeframe` | str | `1d` | 主周期；允许 `1d/15m/1h/1w/1mo` |
| `secondary_timeframe` | str | `15m` | 副周期；允许同上 |
| `adj` | str | `qfq` | 复权方式 |
| `as_of` | str | `latest` | 截止时间（当前仅支持 `latest`） |

### 10.2 响应结构

```json
{
  "primary": { "<primary_timeframe>": { 5 factor groups } | null },
  "secondary": { "<secondary_timeframe>": { 5 factor groups } | null },
  "relation": {
    "primary_dir": int|null,
    "secondary_dir": int|null,
    "trend_alignment": "aligned"|"divergent"|null,
    "primary_swing_position": float|null,
    "secondary_swing_position": float|null,
    "primary_slope_atr": float|null,
    "secondary_slope_atr": float|null,
    "secondary_vs_primary_position_delta": float|null,
    "notes": [str]
  },
  "meta": {
    "as_of": "ISO time",
    "primary_lookback_bars": 250,
    "secondary_lookback_bars": 500,
    "degraded_reasons": [str],
    "warmup_notes": [str]
  }
}
```

### 10.3 五组结构因子 V1.8 完整字段

#### dsa_segment（DSA 段质量）

**V1.7 保留字段**：`segment_id`/`segment_dir`/`segment_start_price`/`segment_start_bar_index`/`age_bars`/`segment_extents_pct`

**V1.8 基础字段**：
- `dsa_value` = 当前 bar DSA VWAP（`factor_per_bar["dsa_vwap"].iloc[-1]`）
- `price_vs_dsa_atr` = `(close - dsa_value) / last_atr`

**V1.8 当前段字段**：
- `current_dsa_segment_id` = `factor_per_bar["regime_id"].iloc[-1]`
- `current_dsa_segment_dir` = `visual_segments[-1].direction`
- `current_dsa_segment_age_bars` = `last_bar_index - current_start_bar_index + 1`
- `current_dsa_segment_return_pct` = `close_last / current_start_price - 1`（基于 close，不用 dsa_vwap）
- `current_dsa_segment_slope_pct_per_bar` = `current_return_pct / current_age_bars`
- `current_dsa_segment_slope_atr_per_bar` = `(close_last - current_start_price) / (mean(ATR over segment) * age_bars)`
- `current_dsa_segment_efficiency_0_1` = `abs(close_last - start_price) / sum(abs(diff(close_i)))`（段内路径效率，[0,1]）
- `current_segment_volume_sum` = `sum(volume over segment bars)`

**V1.8 前一段字段**（无 prev 段时为 null）：
- `prev_dsa_segment_dir`、`prev_dsa_segment_age_bars`、`prev_dsa_segment_return_pct`、`prev_dsa_segment_slope_pct_per_bar`、`prev_dsa_segment_slope_atr_per_bar`、`prev_dsa_segment_efficiency_0_1`、`prev_segment_volume_sum`

**V1.8 段间对比字段**（prev 为 null 时为 null）：
- `segment_return_abs_ratio` = `abs(current_return_pct) / abs(prev_return_pct)`
- `segment_slope_abs_ratio` = `abs(current_slope_atr_per_bar) / abs(prev_slope_atr_per_bar)`
- `segment_duration_ratio` = `current_age_bars / prev_age_bars`
- `segment_efficiency_delta` = `current_efficiency - prev_efficiency`
- `current_vs_prev_volume_ratio` = `current_segment_volume_sum / prev_segment_volume_sum`
- `current_segment_return_per_volume` = `current_return_pct / current_segment_volume_sum`
- `prev_segment_return_per_volume` = `prev_return_pct / prev_segment_volume_sum`
- `return_per_volume_ratio` = `current_return_per_volume / prev_return_per_volume`
- `volume_per_1pct_return` = `current_segment_volume_sum / abs(current_return_pct * 100)`

**V1.9 DSA age 统一口径**：
- `age_bars` = `current_dsa_segment_age_bars`（含起始 bar，+1 口径，即 `last_bar_index - current_start_bar_index + 1`）；
- V1.7 `age_bars` 与 V1.8 `current_dsa_segment_age_bars` 现在统一为同一值，不再相差 1 或 2；
- `segment_duration_ratio` 等段间对比字段统一使用该 +1 口径 age_bars。

#### swing_position（Swing 结构位置）

**V1.7 保留字段**：`confirmed_swing_high`/`confirmed_swing_low`/`bars_since_swing_high`/`bars_since_swing_low`/`swing_high_to_close_pct`/`swing_low_to_close_pct`

**V1.8 新增字段**：
- `swing_range` = `confirmed_swing_high - confirmed_swing_low`
- `price_position_in_swing_0_1` = `(close - confirmed_swing_low) / swing_range`
- `distance_to_swing_high_atr` = `(close - confirmed_swing_high) / last_atr`
- `distance_to_swing_low_atr` = `(close - confirmed_swing_low) / last_atr`
- `retracement_from_high_0_1` = `(confirmed_swing_high - close) / swing_range`
- `rebound_from_low_0_1` = `(close - confirmed_swing_low) / swing_range`

注：`swing_range <= 0` 时所有比例字段为 null；`retracement_from_high_0_1` 与 `price_position_in_swing_0_1` 互为补数（和=1）。

**V1.9 新增字段**（active swing + confirmed pivot 别名，修复强上涨段 `price_position_in_swing_0_1` 突破 1 的语义问题）：

confirmed pivot 别名字段：
- `bars_since_confirmed_swing_high` = 距 confirmed swing high 的 bar 数（与 `bars_since_swing_high` 同义，统一命名）
- `bars_since_confirmed_swing_low` = 距 confirmed swing low 的 bar 数（与 `bars_since_swing_low` 同义，统一命名）
- `price_position_in_confirmed_swing_raw` = `(close - confirmed_swing_low) / (confirmed_swing_high - confirmed_swing_low)`（**不 clip**，可 <0 或 >1，用于突破状态判断）
- `confirmed_swing_breakout_state` = close 相对 confirmed 区间的位置分类：
  - `inside`：`confirmed_swing_low <= close <= confirmed_swing_high`
  - `above_confirmed_high`：`close > confirmed_swing_high`
  - `below_confirmed_low`：`close < confirmed_swing_low`
  - `null`：confirmed_swing_high 或 confirmed_swing_low 缺失

active swing 字段（反映当前正在发展的结构区间，clip 到 [0,1]，避免强趋势段位置越界）：
- `active_swing_dir` = 当前 active leg 方向：`1` = up leg（高点待更新），`-1` = down leg（低点待更新），`None` = fallback（无 confirmed pivot 时使用最近 120 根 bar 的 high/low）
- `active_swing_high` = active 区间高点（up leg 时为最近 confirmed swing high，down leg 时为最新跟踪的最高点）
- `active_swing_low` = active 区间低点（down leg 时为最近 confirmed swing low，up leg 时为最新跟踪的最低点）
- `bars_since_active_swing_high` = 距 active swing high 的 bar 数
- `bars_since_active_swing_low` = 距 active swing low 的 bar 数
- `active_swing_range` = `active_swing_high - active_swing_low`
- `price_position_in_active_swing_raw` = `(close - active_swing_low) / active_swing_range`（不 clip，用于诊断）
- `price_position_in_active_swing_0_1` = `clip((close - active_swing_low) / active_swing_range, 0, 1)`（**clip 到 [0,1]**，前端摘要卡唯一位置字段）
- `distance_to_active_swing_high_atr` = `(close - active_swing_high) / last_atr`
- `distance_to_active_swing_low_atr` = `(close - active_swing_low) / last_atr`
- `active_retracement_from_high_0_1` = `(active_swing_high - close) / active_swing_range`
- `active_rebound_from_low_0_1` = `(close - active_swing_low) / active_swing_range`

**V1.9 位置语义说明**：
- confirmed pivot 位置（`price_position_in_confirmed_swing_raw`）保留 raw 值（可 <0 或 >1），用于突破状态判断，**不作为前端摘要卡位置字段**；
- active swing 位置（`price_position_in_active_swing_0_1`）clip 到 [0,1]，反映当前正在发展的结构区间，**是前端 Swing 摘要卡和 Temporal `m15_position_relative_to_daily` 的唯一位置字段**；
- `active_swing_range <= 0` 或 active high/low 缺失时所有 active 比例字段为 null；
- fallback 模式（无 confirmed pivot）使用最近 120 根 bar 的 high/low 作为 active swing high/low，`active_swing_dir=None`。

**V1.10 新增字段**（developing swing，反映"当前正在发生的回落/反弹结构"）：

active major leg 描述从 confirmed pivot anchor 到当前价格的整段区间；developing swing 在 active major leg 基础上进一步细化，用于时序特征卡片展示当前 developing 结构，**不作为 Swing 摘要卡或 Temporal derived_relation 字段**。

developing swing 字段：
- `developing_swing_dir` = 当前 developing 结构方向：
  - `1` = 仍在创新高（major up leg 且 active_high 在最新 bar）/ 反弹中（major down leg 且 active_low 之前的 bar 中）
  - `-1` = 回落中（major up leg 且 active_high 之前的 bar 中）/ 仍在创新低（major down leg 且 active_low 在最新 bar）
  - `None` = fallback（无 confirmed pivot，developing=active）
- `developing_swing_high` = developing 区间高点
- `developing_swing_low` = developing 区间低点
- `developing_swing_high_bar_index` = developing high 在序列中的 bar index
- `developing_swing_low_bar_index` = developing low 在序列中的 bar index
- `bars_since_developing_swing_high` = 距 developing swing high 的 bar 数
- `bars_since_developing_swing_low` = 距 developing swing low 的 bar 数
- `developing_swing_range` = `developing_swing_high - developing_swing_low`
- `price_position_in_developing_swing_raw` = `(close - developing_swing_low) / developing_swing_range`（不 clip，用于诊断）
- `price_position_in_developing_swing_0_1` = `clip((close - developing_swing_low) / developing_swing_range, 0, 1)`（**clip 到 [0,1]**，时序特征卡片 developing 位置字段）
- `distance_to_developing_swing_high_atr` = `(close - developing_swing_high) / last_atr`
- `distance_to_developing_swing_low_atr` = `(close - developing_swing_low) / last_atr`
- `developing_retracement_from_high_0_1` = `(developing_swing_high - close) / developing_swing_range`
- `developing_rebound_from_low_0_1` = `(close - developing_swing_low) / developing_swing_range`

developing swing 计算规则（依据 active major leg 方向和 active high/low bar 位置）：
1. **major up leg 且 active_high_bar_index < current_idx**（已从高点回落）：
   - `developing_swing_dir = -1`
   - `developing_swing_high = active_swing_high`（保留 active high）
   - `developing_swing_low = min(lows[active_high_bar_index:now])`（从 active high 起回落段的最低 low）
2. **major up leg 且 active_high_bar_index == current_idx**（仍在创新高）：
   - `developing_swing_dir = 1`
   - `developing_swing_high = active_swing_high`
   - `developing_swing_low = active_swing_low`
3. **major down leg 且 active_low_bar_index < current_idx**（已从低点反弹）：
   - `developing_swing_dir = 1`
   - `developing_swing_low = active_swing_low`（保留 active low）
   - `developing_swing_high = max(highs[active_low_bar_index:now])`（从 active low 起反弹段的最高 high）
4. **major down leg 且 active_low_bar_index == current_idx**（仍在创新低）：
   - `developing_swing_dir = -1`
   - `developing_swing_high = active_swing_high`
   - `developing_swing_low = active_swing_low`
5. **fallback**（无 confirmed pivot，active_dir=None）：developing = active，`developing_swing_dir = None`

**V1.10 位置语义说明**：
- confirmed pivot 位置（`price_position_in_confirmed_swing_raw`）保留 raw 值（可 <0 或 >1），用于突破状态判断，**不作为前端摘要卡位置字段**；
- active swing 位置（`price_position_in_active_swing_0_1`）clip 到 [0,1]，反映当前正在发展的结构区间，**是前端 Swing 摘要卡和 Temporal `m15_position_relative_to_daily` 的唯一位置字段**；
- developing swing 位置（`price_position_in_developing_swing_0_1`）clip 到 [0,1]，反映"当前正在发生的回落/反弹结构"，**用于时序特征卡片展示 developing 结构，不作为 Swing 摘要卡或 Temporal `m15_position_relative_to_daily` 字段**；
- `developing_swing_range <= 0` 或 developing high/low 缺失时所有 developing 比例字段为 null；
- Temporal `m15_position_relative_to_daily = m15_price_position_in_active_swing_0_1 - daily_price_position_in_active_swing_0_1`，任一缺失返回 null，**不回退 confirmed raw**。

#### cost_position（成本/节点）

**V1.7 保留字段**：`poc_price`/`nearest_upper_node`/`nearest_lower_node`/`position_0_1`/`close_to_poc_pct`

**V1.8 新增字段**：
- `price_vs_poc_atr` = `(close - poc_price) / last_atr`
- `value_area_position_0_1` = `(close - val_price) / (vah_price - val_price)`（不 clip，可超出 [0,1]）
- `nearest_node_above_price` = `upper_node.price_mid`（无则 null）
- `nearest_node_below_price` = `lower_node.price_mid`（无则 null）
- `distance_to_node_above_atr` = `(close - node_above_price) / last_atr`
- `distance_to_node_below_atr` = `(close - node_below_price) / last_atr`
- `node_above_strength` = 从 `peak_df` 按 `price_mid` 查找 `total_volume`（无则 null）
- `node_below_strength` = 同上

**V1.8 位置语义修复字段**（区分 VP 全区间 / 节点区间 / VA 区间，避免误读）：
- `val_price` = `vp_result.val_price`（Value Area Low 原值，无则 null）
- `vah_price` = `vp_result.vah_price`（Value Area High 原值，无则 null）
- `position_0_1` = `vp_result.position_0_1(last_close)`（**VP 全价格范围 lowest~highest 中的位置**，保持原语义不 clip；前端标签改为「VP全区间位置[0,1]」避免与节点区间位置混淆）
- `node_interval_position_0_1` = `clip((close - lower) / (upper - lower), 0, 1)`（**节点区间位置**：close 在 [nearest_node_below_price, nearest_node_above_price] 中的位置；upper/lower 任一缺失或 upper <= lower → null）
- `node_interval_position_raw` = `(close - lower) / (upper - lower)`（不 clip，可 > 1 或 < 0，用于诊断）
- `cost_position_zone` = close 相对节点的位置分类：
  - upper/lower 都存在：`below_lower_node`（close < lower）/ `between_nodes`（lower <= close <= upper）/ `above_upper_node`（close > upper）
  - 只存在 upper：`below_upper_node`
  - 只存在 lower：`above_lower_node`
  - 都不存在：null
- `value_area_zone` = close 相对 VA 的位置分类：
  - vah/val 任一缺失：null
  - close < val：`below_va`
  - close > vah：`above_va`
  - val <= close <= vah：`inside_va`

注：`nearest_nodes()` 返回的 node dict 不含 `total_volume`，需从 `peak_df` 按 `price_mid` 匹配查找。

**位置语义说明**（避免误读）：
- `position_0_1` 是 VP 全价格范围位置（lowest~highest），不是节点区间位置；
- `node_interval_position_0_1` 是节点区间位置（lower~upper），clip 到 [0,1]；
- `value_area_position_0_1` 是 VA 区间位置（val~vah），不 clip，可超出 [0,1]；
- 截图案例：close=147.62, lower=123.22, upper=147.63 → `position_0_1`≈0.705（VP 全区间），`node_interval_position_0_1`≈1.000（节点区间接近 upper）。

#### volatility_momentum（动量/波动）

**V1.7 保留字段**：`bb_percent_b`/`bb_bandwidth_pct`/`bb_bandwidth_percentile`/`sqzmom_val`/`sqzmom_delta_1`/`sqzmom_percentile`

**V1.8 新增字段**：
- `distance_to_bb_upper_atr` = `(close - bb_upper) / last_atr`
- `distance_to_bb_lower_atr` = `(close - bb_lower) / last_atr`
- `sqzmom_abs_percentile` = `percentile_rank(abs(sqzmom_val), abs(sqzmom_series), 120)`
- `sqz_on` = `sqz["sqzOn"][-1]`（bool）
- `sqz_off` = `sqz["sqzOff"][-1]`（bool）

#### participation（成交参与）

**V1.7 保留字段**：`volume_ratio_20`/`volume_percentile_120`

**V1.8 段级成交量字段**（从 dsa_segment 共享，避免前端跨组拼接）：
- `current_segment_volume_sum`、`prev_segment_volume_sum`、`current_vs_prev_volume_ratio`、`current_segment_return_per_volume`、`prev_segment_return_per_volume`、`return_per_volume_ratio`

### 10.4 计算约束

- 主周期 lookback = 250 bar，副周期 lookback = 500 bar；
- 副周期（15m）仅使用已完成 bar（`include_realtime=False`）；
- Swing 仅使用已确认 pivot（`_tv_pivots_confirmed`，无未来函数）；
- ATR 使用 SSOT `app.strategy_assets.algorithms.features.atr_utils.compute_atr`（Pine RMA 等价）；
- Node/POC 使用 SSOT `compute_unified_volume_profile`；
- BB 使用 `bollinger`（`std(ddof=0)` 与 Pine 对齐）；
- SQZMOM 复用 `compute_sqzmom_lb` SSOT；
- **V1.8 段收益/斜率/效率一律基于 close 或 segment 实际端点价格，不用 dsa_vwap 替代价格**；
- **V1.8 node_strength 从 peak_df 按 price_mid 查找**（nearest_nodes 不返回 total_volume）；
- **V1.8 participation 段级成交量从 dsa_segment 共享**（编排器先算 dsa_segment 再传给 participation）；
- **V1.8 relation 移除 momentum_alignment**，只输出客观关系字段（primary_dir/secondary_dir/trend_alignment/swing_position/slope_atr/position_delta）；
- 每个因子组独立 try/except，单组失败返回 `null` + `degraded_reasons`，不阻塞其他组；
- `last_atr` 为 NaN 或 ≤0 时所有 ATR 标准化字段为 null。

### 10.5 降级策略

- API 失败 → 前端显示"暂无数据"；
- 单组因子 `null` → 前端卡片显示"-"；
- `meta.degraded_reasons` 非空 → 前端显示降级警告条；
- 数据不足（warmup）→ `meta.warmup_notes` 记录提示。

### 10.6 限制

- 结构状态因子不接入选股、监控、飞书、消息中心、事件系统；
- 不新增数据库表；
- 不写入任何持久化存储（纯实时计算）；
- 不改 DSA VWAP、筹码峰、K 线实时行情合并逻辑；
- 不引入 amount 相关因子（只使用 volume）。

## 11. 时序特征 API 契约 V1

### 11.1 端点

`GET /api/v1/instruments/{instrument_id}/temporal-features`

参数：
- `primary_timeframe`（默认 `1d`，允许 `1d|15m|1h|1w|1mo`）
- `secondary_timeframe`（默认 `15m`，允许 `1d|15m|1h|1w|1mo`）
- `adj`（默认 `qfq`，允许 `qfq|none`）
- `as_of`（V1 只支持 `latest`，不实现历史回放截断；`as_of != "latest"` 返回 400）

无认证要求（与 `/structural-factors` 一致）。非法参数返回 400。不存在 instrument 返回 200 + `degraded_reasons`（不报 404）。

### 11.2 响应结构

```json
{
  "daily_context": {9 字段},
  "m15_response": {9 字段},
  "derived_relation": {3 字段},
  "meta": {
    "as_of": "...",
    "primary_timeframe": "1d",
    "secondary_timeframe": "15m",
    "degraded_reasons": [],
    "warmup_notes": []
  }
}
```

### 11.3 字段表

#### daily_context（9 字段，长周期结构背景）

| 字段 | 公式 |
|---|---|
| daily_dsa_dir | `primary.dsa_segment.current_dsa_segment_dir` |
| daily_dsa_segment_duration_percentile | `percentile_rank(current_age_bars, hist_age_bars)`；历史样本为当前 1d lookback 内所有已完成 DSA segments 的 age_bars；<5 segments → null + warmup_notes；只表示持续度，不表示成熟/衰竭 |
| daily_dsa_slope_atr_per_bar | `primary.dsa_segment.current_dsa_segment_slope_atr_per_bar` |
| daily_dsa_efficiency_0_1 | `primary.dsa_segment.current_dsa_segment_efficiency_0_1` |
| daily_price_position_in_swing_0_1 | `primary.swing_position.price_position_in_swing_0_1` |
| daily_distance_to_swing_high_atr | `primary.swing_position.distance_to_swing_high_atr` |
| daily_distance_to_node_above_atr | `primary.cost_position.distance_to_node_above_atr`（无上方 node 返回 null） |
| daily_sqzmom_change_since_segment_start | `sqzmom_now - sqzmom_at_seg_start`；`sqzmom_now=primary.volatility_momentum.sqzmom_val`；seg_start 按 DSA segment start 定位，重算 `compute_sqzmom_lb` 取 `val_list[seg_start_idx]`；找不到或 warmup 返回 null |
| daily_volume_percentile_change_since_segment_start | `vol_pct_now - vol_pct_at_seg_start`；`vol_pct_now=primary.participation.volume_percentile_120`；seg_start 的 volume percentile 必须按当时可见历史计算（`percentile_rank(volumes[idx], volumes[:idx+1], 120)`），不得使用未来数据 |

#### m15_response（9 字段，短周期响应；不使用 15m DSA 位置类字段作为核心输入）

| 字段 | 公式 |
|---|---|
| m15_price_position_in_swing_0_1 | `secondary.swing_position.price_position_in_swing_0_1` |
| m15_position_change_since_swing_anchor | anchor 规则：`bars_since_swing_low < bars_since_swing_high` → anchor=swing_low_bar；否则 anchor=swing_high_bar。`m15_pos_at_anchor = (close_anchor - confirmed_swing_low) / (confirmed_swing_high - confirmed_swing_low)`；change = `m15_pos_now - m15_pos_at_anchor`；anchor 时 swing range 不完整或为 0 返回 null |
| m15_distance_to_swing_high_atr | `secondary.swing_position.distance_to_swing_high_atr` |
| m15_distance_to_swing_low_atr | `secondary.swing_position.distance_to_swing_low_atr` |
| m15_sqzmom_change_since_swing_anchor | `sqzmom_now - sqzmom_at_swing_anchor`；重算 15m `compute_sqzmom_lb` 取 `val_list[anchor_idx]`；anchor SQZMOM warmup 返回 null |
| m15_sqzmom_abs_percentile | `secondary.volatility_momentum.sqzmom_abs_percentile` |
| m15_sqz_off | `secondary.volatility_momentum.sqz_off` |
| m15_bb_bandwidth_change_since_swing_anchor | `bb_bandwidth_percentile_now - bb_bandwidth_percentile_at_anchor`；anchor 值必须按当时可见历史计算（重算 BB bandwidth 序列再 `percentile_rank`） |
| m15_volume_percentile_change_since_swing_anchor | `vol_pct_now - vol_pct_at_anchor`；anchor 值必须按当时可见历史计算（`percentile_rank(volumes[idx], volumes[:idx+1], 120)`） |

#### derived_relation（3 字段，只由 daily_context + m15_response 派生，不引入新信息）

| 字段 | 公式 |
|---|---|
| m15_position_relative_to_daily | `m15_price_position_in_active_swing_0_1 - daily_price_position_in_active_swing_0_1`（使用 active swing clip [0,1]，不回退 confirmed raw） |
| m15_response_direction_relative_to_daily | daily_dir=1 且 m15_position_change>0 → `"aligned"`；daily_dir=1 且 <0 → `"counter"`；daily_dir=-1 且 <0 → `"aligned"`；daily_dir=-1 且 >0 → `"counter"`；=0 或数据不足 → null。不得解释为回踩、反弹、机会、风险 |
| m15_response_intensity | `mean_abs_non_null([m15_position_change_since_swing_anchor, m15_sqzmom_change_since_swing_anchor, m15_bb_bandwidth_change_since_swing_anchor, m15_volume_percentile_change_since_swing_anchor])`；全部 null 返回 null。不做强弱标签 |

#### V1.9 新增字段（active swing + confirmed raw，配合 swing_position V1.9 active swing）

daily_context V1.9 新增字段：
- `daily_price_position_in_active_swing_0_1` = `primary.swing_position.price_position_in_active_swing_0_1`（clip [0,1]，**前端 Swing 摘要卡和 derived_relation 唯一位置字段**）
- `daily_active_swing_high` = `primary.swing_position.active_swing_high`
- `daily_active_swing_low` = `primary.swing_position.active_swing_low`
- `daily_distance_to_active_swing_high_atr` = `primary.swing_position.distance_to_active_swing_high_atr`
- `daily_distance_to_active_swing_low_atr` = `primary.swing_position.distance_to_active_swing_low_atr`
- `daily_price_position_in_confirmed_swing_raw` = `primary.swing_position.price_position_in_confirmed_swing_raw`（不 clip，用于突破状态判断）
- `daily_confirmed_swing_breakout_state` = `primary.swing_position.confirmed_swing_breakout_state`（inside/above_confirmed_high/below_confirmed_low/null）

m15_response V1.9 新增字段：
- `m15_price_position_in_active_swing_0_1` = `secondary.swing_position.price_position_in_active_swing_0_1`（clip [0,1]，**前端 Swing 摘要卡和 derived_relation 唯一位置字段**）
- `m15_active_swing_high` = `secondary.swing_position.active_swing_high`
- `m15_active_swing_low` = `secondary.swing_position.active_swing_low`
- `m15_distance_to_active_swing_high_atr` = `secondary.swing_position.distance_to_active_swing_high_atr`
- `m15_distance_to_active_swing_low_atr` = `secondary.swing_position.distance_to_active_swing_low_atr`

#### V1.10 新增字段（developing swing，反映当前正在发生的回落/反弹结构）

daily_context V1.10 新增字段：
- `daily_developing_swing_dir` = `primary.swing_position.developing_swing_dir`（1/-1/None）
- `daily_developing_swing_high` = `primary.swing_position.developing_swing_high`
- `daily_developing_swing_low` = `primary.swing_position.developing_swing_low`
- `daily_price_position_in_developing_swing_0_1` = `primary.swing_position.price_position_in_developing_swing_0_1`（clip [0,1]，**时序特征卡片 developing 位置字段，不作为 derived_relation 字段**）
- `daily_distance_to_developing_swing_high_atr` = `primary.swing_position.distance_to_developing_swing_high_atr`
- `daily_distance_to_developing_swing_low_atr` = `primary.swing_position.distance_to_developing_swing_low_atr`

m15_response V1.10 新增字段：
- `m15_developing_swing_dir` = `secondary.swing_position.developing_swing_dir`
- `m15_developing_swing_high` = `secondary.swing_position.developing_swing_high`
- `m15_developing_swing_low` = `secondary.swing_position.developing_swing_low`
- `m15_price_position_in_developing_swing_0_1` = `secondary.swing_position.price_position_in_developing_swing_0_1`（clip [0,1]，**时序特征卡片 developing 位置字段，不作为 derived_relation 字段**）
- `m15_distance_to_developing_swing_high_atr` = `secondary.swing_position.distance_to_developing_swing_high_atr`
- `m15_distance_to_developing_swing_low_atr` = `secondary.swing_position.distance_to_developing_swing_low_atr`

V1.10 位置语义说明：
- `m15_position_relative_to_daily` 使用 active swing 位置（`m15_price_position_in_active_swing_0_1 - daily_price_position_in_active_swing_0_1`），避免 confirmed raw 可能 <0 或 >1 污染派生关系；
- developing swing 字段保留在响应中，用于时序特征卡片展示当前 developing 结构，**不回退到 active swing 或 confirmed raw 计算 derived_relation**；
- 任一 active swing 字段缺失返回 null，**不回退 confirmed raw**。


### 11.4 计算约束

- V1 only `as_of=latest`，不实现历史回放截断；API 层校验 `as_of != "latest"` 返回 400；
- 主周期 lookback = 250 bar，副周期 lookback = 500 bar；
- 复用 V1.8 `compute_structural_factors` 获取 primary/secondary factors（不重写 DSA/SQZMOM/BB/ATR/Node）；
- 历史序列重算（SQZMOM/BB bandwidth/volume_percentile）均 point-in-time 切片，无未来函数；
- DSA 历史 segments 通过 `compute_dsa_bundle` 重算 + `_find_bar_index_by_time` 定位起止 bar index；
- swing anchor 通过 `bars_since_swing_high/low` 反推 `anchor_bar_idx = len(bars) - 1 - min(bsh, bsl)`；
- **组级异常隔离**：每个组（daily/m15/derived）独立 try/except，单组失败返回该组全 null dict + `degraded_reasons` 记录 `{group_name} failed: {exc}`，不影响其他组或整体 API 返回；
- 字段无法计算返回 null + `warmup_notes`，不影响整体返回；
- 15m 周期不使用 15m DSA 位置类字段作为核心输入（只用 swing/动量/波动/成交响应）。

### 11.5 降级策略

- 单组失败 → 对应 dict 字段为 null + `meta.degraded_reasons` 记录原因；
- 数据不足（warmup）→ `meta.warmup_notes` 记录提示；
- 不存在的 instrument → 返回 200 + degraded（不报 404，与 structural-factors 一致）。

### 11.6 限制

- Temporal V1 不接入选股、监控、飞书、消息中心、事件系统；
- 不新增数据库表 / worker / 全市场预计算；
- 不写入任何持久化存储（纯实时计算）；
- 不重复 V1.8 当前状态字段，只补变化量/持续度/派生关系；
- 不实现历史回放（`as_of != latest` 时报 400 或忽略，V1 不支持）。

## 12. 实时行情可信化契约

### 12.1 `/api/v1/instruments/{instrument_id}/quote`

`GET /api/v1/instruments/{instrument_id}/quote` 返回标的实时报价，必须明确暴露数据来源、实时性、新鲜度与降级状态。

响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `instrument_id` | string (UUID) | 标的 ID |
| `symbol` | string | 股票代码 |
| `name` | string | 股票名称 |
| `current_price` | float | 当前价 |
| `open` / `high` / `low` / `close` | float | 当日开/高/低/收 |
| `volume` | float | 成交量 |
| `prev_close` | float | 昨收 |
| `change_pct` | float | 涨跌幅 |
| `update_time` | string (ISO8601) | 行情更新时间（Asia/Shanghai） |
| `source` | `"pytdx" \| "daily_fallback"` | 数据来源 |
| `is_realtime` | bool | 是否为实时行情 |
| `freshness_seconds` | float | 相对 `update_time` 的新鲜度（秒） |
| `degraded` | bool | 是否降级 |
| `degraded_reason` | string \| null | 降级原因 |

行为规则：

- 仅当 `market_status_service.compute_market_session` 返回 `MORNING_SESSION` 或 `AFTERNOON_SESSION` 时尝试 pytdx 实时拉取；午休/盘前/盘后/非交易日不尝试 pytdx。
- pytdx 成功 → `source="pytdx"`、`is_realtime=true`、`degraded=false`。
- 非交易时段直接读 DB 日线 fallback → `source="daily_fallback"`、`is_realtime=false`、`degraded=false`。
- 交易时段 pytdx 失败 → `source="daily_fallback"`、`is_realtime=false`、`degraded=true`，`degraded_reason` 记录 pytdx 失败原因。
- 无 pytdx 数据且无 DB fallback 数据 → 返回 404。
- Redis 短缓存（10s TTL）用于削峰，缓存命中时直接返回缓存的 pytdx 结果。

### 12.2 `/api/v1/instruments/{instrument_id}/bars` 数据源诊断

Bar 列表响应在分页字段之外补充以下诊断字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `data_source` | string | 数据来源，如 `bars_daily`、`bars_15min` 等 |
| `as_of` | string \| null | 数据截止时间（ISO8601） |
| `is_partial` | bool | 是否包含未完成 bar；`timeframe=1d` 时代表后端已合成 partial daily bar |
| `last_live_bar_time` | string \| null | 最新 live bar 时间；仅当 `is_partial=true` 时非空 |
| `freshness_seconds` | float | 数据新鲜度（秒） |
| `degraded` | bool | 是否降级 |
| `degraded_reason` | string \| null | 降级原因 |

`is_partial` 是前端判断 K线实时状态的事实源：

- 交易时段 `timeframe=1d && include_realtime=true` 必须返回 `is_partial=true`；
- 收盘后/非交易时段 `timeframe=1d` 必须返回 `is_partial=false`；
- `/quote` 的 `is_realtime` 只代表顶部行情卡片实时，不得用于推断 K线 `is_partial`。

### 12.3 K 线实时合并约束

`frontend/src/utils/chart.ts::mergeRealtimeQuoteIntoBars` 只允许将可信实时行情合并到 `displayBars`（仅用于图表显示，不用于指标计算）。

合并必须同时满足：

- 后端 `/bars` 未返回 `is_partial=true`（即 `backendIsPartial=false`）；
- `quote.is_realtime === true`；
- `quote.source === "pytdx"`；
- `quote.freshness_seconds <= 60`。

**后端已返回 partial bar 时，前端不得用 quote 覆盖 K线；** 此时 `displayBars` 必须直接使用 `baseBars`。

不满足上述条件时，quote 只能用于顶部报价 fallback/状态提示，不得混入 K 线。

- `1d` 周期保留日期语义：同一交易日合并到最后一根 bar；跨日追加一根新的日期粒度实时 bar。
- intraday 周期（15m/1h 等）使用 `quote.update_time` 更新最后一根 bar 的时间。
- `1m` 周期当前不在工具栏暴露，后端 bars API 亦不返回 1m 数据，不执行 1m 合并。

### 12.4 K 线与 DSA overlay 时间戳/时区/数据源对齐契约

DSA overlay（`indicators.source_bar_times` / `indicators.source_bar_hash`）必须与当前图表同 `symbol/timeframe/adj/source/timezone` 数据对齐。前端 `frontend/src/utils/chartTime.ts::normalizeChartTime` 通过正则提取 date+HH:MM 前缀作为 canonical key，使 K线 `trade_time`（aware ISO）与 `source_bar_times`（naive ISO）产生相同 canonical key。

#### 12.4.1 时间戳与时区

| 字段 | 1d/1w/1mo | 15m/1h |
|---|---|---|
| `bars.trade_date` | `YYYY-MM-DD`（无时区，date 对象） | `null` |
| `bars.trade_time` | `null` | `YYYY-MM-DDTHH:MM:SS+08:00`（aware Asia/Shanghai ISO） |
| `indicators.source_bar_times[]` | `YYYY-MM-DD` | `YYYY-MM-DDTHH:MM:SS`（naive，无时区后缀） |
| `indicators.source_bar_hash` | OHLCV + `YYYY-MM-DD` 拼接 SHA256[:16] | OHLCV + `YYYY-MM-DDTHH:MM:SS` 拼接 SHA256[:16] |

约束：

- 15m/1h `bars.trade_time` **必须** 返回 aware datetime（Asia/Shanghai tzinfo），序列化为 `+08:00` 后缀，避免前端 `new Date(...)` 在非亚洲时区浏览器中把 naive ISO 当作本地时间解析导致时区误判（如 `2026-07-06T15:00:00` 在 `America/New_York` 浏览器中显示为 `2026-07-07 03:00`）。
- 1d `bars.trade_date` 仍为 `YYYY-MM-DD` date 对象（无时区），向后兼容。
- `indicators.source_bar_times` 与 `source_bar_hash` 必须按当前 `timeframe` 使用对应 macd_bars（而非永远用 daily_bars），格式随 timeframe：
  - 1d/1w/1mo: `YYYY-MM-DD`
  - 15m/1h: `YYYY-MM-DDTHH:MM:SS`（无时区后缀）

#### 12.4.2 DSA source mismatch 保护

前端 `frontend/src/components/StrategyChart.tsx` 在 DSA overlay 渲染前比较 `displayTimes` 与 `source_bar_times` 的 canonical key 交集：

- 交集比例 `matched / klineKeys.size < 0.5` → 触发 "DSA 数据源不一致，已暂停渲染" banner，DSA overlay 不渲染，但 structural/temporal 因子卡片仍可显示。
- canonical key 由 `normalizeChartTime(time, timeframe)` 计算：
  - 15m/1h: `"YYYY-MM-DD HH:MM"`（提取前 16 字符）
  - 1d: `"YYYY-MM-DD"`
- 关键不变量：`normalizeChartTime` 仅提取 date+HH:MM 前缀，忽略 `+08:00` 时区后缀和秒数，使 K线（aware）与 `source_bar_times`（naive）产生相同 canonical key。
- 故意构造的 source mismatch（如 15m source 仍是日线日期格式）仍触发 banner，保护 DSA 数学正确性。

#### 12.4.3 图表 header 时间显示

- 15m/1h 时间轴刻度 `timeTicks` 使用 `Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai' })` 格式化，确保 A 股交易时间正确显示。
- 1d 时间轴刻度仅显示 `MM-DD`。
- 不应在 15m 图表显示 `03:00` 这类非 A 股交易时间（naive datetime 在非亚洲时区浏览器中的时区误判）。

### 12.5 Indicator overlay 周期策略与 cache schema 版本契约

#### 12.5.1 DSA overlay 周期策略（全周期支持）

DSA（Pine 标签 + VWAP）支持全周期渲染（1d/15m/1h/1w/1mo）。1d 是主结构锚，非 1d 是验证图层（用于核查该周期结构，不作为主趋势锚）。

- DSA overlay 按钮在所有周期可点击（不 disabled），`title` 由 `frontend/src/utils/dsaOverlayPolicy.ts::DSA_TITLE_HINT(timeframe)` 提供：
  - 1d: "DSA VWAP 日线结构锚。"
  - 非 1d: "DSA VWAP 当前周期验证图层：用于核查该周期结构，不作为主趋势锚。"
- DSA source mismatch 校验由 `shouldCheckDsaMismatch(timeframe)` 控制：全周期返回 `true`（DSA 全周期渲染，全部需校验 source 对齐）；
- DSA 数据必须用当前 timeframe bars 计算（`indicator_service` 传 `bars_daily=macd_bars`），不得用日线 DSA 映射到其他周期；
- 仍保留 source mismatch 保护：当 K线时间与 `source_bar_times` 匹配率 < 50% 时暂停渲染并提示，不允许无校验强画；
- DSA 渲染/toggle/y-axis 决策由 `frontend/src/utils/dsaOverlayPolicy.ts` 集中控制（PR #33 前端硬编码清理）：
  - `shouldRenderDsaLayer(layerId, layers, dsaSourceMismatch, timeframe)`：DSA 渲染决策，不再按 `timeframe !== '1d'` 跳过；
  - `shouldToggleDsa(groupId, isCaptureMode, captureLayers)`：DSA toggle 决策，非 capture 模式下 DSA 全周期可切换；
  - `shouldIncludeDsaInPriceRange(layerId, layers, timeframe)`：DSA 纵轴范围候选决策，DSA 全周期参与 y-axis range（不再 `timeframe === '1d'` 限制）；
- DSA 数据不足时的 degraded 语义：当 `macd_bars` 数量低于 DSA 算法 warmup 门槛（如 1mo 月线 < 算法最低 lookback）时，`time` / `dsa_vwap` / `visual_segments` / `regime_id` 返回空数组（不报错，不伪造数据）；前端 DSA overlay 仍可点击，但无数据可渲染，应显示 "no data / 不足 warmup" 提示而非空白；
- DSA `visual_segments.points.time` / `anchor.time` / `time` 数组必须按 timeframe 序列化（PR #34）：
  - 后端 `format_dsa_time(x)`（`dynamic_swing_anchored_vwap.py`）按 `pd.Timestamp` 是否含时间部分选择序列化格式：
    - 1d/1w/1mo（hour/minute/second/microsecond 全 0）→ `strftime("%Y-%m-%d")`，与历史 1d 契约一致；
    - 15m/1h（含非零时间部分）→ `isoformat()`（含 `T`），保留 `THH:MM:SS`；
  - `_make_segment` / `compute_dsa_bundle.anchor` / `compute_indicators.time` / `_show_segments` 全部改用 `format_dsa_time`，禁止再无条件 `strftime("%Y-%m-%d")`；
  - 前端 `normalizeChartTime('15m'/'1h')` 要求 raw 含 `HH:MM` 才能产生 canonical key，否则返回 `null`，`dsa_polyline` renderer matched=0，开关打开也画不出线；
  - 不改变 `dsa_vwap` / `dsa_dir` / `regime_id` / `visual_segments.direction` / `points.value` 的数学含义，只改时间序列化格式。
- 右侧 `StockStructuralStatePanel` 仍可显示 daily DSA 背景和 m15 response（结构状态因子不受图层渲染影响）。

#### 12.5.2 BB/MACD/SQZMOM overlay 跟随当前周期

BB / MACD / SQZMOM overlay 必须使用当前图表周期（timeframe）的 bars 计算，全周期支持（1d/15m/1h/1w/1mo）：

- `indicator_service._adapt_watchlist_bb` 在 15m/1h/1w/1mo 必须用 `macd_bars`（当前 timeframe bars）调用 `compute_bollinger(macd_bars, length=20, mult=2.0)` 重新计算 BB upper/mid/lower/pos/width；
- 1w/1mo 不再移除 BB 字段（PR #32 修复：之前直接 `pop` BB 字段导致前端无 BB overlay）；
- 字段映射：`bb_pos_01` → `bb_pos`，`bb_width_norm` → `bb_width`；NaN 转 `None` 以保证 JSON 可序列化；
- 当 `len(macd_bars) < 20`（BB 计算窗口不足）时，BB 字段填 `None`，但 `time` 数组仍与 `macd_bars` 对齐；
- MACD / SQZMOM 同理：必须用 `macd_bars`（当前 timeframe）计算，不允许串日线；
- BB overlay 时间轴必须用 `buildDisplayIndexMap` 按 canonical time 对齐，禁止尾部截取（tail slice）。

#### 12.5.3 Indicator cache schema 版本契约

`backend/app/services/indicator_cache.py::ALGORITHM_VERSION` 是 indicator 缓存的 schema 版本号。任何修改 indicator 计算逻辑、`source_bar_times` 格式、BB/SQZMOM/MACD 计算路径的变更**必须 bump `ALGORITHM_VERSION`**，使旧缓存自然失效：

- cache key 格式：`indicator:{algorithm_version}:{timeframe}:{adj}:{bars}:{symbol}`；
- 旧版本 cache key 与新版本不匹配，强制重算，避免旧格式 `source_bar_times` 或日线阶梯线 BB 污染渲染；
- 禁止通过手动 `DEL` 单只股票 key 修复缓存问题（不可扩展，且无法覆盖所有时间周期）；
- 当前 `ALGORITHM_VERSION = "v5"`（PR #32：DSA 全周期支持 + 1w/1mo BB 用 compute_bollinger 计算）。

#### 12.5.3.1 `force_refresh` / `capture` 查询参数（旁路缓存）

`GET /api/v1/instruments/{instrument_id}/indicators` 与 Capture Snapshot 端点支持 `force_refresh=1` / `capture=1` 查询参数：

- `bypass_cache = force_refresh or capture` 时跳过 Redis indicator 读缓存，强制实时重算，但仍写回最新结果（避免污染常规用户路径缓存一致性）；
- 保留 `X-Data-Source` / `X-Cache-Hit` / `X-Total-Ms` 响应头；
- 飞书截图链路固定携带 `force_refresh=1&capture=1`，确保图片内指标为当前实时数据，不复用旧指标缓存。

#### 12.5.4 ?debugIndicatorAlignment=1 诊断工具

`StrategyChart` 支持通过 URL 参数 `?debugIndicatorAlignment=1` 输出 overlay 对齐诊断，默认不打印，不刷日志：

- `console.table` 输出 `bars`（timeframe/count/first/last/canonical_first/canonical_last）；
- `console.table` 输出 `dsa_mismatch`（check_enabled/mismatched/source_bar_hash/source_bar_times_count）；
- `console.table` 输出 `indicators.layers`（layer_id/renderer/fields/time_count）；
- 仅用于诊断 15m/1h overlay 对齐问题，不影响生产渲染逻辑。

## 13. Feature Snapshot 持久化契约

### 13.1 `stock_feature_snapshots` 表契约

`stock_feature_snapshots` 是自选股监控指标与历史特征因子探索的唯一持久化事实源。盘后 orchestrator 生成当日快照，`/watchlist/monitor-status` 只读不写。

| 字段 | 类型 | 约束 | 语义 |
|---|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` | 快照 ID |
| `instrument_id` | UUID | FK→`instruments.id`, NOT NULL | 股票 ID |
| `trade_date` | DATE | NOT NULL | 业务交易日 |
| `primary_timeframe` | TEXT | NOT NULL, default `'1d'` | 主周期 |
| `secondary_timeframe` | TEXT | NOT NULL, default `'15m'` | 次周期 |
| `adj` | TEXT | NOT NULL, default `'qfq'` | 复权方式 |
| `schema_version` | INT | NOT NULL, default `1` | schema 版本（变更需 bump） |
| `source_primary_bar_time` | TIMESTAMPTZ | NULL | 主周期数据源截止时间；1d 规范化为 `trade_date 15:00+08:00` |
| `source_secondary_bar_time` | TIMESTAMPTZ | NULL | 15m 数据源截止时间（最后一根 15m bar 的实际 trade_time） |
| `structural_payload` | JSONB | NOT NULL | `_compute_all_factors_for_bars` 完整输出（primary + secondary + relation + meta） |
| `temporal_payload` | JSONB | NOT NULL | `_compute_daily_context` / `_compute_m15_response` / `_compute_derived_relation` 完整输出 |
| `summary_payload` | JSONB | NOT NULL | 前端列表用摘要，含 `_source='feature_snapshot'` |
| `degraded_reasons` | JSONB | NOT NULL, default `'[]'` | 单股降级原因列表（不阻断其他股票） |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` | 创建时间 |
| `updated_at` | TIMESTAMPTZ | NOT NULL, default `now()` | 更新时间（upsert 覆盖） |

**唯一约束**：`uq_feature_snapshot_instrument_date_tf_adj_schema (instrument_id, trade_date, primary_timeframe, secondary_timeframe, adj, schema_version)`，支持 upsert 幂等写入。

**索引**：
- `ix_feature_snapshot_trade_date_schema (trade_date, schema_version)`：按日查询。
- `ix_feature_snapshot_instrument_date (instrument_id, trade_date DESC)`：单股历史回溯。
- `ix_feature_snapshot_date_instrument (trade_date, instrument_id)`：按日批量扫描。

**不给 full payload 加 GIN 索引**（节省磁盘，5000 股 × 250 天 JSONB 体积约 25-50 GB）。

### 13.2 `summary_payload` 字段契约

`summary_payload` 是 `/watchlist/monitor-status` 响应 `metrics` 字段的唯一来源。所有字段缺失时填 `None`，不抛异常。

| 字段 | 来源 | 语义 |
|---|---|---|
| `_source` | 固定 `'feature_snapshot'` | 数据源标识（前端区分 snapshot vs fallback） |
| `as_of` | `trade_date.isoformat()` | 业务交易日 |
| `source_bar_time` | `source_secondary_bar_time.isoformat()` 或 `source_primary_bar_time.isoformat()` | 数据源截止时间 |
| `current_price` | `df_1d.close[-1]` | 当前价（最后一根日线 close） |
| `change_pct` | `(close[-1] - close[-2]) / close[-2] * 100` | 涨跌幅（4 位小数） |
| `bb_upper` / `bb_mid` / `bb_lower` | `bollinger(df_1d, 20, 2.0)` | BB 绝对值（最后一根） |
| `poc_price` | `structural.primary.1d.cost_position.poc_price` | 成本 POC |
| `nearest_node_above` / `nearest_node_below` | `cost_position.nearest_node_above_price` / `nearest_node_below_price` | 上下方节点 |
| `distance_to_node_above_atr` / `distance_to_node_below_atr` | `cost_position.distance_to_node_*_atr` | ATR 距离 |
| `node_interval_position_0_1` | `cost_position.node_interval_position_0_1` | 节点区间位置 [0,1] |
| `cost_position_zone` / `value_area_zone` | `cost_position.cost_position_zone` / `value_area_zone` | 区域枚举 |
| `daily_developing_swing_dir` / `daily_developing_swing_high` / `daily_developing_swing_low` | `structural.primary.1d.swing_position.developing_swing_*` | 日线 developing swing |
| `m15_developing_swing_dir` / `m15_developing_swing_high` / `m15_developing_swing_low` | `structural.secondary.15m.swing_position.developing_swing_*` | 15m developing swing |
| `m15_position_relative_to_daily` | `temporal.derived_relation.m15_position_relative_to_daily` | 派生关系 |

### 13.3 `/watchlist/monitor-status` 数据源契约

`GET /api/v1/watchlist/monitor-status` 响应的 `metrics` 字段唯一来自 `stock_feature_snapshots.summary_payload`，不再走 `MonitorSnapshotService` 实时计算或 `MonitorState.payload` fallback。`MonitorEvaluation` 仅用于展示评估状态字段（`evaluation_status` / `retry_count` / `error_code` / `source_bar_time`），不作为 metrics 数据源。

**Run gate（Phase 8 新增）**：`/watchlist/monitor-status` 只读取 `expected_snapshot_trade_date` 对应存在 `stock_feature_snapshot_runs.status='succeeded'`（且 `published_at` 非空）的 snapshot 行。run 状态为 `running` / `failed` / 不存在时，watchlist 不得读取该日期的 snapshot，应返回 `WAITING_SNAPSHOT` 或 `NO_SNAPSHOT`。

**`calculation_status` 三态语义**：

| 状态 | 触发条件 | metrics 内容 | `freshness_seconds` |
|---|---|---|---|
| `SUCCEEDED` | `expected_snapshot_trade_date` 对应的 snapshot 存在（含盘中读昨日、收盘后读今日、非交易日读最近交易日） | 来自 `summary_payload` | `now_cst - snapshot.updated_at`（秒） |
| `WAITING_SNAPSHOT` | 交易日已收盘（`MARKET_CLOSED`）但当日 snapshot 缺失（orchestrator 未跑或失败）；**仅在 `MARKET_CLOSED` 时出现，盘中不出现** | 空 dict | `None` |
| `NO_SNAPSHOT` | 盘中无昨日 snapshot / 非交易日无历史 snapshot / 无法解析交易日 | 空 dict | `None` |

**`_resolve_expected_snapshot_trade_date` 规则**（`/watchlist/monitor-status` 内部 helper）：

| 今日类型 | 市场状态 | `expected_snapshot_trade_date` | 说明 |
|---|---|---|---|
| 交易日 | 未收盘（PRE_OPEN / MORNING_SESSION / LUNCH_BREAK / AFTERNOON_SESSION） | 上一已完成交易日（`calendar_service.get_previous_trading_day_async`，严格 `< today`） | 盘中读昨日 snapshot，前端展示 SUCCEEDED + 昨日 metrics |
| 交易日 | MARKET_CLOSED | `today` | 收盘后等待当日 snapshot；缺失时返回 WAITING_SNAPSHOT |
| 非交易日 | NON_TRADING_DAY | 最近交易日（`calendar_service.get_most_recent_trading_day_async`，`<= today`） | 周末/节假日读最近交易日 snapshot |
| 任一 | 任一 | 无法解析最近交易日（如日历表为空） | `None`，前端展示 NO_SNAPSHOT |

复用 `calendar_service` / `trading_calendar` 表，禁止硬编码周末。

**`monitor_status` 兼容字段**：`calculation_status != 'SUCCEEDED'` 时与 `calculation_status` 一致；`SUCCEEDED` 时回落到 `market_session`（保持前端旧字段兼容）。

**point-in-time 约束**：
- 1d bars 截断到 `index.date <= trade_date`；
- 15m bars 截断到 `index.date <= trade_date`；
- 禁止使用 `trade_date` 之后数据；
- `include_realtime=False`，只取已完成 bar。

### 13.4 `structural_payload.relation` 字段

`structural_payload` 包含 4 个 top-level key：`primary` / `secondary` / `relation` / `meta`。

- `primary`：`{primary_timeframe: _compute_all_factors_for_bars(df_1d)}` 完整输出
- `secondary`：`{secondary_timeframe: _compute_all_factors_for_bars(df_15m)}` 完整输出
- `relation`：`_compute_relation(primary_factors, secondary_factors)` 输出，包含 `trend_alignment` / `secondary_vs_primary_position_delta` / `primary_dir` / `secondary_dir` 等 V1.8 客观关系字段（值可能为 `null`，由数据充分性决定）
- `meta`：`degraded_reasons` + `warmup_notes`

**禁止**在 `feature_snapshot_service` 内复制 `_compute_relation` 公式，必须复用 `structural_factor_service._compute_relation`。

### 13.5 计算约束与事务边界

- 复用 `_compute_all_factors_for_bars` / `_compute_relation` / `_compute_daily_context` / `_compute_m15_response` / `_compute_derived_relation` / `bollinger()`，**不复制 DSA/BB/swing/temporal 数学公式**；
- 单股失败写 `degraded_reasons`，不阻断批次其他股票；
- 失败比例超过 `failure_threshold`（默认 0.3）抛 `RuntimeError`，由 caller 决定 rollback；
- upsert 按唯一键 `ON CONFLICT DO UPDATE`，`structural_payload` / `temporal_payload` / `summary_payload` / `source_*_bar_time` / `degraded_reasons` 覆盖，`updated_at = now()`；
- 1d bar 时间规范化为 `trade_date 15:00+08:00` aware datetime；15m bar 时间取最后一根 15m bar 实际 trade_time，转 `Asia/Shanghai` aware datetime。

### 13.6 事务边界与不可读取失败半成品

- `compute_for_trade_date` 只负责 upsert（flush）+ 返回统计，**不内部 commit**；
- caller（`after_close_orchestrator` / `feature_snapshot_backfill`）决定 commit / rollback：
  - 成功（`failure_rate <= threshold`）→ commit；
  - `RuntimeError`（失败比例超阈值）→ rollback 半成品 → 标记 `failed`；
- `/watchlist/monitor-status` 只读取已 commit 的 snapshot 行；失败日期不应有部分已 commit 行（half-baked）；
- backfill 单日失败时该日所有半成品 rollback，不影响其他日期。

### 13.7 `stock_feature_snapshot_runs` 表契约

`stock_feature_snapshot_runs` 是 snapshot 计算 run 级别的成功标记表。`/watchlist/monitor-status` 只读取 `status='succeeded'`（且 `published_at` 非空）的 run 对应日期的 snapshot 行，避免读取半成品或失败日期的 snapshot。

| 字段 | 类型 | 约束 | 语义 |
|---|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` | run ID |
| `trade_date` | DATE | NOT NULL | 业务交易日 |
| `schema_version` | INT | NOT NULL | snapshot schema 版本（与 `stock_feature_snapshots.schema_version` 对齐） |
| `primary_timeframe` | TEXT | NOT NULL, default `'1d'` | 主周期 |
| `secondary_timeframe` | TEXT | NOT NULL, default `'15m'` | 次周期 |
| `adj` | TEXT | NOT NULL, default `'qfq'` | 复权方式 |
| `run_type` | TEXT | NOT NULL | `after_close` / `backfill` / `manual` |
| `status` | TEXT | NOT NULL | `running` / `succeeded` / `failed` |
| `expected_count` | INT | NULL | 预期 snapshot 数（active A 股数） |
| `snapshot_count` | INT | NULL | 实际成功写入数 |
| `failed_count` | INT | NULL | 失败数 |
| `skipped_count` | INT | NULL | 跳过数（resume 场景） |
| `failure_rate` | FLOAT | NULL | 失败率 = `failed_count / expected_count` |
| `started_at` | TIMESTAMPTZ | NOT NULL, default `now()` | run 开始时间 |
| `finished_at` | TIMESTAMPTZ | NULL | run 结束时间（succeeded/failed） |
| `published_at` | TIMESTAMPTZ | NULL | 发布时间（仅 `status='succeeded'` 时写入） |
| `metadata_` | JSONB | NOT NULL, default `'{}'` | 审计元数据（`source` / `error` / `scope` / `batch_size` 等）；`scope` 字段为 watchlist gate 必查键，取值 `full`（可读）或 `sample`（不可读） |

**唯一约束**：`uq_snapshot_runs_active_key (trade_date, schema_version, primary_timeframe, secondary_timeframe, adj, run_type) WHERE status = 'running'`（partial unique index，仅约束 running 状态，允许 failed run 与新 retry 并存）。

**索引**：
- `ix_snapshot_runs_trade_date (trade_date)`：按日查询。
- `ix_snapshot_runs_status (status)`：按状态过滤。
- `ix_snapshot_runs_schema_version (schema_version)`：按版本过滤。

**Run lifecycle 规则**：

| 阶段 | status | published_at | metadata_.scope | 说明 |
|---|---|---|---|---|
| 开始 | `running` | NULL | `full` 或 `sample` | `create_snapshot_run(scope=...)` 幂等创建（已有 running 则返回已有） |
| 成功 | `succeeded` | 非空 | `full` 或 `sample` | `finish_snapshot_run(status='succeeded', metadata={'scope': ...})` 写 `published_at = now()` |
| 失败 | `failed` | NULL | `full` 或 `sample` | `finish_snapshot_run(status='failed', metadata={'scope': ...})` 不写 `published_at` |

- `after_close` / `backfill` 开始时创建 `running` run（独立 session + commit），保证 run 记录持久化；
- snapshot 计算在独立 session 中进行，失败时 session 自动 rollback 半成品行；
- run finalization 在独立 session 中进行（`succeeded` / `failed`），保证 run 状态不受 snapshot rollback 影响；
- `/watchlist/monitor-status` 通过 `_has_succeeded_snapshot_run` 判断 `expected_snapshot_trade_date` 是否存在 `status='succeeded' + published_at IS NOT NULL + metadata_['scope']='full'` 的 run；
- **[Blocker Fix] scope 必传**：`create_snapshot_run(scope=...)` 和 `finish_snapshot_run(metadata={'scope': ...})` 都必须传入 scope。`finish_snapshot_run` 的 metadata 完全替换 create 时的 metadata，调用方必须在 finish 时再次包含 scope，否则 watchlist gate 会因缺失 scope 而拒绝读取；
- `after_close` 固定 `scope='full'`（处理全市场 A 股）；
- 普通 `backfill` 不带 `--symbols`/`--limit-instruments` 时 `scope='full'`；
- 小样本 `backfill` 带 `--symbols` 或 `--limit-instruments` 时 `scope='sample'`（watchlist 不可读）；
- failed run 允许新 retry（partial unique index 仅约束 running）。

## 14. 用户表格视图配置 API 契约

### 14.1 `user_table_view_presets` 表契约

`user_table_view_presets` 保存用户在表格（如趋势选股页）的筛选/排序/列设置 preset，支持命名、应用、重命名、删除、设为默认。

| 字段 | 类型 | 约束 | 语义 |
|---|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` | preset ID |
| `user_id` | UUID | FK→`users.id`, NOT NULL | 用户 ID（由认证上下文注入，不接受 body 传入） |
| `table_id` | TEXT | NOT NULL | 表格标识（如 `screener`/`watchlist`，由前端约定） |
| `strategy_key` | TEXT | NULL | 策略 key（可空，适用于无策略的表格） |
| `name` | TEXT | NOT NULL | 配置名称（用户自定义，同维度唯一） |
| `config` | JSONB | NOT NULL | 配置内容（仅允许 keyword/sort/filters/hiddenColumns/pageSize/industry/concept） |
| `is_default` | BOOLEAN | NOT NULL, default `false` | 是否默认配置（同 user+table_id+strategy_key 至多 1 个 true） |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` | 创建时间 |
| `updated_at` | TIMESTAMPTZ | NOT NULL, default `now()`, onupdate `now()` | 更新时间 |

**唯一约束**（两个 partial unique index，解决 PostgreSQL NULL!=NULL 问题）：
- `uq_user_table_view_preset_strategy_not_null (user_id, table_id, strategy_key, name) WHERE strategy_key IS NOT NULL`
- `uq_user_table_view_preset_strategy_null (user_id, table_id, name) WHERE strategy_key IS NULL`

保证同用户同表同策略下配置名不重复（含 strategy_key 为 NULL 的无策略表格场景）。

**索引**：`ix_user_table_view_presets_user_table_strategy (user_id, table_id, strategy_key)`，用于查询和 quota 检查。

### 14.2 `config` JSONB schema

`config` 字段仅允许以下 7 个 key（Pydantic schema `TableViewPresetConfig` 强制 `extra="forbid"`）：

| 字段 | 类型 | 约束 | 语义 |
|---|---|---|---|
| `keyword` | string \| null | max_length=200 | 关键字搜索 |
| `sort` | dict \| null | 必须含 `key`（非空 string）+ `direction`（`asc`/`desc`） | 排序配置 |
| `filters` | list[dict] \| null | 每元素必须是 dict 且含 `key`/`op`/`value`；`op` 限制白名单：`contains`/`eq`/`gt`/`gte`/`lt`/`lte`/`between`/`empty`/`not_empty` | 筛选条件列表 |
| `hiddenColumns` | list[string] \| null | 每元素必须是 string | 隐藏列 key 列表 |
| `pageSize` | int \| null | 1-500 | 每页大小 |
| `industry` | string \| null | max_length=100 | 行业板块筛选名称（CHANGE-20260713-006，JSONB 列无新 migration） |
| `concept` | string \| null | max_length=100 | 概念板块筛选名称（CHANGE-20260713-006，JSONB 列无新 migration） |

**禁止字段**（`_FORBIDDEN_CONFIG_KEYS`，由 `_validate_config_keys` 函数强制拒绝）：

- `selectedKeys`（选中股票是会话态，不持久化）
- `page`（当前页码是会话态）
- `activeRunId`（当前批次是会话态）
- `rows`/`results`/`resultData`（结果数据是业务数据，不持久化）

### 14.3 API 端点

所有端点权限：`require_active_subscription` + `require_feature("trend_selection")`（admin 豁免），与趋势选股一致。

#### GET `/me/table-view-presets`

查询当前用户的 preset 列表（按 `table_id` + `strategy_key` 过滤）。

查询参数：
- `table_id`（必填，min_length=1, max_length=64）
- `strategy_key`（可选，max_length=64；传空字符串匹配 NULL，传非空字符串精确匹配）

响应：`TableViewPresetListResponse { items: TableViewPresetResponse[], total: int }`，按 `created_at` 升序。

#### POST `/me/table-view-presets`

创建 preset。

请求体：`TableViewPresetCreate { table_id, strategy_key?, name, config, is_default? }`

业务规则：
- `user_id` 由 JWT 上下文注入，body 中 `user_id` 字段被忽略（安全约束）；
- quota 检查：同 `user_id+table_id+strategy_key` 已有 preset 数量 ≥ 20 时返回 422；
- `is_default=true` 时自动取消同维度其他默认（`_unset_default_for_scope`）；
- 唯一约束冲突返回 409。

响应：`TableViewPresetResponse`，状态码 201。

#### PATCH `/me/table-view-presets/{preset_id}`

更新 preset（`name`/`config`/`is_default`，`user_id`/`table_id`/`strategy_key` 不可改）。

请求体：`TableViewPresetPatch { name?, config?, is_default? }`（至少一个字段）

业务规则：
- 只能操作自己的 preset，他人 preset 返回 404（避免泄露存在性）；
- 重命名冲突返回 409；
- `is_default=true` 时自动取消同维度其他默认（排除自身）。

响应：`TableViewPresetResponse`。

#### DELETE `/me/table-view-presets/{preset_id}`

删除 preset。

业务规则：
- 只能删除自己的 preset，他人 preset 返回 404。

响应：204 No Content。

### 14.4 限制

- preset 不接入选股、监控、飞书、消息中心、事件系统；
- config 不保存业务数据（selectedKeys/page/activeRunId/rows）；
- preset 不影响后端策略计算，只影响前端表格视图；
- `is_default` 互斥更新由应用层 `_unset_default_for_scope` 实现（非数据库约束）。

## 15. 个股上下文 API 契约（stock_context）

`GET /api/v1/stocks/{symbol}/context` 是用户面 `EventStatePanel` 单一数据源，返回个股最新状态与历史事件聚合。前端 `useStockContext` hook 调用此端点，禁止前端重新计算或拼接。

### 15.1 Evidence DTO 映射

- `_event_to_dto` 将 ORM `event.evidence` 映射为用户面 Evidence DTO；
- 不再从 `event.payload` 拼装证据字段，Evidence 以 ORM `event.evidence` 为唯一来源。

### 15.2 时区

- 时间字段统一使用 `ZoneInfo("Asia/Shanghai")`（不再使用 UTC）；
- 事件时间、发布时间、完成时间均以 CST 返回。

### 15.3 历史事件截止

- 历史事件 cutoff 使用 **次日 00:00 exclusive**（`trade_date + 1 day, 00:00:00`，不包含该时刻）；
- 不再使用 `max.time + 1 day - 1 second` 口径。

### 15.4 Run 查询排序

- Run 查询使用确定性 DESC 排序：`ORDER BY trade_date DESC, published_at DESC, finished_at DESC`；
- 保证相同 trade_date 下多 run 的顺序确定，避免分页/缓存抖动。

### 15.5 strategy_events 幂等键

- `idempotency_key` 格式：`symbol:source_run_id:algorithm_version`；
- 旧格式 `symbol:trade_date:algorithm_version:hash(evidence)` 已废弃；
- 每只股票每个 run 至多生成一个事件（`source_run_id` 维度幂等）。

