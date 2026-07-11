# 04 前端、路由与 UX

## 1. 前端职责

前端使用 React、TypeScript、React Router。前端负责页面、交互、DTO 到 ViewModel、图表和页面状态。

前端不得重新实现：

```text
后端权限
套餐额度
DSA 算法
Node Cluster 算法
发布门禁
监控资格
```

## 2. 路由与守卫

| 路由 | 守卫 | 壳层 | 页面 |
|---|---|---|---|
| `/` | Public | — | 门户 |
| `/login` | Public | — | 登录/注册 |
| `/subscription-expired` | Authenticated | — | 续期 |
| `/membership-expired` | Redirect | — | 兼容跳转 |
| `/capture/stock/:symbol` | Capture Token | **无壳层** | 截图专用页面 |
| `/market` | Subscriber/Admin | UserAppShell | 行情工作区（三栏：左列表+中K线+右结构状态，`MarketWorkspacePage`） |
| `/screener` | Subscriber/Admin | UserAppShell | 趋势选股 |
| `/stock/:symbol` | Subscriber/Admin | UserAppShell | 个股详情 |
| `/messages` | Authenticated | UserAppShell | 历史消息 |
| `/settings` | Authenticated | UserAppShell | 账户和通知渠道 |
| `/admin/*` | Admin | AdminAppShell | 管理页面（含 `/admin/overview`、`/admin/users`、`/admin/strategies`、`/admin/jobs`、`/admin/beta-applications`、`/admin/after-close`） |
| `/overview` | Redirect | — | 兼容重定向 → `/market` |
| `/watchlist` | Redirect | — | 兼容重定向 → `/market?scope=watchlist` |

### 2.1 壳层拆分（阶段二确立）

- `UserAppShell`（`frontend/src/layouts/UserAppShell.tsx`）：普通用户布局壳，顶部品牌 + 一级导航（行情 `/market`、趋势选股 `/screener`）+ 右上角账户菜单；**无左侧栏**。
- `AdminAppShell`（`frontend/src/layouts/AdminAppShell.tsx`）：管理员独立布局壳，侧栏管理导航 + 顶栏账户菜单；仅承载 `/admin/*`。
- `AccountMenu`（`frontend/src/components/AccountMenu.tsx`）：右上角下拉菜单，包含消息、设置、管理后台（仅 `is_admin`）、退出；支持点击外部关闭、Escape 关闭、基本 ARIA。
- `ProtectedLayout` 只负责认证和 access profile，不再固定渲染同一壳层；`UserAppShell`/`AdminAppShell` 由各自路由组在 `ProtectedLayout` 之下作为 layout element 挂载。
- `/capture/stock/:symbol` 位于两套壳层之外，只使用 `captureClient`。
- 导航/路由常量集中于 `frontend/src/navigation/appNavigation.ts`，避免路径散落。

### 2.2 兼容重定向

旧地址保留重定向，避免书签/旧链接 404：
- `/overview` → `/market`
- `/watchlist` → `/market?scope=watchlist`

### 2.3 尚未完成（下一阶段）

- 复盘模式本轮不开发。

### 2.4 统一行情工作区

- `/market` 渲染 `MarketWorkspacePage`（`frontend/src/features/market-workspace/MarketWorkspacePage.tsx`），三栏布局：
  - 左栏 `MarketInstrumentPane`：`scope=watchlist` 使用 `useWatchlistMonitorStatus`（enabled=scope==='watchlist'）；`scope=market` 使用 `useInstruments` 搜索（enabled=scope==='market' && 搜索词 trim 后 ≥2 字符，限制 50 条，不发 N+1 请求）。两个查询按 scope 互斥启用，未激活的 scope 不发请求。搜索结果列表仅在 `scope==='market' && canSearch` 时渲染；关键词不足 2 字符、清空输入或切换 scope 时不得显示缓存结果（Query 仍通过 `enabled` 门控，不条件调用 Hook，只门控渲染）。
  - 中栏 `StockResearchWorkspace`（`frontend/src/features/stock-research/StockResearchWorkspace.tsx`）：K 线研究区，由 `MarketWorkspacePage` 和 `StockDetailPage` 共用。
  - 右栏 `EventStatePanel`（`frontend/src/features/research-context/EventStatePanel.tsx`）：可收起；收起时不挂载、不请求数据，中栏自动扩展。面板使用 `useStockContext` 单一接口（`GET /api/v1/stocks/{symbol}/context`），展示数据日期/质量、当前价格结构、成交密集区关系、SQZMOM 动量、波动位置、最近状态变化时间线；普通用户不显示内部字段名（`sourceField`）、算法参数、`idempotencyKey`、JSON 或商业机密；原始 factor/feature/JSON 仅在 `/admin/stocks/:symbol/debug` 展示。
