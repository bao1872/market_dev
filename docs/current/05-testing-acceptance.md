# 05 测试、CI 与验收

## 1. 测试数据库

所有数据库集成测试使用 PostgreSQL 测试库和真实 Alembic。禁止 SQLite、aiosqlite、内存数据库、测试手写生产 Schema 和模块级 db_session 覆盖。

## 2. 测试层级

| 层级 | 覆盖 |
|---|---|
| Unit | 纯函数、算法、状态转换 |
| Integration | PostgreSQL、ORM、Service、事务、锁、Worker |
| API | 认证、资格、所有权、响应、错误 |
| Frontend | Adapter、路由、状态、交互 |
| E2E | 用户操作到数据库、消息、飞书、截图 |
| Deployment | Compose、迁移、健康、版本、Worker |

## 3. 关键回归

- 趋势选股：universe、result_count、partial_failed 禁止发布、分页不改变全量；
- 权限订阅：active/expired/no-subscription/disabled/admin/用户 A-B；
- 盘中监控：完成 1m Bar、幂等、投递前资格复核；
- 行情聚合：尾部补齐、partial、degraded、页面/指标/截图同源；
- 飞书：文字图片成功、partial_failed、仅重试图片、不重复文字、所有权；
- 管理任务：真实 API、审计日志、run key、heartbeat、lease、stale recovery、Worker Git SHA。
- SQZMOM_LB：
  - 后端算法单元测试覆盖 Pine 等价（`dev = multKC * stdev`、`linreg offset=0`、`nz(val[1])` 颜色逻辑、数据不足不 500）；
  - `indicator_service` 在 `compute_all_indicators` 中注入 `sqzmom_lb` 图层与序列；
  - 前端 contract test 覆盖：开关默认关闭、renderer 已注册、独立 pane 分配、API 缺失不崩溃、前端不重新计算指标。
- 结构状态因子：
  - 后端 `test_atr_utils.py` 覆盖 ATR SSOT 与 Pine RMA 等价（首根 TR、RMA seed、数据不足、空输入、返回类型）；
  - 后端 `test_structural_factor_service.py` 覆盖 5 组因子（DSA 段/Swing/成本节点/动量波动/成交参与）+ 异常隔离 + meta 结构 + 无未来函数；
  - 后端 `test_structural_factors_api.py` 覆盖 API 路由（合法请求、非法 timeframe/adj、不存在 instrument、meta 结构）；
  - 前端 `structural-state-panel.test.ts` contract test 覆盖：React 组件存在、使用 `useStructuralFactors` hook、双周期 tabs、5 张卡片、null 占位、降级提示、API 失败处理、前端不重新计算因子。
- 结构状态因子 V1.8：
  - 后端 `test_structural_factor_service.py` 新增 V1.8 测试：`test_v18_dual_period_difference`（构造不同 1d/15m bars，断言字段结构相同但数值不同）、`test_v18_no_future_function_confirmed_pivots`（修改最后一根 bar 不影响已确认 swing pivot）、`test_volatility_v18_sqz_on_off`（sqz_on/sqz_off 互斥）、Relation `primary_dir`/`trend_alignment` 测试、DSA 单 segment/双 segment 段收益与段间对比测试、Swing position/retracement 测试、Node degraded 测试、SQZMOM abs percentile 测试；
  - 前端 `structural-state-panel.test.ts` 新增 V1.8 字段存在性断言：`v18Keys`（33 项含 distance_to_bb_upper_atr/sqz_on/current_dsa_segment_dir/current_vs_prev_volume_ratio 等）+ `v18RelationKeys`（7 项含 primary_dir/secondary_dir/trend_alignment 等），并断言已移除 `momentum_alignment` 引用。
- 时序特征 V1（Temporal Features V1）：
  - 后端 `test_temporal_feature_service.py` 覆盖：daily_context 9 字段结构、duration percentile 公式、sqzmom/volume change since segment start（point-in-time）、m15 swing anchor 选择规则（bsl<bsh→anchor=low / bsh<=bsl→anchor=high）、m15_position_change_since_swing_anchor 手算验证、anchor 处 volume_percentile/bb_bandwidth_percentile 修改后不变（point-in-time）、m15 position/sqzmom/bb_bandwidth/volume change since anchor、derived_relation 只由 daily + m15 派生、alignment direction 4 种情况、intensity mean(abs)、数据不足 null + warmup_notes、单字段失败异常隔离、无未来函数、组级异常隔离（mock m15_response 抛异常 → 整体 200 + m15_response 全 null + degraded_reasons）；
  - 后端 `test_temporal_features_api.py` 覆盖 API 路由（合法请求、非法 timeframe/adj、`as_of != "latest"` 返回 400、不存在 instrument 200 + degraded、meta 结构）；
  - 前端 `structural-state-toggle.test.ts` contract test 覆盖：面板默认隐藏、开关按钮存在、localStorage 持久化、`hideStructuralState=1` 强制隐藏、`capture=1` 强制隐藏、`capture=feishu` 强制隐藏、强制隐藏时禁用 toggle、toggle 按钮在 `tv-chart-column` 内部（以 `position: relative` 为定位上下文）；
  - 验收：所有 anchor 取值必须 point-in-time，不得使用未来 bar；15m 不使用 DSA 位置类字段作为核心输入；V1 只支持 as_of=latest；任一组（daily/m15/derived）异常不得导致整体 API 500。

## 3.1 本轮新增回归

