# Test Coverage Map

> 本文件是关键规则到测试的索引。实际测试列表以仓库为准。

## 1. 权限与订阅

| 规则 | 测试 |
|---|---|
| active/expired/no-sub/admin | `test_trend_selection_api_permissions.py`, watchlist permission tests |
| AccessContext | `test_eligible_user_service.py`, access control tests |
| Capture Token 隔离 | `test_capture_token_isolation.py`, auth tests |
| Worker 心跳 admin API（admin/non-admin/unauthenticated + status 筛选 + health_state 分类） | `test_admin_worker_heartbeats_api.py` |

## 2. 趋势选股

| 规则 | 测试 |
|---|---|
| partial_failed 不发布 | `test_dsa_publish_validation.py`, strategy batch tests |
| computable 结果覆盖 | `test_strategy_batch.py` |
| DSA benchmark | `backend/reports/dsa_benchmark_20260702.md` |
| Node Cluster 输入 | `test_node_cluster_contract.py` |
| run 级总超时可配置 / insufficient_history skipped / run_timeout_budget_exhausted / execute_run 保留 skipped_count | `test_strategy_batch_service.py` |
| 全量 universe 主表（strategy_run_items LEFT JOIN strategy_results）+ skipped/failed 行 + metric_filter + watchlist 过滤 | `test_strategy_results_universe.py` |
| adapter 处理 null id/payload（skipped/failed 行 resultId=''、payload={}） | `frontend/src/features/trend-selection/__tests__/adapter.test.ts` |
| 生产验证：run_id=f0c15e1c, source_total=5293, succeeded 行 35 个 DSA 指标正确显示，skipped 行显示股票但指标为空（JOIN 改用 `(run_id, instrument_id)` 绕过 result_id 未回填问题，ALIGN-032 CLOSED, ALIGN-033 P2） | 生产 API + DB 只读核对（CHANGE-20260704-029） |

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
| `/quote` 时区输出 `+08:00` | `test_quote_timezone.py` |
| `/quote` 可信化字段（source/is_realtime/freshness_seconds/degraded/degraded_reason） | `test_quote_trustworthy.py` |
| K线实时契约（blocking）：交易时段 1d partial、收盘后 non-partial、`/quote` +08:00、前端 bars 状态展示、不可信 quote 不混入 K 线 | `test_market_data_aggregation_partial_daily.py`, `test_quote_timezone.py`, `test_quote_trustworthy.py`, `frontend/src/utils/__tests__/chart.test.ts` |
| 后端已返回 1d partial bar 时前端不得用 quote 覆盖；后端未返回 partial bar 时 quote 可兜底追加 | `frontend/src/utils/__tests__/chart.test.ts` |

## 3.5 自选股监控

| 规则 | 测试 |
|---|---|
| monitor-status 无 MonitorState 或 payload 无效时 fallback | `test_watchlist_monitor_status_fallback.py` |
| monitor-status 单只 fallback 失败单行降级 | `test_watchlist_monitor_status_fallback.py` |
| 自选监控页无每行状态栏、页眉全局状态、数据列可过滤、compact-table 对齐 | `frontend/src/features/watchlist-monitor/__tests__/columns.test.ts` |
| admin monitor 资格：active admin 与 active member + 有效 subscription 放行，disabled admin / 无订阅普通用户排除 | `test_monitor_eligible.py` |
| monitor_batch / event_recipient / outbox_relay / delivery_worker 监控资格口径一致 | `test_outbox_relay_monitor_eligibility_consistency.py`, `test_delivery_worker_monitor_eligible.py` |
| monitor_batch 使用 live 1m 输入（`include_realtime=True`）并剔除最后一根未完成 bar | `test_monitor_batch_live_minute.py` |

## 4. 飞书与通知

| 规则 | 测试 |
|---|---|
| Platform App only | `test_feishu_platform_app_only.py` |
| target_channel_id | `test_outbox_target_channel_id.py` |
| 状态机 | `test_state_machine.py`, `test_stock_detail_feishu_status.py` |
| beta admin 通知 | `test_beta_application_notifier.py` |
| `_notify_monitor_status` 直接发送路径 | **无测试**（缺口，ALIGN-025） |
| 飞书消息时间显示中国时区 | `test_feishu_timezone_format.py` |

## 5. 前端

