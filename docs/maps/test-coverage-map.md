# Test Coverage Map

> 本文件是关键规则到测试的索引。实际测试列表以仓库为准。

## 1. 权限与订阅

| 规则 | 测试 |
|---|---|
| active/expired/no-sub/admin | `test_trend_selection_api_permissions.py`, watchlist permission tests |
| AccessContext | `test_eligible_user_service.py`, access control tests |
| Capture Token 隔离 | `test_capture_token_isolation.py`, auth tests |
| Worker 心跳 admin API（admin/non-admin/unauthenticated + status 筛选 + health_state 分类） | `test_admin_worker_heartbeats_api.py` |
| **壳层与导航拆分（阶段二）**：用户导航仅行情/趋势选股、admin 入口仅管理员可见、旧路由兼容重定向、admin 路由独立壳层、capture 路由不渲染任一壳层、默认入口 `/market`、getAccountMenuItemsForVariant variant=user/admin 菜单项 | `frontend/src/navigation/__tests__/appNavigation.test.ts`（10 用例） |
| **路由层级契约（阶段二 fixup）**：Capture 位于 ProtectedLayout 之外、/market+/screener+/stock/:symbol 经 UserAppShell+SubscriberRoute、/messages+/settings 经 UserAppShell 不经 SubscriberRoute、/admin/* 经 AdminRoute+AdminAppShell | `frontend/src/navigation/__tests__/routeStructure.test.ts`（10 用例） |
| **行情工作区 URL 状态（PR #74 表格视图重构，无 `debug` 参数）**：URL parse/serialize 往返（scope/query/page/page_size/sort/selected/industry/concept/state/event_id）、decode 默认值（scope=watchlist/query=''/page=1/pageSize=DEFAULT_PAGE_SIZE/sort/selected/industry/concept/state=null）、非法 page 回退 1、page_size 超过 100 回退 50、非法 state 回退 null、默认值省略（query=''/page=1/selected=null/industry=null/event_id=null）、buildMarketWorkspaceUrl 生成完整 URL、`selectInstrumentInTable` 设置 selected 并保留 scope/query/page/pageSize/sort/industry/concept/state + 清除 eventId、`changeMarketScope` 重置 page=1 + 清除 selected/eventId + 保留 query/sort、`changeMarketFilter` 重置 page=1 + 清除 selected、`normalizeInternalReturnTo` 白名单校验（仅允许 /screener /market /messages 前缀，拒绝 /stock/外部 URL/双斜杠/javascript/超长/admin/unknown）、event_id 解析/写入/省略 | `frontend/src/features/market-workspace/__tests__/marketWorkspaceUrlState.test.ts`（19 用例；CHANGE-20260713-009 新增 7 项覆盖 `decodeMarketListContext`/`buildStrategyResultQueryParams`；`normalizeInternalReturnTo` 长度限制 200→500） |
| **timeframe 单一真源与请求门控（阶段三最终验收）**：URL 15m→请求15m→图表15m、工具栏切换写回 URL、scope 查询互斥、右栏收起不请求、Capture 和 StockDetail 回归、selection 上下文重置、搜索结果渲染门控 | 浏览器 E2E（CDP，22 项断言全部 PASS） |
| **StockDetailPage 共享研究核心（阶段四）**：stockResearchTypes 纯函数（ALLOWED_TIMEFRAMES/DEFAULT_TIMEFRAME/DEFAULT_SOURCE/BARS_COUNT_BY_TIMEFRAME/defaultStrategyForSource/normalizeDisplayTimeframe/normalizeResearchSource） | `frontend/src/features/stock-research/__tests__/stockResearchTypes.test.ts`（13 用例） |
| **/market 和 /stock 共享研究核心（阶段四 E2E）**：market 和 stock 同 symbol/timeframe 的 bars/indicators 参数一致、stock 仅一组 instrument/bars/indicators/quote/events 请求、stock 15m/1h 请求与图表一致、详情页 UI 元素（返回/自选/上下只/全屏/备忘录/飞书/结构状态/图表/状态条/无"日线回退"文案）、capture 隔离（无 topbar/sidebar）、market 阶段三回归 | 浏览器 E2E（CDP，39 项断言全部 PASS） |
| **原型最终对齐（阶段五）**：Screener/Messages 跳转 URL 含 returnTo/event_id（`buildMarketEntryFromScreener`/`buildMarketEntryFromMessage`）、`buildStockDetailState` 携带 returnTo、`resolveBackPath` 优先 returnTo/按 source fallback | `frontend/src/pages/__tests__/detailNavigation.test.ts`（7 用例） |
| **原型最终对齐 E2E（阶段五）**：event 详情成功/404/错误、普通用户不显示 debug/管理员 debug=1 显示原始数据、右栏关闭后 event/structural/temporal 新请求为 0、market/stock 同周期请求一致无重复、指标开关/全屏/自选/memo/飞书/上下只正常、capture 隔离、1024/1440/1920 无横向溢出、旧 /overview//watchlist//stock 链接可用 | 浏览器 E2E（CDP） |

## 2. 趋势选股

| 规则 | 测试 |
|---|---|
| partial_failed 不发布 | `test_dsa_publish_validation.py`, strategy batch tests |
| computable 结果覆盖 | `test_strategy_batch.py` |
| DSA benchmark | `backend/reports/dsa_benchmark_20260702.md` |
| Node Cluster 输入 | `test_node_cluster_contract.py` |
| run 级总超时可配置 / insufficient_history skipped / run_timeout_budget_exhausted / execute_run 保留 skipped_count | `test_strategy_batch_service.py` |
| 全量 universe 主表（strategy_run_items LEFT JOIN strategy_results）+ skipped/failed 行 + metric_filter + watchlist 过滤 | `test_strategy_results_universe.py` |
| **keyword 三字段匹配（CHANGE-20260713-005）**：股票代码 ILIKE、中文名称 ILIKE、拼音首字母 ILIKE；items 与 total 条件一致 | `backend/tests/test_strategy_results_keyword.py`（3 用例） |
| **industry/concept 板块筛选（CHANGE-20260713-006）**：industry 过滤、concept 过滤、industry+concept AND 交集、不存在的板块返回空、无板块数据返回空、无筛选返回全部；覆盖 `board_filter_helper` EXISTS 条件 + `strategy_result_repository.query_run_items_with_results` industry/concept 参数 + `items`/`filtered_total` 一致性 | `backend/tests/test_strategy_results_industry_concept.py`（6 用例） |
| **数量契约四层语义（CHANGE-20260713-007）**：`source_total`（published run 原始总量，不受业务筛选影响）、`universe_total`（all/watchlist 范围总量）、`filtered_total`（keyword+industry+concept+metric_filters 后总量）、`items`（filtered_total 当前页）；`len(items) <= filtered_total`；无筛选时 `source_total == universe_total == filtered_total`；keyword/industry/concept 筛选时 `filtered_total < universe_total` 且 `source_total` 不变；`items`/`filtered_total` 必须应用完全相同条件 | `backend/tests/test_strategy_results_industry_concept.py`（新增数量语义用例） |
| adapter 处理 null id/payload（skipped/failed 行 resultId=''、payload={}） | `frontend/src/features/trend-selection/__tests__/adapter.test.ts` |
| **批量加入自选按 instrumentId 匹配+去重**：handleBatchAdd 按 `r.instrumentId` 匹配 selectedKeys（禁止 resultId）、instrumentId 去重、空选 toast 提示、成功/失败 toast 真实数量、保留 useAddToWatchlist 缓存失效 | `frontend/src/pages/__tests__/ScreenerPage.batch.test.ts`（6 用例） |
| **change_pct 独立列 + action 按钮 stopPropagation + 行内导航/自选（CHANGE-20260713-005 扩展）**：列存在、title/shortTitle、dataType=percent、sortable/filterable、width≈86、render 用 fmtChange+changePctColorClass、sortValue 读取 payload、位于 stock 列之后；action 列 onDetail/onAddToWatchlist 按钮 stopPropagation；onNavigate 链接 stopPropagation；onToggleWatchlist 模式按钮 stopPropagation + 加入/移除自选 + title="自选"；股票名称链接 `<a>`+preventDefault；renderStock 不渲染行内涨跌幅 | `frontend/src/features/trend-selection/__tests__/columns.test.ts`（13 用例） |
| **StrategyChart 用户文案契约（CHANGE-20260713-005）**：POC 峰→"核心共识价"、峰→"共识价"、POC 中心线显示"核心共识价"、tooltip POC/PEAK 文案、缺失提示"筹码共识价暂不可用"、内部字段名不变 | `frontend/src/components/__tests__/chartLabels.test.ts`（5 用例） |
| **StrategyChart Pointer Events 拖拽契约（CHANGE-20260713-005）**：Pointer Events 使用、setPointerCapture/releasePointerCapture、dragRef 字段、4px 阈值、grab/grabbing cursor、不使用旧 mouse 事件、从 startViewport 计算位移 | `frontend/src/components/__tests__/chartDrag.test.ts`（7 用例） |
| **BrandLogo 使用批准 PNG 资产（CHANGE-20260713-007）**：`BrandLogo` 引用批准 PNG 资产（`logo_symbol_128.png`/`logo_horizontal_dark.png`）、不再使用手绘 SVG、asset 路径正确、品牌资产在 `frontend/src/assets/brand/` 下 | `frontend/src/components/__tests__/brandLogo.test.ts` |
| **视觉 V1.0 token 体系（CHANGE-20260713-007）**：`variables.scss` V1.0 token 完整性（品牌 #00F6C2/#39F5CF/#00B28A；背景 #0A0F14/#111A23/#161F29；文本 #F2F6F8/#A4B0BC/#6B7785；状态 success/warning/danger/info）；global.scss focus/selection/toggle 使用 `var(--brand)`；LandingPage/BetaApplicationModal 按钮使用品牌绿渐变；无旧蓝色硬编码残留 | `frontend/src/components/__tests__/visualTokens.test.ts` |
| **StrategyChart 右侧留白契约（CHANGE-20260713-008）**：`RIGHT_PADDING_RATIO = 0.20`、`effectivePlotW = plotW * (1 - RIGHT_PADDING_RATIO)`、`step = effectivePlotW / display.length`、最右端有 20% 留白、保留 Pointer Events 拖拽/滚轮锚点缩放/双击复位/移动端双指缩放 | `frontend/src/components/__tests__/chartRightPadding.test.ts` |
| **MarketToolbar 搜索框契约（CHANGE-20260713-005）**：受控 keyword/onKeywordChange、placeholder 文案、Enter 提交、blur 提交、清空立即提交、searchable={false}、externalKeyword 受控、单一搜索状态 | `frontend/src/features/market-workspace/__tests__/marketToolbarSearch.test.ts`（8 用例） |
| **MessagesPage 数量一致性与跳转（CHANGE-20260713-005）**：useUnreadCount SSOT、total from backend、页头文案、不显示误导数字、单只股票跳转 /stock/:symbol、selection_composite 跳转 /market、AccountMenu 动态链接、AccountMenu 未读数 badge | `frontend/src/pages/__tests__/messagesCounts.test.ts`（8 用例） |
| **CHART_LAYER_MANIFEST 用户文案（CHANGE-20260713-005 扩展，CHANGE-20260715-001 新增 3 个 SMC 用例）**：sqzmom→"挤压动量"、node→"筹码共识价"、内部 ChartLayerKey 不变；smc 图层存在（name="智能资金"、kind="main"）、smc 默认关闭、ChartLayerVisibility 含 8 键 | `frontend/src/features/stock-research/__tests__/indicatorManifest.test.ts`（15 用例） |
| **表格视图配置 preset API**：权限矩阵（401/403/200/201）、CRUD、用户隔离、重名冲突 409（含 NULL strategy_key 场景）、quota 422、非法 config 422、filters/hiddenColumns/sort 深度校验、op 白名单校验、is_default 互斥、必填字段校验、user_id 注入安全、PATCH 空请求 422、迁移幂等、**跨 session 持久化（create/update/delete 真实 commit 验证）** | `backend/tests/test_table_view_presets_api.py`（50 用例） |
| **preset 保存后前端列表刷新**：成功保存后清空输入/刷新列表/失败显示后端 detail | `frontend/src/components/__tests__/tablePresetMenu.test.ts`（4 用例） |
| **sticky 表头 viewport 模式**：`StrategyDataTable` 支持 `stickyHeaderMode="viewport"`、ScreenerPage 传入 viewport、global.scss 中 `.table-wrap.viewport-sticky` overflow visible + 表头 top `var(--topbar)` z-index 18 | `frontend/src/components/__tests__/stickyHeader.test.ts`（4 用例） |
| **P0 列对齐契约（CHANGE-20260713-004）**：`reorderVisibleColumns` 纯函数（`columnOrdering.ts`）— 默认顺序/空 columnOrder/hiddenColumns 过滤/columnOrder 重排/action 列固定末尾/columnOrder 不完整/陈旧 key 忽略/组合/select 列固定末尾/空列/全隐藏（10 用例）；明显不同测试值逐列断言（2 用例）；源码契约 — thead th/tbody td/colgroup col 三者从 visibleColumns.map 派生、td 按 col.key 取值、td/th/colgroup key 使用 col.key、action 列 isAction 标记、selectable 列固定 id、colSpan 使用 visibleColumns.length、min-width 使用 visibleColumnsWidthSum（9 用例）；columnOrder 持久化 — state 存在/saveColumnOrder 持久化/onMoveUp/onMoveDown 交换/onReset 清除/currentConfig 包含/applyPresetConfig 应用（6 用例）；onRowClick/activeRowKey props（2 用例） | `frontend/src/components/__tests__/columnAlignment.test.ts`（31 用例） |
| 生产验证：run_id=f0c15e1c, source_total=5293, succeeded 行 35 个 DSA 指标正确显示，skipped 行显示股票但指标为空（JOIN 改用 `(run_id, instrument_id)` 绕过 result_id 未回填问题，ALIGN-032 CLOSED, ALIGN-033 P2） | 生产 API + DB 只读核对（CHANGE-20260704-029） |
| **Excel 导出 API 集成（CHANGE-20260713-010）**：权限矩阵（401/403/200）、published run only、all/watchlist universe、keyword/industry/concept/metric_filters 筛选、sort_by/sort_desc 排序、行数=`filtered_total`（非当前页）、列白名单（仅公共 DSA 列）、列顺序（visibleColumnKeys/columnOrder）、stock 列 `payload_key=null`（不导出操作列）、公式注入防护（`=+-@` 前缀加单引号）、MAX_EXPORT_ROWS=10000 超限 422、MIME `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`、Content-Disposition RFC 5987、X-Source-Total/X-Universe-Total/X-Filtered-Total/X-Export-Rows 四层语义响应头、SpooledTemporaryFile 内存释放不落永久文件、admin 豁免、无 N+1（`len(items) <= filtered_total`） | `backend/tests/test_excel_export_api.py`（21 用例） |
| **Excel 导出 Service 单元（CHANGE-20260713-010）**：`generate_xlsx` 使用标准库 `zipfile + XML`（OOXML），禁止 openpyxl/xlsxwriter；数值单元格为数值类型、比例字段为百分比格式；`_sanitize_formula_injection` 处理 `=+-@` 前缀；`extract_row_data` 按列定义读取 payload；`MAX_EXPORT_ROWS=10000` 常量；空结果生成有效 xlsx（仅表头） | `backend/tests/test_excel_export_service.py`（9 用例） |
| **latest_change_pct 契约（CHANGE-20260714-001）**：`/strategy-runs/{run_id}/results` 响应 `latest_change_pct`/`latest_change_trade_date` 字段（从 `bars_daily` 表用 window function 计算最新两根完成交易日涨跌幅）；正常两根有效日线（T-1/T）计算正确；盘后未完成 bar 不计入（使用 T-1/T-2）；单 bar 返回 null；null close 返回 null；prev_close=0 返回 null（避免除零）；红涨绿跌颜色逻辑（前端 contract test）；sort_by=change_pct 降序（null 排末尾，走 `CHANGE_PCT_METRIC_KEY` 特殊 sort 路径）；metric_filters change_pct > 3 筛选（走 `CHANGE_PCT_METRIC_KEY` 特殊 filter 路径）；无 N+1（window function 批量计算，SQL 查询数固定，`len(items) <= filtered_total`） | `backend/tests/test_latest_change_pct.py`（9 用例）；运行命令：`APP_ENV=test TEST_DATABASE_URL=postgresql+psycopg://... pytest backend/tests/test_latest_change_pct.py -q` |

