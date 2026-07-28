# CHANGE-20260728-008：统一盘后编排 + 列表视图 99 字段 + 永久删除持久测试库

状态：进行中（代码+目标测试通过，待 commit + push dev + CI + merge main + 部署 + 生产运行）
日期：2026-07-28
类型：architecture + feature + security
领域：盘后编排 / 行情体验 / 测试基础设施

相关 PRD：

- `../../prd/30-after-close.md`：AC-16（统一盘后编排）
- `../../prd/40-market-stock-experience.md`：MX-20（列表视图第一金字塔全量字段）
- `../../prd/80-system-runtime.md`：SR-03（共享 PostgreSQL）

相关 Maps：

- `../../maps/30-after-close.md`：盘后编排入口与状态链
- `../../maps/40-market-stock-experience.md`：行情列表字段与列注册
- `../../maps/80-system-runtime.md`：SR-03a 持久测试库已删除

相关 Rules：

- `../../../AGENTS.md`：§8 基础安全边界（持久测试库禁用）
- `../../../rules/40-testing-quality.md`：持久测试数据库禁用
- `../../../rules/20-market-data-indicators.md`：盘后编排统一入口

相关提交或 PR：

- 基线：6c9c55b（dev = origin/dev = origin/main）
- 本轮 commit：待填写

## 1. 摘要

本轮在 dev 分支完成三项紧密关联的架构变化：

1. **统一盘后编排**：删除 `dsa_only` 独立端点、前端按钮、hook、特殊 mode 分支与覆盖率常量；"从 DSA 阶段重算"并入现有 `force` 端点 + `restart_from="daily_ready"` 参数，仍是同一 `after_close` 任务，不创建 `dsa_only` 类型，不跳过后续特征/快照/发布。系统只允许 `job_name=after_close_orchestrator`、`run_type=full`。
2. **列表视图第一金字塔全量字段**：新增后端唯一扁平化函数 `first_pyramid_flatten.flatten_first_pyramid` 和前端唯一 ColumnRegistry `firstPyramidColumns.tsx`，覆盖 99 个 `fp_` 键（快照 7 / 趋势 18 / 结构 8 / 结构事件 21 / 动量 13 / 动量事件 9 / 筹码 10 / 量能 13）；列表 API 批量读取快照，无 N+1；复用 `TableViewPreset` 保存显隐与顺序。
3. **永久删除持久测试库**：DROP DATABASE `bz_stock_test`；更新 conftest.py 强制非 CI 环境必须 `PURE_UNIT_TEST=1`；CI 通过 `GITHUB_ACTIONS=true` 或 `PANJI_CI_DB_TEST=1` 识别；AGENTS.md §8 与 rules/40-testing-quality.md 增加硬边界。

## 2. 背景与问题

- **dsa_only 双路径**：`/admin/after-close-runs/dsa-only` 独立端点导致盘后存在两种任务类型，状态链与覆盖率检查分裂；`createDsaOnlyRun`/`useDsaOnlyRun` 前端 hook 与按钮重复入口。
- **列表视图缺失第一金字塔**：行情列表只显示基础列，不显示第一金字塔 99 字段；用户需进入个股详情才能看到趋势/结构/动量/筹码/量能维度。
- **持久测试库违反安全边界**：`bz_stock_test` 持久存在于本地 Postgres，conftest.py 在非 PURE_UNIT_TEST 时可能连接持久测试库，违反"本地测试只能纯单元/mock"原则。

## 3. 修改前行为

- 盘后存在 `after_close`（full）和 `dsa_only` 两种任务类型；`/admin/after-close-runs/dsa-only` 独立端点；前端有 `createDsaOnlyRun`/`useDsaOnlyRun` hook 与按钮。
- 行情列表 API 不返回 `first_pyramid` 字段；前端无 99 字段列注册。
- `bz_stock_test` 持久数据库存在于本地 Postgres（12 MB）；conftest.py 仅要求 `APP_ENV=test` + `TEST_DATABASE_URL`，不区分 CI 与本地。

## 4. 修改后行为

- 盘后只允许 `after_close_orchestrator` + `run_type=full`；`force?restart_from=daily_ready` 从 DSA 阶段重算，仍执行完整后续链路（特征/快照/发布）；必须先验证日线覆盖率 ≥ 90%。
- 行情列表 API 返回 `first_pyramid` 字段（99 个 `fp_` 键）；前端 ColumnRegistry 提供 99 列，分组可显隐、拖拽排序；复用 `TableViewPreset` 保存。
- `bz_stock_test` 已 DROP；conftest.py 强制非 CI 环境 `PURE_UNIT_TEST=1`；CI 通过 `GITHUB_ACTIONS=true` 识别。

## 5. 影响模块

- 盘后编排：`admin_after_close.py`、`after_close_orchestrator.py`、前端 `AfterClosePipelineCard.tsx`、`endpoints.ts`、`useApi.ts`
- 行情列表：`market_stocks_service.py`、`market_stocks.py`、`first_pyramid_flatten.py`、`firstPyramidColumns.tsx`、`MarketWorkspacePage.tsx`、`StrategyDataTable.tsx`
- 测试基础设施：`conftest.py`、AGENTS.md §8、`rules/40-testing-quality.md`

