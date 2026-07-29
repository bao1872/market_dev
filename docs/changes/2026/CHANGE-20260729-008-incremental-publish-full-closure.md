# CHANGE-20260729-008 增量发布最终收口

**日期**：2026-07-29
**类型**：架构收口 + 功能完成
**关联**：CHANGE-20260729-006 / CHANGE-20260729-007
**提交**：本轮 dev 最终提交 SHA

## 摘要

完成增量发布重构的最后闭环：Worker 真实接入 run items、市场列表绑定 publication pointer、历史回补 DB-only CLI、管理状态 API、市场聚合独立 job、事件 outbox 模型支持。

## 修改前

- Worker 仍调用 `compute_review_core_batch_for_trade_date` 共享 session 批量模式
- `market_stocks` 的 LATERAL 子查询按每股 latest，可能跨 run 混读
- 历史回补仅有 `backfill_first_pyramid_history_batch` dict 版本
- 无管理状态 API
- 无市场聚合独立 job
- `first_pyramid_history_run_items` 缺少 worker/lease 字段

## 修改后

### 后端代码闭环

1. **Worker 真实接入 run items**：`feature_snapshot_service.compute_review_core_with_run_items`
   - 调用 `create_run_items` / `claim_items` / `mark_item_succeeded` / `mark_item_failed`
   - 每股独立 AsyncSession，计算与 commit 独立，失败只回滚该股
   - coverage 从 DB 实时统计
   - `after_close_orchestrator` 主链切换到该入口

2. **market_stocks LATERAL 绑定 pointer**：
   - `_build_snap_lateral(snapshot_run_id=...)` 严格过滤已发布 run
   - `get_market_stocks` 在构建 LATERAL 前先读取 publication pointer
   - 无 pointer 时回退每股 latest（兼容历史数据）

3. **history run/item 真实接入 + DB-only CLI**：
   - 新增 `backfill_history_with_run_items` + `create_history_run` + `create_history_run_items`
     + `claim_history_items` + `mark_history_item_*` + `finish_history_run`
   - 单股独立事务 + lease_epoch fencing
   - `_fetch_db_only_daily_bars` 直接调 `_query_daily_bars`，禁止 pytdx 自动拉取
   - 新 CLI：`scripts/first_pyramid_history_backfill_cli.py`，支持 `--canary/--limit/--all/--symbols/--resume/--dry-run/--output-bars`

4. **管理状态 API**：`backend/app/api/admin_incremental_publish.py`
   - `GET /admin/incremental-publish/status`：core/aggregation/history/pointer 综合状态
   - `GET /admin/incremental-publish/core/runs` + `/{snapshot_run_id}/progress`：snapshot run 列表 + 进度 + 失败清单
   - `GET /admin/incremental-publish/history/runs` + `/{history_run_id}/progress`：history run 列表 + 进度 + 失败清单
   - `GET /admin/incremental-publish/pointers`：publication pointer 列表

5. **市场聚合独立 job**：`market_factor_aggregation_service.run_market_factor_aggregation`
   - 读取已发布 stock_core pointer，校验后切 market_aggregation pointer
   - 失败只重跑聚合，不回滚核心

6. **事件 outbox 模型支持**：
   - `StockFeatureSnapshotRunItem.phase='event_outbox'` 已定义，可独立 claim/commit
   - 实际事件写入由 `stock_state_event` 表（稳定唯一键幂等）承载

### 迁移调整（073，生产未应用）

- `first_pyramid_history_run_items` 表新增 `worker_instance_id / lease_epoch / lease_expires_at / started_at / heartbeat_at` 字段
- 新增 `ix_history_run_items_lease_expires` 索引
- ORM `FirstPyramidHistoryRunItem` 同步更新

## 受影响

- 后端：盘后核心计算主链改为单股事务，发布门禁基于 DB 实时 coverage
- 后端：市场列表读取固定指向已发布 run，禁止跨 run 混读
- 后端：历史回补支持 canary/全量/resume，DB-only
- 后端：管理后台新增增量发布状态视图
- 迁移：073 schema 调整（生产未应用，可保留修正版）

## 验证

- 27 个纯单元测试 PASS（含 ORM 字段、claim/mark、coverage、pointer 校验）
- 6 个 PG 集成测试 SKIP（PURE_UNIT_TEST=1，待 CI 临时 PG 容器运行）
- Ruff check PASS
- TSC：StockDetailPage.tsx 通过（仅 1 个 pre-existing tsconfig.node.json 配置警告）
- ESLint：StockDetailPage.tsx PASS
- 本地 Backend/Frontend/Capture 服务健康（200/200/200）

## 未解决

- PG 集成测试待 CI 运行（fault injection / resume / coverage gating / 并发 claim / pointer 防倒退）
- 浏览器 AUTH_WALL 受限，UI 视觉验收待部署后生产 URL 验证
- 服务器迁移、build 部署、canary、全量回补：本轮代码闭环后连续执行
