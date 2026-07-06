# Frontend Route Map

> 事实源：`frontend/src/App.tsx`。

## 1. 路由表

| 路由 | 页面 | 守卫 | 说明 |
|---|---|---|---|
| `/` | `LandingPage` | Public | 门户页 lazy load |
| `/login` | `LoginPage` | Public | 登录/邀请码注册 |
| `/subscription-expired` | `SubscriptionExpiredPage` | Authenticated | canonical 到期/续期页 |
| `/membership-expired` | redirect | Public | 兼容路由 |
| `/capture/stock/:symbol` | `CaptureStockPage` | Capture Token | 专用截图路由，不走 AppShell |
| `/overview` | `IndexPage` | Subscriber/Admin | 服务总览 |
| `/screener` | `ScreenerPage` | Subscriber/Admin | 趋势选股；行 key 使用 `instrumentId`（不依赖 `result_id`），"筛选结果" 标签替代原 "命中"，全量 universe 展示含 skipped/failed 行。succeeded 行显示 35 个 DSA 指标（后端 `(run_id, instrument_id)` JOIN，绕过 result_id 未回填问题，见 ALIGN-033），skipped/failed 行指标列显示 "-" |
| `/watchlist` | `WatchlistPage` | Subscriber/Admin | 我的自选；页眉全局展示市场状态，桌面表格/移动卡片不显示每行状态栏，数据列可表头过滤，表格使用 `compact-table` 与趋势选股页对齐 |
| `/stock/:symbol` | `StockDetailPage` | Subscriber/Admin | 个股详情；按 timeframe 请求对应根数（1d=250/15m=4000/1h=1200/1w=260/1mo=120，`1m` 不暴露），K 线通过 `mergeRealtimeQuoteIntoBars` 合并实时行情显示，但仅当 `quote.is_realtime === true && quote.source === "pytdx" && quote.freshness_seconds <= 60` 时才合并；1d 保留日期语义、intraday 使用 `quote.update_time`；顶部报价条与状态徽章根据 quote 来源/实时性/新鲜度/降级显示“实时行情 / 日线回退 / 数据延迟 / 行情降级”和 `update_time`，不再固定显示“实时行情”；K 线状态条展示 bars 的 `data_source`/`as_of`/`is_partial`/`degraded`/`degraded_reason`；quote 10s 轮询、bars/indicators 30s 轮询，页面 hidden 时停止后台轮询；新增 SQZMOM_LB 图层开关（默认关闭），开启后在 K 线下方显示独立副图，前端只消费后端 DTO 不重新计算；V1.8 右侧 340px 新增 `StockStructuralStatePanel` 结构状态因子面板（双周期 tabs + 5 张卡片 + 约 50 字段，含 dsa_segment 段分析/swing_position/cost_position/volatility_momentum/participation/客观 relation），bool 字段以"是/否"展示；**V1 默认隐藏**：面板默认不渲染，用户点击图表上方 toolbar 右侧「显示结构状态」按钮显示，文案动态切换（显示/隐藏结构状态），localStorage 持久化；**强制隐藏**：`?hideStructuralState=1` / `?capture=1` / `?capture=feishu` 强制隐藏按钮和面板且禁用开关按钮；截图模式（`capture=feishu`）默认隐藏面板，仅渲染 K 线和基础信息；面板末尾含「时序特征 V1」折叠卡片渲染 `temporal-features` API DTO（daily_context 9 + m15_response 9 + derived_relation 3 + meta），null 显示「-」；窄屏（≤1250px）保持单列 |
| `/settings` | `SettingsPage` | Authenticated | 设置与通知渠道 |
| `/messages` | `MessagesPage` | Authenticated | 历史消息 |
| `/admin`, `/admin/overview` | `AdminIndexPage` | Admin | 管理总览 |
| `/admin/users` | `AdminUsersPage` | Admin | 用户/订阅/邀请码 |
| `/admin/beta-applications` | `AdminBetaApplicationsPage` | Admin | 内测申请 |
| `/admin/strategies` | `AdminStrategiesPage` | Admin | 策略管理 |
| `/admin/jobs` | `AdminJobsPage` | Admin | 定时任务/策略计算/Worker 心跳（worker_heartbeats 实时视图，health_state fresh/stale/stopped）/投递 |

## 2. 守卫语义

- `ProtectedLayout`：检查 auth store + localStorage access token，并重新调用 `/me/access`；
- `SubscriberRoute`：admin 直接通过；普通用户要求 `subscription_active`；
- `AdminRoute`：要求 `is_admin === true`；
- Capture 路由不经过 ProtectedLayout/SubscriberRoute/AppShell，只使用 capture client。

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
