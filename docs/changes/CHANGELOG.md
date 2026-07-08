# 项目修改索引

本文件只做索引。每次代码、配置、测试、部署或当前设计变化，都必须使用独立分支并在 `records/` 下建立独立记录。

## 2026-07-08

- CHANGE-20260708-050: 修复 Monitor 与 Notification latest-event 图片 Capture Token Claims
  - 新增 `backend/app/constants/capture.py` 定义 `CAPTURE_SCOPE_STOCK_DETAIL`，避免服务层 import `app.core.deps` 导致循环依赖
  - `backend/app/core/deps.py` 改为从常量模块导入 `CAPTURE_SCOPE_STOCK_DETAIL`
  - 修复 `backend/app/services/monitor_batch_service.py::_send_chart_images_via_outbox()` 生成 capture token 时缺失 `scope/user_id/instrument_id` 的问题
  - 修复 `backend/app/services/notification_service.py::test_channel_latest_event()` 生成 capture token 时缺失 `scope/user_id/instrument_id` 的问题
  - `backend/app/services/stock_detail_feishu_service.py` 硬编码 `"stock_detail_capture"` 改为常量（业务逻辑不变）
  - 更新 `backend/app/core/security.py::create_capture_token` 文档：明确所有 capture worker 调用方必须传递 `scope/user_id/instrument_id/event_id`
  - 新增测试 `backend/tests/test_monitor_batch_capture_image.py`（5 用例）与 `backend/tests/test_notification_latest_event_capture.py`（2 用例）
  - 更新 `03-jobs-integrations-operations.md`、`05-testing-acceptance.md`、notification-flow-map、worker-job-map、test-coverage-map
  - 新增 ALIGN-038：monitor 文字成功但图片缺失待部署后 smoke 验证
  - 部署验证完成：构造 `[SMOKE_IMAGE]` 单标的事件，capture_jobs.succeeded、image outbox processed、image delivery success、飞书图片投递成功，ALIGN-038 已关闭
  - 不修改 MDAS、前端 K线、monitor 触发、文字通知、outbox_relay、delivery_worker、feishu adapter

## 2026-07-07

- CHANGE-20260707-049: Backfill Multiprocessing 优化
  - `feature_snapshot_backfill.py` 新增 `--workers N` 参数（默认 1 单进程，>1 启用 multiprocessing）
  - 新增 `_worker_process_instruments()`：top-level 可 pickle worker 函数，独立 `async_engine`（pool_size=1/max_overflow=0/pool_pre_ping=True）
  - 新增 `backfill_instrument_first_parallel()`：ProcessPoolExecutor + `asyncio.gather(return_exceptions=True)` 编排（按 chunk 顺序映射，避免 Python 3.12 `as_completed` wrapper future 不可回溯）
  - worker 循环重构为三阶段（load_bars / compute / commit 分离），load 失败时正确标 failed
  - **[Blocker Fix v2]** per-instrument commit 改为 per-date commit（`upsert → db.commit() → success++`，异常 `rollback + failed++`，commit 失败不计 success）；worker future 异常整个 chunk 计 failed（避免 worker 崩溃仍 finalized succeeded）；pool_size 5→1, max_overflow 10→0；`--workers < 1` 拒绝、`> cpu_count` 自动 cap
  - 测试：73 passed（v1 9 + v2 8 Blocker Fix + 56 原有），ruff clean，mypy 0 新增错误
  - 文档：03-jobs / 05-testing / backend-module-map / worker-job-map / test-coverage-map 随 PR 更新
  - 部署边界：未执行生产部署，需部署后 `--workers 2 --dry-run` + 小样本 `--symbols` 验证，再扩大到 `--workers 4`
- CHANGE-20260707-048: Snapshot Run Gate + Instrument-first Backfill
  - 新增 `stock_feature_snapshot_runs` 表（partial unique index 仅约束 `status='running'`，3 btree 索引）
  - 新增 `backend/app/models/stock_feature_snapshot_run.py` + migration `057_stock_feature_snapshot_runs`
  - `feature_snapshot_service` 新增 `create_snapshot_run` / `finish_snapshot_run` run lifecycle（running → succeeded/failed）
  - `after_close_orchestrator` feature_snapshot 步骤前后写 run lifecycle（独立 session 保证 run 记录持久化，snapshot rollback 不影响）
  - `watchlist` 新增 `_has_succeeded_snapshot_run` helper，只读 `status='succeeded'`（且 `published_at` 非空）的 snapshot
  - `feature_snapshot_backfill` 重构为 instrument-first（每只股票每周期只调用一次 `load_instrument_bars`，内存中按 `trade_date` slice）
  - backfill 新增 `--symbols` / `--limit-instruments` 小样本参数；run gate：每个 trade_date 创建 `succeeded`/`failed` run
  - `backend/Dockerfile` 新增 `COPY scripts ./scripts`
  - 测试：49 passed（21 backfill + 11 orchestrator + 11 watchlist + 6 run service），ruff clean，mypy 0 新增错误
  - 部署边界：未执行生产库 migration、未全量 backfill、未 merge/部署；test DB 已验证 alembic upgrade/downgrade/upgrade 链路
- CHANGE-20260707-047: Feature Snapshot 持久化（自选股监控指标从实时计算切换为盘后快照）
  - 新增 `stock_feature_snapshots` 表（JSONB payload + 唯一约束 + 3 btree 索引，无 GIN 索引）
  - 新增 `backend/app/services/feature_snapshot_service.py`：复用 `_compute_all_factors_for_bars` / `_compute_relation` / `_compute_daily_context` / `_compute_m15_response` / `_compute_derived_relation` / `bollinger()` 不复制公式；point-in-time 截断 `index.date <= trade_date`；upsert 幂等；单股失败写 `degraded_reasons` 不阻断批次；`compute_for_trade_date` 不内部 commit，caller 控制 commit/rollback
  - 修改 `backend/app/services/after_close_orchestrator.py` 状态机：`quality_gate → feature_snapshot → publishing`，断点恢复路径更新；feature_snapshot 失败显式 rollback 不进入 publishing
  - 修改 `backend/app/api/watchlist.py::get_watchlist_monitor_status`：metrics 唯一来自 `summary_payload`，新增 `calculation_status` 三态（SUCCEEDED/WAITING_SNAPSHOT/NO_SNAPSHOT），`_resolve_expected_snapshot_trade_date`（async）复用 `calendar_service`，删除 `MonitorSnapshotService` 实时 fallback 与 `MonitorState.payload` fallback
  - 新增 `backend/scripts/feature_snapshot_backfill.py` 历史回补 CLI 脚本（核心计算复用 service，脚本只做 CLI/dry-run/resume 真正跳过/批量调用/per-date 事务）
  - 删除 `backend/tests/test_watchlist_monitor_status_fallback.py`（278 行旧 fallback 测试），新增 4 个新测试文件（service + backfill + API 契约 + orchestrator 调整）
  - **PR #38 Review Blocker 修复**：6 个 blocker（`_resolve_expected_snapshot_trade_date` 规则、半成品 rollback、backfill resume 真正跳过、`structural_payload.relation` 字段、PR/docs 历史表述更正、test DB 验证）详见 `records/CHANGE-20260707-047.md` Blocker 修复章节
  - 部署边界：未执行生产库 migration、未全量 backfill、未 merge/部署；test DB 已验证 alembic upgrade/downgrade/upgrade 链路
