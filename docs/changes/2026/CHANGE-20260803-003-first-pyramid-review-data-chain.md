# CHANGE-20260803-003: 第一金字塔 stock_core 数据链 + Review 就绪状态调查与修复

- 日期：2026-08-03
- 类型：behavior+contract+bugfix
- 领域：量化模型 / 复盘模块 / 行情体验
- 关联 PRD：`docs/prd/20-quant-model.md`（QM-01~QM-43）、`docs/prd/70-review.md`（RV-25-01）
- 关联 Maps：`docs/maps/20-quant-model.md`、`docs/maps/70-review.md`
- 数据操作：**零写入**（只读查询现有共享开发业务数据库；未部署、未重算 stock_core、未运行 chip/Review/Auction）

## 1. 为什么改

独立审查发现部分第一金字塔 `fp_*` 字段在数据库最新 `stock_core` run 中为 null，需定位空值发生的实际层，并修复仍存在于当前代码的合同遗漏。

## 2. 只读数据证据（2026-07-31 run, run_id=e616b2d4-b12e-4aa9-b45a-d174c9ce06fd, algo=1.0.0-core-split, coverage=1.0, 5293 行）

| 字段 | 非空数 | 空值率 | 说明 |
|---|---|---|---|
| `first_pyramid.statusText`（顶层） | 0 | 100% | 聚合状态文本全空 |
| `first_pyramid_flat.fp_summary` | 0 | 100% | 同源 statusText |
| `first_pyramid_flat.fp_run_id` | 0 | 100% | 尽管 `stock_feature_snapshots.source_run_id` 列有值（5293） |
| `first_pyramid_flat.fp_calculated_at` | 0 | 100% | 写入时未覆盖 created_at |
| `first_pyramid_flat.fp_chip_available` | 0 | 100% | 应为 boolean，但存储为 null |
| `first_pyramid.trend.continuousFactors.segment_change_pct` | 0 | 100% | 趋势段涨跌全空 |
| `first_pyramid_flat.fp_momentum_volume_relation` | 433 | ~92% | 少数有值 |
| `first_pyramid_flat.fp_squeeze_avg_volume` | 2334 | ~56% | 约 44% 有值 |
| `first_pyramid.trend.statusText` | 5184 | ~2% | 趋势维度状态有值 |
| `first_pyramid.momentum/structure.statusText` | 5184 | ~2% | 有值 |

## 3. 根因判定

**`first_pyramid_flat.fp_run_id` 全 null 但 `source_run_id` 列有值**：说明该 run 由**旧版本代码**写入（旧代码未在写入时覆盖 `fp_run_id`）。当前 `feature_snapshot_service.build_summary_payload` 已传 `source_run_id` 覆盖 `fp_run_id`。**当前代码已修复 fp_run_id 覆盖**。

**`fp_segment_change_pct` 等非 chip 字段全 null**：当前代码 `compute_first_pyramid_core_snapshot` + `_build_trend_dimension` + `flatten_first_pyramid` 经 250 synthetic bars 验证**能正确产出**（fp_summary / fp_segment_change_pct / fp_run_id / fp_calculated_at 均非空）。这些 null 是**旧 run 产物**，非当前代码计算层 bug。

**仍存在的当前代码 bug（本轮已修复）**：`build_summary_payload` 组装 `first_pyramid_flat` 时只传了 `trade_date`/`source_run_id`，**未传 `created_at`**，导致 `fp_calculated_at` 持久化为 null。修复：写入时用 `datetime.now(UTC).isoformat()` 回填 `created_at`。

**chip 字段（fp_chip_available/fp_poc_price 等）**：在 review-core 路径 `compute_review_core_for_trade_date` 强制 `chipConsensus=None`（`feature_snapshot_service.py`），chip 由异步 `after_close_chip_consensus` job 独立计算进 `stock_chip_consensus_snapshots` 表。当前代码 `assemble_first_pyramid_read_model` 在无 chip 时设 `fp_chip_available=False`（明确 boolean 非 null），且 chip 字段不影响非 chip 字段。**G 类（chip 任务未完成）**，数据补齐属后续数据操作，本轮 `data_closed=false`。

**动量条件字段（fp_momentum_volume_relation / fp_squeeze_avg_volume）**：无活跃 squeeze 区间时为合法 None（`_build_momentum_dimension` 仅在有 squeeze-on 区间时计算 `squeeze_period_volume_mean` / `vol_divergence`），非字段名错误。禁止用 0 伪装未知。

