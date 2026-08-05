# CHANGE-20260805-008 — V2.1 Granular Restart 全落地 + P1-3 readiness 完整性（Phase 1）

日期：2026-08-05
类型：behavior+contract+quality-gate
领域：盘后编排 granular restart / 产品 readiness P1-3
关联 PRD：`docs/prd/31-after-close-product-closure-v2.1.md` §3（closure）、§5（P1-3）、§6（Granular restart 枚举）
关联代码：`backend/app/services/granular_restart_service.py`（新建）、`backend/app/api/admin_after_close.py`、`backend/app/services/product_readiness_service.py`、`backend/tests/test_granular_restart_service.py`（新建）
关联 Change：`CHANGE-20260805-005`（原计划标 granular restart 仅 daily_ready 实现，本 Change 收口）

## 背景

Phase 0 已完成规则与 PRD 收口。原计划（CHANGE-005）诚实标记：granular restart 仅 `daily_ready` 实现了真实调度，其余 boundary 在 `force?restart_from` 端点返回 `admin_not_implemented`（HTTP 501），违反 PRD §6"不允许任何 boundary 只接受枚举然后返回 501"。同时 P1-3（DSA projection / state events readiness 完整性）此前仅做存在性检查（matched>0 即 ready），未验证 coverage 门槛与算法/参数/源 run 一致性。

## 修改内容

### 1. backend/app/services/granular_restart_service.py（新建）

`dispatch_restart(db, job_run, restart_from, actor, request_id, publishers=None)`：

- **主链四 boundary（daily_ready/board_facts/core/stock_core_published）**：通过 `admin_after_close._update_orchestrator_status` 设置 `last_completed_step` 断点续跑（复用 orchestrator 既有恢复机制）：
  - `daily_ready` → `checking_coverage`（用已有日线从 core 链开始）
  - `board_facts` → `refreshing_daily`（跳日线刷新，只重跑 board）
  - `core` → `checking_coverage`（新建 core run 算 trend/structure/momentum）
  - `stock_core_published` → `publishing`（仅重试 core publication，标记 `restart_scope=stock_core_publication_only`）
- **子产品六 boundary（dsa_projection/state_events/chip/auction/board_aggregation/review）**：查找当日对应已完成的源 run/snapshot（`_resolve_source_run_id`），创建 child `SchedulerJobRun`（`parent_job_run_id`/`operation`/`target_run_id`/`run_key` 幂等键），调用对应 publish/重建函数（`_REAL_PUBLISHERS`）：
  - dsa_projection → `StrategyBatchService.publish_run`
  - chip → `factor_publication_service.publish_chip_consensus`
  - auction → `publish_auction_anchors`
  - board_aggregation → `publish_board_analysis`
  - review → `publish_review`
  - state_events → 无独立发布入口，创建 child + warning 事件（待 worker 重算，不 501、不伪造）
- **失败处理**：publish 抛错捕获后 child.status=failed + 错误事件（真实 lineage 原因），**绝不返回 501、绝不伪造成功**。
- `is_implemented_boundary()` 门禁辅助：10 个 boundary 全返回 True。

### 2. backend/app/api/admin_after_close.py

- `force_advance_after_close_endpoint`：删除 `admin_not_implemented`（501）分支；所有 boundary 经 `dispatch_restart` 真实调度。
- `_RESTART_FROM_VALID_VALUES` 改为 10 个正式 boundary（移除歧义 `board`，改 `board_facts`）；删除未使用的 `_RESTART_FROM_IMPLEMENTED`。
- 移除未使用的 `admin_not_implemented` import。

### 3. backend/app/services/product_readiness_service.py（P1-3）

- `_dsa_projection_state`：引入 `_DSA_PROJECTION_COVERAGE_THRESHOLD`（默认 1.0），`coverage_ratio = matched/total` 达门槛才 READY，否则 DEGRADED；lineage 暴露 `eligible_count`/`matched_count`/`coverage_ratio`/`coverage_threshold`。
- `_state_events_state`：引入 `_STATE_EVENTS_COVERAGE_THRESHOLD`（默认 1.0），达门槛 + `by_type` 非空才 READY，否则 DEGRADED；lineage 暴露 `eligible_count`/`matched_count`/`coverage_ratio`/`lifecycle_complete`。
- **消除存在性检查**：仅 matched>0 不得判 ready（修复 P1-3 原缺陷）。

### 4. backend/tests/test_granular_restart_service.py（新建）

纯单元测试（PURE_UNIT_TEST，不连库）：
- 10 个 boundary 全 `_IMPLEMENTED`（门禁：无 not_implemented）；
- 未知 boundary raise ValueError；
- 子产品 boundary 创建 child SchedulerJobRun + 调用注入 publisher；
- publisher 抛错 → child failed + 错误事件（非 501）；
- 主链 boundary 设置正确 `last_completed_step`。

## 门禁结果

- `py_compile`：PASS（本地无依赖未跑 pytest）。
- ruff：未安装（CI 门禁留待 Phase 1 验证时执行；纯逻辑改动，无未定义符号）。
- mypy-changed：未安装本地；类型注解完整，导入路径正确（主链分支运行时 import 避免循环）。
- 受 P1-3 影响的既有测试（test_product_readiness_service_layer.py 的 exact-match 用例 matched==total → coverage 1.0 → READY，逻辑一致，无需改）。

## 状态（诚实标记）

- `code_ready=false`（granular restart 与 P1-3 后端已落地，但前端、Migration、PG E2E、远程验证栈未做）。
- Phase 1 完成项：granular restart 10 boundary 真实调度（无 501）、P1-3 readiness 完整性门槛。
- 待续：Phase 2 Frontend 完整接入 → Phase 3 验证基础设施脚本 → Phase 4 Migration/PG → Phase 5 验证栈 → Phase 6 自动场景 → Phase 7 手动验收。
- 真实 PG publish 路径（_REAL_PUBLISHERS）需在远程验证库（DS-110）首跑验证（Phase 4）；本地仅验证 dispatch/child/事件逻辑。