| 规则 | 测试 |
|---|---|
| Capture 页面契约 | frontend contract capture tests |
| TypeScript/lint/build | CI blocking jobs |
| K 线合并实时行情（1d 保留日期、intraday 使用 update_time、跨日追加） | `frontend/src/utils/__tests__/chart.test.ts` |
| SQZMOM_LB 后端算法 Pine 等价 | `backend/tests/test_sqzmom_lb.py` |
| SQZMOM_LB indicator service 注入 | `backend/tests/test_indicator_service.py` |
| SQZMOM_LB 前端图层开关/副图/渲染契约 | `frontend/scripts/contract-tests/sqzmom-layer.test.ts` |
| 结构状态因子 ATR SSOT Pine RMA 等价 | `backend/tests/test_atr_utils.py` |
| 结构状态因子 5 组因子 + 异常隔离 + 无未来函数 | `backend/tests/test_structural_factor_service.py` |
| 结构状态因子 API 路由（合法/非法参数/降级） | `backend/tests/test_structural_factors_api.py` |
| 结构状态因子前端面板契约（双周期 tabs/5 卡片/null/降级/不重算） | `frontend/scripts/contract-tests/structural-state-panel.test.ts` |
| 结构状态因子 V1.8 双周期差异 + 无未来函数 + sqz_on/primary_dir 字段 | `backend/tests/test_structural_factor_service.py::test_v18_dual_period_difference` / `test_v18_no_future_function_confirmed_pivots` / `test_volatility_v18_sqz_on_off` |
| 结构状态因子 V1.8 前端字段契约（v18Keys 39 项含 node_interval_position_0_1/node_interval_position_raw/cost_position_zone/value_area_zone/val_price/vah_price + v18RelationKeys 7 项） | `frontend/scripts/contract-tests/structural-state-panel.test.ts` |
| 结构状态因子 V1.8 成本/节点位置语义修复（zone 分类 6 种 + node_interval_position 公式 clip/raw + null + 截图案例 close=147.62→1.000 + position_0_1 保持 VP 全区间语义 + 前端标签修复 VP全区间位置/节点区间位置/VA状态/VAL/VAH） | `backend/tests/test_structural_factor_service.py`（18 个新增） + `frontend/scripts/contract-tests/structural-state-panel.test.ts::Cost/Node card uses unambiguous position labels` |
| 时序特征 V1 后端服务（daily_context 9 字段 + m15_response 9 字段 + derived_relation 3 字段 + 异常隔离 + 无未来函数 + anchor 规则 bsl<bsh→low / bsh<=bsl→high + position_change 手算 + anchor percentile 不变性 + 组级异常隔离） | `backend/tests/test_temporal_feature_service.py` |
| 时序特征 V1 API 路由（合法/非法参数/`as_of!=latest` 400/降级/meta 结构） | `backend/tests/test_temporal_features_api.py` |
| 结构状态面板隐藏开关契约（默认隐藏/开关/localStorage/强制隐藏参数/禁用 toggle/toggle 在 tv-chart-column 内部） | `frontend/scripts/contract-tests/structural-state-toggle.test.ts` |
| 结构状态因子 V1.9 active swing + confirmed pivot 别名 + DSA age 统一（上涨突破 raw>1 且 active in [0,1] / 下跌破位 raw<0 且 active in [0,1] / 单边上涨 active high 跟随 / 单边下跌 active low 跟随 / bars_since_active 正确 / confirmed_swing_breakout_state 三态 / fallback 模式 / DSA age +1 口径 / age_bars == current_dsa_segment_age_bars） | `backend/tests/test_structural_factor_service.py`（新增 9 个 V1.9 测试） |
| 时序特征 V1.9 active swing 字段 + derived_relation 改用 active swing（daily/m15 active swing 字段存在性 + m15_position_relative_to_daily == active - active + 强趋势段不再 -1.755） | `backend/tests/test_temporal_feature_service.py`（新增 3 个 V1.9 测试） |
| 结构状态因子 V1.10 developing swing（major up leg 回落 developing_low = min(lows[active_high_bar:now]) 不得等于 active_low / major down leg 反弹 developing_high = max(highs[active_low_bar:now]) 不得等于 active_high / 继续创新高 dev=active dir=1 / 继续创新低 dev=active dir=-1 / 000100 类似 case developing_low 不得等于 4.45） | `backend/tests/test_structural_factor_service.py`（新增 5 个 V1.10 测试） |
| 时序特征 V1.10 developing swing 字段 + derived_relation 改用 developing swing（daily/m15 developing swing 字段存在性 + m15_position_relative_to_daily == developing - developing + 不回退 active major leg 或 confirmed raw + developing 缺失返回 null） | `backend/tests/test_temporal_feature_service.py`（新增 2 个 V1.10 测试 + 替换 2 个 V1.9 旧测试） |
| 盘后 publish auto-trigger（DSA scheduled+completed 触发 / 非 DSA 不触发 / missing trade_date 不触发 / create_failure_no_propagation） | `backend/tests/test_worker_auto_trigger.py`（4 个测试） |
| Swing 摘要卡 V1.10 developing 字段契约（摘要卡只显示 developing 字段 / active major leg 只在明细 / confirmed pivot 只在明细 / 禁止模糊标签 / 禁止 Active high/low 作为主字段 / 时序位置标签含 developing 或 confirmed 前缀） | `frontend/scripts/contract-tests/structural-state-panel.test.ts`（替换 3 个 V1.9 测试为 V1.10 版本） |
| capture 布局 V1.9 单列契约（capture=feishu 不渲染按钮/侧列/Temporal / .tv-side-column 隐藏 / .tv-chart-column 占宽 100% / testid 在 tv-chart-column） | `frontend/scripts/contract-tests/capture-stock-page.test.ts`（新增 2 个 V1.9 测试） |
| 结构状态开关 V1.9 capture 模式契约（isCaptureMode 判定 / capture=feishu 强制隐藏且禁用 toggle / capture=1 强制隐藏 / hideStructuralState=1 强制隐藏） | `frontend/scripts/contract-tests/structural-state-toggle.test.ts`（新增 2 个 V1.9 测试） |


## 6. 文档和工程治理

| 规则 | 测试 |
|---|---|
| docs consistency | `tools/tests/test_check_docs_consistency.py` |
| architecture rules | `tools/check_architecture.py` |
| test allowlist | `tools/check_test_allowlist.py` |
| Ruff/Mypy baseline | CI baseline regression jobs |

## 7. v2 应用后需要新增/调整

- 修改 docs consistency 测试，让它检查 `current/MANIFEST.md` 而不是每个 current 文件头；
- 新增 maps 必备文件存在性检查；
- 新增旧 `docs/current/00-18` 不再作为 current 事实源的检查；
- 新增 local links 覆盖 `maps/`。
