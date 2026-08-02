# CHANGE-20260729-006：盘后编排与历史回补增量检查点/分层发布重构

> **历史状态（2026-08-02 治理收口）**
> - `historical_status`: historical（代码事实保留；"分层发布"流程术语已弃用）
> - `superseded_by`: CHANGE-20260802-003（开发治理收口弃用多阶段发布术语）
> - `current_authority`: `rules/80-deployment-data-safety.md` + `docs/runbooks/development-deployment.md`
> - 说明：增量发布 / 分层发布的"发布阶段"术语已废止；本 Change 记录的盘后编排与历史回补实现仍属历史事实。

状态：历史（代码事实；"分层发布"发布流程术语已弃用）
日期：2026-07-29
类型：architecture + behavior + contract
领域：盘后编排 / 历史回补 / 分层发布

相关 PRD：
- `../../prd/30-after-close.md`：AC-08 / AC-09 / AC-10 / AC-14（计算与发布分离、发布指针、两阶段发布、部分失败）
- `../../prd/20-quant-model.md`：QM-01~QM-43（第一金字塔）、QM-60~QM-62（事件与连续因子分离）

相关 Maps：
- `../../maps/30-after-close.md`：盘后链路、状态机、发布指针
- `../../maps/20-quant-model.md`：第一金字塔 SSOT 调用链

相关 Rules：
- `../../../rules/80-deployment-data-safety.md`：§分层发布与增量检查点纪律（新增）

相关提交：
- 基线：d1bbf02（dev = origin/dev）
- 本轮 commit：5152766（已 push origin/dev）

替代：无

被替代：无

## 1. 摘要

本轮在 dev 分支引入"单股×阶段"为最小计算/事务/检查点粒度的增量发布架构，新增 4 张表（073 迁移）和 2 个服务，统一 ID 合同，并补齐历史回补 run 级追踪。

关键变化：
1. 新增 `stock_feature_snapshot_run_items` 表，以 `(snapshot_run_id, instrument_id, phase)` 为唯一键，支持 per-stock commit、失败隔离、断点恢复。
2. 新增 `first_pyramid_history_runs` / `first_pyramid_history_run_items` 表，记录历史回补 run 级进度和单股 item 状态。
3. 新增 `factor_publications` 表，分层发布指针（stock_core / market_aggregation / history_cross_section），发布只做小事务原子切换指针，不复制结果数据。
4. 统一 ID 合同：`chip.core_run_id = snapshot_run_id`（不再指向 `SchedulerJobRun.id`）。
5. 新增 `CORE_PUBLICATION_MIN_COVERAGE = 0.98` 门禁，覆盖率达标后才能切 stock_core pointer。

## 2. 背景与问题

### 2.1 旧状态问题

1. **batch 即发布边界**：原 `compute_review_core_batch_for_trade_date` 在一个事务内批量计算全部股票，单股失败会回滚整批，导致已成功股票被丢失；batch_size 既是吞吐控制又是发布边界，违反"单股失败不影响其他股票"原则。
2. **ID 双义**：`chip.core_run_id` 字段名义为"core run id"，实际写入 `SchedulerJobRun.id`（任务追踪 ID），而 `StockFeatureSnapshotRun.id`（数据版本 ID）才是真正的核心数据版本。一列双义使查询语义混乱。
3. **无覆盖率门禁**：原 `publish_run` 只检查 `StrategyRun.status=completed`，没有显式的"覆盖率 ≥ 98%"门禁，部分失败仍可发布。
4. **无 publication pointer**：API 通过 `published_at IS NOT NULL` 判断发布状态，无法原子切换数据版本，新旧 run 数据可能混读。
5. **历史回补无 run 追踪**：`backfill_first_pyramid_history_batch` 返回 dict，没有持久化 run/item 级状态，无法断点恢复。
6. **重启重算已成功股票**：原中断恢复无 item 粒度检查点，重启后整批重算，浪费资源且可能覆盖已发布结果。

### 2.2 风险

- 单股失败导致整批回滚：生产环境某只停牌股票会阻塞全市场发布。
- ID 双义：chip 查询可能匹配到错误的 job_run_id，返回错误数据。
- 无门禁：部分失败（如 90% 成功）仍可发布，用户看到的数据不完整。

## 3. 变化前

- `after_close_orchestrator.execute_after_close_run` 调用 `compute_review_core_batch_for_trade_date` 批量计算。
- `chip.core_run_id` 写入 `job_run_id`（SchedulerJobRun.id）。
- `publish_run` 只检查 `StrategyRun.status=completed`。
- API 通过 `StockFeatureSnapshotRun.published_at IS NOT NULL` 判断发布。
- 历史回补 `backfill_first_pyramid_history_batch` 返回内存 dict，无持久化。