- CHANGE-20260707-046: 修复 pytdx_adapter 对 aware 1m start/end 的比较异常
  - 根因：PR #35 后 `MarketDataAggregationService` 传入 aware `Asia/Shanghai` start/end，但 `pytdx_adapter.get_minute_bars` 内部 pytdx 数据 `datetime` 列为 naive，比较时触发 `Invalid comparison between dtype=datetime64[us] and Timestamp`
  - 修复：`get_minute_bars` 过滤前将 aware start/end 按 `Asia/Shanghai` 解释后转为 naive
  - 新增测试：`test_pytdx_adapter_minute_aware.py`（aware 过滤 + naive 兼容）
  - 更新 `02-data-api-contracts.md`、`05-testing-acceptance.md`、`test-coverage-map.md`、ALIGN-037
  - 后端 8/8 测试通过，ruff 零错误
- CHANGE-20260707-045: 修复 MDAS live 1m 时区不一致导致 monitor 无事件
  - 根因：`MarketDataAggregationService` 构造 `live_start` 为 naive datetime、`live_end` 为 aware Asia/Shanghai datetime，传入 `pytdx_adapter.get_minute_bars` 后触发 `can't subtract offset-naive and offset-aware datetimes`
  - 修复：两处实时 1m 拉取统一使用 aware `Asia/Shanghai` `live_start`/`live_end`
  - 新增测试：`test_partial_daily_fetch_minute_bars_uses_aware_datetime`、`test_intraday_1m_fetch_minute_bars_uses_aware_datetime`、`test_monitor_cycle_1m_uses_include_realtime`
  - 更新 `02-data-api-contracts.md`、`03-jobs-integrations-operations.md`、`05-testing-acceptance.md`、maps、ALIGN-037
  - 后端 6/6 测试通过，ruff 零错误
- CHANGE-20260707-044: DSA visual_segments 时间格式按 timeframe 序列化
  - 修 PR #33 遗留：15m/1h DSA 开关可打开但 canvas 看不到线
  - 根因：`_make_segment` / `compute_dsa_bundle.anchor` / `compute_indicators.time` 写死 `strftime("%Y-%m-%d")`，15m/1h segment time 丢失时间信息，`normalizeChartTime('15m'/'1h')` 返回 null，renderer matched=0
  - 新增 `format_dsa_time(x)`：1d/1w/1mo（无时间部分）→ `strftime("%Y-%m-%d")`；15m/1h（含时间部分）→ `isoformat()`
  - 替换 4 处 strftime：`_make_segment` / `_show_segments` / `compute_dsa_bundle.anchor` / `compute_indicators.time`
  - 前端新增 `frontend/src/utils/dsaSegmentMatch.ts::computeDsaSegmentMatchStats` 纯函数，renderDsaPolyline 在 `?debugIndicatorAlignment=1` 时输出 segment matched 诊断（total/matched/ratio/degradedReason/first-last segment time/first-last display time）
  - 不改 DSA 数学公式（`dsa_vwap` / `dsa_dir` / `regime_id` / `visual_segments.direction` / `points.value` 不变）
  - 后端测试 9/9 通过（`test_dsa_visual_segments_time_format.py`），既有 63 个 DSA 测试无回归
  - 前端 contract 39/39 通过（`dsaSourceAlignment.test.ts`，原 32 + 新增 7 个 PR #34 测试）
- CHANGE-20260707-043: Indicator Overlay Frontend Hardcode Cleanup
  - 修 PR #32 遗留：StrategyChart 仍有 4 处 1d-only / 1w-1mo skip 硬编码
  - L2226 `if (groupId === 'dsa' && timeframe !== '1d') return` → `shouldToggleDsa(groupId, isCaptureMode, captureLayers)`
  - L1661 `if (layer.layer_id === 'dsa_vwap' && timeframe !== '1d') return` → `shouldRenderDsaLayer(layerId, layers, dsaSourceMismatch, timeframe)`
  - L1666 `if (layer.layer_id === 'bb' && (timeframe === '1w' || timeframe === '1mo')) return` → `shouldRenderBbLayer(layerId, layers, timeframe)`
  - L1503 `if (layer.layer_id === 'dsa_vwap' && layers.dsa && timeframe === '1d')` → `shouldIncludeDsaInPriceRange(layerId, layers, timeframe)`
  - 新增 5 个纯函数到 `dsaOverlayPolicy.ts`：`shouldAllowBbOverlay` / `shouldRenderDsaLayer` / `shouldRenderBbLayer` / `shouldToggleDsa` / `shouldIncludeDsaInPriceRange`
  - DSA toggle 全周期可切换（非 capture 模式），DSA/BB 渲染不再按 timeframe 跳过，DSA 全周期参与 y-axis range
  - 保留 source mismatch 保护（shouldRenderDsaLayer 在 mismatch=true 时全周期 false）
  - 保留 capture 锁定（shouldToggleDsa 在 capture 模式锁定 DSA 不可关闭）
  - 前端新增 14 个 contract 测试（dsaSourceAlignment.test.ts 第 5 节），后端 42 测试不变（PR #32 修复仍有效）
  - 不改 DSA/BB 数学公式，不改后端 API 契约，不改 cache version（仍 v5）
- CHANGE-20260707-042: Indicator Overlay All Timeframes
  - 修复 PR #31 的两个错误规则：DSA 1d-only 误禁用 + 1w/1mo BB 字段被直接 pop
  - DSA overlay 全周期支持（1d/15m/1h/1w/1mo），不再 1d-only by design
  - `shouldAllowDsaOverlay` / `shouldCheckDsaMismatch` 全周期返回 true，全部需校验 source 对齐（不绕过 mismatch 保护）
  - DSA toggle 全周期可点击，`DSA_TITLE_HINT(timeframe)` 按周期返回 title（1d="日线结构锚"，非 1d="当前周期验证图层"）
  - 后端 `MarketDataContext.bars_daily=macd_bars` + `daily_time_list=macd_bars.index`，DSA 在所有周期用当前 timeframe bars 计算
  - 后端 `_adapt_watchlist_bb` 1w/1mo 合并到 15m/1h 路径，统一用 `compute_bollinger(macd_bars)` 计算 BB（不再 pop BB 字段）
  - 后端 `chart_layers` 循环删除 1w/1mo BB `continue` 跳过逻辑，1w/1mo BB 图层正常进入 renderer
  - `indicator_cache.ALGORITHM_VERSION` v4→v5，旧 v4 缓存 key 不匹配，强制重算（避免旧缓存返回 1d-only DSA + 1w/1mo 无 BB）
  - 后端新增/修订 6 个测试（cache v5 + BB 1w/1mo + DSA 全周期），前端重写第 4 节 4 个 contract 测试
