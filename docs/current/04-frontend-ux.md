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

| 路由 | 守卫 | 页面 |
|---|---|---|
| `/` | Public | 门户 |
| `/login` | Public | 登录/注册 |
| `/subscription-expired` | Authenticated | 续期 |
| `/membership-expired` | Redirect | 兼容跳转 |
| `/capture/stock/:symbol` | Capture Token | 截图专用页面 |
| `/overview` | Subscriber/Admin | 服务总览 |
| `/screener` | Subscriber/Admin | 趋势选股 |
| `/watchlist` | Subscriber/Admin | 我的自选 |
| `/stock/:symbol` | Subscriber/Admin | 个股详情 |
| `/messages` | Authenticated | 历史消息 |
| `/settings` | Authenticated | 账户和通知渠道 |
| `/admin/*` | Admin | 管理页面 |

刷新后必须重新调用 `/me/access`，不能永久相信本地缓存。

## 3. 页面职责

### 趋势选股

- 以全量 active 股票 universe 为展示主表（`strategy_run_items`），DSA 指标为 LEFT JOIN 附加字段；
- 默认无隐式筛选时显示全量股票（succeeded 有指标、skipped/failed 指标为空但仍显示）；
- succeeded 行应正确显示 35 个 DSA 指标（如 `bb.position`、`bb.width`、`node.position` 等）；后端通过 `(run_id, instrument_id)` 关联 `strategy_results` 加载指标（因 `strategy_run_items.result_id` 当前未回填，见 ALIGN-033）；
- skipped 行（reason_code=insufficient_history）显示股票代码和名称，指标列显示 "-"；
- failed 行显示股票代码和名称，指标列显示 "-"，附带 reason_code/error_message；
- 展示 source_total、filtered_total、成功、失败、跳过和覆盖率；
- 批次不完整显示阻断，不伪装正常；
- 行 key 使用 `instrumentId`（不依赖 `result_id`，skipped/failed 行也能选中加入自选）；
- "筛选结果" 标签替代原 "命中"（`filtered_total` 是当前筛选条件下的数量，不是命中数）。

### 我的自选

- 展示股票、价格、涨跌幅、上下节点、POC、最近事件；
- 新增/删除/恢复后刷新服务器状态；
- 到期用户不加载列表，进入续期路径；
- 已存在、软删除、额度不足提示不同；
- 桌面端表格不显示每行状态栏，交易/非交易日状态统一在页眉用 `MonitorStatusBadge` 全局展示；
- 移动端卡片头不显示每行状态徽章；
- 数据列开启表头过滤（`filterable=true`）；
- 表格使用 `compact-table` 与趋势选股页字体/布局对齐。

### 个股详情

- K 线、指标和截图共享行情快照；
- 展示 quote 的 `source`/`is_realtime`/`update_time`/`freshness_seconds`/`degraded`/`degraded_reason`，以及 bars 的 `data_source`/`as_of`/`is_partial`/`degraded`/`degraded_reason`；
- DSA 与 Node 图层可开关；
- **SQZMOM_LB 图层开关**：位于技术指标分组，默认关闭；开启后在 K 线下方新增独立副图，使用后端返回的 `val` 渲染 histogram、`bcolor` 渲染柱色、`scolor` 渲染 0 轴 squeeze marker；前端只消费后端 DTO，不重新计算 `val`/`sqzOn`/`sqzOff`/`noSqz`；API 未返回 `sqzmom_lb` 时页面不崩溃；
- 截图区设置 render-ready 标志；
- 按 timeframe 请求对应根数（1d=250、15m=4000、1h=1200、1w=260、1mo=120），与 Node Cluster / indicator_contract 对齐；`1m` 不在工具栏暴露；
- 实时报价通过 `mergeRealtimeQuoteIntoBars` 合并到最后一根 K 线用于显示，但仅当 `quote.is_realtime === true && quote.source === "pytdx" && quote.freshness_seconds <= 60` 时才合并；daily_fallback / 延迟 / 降级行情只用于顶部报价 fallback/状态提示，不混入 `displayBars`；1d 保留日期语义并跨日追加实时 bar，intraday（15m/1h 等）使用 `quote.update_time`；`baseBars` 仍用于指标计算，避免污染算法输入；
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

### 消息与飞书

- 消息显示股票、事件时间、详情入口；
- 文字和图片显示独立状态；
- partial_failed 展示失败步骤和仅重试图片；
- Worker 不可用时不显示整体成功。

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