- `/stock/:symbol` 是唯一个股详情和 K线入口（PRD V1.1），渲染 `StockDetailPage`（`frontend/src/pages/StockDetailPage.tsx`）：
  - 使用共享 `useStockResearchData` + `StockResearchWorkspace` 渲染图表区，不再独立调用 useBars/useIndicators/useRealtimeQuote/useInstrumentEvents。
  - 详情页专属能力拆到 `useStockDetailActions`（自选/上下切换/memo + returnTo 上下文恢复左栏列表）和 `useStockDetailFeishu`（飞书截图/轮询/超时）。
  - 保留 header、价格条、返回、上下只、watchlist、memo、飞书、全屏、事件状态面板开关。
  - `StockResearchWorkspace` 通过 `toolbar`/`rightPanel`/`showRightPanel`/`chartColumnProps` 可选 props 支持详情页的事件状态面板开关和截图模式属性。
  - **禁止重定向到 `/market`**（PRD V1.1 硬性规定）。
- `/admin/stocks/:symbol/debug` 渲染 `AdminStockDebugPage`（`frontend/src/pages/AdminStockDebugPage.tsx`），位于 `AdminRoute` + `AdminAppShell` 下，普通用户不可访问（403）：
  - 复用 `MarketInstrumentPane`（股票搜索）、`useStockResearchData`（bars/indicators/quote/events）、`StockResearchWorkspace`（K线研究区）、`useAdminStockDebug`（含原始 payload 的管理员调试接口）。
  - 管理员调试能力独立于 `/market`，`/market` 不承载任何原始因子或 JSON。
- 共享类型（`DisplayTimeframe`/`ResearchSource`/`ALLOWED_TIMEFRAMES`/`BARS_COUNT_BY_TIMEFRAME`/`defaultStrategyForSource`/`normalizeDisplayTimeframe`/`normalizeResearchSource`）权威定义在 `frontend/src/features/stock-research/stockResearchTypes.ts`；`marketWorkspaceUrlState.ts` 从该文件导入并重新导出，依赖方向为 market-workspace → stock-research（禁止反向依赖）。
- `useStockResearchData` 只保留图表核心查询：instrument/bars/indicators/quote/events + priceSummary/quoteStatus/barsStatus/isRenderReady；自选操作、上下切换、memo、飞书由 `useStockDetailActions`/`useStockDetailFeishu` 负责（禁止加入核心 hook）。
- URL 状态：`/market?scope=watchlist|market&symbol=xxx&timeframe=1d&source=watchlist|selection&strategy=xxx&event_id=xxx&returnTo=...`；scope/symbol/timeframe/source/strategy/event_id/returnTo 进 URL，右栏折叠和 viewport 留本地。切换股票不整页刷新。非法 timeframe 回退 1d。`/stock/:symbol` 的 timeframe 也从 URL 解析（单一真源），工具栏切换写回 URL。`returnTo` 为来源页 URL，必须经 `normalizeInternalReturnTo`（`frontend/src/features/market-workspace/marketWorkspaceUrlState.ts`）校验——仅允许 `/screener`、`/market`、`/messages` 前缀（含 query/hash），拒绝外部 URL（http/https）、`javascript:`、双斜杠、非白名单前缀（如 `/admin`、`/login`、`/capture/stock`）、超长字符串（>2000 字符）；左栏选股或切 scope 时清除 `returnTo`。
- `timeframe` 受控单一真源：URL → `useStockResearchData`（bars/indicators 请求参数）→ `StockResearchWorkspace`（图表渲染）三者始终使用同一 `DisplayTimeframe`（'15m'|'1h'|'1d'|'1w'|'1mo'）；工具栏切换通过 `onTimeframeChange` 回调写回 URL，禁止子组件 `useState` 维护独立 timeframe。
- URL 状态保留：切换周期/切换 scope/选择新股票时必须保留其他字段；选择新股票时清除旧 `event_id` 和 `returnTo`。
- 左栏选择上下文重置：从 `MarketInstrumentPane` 选择任意股票时必须写 `source='watchlist'`、`strategy='watchlist_monitor'`、`eventId=null`（退出 selection 上下文）；用户切换 scope（watchlist 或 market）时也必须退出 selection 上下文并清除旧 `event_id`；timeframe 在上述操作中继续保留。状态转换必须通过纯函数 `selectInstrumentFromMarketPane(state, newSymbol)` 和 `changeMarketScope(state, newScope)` 处理，禁止在多个 callback 中重复拼对象。
- 图表显示周期不改变 1d+15m 监控配置或 1m 事件触发口径。
- 错误状态：instrument/bars/indicators 加载失败时显示明确错误状态（含重试按钮），不伪装为空图。
- 周期文案：根据 timeframe 显示真实周期（1d=完整日线、15m=完整15分钟K线、1h=完整1小时K线、1w=完整周线、1mo=完整月线）；非实时非降级时统一显示"行情回退"（禁止所有非 1d 周期显示"日线回退"）；partial 文案必须包含当前周期（如"K线含未完成 bar（15m）"），禁止所有周期统一显示"日线"。
- `/capture/stock/:symbol` 完全独立，不使用 `useStockResearchData`/`StockResearchWorkspace`/`apiClient`，只使用 `captureClient`。
- `AccountMenu` 复用 `appNavigation.getAccountMenuItemsForVariant(isAdmin, variant)` 单一真源构建菜单项。
- 研究上下文纯函数：`buildStructureSummary`（`frontend/src/features/research-context/buildStructureSummary.ts`）从 `primary[timeframe].cost_position` 等真实 DTO 路径提取结构状态摘要（合并 degraded_reasons/warmup_notes、日线/15m 摘要、成本位置/节点）；`buildUserEventExplanation`（`frontend/src/features/research-context/buildUserEventExplanation.ts`）只消费白名单字段（event_time/event_type/payload.facts[].text_content/summary）并校验 `event.instrument_id` 与 `currentInstrumentId` 一致性（不一致时隐藏价格，显示"该事件属于其他股票"）。两个纯函数无 React 依赖，可被 `node --test` 直接运行。