- **[PR #74 阶段二 StockContext reasonCode]** `test_stock_state_and_events.py` 新增 6 项 reasonCode API 测试 + 1 项无写副作用测试：
  - `test_p02_context_no_published_full_run`：无 succeeded+published+full run 时 reasonCode=`no_published_full_run`，state=null
  - `test_p02_context_snapshot_missing`：run 有但 snapshot 缺失时 reasonCode=`snapshot_missing`，state=null
  - `test_p02_context_exact_source_run_id`：精确匹配 source_run_id 时 state 非 null，reasonCode=null
  - `test_p02_context_snapshot_run_not_linked`：legacy snapshot source_run_id=NULL 时 reasonCode=`snapshot_run_not_linked`（内部函数层面），API 层面 state 非 null（legacy 匹配成功）
  - `test_p02_context_legacy_snapshot_ambiguous`：legacy snapshot 多 run 候选时 reasonCode=`legacy_snapshot_ambiguous`
  - `test_p02_context_normal_returns_state`：正常场景 state 非 null，reasonCode=null
  - `test_p02_context_get_no_write_side_effect`：连续 3 次 GET context 后 snapshot/run 行数不变（GET 只读）
- **[PR #74 阶段二 快照归属修复工具]** `test_repair_snapshot_run_ownership.py` 新增 2 项测试：
  - `test_repair_dry_run_does_not_write`：dry-run 只查询不写库，source_run_id 仍为 NULL
  - `test_repair_apply_writes_and_idempotent`：第一次 apply 写入 1 行，第二次 apply 0 行（幂等，WHERE source_run_id IS NULL）
- **[PR #74 阶段二 EventStatePanel reasonCode 文案]** `frontend/src/features/research-context/__tests__/reasonCodeMessages.test.ts` 新增 8 个子测试覆盖 `getReasonCodeMessage` 纯函数：no_published_full_run / snapshot_missing 含/不含 runTradeDate / snapshot_run_not_linked 含"待修复归属" / legacy_snapshot_ambiguous / null / 未知 code / 所有已知 code 非默认文案
- `BarsCoverageService` 统一 A 股口径，排除指数/ETF，默认使用 `shanghai_business_date`，返回 `coverage`（展示）与 `coverage_raw`（阈值判断）；
- `/admin/after-close-runs/dsa-only`、`bars_scheduler`、系统概览 `WAITING_DSA` 判定等覆盖率门禁使用 `coverage_raw` 原始值；
- `/admin/after-close-runs/dsa-only` 当日无数据时 fallback 到最新交易日，覆盖率不足返回 409；
- `/watchlist/monitor-status` 无 `MonitorState` 或 `payload` 无效时通过 `MonitorSnapshotService` fallback 返回指标，单只失败单行降级（**已废弃，见 3.6 节**：fallback 已删除，metrics 改为读 `stock_feature_snapshots.summary_payload`）；
- 飞书消息时间统一格式化为 Asia/Shanghai，文本中触发时间显示 CST；
- 前端 `mergeRealtimeQuoteIntoBars` 不修改原数组、1d 保留日期语义、intraday 使用 `quote.update_time`。
- admin monitor 资格：
  - `test_monitor_eligible.py` 覆盖 `filter_monitor_eligible_recipients`/`is_user_eligible_for_monitor`：active admin 放行、active member + 有效 subscription 放行、disabled admin 排除、无订阅普通用户排除；
  - `monitor_batch_service`/`event_recipient_service`/`outbox_relay` 三处统一使用监控资格过滤，outbox relay 端到端一致性测试验证 MessageDelivery 生成数量符合预期。
- 实时行情可信化：
  - `test_quote_trustworthy.py` 覆盖交易时段 pytdx 成功（`source=pytdx`、`is_realtime=true`、`degraded=false`）、交易时段 pytdx 失败降级（`source=daily_fallback`、`degraded=true`）、非交易时段 fallback（`degraded=false`）、无数据 404、Redis 缓存命中不走 pytdx；
  - `scripts/verify_quote_trustworthy.py` 本地 ASGI 端到端验证三个场景并输出 curl 示例；
  - 前端 `chart.test.ts` 覆盖不可信 quote 不合并入 K 线、1d 日期语义、intraday 使用 `quote.update_time`。
- monitor 投递与 live bar 后续修复：
  - `test_delivery_worker_monitor_eligible.py` 覆盖 `delivery_worker` 对 `monitor_event` 使用 `is_user_eligible_for_monitor`：active admin 放行、active member + 有效 subscription 放行、disabled admin 排除、无订阅普通用户排除；
  - `test_monitor_batch_live_minute.py` 覆盖 `monitor_batch_service.execute_monitor_cycle` 使用 `include_realtime=True` 拉取 1m、剔除最后一根未完成 bar、记录 `last_minute_bar_time`/`last_minute_data_source`；
  - `test_market_data_aggregation_partial_daily.py` 覆盖交易时段 1d 合成 partial daily bar（`data_source=hybrid`、`is_partial=true`、`last_live_bar_time` 非空）、非交易时段不合成；
  - `test_market_data_aggregation_partial_daily.py::test_partial_daily_fetch_minute_bars_uses_aware_datetime` 与 `test_market_data_aggregation_partial_daily.py::test_intraday_1m_fetch_minute_bars_uses_aware_datetime` 覆盖 `MarketDataAggregationService` 调用 `fetch_minute_bars` 时 `start_time`/`end_time` 必须同为 `Asia/Shanghai` aware datetime，禁止 naive/aware 混用；
  - `test_pytdx_adapter_minute_aware.py` 覆盖 `pytdx_adapter.get_minute_bars` 接收 aware `Asia/Shanghai` start/end 时，能正确与 pytdx 返回的 naive `datetime` 列比较过滤，不再抛出 `Invalid comparison between dtype=datetime64[us] and Timestamp`；
  - `test_monitor_batch_live_minute.py::test_monitor_cycle_1m_uses_include_realtime` 覆盖 `monitor_batch_service` 调用 MDAS 1m 时必须带 `include_realtime=True`；
  - `test_quote_timezone.py` 覆盖 `/quote` 返回 `update_time` 带 `+08:00`、UTC 字符串被修正为 `+08:00`。
- **[CHANGE-20260714-001 latest_change_pct]** `test_latest_change_pct.py` 新增 9 项测试覆盖 `/strategy-runs/{run_id}/results` 响应中 `latest_change_pct`/`latest_change_trade_date` 字段（从 `bars_daily` 表用 window function 计算最新两根完成交易日涨跌幅）：
  1. `test_latest_change_pct_normal_two_bars`：正常两根有效日线（T-1 close=10、T close=11）→ `latest_change_pct=10.0`、`latest_change_trade_date=T`
  2. `test_latest_change_pct_after_close_incomplete_bar_excluded`：盘后 T 日 bar 未完成（`is_partial=true` 或未入库）→ 使用 T-1/T-2 两根完成日线计算，`latest_change_trade_date=T-1`
  3. `test_latest_change_pct_single_bar_returns_null`：只有一根日线（新股）→ `latest_change_pct=null`、`latest_change_trade_date=null`
  4. `test_latest_change_pct_null_close_returns_null`：最新 bar `close=null`（停牌/数据缺失）→ `latest_change_pct=null`
  5. `test_latest_change_pct_prev_close_zero_returns_null`：前一日 `close=0`（异常数据）→ `latest_change_pct=null`（避免除零）
  6. `test_latest_change_pct_color_logic_red_up_green_down`：A 股红涨绿跌——`latest_change_pct > 0` 时前端 `changePctColorClass` 返回红、`< 0` 返回绿、`null` 返回中性（前端 contract test 覆盖）
  7. `test_latest_change_pct_sort_desc`：`sort_by=change_pct&sort_desc=true` → 结果按 `latest_change_pct` 降序排列（null 排末尾），走 `CHANGE_PCT_METRIC_KEY` 特殊 sort 路径
  8. `test_latest_change_pct_filter_gt`：`metric_filters=[{key: change_pct, op: gt, value: 3}]` → 只返回 `latest_change_pct > 3` 的行，走 `CHANGE_PCT_METRIC_KEY` 特殊 filter 路径
  9. `test_latest_change_pct_no_n_plus_1`：一次请求返回 N 只股票的 `latest_change_pct`，SQL 查询数固定（window function 子查询批量计算，不逐行查询 `bars_daily`）；`len(items) <= filtered_total`
  - 运行命令：`APP_ENV=test TEST_DATABASE_URL=postgresql+psycopg://... pytest backend/tests/test_latest_change_pct.py -q`
  - 回归要求：修改 `backend/app/repositories/strategy_result_repository.py`（`_build_latest_change_pct_subquery`/`_fetch_latest_change_pct_map`/`CHANGE_PCT_METRIC_KEY`）、`backend/app/api/strategy_runs.py`、`bars_daily` 表结构或索引时必须跑此测试。

### 壳层与导航拆分

- 前端 `src/navigation/__tests__/appNavigation.test.ts` 覆盖：
  1. 用户一级导航仅含 `/market`（行情）和 `/screener`（趋势选股），不含消息/设置/总览/自选入口；
  2. 账户菜单中管理后台入口仅 `is_admin=true` 可见（`getAccountMenuItems(isAdmin)` 过滤）；
  3. 旧路由兼容重定向：`/overview` → `/market`、`/watchlist` → `/market?scope=watchlist`；
  4. 管理员路由集中于 `/admin/*`（`ADMIN_NAV_ITEMS` 全部以 `/admin` 开头）；
  5. Capture 路由 `/capture/stock/:symbol` 不在用户/管理员导航或账户菜单中；
  6. 默认登录/兜底入口为 `/market`（`DEFAULT_ENTRY`）。
- 运行命令：`node --experimental-strip-types --test src/navigation/__tests__/appNavigation.test.ts`
- 回归要求：修改 `App.tsx`、`navigation/appNavigation.ts`、`UserAppShell.tsx`、`AdminAppShell.tsx`、`AccountMenu.tsx` 或路由结构时必须跑此测试。
- `tsc --noEmit` 零错误；改动文件 eslint 零错误（既有 warnings 不计）。

### 统一行情工作区

- 前端 `src/features/market-workspace/__tests__/marketWorkspaceUrlState.test.ts` 覆盖（含 `normalizeInternalReturnTo` 安全校验）：
  1. URL parse/serialize 往返一致（scope/symbol/timeframe/source/strategy/event_id/returnTo）；
  2. 非法 timeframe 回退 1d；
  3. 非法 source 回退 watchlist；
  4. source=watchlist 默认 strategy=watchlist_monitor，source=selection 默认 strategy=dsa_selector；
  5. strategy 等于 source 默认值时 encode 省略 strategy；
  6. event_id=null 时 encode 不含 event_id（选择新股票清除旧 event_id）；
  7. buildMarketWorkspaceUrl 生成完整 URL（含/省略 strategy 场景）；
  8. `selectInstrumentFromMarketPane(state, newSymbol)`：从 selection+dsa_selector+event_id 上下文选股后，source 重置为 watchlist、strategy 重置为 watchlist_monitor、eventId 清为 null，scope/symbol/timeframe 正确更新；
  9. `changeMarketScope(state, 'watchlist')` 和 `changeMarketScope(state, 'market')`：切换 scope 时退出 selection 上下文（source→watchlist、strategy→watchlist_monitor、eventId→null），保留 timeframe；
  10. 选择股票后 timeframe 不变（timeframe 在选股和切 scope 时必须保留）；
  11. `returnTo` 参数解码（URL 编码的路径含 `&` 字符正确解析）；
  12. `returnTo` 非 null 时 encode 写入，null 时省略；
  13. `selectInstrumentFromMarketPane` 选股后清除 `returnTo`；
  14. `changeMarketScope` 切 scope 后清除 `returnTo`；
  15. `normalizeInternalReturnTo` 白名单前缀（`/screener`、`/market`、`/messages`）通过；
  16. `normalizeInternalReturnTo` 拒绝外部 URL（`http://`、`https://`、`//`、`javascript:`）；
  17. `normalizeInternalReturnTo` 拒绝非白名单前缀（`/admin`、`/login`、`/capture/stock`、`/settings`、`/stock`）；
  18. `normalizeInternalReturnTo` 拒绝超长字符串（>2000 字符）；
  19. `normalizeInternalReturnTo` 空/null/undefined 返回 null；
  20. `normalizeInternalReturnTo` 允许白名单前缀 + query/hash。
  - `debug` 不在 `/market` URL 契约中（已移除，管理员调试独立到 `/admin/stock-debug`）。
- 前端 `src/navigation/__tests__/appNavigation.test.ts` 覆盖 `getAccountMenuItemsForVariant`：
  1. variant=user + isAdmin=false → 只有消息+设置；
  2. variant=user + isAdmin=true → 消息+设置+管理后台；
  3. variant=admin → 消息+设置+返回行情（无管理后台）；
  4. variant=admin + isAdmin=true 仍不显示管理后台。
- 前端 `src/navigation/__tests__/routeStructure.test.ts` 覆盖 Capture 路由回归（位于 ProtectedLayout 之外，不渲染任一壳层）。
- 前端 `src/pages/__tests__/detailNavigation.test.ts` 覆盖（7 个用例）：watchlist fallback 改为 `/market?scope=watchlist`、`buildMarketEntryFromScreener` 生成含 scope/symbol/source/strategy/returnTo 的 URL、`buildMarketEntryFromMessage` 生成含 symbol/event_id 的 URL、`/stock/:symbol` 兼容路由 URL、`buildStockDetailState` 携带 returnTo、`resolveBackPath` 优先 returnTo、`resolveBackPath` 无 returnTo 时按 source fallback。
- 浏览器 E2E（CDP，禁止安装 Playwright）：使用现有 Chromium + Node 22 `fetch`/`WebSocket` 通过 DevTools Protocol 执行，临时脚本 `/tmp/market-workspace-e2e.mjs`，profile `/tmp/panji-cdp-profile`，无法登录时用 `Fetch.enable` 域 mock 必要 API（mock 和脚本只能放 `/tmp`）。必须验证 8 项场景，每项输出 PASS/FAIL 和实际请求 URL：
  1. 打开 `/market?scope=market&symbol=<有效股票>&timeframe=15m`：工具栏选中 15m，bars 请求含 `timeframe=15m`，indicators 请求含 `timeframe=15m`；
  2. 点击工具栏 1h 按钮：URL 写入 `timeframe=1h`，bars 请求切为 `timeframe=1h`，indicators 请求切为 `timeframe=1h`，source/strategy/event_id 按规则保留；
  3. market scope 空关键词和 1 字符：0 次 instruments 请求、0 次 monitor-status 请求；2 字符后仅发 1 次 instruments 请求（不发 monitor-status）；
  4. 切 watchlist scope：只发 monitor-status 请求，不发 instruments 搜索请求；
  5. 从 `source=selection&strategy=dsa_selector&event_id=x` 进入后，点击左栏股票：URL 变为 `source=watchlist`、`strategy=watchlist_monitor`、`event_id` 消失；
  6. 收起右栏后选择另一只股票：新股票 0 次 structural-factors 请求、0 次 temporal-features 请求；
  7. `/capture/stock/:symbol` 不出现 UserAppShell 和 AdminAppShell（无导航、无账户菜单）；
  8. `/stock/:symbol` 原详情页可加载（body 有内容）。
- E2E 结果（2026-07-11）：22 项断言全部 PASS，覆盖 8 个场景；mock 数据形状对齐后端 DTO（`StructuralFactorResponse`/`TemporalFeaturesResponse`/`IndicatorResponse`/`BarListResponse`/`WatchlistMonitorStatusItem`），`navigateAndWait` 采用 800ms 延迟 + 连续 2 次条件为真防 stale DOM。
- 运行命令：`node --experimental-strip-types --test src/features/market-workspace/__tests__/marketWorkspaceUrlState.test.ts src/navigation/__tests__/appNavigation.test.ts src/navigation/__tests__/routeStructure.test.ts src/pages/__tests__/detailNavigation.test.ts`
- 回归要求：修改 `MarketWorkspacePage`、`marketWorkspaceUrlState.ts`、`StockResearchWorkspace.tsx`、`useStockResearchData.ts`、`MarketToolbar.tsx`、`MarketStockTable.tsx`、`appNavigation.ts`、`AccountMenu.tsx`、`detailNavigation.ts` 或路由结构时必须跑此测试。

### 共享研究核心

- 前端 `src/features/stock-research/__tests__/stockResearchTypes.test.ts` 覆盖（13 个用例）：
  1. `ALLOWED_TIMEFRAMES` 包含 `1d`/`15m`/`1h`/`1w`/`1mo` 五个值且顺序固定；
  2. `DEFAULT_TIMEFRAME === '1d'`；
  3. `DEFAULT_SOURCE === 'watchlist'`；
  4. `BARS_COUNT_BY_TIMEFRAME` 各周期取值与 Node Cluster 契约一致（1d=250、15m=4000、1h=1200、1w=500、1mo=500）；
  5. `defaultStrategyForSource('watchlist') === 'watchlist_monitor'`；
  6. `defaultStrategyForSource('selection') === 'dsa_selector'`；
  7. `defaultStrategyForSource` 对未知 source 抛错；
  8. `normalizeDisplayTimeframe('1d')` 原样返回合法值；
  9. `normalizeDisplayTimeframe('15m')` 原样返回合法值；
  10. `normalizeDisplayTimeframe('2h')` 回退为 `1d`（非法值回退）；
  11. `normalizeDisplayTimeframe(undefined)` 回退为 `1d`（缺省回退）；
  12. `normalizeResearchSource('watchlist')` 原样返回合法值；
  13. `normalizeResearchSource(undefined)` 回退为 `watchlist`（缺省回退）。
- 运行命令：`node --experimental-strip-types --test src/features/stock-research/__tests__/stockResearchTypes.test.ts`
- 回归要求：修改 `stockResearchTypes.ts`、`marketWorkspaceUrlState.ts`、`useStockResearchData.ts`、`StockDetailPage.tsx` 时必须跑此测试。
- 测试实现说明：因 `marketWorkspaceUrlState.ts` 直接被 `node --experimental-strip-types --test` 执行，导入 `stockResearchTypes` 必须带 `.ts` 扩展名（相对路径），不得使用 `@/` 别名。

### 研究上下文纯函数

- 前端 `src/features/research-context/__tests__/buildStructureSummary.test.ts` 覆盖（18 个用例）：null 输入返回空摘要；degraded_reasons 合并；warmup_notes 合并；daily_context 提取日线摘要；m15_response + derived_relation 提取 15m 摘要；`primary[timeframe].cost_position.position_0_1` DTO 路径正确读取；`price_vs_poc_atr` 距离；`poc_price` 提取；`nearest_upper_node.price_mid` 节点价；`nearest_lower_node.price_mid` 节点价；primary 为空/null/无 cost_position 子组时返回 null；非数字字段处理。
- 前端 `src/features/research-context/__tests__/buildUserEventExplanation.test.ts` 覆盖（19 个用例）：null eventDetail 返回 hasEvent=false；eventLabel 回退到原 eventType；`payload.facts[]` 数组提取价格（`current_price`/`price`/`现价` 白名单键）；顶层字段提取价格（`current_price`/`price`/`last_price`/`close_price`）；正则提取价格；evidence 只消费 `text_content` + `summary` 并去重；instrument mismatch（`event.instrument_id` ≠ `currentInstrumentId`）时 `instrumentMismatch=true`；`currentInstrumentId` 为 null 时不校验；`eventDetail.instrument_id` 为 null 时不校验；两者都有且一致时 `instrumentMismatch=false`。
- 运行命令：`node --experimental-strip-types --test src/features/research-context/__tests__/buildStructureSummary.test.ts src/features/research-context/__tests__/buildUserEventExplanation.test.ts`
- 回归要求：修改 `buildStructureSummary.ts`、`buildUserEventExplanation.ts`、`StructureSummaryCard.tsx`、`EventExplanationCard.tsx` 或后端 `StructuralFactorResponse`/`TemporalFeaturesResponse`/`StrategyEventDetail` DTO 时必须跑此测试。
- 纯函数无 React 依赖，可被 `node --test` 直接运行，不需要 DOM 环境。

### /market 和 /stock 共享研究核心 E2E

- 浏览器 E2E（CDP，禁止安装 Playwright）：复用 CDP 工具链（Node 22 `fetch`/`WebSocket` + DevTools Protocol），临时脚本 `/tmp/stock-detail-e2e.mjs`，profile `/tmp/panji-cdp-profile-stage4`，通过后端 `create_access_token` 生成 JWT 并注入完整 Zustand persist `auth-store` 状态。
- 必须验证 9 类场景，共 39 项断言，每项输出 PASS/FAIL：
  1. `/market?symbol=<X>&timeframe=15m` 与 `/stock/<X>?timeframe=15m` 的 bars/indicators 请求参数一致（同 symbol、同 timeframe、同 bars_count）；
  2. `/stock/:symbol` 切换 1h 后 URL 写入 `timeframe=1h`，bars/indicators 请求切为 1h，图表渲染 1h；
  3. `/stock/:symbol` 仅一组 instrument/bars/indicators/quote/events 请求（无重复请求链路）；
  4. 详情页 header、返回按钮、价格卡片、上一只/下一只、加入/移出自选、备忘录（保存/删除）、结构面板开关、全屏按钮均存在且可交互；
  5. 飞书分享按钮存在，点击触发 `POST /capture/snapshot` 创建任务，轮询 `GET /capture/snapshot/:id` 直至 succeeded/failed 或超时（mock 链路验证）；
  6. `/capture/stock/:symbol` 无 `UserAppShell`/`AdminAppShell`（无 topbar、无 sidebar、无账户菜单），请求契约不变（仅 `captureClient`）；
  7. `/stock/:symbol` 非实时非降级状态文案为"行情回退"（非"日线回退"），partial 文案含当前周期；
  8. `/market` 22 项回归继续通过（timeframe 单一真源、scope 互斥、selection 上下文重置、搜索渲染门控、capture 隔离）；
  9. `StockDetailPage` 不再包含独立的 `useBars`/`useIndicators`/`useRealtimeQuote`/`useInstrumentEvents`/`barsCount` 映射/`StrategyChart` 重复实现。
- E2E 结果（2026-07-11）：39 项断言全部 PASS，覆盖 9 个场景。
- 回归要求：修改 `StockDetailPage.tsx`、`useStockResearchData.ts`、`useStockDetailActions.ts`、`useStockDetailFeishu.ts`、`StockResearchWorkspace.tsx`、`stockResearchTypes.ts` 或路由结构时必须跑此 E2E。
- 临时文件约束：脚本/profile/日志/vite build 输出仅放 `/tmp`，验证结束后必须删除；不得保存截图/video/trace。

### 原型最终对齐阻断验收

- 前端 `src/features/market-workspace/__tests__/marketWorkspaceUrlState.test.ts` 覆盖 `normalizeInternalReturnTo` 安全校验（白名单前缀、外部 URL 拒绝、`javascript:` 拒绝、双斜杠拒绝、非白名单前缀拒绝、超长字符串拒绝、空/null/undefined 处理）。
- 前端 `src/pages/__tests__/detailNavigation.test.ts` 覆盖 returnTo 安全校验集成（白名单 returnTo 通过、外部 URL 拒绝、非白名单前缀拒绝、超长 returnTo 拒绝、`resolveBackPath` 外部 URL 时按 source fallback、`resolveBackPath` 非白名单前缀时按 source fallback）。
- 浏览器 E2E（CDP）：使用现有 Chromium + DevTools Protocol，临时脚本 `/tmp/prototype-alignment-e2e.mjs`，profile `/tmp/panji-cdp-profile-final`。必须验证：Screener/Messages 跳转 URL 含 returnTo/event_id；event 详情成功/404/错误状态；普通用户 `/market` 不显示原始因子/JSON；管理员 `/admin/stock-debug/:symbol` 可见原始 factor/feature/JSON；`/market?debug=1` 管理员重定向到 `/admin/stock-debug/:symbol`，普通用户清除 debug；右栏关闭后 event/structural/temporal 新请求为 0；market/stock 同周期请求一致无重复；指标开关/全屏/自选/memo/飞书/上下只正常；capture 隔离；1024/1440/1920 无横向溢出、左右栏折叠和中心扩展正确；旧 /overview、/watchlist、/stock 链接可用。
- 回归要求：修改 `ResearchContextPanel`/`EventExplanationCard`/`StructureSummaryCard`/`AdminFactorDebugPanel`/`useResearchContext`/`marketWorkspaceUrlState.ts`/`detailNavigation.ts`/`MarketWorkspacePage.tsx`/`ScreenerPage.tsx`/`MessagesPage.tsx`/`StockStructuralStatePanel.tsx` 或删除页面时必须跑此组测试。
- 临时文件约束：脚本/profile/日志/vite build 输出仅放 `/tmp`，验证结束后必须删除；不得保存截图/video/trace。

## 3.2 K线实时契约门禁（blocking）

- 任何修改 `backend/app/api/bars.py`、`backend/app/services/market_data_aggregation_service.py`、`backend/app/core/pytdx_adapter.py`、`frontend/src/pages/StockDetailPage.tsx`、`frontend/src/utils/chart.ts` 必须跑 K线实时契约测试；
- 必须覆盖：
  - 交易时段 1d partial daily bar（`is_partial=true`、`last_live_bar_time` 非空、最后一根日期为今日）；
  - 收盘后/非交易时段 1d 非 partial（`is_partial=false`、最后一根为完整日线）；
  - `/quote` 返回 `update_time` 带 `+08:00`；
  - 前端状态展示区分 quote 实时状态与 K线 partial 状态；
- 这些测试不得 `xfail`，不得删除或以适配错误实现；
- 回归命令：

```bash
cd /root/web_dev/backend
APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5432/bz_stock_test \
pytest tests/test_delivery_worker_monitor_eligible.py tests/test_monitor_batch_live_minute.py tests/test_market_data_aggregation_partial_daily.py tests/test_pytdx_adapter_minute_aware.py tests/test_quote_timezone.py -q
```

## 3.3 V1.9 + V1.10 swing + capture + auto-trigger 回归（blocking）

任何修改 `backend/app/services/structural_factor_service.py`、`backend/app/services/temporal_feature_service.py`、`backend/app/worker.py`、`frontend/src/components/StockStructuralStatePanel.tsx`、`frontend/src/pages/StockDetailPage.tsx`、`frontend/src/pages/CaptureStockPage.tsx`、`frontend/src/styles/global.scss` 必须跑 V1.9 + V1.10 回归测试。

后端 active swing 计算回归（V1.9）：
- 上涨突破场景：close > confirmed_swing_high 时 `price_position_in_confirmed_swing_raw > 1` 且 `price_position_in_active_swing_0_1 in [0, 1]`；
- 下跌破位场景：close < confirmed_swing_low 时 `price_position_in_confirmed_swing_raw < 0` 且 `price_position_in_active_swing_0_1 in [0, 1]`；
- 单边上涨场景：`active_swing_high` 跟随最新高点更新，`active_swing_dir == 1`；
- 单边下跌场景：`active_swing_low` 跟随最新低点更新，`active_swing_dir == -1`；
- `bars_since_active_swing_high`/`bars_since_active_swing_low` 计算正确（与 bar 索引对齐）；
- `confirmed_swing_breakout_state` 三态分类正确（inside/above_confirmed_high/below_confirmed_low/null）；
- fallback 模式（无 confirmed pivot）使用最近 120 根 bar high/low，`active_swing_dir is None`。

后端 developing swing 计算回归（V1.10）：
- major up leg + active_high_bar_idx < current_idx（回落场景）：`developing_swing_dir == -1`，`developing_swing_high == active_swing_high`，`developing_swing_low == min(lows[active_high_bar_idx:now])`，**不得等于 active_swing_low**（active_swing_low 仍是大段起点，developing_swing_low 是从 active_high 起回落段的最低 low）；
- major down leg + active_low_bar_idx < current_idx（反弹场景）：`developing_swing_dir == 1`，`developing_swing_low == active_swing_low`，`developing_swing_high == max(highs[active_low_bar_idx:now])`，**不得等于 active_swing_high**；
- major up leg + active_high_bar_idx == current_idx（继续创新高）：`developing_swing_dir == 1`，`developing_swing_high == active_swing_high`，`developing_swing_low == active_swing_low`；
- major down leg + active_low_bar_idx == current_idx（继续创新低）：`developing_swing_dir == -1`，`developing_swing_high == active_swing_high`，`developing_swing_low == active_swing_low`；
- 000100 类似 case（4.45 → 6.26 → 5.19 回落）：`developing_swing_low` 接近当前回落 low（如 5.0），**不得等于 4.45**（4.45 是大段起点，不是从 6.26 回落后的当前 developing low）；
- fallback 模式（无 confirmed pivot）：`developing_swing_dir is None`，developing = active；
- `bars_since_developing_swing_high`/`bars_since_developing_swing_low` 与 bar 索引对齐；
- `developing_swing_range <= 0` 或 high/low 缺失时所有 developing 比例字段为 null。

DSA age 一致性回归：
- `age_bars` == `current_dsa_segment_age_bars`（+1 口径，含起始 bar）；
- `segment_duration_ratio` 等段间对比字段使用统一 +1 口径；
- 不再出现 V1.7 `age_bars` 与 V1.8 `current_dsa_segment_age_bars` 相差 1 或 2 的情况。

Temporal relation developing swing 回归（V1.10）：
- `m15_position_relative_to_daily` == `m15_price_position_in_developing_swing_0_1 - daily_price_position_in_developing_swing_0_1`；
- 任一 developing 字段缺失返回 null，**不回退 active major leg 或 confirmed raw**；
- V1.8 `daily_price_position_in_swing_0_1`/`m15_price_position_in_swing_0_1` 仍保留在响应中（向后兼容）但 derived_relation 不再使用；
- V1.9 active swing 字段仍保留在响应中（向后兼容）但 derived_relation 不再使用。

盘后 publish auto-trigger 回归：
- DSA `scheduled + completed` run 完成后自动调用 `create_after_close_run(trade_date, run_id)`；
- 非 DSA 策略（如 `watchlist_monitor`）不触发 auto-trigger；
- `trade_date` 缺失时不触发，记录 warning；
- `create_after_close_run` 失败不传播异常，仅记录日志，不影响 `strategy_batch_worker` 主流程；
- 同 `trade_date` 已有 after_close 任务时返回已有任务（幂等）。

前端契约回归（V1.10）：
- Swing 摘要卡只显示 developing 标签字段（`developing_swing_dir`/`developing_swing_high`/`developing_swing_low`/`bars_since_developing_swing_high`/`bars_since_developing_swing_low`/`price_position_in_developing_swing_0_1`/`distance_to_developing_swing_high_atr`/`distance_to_developing_swing_low_atr`）；
- 摘要卡不得出现 `Active high`/`Active low`/`Active 位置` 作为主字段（active major leg 在明细 JSON 中查看）；
- 禁止模糊标签「最近 swing high/low」「Swing 位置[0,1]」；时序位置标签必须含 `developing` 或 `confirmed` 前缀；
- active major leg 字段、confirmed pivot 字段只在明细卡显示，不在摘要卡；
- capture 模式（`capture=feishu` 或 `capture=1` 或 `hideStructuralState=1`）不渲染结构按钮、右侧结构列、Temporal Features；
- capture 模式 `.tv-side-column { display: none; }` 且 `.tv-chart-column { width: 100%; }`；
- capture 模式 `data-testid="tv-chart-column"` 挂在 `.tv-chart-column` 元素（不在 `.tv-content`）；
- capture 模式 chart 列占宽比例 >= 0.95（单列布局）。

回归命令：

```bash
cd /root/web_dev/backend
APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5432/bz_stock_test \
pytest tests/test_structural_factor_service.py tests/test_temporal_feature_service.py tests/test_worker_auto_trigger.py -q

cd /root/web_dev/frontend
npm run lint && npx tsc --noEmit
node --experimental-strip-types --test scripts/contract-tests/structural-state-panel.test.ts \
  scripts/contract-tests/capture-stock-page.test.ts \
  scripts/contract-tests/structural-state-toggle.test.ts
```

## 3.4 DSA overlay source alignment 回归（blocking）

任何修改 `backend/app/api/bars.py::_df_to_responses`、`backend/app/services/chart_bars_service.py::compute_source_bar_times/hash`、`backend/app/services/indicator_service.py`（source_bar_times/hash 计算）、`frontend/src/utils/chartTime.ts`、`frontend/src/components/StrategyChart.tsx`（normalizeChartTime 调用方）必须跑 DSA overlay source alignment 回归测试。

后端 source 对齐回归：
- `compute_source_bar_times(df, "15m")` 返回 `YYYY-MM-DDTHH:MM:SS`（含时间）；
- `compute_source_bar_times(df, "1h")` 返回 `YYYY-MM-DDTHH:MM:SS`（含时间）；
- `compute_source_bar_times(df, "1d")` 仍返回 `YYYY-MM-DD`（向后兼容）；
- `compute_source_bar_hash(df, "15m")` 拼接串含时间，与 15m 一致；
- `compute_source_bar_hash(df, "1d")` 仍用 `YYYY-MM-DD`（向后兼容）；
- `indicator_service.compute_all_indicators` 在 15m/1h 使用 `macd_bars`（当前 timeframe bars），不得永远用 `daily_bars`；
- 15m/1h `bars.trade_time` 必须返回 aware datetime（带 `Asia/Shanghai` tzinfo），序列化为 `+08:00` 后缀；
- 1d `bars.trade_date` 仍为 date 对象（无时区）。

前端 contract 回归（`src/components/__tests__/dsaSourceAlignment.test.ts`）：
- `normalizeChartTime("2026-07-06T15:00:00+08:00", "15m")` 返回 `"2026-07-06 15:00"`；
- `normalizeChartTime("2026-07-06T15:00:00", "15m")` 返回 `"2026-07-06 15:00"`（naive 与 aware 产生相同 canonical key）；
- 15m K线 aware 与 source_bar_times naive 全部匹配（matched / klineKeys.size = 1.0，不触发 mismatch）；
- 故意构造的 source mismatch（15m source 仍是日线日期格式）仍触发暂停（matched = 0，ratio < 0.5）；
- 1d K线 trade_date 与 source_bar_times 全部匹配；
- `timeTicks` 15m aware 时间显示北京交易时间（`14:45`/`15:00`），不显示 `03:00` 这类非交易时段错误时间；
- 1d `timeTicks` 仅显示 `MM-DD`。

回归命令：

```bash
cd /root/web_dev/backend
APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@127.0.0.1:5432/bz_stock_test \
pytest tests/test_chart_bars_service.py tests/test_indicator_service.py tests/test_bars_vectorization.py -v

cd /root/web_dev/frontend
node --experimental-strip-types --test src/components/__tests__/dsaSourceAlignment.test.ts
```

## 3.5 Indicator overlay alignment 回归（blocking）

任何修改 `backend/app/services/indicator_cache.py`（`ALGORITHM_VERSION`）、`backend/app/services/indicator_service.py::_adapt_watchlist_bb`、`frontend/src/utils/dsaOverlayPolicy.ts`、`frontend/src/components/StrategyChart.tsx`（DSA toggle / BB overlay 对齐 / debug 工具）必须跑 indicator overlay alignment 回归测试。

后端 cache schema 版本回归：
- `indicator_cache.ALGORITHM_VERSION == "v7"`（CHANGE-20260715-002 bump：SMC 从 SMA 基线升级为 Pine parity 核心 `smc_pine_core.py`，旧 v6 缓存强制失效；CHANGE-20260715-001：新增 SMC 指标并按需启用，缓存 key 追加 `:smc` 后缀隔离）；
- 旧 v5/v6 cache key 与新 `build_cache_key` 生成的 key 不相等（旧缓存自然失效，避免旧缓存返回无 SMC 数据或 SMA 基线 SMC 结果被误用）；
- `include_smc=true` 时 cache key 追加 `:smc` 后缀，与默认路径（`include_smc=false`）完全隔离，互不污染；
- 修改 indicator 计算逻辑、`source_bar_times` 格式、BB/SQZMOM/MACD 计算路径、DSA 全周期支持、1w/1mo BB 计算、SMC 计算路径必须 bump `ALGORITHM_VERSION`。

后端 DSA 全周期计算回归：
- `MarketDataContext.bars_daily` 在所有周期（1d/15m/1h/1w/1mo）都使用 `macd_bars`（当前 timeframe bars），DSA 不再仅由日线驱动；
- `daily_time_list` 使用 `macd_bars.index`（与策略输出长度一致），使 15m/1h/1w/1mo DSA 的 `time` 数组正确反映当前周期；
- 15m 下 DSA `time[0]` 含 `T` 分隔符（含时间部分，非日线 YYYY-MM-DD）。

后端 BB overlay 计算回归：
- `_adapt_watchlist_bb` 在 1d/15m/1h/1w/1mo 全部用 `macd_bars` 调用 `compute_bollinger(macd_bars, length=20, mult=2.0)` 计算 BB（不再移除 1w/1mo BB 字段）；
- 1w/1mo BB 字段 `bb_upper`/`bb_mid`/`bb_lower`/`bb_pos`/`bb_width` 与 `compute_bollinger(macd_bars)` 计算结果一致；
- BB `time` 数组长度与 `macd_bars` 对齐（非日线长度）；
- `len(macd_bars) < 20` 时 BB 字段填 `None`，`time` 数组仍与 `macd_bars` 对齐；
- `chart_layers` 循环不得 `continue` 跳过 1w/1mo BB 图层。

前端 DSA overlay policy 回归（`src/components/__tests__/dsaSourceAlignment.test.ts`）：
- `shouldAllowDsaOverlay('1d'/'15m'/'1h'/'1w'/'1mo')` 全部返回 `true`（DSA 全周期支持，不再 1d-only）；
- `shouldCheckDsaMismatch('1d'/'15m'/'1h'/'1w'/'1mo')` 全部返回 `true`（全周期渲染，全部需校验 source 对齐）；
- `DSA_TITLE_HINT('1d')` 含 "日线结构锚"；
- `DSA_TITLE_HINT('15m'/'1h'/'1w'/'1mo')` 含 "当前周期验证图层" 且不含 "日线结构锚"。

前端 overlay 渲染/toggle/y-axis 决策回归（PR #33 前端硬编码清理，`src/components/__tests__/dsaSourceAlignment.test.ts` 第 5 节）：
- `shouldRenderDsaLayer('dsa_vwap', {dsa:true}, false, tf)` 在 1d/15m/1h/1w/1mo 全部返回 `true`（不再 `timeframe !== '1d'` 跳过）；
- `shouldRenderDsaLayer('dsa_vwap', {dsa:false}, false, tf)` 全周期 `false`（开关关闭）；
- `shouldRenderDsaLayer('dsa_vwap', {dsa:true}, true, tf)` 全周期 `false`（dsaSourceMismatch=true 跳过，保留 source mismatch 保护）；
- `shouldRenderDsaLayer('bb', {dsa:true}, false, tf)` 返回 `false`（layer_id 非 dsa_vwap 不归此函数管）；
- `shouldAllowBbOverlay('1d'/'15m'/'1h'/'1w'/'1mo')` 全部返回 `true`（BB 全周期支持，1w/1mo 不再被 skip）；
- `shouldRenderBbLayer('bb', {bb:true}, '1w'/'1mo')` 返回 `true`（不再 `timeframe === '1w' || '1mo'` 跳过）；
- `shouldRenderBbLayer('bb', {bb:false}, tf)` 全周期 `false`（开关关闭）；
- `shouldRenderBbLayer('dsa_vwap', {bb:true}, '1d')` 返回 `false`（layer_id 非 bb 不归此函数管）；
- `shouldToggleDsa('dsa', true, FEISHU_CAPTURE_LAYERS)` 返回 `false`（capture 模式锁定 DSA 不可关闭，保留截图模式锁定）；
- `shouldToggleDsa('dsa', false, FEISHU_CAPTURE_LAYERS)` 返回 `true`（非 capture 模式 DSA 全周期可切换，不再 `timeframe !== '1d'` disable）；
- `shouldToggleDsa('bb', false, FEISHU_CAPTURE_LAYERS)` 返回 `true`（非 dsa group 不归此函数管，不阻塞）；
- `shouldIncludeDsaInPriceRange('dsa_vwap', {dsa:true}, tf)` 在 1d/15m/1h/1w/1mo 全部返回 `true`（不再 `timeframe === '1d'` 限制，DSA 全周期参与 y-axis range）；
- `shouldIncludeDsaInPriceRange('dsa_vwap', {dsa:false}, tf)` 全周期 `false`（开关关闭）；
- `shouldIncludeDsaInPriceRange('bb', {dsa:true}, '1d')` 返回 `false`（layer_id 非 dsa_vwap 不归此函数管）。

DSA visual_segments time alignment 回归（PR #34，`backend/tests/test_dsa_visual_segments_time_format.py` + `frontend/src/components/__tests__/dsaSourceAlignment.test.ts` 第 6 节）：
- 后端 `format_dsa_time(x)`：1d（hour/minute/second/microsecond 全 0）返回 `YYYY-MM-DD`；15m/1h（含非零时间部分）返回 `isoformat()`（含 `T`）；
- `compute_dsa_bundle` 15m `visual_segments.points.time` 全部匹配 `^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}`，不再退化为纯日期；
- `compute_dsa_bundle` 15m `anchor.time` 全部匹配 `^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}`；
- `DSASelector.compute_indicators` 15m `time` / `visual_segments.points.time` 全部含 `HH:MM`；
- `compute_dsa_bundle` 1d `visual_segments.points.time` / `anchor.time` / `compute_indicators.time` 全部为 `YYYY-MM-DD`（向后兼容）；
- 15m `visual_segments.points.time` 与 `source_bar_times` canonical 匹配率 > 0.5（模拟前端 `normalizeChartTime('15m')` 行为）；
- 前端 `computeDsaSegmentMatchStats(segments, displayTimes, '15m')`：segment points 含 `THH:MM` 时 `ratio > 0.5`；
- 前端 `computeDsaSegmentMatchStats(segments, displayTimes, '1h')`：segment points 含 `THH:MM` 时 `ratio > 0.5`；
- 前端 `computeDsaSegmentMatchStats` 旧 YYYY-MM-DD segment times 在 15m 下 `matched=0` / `ratio=0` / `degradedReason='segment_time_no_match'`（防御性：若后端回退到旧 strftime 实现，必须触发诊断）；
- 前端 `computeDsaSegmentMatchStats` 空 segments 返回 `degradedReason='no_segments'`；
- 前端 `computeDsaSegmentMatchStats` 多 segment 累计 matched（段间不连线时仍正确累计 total/matched）。

回归命令：

```bash
cd /root/web_dev/backend
APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@127.0.0.1:5432/bz_stock_test \
pytest tests/test_indicator_cache.py tests/test_indicator_service.py -v

cd /root/web_dev/frontend
node --experimental-strip-types --test src/components/__tests__/dsaSourceAlignment.test.ts
```

## 3.6 Feature Snapshot 持久化回归（blocking）

任何修改 `backend/app/services/feature_snapshot_service.py`、`backend/app/api/watchlist.py::get_watchlist_monitor_status`、`backend/app/services/after_close_orchestrator.py`（状态机或 `feature_snapshot` 步骤）、`backend/scripts/feature_snapshot_backfill.py`、`backend/app/models/stock_feature_snapshot.py` 必须跑 Feature Snapshot 持久化回归测试。

后端 service 回归（`tests/test_feature_snapshot_service.py`，18 个用例）：
- `build_summary_payload` 必须返回所有前端列表必需字段（`poc_price` / `nearest_node_above` / `nearest_node_below` / `distance_to_node_*_atr` / `node_interval_position_0_1` / `cost_position_zone` / `value_area_zone` / `daily/m15_developing_swing_*` / `m15_position_relative_to_daily` / `_source='feature_snapshot'` / `as_of` / `source_bar_time`）；
- `build_summary_payload` 缺字段时填 `None`，不抛异常；
- `_truncate_bars_to_trade_date` 必须按 `index.date <= trade_date` 截断，禁止未来数据；`None` 输入返回 `None`；15m bars 截断到当日；
- `compute_feature_snapshot_for_date` 必须使用 `<= trade_date` 数据；数据不足时写 `degraded_reasons` 不抛异常；`source_primary_bar_time` 与 `source_secondary_bar_time` 必须为 `Asia/Shanghai` aware datetime；1d bar 时间规范化为 `trade_date 15:00+08:00`；
- **`structural_payload` 必须包含 4 个 top-level key：`primary` / `secondary` / `relation` / `meta`**；`relation` 来自 `_compute_relation(primary_factors, secondary_factors)`；
- `upsert_snapshot` 必须幂等：同 `(instrument_id, trade_date, primary_timeframe, secondary_timeframe, adj, schema_version)` 重复 upsert 只生成一行，`structural_payload`/`temporal_payload`/`summary_payload` 被第二次覆盖；
- `compute_for_trade_date` 单股失败不阻断其他股票，失败比例超过 `failure_threshold`（默认 0.3）抛 `RuntimeError`；
- **[half-baked rollback] `compute_for_trade_date` 不内部 commit**：超阈值抛 `RuntimeError` 后 caller rollback，DB 中不应残留该 trade_date 的部分 snapshot 行。
- **[P0-4 published snapshot 保护]** `create_snapshot_run(scope='full', allow_republish=False)` 在已存在 canonical succeeded+published+full run 时抛 `PublishedSnapshotRunExistsError`；`allow_republish=True` 绕过检查；`scope='sample'` 不受限；`upsert_snapshot(allow_republish=False)` WHERE 子句保护已归属 published run 的 snapshot 不被覆盖；`allow_republish=True` 可覆盖；`after_close_orchestrator` 捕获 `PublishedSnapshotRunExistsError` 后跳过计算复用已有 run。

后端 backfill 脚本回归（`tests/test_feature_snapshot_backfill.py`，42 个用例，含 multiprocessing + [Blocker Fix] 事务/统计修正）：
- `parse_args` 默认值：`end='latest'`、`batch_size=20`、`failure_threshold=0.3`、`resume=False`、`dry_run=False`、`symbols=None`、`limit_instruments=None`、`workers=1`、`allow_republish=False`；自定义值正确解析；缺失 `--start` 报 `SystemExit`；
- `get_trade_dates_from_bars` 返回升序 trade_dates；空表返回空列表；
- `get_latest_bar_date` 返回 `bars_daily.trade_date` 最大值；空表返回 `None`；
- **`get_existing_instrument_ids`** 返回某日已存在 snapshot 的 instrument_id 集合，按完整唯一键 `(instrument_id, trade_date, primary_timeframe, secondary_timeframe, adj, schema_version)` 过滤；按 `schema_version` 严格过滤；
- **`get_instruments_for_backfill`** 支持 `--symbols`（逗号分隔代码过滤）和 `--limit-instruments`（数量限制）小样本过滤；
- **`load_instrument_bars`** 一次性加载 1d + 15m bars（每只股票每周期只调用一次）；
- **`backfill_instrument_first`（Phase 8 新增）**：
  - `--dry-run` 不写库，输出 trade_dates / active instruments / missing rows / 预计 batch 数；
  - instrument-first 不重复调用 `load_instrument_bars`（mock 断言：2 instrument × 2 date = 2 load 调用，不是 4）；
  - `--resume` 跳过已存在 snapshot 且所属日期有 `succeeded` run 的行（双重过滤）；
  - 成功创建 `succeeded` run（写 `published_at`）；
  - 失败比例超阈值创建 `failed` run（不抛 RuntimeError，不阻断其他日期）；
- `main` `--end=latest` 解析为 `bars_daily` 表最新 trade_date；`start > end` 直接 `sys.exit(1)`；`--symbols` 小样本过滤正确。
- **[Blocker Fix] scope 区分测试（4 个新增）**：
  - `_resolve_run_scope(symbols=['000100','603303'], limit_instruments=None)` 返回 `'sample'`；
  - `_resolve_run_scope(symbols=None, limit_instruments=20)` 返回 `'sample'`；
  - `_resolve_run_scope(symbols=None, limit_instruments=None)` 返回 `'full'`；
  - `backfill_instrument_first(scope='sample')` → `create_snapshot_run` 收到 `scope='sample'` kwarg + `finish_snapshot_run` 的 metadata 含 `'scope': 'sample'`，防止小样本 run 污染 watchlist SUCCEEDED。
- **multiprocessing 测试（9 个新增，CHANGE-049）**：
  - `test_parse_args_workers_default_is_1` / `test_parse_args_workers_custom`：参数解析（默认 1，自定义 N）；
  - `test_worker_process_instruments_per_date_commit`：per-date commit 全成功（2 instruments × 2 dates = 4 commits）；
  - `test_worker_process_instruments_resume_skips_existing`：worker resume 跳过已存在行；
  - `test_worker_process_instruments_single_failure_doesnt_block`：load 失败 → failed + rollback，不阻塞其他 instrument；
  - `test_backfill_instrument_first_parallel_empty_inputs`：空输入返回；
  - `test_backfill_instrument_first_parallel_creates_and_finalizes_run`：主进程创建 + finish succeeded run；
  - `test_backfill_instrument_first_parallel_high_failure_marks_failed`：高失败率 → run.status='failed'；
  - `test_backfill_instrument_first_parallel_propagates_scope`：scope='sample' 传播到 create/finish metadata。
- **multiprocessing Blocker Fix 测试（8 个新增，CHANGE-049 v2）**：
  - `test_backfill_parallel_worker_exception_counts_as_failed`：worker future 抛 `RuntimeError` → run.status='failed' + chunk 内每个 instrument × 每个 trade_date 计入 `failed_count`，不能 finalized 为 `succeeded`；
  - `test_worker_commit_failure_doesnt_count_as_success`：`db.commit()` 抛异常 → `success=0`、`failed=1`、`rollbacks=1`（DB 写入与 stats 严格一致，不允许 commit 失败仍计 success）；
  - `test_worker_upsert_exception_rollback_continues`：第一个 date upsert 抛异常 → `rollback + failed++`，第二个 date 仍可继续成功 `commit + success++`，stats 正确（per-date 独立事务）；
  - `test_worker_pool_config_size_1_overflow_0`：mock `create_async_engine` 断言 `pool_size=1, max_overflow=0, pool_pre_ping=True`（避免 4 workers × 15 = 60 连接打满 PG）；
  - `test_parse_args_workers_zero_rejected`：`--workers 0` → `SystemExit`（argparse error）；
  - `test_parse_args_workers_negative_rejected`：`--workers -1` → `SystemExit`（argparse error）；
  - `test_parse_args_workers_cap_to_cpu_count`：`--workers > cpu_count` → `warnings.warn()` + 自动 cap；
  - `test_worker_per_date_commit_all_succeed`：回归 per-date commit 路径，2 instruments × 2 dates = 4 commits 全部 success。
- **multiprocessing Blocker Fix 非回归测试（已有，未修改）**：
  - sample scope 仍不污染 watchlist（`test_backfill_instrument_first_parallel_propagates_scope`）；
  - full scope gate 不回归（`test_backfill_instrument_first_parallel_creates_and_finalizes_run`）。

API 契约回归（`tests/test_watchlist_monitor_status_snapshot.py`，14 个用例）：
- `SUCCEEDED`：交易日已收盘且 snapshot 存在，`calculation_status='SUCCEEDED'`，`metrics` 来自 `summary_payload` 且 `_source='feature_snapshot'`，`freshness_seconds` 为整数；
- `NO_SNAPSHOT`：非交易日，`calculation_status='NO_SNAPSHOT'`，`metrics` 为空 dict，`freshness_seconds=None`；
- `WAITING_SNAPSHOT`：交易日已收盘但 snapshot 缺失，`calculation_status='WAITING_SNAPSHOT'`，`metrics` 为空 dict；
- **[盘中读上一交易日] 交易日 10:00 + 有昨日 snapshot → `SUCCEEDED` + 昨日 metrics**；
- **[非交易日读最近交易日] 非交易日 + 有最近交易日 snapshot → `SUCCEEDED`**；
- **[非交易日无历史] 非交易日 + 无历史 snapshot → `NO_SNAPSHOT`**；
- **[盘中缺上一交易日 snapshot] 交易日 10:00 + trading_calendar 存在上一交易日 + 无昨日 snapshot → `NO_SNAPSHOT`（不是 `WAITING_SNAPSHOT`）+ `metrics={}`**；防止盘中历史快照缺失被误报 WAITING_SNAPSHOT；
- `_resolve_expected_snapshot_trade_date` 复用 `calendar_service.get_previous_trading_day_async` / `get_most_recent_trading_day_async`，禁止硬编码周末；
- **[Run gate - Phase 8 新增]**：
  - `running` run 存在时 watchlist 不读 snapshot（返回 `WAITING_SNAPSHOT` 或 `NO_SNAPSHOT`）；
  - `failed` run 存在时 watchlist 不读 snapshot；
  - `succeeded` run 存在时 watchlist 读 snapshot 返回 `SUCCEEDED`；
  - 无 run 记录时 watchlist 不读 snapshot。
- **[Blocker Fix - publish gate 严格化测试（3 个新增）]**：
  - `succeeded` run 但 `published_at=NULL`（异常状态）→ watchlist 不得返回 `SUCCEEDED`（`calculation_status != 'SUCCEEDED'` + `metrics={}`）；
  - `backfill` full scope run（`scope='full'` + `published_at` 非空）→ watchlist 返回 `SUCCEEDED` + 可读 snapshot；
  - `backfill` sample scope run（`scope='sample'` + `published_at` 非空）→ watchlist 不得读 snapshot（`calculation_status != 'SUCCEEDED'` + `metrics={}`），防止小样本验证数据污染生产 watchlist SUCCEEDED 状态。

orchestrator 状态机回归（`tests/test_after_close_orchestrator.py`，17 个用例）：
- `AfterCloseRunStatus` 枚举包含 `FEATURE_SNAPSHOT`；
- 状态机流转顺序：`quality_gate → feature_snapshot → publishing`；
- 断点恢复：`last_completed_step='quality_gate'` 时 `skip_snapshot=False`；`last_completed_step='feature_snapshot'` 时 `skip_snapshot=True`；
- `feature_snapshot` 步骤使用独立 `AsyncSessionLocal`，不依赖请求 session；
- **[feature_snapshot 失败不进入 publishing] `compute_for_trade_date` 抛 `RuntimeError` → `publish_run` 不被调用 + `job_run.status='failed'` + 不应有 publishing/succeeded 事件**；
- **[Run lifecycle - Phase 8 新增]**：
  - feature_snapshot 成功写 `stock_feature_snapshot_runs.status='succeeded'` + `published_at` 非空 + `snapshot_count` / `failed_count` 正确；
  - feature_snapshot 失败写 `stock_feature_snapshot_runs.status='failed'` + `published_at` 为 None + 不进入 publishing。
- **[Heartbeat 保活 - CHANGE-20260709-006]**：
  - `feature_snapshot` 阶段启动 `_job_run_heartbeat_loop`（间隔 30s），长计算期间持续刷新 `heartbeat_at` 与 `lease_expires_at`；
  - `compute_for_trade_date` 每处理完一个 batch 调用 `progress_callback`，回调写入 `metadata.feature_snapshot_progress` 并按每 500 只股票采样生成 `job_run_events`；
- **[Repair 修复 - CHANGE-20260709-006]**：
  - `repair_stale_after_close_snapshot_runs`：存在 `status='interrupted'/'failed'` 的 after_close job_run 且同 trade_date 的 `feature_snapshot_run.status='running'` 超阈值时，实际 snapshot 行数 ≥ 95% 标 `succeeded` 并写 `published_at`，否则标 `failed`；
  - `execute_after_close_run` 启动前自动调用 repair，避免 stuck running snapshot_run 阻塞新任务；
  - running orchestrator 或未达到 stale 阈值的 running snapshot_run 不被误修复。

Run service 回归（`tests/test_feature_snapshot_run_service.py`，6 个用例 - Phase 8 新增）：
- `create_snapshot_run` 创建 `running` 记录；
- `create_snapshot_run` 幂等：已有 `running` run 时返回已有；
- `create_snapshot_run` 允许 failed run 后新 retry（partial unique index 仅约束 running）；
- `finish_snapshot_run(status='succeeded')` 写 `published_at`；
- `finish_snapshot_run(status='failed')` 不写 `published_at`；
- `finish_snapshot_run` 接受 `metadata` 用于审计。

迁移幂等回归：
- `alembic upgrade head` 在 test DB 上能成功创建 `stock_feature_snapshots` 和 `stock_feature_snapshot_runs` 表；
- `alembic downgrade -1` 能删除 `stock_feature_snapshot_runs` 表；
- `alembic upgrade head` 再升级不报错（幂等）；
- `stock_feature_snapshots` 含唯一约束 `uq_feature_snapshot_instrument_date_tf_adj_schema` 与 3 个 btree 索引；
- `stock_feature_snapshot_runs` 含 partial unique index `uq_snapshot_runs_active_key`（仅约束 `status='running'`）与 3 个 btree 索引。

回归命令：

```bash
cd /root/web_dev/backend
APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
pytest tests/test_feature_snapshot_service.py tests/test_feature_snapshot_backfill.py \
       tests/test_feature_snapshot_run_service.py \
       tests/test_watchlist_monitor_status_snapshot.py tests/test_after_close_orchestrator.py \
       tests/test_job_runs_and_monitor_status.py -q

ruff check app/services/feature_snapshot_service.py \
  app/api/watchlist.py \
  app/services/after_close_orchestrator.py \
  app/services/after_close_pipeline_service.py \
  app/schemas/after_close_pipeline.py \
  scripts/feature_snapshot_backfill.py \
  tests/test_feature_snapshot_service.py \
  tests/test_feature_snapshot_backfill.py \
  tests/test_watchlist_monitor_status_snapshot.py \
  tests/test_after_close_orchestrator.py \
  tests/test_admin_after_close_pipeline.py

mypy app/services/feature_snapshot_service.py \
  app/services/after_close_orchestrator.py \
  app/services/after_close_pipeline_service.py \
  app/schemas/after_close_pipeline.py
```

预期：47 passed、ruff 零错误、mypy 零错误。

小范围 dry-run 验证命令（不写库，仅打印计划）：

```bash
cd /root/web_dev/backend && .venv/bin/python -m scripts.feature_snapshot_backfill \
    --start 2026-07-04 --end 2026-07-07 --dry-run
```

## 3.6.1 盘后流水线聚合 API 回归（blocking）

任何修改 `backend/app/services/after_close_pipeline_service.py`、`backend/app/api/admin_after_close.py`（pipeline 端点部分）、`backend/app/schemas/after_close_pipeline.py` 必须跑盘后流水线聚合 API 回归测试。

后端回归（`tests/test_admin_after_close_pipeline.py`，12 个用例）：
- 盘前无 run 时返回 `overall_status='not_started'` + `watchlist_ready=false`；
- 收盘后超过 30 分钟阈值无 run → `overall_status='blocked'`；
- latest 在交易日不回退历史 run（today 无 run 必须返回 today 的 blocked，不返回昨天 succeeded）；
- 运行中 run（status=running）时返回 `overall_status='running'` + 当前步骤 status='running'；
- 成功 run + snapshot succeeded + scope=full → `overall_status='succeeded'` + `watchlist_ready=true`；
- `watchlist_ready` 严格判定：snapshot `scope='sample'` 时 `watchlist_ready=false`（sample backfill 不计入）；
- full+sample 同日共存：watchlist_ready=true 时 feature_snapshot_run 主摘要必须为 full run（显式 created_at，不依赖 DB 默认顺序）；
- 失败 run（status=failed）时返回 `overall_status='failed'` + error_message 非空；
- **中断后 UI 展示（CHANGE-20260709-006）**：`orchestrator_status='interrupted'` 且 `feature_snapshot_run.status='running'` 时，第 6 步 `feature_snapshot` 显示 `running`，`feature_snapshot_lost_contact=true`，`after_close_run` 摘要暴露 `feature_snapshot_run_id` 与 `feature_snapshot_progress`；
- POST `/after-close/pipeline/run` 幂等：同 trade_date 已有 queued/running/succeeded run 时返回 existing（`is_new=false`）；
- events 列表限制 100 条；
- 非 admin 用户访问返回 403。

生产 smoke 验收（部署后执行，不阻塞 CI）：
- `/health` → 200；
- `/admin/after-close/pipeline/latest` → 200；
- `/admin/after-close/pipeline?trade_date=<today>` → 200；
- `/admin/after-close/pipeline/runs?limit=20` → 200；
- `/admin/overview` → 200（摘要卡可见）；
- `/admin/after-close` → 200（详情页可见）；
- backend/frontend 20m 日志无 5xx/502/timeout。

前端构建验证：
- `tsc --noEmit` 零错误；
- `npm run build` 成功（含新页面 `AdminAfterClosePipelinePage` 和摘要卡改造）。

回归命令：

```bash
cd /root/web_dev/backend
APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
pytest tests/test_admin_after_close_pipeline.py -q

ruff check app/schemas/after_close_pipeline.py \
  app/services/after_close_pipeline_service.py \
  app/api/admin_after_close.py \
  tests/test_admin_after_close_pipeline.py

mypy app/schemas/after_close_pipeline.py app/services/after_close_pipeline_service.py app/api/admin_after_close.py

cd /root/web_dev/frontend && npm run build
```

预期：11 passed、ruff 零错误、mypy 零错误、前端 build 成功。

## 3.7 Monitor Image Capture Token 回归（blocking）

任何修改 `backend/app/services/monitor_batch_service.py`、
`backend/app/services/notification_service.py::test_channel_latest_event`、
`backend/app/core/security.py::create_capture_token`、
`backend/app/core/deps.py::get_capture_token_payload`、
`backend/app/constants/capture.py` 必须跑 Monitor Image Capture Token 回归测试。

后端 `tests/test_monitor_batch_capture_image.py`：
- `test_capture_token_contains_required_claims`：`_send_chart_images_via_outbox` 生成的 capture token 解码后必须包含 `type="capture"`、`scope="stock_detail_capture"`、`user_id`、`instrument_id`、`event_id`；
- `test_capture_token_instrument_id_matches_trigger_stock`：token 的 `instrument_id` 必须等于触发股票的 `inst_id`；
- `test_capture_success_writes_image_outbox`：capture worker 返回 `image_url` 时，必须写入 `delivery_type="image"` 的 Outbox payload，且 `image_url` 非空、`message_group_id` 与文字通知同组；
- `test_capture_failure_does_not_block_text_notification`：capture worker 返回 401/403 或无 `image_url` 时，必须写 `capture_jobs.status=FAILED`，且**不写** image Outbox，文字通知不受影响；
- `test_capture_success_writes_capture_job_succeeded`：截图成功写 `capture_jobs.status=SUCCEEDED` 并记录 `image_url`。

后端 `tests/test_notification_latest_event_capture.py`：
- `test_channel_latest_event_capture_token_has_full_claims`：`test_channel_latest_event` 生成的 capture token 解码后必须包含 `type="capture"`、`scope="stock_detail_capture"`、`user_id`、`instrument_id`、`event_id`；
- `test_channel_latest_event_token_instrument_id_matches_event_instrument`：token 的 `instrument_id` 必须等于事件对应标的 ID。

回归命令：

```bash
cd /root/web_dev/backend
APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
  pytest tests/test_monitor_batch_capture_image.py tests/test_notification_latest_event_capture.py -q

ruff check app/constants/capture.py app/core/deps.py app/core/security.py \
  app/services/monitor_batch_service.py app/services/notification_service.py \
  app/services/stock_detail_feishu_service.py \
  tests/test_monitor_batch_capture_image.py tests/test_notification_latest_event_capture.py
```

预期：6 passed、ruff 零错误。

## 3.8 飞书盘中高清实时截图回归（blocking）

任何修改 `backend/app/services/stock_capture_service.py`、`backend/app/capture_main.py`、`backend/app/api/capture.py`、`backend/app/services/monitor_snapshot_service.py`、`backend/app/api/indicators.py`、`backend/app/services/stock_detail_feishu_service.py`、`backend/app/services/notification_service.py`、`frontend/src/pages/CaptureStockPage.tsx`、`frontend/src/pages/StockDetailPage.tsx`、`frontend/src/components/StrategyChart.tsx` 必须跑以下回归，覆盖三件事：高清截图、不复用旧图/旧指标、K线标题显示股票名称。

后端：

- `tests/test_stock_capture_service.py`：`capture_stock_chart` 返回 `CaptureResult`（png_bytes + width/height/device_scale_factor/cache_hit）；缓存 key 维度含 `timeframe` / `source_bar_time` / `capture_run_id` / `device_scale_factor`；`disable_cache=True` 跳过读缓存但写最新；
- `tests/test_capture_snapshot.py`：Capture Snapshot 端点 `include_realtime=True`、周期透传、`bars_limit` 按 `INDICATOR_BARS` 对齐；**阻断验收**：请求 `timeframe=15m` 时，`get_bars` 必须收到 `timeframe="15m"` 且 `include_realtime=True`，`compute_all_indicators` 必须收到 `timeframe="15m"` 且 `bars=INDICATOR_BARS["15m"]`，`_df_to_responses` 必须使用 `15m`；响应 `bars.timeframe`、items 时间格式（15m 用 `trade_time`、1d 用 `trade_date`）、indicators timeframe 三者必须一致，禁止回退 `_CAPTURE_TIMEFRAME`；
- `tests/test_indicator_contract.py`：禁止散落硬编码受控字面量（250/4000 等），`INDICATOR_BARS` 为唯一真源；
- `tests/test_indicator_cache.py`：`force_refresh` / `capture` 跳过 Redis 读缓存但写最新；
- 飞书业务 payload 周期断言（CHANGE-20260710-002）：`tests/test_monitor_batch_capture_image.py` / `tests/test_stock_detail_feishu.py` 中 `capture_payload["timeframe"]` 必须是 `1d` / `capture_run_id` / `source_bar_time` / `disable_cache=True`；`test_notification_latest_event_capture.py` 仅校验 selector latest-event 截图的 capture token claims（其 timeframe 由 selector 链路决定，是独立路径，不属于 watchlist_monitor 飞书 1d 业务默认约束）；
- `tests/test_monitor_batch_live_minute.py::test_monitor_calc_inputs_daily_15m_non_realtime`：watchlist_monitor 计算输入 `bars_daily`/`bars_15min` 必须 `include_realtime=False`（不被截图实时性污染），1m 必须 `include_realtime=True` 且剔除最后一根未完成 bar，`source_bar_time` 来自最新已完成 1m；
- Capture Snapshot API 多周期能力测试保留（见上方 `test_capture_snapshot.py` 15m 透传阻断验收）：API 支持 15m 是能力，不等于飞书业务默认 15m；
- `tests/test_bars.py`：K线实时契约（partial daily bar 为真，前端不伪造）。

前端：

- `frontend/src/pages/CaptureStockPage.tsx`：实时状态（`last_live_bar_time` / `is_partial` / `data_source`）必须从 `snapshot.bars` 读取（`barsResponse.last_live_bar_time`），禁止从 `snapshot` 顶层读取（后端 `last_live_bar_time` 只存在于 `bars` 内）；`endpoints.ts` 的 `BarListResponse` 必须包含 `last_live_bar_time` / `last_persisted_bar_time` 字段，且 `CaptureSnapshotResponse` 顶层不得放 `last_live_bar_time`。

回归命令：

```bash
cd /root/web_dev/backend
APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
  pytest tests/test_stock_capture_service.py tests/test_capture_snapshot.py \
        tests/test_indicator_contract.py tests/test_indicator_cache.py \
        tests/test_monitor_batch_capture_image.py tests/test_notification_latest_event_capture.py \
        tests/test_bars.py -q

ruff check app/services/stock_capture_service.py app/capture_main.py app/api/capture.py \
  app/services/monitor_snapshot_service.py app/api/indicators.py \
  app/services/stock_detail_feishu_service.py app/services/notification_service.py \
  app/services/monitor_batch_service.py tests/test_stock_capture_service.py
```

预期：全部 passed、ruff 零错误。验收要点：① 截图 PNG 清晰度提升（viewport 1920×1200 + dsf=2）；② 连续两次盘中截图不复用上一轮旧图/旧指标（cache key 含实时 source_bar_time）；③ K线主标题显示 `名称（代码）`。

**K线实时契约门禁延伸**：本次修改 `StockDetailPage.tsx` / `StrategyChart.tsx` 同时受 §3.2 K线实时契约门禁约束，必须保留后端 partial daily bar 为真、前端 `mergeRealtimeQuoteIntoBars()` 仅作兜底。

## 3.9 研究特征矩阵因果口径回归（blocking）

任何修改 `backend/app/research/feature_causality_registry.py`、`backend/app/research/research_matrix_writer.py`、`backend/app/research/feature_computer.py`、`backend/app/models/research_feature_matrix.py`、`backend/scripts/research_feature_matrix_backfill.py` 必须跑研究特征矩阵因果口径回归测试。

后端 registry 回归（`tests/test_feature_causality_registry.py`，~30 个用例）：
- `FeatureSpec` 必填 `namespace` / `source` / `compute_policy`，缺一抛 `ValueError`；
- `key` 必须以 `{namespace}.` 开头（如 `causal.atr`），不匹配抛 `ValueError`；
- `FeatureSpec.db_column` 把 dotted key 映射为下划线列名（`causal.atr` → `causal_atr`）；
- `hindsight.*` 的 `allowed_for_backtest` 必须 `False`；
- `label.*` 的 `allowed_for_backtest` 必须 `False`；
- `causal.*` 的 `allowed_for_backtest` 必须 `True`；
- `confirmed_delay.*` 的 `allowed_for_backtest` 必须 `True`；
- DSA 必须同时存在 `causal.dsa_confirmed_*` 与 `hindsight.dsa_finalized_*` 两类（缺一视为口径不完整）；
- Node Cluster 只能是 `hindsight.node_cluster_*`，不得出现在 causal；
- `confirmed_swing_*` 必须是 `confirmed_delay`，不得作为 hindsight 默认回填；
- `FeatureCausalityRegistry.register` 重复 key 抛 `ValueError`；
- 默认 registry 必须包含关键 causal/label 字段（`causal.atr` / `causal.bb_percent_b` / `causal.sqzmom_val` / `causal.volume_ratio_20` / `causal.active_swing_dir` / `causal.developing_swing_dir` / `causal.dsa_confirmed_*` / `label.future_return_*` / `label.future_max_drawdown_*` / `label.breakout_success_10d` / `label.failure_breakdown_10d`）；
- `build_default_registry()` 返回 33 个字段（causal 16 + confirmed_delay 4 + hindsight 6 + label 7）。

后端 writer 回归（`tests/test_research_matrix_writer.py`，~32 个用例，async DB savepoint 模式）：
- 三道硬阈值（`TestDiskThreshold` / `TestMonthSizeThreshold` / `TestFailureRateThreshold`）：
  - 磁盘边界 `15 * (1024**3)` 字节（用 1024^3 而非 10^9，与 `check_disk_threshold` 的 GB 计算一致）；
  - 单月大小边界 `MONTH_SIZE_MAX_GB`（3.0GB）；
  - 失败率边界 5%（5/100 通过，6/100 不通过，total=0 通过）；
- 月份解析（`TestResolveMonthRange`）：1月/2月非闰/2月闰年/12月/非法格式抛 `ValueError`；
- 单月大小估算（`TestEstimateMonthSize`）：小样本/全月/零；
- monthly run 生命周期（`TestRunLifecycle`，async DB）：
  - `create_or_resume_run` 首次创建返回 `running`；
  - 相同 `run_key` 第二次调用返回已存在 run（不重复创建）；
  - 不同 scope（`full` / `sample_100`）→ 不同 `run_key`；
  - `finalize_run(succeeded)` 更新 status/统计/duration/finished_at；
  - `finalize_run(failed)` status=failed；
  - **[Blocker Fix] `test_finalize_run_records_failed_instruments_and_rows`**：`finalize_run(failed_instruments=500, failed_count=10000)` 后 `metadata_json.failed_instruments=500` + `metadata_json.failed_rows=10000`；
  - **[Blocker Fix] `test_resume_does_not_reset_completed_run`**：先 `finalize_run(succeeded)`，再 `create_or_resume_run` 同 `run_key`，验证 status/统计/duration 不被重置（resume 不破坏已完成 full run 的统计）；
- 批量 upsert rows（`TestUpsertRowsBatch`，async DB）：
  - 首次 upsert 写入新行；
  - 相同 `(instrument_id, trade_date)` → `ON CONFLICT DO UPDATE` 覆盖旧值；
  - 空 list 返回 0；
  - 1050 行分批（UPSERT_BATCH_SIZE=1000）；
- **[Blocker Fix] 进程锁（`TestProcessLock`，~5 个用例，纯函数 + tmp_path）**：
  - `test_advisory_lock_key_stable`：同 `month+scope` 跨进程生成相同 `(namespace, key)`；
  - `test_advisory_lock_key_diff_scope`：不同 scope 生成不同 key；
  - `test_lock_file_create`：`acquire_lock_file` 创建 `/tmp/research_matrix_backfill_{month}_{scope}.lock`，写入 pid + started_at；
  - `test_lock_file_reject_when_exists`：lock file 已存在时返回 None（拒绝启动）；
  - `test_release_lock_file_idempotent`：删除不存在 lock file 不抛异常；
- dry-run 估算（`TestDryRunEstimation`）：全月估算/极端场景。

后端计算模块回归（`tests/test_feature_computer.py`，~26 个用例）：
- `compute_all_features(bars)` 返回 DataFrame 含 33 个 feature 列；
- per-bar 计算 vs single-snapshot 区分（每根 bar 都有值，warmup 期 NaN）；
- causal rolling 字段（ATR/BB/SQZMOM/volume）复用现有算法 SSOT；
- DSA 双轨（`causal.dsa_confirmed_*` vs `hindsight.dsa_finalized_*`）；
- confirmed_delay swing（只在确认 bar 生效，不回填 anchor）；
- label 字段（未来收益/最大回撤/突破成功/破位失败）；
- 空输入/数据不足不抛异常（返回空 DataFrame）；
- **[Blocker Fix] `test_hindsight_phase1_all_null`**：`hindsight_dsa_finalized_*` 3 列在 Phase 1 必须全 NaN（不得用 causal 近似冒充）；
- **[Blocker Fix] `test_hindsight_not_equals_causal_approx`**：验证 hindsight 不等于 causal segment_ids/directions/age（确保没有偷懒复用）；
- **[Blocker Fix] `test_node_cluster_phase1_all_null`**：`hindsight_node_cluster_*` 3 列在 Phase 1 必须全 NaN。

后端 model 回归（`tests/test_research_feature_matrix_model.py`）：
- `ResearchFeatureMatrixRun` 16 列结构 + `run_key` 唯一约束 + month/status 索引；
- `ResearchFeatureMatrixRow` 39 列结构（5 metadata + 33 feature + 1 created_at）+ `(instrument_id, trade_date)` 唯一约束 + 3 btree 索引；
- 状态枚举常量 `STATUS_RUNNING` / `STATUS_SUCCEEDED` / `STATUS_FAILED`。

后端 CLI backfill 回归（`tests/test_research_feature_matrix_backfill.py`，~13 个用例，mock + tmp_path）：
- **[Blocker Fix] 参数解析（`TestParseArgs`，5 个用例）**：
  - `--month YYYY-MM` 解析正确；
  - `--month` 与 `--start` 互斥（同时给抛错）；
  - `--resume` flag 解析为 True；
  - `--export-parquet PATH` 解析正确；
  - 旧 `--output` 参数已移除（传入抛 `SystemExit`）；
- **[Blocker Fix] 失败行数统计（`TestProcessInstrumentFailureRows`，2 个用例）**：
  - bars 不足时 `_process_instrument` 返回 `(0, expected_rows)` 而非 `(0, 1)`，验证 `failed_rows == trade_dates_count`；
  - features 空时同样返回 `(0, expected_rows)`；
- **[Blocker Fix] rollback（`TestProcessInstrumentRollback`，2 个用例）**：
  - upsert 异常时 `db.rollback()` 被调用（用 AsyncMock 验证）；
  - rollback 失败时只记日志不 crash（继续下一只股票）；
- **[Blocker Fix] 锁拒绝（`TestLockRejection`，4 个用例）**：
  - `_advisory_lock_key(month, scope)` 稳定 hash（同输入跨进程一致）；
  - `acquire_lock_file` 已存在时返回 None（拒绝启动）；
  - 不同 scope 可同时持有 lock（不互斥）；
  - `release_lock_file` 释放不存在的 lock file 不报错。

回归命令：

```bash
cd /root/web_dev/backend
APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
  pytest tests/test_feature_causality_registry.py \
         tests/test_research_matrix_writer.py \
         tests/test_feature_computer.py \
         tests/test_research_feature_matrix_model.py \
         tests/test_research_feature_matrix_backfill.py -q

ruff check app/research/feature_causality_registry.py \
  app/research/research_matrix_writer.py \
  app/research/feature_computer.py \
  app/models/research_feature_matrix.py \
  scripts/research_feature_matrix_backfill.py \
  tests/test_feature_causality_registry.py \
  tests/test_research_matrix_writer.py \
  tests/test_feature_computer.py \
  tests/test_research_feature_matrix_model.py \
  tests/test_research_feature_matrix_backfill.py

mypy app/research/feature_causality_registry.py \
  app/research/research_matrix_writer.py \
  app/research/feature_computer.py \
  app/models/research_feature_matrix.py \
  scripts/research_feature_matrix_backfill.py
```

预期：所有测试 passed、ruff 零错误、mypy 零错误。

dry-run 验证命令（不写库，仅打印计划）：

```bash
cd /root/web_dev/backend && python -m scripts.research_feature_matrix_backfill \
    --month 2026-01 --dry-run
```

## 3.9 研究特征矩阵生产分阶段验收（production staged validation）

PR merge + migration 058 应用后，必须按 A → B → C → D → E 顺序逐阶段验收，前阶段未通过禁止进入下一阶段：

| 阶段 | 命令（前台执行） | 验收点 | 失败处理 |
|---|---|---|---|
| A. dry-run | `docker exec trading-backend python -m scripts.research_feature_matrix_backfill --month 2026-01 --dry-run` | 打印 `expected_rows` / `estimated_db_size` 合理，不写 DB | 不影响后续 |
| B. 2 symbols | `docker exec trading-backend python -m scripts.research_feature_matrix_backfill --month 2026-01 --symbols 000001,600000` | run.status=succeeded，rows_count 正确，failed_rows=0 | 检查日志 traceback，修复后重跑 |
| C. 100 stocks × 1 month | `docker exec trading-backend python -m scripts.research_feature_matrix_backfill --month 2026-01 --limit-instruments 100` | run.status=succeeded，failed_rate < 5% | 检查失败股票，修复后 `--resume` 重跑 |
| D. 全市场 2026-01 | `docker exec trading-backend python -m scripts.research_feature_matrix_backfill --month 2026-01` | run.status=succeeded，磁盘占用合理，表大小合理 | 检查磁盘/失败率，修复后 `--resume` 重跑 |
| E. 后台逐月回补 | nohup 串行跑 `2026-02` 到当前（见 03-jobs §2.4.2.7 runbook） | 每月 run.status=succeeded，磁盘监控 | 检查日志，停止后台任务，修复后重跑 |

**每阶段必须检查项**：
```bash
# 磁盘剩余（必须 >= 15GB）
df -h /

# 最新 run 状态
docker exec trading-backend python -c "
import asyncio
from app.db import AsyncSessionLocal
from app.models.research_feature_matrix import ResearchFeatureMatrixRun
from sqlalchemy import select
async def main():
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(ResearchFeatureMatrixRun).order_by(ResearchFeatureMatrixRun.started_at.desc()).limit(1))
        run = r.scalar_one_or_none()
        if run:
            print(f'run_key={run.run_key} status={run.status} rows={run.rows_count} failed_rows={run.failed_count} failed_instruments={run.metadata_json.get(\"failed_instruments\") if run.metadata_json else None}')
asyncio.run(main())
"

# 表大小
docker exec trading-postgres psql -U bz -d bz_stock -c "
SELECT relname, pg_size_pretty(pg_total_relation_size(relid))
FROM pg_catalog.pg_statio_user_tables
WHERE relname LIKE 'research_feature_matrix%';"

# 日志 traceback 检查
docker logs trading-backend --tail 200 2>&1 | grep -i traceback
```

**验收通过标准**：
- `run.status = succeeded`；
- `failed_rate = failed_rows / expected_rows <= 5%`；
- `df -h /` 剩余 >= 15GB；
- 表大小合理（单月 < 3GB）；
- 日志无 traceback。

**Phase 1 实际验收数据（2026-07-09）**：
- A dry-run：5293 股 × 20 交易日，expected_rows=105860，estimated_db_size=0.20GB；
- B 2 symbols：rows=40，failed=0，duration=0.6s；
- C 100 stocks：rows=1992，failed=0，duration=19.3s；
- D full Jan：rows=102603，failed=3060，failed_rate=2.90%，duration=1088.8s，表大小 38MB；
- E 后台 6 个月：rows 合计 621,769（2026-01 到 2026-07），覆盖 2026-01-05 到 2026-07-08，表大小 223MB；
- 全部 9 个 run 均 succeeded，failed_rate 最高 4.11%（2026-02），未超过 5% 阈值；
- hindsight / Node Cluster 列全 NULL（Phase 1 未实现）。

**阶段 E 后台逐月回补启动前置条件**：
- 阶段 D（全市场 2026-01）前台验收通过；
- 磁盘剩余 >= 15GB；
- 无其他 research matrix backfill 后台任务运行（lock file 不存在）。

**禁止项**：
- 不要跳过任何阶段（A→B→C→D→E 必须顺序执行）；
- 不要在阶段 D 未通过时启动后台逐月回补；
- 不要并行多月回补（每月串行）；
- 不要在后台任务运行时启动新的同 month/scope 任务（进程锁拒绝）。

## 3.10 趋势选股批量加入 + change_pct + 表格视图配置 + sticky 表头回归（blocking）

任何修改 `frontend/src/pages/ScreenerPage.tsx`（handleBatchAdd）、`frontend/src/features/trend-selection/columns.tsx`（change_pct 列）、`frontend/src/components/StrategyDataTable.tsx`（preset 集成）、`frontend/src/components/TablePresetMenu.tsx`、`frontend/src/styles/global.scss`（sticky 表头/选择列）、`backend/app/api/me_table_view_presets.py`、`backend/app/schemas/table_view_preset.py`、`backend/app/models/table_view_preset.py`、`backend/alembic/versions/059_user_table_view_presets.py` 必须跑本节回归测试。

### 3.10.1 后端 preset API 回归（`tests/test_table_view_presets_api.py`，50 个用例）

权限矩阵（10 个用例）：
- 未认证 → 401（GET/POST/PATCH/DELETE 各一）；
- expired subscription → 403（GET/POST 各一）；
- no subscription → 403（GET/POST 各一）；
- active subscription + trend_selection feature → 200/201（GET/POST 各一）；
- admin → 200/201（GET/POST 各一，admin 豁免 feature 检查）。

CRUD（8 个用例）：
- GET 按table_id 过滤；
- GET 按 table_id + strategy_key 过滤（含 NULL strategy_key 匹配空字符串）；
- POST 创建返回 201 + 完整字段；
- PATCH 更新 name/config/is_default；
- DELETE 返回 204；
- PATCH 他人 preset → 404（避免泄露存在性）；
- DELETE 他人 preset → 404。

用户隔离（2 个用例）：
- 用户 A 创建的 preset 用户 B GET 不可见；
- 用户 B PATCH/DELETE 用户 A 的 preset → 404。

重名冲突（4 个用例）：
- POST 同维度同名（strategy_key 非空）→ 409；
- PATCH 重命名为同维度已有 name（strategy_key 非空）→ 409；
- POST 同维度同名（strategy_key=NULL）→ 409（验证 partial unique index `uq_user_table_view_preset_strategy_null`）；
- PATCH 重命名为同维度已有 name（strategy_key=NULL）→ 409。

NULL strategy_key 隔离（2 个用例）：
- 不同 table_id + strategy_key=NULL + 同名 → 允许（201）；
- 不同 user + 同 table_id + strategy_key=NULL + 同名 → 允许（201）。

quota（2 个用例）：
- 同维度已有 20 个 preset 时 POST → 422；
- 不同 strategy_key 不共享 quota。

非法 config（5 个用例）：
- config 含 `selectedKeys` → 422；
- config 含 `page` → 422；
- config 含 `activeRunId` → 422；
- config 含 `rows` → 422；
- config 含未知字段 → 422。

config 深度校验（6 个用例）：
- config.filters 元素不是 dict → 422；
- config.filters 元素缺 key 字段 → 422；
- config.filters op 不在白名单（如 `regex`）→ 422；
- config.filters 所有合法 op（contains/eq/gt/gte/lt/lte/between/empty/not_empty）通过 → 201；
- config.hiddenColumns 元素不是 string → 422；
- config.sort.key 为空字符串 → 422。

is_default 互斥（2 个用例）：
- POST is_default=true 时同维度旧默认自动取消；
- PATCH is_default=true 时同维度旧默认自动取消（排除自身）。

必填字段校验（3 个用例）：
- POST 缺 table_id → 422；
- POST 缺 name → 422；
- POST 缺 config → 422。

user_id 注入安全（1 个用例）：
- POST body 中传 `user_id` 字段被忽略（user_id 由 JWT 上下文注入）。

PATCH 空请求（1 个用例）：
- PATCH 不传任何字段 → 422（至少一个字段）。

迁移幂等（1 个用例）：
- `alembic upgrade head` 创建 `user_table_view_presets` 表 + 两个 partial unique index；
- `alembic downgrade -1` 删除表；
- `alembic upgrade head` 再升级不报错。

跨 session 持久化（3 个用例）：
- `test_create_persists_across_sessions`：POST 创建 preset 后，使用新 `AsyncSession` 重新查询，确认记录已持久化；
- `test_update_persists_across_sessions`：PATCH 更新 name/config/is_default 后，使用新 `AsyncSession` 确认字段已持久化；
- `test_delete_persists_across_sessions`：DELETE preset 后，使用新 `AsyncSession` 确认记录不存在。

### 3.10.2 前端 columns.test.ts 回归（13 个用例，CHANGE-20260713-005 扩展）

- change_pct 列存在于 trend-selection columns；
- title=`当日涨跌幅`、shortTitle=`涨跌幅`；
- dataType=`percent`、sortable=true、filterable=true、width≈86；
- render 使用 `fmtChange` + `changePctColorClass`（涨红跌绿）；
- sortValue 读取 payload `change_pct`/`pct_change`/`change_percent`；
- change_pct 列位于 stock 列之后；
- action 列 onDetail 按钮 onClick 调用 `e.stopPropagation()`；
- action 列 onAddToWatchlist 按钮 onClick 调用 `e.stopPropagation()`；
- 股票名称链接 onNavigate 调用 `e.stopPropagation()`（CHANGE-20260713-005）；
- action 列 onToggleWatchlist 模式按钮 stopPropagation + 显示"加入自选/移除自选"（CHANGE-20260713-005）；
- action 列 onToggleWatchlist 模式下 title 动态为"自选"（CHANGE-20260713-005）；
- 股票名称链接使用 `<a>` + `e.preventDefault()`（CHANGE-20260713-005）；
- renderStock 只显示名称/代码/市场，不渲染行内涨跌幅（CHANGE-20260713-005）。

### 3.10.2a 前端 chartLabels.test.ts 回归（5 个用例，CHANGE-20260713-005 新增）

- 节点价格标签：POC 峰 → "核心共识价"，普通峰 → "共识价"；
- POC 中心线标签显示"核心共识价"（非裸"POC"）；
- tooltip 中 POC → "核心共识价"，PEAK → "共识价"；
- VP 数据缺失提示为"筹码共识价暂不可用"；
- 内部字段名 `n.poc`/`profile.pocPrice`/`row.is_poc`/`is_poc` 保留（不改 DTO/算法）。

### 3.10.2b 前端 chartDrag.test.ts 回归（7 个用例，CHANGE-20260713-005 新增）

- 使用 Pointer Events（pointerdown/pointermove/pointerup/pointercancel）；
- 使用 setPointerCapture / releasePointerCapture；
- dragRef 保存 startClientX + startViewport + pointerId；
- dragMovedRef 4px 阈值抑制 click；
- cursor 为 grab/grabbing；
- 不使用旧 mouse 事件（mousedown/mousemove/mouseup 作为事件监听器）；
- pointermove 从 startViewport 计算总位移（不在 stale viewport 上累计）。

### 3.10.2c 前端 marketToolbarSearch.test.ts 回归（8 个用例，CHANGE-20260713-005 新增）

- MarketToolbar 接受受控 keyword/onKeywordChange props；
- placeholder 为"搜索股票代码/名称/拼音首字母"；
- Enter 提交（onKeyDown Enter 调用 onKeywordChange）；
- blur 提交（onBlur 调用 onKeywordChange）；
- 清空立即提交（inputValue 为空时立即调用 onKeywordChange('')）；
- /market 通过 `searchable={false}` 隐藏 StrategyDataTable 内置搜索；
- StrategyDataTable 接受 externalKeyword/onKeywordChange 受控模式；
- 单一搜索状态真源（顶栏 keyword 与 StrategyDataTable URL keyword 一致）。

### 3.10.2d 前端 messagesCounts.test.ts 回归（8 个用例，CHANGE-20260713-005 新增）

- MessagesPage 使用 useUnreadCount 作为未读 SSOT；
- "全部"计数使用后端 `messagesQuery.data?.total`（非 items.length）；
- 页头显示"共 X 条 · 未读 Y 条"；
- selection/price/system/process 不显示误导数字（仅 all/unread 显示计数）；
- 单只股票消息跳转 `/stock/:symbol?event_id=...&returnTo=/messages`；
- selection_composite 跳转 `/market`（非 `/screener`）；
- AccountMenu unread>0 时消息链接为 `/messages?filter=unread`；
- AccountMenu 消息项显示未读数（itemBadge）。

### 3.10.2e 前端 indicatorManifest.test.ts 回归（15 个用例，CHANGE-20260713-005 扩展，CHANGE-20260715-001 新增 3 个 SMC 用例）

- 原 10 个用例不变；
- manifest 用户文案：sqzmom → "挤压动量"，node → "筹码共识价"；
- 内部 ChartLayerKey 不变：sqzmom/node 仍为内部 id；
- **SMC 图层断言（CHANGE-20260715-001 新增 3 用例）**：`smc` 图层存在于 `CHART_LAYER_MANIFEST`，name="智能资金"，kind="main"；`smc` 默认关闭（`default` 中 `smc: false`）；`ChartLayerVisibility` 类型含 8 键（含 `smc`）。

### 3.10.2f 后端 test_strategy_results_keyword.py 回归（3 个用例，CHANGE-20260713-005 新增）

- keyword 按股票代码 ILIKE 匹配（如 `600519` 匹配 `贵州茅台`），items 与 total 条件一致；
- keyword 按中文名称 ILIKE 匹配（如 `茅台` 匹配 `贵州茅台`），items 与 total 条件一致；
- keyword 按拼音首字母 ILIKE 匹配（如 `gzmt` 匹配 `贵州茅台`），items 与 total 条件一致。

### 3.10.3 前端 ScreenerPage.batch.test.ts 回归（6 个用例）

- handleBatchAdd 按 `r.instrumentId` 匹配 `selectedKeys`（禁止用 `r.resultId`）；
- 选中后无可加入股票时 toast 提示（非静默）；
- 成功/失败 toast 真实反映数量；
- 对 `instrumentId` 去重避免重复加入；
- rowKey 与 selectedKeys 一致（都是 instrumentId）；
- 保留 `useAddToWatchlist` 现有缓存失效逻辑。

### 3.10.4 前端 StrategyDataTable preset 集成回归

- `currentConfig` 从内部 state 构建 config 快照（keyword/sort/filters/hiddenColumns/pageSize）；
- `applyPresetConfig` 从 config 重置内部 state；
- 默认 preset 自动应用（每个 tableId:strategyKey 只应用一次，useRef 防重复）；
- `TablePresetMenu` 渲染保存/应用/覆盖/重命名/设默认/删除按钮；
- 点击外部关闭下拉；
- 错误处理：catch + toast 显示后端 detail 消息。

### 3.10.5 sticky 表头/选择列回归

- `global.scss` 中 `.interactive-table thead th` sticky top:0 z-index:4；
- `.interactive-table .sticky-col` sticky left:0 z-index:3；
- `.interactive-table .table-select-column` sticky left:0 z-index:3；
- `.interactive-table thead th.sticky-col, thead th.table-select-column` z-index:5（角落单元格最高）；
- `.interactive-table .table-select-column + th.sticky-col` left:40px（首列偏移选择列宽度）。

### 3.10.6 前端 tablePresetMenu.test.ts 回归（4 个用例）

- `savePreset` 空名称时提示输入名称并直接返回，不调用 mutation；
- `savePreset` 成功时 trimmed 名称、清空输入框、清除下拉错误、toast 成功、并调用 `presetsQuery.refetch()` 刷新列表；
- `savePreset` 失败时在下拉内显示后端 detail 并 toast 错误；
- `savePreset` 失败且无 detail 时使用默认文案“保存失败”。

### 3.10.7 前端 stickyHeader.test.ts 回归（4 个用例）

- `StrategyDataTable` 支持 `stickyHeaderMode?: "viewport" | "container"` prop；
- `stickyHeaderMode === "viewport"` 时为 `.table-wrap` 附加 `viewport-sticky` class；
- `ScreenerPage` 对 `StrategyDataTable` 传入 `stickyHeaderMode="viewport"`；
- `global.scss` 中 `.table-wrap.viewport-sticky` 不抢占滚动容器（`overflow: visible`）；
- `.table-wrap.viewport-sticky .data-table th` 的 `top` 使用 `var(--topbar)`，`z-index` 为 18。

回归命令：

```bash
cd /root/web_dev/backend
APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
pytest tests/test_table_view_presets_api.py -q

ruff check app/api/me_table_view_presets.py \
  app/schemas/table_view_preset.py \
  app/models/table_view_preset.py \
  alembic/versions/059_user_table_view_presets.py \
  tests/test_table_view_presets_api.py

mypy app/api/me_table_view_presets.py \
  app/schemas/table_view_preset.py \
  app/models/table_view_preset.py

cd /root/web_dev/frontend
npx tsc --noEmit
node --experimental-strip-types --test \
  src/features/trend-selection/__tests__/columns.test.ts \
  src/pages/__tests__/ScreenerPage.batch.test.ts \
  src/components/__tests__/tablePresetMenu.test.ts \
  src/components/__tests__/stickyHeader.test.ts
```

预期：50 passed（后端）+ 20 passed（前端 columns 6 + batch 6 + tablePresetMenu 4 + stickyHeader 4）、ruff 零错误、mypy 零错误、tsc 零错误。

## 3.11 管理员入口 + 批准 Logo + 视觉 V1.0 + K线右侧留白 + 数量契约回归（blocking）

任何修改 `frontend/src/components/AccountMenu.tsx`、`frontend/src/navigation/appNavigation.ts`、`frontend/src/components/AdminRoute.tsx`（或 `ProtectedLayout` 权限判断）、`frontend/src/components/BrandLogo.tsx`、`frontend/src/styles/variables.scss`、`frontend/src/styles/global.scss`、`frontend/src/components/StrategyChart.tsx`（右侧留白相关）、`backend/app/repositories/strategy_result_repository.py`（total 语义相关）、`backend/app/api/strategy_runs.py` 必须跑本节回归测试。

### 3.11.1 前端 appNavigation.test.ts 回归（CHANGE-20260713-007 扩展）

- `getAccountMenuItemsForVariant(false, 'user')` 不含"管理后台"入口（普通用户 DOM 不渲染）；
- `getAccountMenuItemsForVariant(true, 'user')` 含"管理后台"链接到 `/admin`；
- `getAccountMenuItemsForVariant(true, 'admin')` 含"返回行情"链接到 `/market`，不含"管理后台"（避免管理员在 AdminAppShell 内重复入口）；
- `getAccountMenuItemsForVariant(false, 'admin')` 仍不含"管理后台"（is_admin=false 时即使 variant=admin 也不显示管理入口）。

### 3.11.2 前端 brandLogo.test.ts 回归（CHANGE-20260713-007 新增）

- `BrandLogo` 渲染 `<img>` 标签引用 `logo_symbol_128.png`（sidebar variant）或 `logo_horizontal_dark.png`（landing/footer variant）；
- 不再渲染手绘 SVG（无 `<svg>` 元素或内联 path）；
- 资产路径位于 `frontend/src/assets/brand/`；
- ref 路径不作为运行时依赖（`import` 来自 `@/assets/brand/`）。

### 3.11.3 前端 visualTokens.test.ts 回归（CHANGE-20260713-007 新增）

- `variables.scss` 含 `$color-brand: #00F6C2` / `$color-brand-light: #39F5CF` / `$color-brand-dark: #00B28A`；
- 背景三色 `$color-bg: #0A0F14` / `$color-bg-elevated: #111A23` / `$color-bg-overlay: #161F29`；
- 文字三色 `$color-text-primary: #F2F6F8` / `$color-text-secondary: #98A1B3` / `$color-text-tertiary: #657281`；
- 边框 `$color-border: #263440`；
- 涨跌色 `$color-up: #FF4D4F` / `$color-down: #22C55E`；
- info/warning `$color-info: #3882F6` / `$color-warning: #F59E0B`；
- 品牌绿只用于 Logo、主按钮、选中、focus 和关键节点（源码契约：不替代涨跌色或所有信息蓝）。

### 3.11.4 前端 chartRightPadding.test.ts 回归（CHANGE-20260713-008 新增）

- `StrategyChart` 源码含 `RIGHT_PADDING_RATIO = 0.20` 常量（落在 0.18-0.22 区间）；
- `effectivePlotW = plotW * (1 - RIGHT_PADDING_RATIO)` 计算逻辑存在；
- `step = effectivePlotW / display.length` 用于交互坐标映射；
- 十字线/滚轮锚点/Pointer 拖拽/双击复位/节点命中/事件命中统一使用 `step`（源码契约）；
- 网格线和十字线水平线仍延伸到 `g.plotRight`（保持全宽，不收缩到 effectivePlotW）；
- 时间轴标签使用 `effectivePlotW`。

### 3.11.5 后端 test_strategy_results_industry_concept.py 回归（CHANGE-20260713-007 扩展）

- provider unavailable 场景：`boards.available=false` 时 industry/concept 筛选返回空结果，`filtered_total=0`，`source_total` 仍为 run 原始总量；
- nonexistent 板块场景：industry/concept 传入不存在的板块名称时返回空，`filtered_total=0`；
- **数量契约四层语义**：`source_total`（published run 原始总量，不受业务筛选影响）≥ `universe_total`（all/watchlist 范围总量）≥ `filtered_total`（keyword+industry+concept+metric_filters 后总量）≥ `len(items)`（当前页）；默认无筛选时 `source_total == universe_total == filtered_total`；
- 禁止 `source_total` 受 keyword/industry/concept/metric_filters 影响。

回归命令：

```bash
cd /root/web_dev/backend
APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
pytest tests/test_strategy_results_industry_concept.py -q

cd /root/web_dev/frontend
npx tsc --noEmit
node --experimental-strip-types --test \
  src/navigation/__tests__/appNavigation.test.ts \
  src/components/__tests__/brandLogo.test.ts \
  src/components/__tests__/visualTokens.test.ts \
  src/components/__tests__/chartRightPadding.test.ts
```

## 3.12 CHANGE-010 回归（blocking，市值 + Excel 导出 + 小 K 线 + 股票名称筛选）

```bash
APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
  pytest backend/tests/test_excel_export_api.py backend/tests/test_excel_export_service.py -q

node --experimental-strip-types --test \
  src/features/market-workspace/__tests__/change010Contract.test.ts
```

覆盖规则：

- **市值字段（CHANGE-20260713-010）**：后端 `QuoteResponse` 含 `total_market_cap`/`float_market_cap`/`market_cap_as_of`/`market_cap_source`/`market_cap_degraded_reason` 5 个字段；数据缺失时返回 `market_cap_degraded_reason="market_cap_data_unavailable"` 不伪造；前端 `QuoteResponse` 类型含 5 个可选字段，`PriceSummary` 接口含 `totalMarketCap`/`floatMarketCap`/`marketCapAsOf`；`StockQuoteStrip` 暴露 `formatMarketCap`（区分万/亿/万亿元，空值显示 `--`）+ `QuoteMetric` 子组件；tooltip 显示数据日期。
- **Excel 导出**：`ExportRequest` schema 含 universe/keyword/industry/concept/metric_filters/sort_by/sort_desc/visible_columns；`excel_export_service` 含 `MAX_EXPORT_ROWS=10000`、`_sanitize_formula_injection`、`zipfile` 生成真实 .xlsx、`numFmt` 百分比格式、禁止 `openpyxl`/`xlsxwriter` import；`strategy_runs` API 含 `POST /strategy-runs/{run_id}/results/export` 端点、`X-Source-Total`/`X-Universe-Total`/`X-Filtered-Total` 响应头、文件名 `盘迹_DSA_YYYYMMDD_筛选结果.xlsx` + RFC 5987 编码、超 10000 行 422；前端 `ExportContext` 类型 + `MarketWorkspacePage.handleExport` 复用 `convertFiltersToMetricFilters`（与 `buildStrategyResultQueryParams` 同源）、`stock` 列 `payload_key=null` 不导出操作列；后端 API 集成测试 21 项覆盖权限（401 未登录/403 无订阅/403 过期/200 admin/200 active member）+ published run 校验（404 不存在/404 未发布）+ universe all/watchlist + keyword/industry/concept/metric_filters/sort 筛选 + 行数 = `filtered_total` + visible_columns 列顺序 + 非法 sort_by 422 + 公式注入防护 + 10000 上限 422 + MIME/Content-Disposition + 无 N+1 + 文件 zip 完整性/XML 解析/workbook 关系/单元格类型。
- **股票名称筛选 alias**：`DataTableColumn.filterAlias?: 'keyword'` 字段；`StrategyDataTable` 含 `KeywordFilterPopover` 组件 + `isKeyword` flag + `effectiveKeyword`；`stock` 列设置 `filterAlias: 'keyword'`（与顶部搜索共用唯一真源）。
- **小 K 线**：`useMiniKlineData` 定义 `BARS_COUNT` = `{1d: 80, 1w: 60, 1mo: 48}` + `refetchInterval: false`；`MiniKlineCard` 使用 `lightweight-charts createChart` + `CandlestickSeries` + 三按钮"日线/周线/月线" + 默认 `1d`；不引入 `addVolumeSeries`/`VolumeSeries`/指标/Node/事件依赖；`MarketRightPanel` 组合 `MiniKlineCard` + `EventStatePanel`。
- **详情来源上下文不回归**：`MarketWorkspacePage.handleExport` 复用 `convertFiltersToMetricFilters`；`marketWorkspaceUrlState` 导出 `convertFiltersToMetricFilters` 并复用 `normalizeMetricValue`。

`change010Contract.test.ts` 49 项源码契约测试覆盖五大主题（市值字段映射 + Excel 导出契约 + 股票名称视觉入口 + 小 K 线 + 详情来源上下文）。

## 3.13 CHANGE-20260715-001 回归（SMC 指标 + MiniKline viewport P0 修复）

```bash
# SMC 后端算法 + 缓存 + 服务测试
APP_ENV=test TEST_DATABASE_URL=postgresql+psycopg://bz:bz@localhost:5433/bz_stock_test \
  pytest backend/tests/test_smc_indicator.py backend/tests/test_indicator_cache.py backend/tests/test_indicator_service.py -q

# SMC + MiniKline viewport 前端契约测试
node --experimental-strip-types --test \
  src/features/stock-research/__tests__/indicatorManifest.test.ts \
  src/features/market-workspace/__tests__/miniKlineViewport.test.ts
```

覆盖规则：

- **SMC 后端算法（`test_smc_indicator.py`，34 用例）**：`compute_smc` 纯函数仅依赖 Python 标准库（无 numpy/pandas 依赖）；市场结构关键点位序列长度与输入 bar 对齐；边界用例（bar 不足、全平数据、单调序列）；FVG 完全排除（输出不含任何 FVG 字段）。
- **SMC 缓存隔离（`test_indicator_cache.py` 新增 3 用例）**：`include_smc=true` 时 cache key 追加 `:smc` 后缀；`include_smc=false`（默认）cache key 不含 `:smc` 后缀；两路径互不污染（默认路径缓存不含 SMC 字段）。
- **SMC 服务层（`test_indicator_service.py` 新增 3 用例）**：`compute_all_indicators(include_smc=False)` 默认不计算 SMC；`compute_all_indicators(include_smc=True)` 计算 SMC 且响应含 `data.smc`；`include_smc` 不影响 DSA/Node/BB/MACD/SQZMOM 计算结果。
- **前端 manifest（`indicatorManifest.test.ts`，15 用例）**：见 §3.10.2e。
- **MiniKline viewport（`miniKlineViewport.test.ts`，12 用例）**：纯函数 `computeMiniKlineViewport` 按周期 clamp——15m/60m 50-64、日线 48-58、周线 40-52、月线 30-40；右侧留白 3 根 bar；不调用 `fitContent`；bar 不足时 clamp 不越界；各周期 viewport `toIndex - fromIndex` 落在对应区间内。

SMC 只进入 `/stock/:symbol` 个股详情指标链，不进入 `/market`、DSA、Node、Capture、盘中监控、选股；FVG 完全排除。

## 3.14 CHANGE-20260715-002 回归（SMC Pine parity + MiniKline viewport 重写 + SMC renderer 对齐）

```bash
# SMC Pine 语义测试 + golden fixture skip + 已有测试更新
APP_ENV=test TEST_DATABASE_URL=postgresql+psycopg://bz:bz@localhost:5433/bz_stock_test \
  pytest backend/tests/test_smc_indicator.py backend/tests/test_indicator_cache.py backend/tests/test_indicator_service.py -q

# MiniKline viewport 重写后的纯函数测试
node --experimental-strip-types --test \
  src/features/market-workspace/__tests__/miniKlineViewport.test.ts
```

覆盖规则：

- **Pine 语义原语（`test_smc_indicator.py::TestPineSemantics` 8 用例）**：
  - `pine_rma` Wilder 递推：SMA 播种 + `rma[i]=(rma[i-1]*(length-1)+src[i])/length`，前 `length-1` 个为 NaN
  - `pine_rma` min_periods：前 `length-1` 个值为 NaN
  - `pine_cumulative_mean_range` bar0=NaN（除零行为，`ta.cum(ta.tr)/bar_index`）
  - `pine_atr = pine_rma(pine_true_range, length)`：所有 bar 相等
  - `pine_crossover`/`pine_crossunder`：穿越检测正确
  - `pine_highest`/`pine_lowest`：滚动极值不含当前 bar
- **Pine golden fixture（`test_smc_indicator.py::TestPineGoldenFixture`）**：fixture 不存在时 skip，**没有 Pine golden fixture 不得宣称"完全对齐"**；fixture 路径 `backend/tests/fixtures/smc_pine/`，包含美诺华 603538 日线 1000 根 + 一个 15m 样本
- **TV CSV parity 门禁（`test_smc_tv_parity.py`，CHANGE-20260716-001）**：使用 `ref/smc_user_export.pine`（派生导出副本，真源 `ref/smc_user_source.pine` 不可变）末尾 18 个 `display=display.none` 隐藏 plot 字段从 TV 导出 CSV（含 time/OHLC + Pine 事件布尔值 + bias）；fixture 路径 `backend/tests/fixtures/smc_pine/smc_tv_<symbol>_<tf>.csv`；无 fixture 时 skip（`PINE_PARITY_PENDING`）；3 项测试：
  - `test_tv_csv_bar_parity`：断言 time/OHLC/bar 数量逐项相等（浮点容差 1e-8），不相等写 `INPUT_BAR_MISMATCH`，**不得调整算法迎合截图**
  - `test_tv_csv_event_parity`：比较事件有序序列（bar_index ±1 容差）
  - `test_tv_csv_swing_bias_parity`：比较最后一根 bar 的 swing_bias
  - **禁止从 DB 重新取另一套 Bar**；产品默认前复权，TV parity fixture 使用与 TV 完全相同的复权方式、数据源和 completed-bar 边界
- **events 字段契约更新**：`test_event_kind_valid` → `test_event_internal_field_valid`（验证 `internal: bool` 替代旧 `kind` 字段）
- **缓存隔离（`test_indicator_cache.py`）**：`ALGORITHM_VERSION == "v9"`（CHANGE-20260716-001：v8→v9，crossover/crossunder 修正后旧 v8 缓存强制失效）；SMC/non-SMC 键隔离（`include_smc=true` 追加 `:smc` 后缀）
- **服务层 warmup（`test_indicator_service.py`）**：1d timeframe 使用 `full_daily_bars`（≥500 warmup）；SMC 输出不调用 `_truncate_lists` 截断（time 数组保持完整长度）；**CHANGE-20260716-001 required_inputs**：`_REQUIRED_INPUTS` 映射 + `_determine_required_bars()`；15min 回看 400 天（limit=4000）、minute 回看 5 天（limit=2）、60min 回看 750 天；`needs_15min = "15min" in required_bars or timeframe == "15m"`、`needs_minute = "minute" in required_bars`；`_query_minute_bars` 新增 `limit` 参数（DESC + LIMIT + 反转）；**SMC source diagnostics（CHANGE-20260716-001）**：`smc_source_bar_hash` 基于 SMC 实际完整输入（1d 用 `full_daily_bars`，其他用 `macd_bars`），不复用截断后的 macd_bars hash
- **MiniKline viewport（`miniKlineViewport.test.ts`，CHANGE-20260716-001 真实方案）**：
  - 目标根数：15m=48、60m=44、日=40、周=36、月=30（`bars.slice(-target)` 传给 series）
  - `setData` 后设置 logical range `{from:-2, to:visibleData.length-1+3}`（真实左 2/右 3 空位）
  - **删除死 barSpacing 计算**（旧 `barSpacing = clamp(contentWidth/visibleBars, 5.5, 8)` 只计算未应用，是死参数）
  - `computeAutoscaleRange(minLow, maxHigh)`：上方 12%，下方 15%（只基于 visibleData 的 high/low）
  - 五周期边界验证
  - 空数据返回零区间
  - 真实 mock 测试：setData 根数、range、五周期切换、ResizeObserver cleanup、chart.remove、0 旧数据残留
- **前端 SMC renderer 对齐 Pine（无截图 E2E，CHANGE-20260715-002 → CHANGE-20260716-001 修正）**：internal=虚线 `[4,3]`/tiny 8px、swing=实线/small 11px；**标签不加 `·I` 后缀**（CHANGE-20260716-001，与 TV 文字一致）；标签中点 `(x1+x2)/2`+`'center'`；trailing 文案"强高/弱高/强低/弱低"，`swing_bias` 直接从 DTO 读取（CHANGE-20260716-001，禁止猜测）；OB 半透明 box（active 0.12、mitigated 0.05）；Historical 全相交事件；颜色多头红 `#FF4D4F`、空头绿 `#22C55E`；**anchor_index 统一**（CHANGE-20260716-001：前端不得读取 `bar_index`）；**viewport 区间求交**（CHANGE-20260716-001：anchor 左侧 clip+clipped_left，confirmed 右侧 clamp 到 plotRight，仅完全不相交跳过）；**OB slice(0,5)**（CHANGE-20260716-001：只显示数组头部最近 5 个 `internal && !mitigated` OB，活动 OB 延伸到右端）；**纵轴候选完整**（CHANGE-20260716-001：event.level、OB high/low、EQH/EQL level、trailing top/bottom）；**EQH/EQL 视觉线端点使用 `second_pivot_index`**（CHANGE-20260716-001）；**纯函数 + Canvas mock 行为测试**（`smcRendering.test.ts`，CHANGE-20260716-001：禁止只用源码正则）
- **SMC 隔离边界**：SMC 仅属于 `/stock` 指标链；`include_smc=false` 时 0 计算；`/market` 右栏不请求 SMC；true/false 缓存键隔离；DSA/Node/监控/Capture/published run 不修改；无新表/migration/worker/历史回填

## 3.15 CHANGE-20260715-003 回归（SMC trailing 顺序修复 + sticky 列 + 工具栏对齐 + MiniKlineCard 契约测试）

```bash
# SMC trailing 顺序修复后的回归测试
cd backend && APP_ENV=test TEST_DATABASE_URL=... python -m pytest \
  tests/test_smc_indicator.py tests/test_indicator_cache.py \
  tests/test_indicator_service.py tests/test_indicator_contract.py \
  tests/test_indicators_api.py -q --no-header

# MiniKlineCard 组件契约测试
cd frontend && node --experimental-strip-types --test \
  src/features/market-workspace/__tests__/miniKlineCardContract.test.ts
```

- **SMC trailing 执行顺序（`test_smc_indicator.py`）**：`_SMCPineState.run()` 中 `update_trailing_extremes` 必须在 `get_current_structure` 之前执行（对齐 Pine lines 766-807）；trailing 极值不被当前 bar 的 high/low 二次覆盖；Strong/Weak High-Low 事件输出与 Pine 一致；已有 109 项测试全部通过（含 Pine 语义 8 项 + FVG 排除 + golden fixture skip）
- **MiniKlineCard 组件契约（`miniKlineCardContract.test.ts` 15 用例）**：不调用 `fitContent`/`resetTimeScale`/`scrollToRealTime`（正则 `\.fitContent\(` 检查实际方法调用）；调用 `setVisibleLogicalRange`；使用 `computeMiniKlineViewport` 纯函数；使用 `computeAutoscaleRange` + `autoscaleInfoProvider`；`ResizeObserver` 响应式 + `disconnect()`；`requestAnimationFrame` 延迟应用 range；五周期按钮（15m/1h/1d/1w/1mo）；`attributionLogo: false`；图表高度 190px + `CHART_HEIGHT` 常量；`minimumWidth=MIN_PRICE_SCALE_WIDTH`；`autoScale: true` + `scaleMargins {0.08, 0.08}`；`shiftVisibleRangeOnNewBar: false`；`chart.remove()` 卸载清理；A 股配色（`#FF4D4F`/`#22C55E`）；容器宽度 `Math.floor` 整数化
- **sticky 列固定宽度（无截图 E2E）**：`.interactive-table` 定义 CSS 变量 `--stock-col-width: 150px`/`--select-col-width: 40px`；`.sticky-col` 三重固定宽度（width/min-width/max-width）；横向滚动时 sticky 列不与后续列重叠；长名称 ellipsis 截断；背景不透明
- **工具栏 sticky 对齐（无截图 E2E）**：`.table-meta-bar` + `.table-pager` 添加 `position: sticky; left: 0; width: 100%; z-index: 6`；横向滚动时工具栏和分页器保持可见；右边界与表格可视区一致

## 3.16 CHANGE-20260715-004 回归（Bug 1 详情左栏 loading 占位 + Pine 真源文件入 Git 跟踪）

```bash
# 详情页来源列表 loading 占位契约测试
cd frontend && node --experimental-strip-types --test \
  src/features/stock-research/__tests__/detailSourceLoadingContract.test.ts
```

- **`useStockDetailActions.sourceListLoading` 字段（源码契约）**：接口含 `sourceListLoading: boolean`；实现逻辑为 `hasMarketContext ? (publishedRunsQuery.isLoading || !activeRunId || sourceResultsQuery.isLoading) : monitorStatusQuery.isLoading`；返回对象包含 `sourceListLoading`
- **`StockDetailPage` loading 占位渲染（源码契约）**：包含 `detailActions.sourceListLoading` 条件分支；渲染 `<aside data-testid="detail-source-list-loading"`；占位文案"加载中…"；header 显示 `sourceListKind === 'market'` ? "行情来源" : "自选来源"
- **列表渲染条件排除 loading（源码契约）**：列表 `<aside data-testid="detail-source-list"` 必须以 `!sourceListLoading && sourceStocks.length > 0` 为前置条件，避免 loading 和列表同时渲染
- **CSS 占位样式（源码契约）**：`global.scss` 存在 `.tv-source-list-placeholder` 类，含 `padding`/`font-size`/`color`/`text-align` 属性
- **`MarketWorkspacePage.handleNavigateToStock` 显式传 source/strategy（源码契约）**：`scope === 'market'` 时 `source='selection'` + `strategy=DSA_STRATEGY_KEY`；`scope === 'watchlist'` 时 `source='watchlist'` + `strategy='watchlist_monitor'`
- **URL 完整性（源码契约）**：`/stock/${symbol}?source=${src}&strategy=${strat}&returnTo=${encodeURIComponent(returnTo)}` 包含 source+strategy+returnTo 三个参数
- **不使用旧 `useMarketStocks`（源码契约）**：`useStockDetailActions.ts` 不存在 `useMarketStocks(` 函数调用（注释允许）
- **上一只/下一只保留 returnTo（源码契约）**：`returnToParam = returnTo ? \`&returnTo=${encodeURIComponent(returnTo)}\` : ''` 保留 returnTo 参数
- **生产 E2E 验证（无截图）**：1) `/market` 设置筛选+排序+页码后点击股票名称进入 `/stock/:symbol`，URL 包含 `source=selection&strategy=dsa_selector&returnTo=...`；2) 详情页左栏先显示 loading 占位（`[data-testid="detail-source-list-loading"]`）再切换到实际列表（`[data-testid="detail-source-list"]`）；3) 左栏 header 显示"行情来源"；4) 上一只/下一只 URL 保留 source/strategy/returnTo/timeframe；5) 返回 `/market` 后筛选/排序/页码完整恢复
- **Pine 真源文件入 Git 跟踪**：`git ls-files ref/smc_user_source.pine` 返回该文件路径；SHA256 为 `0bd3d2ad8819f2dc7a9399f0e869ca3c9eced8100f190aa131aac5fe8191988f`；843 行；`.gitignore` 仍排除 `ref/` 其他文件

