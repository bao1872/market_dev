# CHANGE-20260804-002：FS 批量列合同 P0 + Review 取消/中断终态短路 P0

- 日期：2026-08-04
- 类型：bugfix + behavior + contract
- 领域：特征快照批量行情 / 盘后编排终态
- 关联 PRD：`docs/prd/30-after-close.md`
- 关联 Maps：`docs/maps/30-after-close.md`
- 关联前置：CHANGE-20260804-001（三个切片缺口修复）、审查结论（dev_head=6c21349）

## 1. 背景与问题（来自审查结论）

审查把 `6c21349` 的 FS 批读判为 `feature_snapshot_batch_query=failed`、`batch_bars_dataframe_mapping=failed`、`batch_runtime_path=falls_back_to_per_stock`，并指出两个 P0：

### 1.1 FS-DB-01（P0）：批量 DataFrame 列定义错误
`bar_repository.get_daily_bars_batch` 的 SQL 选 9 列
`(instrument_id, trade_date, open, high, low, close, volume, amount, adj_factor)`，
但构造 DataFrame 用 `columns=["instrument_id"] + _BAR_COLUMNS`（仅 8 列，缺 `trade_date`）。

后果：pandas 把 row 第 2 个值（trade_date）误赋给 `open` 列 → **列错位**，且随后
`df["trade_date"]` 触发 `KeyError`。主链捕获整个 MDAS 批读异常后把 `primary_batch_results`
留空，回退逐股读取 → N+1 性能问题完整恢复。**"数据库级批读"事实上未闭环。**

旧测试只 mock `get_daily_bars_batch`/`get_adj_factor_series_batch` 返回已构造好的 DataFrame，
未验证：SQL Row → DataFrame 的列数/列名匹配、`trade_date` 索引、Decimal 转换、多股票分组、
空结果、as_of 过滤。→ 测试盲区。

### 1.2 AC-CANCEL-01（P0）：Review 取消被覆盖为 partial_success
`review_step_status=cancelled/interrupted` 时，上一轮把 `cancelled` 归入 `_review_failed=true`
→ `optional_failed=true` → 总任务按"可选步骤失败"写成 `partial_success`，且继续执行 chip 入队。

后果：
```
管理员在 Review 阶段取消任务
→ Review step=cancelled
→ 主流程继续
→ chip 步骤继续执行
→ 最终 cancelled 被覆盖为 partial_success
```
`cancelled` 必须立即停止后续步骤并保留取消终态；`interrupted` 应交给 reconcile/restart，
也不应降级为 partial_success。

### 1.3 FS-CONTRACT：批量与单股合同不一致
- `get_bars_batch` 入口未强制 `completed_only → include_realtime=False`（单股 `get_bars`
  在 1306-1308 强制），批量与单股合同不一致。
- `_build_daily_aggregation` 未调用单股路径统一执行的 `_finalize_bars`（排序 + 去重 +
  1d 过滤未完成 bar），而是复制了一份缩减版逻辑 → 长期可能造成单股/批量结果不一致。

## 2. 修改内容

### 2.1 `backend/app/repositories/bar_repository.py`
- **FS-DB-01 修复**：`get_daily_bars_batch` 构造 DataFrame 改为
  `columns=["instrument_id", "trade_date", *_BAR_COLUMNS]`（9 列严格对齐 SQL 返回），
  随后 `df = df.set_index("trade_date").drop(columns=["instrument_id"])`，使输出与单股
  `get_bars` 一致（无 `instrument_id` 列，仅 OHLCV + adj_factor）。

### 2.2 `backend/app/services/market_data_aggregation_service.py`
- **FS-CONTRACT 修复1**：`get_bars_batch` 入口在 `completed_only=True` 时强制
  `include_realtime = False`，与单股 `get_bars` 对齐。