## 3. 页面职责

### 盘后流水线详情（/admin/after-close）

- 顶部状态卡：整体状态（not_started/running/succeeded/failed/blocked/skipped）+ 交易日 + 市场时段 + watchlist_ready + 不可用原因 + 是否已有完整回补；
- 8 步骤垂直时间线：refreshing_daily → checking_coverage → creating_dsa → waiting_dsa_worker → quality_gate → feature_snapshot → publishing → watchlist_ready，每步显示 status/started_at/finished_at/duration/counts/error_message；
- 数据新鲜度卡：行情数据（latest_daily_trade_date / daily_coverage / 15m / 60m / is_behind）+ 选股策略（latest_compute_trade_date / latest_published_trade_date / status / total / failed / published_at）；
- 编排状态详情：after_close_orchestrator job_run 摘要（status/orchestrator_status/started_at/finished_at/worker/heartbeat/lease_expires/last_completed_step/error）+ feature_snapshot_run 摘要（run_type/scope/snapshot_count/failed_count/published_at）；
- 最近 20 次运行列表：after_close_orchestrator + snapshot_run 混合，显示类型/交易日/状态/编排阶段/快照数/失败/开始/结束/ID；
- 事件日志抽屉：展示最近 100 条 job_run_events（来自 pipeline.events，含 step/level/message/payload/created_at）；
- 轮询策略：running 状态 10s 轮询，非 running 60s 轮询，页面不可见暂停（refetchIntervalInBackground=false）；
- 操作按钮：触发当日 after_close 编排（POST /admin/after-close/pipeline/run，幂等，已有任务返回 existing）；
- 系统概览（/admin/overview）中的 AfterClosePipelineCard 改造为摘要卡，提供进入 /admin/after-close 的链接。

### 趋势选股

