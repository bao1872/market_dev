# API Route Map

> 事实源：`backend/app/main.py` include_router 列表。本文只做入口地图。

## 1. Router 列表

| Router | 文件 | 能力 |
|---|---|---|
| health | `app/api/health.py` | 健康/ready/策略资产检查 |
| auth | `app/api/auth.py` | 登录、注册、刷新、当前用户 |
| me | `app/api/me.py` | 当前用户权益与访问上下文 |
| instruments | `app/api/instruments.py` | 股票主数据 |
| calendar | `app/api/calendar.py` | 交易日历 |
| market | `app/api/market.py` | 市场状态；`GET /market/stocks` 服务端分页行情列表（scope=market\|watchlist, query, page, page_size≤100, sort, state=up\|down\|sideways, industry, concept）；industry/concept 已实现（PRD §7.5 qstock 同步后，通过 `filter_instruments_by_board()` 查询 `market_boards` 表筛选；未同步板块数据时返回空列表，不报 422；industry + concept 同时传时取交集 AND 语义）；state 已实现（Phase 4，标量子查询过滤 daily_developed_swing_dir）；固定 5 条 SQL（base+count+bars+snapshots+events），禁止 N+1；**`GET /market/boards?type=industry\|concept`** 返回 `MarketBoardsResponse`（`items` 板块目录列表 + `available: bool` items 非空时 true + `reason_code: str \| null` 无数据时 `board_provider_unavailable` + `updated_at`），前端依据 `available` 决定筛选输入是否禁用 |
| stock_context | `app/api/stock_context.py` | 个股状态上下文只读接口；`GET /api/v1/stocks/{symbol}/context`（require_active_subscription，admin 豁免）返回 StockState + 最近事件 + 数据质量，用户接口通过 `strip_internal_fields_for_user` 剥离 `sourceField`/`idempotencyKey`（返回 dict 而非 Pydantic 模型，字段在 JSON 中**完全移除**，不是 null）；`GET /api/v1/admin/stocks/{symbol}/debug`（require_admin）额外返回原始 payload + idempotencyKey；as_of 历史查询事件 occurred_at ≤ as_of 当日结束 |
| bars | `app/api/bars.py` | 行情查询；`GET /api/v1/instruments/{instrument_id}/quote` 返回实时报价可信状态（`source`/`is_realtime`/`freshness_seconds`/`degraded`/`degraded_reason`），只服务顶部行情卡片；**市值字段（CHANGE-20260713-010）**：响应含 `total_market_cap`/`float_market_cap`/`market_cap_as_of`/`market_cap_source`/`market_cap_degraded_reason` 5 个字段，由 `Instrument.total_share`/`float_share` 与当前价格计算，数据缺失时 `market_cap_degraded_reason="market_cap_data_unavailable"`，禁止用户请求时访问第三方；`GET /api/v1/instruments/{instrument_id}/bars` 返回 K 线并携带 `data_source`/`as_of`/`is_partial`/`degraded`/`degraded_reason`；`timeframe=1d&include_realtime=true` 且交易时段时返回 partial daily bar（`data_source=hybrid`/`is_partial=true`/`last_live_bar_time` 非空），收盘后/非交易时段 `is_partial=false`；`page_size` 最大 4000，与 Node Cluster 15m=4000/1h=1200 契约对齐；**15m/1h `trade_time` 必须返回 aware datetime（Asia/Shanghai tzinfo，序列化为 `+08:00`），1d `trade_date` 仍为 date 对象（无时区）**，避免前端 `new Date(naive ISO)` 在非亚洲时区浏览器中时区误判 |
| capture | `app/api/capture.py` | Capture Snapshot 专用 API |
| indicators | `app/api/indicators.py` | 策略指标实时计算；`bars` 最大 4000，与 bars API 及 Node Cluster 契约对齐；响应 `data.sqzmom_lb` 为全局技术指标，由 `app.strategy_assets.algorithms.features.sqzmom_lb.compute_sqzmom_lb` 计算，前端只渲染不计算；**响应含 `source_bar_times`/`source_bar_hash` 数据源诊断字段**，由 `app.services.chart_bars_service.compute_source_bar_times/hash` 按 `timeframe` 生成（15m/1h 用 `YYYY-MM-DDTHH:MM:SS`，1d 用 `YYYY-MM-DD`）；`indicator_service` 在所有周期（1d/15m/1h/1w/1mo）必须用 `macd_bars`（当前 timeframe bars）而非 `daily_bars` 计算，与 chart bars 同源；**indicator cache schema 版本**由 `indicator_cache.ALGORITHM_VERSION` 控制（当前 `v5`，PR #32：DSA 全周期 + 1w/1mo BB），修改计算逻辑/source 格式/BB 路径必须 bump 版本使旧缓存失效，禁止手动 `DEL` 单只股票 key；**DSA overlay 全周期支持**（PR #32 修正 PR #31 的 1d-only 限制），1d/15m/1h/1w/1mo 全部可渲染；`shouldCheckDsaMismatch` 全周期返回 true（全周期渲染，全部需校验 source 对齐）；DSA `MarketDataContext.bars_daily` 在所有周期使用 `macd_bars`，`daily_time_list` 用 `macd_bars.index`，DSA 不再仅由日线驱动；**BB/MACD/SQZMOM overlay 跟随当前 timeframe bars**，1d/15m/1h/1w/1mo BB 由 `_adapt_watchlist_bb` 用 `compute_bollinger(macd_bars, length=20, mult=2.0)` 计算（PR #32 修正：1w/1mo 不再移除 BB 字段），禁止日线阶梯线映射 |
| structural_factors | `app/api/structural_factors.py` | 双周期结构状态因子 V1.8（约 50 字段，含 dsa_segment 段收益/斜率/效率/段级成交量、swing_range/price_position、price_vs_poc_atr/value_area_position、distance_to_bb_*_atr/sqz_on/sqz_off、客观 relation primary_dir/secondary_dir/trend_alignment 等）；`GET /api/v1/instruments/{id}/structural-factors`，由 `app.services.structural_factor_service.compute_structural_factors` 计算，前端只渲染不计算；无认证要求；250-500 bar lookback；契约详见 `docs/current/02-data-api-contracts.md` 第 10 节 |
| temporal_features | `app/api/temporal_features.py` | 时序特征 V1（双周期 1d+15m，9+9+3 字段，含变化量/持续度/派生关系）；`GET /api/v1/instruments/{id}/temporal-features`，由 `app.services.temporal_feature_service.compute_temporal_features` 计算；复用 V1.8 `compute_structural_factors` 获取 primary/secondary factors；point-in-time 重算历史 SQZMOM/BB/volume_percentile，无未来函数；无认证要求；V1 只支持 `as_of=latest`；契约详见 `docs/current/02-data-api-contracts.md` 第 11 节 |
| strategies | `app/api/strategies.py` | 策略目录/版本 |
| strategy_runs | `app/api/strategy_runs.py` | 策略运行/结果；`/strategy-runs/{run_id}/results` 以 `strategy_run_items` 为主表 LEFT JOIN `strategy_results` + `instruments`，返回全量 universe（含 succeeded/skipped/failed），新增 `item_status`/`reason_code`/`error_message` 字段。JOIN 策略：因 `strategy_run_items.result_id` 当前未回填（ALIGN-033 P2），统一改用 `(run_id, instrument_id)` 关联 `strategy_results`，包括 `selectinload` 替代批量加载、metric_filter 子查询、sort LEFT JOIN 三处。**keyword 搜索（CHANGE-20260713-005）**：`strategy_result_repository.query_results` 的 `keyword` 参数（非空时）ILIKE 同时匹配 `Instrument.symbol`/`Instrument.name`/`Instrument.pinyin_initials`（2 处 or_ 分支同步：keyword 过滤、filtered_total 子查询）；`total` 字段为该 keyword + filters 下的真实总数（不是 items.length）。**industry/concept 筛选（CHANGE-20260713-006）**：`/strategy-runs/{run_id}/results` 支持 `industry`（str \| None, Query）和 `concept`（str \| None, Query）参数，按行业/概念板块名称筛选，通过共享 `backend/app/repositories/board_filter_helper.py::build_board_filter_conditions` 构造 EXISTS 子查询（`MarketBoardMembership` JOIN `MarketBoard`，`type='industry'`/`'concept'`，`name` 匹配）；industry+concept 同时提供时为 AND 语义（两个 EXISTS 条件 AND 连接）。**数量契约四层语义（CHANGE-20260713-007）**：响应含四层数量——`source_total`（published run 原始总量，**不受 keyword/industry/concept/metric_filters 业务筛选影响，也不受 universe=all/watchlist 范围影响**，恒等于 `run.total_instruments` 或 `count_run_items_by_run` fallback）、`universe_total`（all/watchlist 范围总量，业务筛选前；universe=all 时等于 source_total，universe=watchlist 时为自选股范围内的 run_items 数）、`filtered_total`（keyword+industry+concept+metric_filters 后总量）、`items`（filtered_total 当前页）；`len(items) <= filtered_total`；`items`/`filtered_total` 必须应用完全相同的 keyword/industry/concept/metric_filters 条件；SQL 数量固定，禁止 N+1；**禁止文档描述"source_total 与筛选使用相同条件"或"source_total 受 universe 影响"**。**Excel 导出端点（CHANGE-20260713-010）**：`POST /strategy-runs/{run_id}/results/export` 复用 `query_published_selector_results` 同一筛选/排序构造器，导出完整筛选结果（非当前页），上限 `MAX_EXPORT_ROWS=10000` 超 422，响应头含 `X-Source-Total`/`X-Universe-Total`/`X-Filtered-Total`/`X-Export-Rows`，MIME `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`，Content-Disposition RFC 5987 编码文件名 `盘迹_DSA_YYYYMMDD_筛选结果.xlsx`；`excel_export_service` 使用标准库 `zipfile`+`xml.etree` 生成真实 .xlsx（禁止 `openpyxl`/`xlsxwriter`），公式注入防护 `=`/`+`/`-`/`@` 前缀单引号，百分比 `numFmt 0.00%`；`stock` 列 `payload_key=null` 不导出操作列；详见 `docs/current/02-data-api-contracts.md` 第 16 节 |
| monitor_states | `app/api/monitor_states.py` | 监控状态 |
| strategy_events | `app/api/strategy_events.py` | 策略事件 |
| notifications | `app/api/notifications.py` | 消息与通知渠道；`POST /notification-channels/{channel_id}/test-latest-event` 仅管理员可用，普通用户 403 detail 提示使用普通测试接口；`POST /notification-channels/{id}/test` 对所有用户可用，测试成功后渠道置为 active |
| admin_subscription | `app/api/admin_subscription.py` | 订阅/邀请码/调度任务/Worker 心跳/消息投递管理 |
| admin_beta_applications | `app/api/admin_beta_applications.py` | 内测申请管理 |
| admin_after_close | `app/api/admin_after_close.py` | 盘后编排管理；`/after-close-runs/dsa-only` 支持 fallback 到最新可用交易日，覆盖率门禁使用 `coverage_raw` 原始值；新增 `/after-close/pipeline/latest`、`/after-close/pipeline?trade_date=`、`/after-close/pipeline/runs?limit=`、`POST /after-close/pipeline/run`（admin，幂等：同 trade_date 已有 queued/running/succeeded 返回 existing）4 个聚合状态端点，响应模型 `AfterClosePipelineResponse` 含 8 步骤时间线 + watchlist_ready 严格判定（`status='succeeded' AND published_at IS NOT NULL AND metadata_.scope='full'`，sample backfill 不计入）+ data_freshness + 最近 100 条 events |
| watchlist | `app/api/watchlist.py` | 用户自选股；`GET /watchlist/monitor-status` 响应 `metrics` 唯一来自 `stock_feature_snapshots.summary_payload`（`_source='feature_snapshot'`），不再走 `MonitorSnapshotService` 实时计算或 `MonitorState.payload` fallback；新增 `calculation_status` 三态（SUCCEEDED/WAITING_SNAPSHOT/NO_SNAPSHOT）；`MonitorEvaluation` 仅展示评估状态字段（evaluation_status/retry_count/error_code/source_bar_time），不作为 metrics 数据源；`freshness_seconds` 基于 `snapshot.updated_at` |
| me_table_view_presets | `app/api/me_table_view_presets.py` | 用户表格视图配置 CRUD；`GET /me/table-view-presets`（按 table_id + strategy_key 过滤）、`POST /me/table-view-presets`（创建，201）、`PATCH /me/table-view-presets/{id}`（更新 name/config/is_default）、`DELETE /me/table-view-presets/{id}`（删除，204）；权限：`require_active_subscription` + `require_feature("trend_selection")`（admin 豁免）；JWT user_id 隔离（user_id 由认证上下文注入，不接受 body 传入）；config 仅允许 keyword/sort/filters/hiddenColumns/columnOrder/pageSize（Pydantic `TableViewPresetConfig` extra="forbid"，columnOrder 为 CHANGE-20260713-004 新增）；禁止 selectedKeys/page/activeRunId/rows/resultData；每 user+table_id+strategy_key 最多 20 个（quota 422）；`(user_id, table_id, strategy_key, name)` 唯一约束（冲突 409）；is_default 同维度互斥（设置新默认时旧默认自动取消）；**写操作（POST/PATCH/DELETE）在返回前必须 `await db.commit()`，异常分支 rollback 后 re-raise，写后读跨请求可见**；契约详见 `docs/current/02-data-api-contracts.md` 第 14 节 |
| stock_memos | `app/api/stock_memos.py` | 个股备忘录 |
| stock_detail_feishu | `app/api/stock_detail_feishu.py` | 个股详情发送飞书 |
| public_beta | `app/api/public_beta.py` | 公开内测申请 |
| plans | `app/api/plans.py` | 套餐列表 |
| metrics | `app/api/metrics.py` | Prometheus 指标 |

## 2. 权限核对要点

- 核心业务 API 必须 active subscription；
- Admin API 必须 admin；
- Capture API 必须 Capture Token；
- 消息/渠道必须按 JWT user_id 所有权隔离；
- 到期用户只允许历史消息只读和续期相关能力。

## 3. 修改 API 前检查

```text
1. 是否需要更新 current/02-data-api-contracts.md；
2. 是否需要更新 frontend adapter；
3. 是否需要 API 测试覆盖 active/expired/admin；
4. 是否需要更新 maps/api-route-map.md；
5. 是否需要 CHANGE 和 alignment。
```