## 6. 修改文件

后端：
- `backend/app/api/admin_after_close.py`（删除 dsa-only 端点，新增 `restart_from` 参数）
- `backend/app/services/after_close_orchestrator.py`（清除 dsa_only 分支，支持 `restart_from="daily_ready"`）
- `backend/app/services/bars_coverage_service.py`（覆盖率检查适配）
- `backend/app/services/market_stocks_service.py`（集成 `flatten_first_pyramid`，批量读取快照）
- `backend/app/schemas/market_stocks.py`（`MarketStockRow` 新增 `first_pyramid` 字段）
- `backend/app/services/first_pyramid_flatten.py`（新增，99 键扁平化函数）
- `backend/tests/conftest.py`（CI 守卫，非 CI 必须 `PURE_UNIT_TEST=1`）
- `backend/tests/test_after_close_orchestrator.py`（适配 dsa_only 删除）
- `backend/tests/test_after_close_endpoints.py`（适配 dsa_only 删除）
- `backend/tests/test_after_close_board_sync.py`（适配 dsa_only 删除）
- `backend/tests/test_dsa_only_coverage_endpoint.py`（删除）
- `backend/tests/test_first_pyramid_flatten.py`（新增，31 个纯单元测试）

前端：
- `frontend/src/api/endpoints.ts`（`MarketStockRow` 新增 `first_pyramid`；删除 `createDsaOnlyRun`/`useDsaOnlyRun`）
- `frontend/src/hooks/useApi.ts`（新增 `useMarketStocks`；删除 `useDsaOnlyRun`）
- `frontend/src/components/StrategyDataTable.tsx`（新增 `defaultHiddenColumns` prop）
- `frontend/src/features/market-workspace/MarketWorkspacePage.tsx`（集成 99 列 + `useMarketStocks`）
- `frontend/src/features/market-workspace/firstPyramidColumns.tsx`（新增，ColumnRegistry）
- `frontend/src/features/after-close-pipeline/AfterClosePipelineCard.tsx`（删除 dsa-only 按钮）

文档与规则：
- `AGENTS.md`（§8 持久测试库禁用硬边界）
- `rules/20-market-data-indicators.md`（`mode=dsa_only` → `restart_from="daily_ready"`）
- `rules/40-testing-quality.md`（持久测试数据库禁用章节）
- `docs/prd/30-after-close.md`（AC-16 统一盘后编排）
- `docs/prd/40-market-stock-experience.md`（MX-20 列表视图 99 字段）
- `docs/maps/30-after-close.md`（删除 dsa_only 引用，更新入口与验证）
- `docs/maps/80-system-runtime.md`（SR-03a 持久测试库已删除）
- `docs/runbooks/local-development.md`（更新测试规则）
- `docs/runbooks/after-close-production-run.md`（新增，完整盘后生产运行 runbook）

## 7. 测试证据

后端纯单元测试（`PURE_UNIT_TEST=1`）：
- `tests/test_first_pyramid_flatten.py`：31 passed（99 键完整性 + 扁平化逻辑 + 边界情况 + 结构对齐）
- Ruff：修改文件 All checks passed
- Mypy：修改文件 Success: no issues found

前端：
- TSC `--noEmit`：无错误
- ESLint 修改文件：无错误

`git diff --check`：无空白错误

CI 集成测试（待 push 后 CI 运行）：
- `test_after_close_orchestrator.py`、`test_after_close_endpoints.py`、`test_after_close_board_sync.py` 在 CI 临时库运行

## 8. 数据库迁移

无 schema migration。

数据变更：
- DROP DATABASE `bz_stock_test`（本地 Postgres 15432）

## 9. 配置变化

- `backend/tests/conftest.py` 新增 CI 环境守卫（`GITHUB_ACTIONS=true` 或 `PANJI_CI_DB_TEST=1`）
- 本地 Mac 运行测试必须 `PURE_UNIT_TEST=1`
- CI 工作流不变（`POSTGRES_DB: bz_stock_test` 作为容器内临时数据库名，job 结束销毁）

## 10. 风险

- **旧 dsa_only 记录**：生产环境可能存在历史 `dsa_only` queued/running 记录；只读识别，通过正式 cancel/interrupted/retry 服务处理，禁止 DELETE 或直接改 metadata。
- **CI 守卫误判**：`GITHUB_ACTIONS=true` 仅在 GitHub Actions 设置；其他 CI 系统需显式 `PANJI_CI_DB_TEST=1`。
- **99 列默认隐藏**：用户首次看到的基础列与之前一致；新增 20 个核心金字塔列默认可见，其余默认隐藏；列设置中全量存在。

## 11. 遗留问题

- 生产环境完整盘后任务运行验证待部署后执行。
- 浏览器真实链路验收（/market 99 列显示、列设置保存）待部署后执行。
- 历史CHANGE文档中 dsa_only 引用保留为历史记录，不反写。