## 3.17 CHANGE-20260715-005 回归（详情左栏来源状态四态拆分 + 表格 sticky 列和工具栏对齐根治）

```bash
# 详情左栏来源状态四态 + normalizeInternalReturnTo 上限契约测试
cd frontend && node --experimental-strip-types --test \
  src/features/stock-research/__tests__/detailSourceLoadingContract.test.ts \
  src/features/market-workspace/__tests__/marketWorkspaceUrlState.test.ts \
  src/components/__tests__/stickyHeader.test.ts
```

- **`useStockDetailActions` 来源状态四态（源码契约）**：接口含 `sourceListLoading`/`sourceListError`/`sourceListEmpty`/`sourceContextInvalid` 四个布尔字段；`sourceListError`=`publishedRunsQuery.isError || sourceResultsQuery.isError`；`sourceListEmpty`=`!sourceListLoading && !sourceListError && sourceStocks.length === 0`；`sourceContextInvalid`=`source === 'selection' && (!decodedMarketContext || !decodedMarketContext.scope)`
- **source 参数优先级（源码契约）**：显式 `source` 参数 > `returnTo` 推断；`source === 'selection'` → `sourceListKind='market'`（即使 returnTo 无效也不回退 watchlist，仅设置 `sourceContextInvalid=true`）
- **`normalizeInternalReturnTo` 上限 4096（源码契约）**：`marketWorkspaceUrlState.ts` 的 `normalizeInternalReturnTo` 长度上限为 4096（非 500）；仍仅允许 `/screener`/`/market`/`/messages` 前缀，拒绝外部 URL/`javascript:`/双斜杠/非白名单前缀
- **表格结构 `table-shell`（源码契约）**：`StrategyDataTable.tsx` 和 `AdminAfterClosePipelinePage.tsx` 使用 `table-shell > meta-bar + search-bar + table-scroll > table + pager` 结构；只有 `table-scroll` 设置 `overflow-x: auto`；meta-bar/search-bar/pager 移出横向滚动容器
- **`isStickyColumn` 统一判断（源码契约）**：`isStickyColumn(col)` 函数只允许 `col.key === 'stock'` 为 sticky 列；header 和 body 共用同一判断；涨跌幅列保持普通列
- **删除死 CSS（源码契约）**：`global.scss` 不存在 `.sticky-col-change-pct` 类；不存在 `position:sticky;left:0;width:100%` 工具栏补丁（meta-bar/pager 不再需要 sticky）
- **viewport-sticky 模式（源码契约）**：`.table-shell.viewport-sticky .table-scroll { overflow: visible; }`（viewport sticky 模式下 table-scroll 不滚动）
- **生产 E2E 验证（无截图）**：1) `/market` 复杂筛选（keyword+industry+concept+filters+sort+page，URL 编码后 >500 字符）进入详情，returnTo 不被截断；2) returnTo 无效时左栏显示 invalid 引导（不回退 watchlist）；3) 表格横向滚动时配置/列设置/清除/导出和分页器不随滚动消失，右边界对齐；4) 只有 stock 列 sticky，涨跌幅列随滚动；5) `AdminAfterClosePipelinePage` 表格结构与 `/market` 一致