- **FS-CONTRACT 修复2**：`_build_daily_aggregation` 在 qfq 之后插入
  `daily_df = _finalize_bars(daily_df, timeframe, now)`，复用与单股 `get_bars`（1545 行）
  同一套排序/去重/过滤未完成 bar 逻辑，消除两份近似实现。
  （注：单股 1d 路径无"当日 partial daily 合成"步骤——实时尾部仅针对 1m/15m/1h，
  故批量路径不补 `_synthesize_partial_daily_bar`，避免引用未定义函数。）

### 2.3 `backend/app/services/after_close_orchestrator.py`
- 新增模块级纯函数 `_is_terminal_review_short_circuit(review_step_status)`，
  判定 `cancelled` / `interrupted` 必须短路（其余终态走 partial_success 判定）。
- **AC-CANCEL-01 修复**：在 review 步骤完成之后、chip 入队之前插入短路块：
  - 当 `_is_terminal_review_short_circuit(_review_step_status)` 为真，
    写 `_update_orchestrator_status(status=终态)` + `job_run.status = 终态` +
    `finished_at` + `_update_heartbeat_and_step(终态)`，commit，`return`。
  - **不** 继续执行 chip 入队（3244 行前的短路），**不** 覆盖总任务终态；
  - `cancelled` 保持 cancelled，`interrupted` 保持 interrupted 交 reconcile/restart。
  - 类型收窄：`assert _review_step_status is not None` 满足 Mypy。

## 3. 验证证据（PURE_UNIT_TEST=1，未部署/未连接共享库）

- 新增真实行为测试：
  - `tests/test_bar_repository_batch_conversion.py`：**直接调用真实**
    `get_daily_bars_batch`（伪造 session.execute 返回内存 Row，非 mock），覆盖：
    1 只 / 多只分组 / 空 rows 返回 `{}`；
    断言输出列含全部 OHLCV + adj_factor、**不含 instrument_id**；
    DatetimeIndex 正确且单调升序；数值类型为 float（非 Decimal）。
  - `tests/test_after_close_phase0_contracts.py::test_terminal_review_short_circuit_detection`：
    **真实调用** `_is_terminal_review_short_circuit`：
    `cancelled`/`interrupted` → True；`succeeded`/`failed`/`timed_out`/`unavailable`/`None` → False。
- 升级：
  - 保留 `test_feature_snapshot_service.py::test_review_core_with_run_items_uses_mdas_batch_read_and_metrics`
    （spy 底层批量方法调用次数）；真实 Row→DataFrame 转换由上面的独立测试覆盖。
- 回归：`bar_repository_batch_conversion`（3）+ `after_close_phase0_contracts`（含新增 1）
  + `feature_snapshot_service` 相关 = **35 passed / 11 skipped**（skip 为 PG 集成）。
- Ruff：改动文件 `All checks passed!`。
- Mypy：`app/` 总错误数 = 54，与 HEAD（6c21349）= 54 持平，**new_errors=0**。
- 前端：本轮未改前端，TSC 无新增问题。

## 4. 未验证 / 未完成项（如实标注）

- **PG 集成未执行**：批量 SQL 真实 Row 转换（已用内存 Row 单测覆盖，但非真实 PG）、
  每批 bars/factor 查询次数、Review 取消不被覆盖、prereq missing 不推进检查点、
  timeout 变 partial success、publication pointer 不误切等定向 PG 测试（评估第四组）尚未运行。
- **OPS-06 精确 run 导航（第三组）未做**：本轮聚焦第一/二组 P0。当前 OPS-06 仅传
  `tradeDate`（historical date navigation），未传 `runId`（exact run navigation）。
  审查要求 URL 增 `runId`、页面优先按 runId 定位、日期作回退；需后端新增 by-run-id
  聚合接口，属下一轮。
- `_build_daily_aggregation` 的 `need_tail`/实时尾盘合并路径在单元测试中用空 DataFrame 桩替代，
  真实 pytdx 回退未端到端验证。
- 未部署、未 push main、未连接共享业务库；`data_closed=false`。
