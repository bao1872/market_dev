# CHANGE-20260804-001：三个切片缺口修复（Review 终态/检查点、FS 数据库级批读、OPS-06 任务上下文）

- 日期：2026-08-04
- 类型：behavior + contract + performance
- 领域：盘后编排 / 特征快照 / 行情数据 / 管理后台导航
- 关联 PRD：`docs/prd/30-after-close.md`、`docs/prd/40-market-stock-experience.md`
- 关联 Maps：`docs/maps/30-after-close.md`
- 关联前置：CHANGE-20260803-006（FS 主链 MDAS 批读）、评估结论（remote_consistency=passed / incremental_mypy_gate=passed / 三个切片 partial 或部分失败）

## 1. 背景与问题（来自评估结论）

评估把三个切片判为 `partial_pass` / `failed`，本轮闭合其中明确可代码修复的三个缺口：

- **A 组缺口1（Review prereq 检查点）**：`_execute_review_step` 中 stock core / board aggregation / snapshot 前置条件缺失时，`review_status=skipped / prereq_missing=true`，但随后调用 `_update_heartbeat_and_step(db, job_run, COMPUTING_REVIEW.value, ...)` 把 `computing_review` 记成已完成，导致后续 resume 永久跳过 Review。
- **A 组缺口2（执行器超时终态）**：`_review_failed` 仅看 review 业务结果 `result.get("failed")`；执行器 `timed_out/unavailable/interrupted/cancelled` 返回 `result=None` 或 `failed=False`，但 `step_summary.status` 已如实记录，最终 `partial_success` 判定未读取它，存在"超时误判为 succeeded"路径。
- **B 组（FS 仍 N+1）**：`get_bars_batch` 旧实现仅把逐股 `get_bars` 循环收口为接口级批处理，底层仍每只股票单独查 `bars_daily` + `adj_factor`（N×2 DB 往返）。旧测试只 mock `get_bars_batch`，未统计真实 repository SQL 次数，不能宣称消除 N+1。
- **C 组（OPS-06 上下文）**：`AdminJobsPage` 对 `after_close_orchestrator` 任务的"盘后详情"链接固定为 `/admin/after-close`，未携带被点击 run 的 `tradeDate`，目标页默认显示最新任务，历史任务跳转失效。

## 2. 修改内容

### 2.1 `backend/app/services/after_close_orchestrator.py`
1. **prereq 检查点（缺口1）**：`_execute_review_step` 的 `_update_heartbeat_and_step` 调用把 `last_completed_step` 实参由 `AfterCloseRunStatus.COMPUTING_REVIEW.value` 改为 `None`——仅刷新心跳/租约，不推进检查点。`_add_pipeline_event(step=COMPUTING_REVIEW.value, level="warn", message="复盘跳过: ...")` 仍保留用于事件标记，不构成检查点推进。
2. **执行器终态（缺口2）**：解包 review 业务状态时新增 `_review_step_status = _review_step_summary.get("status")`，`_review_failed` 推导同时覆盖业务 `failed` 与执行器终态集合 `{"failed","timed_out","unavailable","interrupted","cancelled"}`。最终 `partial_success` 判定因此同时消费 review 业务结果与 step summary。

### 2.2 `backend/app/repositories/bar_repository.py`
3. 新增**数据库级批量查询** `get_daily_bars_batch(session, instrument_ids, start_date, end_date)`：
   一次 `BarDaily.instrument_id.in_(instrument_ids)` SQL 读取整批日线（OHLCV + adj_factor），按 `instrument_id` 用 `itertools.groupby` 分组为 `{iid: DataFrame}`。
4. 新增 `get_adj_factor_series_batch(session, instrument_ids, as_of=None)`：
   一次 `BarDaily.instrument_id.in_(...)` 且 `adj_factor.isnot(None)` SQL 读取整批复权因子，同样按 `instrument_id` 分组（支持 `as_of` 截断）。