## 3.18 CHANGE-20260715-006 回归（MiniKline 闭包根治 + SMC Pine 对齐 RMA NA 语义 + 首个 pivot off-by-one + EQH/EQL 三时间点）

```bash
# SMC Pine 语义测试（含 RMA NA 语义 + 首个 pivot off-by-one）
cd backend && .venv/bin/pytest tests/test_smc_indicator.py -q

# MiniKline 闭包契约测试（含 5 项闭包契约 16-20）
cd frontend && node --experimental-strip-types --test \
  src/features/market-workspace/__tests__/miniKlineCardContract.test.ts
```

- **`pine_rma` NA 语义（后端单元测试）**：`smc_pine_core.py` 的 `pine_rma(src, length)` 在 `bar_index < length-1` 返回 `na`（非逐步 SMA）；`bar_index == length-1` 写入 `SMA(src, length)` 种子；`bar_index >= length` 使用 Wilder 递推 `rma[i] = (rma[i-1]*(length-1) + src[i]) / length`；`test_pine_rma_min_periods_before_seed` 断言前 `length-1` 根为 NaN
- **首个 pivot off-by-one（后端单元测试）**：`start_of_new_leg`/`start_of_bearish_leg`/`start_of_bullish_leg` 使用 `i >= size`（非 `i > size`）；`get_current_structure` 使用 `if i < size: return`（非 `if i <= size: return`）；首个 leg/pivot 在 `i == size` 检测
- **EQH/EQL DTO 三时间点（后端单元测试，CHANGE-20260715-006 → CHANGE-20260716-001 统一）**：EQL 和 EQH 两处 DTO 含三组时间点：`anchor_index`/`anchor_time`（前一 pivot bar）、`second_pivot_index`/`second_pivot_time`（新 pivot bar, `ref_i=i-size`，**视觉线端点**）、`confirmed_index`/`confirmed_time`（当前检测 Bar `i`，**因果/回放使用**）；`ref_i` 不得命名为 `confirmed`；CHANGE-20260716-001 新增 `detection_index`/`detection_time`（leg change 确认 bar, `i`，与 confirmed 同义但语义更明确）
- **MiniKline 闭包根治（前端源码契约 16-20）**：`MiniKlineCard.tsx` 新增 `barsLengthRef`/`timeframeRef`/`rafIdRef` 持有最新值（每次 render 同步，在 effects 之前）；`applyViewportRange` 改为 `useCallback([], )` 稳定函数从 refs 读取（不再直接闭包捕获 `bars.length`/`timeframe`）；`scheduleApplyRange` 稳定函数取消 pending rAF 后调度新 rAF；`ResizeObserver` 回调调用 `scheduleApplyRange`（不直接闭包捕获 bars/timeframe）；卸载清理取消 pending rAF（`cancelAnimationFrame`）
- **Pine 语义核对（源码契约）**：ATR200=`pine_rma(tr, 200)`；highest/lowest 窗口 `[ref_i+1, ref_i+length+1]`（不含当前 bar）；**crossover/crossunder level_curr/level_prev 快照（CHANGE-20260716-001）**：`displayStructure` 接收 `level_curr`（当前 Bar pivot level）和 `level_prev`（上一 Bar pivot level）独立快照，crossover=`close_curr > level_curr && close_prev <= level_prev`，crossunder=`close_curr < level_curr && close_prev >= level_prev`，NaN→False；每 Bar 快照六个 pivot level（swing/internal 独立，不互相覆盖）；OB slice `[piv.bar_index, current_i)` end-exclusive（Python 切片天然 end-exclusive）；trailing 顺序 `update_trailing_extremes → getCurrentStructure(50) → getCurrentStructure(5) → getCurrentStructure(3) → displayStructure → deleteOrderBlocks`
- **Golden fixture**：仍为 `PINE_OUTPUT_GOLDEN_PENDING`/`PINE_PARITY_PENDING`（`backend/tests/fixtures/smc_pine/` 只有 README.md），无 fixture 时 `TestPineGoldenFixture`/`test_smc_tv_parity.py` 自动 skip，不得宣称"完全对齐"；CHANGE-20260716-001 已建立 TV CSV parity 测试框架（`ref/smc_user_export.pine` 派生文件末尾 18 个隐藏 plot 字段，真源 `ref/smc_user_source.pine` 不可变），待用户从 TradingView 导出 CSV 后即可完成输出级完全一致断言
- **SMC 隔离边界不变（源码契约）**：SMC 仅进入 `/stock` 指标链，默认关闭（`include_smc=false` 时 0 计算）；`/market` 右栏小 K 线不请求 SMC；true/false 缓存键隔离（`:smc` 后缀）；不新增表/migration/worker/依赖
- **生产 E2E 验证（无截图）**：1) MiniKline 切周期/resize 后 viewport 正确（不使用 stale 值，不出现首次 render 的旧 range）；2) SMC `pine_rma` 前 `length-1` 根为 NaN（ATR200 前 199 根为 NaN）；3) 首个 pivot 在 `i==size` 检测（不延迟到 `i==size+1`）；4) EQH/EQL DTO 含 detection_index/detection_time；5) Pine golden fixture 仍 PENDING