- CHANGE-20260707-041: Indicator Overlay Final Alignment
  - 修复 DSA VWAP 15m/1h 误禁用根因：Redis cache `ALGORITHM_VERSION` 未 bump（v3→v4），旧缓存命中返回旧格式 source_bar_times + 日线阶梯线 BB
  - 修复 15m/1h BB 图层错位根因：`_adapt_watchlist_bb` 15m/1h 用 `_map_daily_to_intraday` 映射日线 BB（阶梯线），改用 `compute_bollinger(macd_bars)` 重新计算当前周期 BB
  - DSA overlay 周期策略：DSA 是日线级别结构锚，仅 1d 渲染；15m/1h DSA 按钮 disabled + 提示 "DSA VWAP 当前仅支持日线结构锚；15m/1h 请使用 Swing、BB、SQZMOM。"
  - `shouldCheckDsaMismatch(timeframe)` 仅 1d 返回 true，15m/1h 不校验 mismatch，避免误报 "DSA 数据源不一致"
  - 新增 `?debugIndicatorAlignment=1` 诊断工具：console.table 输出 bars/dsa_mismatch/layers 对齐信息
  - 新增 `frontend/src/utils/dsaOverlayPolicy.ts` 纯 .ts 模块（DSA_DISABLED_HINT + shouldCheckDsaMismatch）
  - 后端新增 5 个测试（cache schema 2 + BB overlay 3），前端新增 4 个 DSA overlay policy contract 测试
- CHANGE-20260707-040: DSA Overlay Source Alignment
  - 修复 15m/1h 图表误报 "DSA 数据源不一致，已暂停渲染" 根因（source_bar_times 永远用日线日期格式）
  - 修复 15m 图顶部显示 2026-07-07 03:00 时区错误根因（trade_time 返回 naive datetime 被前端时区误判）
  - 后端 `_df_to_responses` 对 15m/1h 返回 aware datetime（Asia/Shanghai tzinfo，`+08:00`），1d 仍为 date 对象
  - 后端 `compute_source_bar_times/hash` 新增 `timeframe` 参数（15m/1h 含时间，1d 仍日期）
  - 后端 `indicator_service` 15m/1h 改用 `macd_bars` 计算 source 字段，与 chart bars 同源
  - 前端 `normalizeChartTime`/`timeTicks` 迁移到纯 .ts 模块 `chartTime.ts`，便于 Node 测试
  - 新增 14 个前端 contract 测试 + 12 个后端测试（chart_bars_service 6 + indicator_service 3 + bars_vectorization 3）
- CHANGE-20260707-039: Developing Swing Current State（V1.10）
  - 新增 developing swing 字段（14 个），反映"当前正在发生的回落/反弹结构"
  - 修复 active swing 仍不代表当前状态的问题（000100 active_low=4.45 是大段起点，developing_low 应为 6.26 回落后的当前 low）
  - swing_position 三层语义：confirmed pivot + active major leg + developing swing
  - 前端 Swing 摘要卡改用 developing 字段（active/confirmed 移到明细 JSON）
  - Temporal derived_relation 改用 developing swing，不回退 active/confirmed raw
  - 5 种计算场景：major up 回落 / major up 创新高 / major down 反弹 / major down 创新低 / fallback

## 2026-07-06
- CHANGE-20260706-038: Swing Active State + Capture 布局 + Publish Auto-trigger
  - 新增 active swing 字段（clip [0,1]），修复 confirmed raw >1 问题
  - temporal derived_relation 改用 active swing
  - DSA age 统一为 +1 口径
  - capture 模式隐藏按钮和侧列
  - worker.py DSA 完成后自动触发 after_close_orchestrator
  - 生产补偿发布 2026-07-06 DSA run（job_run_id=90683e3e, published_at=2026-07-06 23:54:17）

## 2026-07-06: 前端不覆盖后端 1d partial bar

- 修复 `StockDetailPage.tsx` 在交易时段后端已返回 1d partial bar 时仍调用 `mergeRealtimeQuoteIntoBars` 覆盖 K线的问题：仅当 `timeframe==='1d' && barsQuery.data?.is_partial !== true` 时才允许 quote 合并，否则 `displayBars` 直接使用 `baseBars`。
- 修复 `frontend/src/utils/chart.ts::mergeRealtimeQuoteIntoBars` 无条件合并 quote 的问题：新增 `backendIsPartial` 参数，后端已返回 partial bar 时直接返回原 bars。
- 新增前端测试 2 个：`1d 后端已返回 partial bar 时 quote 不覆盖`、`1d 后端未返回 partial bar 时 quote 可兜底追加`。
- 更新 `docs/current/02-data-api-contracts.md`：明确 `mergeRealtimeQuoteIntoBars()` 当且仅当后端未返回 `is_partial=true` 时才允许合并；补充 `12.2` 的 `last_live_bar_time` 与 `is_partial` 事实源说明；把“后端未返回 partial”写入 `12.3` 合并条件首位。
- 更新 `docs/maps/frontend-route-map.md`、`docs/maps/test-coverage-map.md`。
- 新增 CHANGE-20260706-037。
- 本次不部署生产，待用户确认 diff、测试结果与验证证据后授权 build/restart。

## 2026-07-06: Monitor 投递与 live bar 后续修复