## 3. 行情聚合

| 规则 | 测试 |
|---|---|
| DB 尾部补齐 | `test_market_data_aggregation_service.py`, `test_chart_bars_service.py` |
| bars API DB-first | `test_bars_api_db_first.py` |
| 指标服务同源 | `test_indicator_service.py` |
| bars_daily 覆盖率统一口径 | `test_bars_coverage_service.py` |
| coverage 阈值判断使用 `coverage_raw` 原始值 | `test_bars_coverage_service.py` |
| dsa-only fallback 到最新交易日 | `test_dsa_only_coverage_endpoint.py` |
| bars API page_size 按 timeframe 限制（15m=4000, 1h=1200, 其他=1000） | `test_bars.py` |
| indicators API `bars` 参数最大 4000 | `test_indicators_api.py` |
| 1d 交易时段 partial daily bar（`data_source=hybrid`、`is_partial=true`、`last_live_bar_time`） | `test_market_data_aggregation_partial_daily.py` |
| MDAS 拉取 live 1m 时 `start_time`/`end_time` 同为 `Asia/Shanghai` aware datetime，禁止 naive/aware 混用 | `test_market_data_aggregation_partial_daily.py::test_partial_daily_fetch_minute_bars_uses_aware_datetime`, `test_market_data_aggregation_partial_daily.py::test_intraday_1m_fetch_minute_bars_uses_aware_datetime` |
| `pytdx_adapter.get_minute_bars` 接收 aware `Asia/Shanghai` start/end 时，正确与 pytdx 返回的 naive `datetime` 列比较过滤 | `test_pytdx_adapter_minute_aware.py::test_get_minute_bars_aware_start_end_filters_naive_datetime`, `test_pytdx_adapter_minute_aware.py::test_get_minute_bars_naive_start_end_still_works` |
| `/quote` 时区输出 `+08:00` | `test_quote_timezone.py` |
| `/quote` 可信化字段（source/is_realtime/freshness_seconds/degraded/degraded_reason） | `test_quote_trustworthy.py` |
| K线实时契约（blocking）：交易时段 1d partial、收盘后 non-partial、`/quote` +08:00、前端 bars 状态展示、不可信 quote 不混入 K 线 | `test_market_data_aggregation_partial_daily.py`, `test_quote_timezone.py`, `test_quote_trustworthy.py`, `frontend/src/utils/__tests__/chart.test.ts` |
| 后端已返回 1d partial bar 时前端不得用 quote 覆盖；后端未返回 partial bar 时 quote 可兜底追加 | `frontend/src/utils/__tests__/chart.test.ts` |
| **BarRepository.get_recent_bars**：按 instrument/timeframe 查询最近 N 根 bar；空表/不足/边界/多 instrument 隔离/时间排序/limit 截断/字段完整 | `test_bar_repository_get_recent_bars.py`（8 个用例） |
| **个股详情市值字段（CHANGE-20260713-010）**：`/quote` 返回 `total_market_cap`/`float_market_cap`/`market_cap_as_of`/`market_cap_source`/`market_cap_degraded_reason`；价格与股本必须同一 `share_as_of`；股本缺失时 `degraded_reason="market_cap_data_unavailable"` 不伪造；quote 端点不发起第三方联网，从 `instruments.total_share`/`float_share`/`share_as_of` 读取并按当前价格计算 | `test_share_capital.py`（14 用例：migration upgrade/downgrade、字段 nullable、sync_share_capitals 成功/失败/BJ 跳过、quote 端点 market_cap 计算 + 数据缺失降级、单位校验）+ 84 项无截图 E2E 已 PASS |
| **股本每日同步（CHANGE-20260713-010）**：`instrument_share_capital_sync_service.sync_share_capitals` 通过 `pytdx.get_finance_info` 同步 SH/SZ 股本（BJ 跳过）；批次 500；`asyncio.to_thread` 包装阻塞调用；写 `share_as_of=trade_date`；幂等 upsert；只保留最新态不做历史回填；18:00 触发；失败保留旧值（不更新失败 symbol） | `test_share_capital.py`（14 用例，含失败保留旧值断言）+ 84 项无截图 E2E 已 PASS |
| **MDAS SSOT 架构守护 AST 测试（CHANGE-20260717-002）**：5 项测试扫描 `app/` 全部生产文件——(1) `test_no_business_module_imports_forbidden_from_bar_repository`：禁止业务层导入 `bar_repository.get_bars`；(2) `test_no_business_module_imports_adj_factor_directly`：禁止业务层直接导入 `apply_adj_factor*`/`_get_adj_factor_df`；(3) `test_only_mdas_imports_kline_aggregator`：仅 MDAS 可导入 `kline_aggregator`；(4) `test_no_business_module_resamples_weekly_monthly`：禁止业务层自行 resample 周/月频率（算法实现层 `strategy_assets/algorithms/` 例外，因其在已获取 bars 上计算算法内部特征 PDH/PDL）；(5) `test_mdas_is_sole_importer_of_private_queries`：禁止除 MDAS 外导入 repository 私有 `_query_*` 行情查询函数 | `backend/tests/test_market_data_ssot_architecture.py`（5 用例）；运行命令：`APP_ENV=test TEST_DATABASE_URL=postgresql+psycopg://... pytest backend/tests/test_market_data_ssot_architecture.py -q` |
| **MDAS v2 契约 + 复权唯一出口（CHANGE-20260717-002）**：`/bars` 请求参数含 `timeframe`/`adj`/`include_realtime`/`completed_only`/`start_date`/`end_date`/`limit`/`warmup_bars`/`adjustment_as_of`；响应 `BarListResponse` 携带 `market_data_contract_version`/`source_bar_hash`/`adj_factor_hash`/`adjustment_as_of`/`completed_through` 诊断字段；raw 始终不复权，qfq 只在 MDAS 出口统一应用一次；`adjustment_as_of` 缺省时取当前业务日，历史回算显式传 `trade_date` 禁止未来除权事件泄漏；同一股票/周期/截止日下 `/bars`、indicator/SMC、strategy_batch、feature_snapshot 的 OHLC 时间序列、`source_bar_hash`、`adj_factor_hash` 必须一致 | `test_market_data_aggregation_service.py`、`test_bars_api_db_first.py`、`test_bars.py`、`test_feature_snapshot_service.py` |
| **AdjustmentFactorService point-in-time + rebuild（CHANGE-20260717-002）**：`get_factor_series(session, instrument_id, as_of=date)` 返回只含 `trade_date <= as_of` 的截断因子序列；`rebuild_factor_series` 从最早受影响日期完整重建该股票日线 factor 序列并原子 upsert（禁止只更新最近 5 根）；公司行为集合或 fingerprint 变化时触发重建；rebuild 失败不得用 `1.0` 伪装成功，必须返回 degraded 状态和原因；不信任 15m/60m/1m 行内旧 `adj_factor` 列（pytdx hybrid bar 自带 `adj_factor=1.0` 错误）；公式 `qfq_price = raw_price × factor(bar_date) / factor(as_of)` | `test_bars.py`（覆盖 as_of 截断/qfq 唯一出口/不信任 bar 自带 adj_factor 列）、`test_market_data_ssot_architecture.py`（架构守护）、`backend/scripts/verify_603538_step6.py`（603538 真实 DB 核对） |
| **603538 美诺华除权真实回归（CHANGE-20260717-002）**：美诺华 603538（instrument_id=`1fea317d-7206-41e9-b371-2ef79a57ce73`，除权日 2026-07-09 价格从 39.33 跳到 28.07）作为除权真实回归样本；验证 `none/qfq × 1d/15m/1h` 逐日/逐 bar 的 raw OHLC、factor(bar_date)、factor(adjustment_as_of)、qfq OHLC、source_bar_hash、adj_factor_hash、completed_through；`adjustment_as_of=2026-07-01/2026-07-03/最新交易日` 三场景下历史回算不得读取未来公司行为；除权日附近价格连续，不得重复复权；600276 恒瑞医药作为无公司行为对照（factor 全 1.0，none 与 qfq 一致）；跨调用方一致性（`/bars`、indicator/SMC、strategy_batch、feature_snapshot 时间序列/source_bar_hash/adj_factor_hash 一致）；SMC 只验证输入一致和无回归，保留 `PINE_PARITY_PENDING` | `backend/scripts/verify_603538_step6.py`（真实 DB 核对脚本，位于 `backend/scripts/`）；详见 `docs/changes/records/CHANGE-20260717-002.md` |

