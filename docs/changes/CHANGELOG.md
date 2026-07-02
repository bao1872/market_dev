# 项目修改索引

本文件只做索引。每次代码、配置、测试、部署或当前设计变化，都必须使用独立分支并在 `records/` 下建立独立记录。

| Change ID | 日期 | 标题 | 状态 | 分支 | Base Code Commit | Head/Merge Commit | 影响文档 |
|---|---|---|---|---|---|---|---|
| CHANGE-20260702-001 | 2026-07-02 | 建立并校正多维度当前设计基线 | ready_for_import | `docs/current-design-baseline` | `6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822` | 导入提交后填写 | 全部 current 文档 |
| CHANGE-20260702-002 | 2026-07-02 | 导入当前设计文档基线到修复分支 | committed | `fix/release-feishu-marketdata-dsa` | `6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822` | `a7b9ca91eba567b3ed3dbc4bb2884c4779471da2` | 全部 current 文档、AGENTS.md、.gitignore |
| CHANGE-20260702-003 | 2026-07-02 | 修复行情聚合服务 Redis 缓存开关未生效导致测试污染 | committed | `fix/release-feishu-marketdata-dsa` | `af3f55696a1abe0afe771a804528ff02b0f31a33` | `c22940d12addd61a4ff5fadca61dc69a7f8d9df4` | `backend/app/services/market_data_aggregation_service.py` |
| CHANGE-20260702-004 | 2026-07-02 | DSA 选股计算性能基准测试（350 只代表性股票） | committed | `fix/release-feishu-marketdata-dsa` | `9b842347e2d571b2b5acca309b7d95d853ce2da1` | `09f344b2633b45ac0431f480d9b6bf3a906657f8` | `backend/reports/dsa_benchmark_20260702.md` |
| CHANGE-20260702-005 | 2026-07-02 | Phase 6 文档对齐与旧术语清理 | committed | `fix/release-feishu-marketdata-dsa` | `a331a406ddf2e7b7837a43788f4372436425c6d1` | `dc88c47625b22ca8a95f30d97036c6155e9a2cc4` | `docs/current/03-business-rules.md`、`10-permissions-security.md`、`11-jobs-integrations.md`、`12-strategy-indicator-contracts.md`、`18-code-doc-alignment.md` |
| CHANGE-20260702-006 | 2026-07-02 | Phase 7 全量测试与构建链路验证，修复测试旧术语断言 | committed | `fix/release-feishu-marketdata-dsa` | `ed476a050b1c562a994f82e23540d9c0492850c6` | `3dfeaca8c4fd7ed3cf6f14373aeedb98f9c6b8b2` | `backend/tests/test_me_entitlements.py`、`docs/changes/records/CHANGE-20260702-006.md` |
| CHANGE-20260702-007 | 2026-07-02 | 文档单一事实源治理与 AGENTS 项目硬规则 | committed | `chore/docs-governance-single-source` | `31f5776a247715f15713549211652dbb5a27d855` | `e6e8897` | `docs/数据结构.md`（删除）、`docs/操作手册.md`（删除）、`docs/指标参数基线.md`（删除）、`tools/update_docs.py`、`AGENTS.md`、`docs/current/*` |
| CHANGE-20260702-008 | 2026-07-02 | 恢复 Node Cluster 250×16 契约 | committed | `fix/node-cluster-250x16-contract` | `e6e8897` | 提交后填写 | `backend/app/constants/indicator_contract.py`、`backend/app/services/monitor_batch_service.py`、`backend/app/strategy_assets/algorithms/features/unified_volume_profile.py`、`backend/tests/*`、`docs/current/12-strategy-indicator-contracts.md`、`docs/current/18-code-doc-alignment.md` |

## 规则

- 当前设计直接写现在确认的状态；
- 历史前后差异写入 CHANGE；
- 编码前建立记录，完成后补全真实分支、Commit、测试和遗留事项；
- 纯样式、测试、配置、性能、依赖和死代码清理同样需要记录；
- 未产生 Head Commit 时可以写“导入提交后填写”，但合并前必须补全。