- 修复 `delivery_worker.py` 对 `monitor_event`/`strategy_event`/`monitor_chart` 仍走普通资格导致 admin 自动监控被排除的问题：投递前调用 `is_user_eligible_for_monitor` 复核，active admin 与 active member + 有效 subscription 放行，disabled admin / 无订阅普通用户标记 dead/USER_INELIGIBLE；`stock_detail_share` 仍跳过资格，`beta_application_admin` 仍跳过 subscription。
- 修复 `monitor_batch_service.py` 盘中监控 1m 输入仍用 `include_realtime=False` 的问题：1m 改为 `include_realtime=True` 并剔除最后一根未完成 bar，日线/15m 输入保持 `include_realtime=False`；`MonitorCycleResult` 新增 `last_minute_is_partial`，cycle done 与单标的日志输出 `instrument/symbol/source_bar_time/minute_data_source/minute_is_partial/events_detected/events_written`。
- 修复 `market_data_aggregation_service.py` 1d 交易时段无 partial daily bar 的问题：`timeframe=1d && include_realtime=true && MORNING_SESSION/AFTERNOON_SESSION` 时，用当日已完成 1m bar 合成 partial daily bar 追加到响应末尾，返回 `data_source=hybrid`、`is_partial=true`、`last_live_bar_time`；非交易时段、收盘后、`include_realtime=false` 时不合成；不写库。
- 修复 `/quote` 时区：`backend/app/api/bars.py` 与 `backend/app/core/pytdx_adapter.py` 对 naive datetime 和 `+00:00` 字符串统一按 Asia/Shanghai 解释，确保前端显示上海时间。
- 修复 Architecture Rules `duplicate-plan-feature-list`：`outbox_relay.py` 与 `delivery_worker.py` 中的 `_MONITOR_SOURCE_TYPES` 提取为 `app/constants/monitor_source_types.py` 单点真源。
- 新增 `AGENTS.md` `### 13. 个股详情 K线实时契约`，把 `/bars?timeframe=1d&include_realtime=true` 固化为个股详情 K线实时的唯一后端契约，明确 `/quote` 实时 ≠ K线实时、`mergeRealtimeQuoteIntoBars()` 只能兜底视觉增强。
- 新增后端测试 4 个文件 6 个用例：`test_delivery_worker_monitor_eligible.py`、`test_monitor_batch_live_minute.py`、`test_market_data_aggregation_partial_daily.py`、`test_quote_timezone.py`。
- 更新 `AGENTS.md`；更新 `docs/current/02-data-api-contracts.md`、`03-jobs-integrations-operations.md`、`04-frontend-ux.md`、`05-testing-acceptance.md`（新增 K线实时契约 blocking 门禁）、`code-doc-alignment.md`；更新 `docs/maps/api-route-map.md`、`backend-module-map.md`、`frontend-route-map.md`、`notification-flow-map.md`、`test-coverage-map.md`。
- 新增/更新 ALIGN-036（delivery_worker monitor 资格修复待生产验证）、ALIGN-037（1d partial daily bar 与 live 1m monitor 待生产验证）。
- 新增 CHANGE-20260706-036（含根因：8c991e3d 统一 MDAS 后旧 `/bars` 1d 实时语义未完整迁移；PR #25 修 quote 可信化但未恢复 1d partial bar）。
- 本次不部署生产，待用户确认 diff、测试结果与验证证据后授权 build/restart。

## 2026-07-05: Admin 监控资格修复 + 个股详情实时行情可信化

- 修复 admin 自选股被监控过滤：新增 `eligible_user_service.filter_monitor_eligible_recipients`/`is_user_eligible_for_monitor`，active admin 与 active member + 有效 subscription 进入监控，disabled admin / 无订阅普通用户排除；`monitor_batch_service`/`event_recipient_service`/`outbox_relay` 三处统一口径。
- 修复个股详情实时行情伪实时：`/api/v1/instruments/{id}/quote` 返回 `source`/`is_realtime`/`update_time`/`freshness_seconds`/`degraded`/`degraded_reason`；pytdx 成功才标实时，非交易时段 fallback 不降级，交易时段 pytdx 失败才降级并记录原因；`mergeRealtimeQuoteIntoBars` 仅当 `quote.is_realtime && source==="pytdx" && freshness_seconds<=60` 才合并；`StockDetailPage` 显示行情状态徽章与 K 线状态条，不再固定显示“实时行情”；删除 1m 配置；午休统一复用 `market_status_service.compute_market_session`；quote 10s、bars/indicators 30s 轮询，页面 hidden 停止后台轮询；pytdx 单例+线程锁+Redis 10s 缓存，带断线重连与超时保护。
- 新增后端测试 10 个（`test_monitor_eligible.py` 5 + `test_quote_trustworthy.py` 5）、前端 chart 测试 8 个、本地 ASGI 验证脚本 `scripts/verify_quote_trustworthy.py`。
- 更新 `docs/current/02-data-api-contracts.md`、`03-jobs-integrations-operations.md`、`04-frontend-ux.md`、`05-testing-acceptance.md`、`MANIFEST.md`、`code-doc-alignment.md`；更新 `docs/maps/api-route-map.md`、`backend-module-map.md`、`frontend-route-map.md`。
- 新增 ALIGN-034（admin monitor 资格待生产验证）、ALIGN-035（quote 可信化与 pytdx 连接保护待生产验证）。
- 新增 CHANGE-20260705-034。
- 本次不部署生产，待用户确认 diff、测试结果与验证证据后授权 build/restart。

## 2026-07-05: 时序特征 V1 + 个股详情页结构状态面板隐藏开关

- 后端新增 `app.services.temporal_feature_service.compute_temporal_features`：双周期（1d+15m）时序特征，补变化量/持续度/派生关系；daily_context 9 字段 + m15_response 9 字段 + derived_relation 3 字段；复用 V1.8 `compute_structural_factors` 获取 primary/secondary factors；point-in-time 重算 SQZMOM/BB bandwidth/volume_percentile，无未来函数；V1 只支持 `as_of=latest`；组级异常隔离（daily/m15/derived 独立 try/except，单组失败返回 null dict + degraded_reasons）。
- 后端新增 API `GET /api/v1/instruments/{id}/temporal-features`，无认证要求，参数 `primary_timeframe`/`secondary_timeframe`/`adj`/`as_of`；非法参数返回 400（含 `as_of != "latest"`）；不存在 instrument 返回 200 + degraded_reasons。
- 前端 `StockDetailPage.tsx` 结构状态面板默认隐藏 + 用户开关 + localStorage 持久化；`?hideStructuralState=1` / `?capture=1` / `?capture=feishu` 强制隐藏且禁用开关；截图模式默认只渲染 K 线和基础信息；toggle 按钮移入 `tv-chart-column` 内部（`position: relative`）确保定位稳定。
- 新增后端测试 26 个（服务 20 + API 6）、前端 contract test 8 个。
- 更新 `docs/current/02-data-api-contracts.md`（新增第 11 节，含 `as_of!=latest` 返回 400 与组级异常隔离描述）、`04-frontend-ux.md`、`05-testing-acceptance.md`、`docs/maps/api-route-map.md`、`frontend-route-map.md`、`test-coverage-map.md`。
- 新增 CHANGE-20260705-033。

## 2026-07-05: 结构状态因子面板升级至 V1.8（补齐 50 字段 + 客观 relation）

- 后端 `structural_factor_service.py` 扩展 V1.8 字段：dsa_segment 新增 current/prev 段收益、斜率、效率、段级成交量、段间对比；swing 新增 swing_range/price_position/retracement/rebound/bars_since；cost 新增 price_vs_poc_atr/value_area_position/nearest_node_*/distance_to_node_*_atr/node_*_strength；volatility 新增 distance_to_bb_*_atr/sqz_on/sqz_off/sqzmom_abs_percentile；participation 共享段级成交量；relation 移除 momentum_alignment，改为 primary_dir/secondary_dir/trend_alignment/primary_swing_position/secondary_swing_position/primary_slope_atr/secondary_slope_atr/secondary_vs_primary_position_delta。
- 段收益/斜率/效率一律基于 close，不再用 dsa_vwap 替代（修复 V1.7 bug）。
- 前端 `StockStructuralStatePanel.tsx` CARDS 扩展为 V1.8 完整字段，新增 `fmtBool` 格式化器；Relation 区块重写为客观关系字段。
- 前端 `endpoints.ts` `StructuralFactorResponse.relation` 类型同步更新。
- 后端新增 10 个 V1.8 测试（双周期差异、无未来函数、sqz_on/sqz_off、Relation primary_dir、段收益、Swing position、Node degraded、SQZMOM abs percentile 等），共 44/44 passed。
- 前端契约测试新增 V1.8 字段存在性断言（v18Keys 33 项 + v18RelationKeys 7 项），共 10/10 passed。
- 更新 `docs/current/02-data-api-contracts.md`（第 10 节 V1.8 完整字段表）、`04-frontend-ux.md`、`05-testing-acceptance.md`、`docs/maps/api-route-map.md`、`frontend-route-map.md`、`test-coverage-map.md`。
- 新增 CHANGE-20260705-032。

