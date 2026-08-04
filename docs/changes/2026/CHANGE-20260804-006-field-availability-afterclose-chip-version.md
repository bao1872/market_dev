# CHANGE-20260804-006：字段级 availability 入盘后链 + chip 覆盖率算法版本隔离

- 日期：2026-08-04
- 类型：contract + bugfix + docs
- 领域：第一金字塔盘后持久化 / chip 覆盖率 / Review schema 描述 / PRD 一致性
- 关联 PRD：`docs/prd/40-market-stock-experience.md`（MX-63）、`docs/prd/70-review.md`（§11.1、RV-27-01）
- 关联 Maps：`docs/maps/20-quant-model.md`、`docs/maps/70-review.md`
- 前置：`CHANGE-20260804-005`（P0 收口）已推送 `fdd4df7`

## 1. 背景

`CHANGE-20260804-005` 修复 P0 后，独立复审确认 A/B/C 主体通过，但遗留两个小的代码合同缺口
与两处文档/schema 矛盾，需要一个小收口提交（非重做）。

## 2. 修改内容

### 缺口 1：fieldAvailability 未进入盘后 stock_core 主链

- `FirstPyramidCoreSnapshot` 新增 `fieldAvailability`（dict，默认空）——盘后 stock_core/Review
  主链使用的 DTO，此前只有即时完整视图 `FirstPyramidSnapshot` 携带。
- `compute_first_pyramid_core_snapshot` 构建 `fieldAvailability`（复用 `_build_field_availability`），
  使盘后链的源携带字段级原因。
- 新增 `inject_field_availability_provenance(availability, source_run_id, calculated_at)`：
  盘后主链在 `compute_review_core_for_trade_date` 落库 summary_payload 前，为每个条目注入
  run 级 `sourceRunId`/`calculatedAt`（同一 run 全股票共享）。单股即时路径无 run 来源时保持 `None`
  （不伪造溯源）。

### 缺口 2：chip 覆盖率未按算法版本隔离

- chip 覆盖查询增加 `algorithm_version == CHIP_CONSENSUS_ALGORITHM_VERSION` 过滤。
- 统计改用 `COUNT(DISTINCT instrument_id)` 按 instrument 去重（chip 表唯一键含
  `algorithm_version`，同一 core run 可存在多版本记录）。
- `chip_coverage` 元数据新增 `algorithm_version`，记录实际采用的 chip 算法版本。
- 无降级判定收紧：`succeeded >= expected` 且 `failed==0` 且 `skipped==0` 且 `missing==0`。

### 文档/Schema 一致性

- `ReviewOverviewResponse.sourceChipRunId` / `ReviewRunResponse.source_chip_run_id` 描述修正：
  恒为 `null`（chip 无独立可追溯 snapshot run ID），chip 质量读取 `chipCoverage`/`degradedReasons`。
- PRD §70 发布门文字由 `trade_date >= run.trade_date` 修正为 `>`，与正确代码一致
  （防止后续开发改回）。

## 3. 修改前后关键差异

| 项 | 修改前 | 修改后 |
|---|---|---|
| 盘后链 fieldAvailability | 无（仅即时视图有） | `FirstPyramidCoreSnapshot.fieldAvailability` + 持久化注入溯源 |
| 字段级条目溯源 | 无 sourceRunId/calculatedAt | `inject_field_availability_provenance` 按 run 注入 |
| chip 覆盖查询 | 不过滤 algorithm_version、count(id) | 按版本过滤 + COUNT(DISTINCT instrument_id) |
| chip_coverage 元数据 | 无版本 | 记录 `algorithm_version` |
| 无降级判定 | coverage==1 | succeeded>=expected 且无 failed/skipped/missing |
| sourceChipRunId 描述 | "null 表示不可用" | 恒 null，质量读 chipCoverage |
| PRD 发布门 | `>=` | `>` |

## 4. 验证

- `data_closed=false`：未连库、未部署、Migration 083 未 apply。
- 后端 PURE_UNIT：FP stock_core + canonical + review dependency + publication safety
  162 passed；feature_snapshot_service 18 passed / 11 skip；新增
  `TestFieldAvailabilityAfterCloseChain`（core 携带 fieldAvailability、注入溯源、无 run 保持 None）
  与 chip 算法版本隔离/去重/ready 收紧 4 测试。
- Ruff passed；Mypy 仅既有 metric_engine/market_review/snapshot_run_item 预存错误（new_errors=0）。
- 前端无改动（本提交不触碰前端）。

## 5. 影响范围

- 后端：`schemas/first_pyramid.py`、`schemas/review.py`、`services/first_pyramid_service.py`、
  `services/feature_snapshot_service.py`、`services/review_orchestrator_service.py`。
- 文档：PRD MX-63 / §70、Map 20-quant-model / 70-review。
- 契约：`FirstPyramidCoreSnapshot.fieldAvailability`；`chip_coverage.algorithm_version`；
  `sourceChipRunId` 描述语义修正（非结构变更，值域不变）。