- 以全量 active 股票 universe 为展示主表（`strategy_run_items`），DSA 指标为 LEFT JOIN 附加字段；
- 默认无隐式筛选时显示全量股票（succeeded 有指标、skipped/failed 指标为空但仍显示）；
- succeeded 行应正确显示 35 个 DSA 指标（如 `bb.position`、`bb.width`、`node.position` 等）；后端通过 `(run_id, instrument_id)` 关联 `strategy_results` 加载指标（因 `strategy_run_items.result_id` 当前未回填，见 ALIGN-033）；
- skipped 行（reason_code=insufficient_history）显示股票代码和名称，指标列显示 "-"；
- failed 行显示股票代码和名称，指标列显示 "-"，附带 reason_code/error_message；
- 展示 source_total、filtered_total、成功、失败、跳过和覆盖率；
- 批次不完整显示阻断，不伪装正常；
- 行 key 使用 `instrumentId`（不依赖 `result_id`，skipped/failed 行也能选中加入自选）；
- "筛选结果" 标签替代原 "命中"（`filtered_total` 是当前筛选条件下的数量，不是命中数）；
- **批量加入自选**：`handleBatchAdd` 必须按 `r.instrumentId` 匹配 `selectedKeys`（与 `rowKey` 一致），禁止用 `r.resultId` 匹配（全量 universe 下 skipped/failed 行 `resultId=''`）；对 `instrumentId` 去重避免重复加入；选中后无可加入股票时 toast 提示而非静默；成功/失败 toast 真实反映数量；保留 `useAddToWatchlist` 现有缓存失效逻辑；
- **当日涨跌幅独立列**：`change_pct` 作为独立列展示（key=`change_pct`、title=当日涨跌幅、shortTitle=涨跌幅、dataType=percent、sortable=true、filterable=true、width≈86），render 使用 `fmtChange` + A股涨红跌绿颜色（`changePctColorClass`）；后端 `dsa_selector.yaml` manifest 已支持 `change_pct` filterable/sortable，无需改后端白名单；`change_pct` 是已为百分比数值的字段，筛选输入 3% 传 3，不要乘除错；
- **表格视图配置 preset**：`StrategyDataTable` 元信息栏新增"配置"入口（`TablePresetMenu` 组件），支持保存当前配置为新 preset、应用已有 preset、覆盖已有 preset config、重命名、设为默认、删除；默认配置进入页面后自动应用（每个 `tableId:strategyKey` 组合只应用一次，`useRef` 防重复）；保存成功后通过 `presetsQuery.refetch()` 立即刷新列表，保持下拉打开并清空输入框，失败时在下拉内显示后端错误并 toast；切换策略/批次时清空选中股票（`selectedKeys`），不保留选中状态；config 只保存 `keyword/sort/filters/hiddenColumns/pageSize`，禁止保存 `selectedKeys/page/activeRunId/rows/resultData`（后端 Pydantic schema 强制白名单）；preset API 按 JWT `user_id` 隔离，普通用户只能操作自己的配置；权限与趋势选股一致（active subscription + trend_selection feature，admin 豁免）；每 `user+table_id+strategy_key` 最多 20 个 preset；`is_default` 同维度至多 1 个 true（设置新默认时旧默认自动取消）；
- **sticky 表头与选择列**：`StrategyDataTable` 支持 `stickyHeaderMode` prop（`container` 默认 / `viewport`）；趋势选股页使用 `stickyHeaderMode="viewport"`，页面滚动时表头吸附在 topbar 下方（`.table-wrap.viewport-sticky { overflow: visible }` + `.table-wrap.viewport-sticky .data-table th { top: var(--topbar); z-index: 18 }`），不抢占局部滚动容器；普通表格保留 `container` 模式，`thead th` sticky top:0 z-index:4；sticky 首列/选择列 sticky left:0 z-index:3；角落单元格（表头+sticky 列）z-index:5（最高）；选择列存在时首列通过相邻兄弟选择器 `.table-select-column + th.sticky-col { left: 40px }` 偏移选择列宽度，避免 sticky 重叠；`box-sizing: border-box` 确保选择列宽度含 padding。
- **URL 状态持久化**：趋势选股页将当前策略 key、keyword、sort、filters、page、pageSize 同步到 URL query；`StrategyDataTable` 负责 keyword/sort/filters/page/pageSize 的 encode/decode，`ScreenerPage` 负责 strategy key 的同步与切换；filters 使用 compact JSON 只保存 `key/op/value/value2`，不保存 rows/selectedKeys/activeRunId/resultData；decode 时按当前有效列 key 集合丢弃陈旧 filter/sort key；切换策略时更新 `strategy=` 并重置 `page=1`；从趋势选股查看详情改进入 `/market?scope=market&symbol=...&source=selection&strategy=dsa_selector&returnTo=<当前 ScreenerPage URL>`（`buildMarketEntryFromScreener`），不再进入 `/stock/:symbol`；`/market` 和 `/stock/:symbol` 返回按钮优先使用 URL `returnTo` 参数，其次 `location.state.returnTo`，没有时按 `source` fallback 到 `/screener` 或 `/market?scope=watchlist`。

### 我的自选

- 展示股票、价格、涨跌幅、上下节点、POC、最近事件；
- 新增/删除/恢复后刷新服务器状态；
- 到期用户不加载列表，进入续期路径；
- 已存在、软删除、额度不足提示不同；
- 桌面端表格不显示每行状态栏，交易/非交易日状态统一在页眉用 `MonitorStatusBadge` 全局展示；
- 移动端卡片头不显示每行状态徽章；
- 数据列开启表头过滤（`filterable=true`）；
- 表格使用 `compact-table` 与趋势选股页字体/布局对齐；
- **指标数据源 = `stock_feature_snapshots.summary_payload`（不再走实时计算 fallback）**：
  - 后端 `GET /api/v1/watchlist/monitor-status` 响应每项包含 `calculation_status` 三态字段与 `metrics` 对象；
  - 前端按 `calculation_status` 三态展示：
    - `SUCCEEDED`：`metrics` 来自 `summary_payload`，正常渲染指标列；页眉可基于 `freshness_seconds` 显示数据新鲜度；
    - `WAITING_SNAPSHOT`：交易日已收盘但盘后 orchestrator 未生成 snapshot，`metrics` 为空 dict，前端指标列展示占位符（如 `—`）并提示「盘后快照生成中」；
    - `NO_SNAPSHOT`：非交易日或交易日内，`metrics` 为空 dict，前端指标列展示占位符（如 `—`），不提示错误；
  - 前端只渲染后端返回的 `metrics` 字段（`poc_price` / `nearest_node_above` / `nearest_node_below` / `daily_developing_swing_dir` / `m15_developing_swing_dir` / `current_price` / `change_pct` 等），**不重新计算**任何 DSA/BB/swing/temporal 因子；
  - 前端不调用 `MonitorSnapshotService` 实时计算路径，不依赖 `MonitorState.payload` fallback；
  - `MonitorEvaluation` 的 `evaluation_status` / `retry_count` / `error_code` / `source_bar_time` 仅用于展示评估状态徽章，不作为 metrics 数据源。