## 3.5 自选股监控

| 规则 | 测试 |
|---|---|
| monitor-status `metrics` 唯一来自 `stock_feature_snapshots.summary_payload`（`_source='feature_snapshot'`），不再走 `MonitorSnapshotService` 实时计算或 `MonitorState.payload` fallback | `test_watchlist_monitor_status_snapshot.py`（14 个用例：SUCCEEDED/WAITING_SNAPSHOT/NO_SNAPSHOT + 盘中读昨日 + 非交易日读最近交易日 + 非交易日无历史 + 盘中缺上一交易日 snapshot + run gate 4 用例：running/failed/succeeded/no_run + [Blocker Fix] publish gate 严格化 3 用例：published_at=null / full scope / sample scope） |
| **[Run gate - Phase 8 新增，[Blocker Fix] 严格化]** watchlist 只读 `stock_feature_snapshot_runs.status='succeeded'` + `published_at IS NOT NULL` + `metadata_['scope']='full'` 的 snapshot；running/failed/无 run/published_at=null/scope='sample' 时不读 snapshot | `test_watchlist_monitor_status_snapshot.py`（run gate 4 + [Blocker Fix] 3 = 7 用例） |
| **[Run lifecycle - Phase 8 新增]** after_close feature_snapshot 成功写 `run.status='succeeded'` + `published_at`；失败写 `run.status='failed'` + 不 publishing | `test_after_close_orchestrator.py`（2 个用例：success_creates_succeeded_run / failure_creates_failed_run） |
| **[Run service - Phase 8 新增]** `create_snapshot_run` 幂等创建 running run；`finish_snapshot_run` succeeded 写 published_at、failed 不写；failed run 允许新 retry | `test_feature_snapshot_run_service.py`（6 个用例） |
| **[Instrument-first backfill - Phase 8 新增]** 每只股票每周期只调用一次 `load_instrument_bars`；`--symbols`/`--limit-instruments` 小样本过滤；`--resume` 跳过已存在 + succeeded run；失败比例超阈值创建 failed run（不抛异常） | `test_feature_snapshot_backfill.py`（25 个用例） |
| **[Blocker Fix - scope 区分]** `_resolve_run_scope(symbols, limit_instruments)` 决定 run scope：任一过滤启用 → `sample`，都未启用 → `full`；`backfill_instrument_first(scope=...)` 传播到 `create_snapshot_run(scope=...)` + `finish_snapshot_run(metadata={'scope': ...})`；sample run 即使 succeeded + published_at 也不被 watchlist 读取 | `test_feature_snapshot_backfill.py`（[Blocker Fix] scope 4 用例） |
| **[multiprocessing - CHANGE-049]** `--workers N`（N>1）启用并行：`_worker_process_instruments` per-date commit + resume 跳过 + load 失败不阻塞；`backfill_instrument_first_parallel` 创建/finalize run + scope 传播 + 高失败率标 failed + 空输入返回 | `test_feature_snapshot_backfill.py`（9 个用例：parse_args_workers_default_is_1/custom + worker_per_date_commit + worker_resume_skips_existing + worker_single_failure_doesnt_block + parallel_empty_inputs/creates_run/high_failure/scope） |
| **[multiprocessing Blocker Fix - CHANGE-049 v2]** worker future 异常计入 failed（chunk × trade_dates）；commit 失败不计 success（rollback + failed++）；upsert 异常后 rollback 后续 date 仍可继续；pool_size=1/max_overflow=0/pool_pre_ping=True；`--workers < 1` SystemExit；`--workers > cpu_count` warning + cap | `test_feature_snapshot_backfill.py`（8 个用例：test_backfill_parallel_worker_exception_counts_as_failed + test_worker_commit_failure_doesnt_count_as_success + test_worker_upsert_exception_rollback_continues + test_worker_pool_config_size_1_overflow_0 + test_parse_args_workers_zero_rejected + test_parse_args_workers_negative_rejected + test_parse_args_workers_cap_to_cpu_count + test_worker_per_date_commit_all_succeed） |
| `calculation_status` 三态语义：SUCCEEDED（snapshot 存在）/ WAITING_SNAPSHOT（交易日已收盘但 snapshot 缺失，仅 MARKET_CLOSED）/ NO_SNAPSHOT（盘中无昨日 / 非交易日无历史 / 无法解析交易日） | `test_watchlist_monitor_status_snapshot.py` |
| `_resolve_expected_snapshot_trade_date` 规则：交易日未收盘 → 上一交易日；交易日已收盘 → today；非交易日 → 最近交易日；无法解析 → None（复用 `calendar_service`，禁止硬编码周末） | `test_watchlist_monitor_status_snapshot.py` |
| `freshness_seconds` 基于 `snapshot.updated_at` | `test_watchlist_monitor_status_snapshot.py::test_succeeded` |
| 自选监控页无每行状态栏、页眉全局状态、数据列可过滤、compact-table 对齐 | `frontend/src/features/watchlist-monitor/__tests__/columns.test.ts` |
| admin monitor 资格：active admin 与 active member + 有效 subscription 放行，disabled admin / 无订阅普通用户排除 | `test_monitor_eligible.py` |
| monitor_batch / event_recipient / outbox_relay / delivery_worker 监控资格口径一致 | `test_outbox_relay_monitor_eligibility_consistency.py`, `test_delivery_worker_monitor_eligible.py` |
| monitor_batch 使用 live 1m 输入（`include_realtime=True`）并剔除最后一根未完成 bar | `test_monitor_batch_live_minute.py` |
| monitor_batch 调用 MDAS 1m 时必须带 `include_realtime=True` | `test_monitor_batch_live_minute.py::test_monitor_cycle_1m_uses_include_realtime` |
| monitor_batch 计算输入 daily/15m `include_realtime=False`（不被截图实时性污染），1m `include_realtime=True` 且剔除最后未完成 bar，`source_bar_time` 来自最新已完成 1m | `test_monitor_batch_live_minute.py::test_monitor_calc_inputs_daily_15m_non_realtime` |
| 飞书业务 payload（手动分享 `stock_detail_feishu_service` + 自动盘中监控截图 `_send_chart_images_via_outbox`）`timeframe=1d`，保留 `capture_run_id`/`source_bar_time`/`disable_cache` | `test_stock_detail_feishu.py::TestStockDetailFeishuCapturePayload`、`test_monitor_batch_capture_image.py::TestMonitorBatchCaptureTimeframe` |

### 3.5.1 Feature Snapshot 持久化

