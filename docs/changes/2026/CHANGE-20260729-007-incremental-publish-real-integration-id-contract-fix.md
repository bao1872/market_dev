# CHANGE-20260729-007：增量发布真实接入收口 + ID 合同修复 + 个股自选按钮微缩

状态：PARTIAL（代码+纯单元测试+Ruff+TSC+ESLint 通过；PG 集成测试待 CI；浏览器验收 AUTH_WALL_BLOCKED；Worker run item 集成、market_stocks LATERAL pointer 接入、历史回补真实接入均未完成）
日期：2026-07-29
类型：architecture + bugfix + contract + UI
领域：盘后编排 / 分层发布 / ID 合同 / 个股详情

相关 PRD：
- `docs/prd/30-after-close.md` §4：AC-08/09/10/14（增量发布架构需求）
- `docs/prd/40-market-stock-experience.md`：个股详情自选按钮位置

相关 Maps：
- `docs/maps/30-after-close.md` §11：增量检查点与分层发布
- `docs/maps/40-market-stock-experience.md`：个股详情布局

相关 Rules：
- `rules/80-deployment-data-safety.md` §分层发布与增量检查点纪律

相关提交：
- 基线：b3b543e（dev = origin/dev）
- 本轮 commit：待 push 后由 Git 历史关联（禁止第二个"回填SHA"提交）

## 1. 摘要

本轮在 CHANGE-006 脚手架基础上完成：ID 合同修复（chip FK + trade_date NOT NULL + is_stale 真源）、publication pointer 接入 stock_context 读取端、publish_market_aggregation source 验证、publish_history_cross_section DB coverage、个股详情自选按钮微缩到左栏活动行 + direct fallback。

**状态：PARTIAL** — Worker run item 真实接入、history 回补真实接入、market_stocks LATERAL pointer 接入、PG 集成测试均未完成。

## 2. 背景与问题

CHANGE-006 只是 service/schema 脚手架，存在以下 P0 问题：
1. **P0-1**：071 migration FK 指向 `scheduler_job_runs.id`，但 orchestrator 已传 `snapshot_run_id` → 生产写入 ForeignKeyViolation
2. **P0-3**：`factor_publications.trade_date` nullable + 普通唯一约束 → 多个 NULL "latest pointer"
3. **P0-5**：`is_stale_snapshot` 用 `StockFeatureSnapshot.max(trade_date)` 而非 `bars_daily.max(trade_date)`
4. **P0-2**：publication pointer 未被任何读取端使用
5. UI：顶部大号自选按钮占用空间

## 3. 变化内容

### 3.1 ID 合同修复

| 修改 | 文件 | 变更 |
|---|---|---|
| 071 FK | `alembic/versions/071_chip_consensus_snapshots.py` | `core_run_id` FK 从 `scheduler_job_runs.id` → `stock_feature_snapshot_runs.id` |
| 071 ORM | `app/models/stock_chip_consensus_snapshot.py` | `ForeignKey("stock_feature_snapshot_runs.id")` + 注释 |
| 073 trade_date | `alembic/versions/073_incremental_factor_publication.py` | `trade_date nullable=True` → `nullable=False` |
| 073 ORM | `app/models/factor_publication.py` | `trade_date: Mapped[date]` (not nullable) |

**证据**：071-073 从未进入持久环境（本地禁止 alembic upgrade；CI 临时 Postgres 容器 job 结束销毁；生产服务器未部署）。因此可修正未部署迁移，无需新增 074。

### 3.2 is_stale 真源修复

`factor_publication_service.is_stale_snapshot` 改为 `SELECT MAX(bars_daily.trade_date)`，不再使用 `StockFeatureSnapshot.max(trade_date)`。与 `market_stocks_service._build_max_trade_date_subquery` 口径一致。

### 3.3 publish_market_aggregation source 验证

新增严格校验：`source_core_run_id` 必须等于该日期已发布的 `stock_core` pointer.data_run_id。不匹配抛 `ValueError`。

### 3.4 publish_history_cross_section DB coverage

新增 `compute_history_coverage(history_run_id)` 从 `FirstPyramidHistoryRun.succeeded_count / expected_count` 计算。`publish_history_cross_section` 不再接受调用方任意传 coverage，以 DB 统计为准。

### 3.5 publication pointer 接入 stock_context 读取端