### 个股详情

- K 线、指标和截图共享行情快照；
- 展示 quote 的 `source`/`is_realtime`/`update_time`/`freshness_seconds`/`degraded`/`degraded_reason`，以及 bars 的 `data_source`/`as_of`/`is_partial`/`degraded`/`degraded_reason`；
- DSA 与 Node 图层可开关；
- **SQZMOM_LB 图层开关**：位于技术指标分组，默认关闭；开启后在 K 线下方新增独立副图，使用后端返回的 `val` 渲染 histogram、`bcolor` 渲染柱色、`scolor` 渲染 0 轴 squeeze marker；前端只消费后端 DTO，不重新计算 `val`/`sqzOn`/`sqzOff`/`noSqz`；API 未返回 `sqzmom_lb` 时页面不崩溃；
- 截图区设置 render-ready 标志；
- 按 timeframe 请求对应根数（1d=250、15m=4000、1h=1200、1w=260、1mo=120），与 Node Cluster / indicator_contract 对齐；`1m` 不在工具栏暴露；
- 个股详情 K 线实时状态以 `/bars` 返回的 `data_source/is_partial/last_live_bar_time/as_of` 为准；`mergeRealtimeQuoteIntoBars()` 只做兜底视觉增强，仅当 `quote.is_realtime === true && quote.source === "pytdx" && quote.freshness_seconds <= 60` 时才合并到最后一根 K 线，不参与指标计算，不替代后端 partial bar；daily_fallback / 延迟 / 降级行情只用于顶部报价 fallback/状态提示，不混入 `displayBars`；1d 保留日期语义并跨日追加实时 bar，intraday（15m/1h 等）使用 `quote.update_time`；`baseBars` 仍用于指标计算，避免污染算法输入；
- 顶部报价条优先使用实时报价，fallback 到最后一根 bar；
- **行情状态徽章**：根据 quote 来源/实时性/新鲜度/降级状态显示“实时行情 / 日线回退 / 数据延迟 / 行情降级”，并显示 `update_time`；不再固定显示“实时行情”；
- **K 线状态条**：显示 bars 的 `data_source`、`as_of`、`is_partial`、`degraded`、`degraded_reason`；交易时段 1d 返回 `is_partial=true` 时，状态条明确提示“盘中 partial bar（未收盘）”；
- **1d K 线实时性**：`include_realtime=true` 且交易时段时，1d bars 最后一根为当日 partial daily bar（由已完成 1m 聚合），收盘后自动恢复为完整日线；
- **轮询与性能**：`useRealtimeQuote` 交易时段 10s 轮询；`useBars`/`useIndicators` 交易时段 30s 轮询；均设置 `refetchIntervalInBackground: false`，页面 hidden 时停止后台轮询。
- **结构状态因子面板（V1.8）**：右侧 340px 新增 `StockStructuralStatePanel` 组件，双列布局（图表 + 因子面板）；面板含 5 张卡片（DSA 段质量/Swing 结构位置/成本节点/动量波动/成交参与），双周期 tabs（1d/15m）切换；V1.8 约 50 字段（含 dsa_segment 段收益/斜率/效率/段级成交量、swing_range/price_position/retracement、price_vs_poc_atr/value_area_position、distance_to_bb_*_atr/sqz_on/sqz_off/sqzmom_abs_percentile、current_vs_prev_volume_ratio、客观 relation 字段 primary_dir/secondary_dir/trend_alignment/primary_slope_atr 等）；前端只渲染后端 DTO，禁止重新计算因子；API 失败显示"暂无数据"，null 字段显示"-"，`degraded_reasons` 显示警告条；bool 字段（sqz_on/sqz_off）以"是/否"展示；数据源 `useStructuralFactors` hook → `GET /api/v1/instruments/{id}/structural-factors`，交易时段 60s 轮询。
  - **V1 默认隐藏**：面板默认不渲染，用户点击图表上方 toolbar 右侧「显示结构状态」开关后显示；`localStorage.showStructuralState` 持久化用户选择（默认 `null`/非 `"true"` 时隐藏）；按钮文案动态切换（隐藏时「显示结构状态」，显示时「隐藏结构状态」）；
  - **强制隐藏**：URL 参数 `?hideStructuralState=1` / `?capture=1` / `?capture=feishu` 任意一个命中即强制隐藏按钮和面板且禁用开关按钮（`toggleStructuralState` 回调 early return），忽略 `localStorage`；
  - **截图模式**：盘中监控截图发送飞书默认必须隐藏结构状态面板（`capture=feishu` 自动命中强制隐藏规则），截图默认只包含 K 线和基础信息；
  - **时序特征 V1 卡片**：`StockStructuralStatePanel` 末尾折叠卡片渲染 `temporal-features` API DTO（daily_context 9 字段 + m15_response 9 字段 + derived_relation 3 字段 + meta），前端只渲染 DTO 不重算，null 显示「-」，`warmup_notes`/`degraded_reasons` 有内容时显示提示；卡片受同一个结构状态开关控制（默认随面板隐藏，用户打开面板后显示）；
  - 窄屏（≤1250px）保持现有单列行为。