## 2026-07-05: 个股详情页新增结构状态因子面板（V1.7）

- 后端新增 ATR SSOT `app.strategy_assets.algorithms.features.atr_utils.compute_atr`（Pine RMA 等价）。
- 后端新增 `app.services.structural_factor_service.compute_structural_factors`：双周期（1d+15m）5 组结构因子（DSA 段/Swing/成本节点/动量波动/成交参与），每组独立 try/except 异常隔离。
- 后端新增 API `GET /api/v1/instruments/{id}/structural-factors`，无认证要求，250-500 bar lookback，15m 仅已完成 bar，Swing 无未来函数。
- 前端新增 `StockStructuralStatePanel.tsx`（5 卡片 + 双周期 tabs + 降级提示 + 明细折叠），`StockDetailPage` 改为双列布局（1fr + 340px），截图模式和窄屏（≤1250px）隐藏面板。
- 前端只渲染后端 DTO，禁止重新计算因子。
- 新增后端测试 34 个（ATR SSOT 9 + 服务 20 + API 5）、前端 contract test 8 个；后端 34/34 passed，前端 71/71 contract test passed。
- 更新 `docs/current/02-data-api-contracts.md`、`04-frontend-ux.md`、`05-testing-acceptance.md` 及相关 maps。
- 新增 CHANGE-20260705-031。

## 2026-07-05: 个股详情页新增 SQZMOM_LB 指标图层

- 后端新增 `app.strategy_assets.algorithms.features.sqzmom_lb`，逐行复刻 TradingView Pine `SQZMOM_LB`。
- `indicator_service.compute_all_indicators` 注入 `sqzmom_lb` 数据与图层；`/api/v1/instruments/{instrument_id}/indicators` 响应新增 `data.sqzmom_lb`。
- 前端 `StrategyChart.tsx` 新增 SQZMOM_LB 图层开关（默认关闭）和独立副图渲染；前端只消费后端 DTO，不重新计算指标。
- 新增后端测试 21 个、前端 contract test 5 个；后端 49/49 passed，前端 63/63 contract test passed。
- 更新 `docs/current/02-data-api-contracts.md`、`04-frontend-ux.md`、`05-testing-acceptance.md` 及相关 maps。
- 新增 CHANGE-20260704-030。

## 2026-07-04 Phase I: 趋势选股 result_id 未回填修复 + 生产验证

- PR #15 部署后发现 succeeded 行 `result_id` 全部为 None（PR #14 batch service 未回填）
- 修复 `query_run_items_with_results`：改用 `(run_id, instrument_id)` 关联 `strategy_results`（非 `result_id`）
- 修复 `_apply_run_item_filters` metric_filter 子查询：JOIN `strategy_results` + `strategy_result_metrics`
- 修复 sort LEFT JOIN：通过 `instrument_id` 关联（非 `result_id`）
- 生产验证通过：run_id=f0c15e1c, source_total=5293, succeeded 行正确显示 35 个 DSA 指标
- ALIGN-032 关闭（全量 universe 展示已验证）
- 新增 ALIGN-033（batch service 未回填 result_id，P2）
- 新增历史债务分级审计 AUDIT-20260704

## 2026-07-04 Phase H: 趋势选股页全量 Universe 展示

- 修复趋势选股页只显示 804 命中/4391 失败的问题：根因为 `/strategy-runs/{run_id}/results` 以 `strategy_results` 为主表（仅 succeeded 行）
- 后端改为以 `strategy_run_items` 为主表 LEFT JOIN `strategy_results` + `instruments`，返回全量 universe（含 succeeded/skipped/failed）
- 新增 `item_status`/`reason_code`/`error_message` 字段，skipped/failed 行 `id`/`payload` 为 null
- 前端 ScreenerPage 行 key 改用 `instrumentId`（不依赖 `result_id`），"命中"改名"筛选结果"
- 前端 adapter 支持 null id/payload 降级（`resultId=''`、`payload={}`）
- AGENTS.md 写入 node:20-alpine 保护规则（第 12 条）
- 新增 4 个后端测试 + 4 个前端 adapter 测试
- 新增 CHANGE-20260704-028，新增 ALIGN-032

## 2026-07-04 Phase G: DSA Run 总超时与 Computable Universe 口径修复

- 修复 DSA-only 运行后 1881 只 failed（全部 reason_code=timeout）：run 级总超时从 600s 改为 7200s（可配置 STRATEGY_RUN_TOTAL_TIMEOUT_SECONDS），与 after_close_orchestrator 对齐
- 新增 _classify_computable_universe：历史日线 < 60 根标的在 create_batch_run 时标记 skipped/insufficient_history，不进入计算循环
- 修复 execute_run 覆盖 skipped_count：初始化 skipped = run.skipped_count or 0，保留预置的 insufficient_history 数量
- run 级总超时耗尽后剩余 pending 项标记 failed/run_timeout_budget_exhausted，与单股 timeout 区分
- 新增 8 个测试用例，21 passed
- 新增 CHANGE-20260704-027，新增 ALIGN-031，更新 ALIGN-030

## 2026-07-04 Phase F: PR #11 部署后热修 bars/indicators page_size 上限

- 生产验证发现 15m/1h 个股详情请求触发 422：`/api/v1/instruments/{id}/bars` page_size 上限 1000，`/api/v1/instruments/{id}/indicators` bars 上限 500
- 将 bars page_size 上限提升至 4000，indicators bars 上限提升至 4000，与 Node Cluster 15m=4000、1h=1200 契约对齐
- 顺手修复 `backend/app/api/bars.py` Ruff 错误（未使用导入、缺失 `get_redis` 导入）
- 新增 CHANGE-20260704-023，更新 `docs/maps/api-route-map.md`

## 2026-07-04 Phase E: 修复 4 个生产功能缺陷

