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
| `/admin/*` | Admin | 管理页面（含 `/admin/overview`、`/admin/users`、`/admin/strategies`、`/admin/jobs`、`/admin/beta-applications`、`/admin/after-close`） |

刷新后必须重新调用 `/me/access`，不能永久相信本地缓存。

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
- "筛选结果" 标签替代原 "命中"（`filtered_total` 是当前筛选条件下的数量，不是命中数）。

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