- **Swing 摘要卡 V1.10 developing swing 字段**（修复 active swing 仍不代表当前状态的问题）：
  - 摘要卡只显示 Developing swing 字段：`developing_swing_dir`、`developing_swing_high`、`developing_swing_low`、`bars_since_developing_swing_high`、`bars_since_developing_swing_low`、`price_position_in_developing_swing_0_1`、`distance_to_developing_swing_high_atr`、`distance_to_developing_swing_low_atr`；
  - active major leg 字段（`active_swing_high`/`active_swing_low`/`bars_since_active_swing_high`/`bars_since_active_swing_low`/`price_position_in_active_swing_0_1`/`distance_to_active_swing_high_atr`/`distance_to_active_swing_low_atr`）只放在 Swing 结构位置明细 JSON 中，不放在摘要卡；
  - confirmed pivot 字段（`confirmed_swing_high`/`confirmed_swing_low`/`bars_since_confirmed_swing_high`/`bars_since_confirmed_swing_low`/`price_position_in_confirmed_swing_raw`/`confirmed_swing_breakout_state`）只放在 Swing 结构位置明细 JSON 中，不放在摘要卡；
  - 禁止使用模糊标签「最近 swing high/low」「Swing 位置[0,1]」；摘要卡位置标签必须明确「Developing 位置[0,1]」，明细卡位置标签必须明确「Confirmed raw 位置」或「Active 位置[0,1]」；
  - 时序特征卡片中位置字段标签必须含 `developing` 或 `confirmed` 前缀，禁止使用无前缀的「Swing 位置」标签，禁止使用 `Active high`/`Active low` 作为主字段；
  - 摘要卡 `developing_swing_dir` 显示方向：`1` → "上涨段"，`-1` → "下跌段"，`None` → "fallback"。
- **capture 布局 V1.9 单列修复**（修复 capture 模式右侧空白问题）：
  - `isCaptureMode` 判定：URL 参数 `capture=feishu` 或 `capture=1` 或 `hideStructuralState=1` 任一命中即 `isCaptureMode=true`；
  - capture 模式下不渲染：结构状态开关按钮、右侧结构状态列（`StockStructuralStatePanel`）、Temporal Features 折叠卡片；
  - capture 模式 CSS：`.tv-side-column { display: none; }` 隐藏右侧列，`.tv-chart-column { width: 100%; }` 让图表列占满宽度；
  - capture 模式 `data-testid="tv-chart-column"` 必须挂在 `.tv-chart-column` 元素上（不再挂在 `.tv-content`），确保截图 testid 与单列布局对齐；
  - 非 capture 模式保持原双列布局（图表列 + 结构状态列）。
- **DSA overlay source mismatch 保护**（修复 15m/1h 误报"DSA 数据源不一致"）：
  - 图表在渲染 DSA overlay 前比较 `displayTimes` 与 `indicators.source_bar_times` 的 canonical key 交集；
  - canonical key 由 `frontend/src/utils/chartTime.ts::normalizeChartTime(time, timeframe)` 计算：15m/1h 用 `"YYYY-MM-DD HH:MM"`（提取前 16 字符），1d 用 `"YYYY-MM-DD"`；忽略 `+08:00` 时区后缀和秒数，使 K线（aware）与 `source_bar_times`（naive）产生相同 key；
  - 交集比例 `matched / klineKeys.size < 0.5` → 触发 "DSA 数据源不一致，已暂停渲染" banner，DSA overlay 不渲染，但 structural/temporal 因子卡片仍可显示；
  - 后端 `compute_source_bar_times` / `compute_source_bar_hash` 必须按当前 `timeframe` 使用对应 macd_bars，格式随 timeframe（1d=`YYYY-MM-DD`，15m/1h=`YYYY-MM-DDTHH:MM:SS`）；禁止 15m/1h source_bar_times 仍返回日线日期格式；
  - 15m/1h `bars.trade_time` 必须返回 aware ISO（`+08:00` 后缀），避免前端 `new Date("2026-07-06T15:00:00")` 在非亚洲时区浏览器中当作本地时间导致时区误判（如显示 `2026-07-07 03:00`）。
  - 15m/1h 时间轴刻度 `timeTicks` 使用 `Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai' })` 格式化，A 股交易时间正确显示，不应出现 `03:00` 这类非交易时段错误时间。
