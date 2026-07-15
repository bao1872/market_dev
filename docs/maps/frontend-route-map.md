# Frontend Route Map

> 事实源：`frontend/src/App.tsx`。

## 1. 路由表

| 路由 | 页面 | 守卫 | 说明 |
|---|---|---|---|
| `/` | `LandingPage` | Public | 门户页 lazy load |
| `/login` | `LoginPage` | Public | 登录/邀请码注册 |
| `/subscription-expired` | `SubscriptionExpiredPage` | Authenticated | canonical 到期/续期页 |
| `/membership-expired` | redirect | Public | 兼容路由 |
| `/capture/stock/:symbol` | `CaptureStockPage` | Capture Token | 专用截图路由，不走任何壳层（UserAppShell/AdminAppShell 均不渲染） |
| `/market` | `MarketWorkspacePage` | Subscriber/Admin | 行情工作区（**无 K 线**）：`MarketToolbar`（同一行布局：scope 分段按钮 → 搜索框 → 行业筛选 → 概念筛选，CHANGE-20260713-005/006；`boards.available=false` 时行业/概念输入禁用但显示，placeholder "板块数据暂不可用"）+ `StrategyDataTable`（DSA 列表，复用 `getTrendSelectionColumns` 列定义，数据来自 `usePublishedRuns` + `useStrategyRunResults` 最新 published DSA run；`scope=market`→`universe=all`，`scope=watchlist`→`universe=watchlist`；`searchable={false}` + `externalKeyword`/`onKeywordChange` 受控模式，CHANGE-20260713-005；列设置支持显示/隐藏/调整顺序/恢复默认/刷新保留，配置复用 `/me/table-view-presets`；action 列改名"自选"显示加入/移除自选按钮，股票名称为 `<a>` 链接进入 `/stock/:symbol?returnTo=`）+ 可收起 `EventStatePanel`（展示 MACD/Evidence/`state.evidence`，使用 `useStockContext` → `GET /api/v1/stocks/{symbol}/context`）；URL 状态 `scope=watchlist\|market&selected=xxx&industry=xxx&concept=xxx`（MarketWorkspacePage via `marketWorkspaceUrlState` 管理）+ `sort/dir/keyword/filters/page/page_size`（StrategyDataTable 内置 `screenerUrlState` 管理）；industry/concept 切 scope/搜索/排序/分页时保留，改变板块筛选重置 page=1；`StrategyDataTable` 通过 `externalIndustry`/`onIndustryChange`/`externalConcept`/`onConceptChange` 受控 props 接收（与 `externalKeyword` 同模式）；preset 配置保存并恢复 industry/concept；批次信息 admin-only 默认折叠（CHANGE-20260713-005）；**默认不显示**形态状态/DSA状态/最近事件列（事件只在 `EventStatePanel` 按需展开时加载）；**右栏（CHANGE-20260713-010）**：`MarketRightPanel` 组合 `MiniKlineCard`（顶部，lightweight-charts v4 createChart + addCandlestickSeries）+ `EventStatePanel`（底部，仍只负责 StockContext）；`useMiniKlineData` 定义 `BARS_COUNT = {1d: 80, 1w: 60, 1mo: 48}`，`refetchInterval: false`；三按钮 1d/1w/1mo 默认 1d；首次默认收起（`panji:market-right-panel-collapsed:v1`），收起时 bars/context 请求均为 0；展开只请求活动周期不预取三周期；chart 实例仅创建一次（`useEffect []`），ResizeObserver + 卸载清理（`disconnect()` + `chart.remove()` + ref 清空），timeframe 独立于 symbol；**MiniKline viewport 彻底重写（CHANGE-20260715-001 → CHANGE-20260715-002）**：`MiniKlineCard` 使用纯函数 `computeMiniKlineViewport`（`frontend/src/features/market-workspace/miniKlineViewport.ts`）替代 `fitContent`；目标根数按周期固定（15m=48、60m=44、日=40、周=36、月=30）；`barSpacing = clamp(contentWidth/visibleBars, 5.5, 8)`；左侧 1-2 根留白 `from=max(-2, n-visible-1)`；右侧 3 根留白 `to=n-1+3`；禁止 `fitContent`/`resetTimeScale`/`scrollToRealTime`；`autoscaleInfoProvider` 扩展价格范围（上 12% 下 15%）；`rightPriceScale` `autoScale=true` + `scaleMargins {0.08,0.08}` + `minimumWidth=56`；图表容器高度固定 190px；切周期不复用旧 logical range；**股票名称筛选（CHANGE-20260713-010）**：列定义 `filterAlias?: 'keyword'`，stock 列设置 `filterAlias='keyword'` + `filterable=true`；`KeywordFilterPopover` 双向同步 `setGlobalQuery` + `onKeywordChange`，清空同步；`isKeyword` flag 区分不进入 `filters` state；URL sync `replace: true` + `skipNextUrlSyncRef` 避免 URL 循环；stock 列不进入 `metric_filters`；**Excel 导出（CHANGE-20260713-010）**：`MarketWorkspacePage.handleExport` 通过 `ExportContext` 暴露给 `StrategyDataTable`，复用 `convertFiltersToMetricFilters`（与 `buildStrategyResultQueryParams` 同源），调用 `POST /api/v1/strategy-runs/{run_id}/results/export`，下载真实 .xlsx；UserAppShell 壳层 |
| `/screener` | redirect | — | 兼容重定向 → `/market`（无页面加载）；趋势选股表格能力由 `/market` 的 `StrategyDataTable` 承载 |
| `/watchlist` | redirect | — | 兼容重定向 → `/market?scope=watchlist` |
| `/stock/:symbol` | `StockDetailPage` | Subscriber/Admin | **唯一个股详情和 K 线入口**（路由适配器，阶段四重构）；使用共享 `useStockResearchData` + `StockResearchWorkspace` 渲染图表区，不再独立调用 useBars/useIndicators/useRealtimeQuote/useInstrumentEvents；详情页专属能力拆到 `useStockDetailActions`（自选/上下切换/memo）和 `useStockDetailFeishu`（飞书截图/轮询/超时）；timeframe 从 URL 解析（单一真源），工具栏切换写回 URL；按 timeframe 请求对应根数（1d=250/15m=4000/1h=1200/1w=260/1mo=120，`1m` 不暴露）；**图表图层 8 键（CHANGE-20260715-001，CHANGE-20260715-002 Pine parity）**：`ChartLayerVisibility` 含 `trend/node/boll/volume/macd/sqzmom/breakout/smc`（localStorage `panji:chart-layer-visibility:v2`）；`smc` 图层 name="智能资金"、kind="main"、默认关闭；`/stock/:symbol` 请求 indicators 时携带 `include_smc=true`，前端只消费后端 `data.smc` DTO 渲染市场结构关键点位（不重算）；SMC 算法真源为 `smc_pine_core.py`（Pine 语义核心，生产+测试共用），`smc_indicator.py` 为薄包装委托层；Pine 原语（`pine_rma` Wilder / `pine_atr` / `pine_cumulative_mean_range` bar0=NaN / `pine_highest/lowest` / `pine_crossover/crossunder`）；events 使用 `internal: bool` 替代旧 `kind` 字段；**SMC renderer 对齐 Pine**：internal=虚线 `[4,3]`/tiny 8px、swing=实线/small 11px；标签中点 `(x1+x2)/2`+`'center'`；trailing 文案"强高/弱高/强低/弱低"；OB 半透明 box（active 0.12、mitigated 0.05）；颜色多头红 `#FF4D4F`、空头绿 `#22C55E`；Historical 全事件；`smcToDisplay` 通过时间匹配自动过滤展示区外事件（后端 SMC 输出不截断）；**FVG 完全排除**（不计算、不返回、不缓存、不渲染）；SMC 只进入个股详情指标链，不进入 `/market` 小 K 线；用户 Pine 代码（`ref/smc_ref.txt`）为原创作品并授权盘迹使用；**Pine golden fixture 状态 PENDING**（等待 TradingView 导出）；**事件状态面板**：`eventPanelCollapsed` 首次默认收起（`true`），localStorage key `panji:event-panel:v1` 持久化用户选择；面板展示 MACD/Evidence/`state.evidence`；**K 线实时状态以 `/bars` 返回的 `data_source/is_partial/last_live_bar_time/as_of` 为准**；`mergeRealtimeQuoteIntoBars()` 只做兜底视觉增强，**后端已返回 partial bar 时不得用 quote 覆盖**；顶部报价条与状态徽章根据 quote 来源/实时性/新鲜度/降级显示"实时行情 / 行情回退 / 数据延迟 / 行情降级"（禁止非 1d 周期显示"日线回退"）；K 线状态条展示 bars 的 `data_source`/`is_partial`/`degraded`/`degraded_reason`，partial 文案含当前周期；`?hideStructuralState=1`/`?capture=feishu` 强制隐藏面板；截图模式（`capture=feishu`）默认隐藏面板；**返回按钮**：优先 URL `returnTo` 参数，其次 `location.state.returnTo`，没有时 fallback 到 `/market?scope=watchlist`；**来源上下文（CHANGE-20260713-009）**：`MarketWorkspacePage.handleNavigateToStock` 按 scope 传递 `source`/`strategy`（`scope=market` → `source=selection&strategy=dsa_selector`；`scope=watchlist` → `source=watchlist&strategy=watchlist_monitor`）+ 完整 `returnTo` URL；详情页左栏 `useStockDetailActions` 复用 DSA published results 链（`usePublishedRuns` + `useStrategyRunResults` + `adaptStrategyResultToTrendRow` + `getStockDisplay`），不再使用 `useMarketStocks`；`decodeMarketListContext`/`buildStrategyResultQueryParams` 共享纯函数（`marketWorkspaceUrlState.ts`）由 `MarketWorkspacePage` 和 `useStockDetailActions` 共用避免 filter drift；`sourceListKind` 由 `marketContext.scope` 派生（`market`→"行情来源"，`watchlist`→"自选来源"）；**报价条 StockQuoteStrip（CHANGE-20260713-010）**：展示 8 项指标（现价/涨跌/开盘/最高/最低/成交额/总市值/流通市值），总市值与流通市值放在成交额右侧；`formatMarketCap(v)` 区分万/亿/万亿元，空值显示 `--`；`QuoteMetric` 子组件统一渲染单格；`PriceSummary` 接口含 `totalMarketCap`/`floatMarketCap`/`marketCapAsOf` 字段（来自 `/quote` 返回的 `total_market_cap`/`float_market_cap`/`market_cap_as_of`/`market_cap_degraded_reason`，缺失时 `degraded_reason="market_cap_data_unavailable"` 不展示空指标）；UserAppShell 壳层 |
| `/settings` | `SettingsPage` | Authenticated | 设置与通知渠道；飞书配置表单支持 `user_id`/`open_id`/`chat_id`/`union_id` 作为 `receive_id_type`，保存按钮文案「保存配置」，保存后状态为 `pending`；渠道卡片对所有人显示「发送测试消息」/「测试并启用」，调用 `POST /notification-channels/{id}/test`，成功后刷新列表；「管理员实测最近事件」按钮仅管理员可见，调用 admin-only 的 `POST /notification-channels/{channel_id}/test-latest-event`；UserAppShell 壳层 |
| `/messages` | `MessagesPage` | Authenticated | 历史消息；单只股票消息点击进入 `/stock/:symbol?event_id=...`（K 线详情 + 事件上下文），多只股票抽屉"查看"按钮同样进入 `/stock/:symbol?event_id=...`；无股票消息保持在消息页；UserAppShell 壳层 |
| `/overview` | redirect | — | 兼容重定向 → `/market` |
| `/admin`, `/admin/overview` | `AdminIndexPage` | Admin | 管理总览；AdminAppShell 壳层 |
| `/admin/users` | `AdminUsersPage` | Admin | 用户/订阅/邀请码；AdminAppShell 壳层 |
| `/admin/beta-applications` | `AdminBetaApplicationsPage` | Admin | 内测申请；AdminAppShell 壳层 |
| `/admin/strategies` | redirect | Admin | redirect-only → `/admin/after-close`（无页面加载，`AdminStrategiesPage.tsx` 已删除） |
| `/admin/jobs` | `AdminJobsPage` | Admin | 定时任务/策略计算/Worker 心跳（worker_heartbeats 实时视图，health_state fresh/stale/stopped）/投递；AdminAppShell 壳层 |
| `/admin/after-close` | `AdminAfterClosePipelinePage` | Admin | 盘后流水线详情：8 步骤时间线（refreshing_daily→checking_coverage→creating_dsa→waiting_dsa_worker→quality_gate→feature_snapshot→publishing→watchlist_ready）+ 数据新鲜度 + 编排状态详情 + 最近 20 次运行列表 + 事件日志抽屉（100 events）；running 10s 轮询、非 running 60s 轮询、页面不可见暂停；AdminAppShell 壳层 |
| `/admin/stock-debug`, `/admin/stock-debug/:symbol` | `AdminStockDebugPage` | Admin | 管理员个股调试：复用 `MarketInstrumentPane`/`useStockResearchData`/`StockResearchWorkspace`/`useAdminStockDebug`（含原始 payload 的管理员调试接口）；原始 factor/feature/JSON 仅在此路由展示；AdminAppShell 壳层 |