## 4. 字段分类（A-G 映射）

- **B 类（聚合/写入层遗漏，已修复）**：`fp_calculated_at`（未传 created_at）。
- **F 类（合法空）**：`fp_momentum_volume_relation` / `fp_squeeze_avg_volume`（无 squeeze 区间）。
- **G 类（chip 任务未完成）**：`fp_chip_available`/`fp_poc_price` 等 10 个 chip 字段（当前代码在无 chip 时设 `fp_chip_available=False` 并保留非 chip 字段）。
- **A/E 类（计算/flatten 正确，旧数据产物）**：`fp_run_id`/`fp_summary`/`fp_segment_change_pct`（当前代码已正确产出；DB null 为旧 run）。

**重要**：非 chip 字段（fp_run_id/fp_summary/fp_segment_change_pct）全 null 的根因是**旧 run 写入代码**，**不是 chip 缺失**。G 类 chip 问题不能解释全部非 chip 字段为空。

## 5. 本次修改

### 5.1 生产代码

`backend/app/services/feature_snapshot_service.py` `build_summary_payload`：组装 `first_pyramid_flat` 时新增 `created_at=datetime.now(UTC).isoformat()`，保证 `fp_calculated_at` 持久化非 null（与 `fp_run_id`/`fp_trade_date` 同源覆盖）。

### 5.2 测试（新增）

- `backend/tests/test_first_pyramid_stock_core_contract.py`（13 项）：250 bars 完整 fixture 生成非 chip 结果 / fp_segment_change_pct 精确映射 / fp_summary 生成 / fp_run_id+calculated_at 存在 / chip 缺失时 fp_chip_available=false（非 null）且不影响趋势结构动量 / chip 存在时正确合并 / flat 键集与 Schema 声明的 99 键一致（新增 Schema 字段时自动失败）/ build_summary_payload 不丢字段（含 created_at 修复断言）。
- `backend/tests/test_review_readiness_contract.py`（8 项）：59 条历史 raw_ready=true normalized_ready=false / 60 条历史 normalized_ready=true（无 off-by-one）/ 历史不足时 rawValue 保留 / 缺单一指标只影响该指标 / P 缺日收益语义不可用 / all-null 不产生有效 ready / coverage 基于 ready_count。

### 5.3 未改动

- 未修改前端（后端/API 已有值，前端映射无需变更，本轮只读核对）。
- 未修改 Migration / 部署文件 / canary / 权限模型 V2 代码。
- 未运行 stock_core 重算 / chip / Review bootstrap / Review run / publish / pointer 切换。

## 6. 验证

| 项 | 结果 |
|---|---|
| 第一金字塔目标测试（contract/flatten/summary/semantic/stock_core contract） | 全部 passed |
| Review 目标测试（readiness/member_fact/cold_start/attribution） | 全部 passed |
| 第一金字塔 + Review 定向集合计 | 220 passed |
| Ruff | All checks passed |
| Mypy（修改生产文件） | 修改文件无新增错误（`snapshot_run_item_service.py` 4 个 pre-existing 错误与本次无关） |
| check_architecture | 0 violations / 11 passed |
| check_docs_consistency | 全部通过 |
| check_governance_rules | PASS |
| git diff --check | 干净 |

## 7. 状态

- `first_pyramid_code_fixed=passed`（created_at 已修复；计算+flatten 链路经 synthetic bars 验证正确）
- `review_code_fixed=passed`（raw/normalized 分离、60 条门槛、发布门禁、all-null 保护已存在并经测试验证）
- `frontend_mapping_fixed=not_required`（后端/API 已有值，前端无需改）
- `stock_core_recomputed=pending_authorization`（未重算；DB 旧 run null 是旧数据）
- `chip_recomputed=pending_authorization`
- `review_bootstrap=pending_authorization`
- `review_run_published=pending_authorization`
- `data_closed=false`
- `deployment=pending_authorization`

**诚实边界**：DB 中 2026-07-31 run 的 `fp_run_id/fp_summary/fp_segment_change_pct` 全 null 是旧代码写入产物；当前代码已正确产出这些字段，但**数据重算未授权**，历史 run 的 null 不因本轮代码修复而自动消失。需用户授权 stock_core 重算 + chip 补齐 + Review bootstrap 后才能验证真实数据闭环。
