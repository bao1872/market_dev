# 项目修改索引

本文件只做索引。每次代码、配置、测试、部署或当前设计变化，都必须使用独立分支并在 `records/` 下建立独立记录。

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
| CHANGE-20260703-013 | 2026-07-03 | 修复 release candidate 新增 backend/app 文件 mypy 错误，解除 Mypy New Files CI 阻断 | committed | `release/docs-aligned-candidate-v3` | `82e4afd` | `d4bff8c` | `backend/app/services/subscription_service.py`、`backend/app/services/market_data_aggregation_service.py`、`backend/app/models/access_audit_log.py`、`backend/app/scripts/fix_instruments_remove_indices.py`、`backend/app/api/capture.py`、`backend/app/api/admin_subscription.py`、`docs/current/15-testing-acceptance.md` |

## 规则

- 当前设计直接写现在确认的状态；
- 历史前后差异写入 CHANGE；
- 编码前建立记录，完成后补全真实分支、Commit、测试和遗留事项；
- 纯样式、测试、配置、性能、依赖和死代码清理同样需要记录；
- 未产生 Head Commit 时可以写“导入提交后填写”，但合并前必须补全。