## 2. 守卫语义

- `ProtectedLayout`：检查 auth store + localStorage access token，并重新调用 `/me/access`；不再固定渲染壳层，只返回 `<Outlet/>`；
- `UserAppShell`：普通用户布局壳（顶栏品牌 + 一级导航行情/趋势选股 + 账户菜单；无左侧栏）；
- `AdminAppShell`：管理员独立布局壳（侧栏管理导航 + 账户菜单）；**响应式布局（CHANGE-20260714-001）**：桌面端（≥1024px）显示左侧管理导航侧栏；小屏（<1024px）隐藏侧栏，顶栏左侧显示"← 返回行情"按钮链接到 `/market`（与 `getAccountMenuItemsForVariant(_, 'admin')` "返回行情"入口一致，CHANGE-20260713-007），管理导航收起为顶部菜单或抽屉；
- `SubscriberRoute`：admin 直接通过；普通用户要求 `subscription_active`；
- `AdminRoute`：要求 `is_admin === true`，非 admin 重定向到 `/market`；**权限真源（CHANGE-20260713-007）**：以 `user.is_admin` 为唯一权限真源（不依赖任何其他角色/字段判断），新增 `accessLoading` 状态防止 auth hydration 未完成时提前判定 false（刷新页面后 access store 重新拉取 `/me/access` 期间显示 loading，避免 access 未就绪时被误判为非 admin 重定向到 `/market`）；
- `AccountMenu`（`frontend/src/components/AccountMenu.tsx`）：**管理员入口契约（CHANGE-20260713-007）**——`getAccountMenuItemsForVariant(isAdmin, 'user')` 当 `is_admin=true` 时显示"管理后台"链接到 `/admin`；普通用户（`is_admin=false`）DOM **完全不渲染**管理后台入口（不是 CSS 隐藏）；`getAccountMenuItemsForVariant(_, 'admin')` 显示"返回行情"链接到 `/market`，不重复显示"管理后台"（避免管理员在 `AdminAppShell` 内重复入口）；
- Capture 路由不经过 ProtectedLayout/SubscriberRoute/UserAppShell/AdminAppShell，只使用 capture client；
- 导航/路由常量集中于 `frontend/src/navigation/appNavigation.ts`。

## 3. 前端高风险点

| 风险 | 规则 |
|---|---|
| 本地缓存过期 | 页面刷新必须 revalidate access |
| Capture token 污染登录态 | 使用独立 `CAPTURE_TOKEN_KEY`，不得写 `ACCESS_TOKEN_KEY` |
| 管理按钮假成功 | 必须调用真实 API 并刷新服务器状态 |
| 前端复制权限 | 后端 403 是最终事实 |
| 消息状态伪成功 | card/image 独立显示，partial_failed 不能显示普通 success |

## 4. 修改建议

页面 UI 改动必须同步：

```text
current/04-frontend-ux.md
maps/frontend-route-map.md
相关 contract/frontend tests
CHANGE
```