## 4. CI 门禁

阻断项：

```text
Architecture Rules
Docs Consistency
Test Allowlist
Ruff New Files
Ruff Baseline Regression
Mypy New Files
Mypy Baseline Regression
Alembic Upgrade/Downgrade/Upgrade
PostgreSQL Integration Tests
Frontend Type Check
Frontend Lint
Frontend Build
```

非阻断历史债务展示：

```text
Ruff Full Repository Report
Mypy Full Repository Report
```

禁止通过扩大 ignore、per-file-ignores、noqa、type ignore、exclude 或关闭全仓检查来绕过新增债务。

## 5. 文档一致性

v2 后 docs consistency 应检查 `current/MANIFEST.md`，而不是要求每个 current 文件重复基线头。应用本包时必须同步改脚本和测试。

## 5.1 Mypy baseline 债务治理验收规则

逐文件清理 mypy baseline 债务时，每个债务 PR 必须满足：

- **Before/After 数量**：PR 描述必须列出目标文件修改前后的 mypy 错误数量（如 `after_close_orchestrator.py: 22 → 0`）；
- **只跑目标文件**：使用 `MYPY_CACHE_DIR=/tmp/mypy_debt_cache .venv/bin/mypy <target_file>` 单独检查，不全仓库反复生成 cache；跑完删除 `/tmp/mypy_debt_cache`；
- **不新增运行时行为变化**：只做类型收窄（get-or-raise helper、显式 None 检查、isinstance 收窄），不改状态机语义、不增减异常类型、不改 API 行为；
- **禁止 `cast` / `type: ignore` / `Any` 掩盖**：所有 None 分支必须显式 raise 或 return；类型不匹配必须用 isinstance 或 helper 收窄；
- **禁止直接删除 baseline 条目**：只有实际修复代码、确认死代码并移出检查范围、或用局部 wrapper/stub 解决第三方 typing 问题后，才能减少 baseline；
- **baseline JSON 同步更新**：修复后从 `tools/quality_baselines/mypy.json` 删除对应 diagnostic 条目，并更新 `total` / `unique` 计数；
- **测试通过**：目标文件相关 pytest 全部 passed、ruff 零错误、docs checks 通过；
- **长命令用 nohup/log/pid**：mypy 冷启动可能超 60s，使用 `nohup` 后台执行，不依赖 Trae 交互式长连接，不污染磁盘。