`stock_context.py` 的 `_find_latest_succeeded_run` 和 `_find_run_by_trade_date` 优先读 `factor_publications` pointer（stock_core kind），无 pointer 时回退到 `published_at IS NOT NULL`。

### 3.6 个股详情自选按钮微缩

- 删除顶部 `.actions` 中的大号"加入/移出自选"按钮
- 新增 `WatchlistToggleButton` 组件（22×22px, 圆角4px, +品牌青绿色/−弱红色）
- 左栏来源列表活动行（`s.symbol === symbol`）显示紧凑按钮
- direct 访问、来源失效、当前股票不在 sourceStocks 时，顶部股票名称旁显示 fallback 按钮
- capture 模式全部隐藏
- `onClick` 使用 `stopPropagation` 避免触发行切股
- `type=button`、`title`、`aria-label`、`aria-pressed`、`aria-busy`、`disabled` 完整

## 4. 变化后

- chip.core_run_id FK 正确指向 `stock_feature_snapshot_runs.id`，与 orchestrator 传入值匹配
- `factor_publications.trade_date NOT NULL`，唯一约束不再允许多 NULL
- `is_stale` 真源为 bars_daily
- `stock_context` API 优先读 publication pointer，无 pointer 时兼容回退
- 个股详情左栏活动行有紧凑 +/- 按钮，direct 访问有 fallback

## 5. 影响范围

### 数据库
- 071 migration FK 修正（未部署，无数据影响）
- 073 migration trade_date NOT NULL（未部署，无数据影响）

### API
- `stock_context` `_find_latest_succeeded_run` / `_find_run_by_trade_date` 优先读 pointer

### 前端
- `StockDetailPage.tsx`：删除顶部大号按钮，新增 WatchlistToggleButton + 左栏活动行 + direct fallback
- `global.scss`：新增 `.tv-source-name-row` 和 `.tv-watchlist-toggle-mini` 样式

## 6. 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| 纯单元测试 | 27 项 | PASS | `PURE_UNIT_TEST=1 pytest tests/test_incremental_publication.py`（27 passed, 6 skipped） |
| Ruff --fix + check | 6 个修改文件 | PASS | `ruff check`（0 remaining，2 次累计） |
| TSC | 全项目 | PASS | `npx tsc --noEmit`（exit 0，无输出） |
| ESLint | StockDetailPage.tsx | PASS | `npx eslint`（exit 0，无输出） |
| PG 集成测试 | 6 项 | SKIP（待 CI） | `PURE_UNIT_TEST=1` 时跳过 |
| 本地服务健康 | Backend/Capture/Frontend | PASS | curl /health 200/200/200 |
| 浏览器验证 | 个股详情页 | BLOCKED | AUTH_WALL_BLOCKED（登录墙拦截，禁止登录 Owner 账户/创建测试用户，与历史会话一致） |

## 7. 迁移与兼容

- 071/073 migration 未部署，修正 FK 和 NOT NULL 无数据影响
- stock_context 优先读 pointer，无 pointer 时兼容回退到 `published_at`
- 前端删除顶部按钮，不影响现有 API 调用

## 8. 遗留问题与风险

1. **Worker run item 未真实接入**：`after_close_orchestrator` 仍调 `compute_review_core_batch_for_trade_date` 批量计算，未调 `claim_items`/`mark_item_succeeded`/`publish_stock_core`
2. **market_stocks LATERAL 未接入 pointer**：`_build_snap_lateral()` 仍按 instrument 取最新 snapshot，未过滤 published run（性能关键查询，需单独 PR）
3. **历史回补未接入 run/item**：`backfill_first_pyramid_history_batch` 仍返回 dict 无持久化
4. **历史回补 CLI 未实现**
5. **管理状态 API 未实现**
6. **PG 集成测试 6 项待 CI**
7. **事件 outbox 未实现**

## 9. 六个门禁（全部未通过）

1. ❌ main 合并：禁止 merge/push main
2. ❌ 服务器部署：禁止部署
3. ❌ migration：未在隔离 PG 验证 upgrade/downgrade/upgrade
4. ❌ core canary：未执行
5. ❌ history canary：未执行
6. ❌ 全量回补：禁止生产回补
7. ❌ chip 回补：未执行

**结论**：任一真实 PG 测试、Worker 接入、pointer 读链（market_stocks 未接入）或 history 安全入口未通过，**不可部署、不可回补**。
