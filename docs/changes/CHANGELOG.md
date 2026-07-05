# 项目修改索引

本文件只做索引。每次代码、配置、测试、部署或当前设计变化，都必须使用独立分支并在 `records/` 下建立独立记录。

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
