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

- `BarsCoverageService` 统一 A 股口径，排除指数/ETF，默认使用 `shanghai_business_date`，返回 `coverage`（展示）与 `coverage_raw`（阈值判断）；
- `/admin/after-close-runs/dsa-only`、`bars_scheduler`、系统概览 `WAITING_DSA` 判定等覆盖率门禁使用 `coverage_raw` 原始值；
- `/admin/after-close-runs/dsa-only` 当日无数据时 fallback 到最新交易日，覆盖率不足返回 409；
- `/watchlist/monitor-status` 无 `MonitorState` 或 `payload` 无效时通过 `MonitorSnapshotService` fallback 返回指标，单只失败单行降级；
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
  - `test_quote_timezone.py` 覆盖 `/quote` 返回 `update_time` 带 `+08:00`、UTC 字符串被修正为 `+08:00`。

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
pytest tests/test_delivery_worker_monitor_eligible.py tests/test_monitor_batch_live_minute.py tests/test_market_data_aggregation_partial_daily.py tests/test_quote_timezone.py -q
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
- `indicator_cache.ALGORITHM_VERSION == "v5"`（PR #32 bump：DSA 全周期 + 1w/1mo BB 改变计算路径）；
- 旧 v4 cache key 与新 `build_cache_key` 生成的 key 不相等（旧缓存自然失效，避免旧 v4 缓存返回 1d-only DSA + 1w/1mo 无 BB）；
- 修改 indicator 计算逻辑、`source_bar_times` 格式、BB/SQZMOM/MACD 计算路径、DSA 全周期支持、1w/1mo BB 计算必须 bump `ALGORITHM_VERSION`。

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