- 修复 DSA-only 覆盖率 0% 与系统概览 98% 口径不一致：新增 `BarsCoverageService` 统一三处重复 SQL，DSA-only 端点 fallback 到最新可用交易日
- 修复个股详情 K 线图未合并实时行情：前端新增 `mergeRealtimeQuoteIntoBars`，区分 baseBars（指标用）与 displayBars（图表用）
- 修复自选股监控列表空值：无 `MonitorState` 时通过 `MonitorSnapshotService` 只读 fallback 计算指标
- 修复飞书消息时间显示 UTC/+0：统一使用 `format_shanghai_datetime` 输出 Asia/Shanghai 时区
- 新增 5 个测试文件覆盖上述修复
- 更新 `docs/current/02-data-api-contracts.md`、`03-jobs-integrations-operations.md`、`04-frontend-ux.md`、`05-testing-acceptance.md` 及相关 maps

## 2026-07-02 Phase D: 剩余 Alignment 缺口修复

- 修复 ALIGN-019：`publish_run` 仅允许 `completed` 发布，拒绝 `partial_failed`
- monitor 行情统一走 `MarketDataAggregationService`，支持 `1m` 周期
- 修正 `monitor_batch_service.py` 陈旧注释（3600 → 4000 = 250×16）
- CI 改为三层 Ruff 门禁：`Ruff New Files` 阻断新增文件错误；`Ruff Baseline Regression` 阻断历史债务新增/增加；`Ruff Full Repository Report` 非阻断上传报告
- CI 改为三层 Mypy 门禁：`Mypy New Files` 阻断新增 backend/app 生产文件错误；`Mypy Baseline Regression` 阻断历史债务新增/增加/总数超基线；`Mypy Full Repository Report` 非阻断上传报告；基线 commit `64ed75c`、诊断总数 242（mypy 2.1.0 + numpy<2.5.0）、当前 241；`backend/pyproject.toml` 固定 mypy==2.1.0，并将 `numpy` 上限收紧为 `<2.5.0`；修复 mypy 报告步骤因历史错误提前失败的问题
- 修复本次新增 mypy 错误：`app/api/stock_detail_feishu.py` 自测代码使用 `getattr(route, "path", None)`；`app/repositories/bar_repository.py` 删除重复 `_query_minute_bars` 定义
- 修正文档 Commit 自引用：代码实现 Commit 与文档 Commit 分离，记录 `implementation_base_commit` / `verified_implementation_commit`
- ALIGN-014 在 GitHub Actions Run #36（最终 HEAD `a053d0c`）全部 blocking jobs 成功后关闭；ALIGN-018 同步关闭
- 测试：1106 passed（后端全量）；frontend tsc/lint/build 通过；frontend contract 52 passed

## 2026-07-02 Phase C: Platform App only + Capture 专用链路

- 永久删除 feishu_webhook_adapter，统一 feishu_platform_app（CHANGE-20260702-009）
- 新增 Capture 专用链路：`/capture/stock/:symbol` + `/api/v1/capture/stocks/{id}/snapshot`
- Capture Token 隔离：type=capture + scope=stock_detail_capture，普通 API 拒绝
- 状态机统一：截图失败返回 partial_failed + failed_step/error_code/error_message
- migration 055：CHECK 约束禁止 feishu_webhook
- 测试：1106 passed（新增 33 个飞书/Capture 相关测试）

