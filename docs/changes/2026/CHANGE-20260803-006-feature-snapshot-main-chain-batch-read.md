# CHANGE-20260803-006：Feature Snapshot 主链 MDAS 批读收口（AC-16）

- 日期：2026-08-03
- 类型：behavior + contract + performance
- 领域：盘后编排 / 特征快照 / 行情数据
- 关联 PRD：`docs/prd/30-after-close.md`（AC-16）
- 关联 Maps：`docs/maps/30-after-close.md`

## 1. 背景与问题

AC-16 要求全市场 `feature_snapshot` 必须经 MDAS 批量入口预读 `symbols × bars × adj_factor`，
同一股票、周期、交易日的 canonical bars frame 与诊断 hash 必须在该批计算内复用，
并暴露 `batch_count` / `mdas_batch_read_count` 等低基数 metrics。

原实现中：
- `compute_for_trade_date`（非盘后主链）已实现 MDAS `get_bars_batch` 批读 + 批内集中 upsert；
- 但 **盘后编排实际调用的主链** `compute_review_core_with_run_items`（单股×阶段检查点）
  仍逐股调用 `compute_review_core_for_trade_date` → `_fetch_bars_from_db`，即 N×2 次
  DB 往返读取 1d bars，未走 MDAS 批读入口。

即"只优化了未被主链调用的 `compute_for_trade_date`"，主链性能缺口未闭合。

## 2. 修改内容（`backend/app/services/feature_snapshot_service.py`）

1. **`compute_review_core_with_run_items` 接入 MDAS 批读**：每个 claim 批内通过
   `MarketDataAggregationService.get_bars_batch` 一次预读 1d point-in-time qfq bars
   （`include_realtime=False / completed_only=True / end_date=trade_date /
   adjustment_as_of=trade_date`），将 canonical frame 与诊断 hash
   （`source_bar_hash`/`adj_factor_hash`）经 `primary_bars`/`primary_source_bar_hash`/
   `primary_adj_factor_hash` 传入单股计算。
   - 批读失败**降级为逐股读取**（不抛，保留原语义，不因批读故障阻断主链）。
   - 仍保持每股独立事务提交（AC-08 单股×阶段检查点不因批读改变）。
   - 返回新增 `batch_count` / `mdas_batch_read_count` 低基数 metrics。

2. **`compute_review_core_for_trade_date` 扩展可选参**：
   - 新增 `primary_source_bar_hash` / `primary_adj_factor_hash` 可选参数；
   - 原"`primary_bars` 传入时诊断 hash 恒为 None"的问题：批读路径现在可从
     `BarAggregationResult` 显式传入 hash，保证 canonical compute 的诊断合同不丢失；
   - 不传时（既有调用方）行为不变（`primary_bars=None` 触发内部 `_fetch_bars_from_db`）。

3. **新增模块级 `_get_mdas()` helper** 与 `BarAggregationResult` / `MarketDataAggregationService`
   模块级 import（无循环依赖）。

## 3. 验证证据（PURE_UNIT_TEST=1，未部署/未连接共享库）

- 新增测试 `test_review_core_with_run_items_uses_mdas_batch_read_and_metrics`：
  - `get_bars_batch` 每个 claim 批一次（3 个非空批 → await_count=3），而非逐股 N×2 次；
  - 预读 bars 通过 `primary_bars` 传入单股计算（canonical frame 批内复用）；
  - 返回 `batch_count=3` / `mdas_batch_read_count=3`；
  - 每股独立计算 + upsert（AC-08 检查点不因批读改变）。
- `tests/test_feature_snapshot_service.py` 全量：16 passed / 11 skipped。
- 盘后相关套件（`test_after_close_orchestrator.py` + `test_after_close_phase0_contracts.py`
  + `test_change_20260729_003.py`）：43 passed / 30 skipped。
- Ruff：改动文件 `All checks passed!`。

## 4. 未验证 / 未完成项（如实标注）

- **真实性能收益未基准**：纯单元测试只能验证 MDAS 批读次数（DB 往返从 N×2 降到 batch_count×1），
  实际耗时/CPU 收益需在共享开发库或生产环境对真实主链（7 小时）基准核验，不在本轮授权范围。
- `compute_feature_snapshot_for_date` 的批读诊断 hash 传播（`compute_for_trade_date` 路径）
  未在本轮改动（非主链，最小必要修改）。
- 未部署、未 push main、未连接共享业务库；`data_closed=false`。