- **DSA overlay 周期策略（全周期支持，PR #32 + PR #33 前端硬编码清理）**：
  - DSA（Pine 标签 + VWAP）支持全周期渲染（1d/15m/1h/1w/1mo）；1d 是主结构锚，非 1d 是验证图层；
  - DSA overlay 按钮在所有周期可点击（不 disabled），`title` 由 `DSA_TITLE_HINT(timeframe)` 提供：1d 显示"DSA VWAP 日线结构锚。"，非 1d 显示"DSA VWAP 当前周期验证图层：用于核查该周期结构，不作为主趋势锚。"；
  - DSA toggle 全周期可切换（PR #33 修复 PR #32 遗留 `if (groupId === 'dsa' && timeframe !== '1d') return` 硬编码 disable），由 `shouldToggleDsa(groupId, isCaptureMode, captureLayers)` 集中决策；capture 模式仍锁定 DSA 不可关闭；
  - DSA 渲染决策由 `shouldRenderDsaLayer(layerId, layers, dsaSourceMismatch, timeframe)` 集中控制（PR #33 修复 PR #32 遗留 `if (layer.layer_id === 'dsa_vwap' && timeframe !== '1d') return` 硬编码 skip）：开关 / source mismatch / 周期支持三要素全周期判断，不再按 timeframe 跳过；
  - DSA 纵轴范围候选由 `shouldIncludeDsaInPriceRange(layerId, layers, timeframe)` 集中控制（PR #33 修复 PR #32 遗留 `if (layer.layer_id === 'dsa_vwap' && layers.dsa && timeframe === '1d')` 硬编码）：DSA 全周期参与 y-axis range，避免非 1d DSA 渲染后被轴范围挤掉；
  - DSA source mismatch 校验由 `shouldCheckDsaMismatch(timeframe)` 控制：全周期返回 `true`（DSA 全周期渲染，全部需校验 source 对齐）；
  - 仍保留 source mismatch 保护：匹配率 < 50% 时暂停渲染并提示，不允许无校验强画；
  - 右侧 `StockStructuralStatePanel` 仍可显示 daily DSA 背景和 m15 response（结构状态因子不受图层渲染影响）；
- **BB/MACD/SQZMOM overlay 跟随当前周期（PR #31/#32 + PR #33 前端硬编码清理）**：
  - 后端 `indicator_service._adapt_watchlist_bb` 在 15m/1h/1w/1mo 必须用 `macd_bars`（当前 timeframe bars）调用 `compute_bollinger(macd_bars, length=20, mult=2.0)` 重新计算 BB，禁止用日线阶梯线伪装成当前周期 BB；
  - 1w/1mo 不再移除 BB 字段（PR #32 修复：之前直接 `pop` BB 字段导致前端无 BB overlay）；
  - BB 渲染决策由 `shouldRenderBbLayer(layerId, layers, timeframe)` 集中控制（PR #33 修复 PR #32 遗留 `if (layer.layer_id === 'bb' && (timeframe === '1w' || timeframe === '1mo')) return` 硬编码 skip）：1w/1mo 不再被前端跳过，开关 / 周期支持两要素全周期判断；
  - BB overlay 时间轴必须用 `buildDisplayIndexMap` 按 canonical time 对齐，禁止尾部截取（tail slice）；
  - MACD / SQZMOM 同理：必须用 `macd_bars`（当前 timeframe）计算，不允许串日线；
- **?debugIndicatorAlignment=1 诊断工具（PR #31 + PR #34 DSA segment matched）**：
  - `StrategyChart` 支持通过 URL 参数 `?debugIndicatorAlignment=1` 输出 overlay 对齐诊断到 console.table；
  - 输出 `bars`（timeframe/count/first/last/canonical_first/canonical_last）、`dsa_mismatch`（check_enabled/mismatched/source_bar_hash/source_bar_times_count）、`indicators.layers`（layer_id/renderer/fields/time_count）；
  - `renderDsaPolyline` 额外调用 `computeDsaSegmentMatchStats(segments, displayTimes, timeframe)`（`frontend/src/utils/dsaSegmentMatch.ts`，PR #34）输出 `console.warn('[DSA segment match]', { timeframe, total, matched, ratio, degradedReason, firstSegTime, lastSegTime, firstDisplayTime, lastDisplayTime })`；
  - `degradedReason` 取值：`null`（正常匹配 ratio > 0.5）/ `no_segments`（segments 为空）/ `no_points`（segments 非空但 points 总数 0）/ `no_display_times`（displayTimes 空）/ `segment_time_no_match`（ratio ≤ 0.5，含 15m 旧 YYYY-MM-DD segment times 退化为日期场景）；
  - 默认不打印，不刷日志，仅用于诊断 15m/1h DSA 开关打开但 canvas 看不到线的问题。
