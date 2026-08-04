# CHANGE-20260804-005：Review 发布门与 chip 覆盖率 P0 + 第一金字塔合同缺口收口

- 日期：2026-08-04
- 类型：behavior + contract + bugfix
- 领域：复盘发布门禁 / chip 依赖矩阵 / 第一金字塔字段级 availability / FP 失败完整性
- 关联 PRD：`docs/prd/70-review.md`（§11.1、RV-27-01）、`docs/prd/40-market-stock-experience.md`（MX-63）
- 关联 Maps：`docs/maps/70-review.md`、`docs/maps/20-quant-model.md`

## 1. 背景

独立审查判定第一金字塔与 Review 存在两个直接阻止真实闭环的 P0 与两个合同缺口。
本轮针对这些判定做代码收口（`data_closed=false`，不连库、不部署、不 apply Migration）。

## 2. 修改内容

### P0-1：Review 发布门误拦正常发布

- 门禁 #7 原检查 `trade_date >= run.trade_date`，会拦截当前 run 自身当日观测
  （`== trade_date`），导致正常 Review 无法发布（确定性逻辑错误）。
- 修正为只拦截**严格未来观测** `trade_date > run.trade_date`（乱序/历史基线污染）。
- 历史基线读取（`load_metric_history`）本就使用 `trade_date < 当日` 过滤，合法 run
  只有当日观测，必然通过本门。

### P0-2：chip 依赖矩阵未计算缺失股票

- `_resolve_chip_dependency` 原只统计 chip 表已有行，把"已有行全 succeeded"误判为
  100% 覆盖，漏掉缺失股票；且把 `source_chip_run_id` 赋为 core run id 冒充 chip run。
- 修正为以 stock_core run 的 `expected_count` 为分母，统计
  `succeeded/failed/skipped/missing` 与真实 `coverage`，判定
  `CHIP_UNAVAILABLE / CHIP_PARTIAL / 无降级`。
- `source_chip_run_id` 恒为 `NULL`（chip 无独立 run 记录），覆盖率写入
  `run.metadata_json["chip_coverage"]`，并经 overview/run API 暴露给前端展示真实覆盖率。

### 合同缺口 1：字段级 availability

- `FirstPyramidSnapshot` 新增 `fieldAvailability`，`FieldAvailability` 合法 reasonCode 六类：
  `not_applicable / insufficient_history / upstream_unavailable / failed / stale / missing`。
- `_build_field_availability` 覆盖高空值字段 `momentum.squeeze_avg_volume`、
  `momentum.volume_relation`、`momentum.sqzmom_value`，维度可用但无挤压标 `not_applicable`，
  上游缺失标 `upstream_unavailable` / `missing`，禁止无原因的空 `null`。

### 合同缺口 2：FP 失败完整性

- 第一金字塔计算异常不再以无原因的 `first_pyramid=None` 冒充成功：
  - `compute_review_core_for_trade_date` 标记 `first_pyramid_status=FP_COMPUTE_FAILED`
    + degraded reason；
  - batch 路径：FP 失败股票计入 `failed_count` 且不 upsert、不进 snapshot_count；
  - run-items 路径：FP 失败股票标记 item failed。
- 使 stock_core coverage 不再因"任务成功但 FP 大量为空"而虚高。

### 历史 OB 事件统一归一

- `flatten_first_pyramid` 中历史 OB 事件改经 `adapt_legacy_pyramid_event` 归一
  （`up/down → bullish/bearish`），与 BOS/CHoCH 一致。

## 3. 修改前后关键差异

| 项 | 修改前 | 修改后 |
|---|---|---|
| 发布门 #7 | `>= trade_date` 拦当前 run 当日观测 | `> trade_date` 只拦严格未来观测 |
| chip 覆盖率 | 已有行比例（漏缺失股票） | 以 expected_count 为分母的真实覆盖率 |
| `source_chip_run_id` | 写成 core run id | 恒为 `NULL`（chip 无独立 run） |
| 可空因子原因 | 维度级 only，因子仅 None | 字段级 `fieldAvailability`（6 类 reasonCode） |
| FP 计算失败 | 静默 None + 标 succeeded | `FP_COMPUTE_FAILED` + 排除出 publish-ready coverage |
| 历史 OB 事件 | 直接读原始 direction | 经兼容 adapter 归一 |

## 4. 验证

- `data_closed=false`：未连接数据库、未部署、Migration 083 未 apply、无业务写入。
- 后端 PURE_UNIT：
  - review 依赖矩阵 + 发布安全 + 冷启动 + metric observation bootstrap：259 passed；
  - 另加 feature_snapshot_service 全套：279 passed / 11 skipped；
  - 新增真实组合测试（compute_scope_metrics → persist → evaluate_publish_gate 合法通过）、
    59/60 真实 metric engine 边界、双股票共享 run 时间、chip 覆盖率六态、字段级
    availability 六 reasonCode、FP 失败不计成功。
- Ruff passed；Mypy 仅既有 metric_engine/market_review/snapshot_run_item 预存错误
  （非本轮引入，new_errors=0）。
- 前端：TSC 0 errors、ESLint 通过、`npm run build` 通过。
- 遗留（外部授权边界）：Migration 083 apply、共享 bz_stock 定向 PG 测试、
  精确 SHA 部署、真实 stock_core/chip/review 运行、浏览器端到端验收。

## 5. 影响范围

- 后端：`review_publication_service.py`、`review_orchestrator_service.py`、
  `feature_snapshot_service.py`、`first_pyramid_service.py`、`first_pyramid_flatten.py`、
  `schemas/first_pyramid.py`、`schemas/review.py`、`api/review.py`、`api/admin_review.py`。
- 前端：`features/review/types.ts`、`features/review/ReviewHeader.tsx`（展示真实 chip 覆盖率）。
- 契约：ReviewOverview/ReviewRun 响应新增 `chipCoverage`；FirstPyramidSnapshot 新增
  `fieldAvailability`。