## 4. 变化内容

### 4.1 新增表（migration 073，纯新增，不改 071/072）

| 表名 | 唯一键 | 职责 |
|---|---|---|
| `stock_feature_snapshot_run_items` | `(snapshot_run_id, instrument_id, phase)` | 单股×阶段检查点，per-stock commit 粒度 |
| `first_pyramid_history_runs` | `id` | 历史回补 run 级追踪 |
| `first_pyramid_history_run_items` | `(history_run_id, instrument_id)` | 历史回补单股 item |
| `factor_publications` | `(scope_type, scope_key, trade_date, publication_kind)` | 分层发布指针 |

### 4.2 ID 合同统一

| ID | 含义 | 写入位置 |
|---|---|---|
| `orchestrator_job_run_id` | `SchedulerJobRun.id`（任务追踪） | `FirstPyramidHistoryRun.scheduler_job_run_id`（metadata） |
| `snapshot_run_id` | `StockFeatureSnapshotRun.id`（当日核心数据版本） | `RunItem.snapshot_run_id` / `StockChipConsensusSnapshot.core_run_id` / `FactorPublication.data_run_id` |
| `history_run_id` | `FirstPyramidHistoryRun.id`（历史回补版本） | `FactorPublication.data_run_id`（kind=history_cross_section） |
| `publication.data_run_id` | 指向数据版本 | 见上 |

**关键变更**：`after_close_orchestrator.py` 中 `create_after_close_chip_consensus_job(core_run_id=snapshot_run_id)`，不再传 `job_run_id`。

### 4.3 新增服务

| 服务 | 模块 | 核心函数 |
|---|---|---|
| Run Item 服务 | `app.services.snapshot_run_item_service` | `create_run_items` / `claim_items` / `mark_item_succeeded` / `mark_item_failed` / `mark_item_skipped` / `get_run_progress` / `get_resume_items` / `recover_stale_running_items` |
| 分层发布服务 | `app.services.factor_publication_service` | `compute_coverage` / `publish_stock_core` / `publish_market_aggregation` / `publish_history_cross_section` / `get_publication` / `get_published_snapshot_run_id` |

### 4.4 关键设计

1. **claim 原子性**：`claim_items` 使用 `UPDATE ... WHERE status IN ('pending','failed','running'+lease过期) ... FOR UPDATE SKIP LOCKED RETURNING`，确保并发 Worker 不重复领取。
2. **lease_epoch fencing**：`mark_item_succeeded/failed/skipped` 支持 `lease_epoch` 参数，旧 Worker 写入被拒绝（rowcount=0）。
3. **coverage 门禁**：`publish_stock_core` 默认 `threshold=CORE_PUBLICATION_MIN_COVERAGE=0.98`，低于门禁抛 `CoverageBelowThresholdError`。
4. **原子指针切换**：`publish_stock_core` 使用 `pg_insert(...).on_conflict_do_update(constraint="uq_factor_publications_scope_date_kind")` 原子切换。
5. **兼容回退**：`get_published_snapshot_run_id` 优先读 publication pointer，无 pointer 时回退到 `StockFeatureSnapshotRun.published_at IS NOT NULL`。

## 5. 变化后

- 新表为空，部署后由新代码异步填充，不影响现有读链路。
- `chip.core_run_id` 语义统一为 `snapshot_run_id`，新代码写入正确；旧数据不回填（不影响新版本执行）。
- publication pointer 建立后，API 优先读 pointer；无 pointer 时兼容回退到 `published_at`。
- Worker 重启只处理 pending / 可重试 failed / lease 过期 running；succeeded 且 hash/version 不变的不重算。

## 6. 影响范围

### API 或契约

- 新增：管理状态 API（未实现，本轮只提供 service 层；API 待后续 PR）
- 兼容：现有列表/详情 API 行为不变（无 publication pointer 时回退到 published_at）

### 数据

- 新增 4 张表（073 migration）
- 不修改 071/072 已有表结构
- 不回填历史数据

### 后端

- 新增 4 个 ORM 模型 + 2 个服务模块
- 修改 `after_close_orchestrator.py`：chip job 创建时传 `snapshot_run_id` 而非 `job_run_id`

### Worker 与任务

- 新增 `claim_items` 提供给 Worker 原子领取 item（未集成到现有 Worker，本轮只提供 service 层）
- 现有 Worker 不受影响（保持 legacy 模式）