### 5.1.1 债务清理路线图

mypy baseline 已清零（total=0, unique=0）。清理历程：

| 阶段 | 目标 | 状态 |
|------|------|------|
| 1 | `after_close_orchestrator.py` 22 个 Optional 类型错误 | 已完成（CHANGE-007） |
| 2 | `app/api/*` + `capture_main.py` 20 个 BaseRoute.path 错误 | 已完成（CHANGE-008） |
| 3 | Batch A-E 全量清零：models、repositories、services、strategy_assets、worker | 已完成（CHANGE-009） |

### 5.1.2 禁止新增 baseline

- mypy baseline 已清零后，禁止新增 baseline 条目；
- 新增 `backend/app/` Python 生产文件必须 mypy 零错误；
- 如遇第三方库 typing 缺陷，必须用局部 wrapper/stub 解决，不得加 `type: ignore` 或写入 baseline；
- 新增债务必须 PR 中解释原因并不得进入 main；
- `tests/` 目录的 mypy 错误不在 baseline 管控范围，但鼓励逐步修复。

### 5.1.3 Ruff baseline 债务治理规则

- **C408（dict/list literal）**：可用 `ruff --fix --unsafe-fixes` 自动修复，修复后从 baseline 删除；
- **N806（变量命名）**：只允许在算法对齐变量上使用局部 `# noqa: N806` 并加注释 "kept to match upstream algorithm naming"；
- **禁止无说明 blanket ignore**：如需 per-file `# ruff: noqa: N806`，必须在文件头注释说明原因（如 "Pine Script replica" / "SMC standard naming"）；
- **strategy_assets 虽是算法资产，但存在生产 import**，不得从质量门禁中整体排除；
- **ruff baseline 更新**：只有实际修复代码或添加合法 noqa 后，才能从 `tools/quality_baselines/ruff.json` 删除对应条目；
- **剩余 ruff 债务**：N815/W293/F841/E741/B905 等其他规则由后续 PR 分批处理。

