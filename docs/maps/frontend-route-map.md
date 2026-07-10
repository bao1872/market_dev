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
| `/market` | `WatchlistPage` | Subscriber/Admin | 行情（阶段二复用 WatchlistPage 作为阶段性真实内容）；UserAppShell 壳层 |
| `/screener` | `ScreenerPage` | Subscriber/Admin | 趋势选股；行 key 使用 `instrumentId`（不依赖 `result_id`），"筛选结果" 标签替代原 "命中"，全量 universe 展示含 skipped/failed 行。succeeded 行显示 35 个 DSA 指标（后端 `(run_id, instrument_id)` JOIN，绕过 result_id 未回填问题，见 ALIGN-033），skipped/failed 行指标列显示 "-"；**批量加入自选**：`handleBatchAdd` 按 `r.instrumentId` 匹配 `selectedKeys`（禁止用 `resultId`），对 `instrumentId` 去重，空选 toast 提示，成功/失败 toast 真实反映数量；**当日涨跌幅独立列**：`change_pct` 独立列（key=change_pct, dataType=percent, sortable, filterable, width≈86），render 用 `fmtChange` + A股涨红跌绿，后端 `dsa_selector.yaml` 已支持 filterable/sortable；**表格视图配置 preset**：`StrategyDataTable` 元信息栏 `TablePresetMenu` 组件（保存/应用/覆盖/重命名/设默认/删除），默认 preset 自动应用（useRef 防重复），config 只保存 keyword/sort/filters/hiddenColumns/pageSize（禁止 selectedKeys/page/activeRunId/rows），preset API `/me/table-view-presets` 按 JWT user_id 隔离；**sticky 表头与选择列**：`thead th` sticky top:0 z-index:4，sticky 列 z-index:3，角落单元格 z-index:5，选择列 sticky left:0，首列偏移 40px；`tableId="screener"` + `strategyKey={activeStrategyKey}` 分离传递用于 preset 隔离；**URL 状态持久化**：策略 key、keyword、sort、filters、page、pageSize 同步到 URL query，filters 用 compact JSON 只保存 key/op/value/value2，decode 丢弃陈旧列 key，切换策略时重置 page=1；进入个股详情时把当前 `location.pathname + location.search` 作为 `state.returnTo` 传入；UserAppShell 壳层 |
| `/watchlist` | redirect | — | 兼容重定向 → `/market?scope=watchlist` |
| `/stock/:symbol` | `StockDetailPage` | Subscriber/Admin | 个股详情；按 timeframe 请求对应根数（1d=250/15m=4000/1h=1200/1w=260/1mo=120，`1m` 不暴露）；**K 线实时状态以 `/bars` 返回的 `data_source/is_partial/last_live_bar_time/as_of` 为准**，交易时段 1d 返回 partial daily bar（`is_partial=true`），收盘后/非交易时段 `is_partial=false`；`mergeRealtimeQuoteIntoBars()` 只做兜底视觉增强，**后端已返回 1d partial bar 时不得用 quote 覆盖**，仅当 `timeframe==='1d' && barsQuery.data?.is_partial !== true && quote.is_realtime === true && quote.source === "pytdx" && quote.freshness_seconds <= 60` 时才合并到最后一根 K 线，不参与指标计算，不替代后端 partial bar；顶部报价条与状态徽章根据 quote 来源/实时性/新鲜度/降级显示“实时行情 / 日线回退 / 数据延迟 / 行情降级”和 `update_time`，不再固定显示“实时行情”；K 线状态条展示 bars 的 `data_source`/`as_of`/`is_partial`/`degraded`/`degraded_reason`；quote 10s 轮询、bars/indicators 30s 轮询，页面 hidden 时停止后台轮询；新增 SQZMOM_LB 图层开关（默认关闭），开启后在 K 线下方显示独立副图，前端只消费后端 DTO 不重新计算；V1.8 右侧 340px 新增 `StockStructuralStatePanel` 结构状态因子面板（双周期 tabs + 5 张卡片 + 约 50 字段，含 dsa_segment 段分析/swing_position/cost_position/volatility_momentum/participation/客观 relation），bool 字段以"是/否"展示；**V1 默认隐藏**：面板默认不渲染，用户点击图表上方 toolbar 右侧「显示结构状态」按钮显示，文案动态切换（显示/隐藏结构状态），localStorage 持久化；**强制隐藏**：`?hideStructuralState=1` / `?capture=1` / `?capture=feishu` 强制隐藏按钮和面板且禁用开关按钮；截图模式（`capture=feishu`）默认隐藏面板，仅渲染 K 线和基础信息；面板末尾含「时序特征 V1」折叠卡片渲染 `temporal-features` API DTO（daily_context 9 + m15_response 9 + derived_relation 3 + meta），null 显示「-」；窄屏（≤1250px）保持单列；**返回按钮**：优先使用 `location.state.returnTo`（从趋势选股带入），没有时按 `source` fallback 到 `/screener` 或 `/market`，加载中/未找到/正常页均走同一 `handleBack()`；UserAppShell 壳层 |
| `/settings` | `SettingsPage` | Authenticated | 设置与通知渠道；飞书配置表单支持 `user_id`/`open_id`/`chat_id`/`union_id` 作为 `receive_id_type`，保存按钮文案「保存配置」，保存后状态为 `pending`；渠道卡片对所有人显示「发送测试消息」/「测试并启用」，调用 `POST /notification-channels/{id}/test`，成功后刷新列表；「管理员实测最近事件」按钮仅管理员可见，调用 admin-only 的 `POST /notification-channels/{channel_id}/test-latest-event`；UserAppShell 壳层 |
| `/messages` | `MessagesPage` | Authenticated | 历史消息；UserAppShell 壳层 |
| `/overview` | redirect | — | 兼容重定向 → `/market` |
| `/admin`, `/admin/overview` | `AdminIndexPage` | Admin | 管理总览；AdminAppShell 壳层 |
| `/admin/users` | `AdminUsersPage` | Admin | 用户/订阅/邀请码；AdminAppShell 壳层 |
| `/admin/beta-applications` | `AdminBetaApplicationsPage` | Admin | 内测申请；AdminAppShell 壳层 |
| `/admin/strategies` | `AdminStrategiesPage` | Admin | 策略管理；AdminAppShell 壳层 |
| `/admin/jobs` | `AdminJobsPage` | Admin | 定时任务/策略计算/Worker 心跳（worker_heartbeats 实时视图，health_state fresh/stale/stopped）/投递；AdminAppShell 壳层 |
| `/admin/after-close` | `AdminAfterClosePipelinePage` | Admin | 盘后流水线详情：8 步骤时间线（refreshing_daily→checking_coverage→creating_dsa→waiting_dsa_worker→quality_gate→feature_snapshot→publishing→watchlist_ready）+ 数据新鲜度 + 编排状态详情 + 最近 20 次运行列表 + 事件日志抽屉（100 events）；running 10s 轮询、非 running 60s 轮询、页面不可见暂停；AdminAppShell 壳层 |

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