| Change ID | 日期 | 标题 | 状态 | 分支 | Base Code Commit | Head/Merge Commit | 影响文档 |
|---|---|---|---|---|---|---|---|
| CHANGE-20260702-001 | 2026-07-02 | 建立并校正多维度当前设计基线 | ready_for_import | `docs/current-design-baseline` | `6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822` | 导入提交后填写 | 全部 current 文档 |
| CHANGE-20260702-002 | 2026-07-02 | 导入当前设计文档基线到修复分支 | committed | `fix/release-feishu-marketdata-dsa` | `6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822` | `a7b9ca91eba567b3ed3dbc4bb2884c4779471da2` | 全部 current 文档、AGENTS.md、.gitignore |
| CHANGE-20260702-003 | 2026-07-02 | 修复行情聚合服务 Redis 缓存开关未生效导致测试污染 | committed | `fix/release-feishu-marketdata-dsa` | `af3f55696a1abe0afe771a804528ff02b0f31a33` | `c22940d12addd61a4ff5fadca61dc69a7f8d9df4` | `backend/app/services/market_data_aggregation_service.py` |
| CHANGE-20260702-004 | 2026-07-02 | DSA 选股计算性能基准测试（350 只代表性股票） | committed | `fix/release-feishu-marketdata-dsa` | `9b842347e2d571b2b5acca309b7d95d853ce2da1` | `09f344b2633b45ac0431f480d9b6bf3a906657f8` | `backend/reports/dsa_benchmark_20260702.md` |
| CHANGE-20260702-005 | 2026-07-02 | Phase 6 文档对齐与旧术语清理 | committed | `fix/release-feishu-marketdata-dsa` | `a331a406ddf2e7b787a43788f4372436425c6d1` | `dc88c47625b22ca8a95f30d97036c6155e9a2cc4` | `docs/current/03-business-rules.md`、`10-permissions-security.md`、`11-jobs-integrations.md`、`12-strategy-indicator-contracts.md`、`18-code-doc-alignment.md` |
| CHANGE-20260702-006 | 2026-07-02 | Phase 7 全量测试与构建链路验证，修复测试旧术语断言 | committed | `fix/release-feishu-marketdata-dsa` | `ed476a050b1c562a994f82e23540d9c0492850c6` | `3dfeaca8c4fd7ed3cf6f14373aeedb98f9c6b8b2` | `backend/tests/test_me_entitlements.py`、`docs/changes/records/CHANGE-20260702-006.md` |
| CHANGE-20260702-007 | 2026-07-02 | 文档单一事实源治理与 AGENTS 项目硬规则 | committed | `chore/docs-governance-single-source` | `31f5776a247715f15713549211652dbb5a27d855` | `e6e8897` | `docs/数据结构.md`（删除）、`docs/操作手册.md`（删除）、`docs/指标参数基线.md`（删除）、`tools/update_docs.py`、`AGENTS.md`、`docs/current/*` |
| CHANGE-20260702-008 | 2026-07-02 | 恢复 Node Cluster 250×16 契约 | committed | `fix/node-cluster-250x16-contract` | `e6e8897` | `1ffb992` | `backend/app/constants/indicator_contract.py`、`backend/app/services/monitor_batch_service.py`、`backend/app/strategy_assets/algorithms/features/unified_volume_profile.py`、`backend/tests/*`、`docs/current/12-strategy-indicator-contracts.md`、`docs/current/18-code-doc-alignment.md` |
| CHANGE-20260702-009 | 2026-07-02 | Phase C - Platform App only + Capture 专用链路 | committed | `fix/feishu-platform-only-capture` | `1ffb992` | `64ed75c` | `backend/app/services/feishu_webhook_adapter.py`（删除）、`backend/app/api/capture.py`（新增）、`backend/app/core/security.py`、`backend/app/core/deps.py`、`backend/alembic/versions/055_feishu_platform_app_only.py`、`frontend/src/App.tsx`、`frontend/src/pages/CaptureStockPage.tsx`、`docs/current/09-api-contracts.md`、`docs/current/10-permissions-security.md`、`docs/current/11-jobs-integrations.md`、`docs/current/18-code-doc-alignment.md` |
| CHANGE-20260702-010 | 2026-07-02 | Phase D - 剩余 Alignment 缺口修复 + Ruff 三层增量阻断策略 + 文档自引用修正 | committed | `fix/release-remaining-alignment-gaps` | `64ed75c` | `ed8bcef` | `backend/app/services/strategy_batch_service.py`、`backend/app/services/monitor_batch_service.py`、`backend/app/services/market_data_aggregation_service.py`、`backend/app/repositories/bar_repository.py`、`.github/workflows/ci.yml`、`backend/tests/*`、`tools/quality_baselines/ruff.json`、`tools/compare_ruff_baseline.py`、`tools/check_architecture.py`、`tools/check_test_allowlist.py`、`AGENTS.md`、`docs/current/14-deployment-operations.md`、`docs/current/15-testing-acceptance.md`、`docs/current/18-code-doc-alignment.md`、`docs/changes/records/CHANGE-20260702-010.md` |
| CHANGE-20260702-011 | 2026-07-02 | Phase C/D - 真正接通 Capture Snapshot 链路与补齐图文状态机 + Mypy 增量阻断策略 | committed | `fix/release-remaining-alignment-gaps` | `8752f20` | `a053d0c` | `frontend/src/pages/CaptureStockPage.tsx`、`frontend/src/api/endpoints.ts`、`frontend/scripts/contract-tests/capture-stock-page.test.ts`、`backend/app/services/stock_capture_service.py`、`backend/app/services/stock_detail_feishu_service.py`、`backend/app/api/stock_detail_feishu.py`、`backend/app/repositories/bar_repository.py`、`backend/pyproject.toml`、`backend/tests/test_capture_snapshot.py`、`backend/tests/test_capture_token_isolation.py`、`backend/tests/test_state_machine.py`、`backend/tests/test_stock_detail_feishu_status.py`、`.github/workflows/ci.yml`、`tools/check_mypy_new_files.py`、`tools/compare_mypy_baseline.py`、`tools/generate_mypy_baseline.py`、`tools/quality_baselines/mypy.json`、`AGENTS.md`、`advice.md`、`docs/current/14-deployment-operations.md`、`docs/current/15-testing-acceptance.md`、`docs/current/18-code-doc-alignment.md` |
| CHANGE-20260703-013 | 2026-07-03 | 修复 release candidate 新增 backend/app 文件 mypy 错误，解除 Mypy New Files CI 阻断 | committed | `release/docs-aligned-candidate-v3` | `82e4afd` | `d5f69d1` | `backend/app/services/subscription_service.py`、`backend/app/services/market_data_aggregation_service.py`、`backend/app/models/access_audit_log.py`、`backend/app/scripts/fix_instruments_remove_indices.py`、`backend/app/api/capture.py`、`backend/app/api/admin_subscription.py`、`docs/current/15-testing-acceptance.md` |
| CHANGE-20260703-014 | 2026-07-03 | 删除独立管理员飞书渠道配置，管理员通知复用管理员用户自己的 feishu_platform_app NotificationChannel | committed | `fix/admin-notification-use-admin-channel` | `5cf0426` | `5cf0426` | `backend/app/constants/system_channel.py`（删除）、`backend/app/services/outbox_relay.py`、`backend/app/services/beta_application_notifier.py`、`backend/app/services/beta_application_service.py`、`backend/app/services/delivery_worker.py`、`backend/app/services/feishu_card_builder.py`、`docker-compose.prod.yml`、`tools/pre_deploy_check.py`、`backend/tests/*`、`docs/current/11-jobs-integrations.md`、`docs/current/18-code-doc-alignment.md` |
| CHANGE-20260703-015 | 2026-07-03 | outbox target_channel_id 跳过 eligible_user_service（ddca659 hotfix 治理闭环） | committed | `chore/governance-baseline-repair-v2` | `ddca659b8c9d64b6a414da0b4bbd6f80f704aef1` | `bbf6215` | `backend/tests/test_outbox_target_channel_id.py`、`docs/current/18-code-doc-alignment.md`、`tools/check_docs_consistency.py`、`tools/tests/test_check_docs_consistency.py`、`docs/current/*.md`、`docs/README.md` |
| CHANGE-20260703-016 | 2026-07-03 | 修复 worker_heartbeats 僵尸 running 记录清理机制 | committed | `fix/worker-heartbeat-stale-cleanup` | `40dd2287f0962910d2e272c468b3e5054abddaaf` | `095c4ad` | `backend/app/worker.py`、`backend/tests/test_worker_heartbeat_stale_cleanup.py`、`docs/current/11-jobs-integrations.md`、`docs/current/18-code-doc-alignment.md` |
| CHANGE-20260703-017 | 2026-07-03 | docs 信息架构重构为 v2 system map（current + maps + onboarding + restore checklist） | merged | `docs/restructure-system-map-v2` | `40dd2287f0962910d2e272c468b3e5054abddaaf` | `cafbdc4` | `docs/current/*`（旧 00-18 归档至 `docs/archive/current-legacy-20260703/`）、`docs/maps/*`、`docs/AI-ONBOARDING.md`、`docs/RESTORE-CHECKLIST.md`、`docs/MAINTENANCE.md`、`docs/MIGRATION-MAP.md`、`docs/TRAE-APPLY-INSTRUCTION.md`、`docs/SOURCE-SNAPSHOT.md`、`docs/README.md`、`docs/changes/records/CHANGE-20260703-017.md`、`docs/changes/CHANGELOG.md`、`tools/check_docs_consistency.py`、`tools/tests/test_check_docs_consistency.py`、`tools/update_docs.py`、`tools/check_architecture.py` |
| CHANGE-20260704-018 | 2026-07-04 | v2 docs 治理收口 + v2 结构检查加强（8 map 全检 + 测试） | committed | `chore/docs-v2-governance-finalize` | `cafbdc4217301d8bf00ff9d42aeabbef43eb58fb` | 待合并后填写 | `docs/current/code-doc-alignment.md`、`docs/current/MANIFEST.md`、`docs/changes/records/CHANGE-20260703-017.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-018.md`、`tools/check_architecture.py`、`tools/tests/test_check_architecture.py` |
| CHANGE-20260704-019 | 2026-07-04 | 新增生产 worker-watchdog 服务让 _recovery_watchdog_loop 在生产运行 | merged | `fix/worker-watchdog-production-service` | `b4b5918c23df2b21a1f54e0e81aaa323f287e150` | `67105c2` | `docker-compose.prod.yml`、`docs/current/03-jobs-integrations-operations.md`、`docs/maps/worker-job-map.md`、`docs/maps/deployment-runtime-map.md`、`docs/current/code-doc-alignment.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-019.md` |
| CHANGE-20260704-020 | 2026-07-04 | 关闭 ALIGN-023：worker-watchdog 生产验证 stale running 清零 | merged | `chore/close-align-023-worker-watchdog` | `67105c2` | `30ddc8a` | `docs/current/code-doc-alignment.md`、`docs/current/03-jobs-integrations-operations.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-020.md` |
| CHANGE-20260704-021 | 2026-07-04 | worker/notification/capture 边界审计 + 后续小 PR 拆分计划 | committed | `chore/boundary-audit-worker-notification-capture` | `30ddc8a` | 待合并后填写 | `docs/architecture-audits/AUDIT-20260704-worker-notification-capture-boundaries.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-021.md`、`docs/maps/worker-job-map.md`、`docs/maps/notification-flow-map.md`、`docs/maps/test-coverage-map.md`、`docs/current/code-doc-alignment.md` |
| CHANGE-20260704-022 | 2026-07-04 | 修复 4 个生产功能缺陷：DSA-only 覆盖率口径、K 线实时行情合并、自选股监控 fallback、飞书消息中国时区；残留修复：覆盖率门禁使用 `coverage_raw`、watchlist fallback 条件扩展、1d K 线日期语义 | committed | `fix/market-data-dsa-watchlist-feishu-timezone` | `4af271d` | 待提交后填写 | `backend/app/services/bars_coverage_service.py`、`backend/app/core/time.py`、`backend/app/api/admin_after_close.py`、`backend/app/api/watchlist.py`、`backend/app/services/after_close_orchestrator.py`、`backend/app/services/bars_scheduler_service.py`、`backend/app/services/message_builder.py`、`backend/app/services/monitor_batch_service.py`、`backend/app/services/notification_service.py`、`backend/app/services/stock_detail_feishu_service.py`、`backend/app/services/system_overview_service.py`、`frontend/src/utils/chart.ts`、`frontend/src/pages/StockDetailPage.tsx`、测试文件、docs |
| CHANGE-20260704-023 | 2026-07-04 | PR #11 部署后热修：bars / indicators page_size、bars 上限与 Node Cluster 15m/1h 契约对齐 | in_validation | `fix/bars-indicators-page-size-15m` | `0f29e5e` | 待合并后填写 | `backend/app/api/bars.py`、`backend/app/api/indicators.py`、`docs/maps/api-route-map.md`、`docs/changes/CHANGELOG.md` |
| CHANGE-20260704-024 | 2026-07-04 | 自选监控页 UI 调整、AGENTS 无备份部署规则、TCL 科技单标历史回补 | committed | `fix/bars-indicators-page-size-15m` | `43e2334` | 待合并后填写 | `frontend/src/features/watchlist-monitor/*`、`frontend/src/pages/WatchlistPage.tsx`、`frontend/src/styles/global.scss`、`frontend/package.json`、`backend/tools/backfill_single_instrument.py`、`AGENTS.md`、`docs/current/02-data-api-contracts.md`、`docs/current/04-frontend-ux.md`、`docs/current/code-doc-alignment.md`、`docs/maps/api-route-map.md`、`docs/maps/frontend-route-map.md`、`docs/maps/test-coverage-map.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-024.md` |
| CHANGE-20260704-025 | 2026-07-04 | Admin Jobs 可观察性补齐 - Worker 心跳 Tab + 只读 admin API | committed | `feat/admin-jobs-observability` | `0f29e5e` | 待合并后填写 | `backend/app/schemas/worker_heartbeat.py`、`backend/app/api/admin_subscription.py`、`backend/tests/test_admin_worker_heartbeats_api.py`、`frontend/src/api/endpoints.ts`、`frontend/src/hooks/useApi.ts`、`frontend/src/pages/AdminJobsPage.tsx`、`docs/current/02-data-api-contracts.md`、`docs/current/03-jobs-integrations-operations.md`、`docs/current/04-frontend-ux.md`、`docs/maps/api-route-map.md`、`docs/maps/frontend-route-map.md`、`docs/maps/worker-job-map.md`、`docs/maps/test-coverage-map.md`、`docs/current/code-doc-alignment.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-025.md` |
| CHANGE-20260704-027 | 2026-07-04 | DSA Run 总超时与 Computable Universe 口径修复 | committed | `fix/dsa-run-timeout-and-computable-universe` | 待填写 | 待合并后填写 | `backend/app/services/strategy_batch_service.py`、`backend/tests/test_strategy_batch_service.py`、`docker-compose.prod.yml`、`docs/current/02-data-api-contracts.md`、`docs/current/03-jobs-integrations-operations.md`、`docs/current/code-doc-alignment.md`、`docs/maps/*`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-027.md` |
| CHANGE-20260704-028 | 2026-07-04 | 趋势选股页全量 universe 展示：主表改 strategy_run_items LEFT JOIN strategy_results，行 key 改 instrumentId，"命中"改名"筛选结果"，AGENTS 写入 node:20-alpine 保护规则 | merged | `fix/screener-full-universe-results` | `d47bb46` | `44d37fd` | `backend/app/repositories/strategy_result_repository.py`、`backend/app/services/selector_query_service.py`、`backend/app/schemas/strategy_run.py`、`backend/app/api/strategy_runs.py`、`backend/app/models/strategy_run.py`、`backend/tests/test_strategy_results_universe.py`、`backend/tests/test_business_integration.py`、`backend/tests/test_selector_query_integration.py`、`frontend/src/api/endpoints.ts`、`frontend/src/features/trend-selection/adapters.ts`、`frontend/src/features/trend-selection/__tests__/adapter.test.ts`、`frontend/src/pages/ScreenerPage.tsx`、`AGENTS.md`、`docs/AI-ONBOARDING.md`、`docs/current/02-data-api-contracts.md`、`docs/current/04-frontend-ux.md`、`docs/current/code-doc-alignment.md`、`docs/maps/api-route-map.md`、`docs/maps/frontend-route-map.md`、`docs/maps/deployment-runtime-map.md`、`docs/maps/test-coverage-map.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-028.md` |
| CHANGE-20260704-029 | 2026-07-04 | 趋势选股 result_id 未回填修复：改用 (run_id, instrument_id) 关联 strategy_results + 历史债务审计 | in_validation | `fix/screener-result-join-by-instrument` | `44d37fd` | 待合并后填写 | `backend/app/repositories/strategy_result_repository.py`、`docs/current/code-doc-alignment.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-029.md`、`docs/architecture-audits/AUDIT-20260704-ruff-mypy-debt-triage.md` |

## 规则

- 当前设计直接写现在确认的状态；
- 历史前后差异写入 CHANGE；
- 编码前建立记录，完成后补全真实分支、Commit、测试和遗留事项；
- 纯样式、测试、配置、性能、依赖和死代码清理同样需要记录；
- 未产生 Head Commit 时可以写“导入提交后填写”，但合并前必须补全。