### 部署与运行

- 073 migration 需在部署前执行（upgrade/downgrade/upgrade 已设计，待 CI 隔离 Postgres 验证）
- 新代码不影响现有运行流程

## 7. 迁移与兼容

- **Migration 073**：纯新增 4 张表，无破坏性变更，upgrade/downgrade 对称。
- **兼容回退**：`get_published_snapshot_run_id` 无 pointer 时回退到 `published_at`。
- **chip.core_run_id 语义统一**：071 migration 的 FK 仍指向 `scheduler_job_runs`（历史遗留），本轮不改 071；新代码通过 orchestrator 传 `snapshot_run_id` 实现语义统一。未来 migration 可修复 FK 指向。
- **旧数据不回填**：新表为空，由新代码异步填充。

## 8. 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| 纯单元测试 | 27 项 | PASS | `PURE_UNIT_TEST=1 pytest tests/test_incremental_publication.py`（27 passed, 6 skipped） |
| PostgreSQL 集成测试 | 6 项 | SKIP（待 CI） | `PURE_UNIT_TEST=1` 时跳过，CI 临时 Postgres 容器运行 |
| Ruff 静态检查 | 9 个修改文件 | PASS | `ruff check` 全部通过 |
| 故障注入模拟 | 3 股中第 2 股失败 | PASS（纯单元） | `test_second_stock_fails_others_succeed` 通过 |
| 中断恢复不重算 | succeeded item 不在 resume 列表 | PASS（纯单元） | `test_resume_does_not_recompute_succeeded` 通过 |
| 覆盖率门禁 | 0.5 < 0.98 拒绝、0.98 ≥ 0.98 允许 | PASS（纯单元） | `test_coverage_below_threshold_blocks_publish` / `test_coverage_at_threshold_allows_publish` 通过 |
| 并发 claim 不重复 | Worker 2 领取已 claim 的 item | SKIP（待 CI） | `test_concurrent_claim_no_duplicate` 在 CI 运行 |
| publication 一致性 | 不同 run 不混读 | SKIP（待 CI） | `test_different_runs_no_mixed_read` 在 CI 运行 |
| 本地服务健康 | Backend/Capture/Frontend | PASS | `curl /health` 200 |

## 9. 文档更新

| 文档 | 更新内容 |
|---|---|
| PRD | 无变化（本轮只实现已有 PRD 的 AC-08/09/10/14） |
| Maps | `maps/30-after-close.md` 新增 §11 增量检查点与分层发布；`maps/20-quant-model.md` 无变化 |
| Runbooks | 无变化 |
| Rules | `rules/80-deployment-data-safety.md` 新增 §分层发布与增量检查点纪律 |

## 10. 回滚方案

- 代码回滚：`git revert` 本轮 commit
- 数据回滚：`alembic downgrade -1`（drop 4 张新表，不影响现有数据）
- 无需恢复旧 Schema 或发布指针（新表为空，回滚后无影响）

## 11. 遗留问题与风险

1. **PostgreSQL 集成测试未在本地运行**：6 项集成测试（含故障注入、并发 claim、publication 一致性）在 CI 临时 Postgres 容器中运行，本地 PURE_UNIT_TEST=1 跳过。
2. **Worker 未集成 run item**：本轮只提供 service 层，未集成到现有 `after_close_orchestrator_worker`；现有 Worker 保持 legacy 模式。
3. **API 未实现**：管理状态 API（core/aggregation/events/chip 状态分别返回）未实现，本轮只提供 service 层。
4. **chip.core_run_id FK 指向**：071 migration 的 FK 仍指向 `scheduler_job_runs`，本轮不改 071；未来 migration 可修复 FK 指向 `stock_feature_snapshot_runs`。
5. **历史回补 CLI**：`--symbols/--limit/--batch-size/--output-bars/--dry-run/--resume/--algorithm-version` 受控 CLI 未实现，本轮只提供 `backfill_first_pyramid_history_batch` 服务层。
6. **六个门禁未通过**：main 合并、服务器部署、core canary、history canary、全量回补、chip 回补均未执行。

## 12. 后续变化

- 后续 PR：将 run item service 集成到 `after_close_orchestrator_worker`，实现真正的 per-stock commit。
- 后续 PR：实现管理状态 API，分别返回 core/aggregation/events/chip 状态。
- 后续 PR：实现历史回补受控 CLI。
- 后续 migration：修复 `chip.core_run_id` FK 指向 `stock_feature_snapshot_runs`。