### 5.1.4 tests mypy 清零规则

`backend/tests/` 目录的 mypy 错误已清零（CHANGE-20260709-011），与 `backend/app/` 一样不得新增错误。

- **mypy app 和 mypy tests 都不得新增错误**：CI 阻断 `Mypy New Files` 检查 `app/`，`tests/` 错误不得通过 `type: ignore` / `cast` / `Any` 掩盖；
- **禁止用 Any/cast/type:ignore 掩盖测试错误**：所有 None 分支必须显式 `assert x is not None` 或 `if x is None: raise`；类型不匹配必须用 `isinstance` 或 Protocol/TypedDict/dataclass 收窄；
- **fixture/mock 必须结构化 typed**：
  - 异步工厂 fixture 返回类型使用 `AsyncFactory[T]`（`Callable[..., Coroutine[Any, Any, T]]`），不得用裸 `Callable[..., T]`；
  - mock 对象使用 `Protocol`、`dataclass`、`TypedDict`、`NamedTuple` 或真实 ORM 最小构造，不得用 `SimpleNamespace` 假装成真实 ORM；
  - `httpx.ASGITransport` 第三方存根缺口用单点 `make_asgi_transport(app)` helper 桥接，不得在每个测试里重复 cast；
- **每个测试债务 PR 必须 before/after**：PR 描述必须列出 tests mypy 修改前后错误数量（如 `tests mypy: 300 → 0`）；
- **不改变测试覆盖**：禁止为过 mypy 删除测试或降低断言强度；历史废弃测试必须证明对应功能已删除且测试不再被引用后才可移除；
- **app 类型声明阻碍测试时**：若 `app/` 类型声明过窄导致测试无法通过且 `mypy app` 仍需 0，允许收紧 app 类型（如 `require_feature` 返回 `Coroutine` 而非 `object`），但必须保证无运行时行为变化并在 PR 中说明。

## 6. 完成标准

一次变更完成必须满足：

```text
代码实现
= 当前设计文档
= 实现地图
= API 和数据契约
= 测试验证
= 部署配置
= CHANGE 记录
```

如果任一层不一致，必须登记到 `current/code-doc-alignment.md`，不能假装完成。