### 2.3 `backend/app/services/market_data_aggregation_service.py`
5. **`get_bars_batch` 改为真正数据库级批读**：整批只发起
   - 1 次 `get_daily_bars_batch`（bars SQL）；
   - 1 次 `get_adj_factor_series_batch`（adj_factor SQL，仅 qfq 路径）；
   - 1 次共享的 `_call_expected_last_completed_daily_bar(now)`（按日期，与标的无关）。
   随后在内存按 `instrument_id` 分组，逐股复用同一套 bars/复权/诊断构造合同，通过新增模块级 `_build_daily_aggregation(...)`（复用日线路径的 qfq 乘权、聚合、hash、latest_daily_quote 逻辑，无 per-instrument DB 查询；`need_tail` 罕见路径才按需 `fetch_daily_bars`）。日内周期（非 1d/1w/1mo）退回逐股 `get_bars` 以保持合同一致。
   → N×2 DB 往返降为约 3 次/批。

### 2.4 `frontend/src/pages/AdminJobsPage.tsx`
6. OPS-06 链接携带被点击 run 的业务日期：`/admin/after-close?tradeDate=${selectedRun.business_date}`。

### 2.5 `frontend/src/pages/AdminAfterClosePipelinePage.tsx`
7. 通过 `useSearchParams` 读取 `tradeDate`，初始化 `selectedDate`，使从 Jobs 页跳转进来的历史任务直接定位到对应 run（而非默认最新）；刷新/返回保持同一任务（URL 为单一事实源）。

## 3. 验证证据（PURE_UNIT_TEST=1，未部署/未连接共享库）

- 新增/升级测试：
  - `test_feature_snapshot_service.py::test_review_core_with_run_items_uses_mdas_batch_read_and_metrics`：**升级**为 spy 底层 `get_daily_bars_batch` / `get_adj_factor_series_batch`，断言每 claim 批只各触发 1 次批量 SQL（共 `batch_count=3` 次），而非逐股 N×2；预读 bars 经 `primary_bars` 传入单股计算；每股独立 upsert（AC-08 检查点不变）。
  - `test_after_close_phase0_contracts.py::test_review_prereq_missing_does_not_advance_checkpoint`：源码守卫，prereq_missing 分支 `_update_heartbeat_and_step` 的 step 实参必须为 `None`，且不得把检查点写为 `COMPUTING_REVIEW`。
  - `test_after_close_phase0_contracts.py::test_review_executor_timeout_forces_partial_success`：源码守卫，`_review_failed` 推导必须读取 `_review_step_summary.status` 且覆盖 `timed_out/unavailable/interrupted/cancelled`。
- 回归：`tests/test_feature_snapshot_service.py` + `test_after_close_orchestrator.py` + `test_after_close_phase0_contracts.py` = **34 passed / 41 skipped**（skip 为 PG 集成，不在 PURE_UNIT 范围）。
- Ruff：改动文件 `All checks passed!`。
- Mypy：`app/` 总错误数 = 54，与基线 HEAD（9f6ab56=55）持平，**new_errors=0**（修改未扩大豁免）。
- 前端：`tsc --noEmit` 退出码 0（两个改动页面类型通过）。

## 4. 未验证 / 未完成项（如实标注）

- **真实性能收益未基准**：单元测试证明数据库级批读调用次数（每批 bars 1 次 + 因子 1 次），但 7 小时主链的真实耗时/CPU 收益需在共享开发库或生产环境基准核验，不在本轮授权范围。
- **PG 集成未执行**：cancel 后旧 Worker 写入 fence、Review 失败/前置缺失检查点、reconcile 产物、partial success 持久化、publication/pointer 不误切、批量查询 SQL 次数统计等定向 PG 测试（评估第二阶段）尚未运行（`PURE_UNIT_TEST=1` 不连库）。
- `_build_daily_aggregation` 的 `need_tail`/实时尾盘合并路径在单元测试中用空 DataFrame 桩替代，真实 pytdx 回退路径未端到端验证。
- 未部署、未 push main、未连接共享业务库；`data_closed=false`。
- **评估仍需人工复验的残余项**：A 组两个缺口的代码逻辑已修复并有源码守卫测试，但端到端行为（resume 续跑、超时进入 partial_success）建议以 PG 集成测试最终确认；B 组 `database_batch_query` / `adj_factor_batch_query` 现已有真实 repository 级批量方法 + 调用计数测试，可由 `failed → passed`；C 组 `selected_run_context` / `historical_run_navigation` 已由 URL tradeDate 接线，待 PG/端到端确认。
