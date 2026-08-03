# CHANGE-20260803-004: 盘迹 PRD V1.0 代码收口 — 盘后状态机 / FS 批处理 / 第一金字塔合同 / SMC 语义 / Review 编排 / 行情导航 / 管理后台流水线

- 日期：2026-08-03
- 类型：behavior+contract+architecture
- 领域：盘后编排 / 量化模型 / 行情体验 / 复盘模块 / 管理后台
- 关联 PRD：`docs/prd/20-quant-model.md`、`docs/prd/30-after-close.md`、`docs/prd/40-market-stock-experience.md`、`docs/prd/70-review.md`
- 关联 Maps：`docs/maps/20-quant-model.md`、`docs/maps/30-after-close.md`、`docs/maps/40-market-stock-experience.md`
- 关联 Changes：CHANGE-20260803-003（第一金字塔数据链修复，本轮在此基础上补齐合同与测试）
- 数据操作：**零写入**（未部署、未运行编排、未修改共享开发业务数据库）

## 1. 为什么改

盘迹 PRD V1.0（`ref/instruction.md`）定义了 7 个核心模块。代码实现已覆盖全部模块，但存在 3 个收口缺口：

1. 盘后编排 step 执行器的纯单元测试被误分类为 postgres，在 PURE_UNIT 模式下 skip；
2. `after_close_orchestrator.py` 的 chip job 创建点 `snapshot_run_id` 类型为 `UUID | None`，未做 None 守卫（mypy arg-type 错误）；
3. 新增前端合同测试文件未被 git track。

此外，7 个 PRD 模块的代码修改和文档更新需要按垂直切片组织提交。

## 2. 修改范围（7 个 PRD 模块）

### 2.1 盘后状态机
- `execute_orchestrator_step` 提取为可独立调用的 step executor（超时/不可用/可选语义）
- cancel / reconcile / restart API 端点
- `snapshot_run_id` None 守卫：chip job 创建前检查，None 时跳过并 warning

### 2.2 Feature Snapshot 批处理
- MDAS batch reads 替换逐条查询
- 进度回调与失败阈值

### 2.3 第一金字塔数据合同
- Schema 新增 `first_pyramid.py` 字段
- `first_pyramid_flatten.py` / `first_pyramid_semantic_adapter.py` / `first_pyramid_service.py` 对齐 PRD 合同

### 2.4 SMC 方向与级别语义
- `smcLabels.ts` / `smcRendering.ts` 使用 PRD 定义的 direction + level 语义
- `StrategyChart.tsx` 集成
- 新增 `firstPyramidSmcContract.test.ts` 合同测试

### 2.5 Review 编排
- Review 闭环字段写入盘后 metadata
- `review_run_id` / `review_status` / `review_coverage` / `review_blockers`

### 2.6 行情导航
- `stockDetailNavigation.ts` 导航逻辑
- `firstPyramidViewModel.ts` 视图模型

### 2.7 管理后台流水线
- `AdminAfterClosePipelinePage.tsx` 四操作 UI（cancel / reconcile / restart / retry）
- `adminAfterClosePipelineHelpers.ts` + 测试
- `endpoints.ts` / `useApi.ts` API 层

## 3. 缺口修复

| 缺口 | 修复 | 文件 |
|---|---|---|
| A. 纯单元测试误分类为 postgres | 将 2 个 step executor 测试移至独立 `test_orchestrator_step_pure_unit.py` | 新增 `backend/tests/test_orchestrator_step_pure_unit.py`，修改 `backend/tests/test_after_close_worker.py` |
| B. `snapshot_run_id` None 未守卫 | chip job 创建前加 `if snapshot_run_id is None:` 跳过并 warning | `backend/app/services/after_close_orchestrator.py` |
| C. 新增前端测试文件未 track | 提交时 `git add` | `frontend/src/features/stock-research/__tests__/firstPyramidSmcContract.test.ts` |

## 4. 验证结果

- 前端 `tsc --noEmit`：0 errors
- 前端 `vite build`：通过
- 后端 `ruff check`：All checks passed
- `check_architecture`：0 violations
- `check_governance_rules`：PASS
- `check_docs_consistency`：全部通过
- PURE_UNIT_TEST：`test_orchestrator_step_pure_unit.py` 3 passed（不再 skip）

## 5. 不在范围

- 24 个 pre-existing mypy 错误（非本次引入）
- 数据库 migration
- 部署、push main、共享开发业务数据库修改
- 前端 contract 测试运行（需 Node 22+，当前 Node 20 不支持 `--experimental-strip-types`）