| 规则 | 测试 |
|---|---|
| `build_summary_payload` 必须返回所有前端列表必需字段（`poc_price`/`nearest_node_above`/`nearest_node_below`/`distance_to_node_*_atr`/`node_interval_position_0_1`/`cost_position_zone`/`value_area_zone`/`daily/m15_developing_swing_*`/`m15_position_relative_to_daily`/`_source='feature_snapshot'`/`as_of`/`source_bar_time`） | `test_feature_snapshot_service.py::test_build_summary_payload_returns_required_fields` |
| `build_summary_payload` 缺字段时填 `None`，不抛异常 | `test_feature_snapshot_service.py::test_build_summary_payload_handles_missing_fields` |
| `_truncate_bars_to_trade_date` 按 `index.date <= trade_date` 截断（point-in-time）；`None` 输入返回 `None`；15m 截断到当日 | `test_feature_snapshot_service.py::test_truncate_*` |
| `compute_feature_snapshot_for_date` 必须使用 `<= trade_date` 数据 | `test_feature_snapshot_service.py::test_compute_snapshot_point_in_time_no_future_data` |
| 数据不足时写 `degraded_reasons` 不抛异常 | `test_feature_snapshot_service.py::test_compute_snapshot_degraded_on_insufficient_data` |
| `source_primary_bar_time` 与 `source_secondary_bar_time` 必须为 `Asia/Shanghai` aware datetime；1d 规范化为 `trade_date 15:00+08:00` | `test_feature_snapshot_service.py::test_compute_snapshot_source_bar_time_timezone_aware` |
| `upsert_snapshot` 幂等：同唯一键重复 upsert 只生成一行，第二次覆盖第一次 | `test_feature_snapshot_service.py::test_upsert_snapshot_idempotent` |
| **[PR #74 snapshot ownership]** `upsert_snapshot` ON CONFLICT DO UPDATE 更新 `source_run_id`；`GET /stocks/{symbol}/context` 按 `source_run_id == run.id` 精确查询返回正确 snapshot；`source_run_id` 缺失时不返回半成品 | `test_feature_snapshot_ownership.py`（集成测试） |
| **[P0-4 published snapshot 保护]** `create_snapshot_run(scope='full', allow_republish=False)` 在已存在 published full run 时抛 `PublishedSnapshotRunExistsError`；`allow_republish=True` 绕过检查；`scope='sample'` 不受限 | `test_feature_snapshot_service.py::test_p0_4_create_snapshot_run_blocks_when_published_full_exists` + `test_p0_4_create_snapshot_run_allow_republish_bypasses_check` + `test_p0_4_create_snapshot_run_sample_scope_not_blocked` |
| **[P0-4 upsert WHERE 保护]** `upsert_snapshot(allow_republish=False)` ON CONFLICT DO UPDATE 时 WHERE 子句保护已归属 published run 的 snapshot 不被覆盖；`allow_republish=True` 覆盖 | `test_feature_snapshot_service.py::test_p0_4_upsert_snapshot_protects_published_run_ownership` + `test_p0_4_upsert_snapshot_allow_republish_overwrites` |
| **[PR #74 idempotency key]** `strategy_events.idempotency_key` 格式 `symbol:source_run_id:algorithm_version`；每只股票每个 run 至多一个事件；旧格式 `symbol:trade_date:algorithm_version:hash(evidence)` 不再生成 | `test_strategy_events_idempotency.py` |
| `compute_for_trade_date` 单股失败不阻断其他股票；失败比例超 30% 抛 `RuntimeError` | `test_feature_snapshot_service.py::test_compute_for_trade_date_single_failure_does_not_block` |
| [half-baked rollback] `compute_for_trade_date` 不内部 commit；超阈值抛 `RuntimeError` 后 caller rollback，DB 无半成品残留 | `test_feature_snapshot_service.py::test_compute_for_trade_date_over_threshold_no_partial_after_rollback` |
| `structural_payload` 必须包含 `primary`/`secondary`/`relation`/`meta` 4 key；`relation` 来自 `_compute_relation` | `test_feature_snapshot_service.py::test_compute_snapshot_structural_payload_contains_relation` |
| `parse_args` 默认值（end=latest/batch_size=20/failure_threshold=0.3）+ 自定义值 + 缺失 `--start` 报 `SystemExit`；**`commit_every` 已移除**；**`--allow-republish` 默认 False** | `test_feature_snapshot_backfill.py::test_parse_args_*` |
| `get_trade_dates_from_bars` 升序 trade_dates + 空表返回空列表 | `test_feature_snapshot_backfill.py::test_get_trade_dates_from_bars*` |
| `get_latest_bar_date` 返回 `bars_daily.trade_date` 最大值；空表返回 `None` | `test_feature_snapshot_backfill.py::test_get_latest_bar_date*` |
| `get_existing_instrument_ids` 返回某日已存在 snapshot 的 instrument_id 集合；按完整唯一键过滤；按 schema_version 严格过滤 | `test_feature_snapshot_backfill.py::test_get_existing_instrument_ids_*` |
| `backfill_single_date` `--dry-run` 输出 missing 数量不写库；正常模式调用 compute；`--resume` 真正跳过已存在 instrument（不重新计算）；全部已存在时跳过 compute | `test_feature_snapshot_backfill.py::test_backfill_single_date_*` |
| `main` `--dry-run` 端到端不写库；单日失败 rollback 半成品不阻断其他日期（验证 rollback/commit 计数）；`--end=latest` 解析；`start > end` 直接 `sys.exit(1)` | `test_feature_snapshot_backfill.py::test_main_*` |
| 盘后编排状态机新增 `feature_snapshot` 步骤（`quality_gate → feature_snapshot → publishing`） | `test_after_close_orchestrator.py`（9 个用例） |
| [feature_snapshot 失败不进入 publishing] `compute_for_trade_date` 抛 `RuntimeError` → `publish_run` 未被调用 + `job_run.status='failed'` + 不应有 publishing/succeeded 事件 | `test_after_close_orchestrator.py::test_execute_feature_snapshot_failure_skips_publishing` |
| **[PR #74 发布原子性]** snapshot 计算完成后不立即写 `succeeded`/`published_at`；只有 DSA `publish_run` 成功后才 `finish_snapshot_run(status='succeeded')` 写 `published_at` 并生成事件；`publish_run` 失败时 snapshot run=`failed`、`published_at=null`、不生成事件 | `test_after_close_orchestrator_atomicity.py`（发布原子性测试） |
| 断点恢复：`last_completed_step='quality_gate'` → `skip_snapshot=False`；`'feature_snapshot'` → `skip_snapshot=True` | `test_after_close_orchestrator.py` |
| 盘后流水线聚合 API（11 场景）：盘前 not_started/收盘后 blocked/latest 不回退历史/运行中/成功/watchlist_ready 判定/sample 不计入/full 优先展示/失败带 error/POST 幂等/events 限 100 条/非 admin 403 | `test_admin_after_close_pipeline.py`（11 个用例） |
| 迁移幂等：`alembic upgrade head` / `downgrade -1` / `upgrade head` 链路不报错；表含唯一约束与 3 个 btree 索引 | 手动验证（test DB） |

## 3.6 个股上下文 API（stock_context）

| 规则 | 测试 |
|---|---|
| **[PR #74 Evidence DTO 映射]** `_event_to_dto` 将 ORM `event.evidence` 映射为用户面 Evidence DTO；不再从 `event.payload` 拼装证据字段 | `test_stock_context_api.py::test_event_to_dto_evidence_mapping` |
| **[PR #74 时区]** 时间字段统一使用 `ZoneInfo("Asia/Shanghai")`（不再使用 UTC）；事件时间/发布时间/完成时间均以 CST 返回 | `test_stock_context_api.py::test_timezone_asia_shanghai` |
| **[PR #74 历史事件截止]** 历史事件 cutoff 使用次日 00:00 exclusive（`trade_date + 1 day, 00:00:00`，不包含该时刻）；不再使用 `max.time + 1 day - 1 second` 口径 | `test_stock_context_api.py::test_historical_event_cutoff_next_day_exclusive` |
| **[PR #74 Run 查询排序]** Run 查询使用确定性 DESC 排序 `ORDER BY trade_date DESC, published_at DESC, finished_at DESC`；相同 trade_date 下多 run 顺序确定 | `test_stock_context_api.py::test_run_query_deterministic_desc_ordering` |
| **[PR #74 阶段二 reasonCode 机制]** StockContext API 返回 `dataQuality.reasonCode` 解释空态原因：`no_published_full_run`（无 succeeded+published+full run）/ `snapshot_missing`（run 有但 snapshot 缺失）/ `snapshot_run_not_linked`（legacy snapshot source_run_id=NULL）/ `legacy_snapshot_ambiguous`（legacy snapshot 多 run 候选）/ `null`（正常或 snapshot 非 None）；`effective_reason = None if snapshot is not None else reason_code`（reasonCode 只在 state=null 时显示）；`dataQuality` 含 `runTradeDate`/`runPublishedAt`/`hasSucceededRun`/`hasSnapshot`/`degradedReasons` | `test_stock_state_and_events.py::test_p02_context_no_published_full_run` + `test_p02_context_snapshot_missing` + `test_p02_context_exact_source_run_id` + `test_p02_context_snapshot_run_not_linked` + `test_p02_context_legacy_snapshot_ambiguous` + `test_p02_context_normal_returns_state` |
| **[PR #74 阶段二 GET context 只读]** GET `/api/v1/stocks/{symbol}/context` 不得产生任何写副作用（不创建 snapshot/run）；连续 3 次调用后 snapshot/run 行数不变 | `test_stock_state_and_events.py::test_p02_context_get_no_write_side_effect` |
| **[PR #74 阶段二 快照归属修复工具]** `tools/repair_snapshot_run_ownership.py` 默认 dry-run 不写库；`--apply` 模式按 (trade_date, schema_version, primary/secondary timeframe, adj) 匹配 canonical succeeded+published+full run，唯一候选→repairable，0 候选→orphan，>1 候选→ambiguous；写模式单事务、失败 rollback、幂等（WHERE source_run_id IS NULL） | `test_repair_snapshot_run_ownership.py::test_repair_dry_run_does_not_write` + `test_repair_apply_writes_and_idempotent` |
| **[PR #74 阶段二 状态观察面板 reasonCode 文案]** `getReasonCodeMessage(reasonCode, runTradeDate)` 纯函数返回 `{title, meta?}`；覆盖 5 种 reasonCode + null + 未知 code；`snapshot_missing` 含/不含 runTradeDate；`snapshot_run_not_linked` 含"待修复归属"；所有已知 code 非默认文案；面板现由 `AtomicFactsPanel` 复用（原 `EventStatePanel` 已删除） | `frontend/src/features/research-context/__tests__/reasonCodeMessages.test.ts`（8 个子测试） |
| **[AFC V1 双合同分离]** 冻结研究合同 `atomic_fact_contract_v1.json`（V4.13 原字段：id/dimension/level/source_paths/formula/raw_type/null_policy/display_order/display_template/prohibited_interpretations/legacy_aliases/default_ui_enabled + 部分项 unit/classification_rule/thresholds_ref/ui_rule，**不含 public_key/public_label 等产品层语义**）；产品展示合同 `atomic_fact_presentation_v1.json` 按 Fact ID 映射 publicKey/publicLabel/visualKind/valuePrecision/groupTitle/secondaryLabel（**恰好 14 Core + 8 Aux，排除 T3/T6/V1**）；生产服务同时读取两份合同（frozen 决定事实/顺序/公式/阈值/路径，presentation 决定产品文案与 UI 类型）；S2 存在；T3/T6 `ui_enabled=false`/feature_flag=false 不进用户 payload；V3/T5 阈值未确认（THR-001）→ 仅显示比值 +「分类未启用」；S3 严格 0.33/0.67（0.63→MIDDLE）；S7/S8 禁止负距离；近期变化 recentChanges 不属于 V4.13 Core Fact；V1 永不进 payload；单公式 fallback（新快照与旧 summary 共用 `compute_atomic_facts`） | `backend/tests/test_atomic_fact_contract_service.py`（25 纯函数）+ `backend/tests/test_atomic_fact_contracts.py`（5 双合同结构：frozen 无产品字段/presentation 14+8 排除 T3/T6/V1/ID 一一对应/V1 无映射）+ `frontend/src/features/research-context/__tests__/atomic-facts.test.ts`（8 契约：Registry 14/10/1、presentation 14+8、frozen 无产品字段、用户面板源码无内部术语）+ `frontend/src/features/market-workspace/__tests__/change010Contract.test.ts`（回归断言 AtomicFactsPanel） |
| **[AFC V1 用户 DTO 不泄露]** `PublicAtomicFactItem`（**无 factId/sourcePath/formula/thresholdRef**）+ `AdminAtomicFactDebugItem`（保留 factId/publicKey/sourcePath/rawValue/thresholdRef/thresholdEnabled/featureFlag/missing）；缺失事实由 `compute_atomic_facts` 从 Core 数组**直接省略**（分母固定 14，`availability.coreMissing` 用 publicKey）；M3 阈值未确认（不声称 1e-6，仅 raw>0→增加/raw<0→减少/raw==0→基本不变，thresholdEnabled=false）；M5 任一输入缺失即省略、双 true→dataQuality 异常；S1 未知枚举省略；S3 越界省略；S7/S8 管理员 sourcePath 随趋势方向动态变化；recentChanges 按各事实**展示精度**比较（T2/M2 4 位、M3 6 位、S3/S7/S8/T5/V3 2 位）返回 fromText/toText/deltaText | `backend/tests/test_stock_context_atomic_facts.py`（14 集成测试：字段不泄露/缺失省略分母14/M3未确认无1e-6/M5双true异常/S1未知省略/S3越界省略/S7S8动态sourcePath/summary优先+fallback/persisted与fallback一致/as_of SQL LIMIT前过滤/精度过滤/GET零写入/admin完整追溯/普通用户403） |
| **[AFC V1 persisted-first 上下文 API]** `GET /api/v1/stocks/{symbol}/context`（require_active_subscription，admin 豁免）优先读取已持久化 `summary_payload.atomic_fact_contract_v1`（校验 contractVersion + publicKey 结构后直接返回），缺失/版本不符/结构不匹配 → 同一纯函数 fallback（**不回写旧快照**）；`GET /api/v1/admin/stocks/{symbol}/debug`（require_admin）额外返回 `rawDebug` + `atomicFactsDebug`（每事实 factId/publicKey/sourcePath/rawValue/thresholdRef/thresholdEnabled/featureFlag/missing 可追溯）；as_of 历史查询在 SQL 层 `trade_date <= as_of` 过滤后再 `DESC LIMIT`；GET 零写入、不覆盖已发布 run | 同上 `test_stock_context_atomic_facts.py`（14 集成测试） |

## 3.8 研究特征矩阵因果口径与 DB 写入

| 规则 | 测试 |
|---|---|
| `FeatureSpec` 必填 `namespace`/`source`/`compute_policy`，缺一抛 `ValueError` | `test_feature_causality_registry.py`（3 个用例：empty namespace/source/compute_policy） |
| `key` 必须以 `{namespace}.` 开头，不匹配抛 `ValueError` | `test_feature_causality_registry.py`（2 个用例：key 前缀匹配/不匹配） |
| `FeatureSpec.db_column` 把 dotted key 映射为下划线列名（`causal.atr` → `causal_atr`） | `test_feature_causality_registry.py`（db_column 映射） |
| `hindsight.*` 的 `allowed_for_backtest` 必须 `False` | `test_feature_causality_registry.py`（hindsight namespace backtest=False） |
| `label.*` 的 `allowed_for_backtest` 必须 `False` | `test_feature_causality_registry.py`（label namespace backtest=False） |
| `causal.*` 的 `allowed_for_backtest` 必须 `True` | `test_feature_causality_registry.py`（causal namespace backtest=True） |
| `confirmed_delay.*` 的 `allowed_for_backtest` 必须 `True` | `test_feature_causality_registry.py`（confirmed_delay namespace backtest=True） |
| DSA 必须同时存在 `causal.dsa_confirmed_*` 与 `hindsight.dsa_finalized_*` 两类 | `test_feature_causality_registry.py`（3 个用例：causal.dsa_confirmed_* 存在 + hindsight.dsa_finalized_* 存在 + 双轨并存 + 各自 compute_policy 正确） |
| Node Cluster 只能是 `hindsight.node_cluster_*`，不得出现在 causal | `test_feature_causality_registry.py`（2 个用例：hindsight.node_cluster_* 存在 + causal 中无 node_cluster） |
| `confirmed_swing_*` 必须是 `confirmed_delay`，不得作为 hindsight 默认回填 | `test_feature_causality_registry.py`（2 个用例：confirmed_delay.confirmed_swing_* 存在 + hindsight 无 confirmed_swing） |
| `FeatureCausalityRegistry.register` 重复 key 抛 `ValueError`；`get`/`all`/`by_namespace`/`keys`/`db_columns` 基础操作 | `test_feature_causality_registry.py`（Registry CRUD + db_columns） |
| `build_default_registry()` 返回 33 个字段（causal 16 + confirmed_delay 4 + hindsight 6 + label 7）；包含关键 causal/label 字段 | `test_feature_causality_registry.py`（默认 registry 完整性 + 关键字段存在） |
| 磁盘阈值边界 `15 * (1024**3)` 字节（< 15GB 停止，= 15GB 通过，> 15GB 通过） | `test_research_matrix_writer.py::TestDiskThreshold`（3 个用例，mock `shutil.disk_usage`，用 1024^3 而非 10^9） |
| 单月大小阈值边界 3.0GB（> 3GB 停止，= 3GB 通过，< 3GB 通过，0 通过） | `test_research_matrix_writer.py::TestMonthSizeThreshold`（4 个用例） |
| 失败率阈值边界 5%（6% 停止，5% 通过，3% 通过，total=0 通过） | `test_research_matrix_writer.py::TestFailureRateThreshold`（4 个用例） |
| 月份解析（1月/2月非闰/2月闰年/12月/非法格式抛 `ValueError`） | `test_research_matrix_writer.py::TestResolveMonthRange`（5 个用例，`calendar.monthrange` 处理闰年） |
| 单月 DB 占用估算（rows × 2KB / 1024³） | `test_research_matrix_writer.py::TestEstimateMonthSize`（3 个用例：小样本/全月/零） |
| `create_or_resume_run` 首次创建返回 `running`；相同 `run_key` 第二次返回已存在 run；不同 scope → 不同 `run_key` | `test_research_matrix_writer.py::TestRunLifecycle`（3 个用例，async DB savepoint） |
| `finalize_run(succeeded/failed)` 更新 status/统计/duration/finished_at | `test_research_matrix_writer.py::TestRunLifecycle`（2 个用例，async DB） |
| `upsert_rows_batch` 首次 upsert 写入新行；相同 `(instrument_id, trade_date)` → `ON CONFLICT DO UPDATE` 覆盖旧值；空 list 返回 0；1050 行分批（UPSERT_BATCH_SIZE=1000） | `test_research_matrix_writer.py::TestUpsertRowsBatch`（4 个用例，async DB） |
| `ResearchFeatureMatrixRun` 16 列结构 + `run_key` 唯一约束 + month/status 索引；`ResearchFeatureMatrixRow` 39 列（5 metadata + 33 feature + 1 created_at）+ `(instrument_id, trade_date)` 唯一约束 + 3 btree 索引 | `test_research_feature_matrix_model.py`（model 自测入口，无 DB） |
| `compute_all_features(bars)` 返回 DataFrame 含 33 个 feature 列；per-bar 计算 vs single-snapshot 区分；causal rolling/DSA 双轨/confirmed_delay swing/label 字段；空输入不抛异常 | `test_feature_computer.py`（~23 个用例） |

## 3.9 wencai 板块同步（pywencai 唯一数据源，CHANGE-20260716-007 + PR #77 收口）

| 规则 | 测试 |
|---|---|
| **WencaiBoardProvider adapter**：pywencai 查询 `同花顺概念，行业分类` / `asyncio.to_thread` 包装同步调用 / Referer 头 / 3 次重试 / 超时 / 解析 / 异常隔离 / 空响应 / cookie 失效 / provider 不可用降级 | `test_wencai_board_provider.py`（53 个用例） |
| **BoardSnapshot 完整性门禁（绝对门禁）**：空板块目录拒绝 / 空成分关系拒绝 / 原始记录 <5000 拒绝 / 代码唯一性 <99.9% 拒绝 / 行业板块 <200 拒绝 / 概念板块 <300 拒绝 / 关系数 <60000 拒绝 / 解析率 <95% 拒绝 / 全部门禁通过 | `test_board_sync.py`（重写，17 个用例覆盖 BoardSnapshot + 新门禁阈值 + source=wencai） |
| **BoardSnapshot 完整性门禁（相对门禁）**：异常降幅 >20% 拒绝 / 正常降幅 ≤20% 通过 / 首次同步不做降幅检查（prev=0） | `test_board_sync.py`（重写，17 个用例） |
| **原子切换 + 事务回滚**：成功同步后数据写入 + 计数一致；校验失败时保持旧数据不删除（`stale=true`）；异常时不修改现有数据 | `test_board_sync.py`（重写，17 个用例，async DB） |
| **Migration 循环**：062 migration `upgrade → downgrade → upgrade` 循环不报错；表 `market_boards`/`market_board_memberships` 存在 | `test_board_sync.py`（重写，17 个用例） |
| **source=wencai**（PR #77）：`sync_boards()` 成功返回 dict 显式带 `source: "wencai"`，防止手工 `record_sync_status(result)` 丢失 source | `test_board_sync.py`（17 个用例） |
| **after_close_orchestrator `syncing_boards` 步骤**：软失败不阻断 DSA/snapshot/publish / 非交易日跳过 / `mode=dsa_only` 跳过 / `BOARD_SYNC_ENABLED=false` 跳过（`reason_code=board_sync_disabled`）/ 成功后 `last_completed_step` 推进 / 失败后仍推进（视为已尝试）/ `degraded_reasons` 记录失败原因 | `test_after_close_board_sync.py`（10 个用例） |
| **BOARD_SYNC_ENABLED 开关**：`false` 时 `syncing_boards` 步骤跳过执行记录 `status=skipped` + `reason_code=board_sync_disabled`，不发任何 pywencai 请求；`true` 时正常执行 | `test_after_close_board_sync.py`（10 个用例） |
| **BOARD_SYNC_ENABLED 环境变量解析**（PR #77）：`config.py` `_resolve_board_sync_enabled()` 优先级「环境变量 > CONFIG_FILE > 默认 False」；接受 `1`/`true`/`yes`/`on`（大小写不敏感）；空值回退 CONFIG_FILE | `test_board_sync_enabled_config.py`（12 个用例，PR #77 新增） |
| **行业关键词 ilike 筛选**（PR #77）：`board_filter_helper.build_board_filter_conditions` industry 改 `MarketBoard.name.ilike('%keyword%', escape='\\')`，匹配完整路径任意一级；`_normalize_keyword` NFKC + trim；`_escape_ilike_pattern` 转义 `\`/`%`/`_`；一级/二级/三级/局部关键词匹配；空值不生成条件；industry+concept AND；concept 精确匹配 | `test_board_filter_helper.py`（23 个用例，PR #77 新增） |
| **/market/boards API**：`available`/`reason_code`/`source`/`stale`/`last_attempt_status` 字段；无数据时 `available=false` + `reason_code=board_provider_unavailable`；有数据时 `available=true`；存在旧数据但最新同步失败时 `stale=true` | `test_market_stocks.py`（现有用例 + 新字段断言） |
| **前端 wencaiBoardSyncContract**（PR #77 重写，28 个用例）：`stale=true` 时展示"沿用上次板块数据"提示 / `source` 字段渲染 / `last_attempt_status` 字段渲染 / `boards.available=false` 时输入禁用 / 行业值 `-` 渲染为 `/`（API 值不变） / **BoardFilterCombobox**：`mode="industry"` 允许任意关键词 / `mode="concept"` 精确匹配 / 本地过滤完整路径最多 12 条建议 / ArrowUp/Down/Enter/Escape 键盘导航 / 点击外部关闭 / 清除按钮 / aria-combobox/listbox/option / 150ms blur 延迟 / 高亮命中 / 不逐字符请求后端 | `frontend/src/features/market-workspace/__tests__/wencaiBoardSyncContract.test.ts`（28 个用例） |

## 4. 飞书与通知

| 规则 | 测试 |
|---|---|
| Platform App only | `test_feishu_platform_app_only.py` |
| target_channel_id | `test_outbox_target_channel_id.py` |
| 状态机 | `test_state_machine.py`, `test_stock_detail_feishu_status.py` |
| beta admin 通知 | `test_beta_application_notifier.py` |
| `_notify_monitor_status` 直接发送路径 | **无测试**（缺口，ALIGN-025） |
| 飞书消息时间显示中国时区 | `test_feishu_timezone_format.py` |
| **[Monitor 图片 Capture Token]** `monitor_batch_service._send_chart_images_via_outbox()` 生成的 capture token 含 `type=capture`、`scope=stock_detail_capture`、`user_id`、`instrument_id`、`event_id`，且 `instrument_id` 与触发股票一致；capture 成功写 `capture_jobs=SUCCEEDED` 并生成 `delivery_type=image` / `image_url` / `message_group_id` Outbox；capture 401/403/无 image_url 写 `capture_jobs=FAILED` 且不阻塞文字通知 | `test_monitor_batch_capture_image.py`（5 个用例） |
| **[Notification latest-event Capture Token]** `notification_service.test_channel_latest_event()` 生成的 capture token 含完整 claims，`instrument_id` 与事件标的一致 | `test_notification_latest_event_capture.py`（2 个用例） |
| **[Image Outbox / MessageDelivery]** monitor 图片链路通过 `message_group_id` 与文字通知关联；`delivery_type=image` 的 MessageDelivery 由 outbox_relay 生成 | 集成于 `test_monitor_batch_capture_image.py` + `test_outbox_relay_monitor_eligibility_consistency.py` |
| **[三类 IndicatorView 独立飞书图片（CHANGE-20260720-001 §三）]** `IndicatorView = Literal["node_cluster", "bollinger", "smc"]` 共享枚举贯穿 `StrategyEvent.payload` → `NotificationMessage.resource_refs` → Capture 请求 → `CaptureJob` → 输出文件名 → 缓存键 → 幂等键 → 状态查询 → 前端 URL 参数；三套 `FEISHU_CAPTURE_PRESETS`（`node_cluster`/`bollinger`/`smc`）layers 互斥（除共享 candlestick）；`build_monitor_event_text` 按 IndicatorView 拆分文字卡片；`CaptureJob.indicator_view` 字段透传链路完整 | `backend/tests/test_monitor_indicator_view.py` + `backend/tests/test_feishu_capture_presets.py` + `backend/tests/test_stock_detail_feishu_indicator_view.py` |
| **[SMC 监控（CHANGE-20260720-001 §二）]** `SmcMonitor` 主输入已完成前复权日线，1m 仅触发检测；调用 `canonical_adapters.compute_smc_adapter`；FVG 完全排除；3 个事件 `smc_bos_retest`/`smc_choch_retest`/`smc_order_block_first_touch`；稳定 `smc_entity_id` + 去重维度含 `instrument/event_type/entity/touch_episode`；`WatchlistMonitor` 三合一（BB+VN+SMC）；`MonitorState` 命名空间 `bb`/`node_cluster`/`smc`/`market`；单子监控失败只标记该项 degraded 不阻塞；`EVENT_LABELS` 5→8 项 | `backend/tests/test_smc_monitor.py` + `backend/tests/test_watchlist_monitor_three_in_one.py` + `backend/tests/test_monitor_state_namespaces.py` |
| **[Canonical 四链 re-export 接入 + AST 硬门禁（CHANGE-20260720-001 §五）]** `canonical_adapters.py` re-export 12 个算法族 adapter；四链模块（indicator_service 5 个 / feature_snapshot_service 4 个 / monitor_batch_service 1 个 / stock_capture_service 0 个）通过 `canonical_adapters` re-export 调用底层 kernel；AST 硬门禁 `test_four_chain_no_direct_kernel_import` 从 `xfail(strict=True)` 升级为硬失败；`canonical_adapters` 作为 SSOT 入口可自由 import kernel；`compute_macd_adapter` 延迟 import 规避循环依赖；`tests/allowlist.json` 移除 issue #83 条目 | `backend/tests/test_four_chain_canonical_architecture.py`（AST 硬门禁）+ `backend/tests/test_canonical_adapters_reexport.py`（re-export 完整性） |

## 5. 前端

| 规则 | 测试 |
|---|---|
| Capture 页面契约 | frontend contract capture tests |
| TypeScript/lint/build | CI blocking jobs |
| K 线合并实时行情（1d 保留日期、intraday 使用 update_time、跨日追加） | `frontend/src/utils/__tests__/chart.test.ts` |
| SQZMOM_LB 后端算法 Pine 等价 | `backend/tests/test_sqzmom_lb.py` |
| SQZMOM_LB indicator service 注入 | `backend/tests/test_indicator_service.py` |
| SQZMOM_LB 前端图层开关/副图/渲染契约 | `frontend/scripts/contract-tests/sqzmom-layer.test.ts` |
| **SMC 后端算法（CHANGE-20260715-001 → CHANGE-20260715-002 Pine parity → CHANGE-20260716-001 crossover 修正 → CHANGE-20260717-001 最终收口）**：`smc_pine_core.compute_smc_pine` 为唯一 Pine 语义核心（生产+测试共用），`smc_indicator.py` 为薄包装委托层；Pine 原语 `pine_rma`（Wilder RMA）/`pine_atr`/`pine_cumulative_mean_range`（bar0=NaN）/`pine_highest/lowest`/`pine_crossover/crossunder`；`_SMCPineState` 状态机按 Pine 执行顺序；events 使用 `internal: bool` 替代旧 `kind` 字段；anchor/second_pivot/confirmed 三时间点因果契约；**FVG 完全排除**（输出不含任何 FVG 字段）；用户 Pine 代码（`ref/smc_user_source.pine`，SHA256 0bd3d2ad，843 行）为原创作品并授权盘迹使用（导出能力见 `ref/smc_user_export.pine`） | `backend/tests/test_smc_indicator.py`（34 用例 + `TestPineSemantics` 8 用例 + `TestPineGoldenFixture` skip） |
| **SMC 服务层按需启用 + warmup（CHANGE-20260715-001/002 → CHANGE-20260717-001 warmup/历史分离）**：`compute_all_indicators(include_smc=False)` 默认不计算 SMC；`compute_all_indicators(include_smc=True)` 计算 SMC 且响应含 `data.smc`；`include_smc` 不影响 DSA/Node/BB/MACD/SQZMOM 计算结果；**warmup/历史分离（CHANGE-20260717-001）**：1d 使用 `full_daily_bars`（≥500 warmup）；**15m 独立查询 `bars+_SMC_WARMUP_BARS`（5000 计算/4000 展示，adapter 裁剪展示窗口）**；1mo 扩展回看 7000 天确保 ≥200 根（ATR200 可初始化）；1h/1w 复用 `macd_bars`；SMC 输出不调用 `_truncate_lists` 截断（time 数组保持完整长度对齐 anchor/confirmed 索引） | `backend/tests/test_indicator_service.py`（新增 3 个 SMC 服务层测试） |
| **MiniKline viewport 彻底重写（CHANGE-20260715-001 → CHANGE-20260715-002）**：纯函数 `computeMiniKlineViewport`（`frontend/src/features/market-workspace/miniKlineViewport.ts`）目标根数按周期固定（15m=48、60m=44、日=40、周=36、月=30）；`barSpacing = clamp(contentWidth/visibleBars, 5.5, 8)`；左侧 1-2 根留白 `from=max(-2, n-visible-1)`；右侧 3 根留白 `to=n-1+3`；不调用 `fitContent`/`resetTimeScale`/`scrollToRealTime`；`autoscaleInfoProvider` 扩展价格范围（上 12% 下 15%）；`rightPriceScale` `autoScale=true` + `scaleMargins {0.08,0.08}` + `minimumWidth=56` | `frontend/src/features/market-workspace/__tests__/miniKlineViewport.test.ts`（15 用例） |
| 结构状态因子 ATR SSOT Pine RMA 等价 | `backend/tests/test_atr_utils.py` |
| 结构状态因子 5 组因子 + 异常隔离 + 无未来函数 | `backend/tests/test_structural_factor_service.py` |
| 结构状态因子 API 路由（合法/非法参数/降级） | `backend/tests/test_structural_factors_api.py` |
| 结构状态因子前端面板契约（双周期 tabs/5 卡片/null/降级/不重算） | `frontend/scripts/contract-tests/structural-state-panel.test.ts` |
| 结构状态因子 V1.8 双周期差异 + 无未来函数 + sqz_on/primary_dir 字段 | `backend/tests/test_structural_factor_service.py::test_v18_dual_period_difference` / `test_v18_no_future_function_confirmed_pivots` / `test_volatility_v18_sqz_on_off` |
| 结构状态因子 V1.8 前端字段契约（v18Keys 39 项含 node_interval_position_0_1/node_interval_position_raw/cost_position_zone/value_area_zone/val_price/vah_price + v18RelationKeys 7 项） | `frontend/scripts/contract-tests/structural-state-panel.test.ts` |
| 结构状态因子 V1.8 成本/节点位置语义修复（zone 分类 6 种 + node_interval_position 公式 clip/raw + null + 截图案例 close=147.62→1.000 + position_0_1 保持 VP 全区间语义 + 前端标签修复 VP全区间位置/节点区间位置/VA状态/VAL/VAH） | `backend/tests/test_structural_factor_service.py`（18 个新增） + `frontend/scripts/contract-tests/structural-state-panel.test.ts::Cost/Node card uses unambiguous position labels` |
| 时序特征 V1 后端服务（daily_context 9 字段 + m15_response 9 字段 + derived_relation 3 字段 + 异常隔离 + 无未来函数 + anchor 规则 bsl<bsh→low / bsh<=bsl→high + position_change 手算 + anchor percentile 不变性 + 组级异常隔离） | `backend/tests/test_temporal_feature_service.py` |
| 时序特征 V1 API 路由（合法/非法参数/`as_of!=latest` 400/降级/meta 结构） | `backend/tests/test_temporal_features_api.py` |
| **[PR #74 事件状态面板开关契约]** `eventPanelCollapsed` 默认展开（`false`，localStorage `panji:event-panel:v1` 持久化，`'collapsed'` 时收起）；开关按钮默认渲染（非截图模式 + symbol 存在）；按钮文案动态切换（显示事件状态/隐藏事件状态）；`shouldShowPanel = !eventPanelCollapsed && !hideStructuralStateParam`；`hideStructuralState=1`/`capture=1`/`capture=feishu` 强制隐藏并禁用 toggle（early return）；`EventStatePanel` 在 `shouldShowPanel && symbol` 时渲染；toggle 按钮在 `tv-chart-column` 内部（toolbar prop 传入）；`TemporalFeaturesCard` 在 `StockStructuralStatePanel` 内；capture 模式无侧列（testid 落在 `tv-chart-column`） | `frontend/scripts/contract-tests/structural-state-toggle.test.ts`（14 用例） |
| **飞书渠道操作按钮权限**：member 不显示 admin 最近事件实测按钮；member 渠道卡显示「测试并启用」/「发送测试消息」；admin 才显示「管理员实测最近事件」；编辑/删除对两者都可见 | `frontend/src/pages/__tests__/settingsFeishuActions.test.ts`（5 用例） |
| **趋势选股 URL 状态 encode/decode**：strategy/keyword/sort/filters/page/pageSize 往返一致；默认 page/pageSize 省略；非法 filters JSON 回退为空数组；陈旧 filter/sort key 丢弃；不保存 selectedKeys/activeRunId/rows/results | `frontend/src/components/__tests__/screenerUrlState.test.ts`（5 用例） |
| **个股详情返回按钮**：优先使用 URL `returnTo` 参数，其次 `location.state.returnTo`，没有时按 source fallback 到 `/screener` 或 `/market?scope=watchlist`；`buildMarketEntryFromScreener`/`buildMarketEntryFromMessage` 生成 `/market` 入口 URL | `frontend/src/pages/__tests__/detailNavigation.test.ts`（7 用例） |
| 结构状态因子 V1.9 active swing + confirmed pivot 别名 + DSA age 统一（上涨突破 raw>1 且 active in [0,1] / 下跌破位 raw<0 且 active in [0,1] / 单边上涨 active high 跟随 / 单边下跌 active low 跟随 / bars_since_active 正确 / confirmed_swing_breakout_state 三态 / fallback 模式 / DSA age +1 口径 / age_bars == current_dsa_segment_age_bars） | `backend/tests/test_structural_factor_service.py`（新增 9 个 V1.9 测试） |
| 时序特征 V1.9 active swing 字段 + derived_relation 改用 active swing（daily/m15 active swing 字段存在性 + m15_position_relative_to_daily == active - active + 强趋势段不再 -1.755） | `backend/tests/test_temporal_feature_service.py`（新增 3 个 V1.9 测试） |
| 结构状态因子 V1.10 developing swing（major up leg 回落 developing_low = min(lows[active_high_bar:now]) 不得等于 active_low / major down leg 反弹 developing_high = max(highs[active_low_bar:now]) 不得等于 active_high / 继续创新高 dev=active dir=1 / 继续创新低 dev=active dir=-1 / 000100 类似 case developing_low 不得等于 4.45） | `backend/tests/test_structural_factor_service.py`（新增 5 个 V1.10 测试） |
| 时序特征 V1.10 developing swing 字段 + derived_relation 改用 developing swing（daily/m15 developing swing 字段存在性 + m15_position_relative_to_daily == developing - developing + 不回退 active major leg 或 confirmed raw + developing 缺失返回 null） | `backend/tests/test_temporal_feature_service.py`（新增 2 个 V1.10 测试 + 替换 2 个 V1.9 旧测试） |
| 盘后 publish auto-trigger（DSA scheduled+completed 触发 / 非 DSA 不触发 / missing trade_date 不触发 / create_failure_no_propagation） | `backend/tests/test_worker_auto_trigger.py`（4 个测试） |
| Swing 摘要卡 V1.10 developing 字段契约（摘要卡只显示 developing 字段 / active major leg 只在明细 / confirmed pivot 只在明细 / 禁止模糊标签 / 禁止 Active high/low 作为主字段 / 时序位置标签含 developing 或 confirmed 前缀） | `frontend/scripts/contract-tests/structural-state-panel.test.ts`（替换 3 个 V1.9 测试为 V1.10 版本） |
| capture 布局 V1.9 单列契约（capture=feishu 不渲染按钮/侧列/Temporal / .tv-side-column 隐藏 / .tv-chart-column 占宽 100% / testid 在 tv-chart-column） | `frontend/scripts/contract-tests/capture-stock-page.test.ts`（新增 2 个 V1.9 测试） |
| 结构状态开关 V1.9 capture 模式契约（isCaptureMode 判定 / capture=feishu 强制隐藏且禁用 toggle / capture=1 强制隐藏 / hideStructuralState=1 强制隐藏） | `frontend/scripts/contract-tests/structural-state-toggle.test.ts`（新增 2 个 V1.9 测试） |
| DSA overlay source alignment：`compute_source_bar_times/hash` 按 timeframe 格式化（15m/1h 含时间，1d 仅日期） | `backend/tests/test_chart_bars_service.py`（新增 6 个 source 对齐测试） |
| DSA overlay source alignment：`indicator_service` 在 15m/1h 使用 macd_bars 计算 source_bar_times/hash，与 chart bars 同源 | `backend/tests/test_indicator_service.py`（新增 3 个 macd_bars 同源测试） |
| DSA overlay source alignment：15m/1h `bars.trade_time` 返回 aware datetime（Asia/Shanghai tzinfo，序列化为 `+08:00`），1d trade_date 仍为 date 对象 | `backend/tests/test_bars_vectorization.py`（新增 3 个 tzinfo 测试） |
| DSA overlay source alignment：`normalizeChartTime` naive 与 aware ISO 产生相同 canonical key / 15m K线 aware 与 source_bar_times naive 全部匹配不误报 mismatch / 故意构造 source mismatch 仍触发暂停 / `timeTicks` 15m aware 时间显示北京交易时间不显示 03:00 | `frontend/src/components/__tests__/dsaSourceAlignment.test.ts`（新增 14 个 contract 测试，纯 .ts 模块 `frontend/src/utils/chartTime.ts`） |
| Indicator overlay alignment：`indicator_cache.ALGORITHM_VERSION == "v11"`（CHANGE-20260718-004：Node Cluster engine 唯一入口 + 三链统一 + `_SCHEMA_VERSION` 2→3，旧 v10 缓存强制失效；CHANGE-20260717-001：SMC Pine parity 最终收口——warmup/历史分离、execution gate、trailing NaN、OB 顺序 newest-first，旧 v9 缓存强制失效；CHANGE-20260716-001：crossover/crossunder 修正 v9；CHANGE-20260715-006：RMA NA + off-by-one + 三时间点 v8；CHANGE-20260715-002：SMC 从 SMA 基线升级为 Pine parity 核心 `smc_pine_core.py` v7，旧 v6 缓存强制失效；CHANGE-20260715-001：新增 SMC 指标按需启用，cache key 追加 `:smc` 后缀隔离）且旧 v5/v6/v7/v8/v9/v10 cache key 不匹配新 `build_cache_key`（旧缓存自然失效）；`include_smc=true` cache key 追加 `:smc` 后缀与默认路径隔离，两路径互不污染 | `backend/tests/test_indicator_cache.py`（CHANGE-20260715-001 新增 3 个 SMC 缓存隔离测试 + 2 个 v5→v6 schema 版本测试 + CHANGE-20260715-002 v6→v7 schema 版本测试 + CHANGE-20260717-001 v9→v10 schema 版本测试 `test_cache_algorithm_version_bumped_to_v10` + CHANGE-20260718-004 v10→v11 schema 版本测试 `test_cache_algorithm_version_bumped_to_v11`） |
| Indicator overlay alignment：`_adapt_watchlist_bb` 1d/15m/1h/1w/1mo 全部用 `macd_bars` 调用 `compute_bollinger` 计算 BB（非日线阶梯线，1w/1mo 不再移除 BB 字段），BB 长度与 macd_bars 对齐，数值与 `compute_bollinger(macd_bars)` 一致 | `backend/tests/test_indicator_service.py`（PR #32 删除 2 个旧 1w/1mo BB 移除测试，新增 2 个 1w/1mo BB 用 macd_bars 计算测试；PR #31 已有 3 个 15m/1h BB overlay 计算测试保留） |
| Indicator overlay alignment：DSA 全周期支持，`MarketDataContext.bars_daily=macd_bars`（所有周期用当前 timeframe bars），`daily_time_list` 用 `macd_bars.index`，15m DSA `time[0]` 含 `T` 分隔符（非日线 YYYY-MM-DD），15m context.bars_daily 第一根 bar `hour==9`（非 daily 的 0） | `backend/tests/test_indicator_service.py`（PR #32 新增 2 个 DSA 全周期计算测试） |
| Indicator overlay alignment：`shouldAllowDsaOverlay` 1d/15m/1h/1w/1mo 全部 true / `shouldCheckDsaMismatch` 全周期 true / `DSA_TITLE_HINT('1d')` 含"日线结构锚" / `DSA_TITLE_HINT('15m'/'1h'/'1w'/'1mo')` 含"当前周期验证图层"且不含"日线结构锚" | `frontend/src/components/__tests__/dsaSourceAlignment.test.ts`（PR #32 重写第 4 节，4 个 DSA overlay policy contract 测试覆盖全周期 + title 按周期区分） |
| Indicator overlay alignment：PR #33 前端硬编码清理 — `shouldRenderDsaLayer` / `shouldRenderBbLayer` / `shouldToggleDsa` / `shouldIncludeDsaInPriceRange` 全周期决策（不再 `timeframe !== '1d'` skip / `1w \|\| 1mo` skip / `timeframe === '1d'` y-axis 限制） | `frontend/src/components/__tests__/dsaSourceAlignment.test.ts`（PR #33 新增第 5 节，10 个 overlay 渲染/toggle/y-axis 决策测试覆盖全周期 + capture 锁定 + source mismatch 保护） |
| DSA visual_segments time alignment：PR #34 后端 `format_dsa_time` 按 timeframe 序列化（15m/1h 含 `THH:MM:SS`，1d/1w/1mo 为 `YYYY-MM-DD`）；`compute_dsa_bundle` 15m `visual_segments.points.time` / `anchor.time` 含 `HH:MM`；`DSASelector.compute_indicators` 15m `time` / `visual_segments.points.time` 含 `HH:MM`；1d 仍为 `YYYY-MM-DD`；15m segment times 与 source_bar_times canonical 匹配率 > 0.5 | `backend/tests/test_dsa_visual_segments_time_format.py`（PR #34 新增，9 个测试覆盖 15m/1d 时间格式 + canonical 对齐） |
| DSA visual_segments matched ratio contract：PR #34 前端 `computeDsaSegmentMatchStats(segments, displayTimes, timeframe)` 计算 segment points 经 `normalizeChartTime` 后与 K线 `displayTimes` canonical key 的匹配率；15m/1h 含 `THH:MM` 时 `ratio > 0.5`；旧 YYYY-MM-DD 在 15m 下 `matched=0` / `degradedReason='segment_time_no_match'`；空 segments `degradedReason='no_segments'`；多 segment 累计 matched | `frontend/src/components/__tests__/dsaSourceAlignment.test.ts`（PR #34 新增第 6 节，7 个 segment matched contract 测试覆盖 15m/1h/1d/empty/多 segment 累计） |
| **CHANGE-010 前端源码契约（CHANGE-20260713-010）**：49 项断言覆盖五大主题——（1）StockQuoteStrip：8 项指标布局、`formatMarketCap` 万/亿/万亿元格式化、空值显示 `--`、`QuoteMetric` 子组件；（2）MiniKlineCard：lightweight-charts v4 createChart + addCandlestickSeries、`useMiniKlineData` BARS_COUNT {1d:80,1w:60,1mo:48}、`refetchInterval:false`、chart 实例 `useEffect []`、ResizeObserver 卸载清理 `disconnect()`+`chart.remove()`+ref 清空、timeframe 独立于 symbol；（3）MarketRightPanel：组合 `MiniKlineCard` 顶部 + `EventStatePanel` 底部、收起时 0 请求、展开只请求活动周期不预取三周期；（4）filterAlias：`DataTableColumn.filterAlias?:'keyword'`、stock 列 `filterAlias='keyword'`+`filterable=true`、`KeywordFilterPopover` 双向同步、`isKeyword` flag 不进入 `filters` state、URL sync `replace:true`+`skipNextUrlSyncRef`、stock 列不进入 `metric_filters`；（5）Excel 导出：`ExportContext` 暴露 `handleExport`、`convertFiltersToMetricFilters` 与 `buildStrategyResultQueryParams` 同源、`POST /strategy-runs/{run_id}/results/export`、下载真实 .xlsx（MIME `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`）、stock 列 `payload_key=null`（不导出操作列） | `frontend/src/features/market-workspace/__tests__/change010Contract.test.ts`（49 用例） |


## 6. 文档和工程治理

| 规则 | 测试 |
|---|---|
| docs consistency | `tools/tests/test_check_docs_consistency.py` |
| architecture rules | `tools/check_architecture.py` |
| test allowlist | `tools/check_test_allowlist.py` |
| Ruff/Mypy baseline | CI baseline regression jobs |
| **ref/ 目录隔离 AST 守护（CHANGE-20260718-004）**：生产代码、测试、工具、构建脚本禁止运行时 `import/open/read/glob ref/`；`ref/` 文件禁止被 claim 为真源/合同/fixture 生成器/运行依赖；`ref/smc_user_export.pine` 已 `git rm --cached`（仅 `ref/smc_user_source.pine` 保留 git 跟踪）；AGENTS clause 23 禁止运行时访问 `ref/`；clause 59/60 明确 ref 仅人工阅读 | `backend/tests/test_ref_isolation.py`（AST + 文本扫描双重守护）+ `tools/check_docs_consistency.py` rules 13/14/15（CLI 层 docs 扫描，与 pytest 互补） |

## 6.1 Node Cluster 三链一致性与计算内核（CHANGE-20260718-004）

| 规则 | 测试 |
|---|---|
| **engine 单元**：`compute_node_cluster_profile` 返回 `NodeClusterProfileResult`（不可变 frozen dataclass）；VAH 上方/VAL 下方 Peak 仍保留在 `peak_rows/all_peak_prices`；多 Peak 全部保留；无 Peak 时 `peak_rows=[]`；`derive_state_for_price` 可选 VA 外 Peak 为 `upper_node`；`detect_crossover_signals` 对 VA 外 Peak 仍触发；`profile_hash` 同输入同输出；result 含 `algorithm_version`/`output_schema_version`/`contract_fingerprint`/`daily_source_hash`/`bars_15m_source_hash` | `backend/tests/test_node_cluster_engine.py`（9 用例 + fixtures：single_peak/multi_peak/no_peak CSV） |
| **三链一致性**：同一 stock/as_of/daily/15m → snapshot（`feature_snapshot_service`）/indicator（`indicator_service`）/monitor（`volume_node_monitor`）三链 `profile_hash` 完全一致；100 行 profile 完全一致；POC/VAH/VAL 完全一致；Peak 价格/强度完全一致；同 reference_price → state 完全一致；000725 真实数据回归（17 events / 21 OB / 2 EQL / swing_bias=1 不变，不称 TV golden 或完全对齐）；603538 真实数据回归 | `backend/tests/test_node_cluster_three_chain_consistency.py`（7 用例，生产只读回归不写） |
| **架构守护 AST**：遍历 `backend/app/**/*.py`（排除 `node_cluster_engine.py` 与 `strategy_assets/algorithms/features/`），断言无 `from ...unified_volume_profile import compute_unified_volume_profile`；同上对 `luxalgo_volume_profile_pytdx_15m_aligned`；AST 检查无业务模块调用 `compute_volume_profile`；扫描 `frontend/src/**/*.{ts,tsx}` 禁止 `computeVolumeProfile`/`computeUnifiedVolumeProfile`/`valueArea` 过滤函数；`StrategyChart.tsx` 中 `is_value_area` 仅用于 alpha 不用于过滤 Peak | `backend/tests/test_node_cluster_architecture.py`（5 用例，模式同 `test_market_data_ssot_architecture.py`） |
| **监控节奏回归**：monitor 链 1m 读取 `completed_only=True`（过滤 partial bar）；`detect_crossover_signals` 公式与原 `_detect_node_crossover_signals` 完全一致（`prev_close <= peak < cur_close` or `cur_close <= peak < prev_close`）；`_check_event_cooldown` 600s 窗口不变；`EVENT_STATE_TTL_SECONDS = 600` 不变；`_send_merged_notification` 按用户合并一张卡片不变 | `backend/tests/test_monitor_rhythm_regression.py`（5 用例） |

## 7. v2 应用后需要新增/调整

- 修改 docs consistency 测试，让它检查 `current/MANIFEST.md` 而不是每个 current 文件头；
- 新增 maps 必备文件存在性检查；
- 新增旧 `docs/current/00-18` 不再作为 current 事实源的检查；
- 新增 local links 覆盖 `maps/`。

## 8. 测试汇总

| 范围 | 数量 | 说明 |
|---|---|---|
| 后端 pytest | 225 tests passing | 全量 backend 测试通过基线 |
| `test_wencai_board_provider.py` | 53 tests | WencaiBoardProvider adapter pywencai 查询/重试/超时/解析/异常（CHANGE-20260716-007） |
| `test_bar_repository_get_recent_bars.py` | 8 tests | BarRepository.get_recent_bars 边界/隔离/排序 |
| `test_board_sync.py` | 17 tests（重写） | BoardSnapshot + 绝对门禁 + 相对门禁 + 原子切换 + Migration 循环 + source=wencai（CHANGE-20260716-007 + PR #77） |
| `test_after_close_board_sync.py` | 10 tests | `syncing_boards` 步骤软失败/非交易日跳过/dsa_only 跳过/BOARD_SYNC_ENABLED 开关（CHANGE-20260716-007） |
| `test_board_filter_helper.py` | 23 tests（PR #77 新增） | industry ilike 关键词匹配/转义 `\`/`%`/`_`/NFKC/AND/concept 精确（CHANGE-20260716-007 PR #77） |
| `test_board_sync_enabled_config.py` | 12 tests（PR #77 新增） | BOARD_SYNC_ENABLED 环境变量解析 1/true/yes/on/TRUE/false/0/no/off/empty/precedence/default（CHANGE-20260716-007 PR #77） |
| `test_market_stocks.py` | 现有用例 + 新字段断言 | /market/stocks + /market/boards available/reason_code/source/stale/last_attempt_status（CHANGE-20260716-007） |
| `wencaiBoardSyncContract.test.ts` | 28 tests（PR #77 重写） | 前端 wencai 板块同步契约 + BoardFilterCombobox：stale/source/last_attempt_status/禁用输入/BoardFilterCombobox mode/keyboard/12-item/clear/blur/aria/highlight（CHANGE-20260716-007 + PR #77） |
| `test_stock_state_and_events.py` | 现有用例 | 个股状态与事件 |
| `test_after_close_orchestrator.py` | 现有用例 | 盘后编排状态机 + feature_snapshot 步骤 |
| `test_excel_export_api.py` | 21 tests | Excel 导出 API 集成（CHANGE-20260713-010） |
| `test_excel_export_service.py` | 9 tests | Excel 导出 Service 单元（CHANGE-20260713-010） |
| `test_share_capital.py` | 14 tests | 股本同步 + migration 063 + quote 市值计算（CHANGE-20260713-010） |
| 前端 node 测试 | 108 tests | 64 route/url/types + 44 contract |
| `change010Contract.test.ts` | 49 tests | CHANGE-010 前端源码契约（市值/MiniKline/MarketRightPanel/filterAlias/Excel） |
| `test_smc_indicator.py` | 34+8+1 tests | SMC Pine 语义核心 + FVG 排除 + Pine 原语 8 用例 + golden fixture skip（CHANGE-20260715-001 → CHANGE-20260715-002 → CHANGE-20260715-006 RMA NA + off-by-one + 三时间点 → CHANGE-20260716-001 crossover level_curr/level_prev 快照 → CHANGE-20260717-001 execution gate/trailing NaN/OB 顺序） |
| `test_smc_view_adapter.py` | 31 tests | SMC view adapter 有界 DTO + 重基准索引 + clipped OB（CHANGE-20260716-001） |
| `test_smc_tv_parity.py` | 3+3 tests | TV CSV parity（bar/event/swing_bias + OB/EQ endpoint/全链 chain），无 fixture 时 skip（`PINE_PARITY_PENDING`）（CHANGE-20260716-001 → CHANGE-20260717-001 golden 重做：EQH 类型误映射/15m 时间戳压缩/OHLC no-op/±1 容差修复 + 0 容差严格逐 bar 对齐） |
| `test_smc_pine_deterministic.py` | 8 测试类（427 行） | SMC 确定性测试（不依赖 TV CSV fixture）：CHoCH 规则/BOS 规则/warmup 一致性（5000 计算 vs 4000 计算重叠窗口一致）/OB newest-first 顺序/OB core→adapter 全链字段/trailing NaN + last_top_time/last_bottom_time/execution gate 关闭→事件为空/EQ 两端点 + 区间方向（CHANGE-20260717-001） |
| `miniKlineViewport.test.ts` | 15 tests | MiniKline viewport（目标根数 + logical range + autoscale range）（CHANGE-20260715-002 → CHANGE-20260716-001 真实方案：slice(-target)、{from:-2,to:len-1+3}、删除死 barSpacing） |
| `miniKlineCardContract.test.ts` | 20 tests | MiniKlineCard 组件源码契约（无 fitContent、setVisibleLogicalRange、autoscaleInfoProvider、ResizeObserver、requestAnimationFrame、五周期按钮、A 股配色、闭包根治 16-20）（CHANGE-20260715-003 → CHANGE-20260715-006） |
| `smcRendering.test.ts` | 40 tests | SMC 渲染纯函数（映射/区间求交/OB 选择/价格候选）+ Canvas mock 行为测试（CHANGE-20260716-001） |
| `detailSourceLoadingContract.test.ts` | 9 tests | 详情页来源列表 loading 占位契约（sourceListLoading 字段、loading 占位渲染、列表渲染条件排除 loading、header 显示、CSS 存在、handleNavigateToStock 显式传 source/strategy、URL 完整性、不使用 useMarketStocks、上一只/下一只保留 returnTo）（CHANGE-20260715-004） |
| `indicatorManifest.test.ts` | 15 tests | CHART_LAYER_MANIFEST 用户文案 + SMC 图层（CHANGE-20260715-001 扩展 3 用例） |
| `test_market_data_ssot_architecture.py` | 5 tests | MDAS SSOT 架构守护 AST 测试：禁止业务层导入 repository 私有行情查询/复权/旧 get_bars、禁止业务层自行 resample 周/月、仅 MDAS 可导入 kline_aggregator（CHANGE-20260717-002） |
| `test_bars.py`（CHANGE-20260717-002 扩展） | 现有用例 + 新增 | MDAS v2 契约 + adjustment_as_of 截断 + 不信任 bar 自带 adj_factor 列 + qfq 唯一出口 |
| `verify_603538_step6.py` | 真实 DB 核对脚本 | 603538 美诺华除权真实回归：none/qfq × 1d/15m/1h + adjustment_as_of 三场景 + 600276 对照 + 跨调用方 hash 一致（CHANGE-20260717-002，非 pytest，独立脚本，位于 `backend/scripts/`） |
| `test_ref_isolation.py` | AST + 文本扫描 | ref/ 目录隔离守护：生产/测试/工具/构建脚本禁止运行时 import/open/read/glob ref/；ref/ 不得称为真源/合同/fixture 生成器/运行依赖（CHANGE-20260718-004） |
| `test_node_cluster_engine.py` | 9 tests | Node Cluster engine 单元：`compute_node_cluster_profile` 不可变结果 / Peak 全保留 / `derive_state_for_price` / `detect_crossover_signals` / `profile_hash` 确定性 / 版本字段（CHANGE-20260718-004） |
| `test_node_cluster_three_chain_consistency.py` | 7 tests | 三链一致性：snapshot/indicator/monitor profile_hash + 100 行 profile + POC/VAH/VAL + Peak + state 完全一致；000725/603538 真实数据回归（17 events / 21 OB / 2 EQL 不变）（CHANGE-20260718-004） |
| `test_node_cluster_architecture.py` | 5 tests | Node Cluster 架构守护 AST：仅 engine 可导入底层 VP；业务模块禁止直接调用 `compute_volume_profile`；前端禁止 VP 计算/VA 过滤（CHANGE-20260718-004） |
| `test_monitor_rhythm_regression.py` | 5 tests | 监控节奏回归：completed 1m only / cross 公式 / dedupe 600s / TTL 600s / 通知合并不变（CHANGE-20260718-004） |
| `test_stock_context_atomic_facts.py`（CHANGE-20260721-001 扩展） | 9 个新增 nodeAvailability 测试 | nodeAvailability 5 态状态机：NO_PUBLISHED_RUN/SNAPSHOT_MISSING/NODE_PROFILE_EMPTY/NODE_15M_MISSING/NODE_COMPUTE_FAILED/NODE_INSUFFICIENT_DAILY_BARS/LEGACY_SNAPSHOT_NO_NODE_CLUSTER + available + admin debug 字段一致 |
| `test_feature_snapshot_service.py`（CHANGE-20260721-001 扩展） | 4 个新增 node_cluster 字段测试 | node_cluster 字段写入：available/missing_15m/insufficient_daily/engine_raises（即使 profile 为 None 也写入最小诊断字段） |
| `test_node_cluster_three_chain_consistency.py`（CHANGE-20260721-001 扩展） | 4 个新增五周期一致性测试 | TestNodeClusterFivePeriodConsistency：profile_hash/profile_rows/independent_of_display_timeframe/degraded_state_when_15m_missing；五周期（1d/15m/1h/1w/1mo）profile_hash 完全一致 |
| `test_factor_reconciliation.py`（CHANGE-20260721-001 扩展） | 4 个新增 `_invalidate_downstream_caches` 测试 | FR-11 缓存失效：test_invalidates_all_three_cache_layers / test_bars_cache_failure_does_not_block_indicator / test_indicator_cache_failure_does_not_block_bars / test_rebuild_factor_series_calls_invalidate_downstream |
| `test_capture_snapshot.py`（CHANGE-20260721-001 扩展） | 6 个 indicator_view 测试 | CaptureStockPage 读取 indicator_view URL 参数；Snapshot API 接收 indicator_view 驱动 include_smc；StrategyChart 使用 INDICATOR_VIEW_LAYER_PRESETS |
| `stockResearchTypes.test.ts`（CHANGE-20260721-001 扩展） | 9 个 INDICATOR_VIEW_LAYER_PRESETS 测试 | INDICATOR_VIEW_LAYER_PRESETS / normalizeIndicatorView / getIndicatorViewLayerPreset 三函数纯函数测试 |

## Phase D 测试（CP-V3-D）

### Auto-resume 受控测试
- `backend/tests/test_phase_d_auto_resume.py`（9 个测试）：
  - refreshing_daily / waiting_dsa_worker / feature_snapshot 三步骤中断恢复
  - attempt_no 递增 + max=3 限制
  - lease_epoch fencing
  - 唯一活跃记录 / 无僵尸 running / 非 after_close 排除
  - 完整状态机闭环（2 轮）

### 因子版本字段测试
- `backend/tests/test_phase_d_factor_version.py`（6 个测试）：
  - stamp 写入 3 个字段
  - find 识别 NULL / 旧算法版本 / 旧对账版本
  - 当前版本不被识别 / 非 active 排除 / 混合场景