- **DSA overlay 依赖 visual_segments 时间与 K 线 canonical 对齐（PR #34）**：
  - `dsa_polyline` renderer 不直接画 `dsa_vwap` 数组，而是画 `visual_segments.points`；
  - 每段 `points[].time` 经 `normalizeChartTime(pt.time, timeframe)` 产生 canonical key，再与 K 线 `displayTimes` 的 canonical key 集合匹配；
  - 后端 `format_dsa_time(x)` 必须按 timeframe 序列化（15m/1h 含 `THH:MM:SS`，1d/1w/1mo 为 `YYYY-MM-DD`），否则 15m/1h 下 `normalizeChartTime` 返回 `null`，renderer matched=0，开关打开也画不出线；
  - `computeDsaSegmentMatchStats` 提供独立的 matched ratio 计算（pure function），用于回归测试与 debug 诊断，不替代 source mismatch 校验（source mismatch 校验 top-level `source_bar_times`，segment matched 校验 `visual_segments.points.time`，两者互补）。

### K线主标题与截图页实时状态

- **K线主标题显示股票名称**：`StrategyChart` 主标题优先显示股票名称，格式 `名称（代码）`（如 `宁德时代（300750）`），名称缺失时回退为代码；`displayName` 由页面传入（`StockDetailPage` / `CaptureStockPage` 传 `inst.name || symbol`）。
- **截图页（CaptureStockPage）实时链路**：
  - 按 URL `timeframe` 初始化（无则 1d）；截图模式不锁定日线，支持 15m 等周期；
  - 请求 snapshot 携带 `force_refresh=1` 与 `source_bar_time=...`，确保指标/行情为当前实时数据，不复用旧指标；
  - 状态栏展示 K线 `data_source` / `is_partial` / `last_live_bar_time` 与 quote status，供人工核对图片为实时数据；
  - 后端 partial daily bar 为真（`/bars?timeframe=1d&include_realtime=true` 返回 `is_partial=true`），前端 `mergeRealtimeQuoteIntoBars()` 仅作兜底视觉增强，不替代后端 partial bar。

### 消息与飞书

- 消息显示股票、事件时间、详情入口；单只股票消息点击进入 `/market?symbol=...&event_id=...`（`buildMarketEntryFromMessage`），多只股票抽屉"查看"按钮同样进入 `/market?symbol=...&event_id=...`；无股票消息保持在消息页；
- 文字和图片显示独立状态；
- partial_failed 展示失败步骤和仅重试图片；
- Worker 不可用时不显示整体成功。

#### 飞书渠道配置（SettingsPage）

- 普通用户与管理员均可在 `/settings` 配置自己的 `feishu_platform_app` 渠道；
- 表单字段：配置名称、App ID、App Secret、接收者 ID、接收者类型（`user_id`/`open_id`/`chat_id`/`union_id`），平台将 `receive_id_type` 原样透传给飞书接口；
- 保存按钮文案为「保存配置」，保存后渠道状态为 `pending`（未验证），不会自动变为 `active`；
- 渠道卡片对所有人显示「发送测试消息」（`active` 状态）或「测试并启用」（`pending` 状态），调用 `POST /notification-channels/{id}/test`；
- 测试成功 toast 显示「测试成功，飞书渠道已启用」，并刷新渠道列表使状态变为 `active`；测试失败展示后端返回的 `delivery.error_code` / `error_message`；
- 「管理员实测最近事件」按钮仅管理员可见，调用 `POST /notification-channels/{channel_id}/test-latest-event`，普通用户点击会收到 403 并提示使用普通测试接口。

### 管理页面

- 所有按钮调用真实 API；
- 启用、禁用、授予、续期、撤销、改套餐、重试都有 loading/error/refresh；
- 禁止用本地 state 或 Toast 模拟成功；
- AdminJobsPage 提供 "Worker 心跳" Tab，展示 worker_name/instance_id/status/health_state/heartbeat_at/age/build_sha/current_job_id，10 秒轮询，health_state 由后端计算（fresh/stale/stopped）。

## 4. UI 状态

所有页面统一支持：loading、refreshing、empty、error、partial、permission、success。

行情、策略结果、任务和消息页面必须显示真实数据时间。图表不连虚假线，partial Bar 有视觉区别。

## 5. 视觉原则

深色、专业、研究型；不夸张承诺收益；上涨红、下跌绿，同时用文字或形状辅助，避免只依赖颜色。图表提供文本摘要，可访问性不能丢。
