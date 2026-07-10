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
| **行情工作区 URL 状态（阶段三最终验收）**：URL parse/serialize 往返（scope/symbol/timeframe/source/strategy/event_id）、非法 timeframe 回退 1d、非法 source 回退 watchlist、source→strategy 默认推导、strategy 等于默认值时省略、event_id=null 不写入、选择新股清除 event_id、buildMarketWorkspaceUrl 含/省略 strategy、`selectInstrumentFromMarketPane` 选股重置 source/strategy/event_id、`changeMarketScope` 切 scope 退出 selection 上下文、选股后 timeframe 保留 | `frontend/src/features/market-workspace/__tests__/marketWorkspaceUrlState.test.ts`（21 用例） |
| **timeframe 单一真源与请求门控（阶段三最终验收）**：URL 15m→请求15m→图表15m、工具栏切换写回 URL、scope 查询互斥、右栏收起不请求、Capture 和 StockDetail 回归、selection 上下文重置、搜索结果渲染门控 | 浏览器 E2E（CDP，22 项断言全部 PASS） |

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
| **批量加入自选按 instrumentId 匹配+去重**：handleBatchAdd 按 `r.instrumentId` 匹配 selectedKeys（禁止 resultId）、instrumentId 去重、空选 toast 提示、成功/失败 toast 真实数量、保留 useAddToWatchlist 缓存失效 | `frontend/src/pages/__tests__/ScreenerPage.batch.test.ts`（6 用例） |
| **change_pct 独立列**：列存在、title/shortTitle、dataType=percent、sortable/filterable、width≈86、render 用 fmtChange+changePctColorClass、sortValue 读取 payload、位于 stock 列之后 | `frontend/src/features/trend-selection/__tests__/columns.test.ts`（6 用例） |
| **表格视图配置 preset API**：权限矩阵（401/403/200/201）、CRUD、用户隔离、重名冲突 409（含 NULL strategy_key 场景）、quota 422、非法 config 422、filters/hiddenColumns/sort 深度校验、op 白名单校验、is_default 互斥、必填字段校验、user_id 注入安全、PATCH 空请求 422、迁移幂等、**跨 session 持久化（create/update/delete 真实 commit 验证）** | `backend/tests/test_table_view_presets_api.py`（50 用例） |
| **preset 保存后前端列表刷新**：成功保存后清空输入/刷新列表/失败显示后端 detail | `frontend/src/components/__tests__/tablePresetMenu.test.ts`（4 用例） |
| **sticky 表头 viewport 模式**：`StrategyDataTable` 支持 `stickyHeaderMode="viewport"`、ScreenerPage 传入 viewport、global.scss 中 `.table-wrap.viewport-sticky` overflow visible + 表头 top `var(--topbar)` z-index 18 | `frontend/src/components/__tests__/stickyHeader.test.ts`（4 用例） |
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
| MDAS 拉取 live 1m 时 `start_time`/`end_time` 同为 `Asia/Shanghai` aware datetime，禁止 naive/aware 混用 | `test_market_data_aggregation_partial_daily.py::test_partial_daily_fetch_minute_bars_uses_aware_datetime`, `test_market_data_aggregation_partial_daily.py::test_intraday_1m_fetch_minute_bars_uses_aware_datetime` |
| `pytdx_adapter.get_minute_bars` 接收 aware `Asia/Shanghai` start/end 时，正确与 pytdx 返回的 naive `datetime` 列比较过滤 | `test_pytdx_adapter_minute_aware.py::test_get_minute_bars_aware_start_end_filters_naive_datetime`, `test_pytdx_adapter_minute_aware.py::test_get_minute_bars_naive_start_end_still_works` |
| `/quote` 时区输出 `+08:00` | `test_quote_timezone.py` |
| `/quote` 可信化字段（source/is_realtime/freshness_seconds/degraded/degraded_reason） | `test_quote_trustworthy.py` |
| K线实时契约（blocking）：交易时段 1d partial、收盘后 non-partial、`/quote` +08:00、前端 bars 状态展示、不可信 quote 不混入 K 线 | `test_market_data_aggregation_partial_daily.py`, `test_quote_timezone.py`, `test_quote_trustworthy.py`, `frontend/src/utils/__tests__/chart.test.ts` |
| 后端已返回 1d partial bar 时前端不得用 quote 覆盖；后端未返回 partial bar 时 quote 可兜底追加 | `frontend/src/utils/__tests__/chart.test.ts` |

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
| `compute_for_trade_date` 单股失败不阻断其他股票；失败比例超 30% 抛 `RuntimeError` | `test_feature_snapshot_service.py::test_compute_for_trade_date_single_failure_does_not_block` |
| [half-baked rollback] `compute_for_trade_date` 不内部 commit；超阈值抛 `RuntimeError` 后 caller rollback，DB 无半成品残留 | `test_feature_snapshot_service.py::test_compute_for_trade_date_over_threshold_no_partial_after_rollback` |
| `structural_payload` 必须包含 `primary`/`secondary`/`relation`/`meta` 4 key；`relation` 来自 `_compute_relation` | `test_feature_snapshot_service.py::test_compute_snapshot_structural_payload_contains_relation` |
| `parse_args` 默认值（end=latest/batch_size=20/failure_threshold=0.3）+ 自定义值 + 缺失 `--start` 报 `SystemExit`；**`commit_every` 已移除** | `test_feature_snapshot_backfill.py::test_parse_args_*` |
| `get_trade_dates_from_bars` 升序 trade_dates + 空表返回空列表 | `test_feature_snapshot_backfill.py::test_get_trade_dates_from_bars*` |
| `get_latest_bar_date` 返回 `bars_daily.trade_date` 最大值；空表返回 `None` | `test_feature_snapshot_backfill.py::test_get_latest_bar_date*` |
| `get_existing_instrument_ids` 返回某日已存在 snapshot 的 instrument_id 集合；按完整唯一键过滤；按 schema_version 严格过滤 | `test_feature_snapshot_backfill.py::test_get_existing_instrument_ids_*` |
| `backfill_single_date` `--dry-run` 输出 missing 数量不写库；正常模式调用 compute；`--resume` 真正跳过已存在 instrument（不重新计算）；全部已存在时跳过 compute | `test_feature_snapshot_backfill.py::test_backfill_single_date_*` |
| `main` `--dry-run` 端到端不写库；单日失败 rollback 半成品不阻断其他日期（验证 rollback/commit 计数）；`--end=latest` 解析；`start > end` 直接 `sys.exit(1)` | `test_feature_snapshot_backfill.py::test_main_*` |
| 盘后编排状态机新增 `feature_snapshot` 步骤（`quality_gate → feature_snapshot → publishing`） | `test_after_close_orchestrator.py`（9 个用例） |
| [feature_snapshot 失败不进入 publishing] `compute_for_trade_date` 抛 `RuntimeError` → `publish_run` 未被调用 + `job_run.status='failed'` + 不应有 publishing/succeeded 事件 | `test_after_close_orchestrator.py::test_execute_feature_snapshot_failure_skips_publishing` |
| 断点恢复：`last_completed_step='quality_gate'` → `skip_snapshot=False`；`'feature_snapshot'` → `skip_snapshot=True` | `test_after_close_orchestrator.py` |
| 盘后流水线聚合 API（11 场景）：盘前 not_started/收盘后 blocked/latest 不回退历史/运行中/成功/watchlist_ready 判定/sample 不计入/full 优先展示/失败带 error/POST 幂等/events 限 100 条/非 admin 403 | `test_admin_after_close_pipeline.py`（11 个用例） |
| 迁移幂等：`alembic upgrade head` / `downgrade -1` / `upgrade head` 链路不报错；表含唯一约束与 3 个 btree 索引 | 手动验证（test DB） |

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
| **飞书渠道操作按钮权限**：member 不显示 admin 最近事件实测按钮；member 渠道卡显示「测试并启用」/「发送测试消息」；admin 才显示「管理员实测最近事件」；编辑/删除对两者都可见 | `frontend/src/pages/__tests__/settingsFeishuActions.test.ts`（5 用例） |
| **趋势选股 URL 状态 encode/decode**：strategy/keyword/sort/filters/page/pageSize 往返一致；默认 page/pageSize 省略；非法 filters JSON 回退为空数组；陈旧 filter/sort key 丢弃；不保存 selectedKeys/activeRunId/rows/results | `frontend/src/components/__tests__/screenerUrlState.test.ts`（5 用例） |
| **个股详情返回按钮**：优先使用 `location.state.returnTo`，没有时按 source fallback 到 `/screener` 或 `/watchlist` | `frontend/src/pages/__tests__/detailNavigation.test.ts`（3 用例） |
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
| Indicator overlay alignment：`indicator_cache.ALGORITHM_VERSION == "v5"`（PR #32：DSA 全周期 + 1w/1mo BB 改变计算路径）且旧 v4 cache key 不匹配新 `build_cache_key`（旧缓存自然失效，避免返回 1d-only DSA + 1w/1mo 无 BB） | `backend/tests/test_indicator_cache.py`（PR #32 修订 2 个 cache schema 版本测试：v4→v5 + 旧 v4 key 不匹配） |
| Indicator overlay alignment：`_adapt_watchlist_bb` 1d/15m/1h/1w/1mo 全部用 `macd_bars` 调用 `compute_bollinger` 计算 BB（非日线阶梯线，1w/1mo 不再移除 BB 字段），BB 长度与 macd_bars 对齐，数值与 `compute_bollinger(macd_bars)` 一致 | `backend/tests/test_indicator_service.py`（PR #32 删除 2 个旧 1w/1mo BB 移除测试，新增 2 个 1w/1mo BB 用 macd_bars 计算测试；PR #31 已有 3 个 15m/1h BB overlay 计算测试保留） |
| Indicator overlay alignment：DSA 全周期支持，`MarketDataContext.bars_daily=macd_bars`（所有周期用当前 timeframe bars），`daily_time_list` 用 `macd_bars.index`，15m DSA `time[0]` 含 `T` 分隔符（非日线 YYYY-MM-DD），15m context.bars_daily 第一根 bar `hour==9`（非 daily 的 0） | `backend/tests/test_indicator_service.py`（PR #32 新增 2 个 DSA 全周期计算测试） |
| Indicator overlay alignment：`shouldAllowDsaOverlay` 1d/15m/1h/1w/1mo 全部 true / `shouldCheckDsaMismatch` 全周期 true / `DSA_TITLE_HINT('1d')` 含"日线结构锚" / `DSA_TITLE_HINT('15m'/'1h'/'1w'/'1mo')` 含"当前周期验证图层"且不含"日线结构锚" | `frontend/src/components/__tests__/dsaSourceAlignment.test.ts`（PR #32 重写第 4 节，4 个 DSA overlay policy contract 测试覆盖全周期 + title 按周期区分） |
| Indicator overlay alignment：PR #33 前端硬编码清理 — `shouldRenderDsaLayer` / `shouldRenderBbLayer` / `shouldToggleDsa` / `shouldIncludeDsaInPriceRange` 全周期决策（不再 `timeframe !== '1d'` skip / `1w \|\| 1mo` skip / `timeframe === '1d'` y-axis 限制） | `frontend/src/components/__tests__/dsaSourceAlignment.test.ts`（PR #33 新增第 5 节，10 个 overlay 渲染/toggle/y-axis 决策测试覆盖全周期 + capture 锁定 + source mismatch 保护） |
| DSA visual_segments time alignment：PR #34 后端 `format_dsa_time` 按 timeframe 序列化（15m/1h 含 `THH:MM:SS`，1d/1w/1mo 为 `YYYY-MM-DD`）；`compute_dsa_bundle` 15m `visual_segments.points.time` / `anchor.time` 含 `HH:MM`；`DSASelector.compute_indicators` 15m `time` / `visual_segments.points.time` 含 `HH:MM`；1d 仍为 `YYYY-MM-DD`；15m segment times 与 source_bar_times canonical 匹配率 > 0.5 | `backend/tests/test_dsa_visual_segments_time_format.py`（PR #34 新增，9 个测试覆盖 15m/1d 时间格式 + canonical 对齐） |
| DSA visual_segments matched ratio contract：PR #34 前端 `computeDsaSegmentMatchStats(segments, displayTimes, timeframe)` 计算 segment points 经 `normalizeChartTime` 后与 K线 `displayTimes` canonical key 的匹配率；15m/1h 含 `THH:MM` 时 `ratio > 0.5`；旧 YYYY-MM-DD 在 15m 下 `matched=0` / `degradedReason='segment_time_no_match'`；空 segments `degradedReason='no_segments'`；多 segment 累计 matched | `frontend/src/components/__tests__/dsaSourceAlignment.test.ts`（PR #34 新增第 6 节，7 个 segment matched contract 测试覆盖 15m/1h/1d/empty/多 segment 累计） |


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
