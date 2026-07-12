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
| `/market` | `MarketWorkspacePage` | Subscriber/Admin | 行情工作区（**无 K 线**）：工具栏 + 分页表格 `MarketStockTable`（名称列 sticky、字体主信息≥14px/辅助≥12px、来源徽章基于 `sourceListKind`）+ 可收起 `EventStatePanel`（展示 MACD/Evidence/`state.evidence`，使用 `useStockContext` → `GET /api/v1/stocks/{symbol}/context`）；URL 状态 `scope=watchlist\|market&symbol=xxx&source=...&strategy=...&event_id=...&returnTo=...`（**无 `debug` 参数**，`debug` 不在用户路由 URL 契约中）；scope=watchlist 用 `useWatchlistMonitorStatus`，scope=market 用 `useInstruments` 搜索（≥2字符，限50条）；UserAppShell 壳层 |
| `/screener` | redirect | — | 兼容重定向 → `/market`（无页面加载）；趋势选股表格能力由 `/market` 的 `MarketStockTable` 承载 |
| `/watchlist` | redirect | — | 兼容重定向 → `/market?scope=watchlist` |
| `/stock/:symbol` | `StockDetailPage` | Subscriber/Admin | **唯一个股详情和 K 线入口**（路由适配器，阶段四重构）；使用共享 `useStockResearchData` + `StockResearchWorkspace` 渲染图表区，不再独立调用 useBars/useIndicators/useRealtimeQuote/useInstrumentEvents；详情页专属能力拆到 `useStockDetailActions`（自选/上下切换/memo）和 `useStockDetailFeishu`（飞书截图/轮询/超时）；timeframe 从 URL 解析（单一真源），工具栏切换写回 URL；按 timeframe 请求对应根数（1d=250/15m=4000/1h=1200/1w=260/1mo=120，`1m` 不暴露）；**事件状态面板**：`eventPanelCollapsed` 默认展开（`false`），localStorage key `panji:event-panel:v1` 持久化；面板展示 MACD/Evidence/`state.evidence`；**K 线实时状态以 `/bars` 返回的 `data_source/is_partial/last_live_bar_time/as_of` 为准**；`mergeRealtimeQuoteIntoBars()` 只做兜底视觉增强，**后端已返回 partial bar 时不得用 quote 覆盖**；顶部报价条与状态徽章根据 quote 来源/实时性/新鲜度/降级显示"实时行情 / 行情回退 / 数据延迟 / 行情降级"（禁止非 1d 周期显示"日线回退"）；K 线状态条展示 bars 的 `data_source`/`is_partial`/`degraded`/`degraded_reason`，partial 文案含当前周期；V1.8 右侧 `StockStructuralStatePanel` 结构状态因子面板（默认隐藏，toolbar 开关，localStorage 持久化，`?hideStructuralState=1`/`?capture=feishu` 强制隐藏）；截图模式（`capture=feishu`）默认隐藏面板；**返回按钮**：优先 URL `returnTo` 参数，其次 `location.state.returnTo`，没有时按 `source` fallback 到 `/screener` 或 `/market?scope=watchlist`；UserAppShell 壳层 |
| `/settings` | `SettingsPage` | Authenticated | 设置与通知渠道；飞书配置表单支持 `user_id`/`open_id`/`chat_id`/`union_id` 作为 `receive_id_type`，保存按钮文案「保存配置」，保存后状态为 `pending`；渠道卡片对所有人显示「发送测试消息」/「测试并启用」，调用 `POST /notification-channels/{id}/test`，成功后刷新列表；「管理员实测最近事件」按钮仅管理员可见，调用 admin-only 的 `POST /notification-channels/{channel_id}/test-latest-event`；UserAppShell 壳层 |
| `/messages` | `MessagesPage` | Authenticated | 历史消息；单只股票消息点击进入 `/market?symbol=...&event_id=...`（`buildMarketEntryFromMessage`），多只股票抽屉"查看"按钮同样进入 `/market?symbol=...&event_id=...`；无股票消息保持在消息页；UserAppShell 壳层 |
| `/overview` | redirect | — | 兼容重定向 → `/market` |
| `/admin`, `/admin/overview` | `AdminIndexPage` | Admin | 管理总览；AdminAppShell 壳层 |
| `/admin/users` | `AdminUsersPage` | Admin | 用户/订阅/邀请码；AdminAppShell 壳层 |
| `/admin/beta-applications` | `AdminBetaApplicationsPage` | Admin | 内测申请；AdminAppShell 壳层 |
| `/admin/strategies` | redirect | Admin | redirect-only → `/admin/after-close`（无页面加载，`AdminStrategiesPage.tsx` 已删除） |
| `/admin/jobs` | `AdminJobsPage` | Admin | 定时任务/策略计算/Worker 心跳（worker_heartbeats 实时视图，health_state fresh/stale/stopped）/投递；AdminAppShell 壳层 |
| `/admin/after-close` | `AdminAfterClosePipelinePage` | Admin | 盘后流水线详情：8 步骤时间线（refreshing_daily→checking_coverage→creating_dsa→waiting_dsa_worker→quality_gate→feature_snapshot→publishing→watchlist_ready）+ 数据新鲜度 + 编排状态详情 + 最近 20 次运行列表 + 事件日志抽屉（100 events）；running 10s 轮询、非 running 60s 轮询、页面不可见暂停；AdminAppShell 壳层 |
| `/admin/stock-debug`, `/admin/stock-debug/:symbol` | `AdminStockDebugPage` | Admin | 管理员个股调试：复用 `MarketInstrumentPane`/`useStockResearchData`/`StockResearchWorkspace`/`useResearchContext`/`AdminFactorDebugPanel`/`StockStructuralStatePanel`（debug=true）；原始 factor/feature/JSON 仅在此路由展示；AdminAppShell 壳层 |

## 2. 守卫语义

- `ProtectedLayout`：检查 auth store + localStorage access token，并重新调用 `/me/access`；不再固定渲染壳层，只返回 `<Outlet/>`；
- `UserAppShell`：普通用户布局壳（顶栏品牌 + 一级导航行情/趋势选股 + 账户菜单；无左侧栏）；
- `AdminAppShell`：管理员独立布局壳（侧栏管理导航 + 账户菜单）；
- `SubscriberRoute`：admin 直接通过；普通用户要求 `subscription_active`；
- `AdminRoute`：要求 `is_admin === true`，非 admin 重定向到 `/market`；
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
