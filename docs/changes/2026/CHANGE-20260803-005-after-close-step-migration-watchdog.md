# CHANGE-20260803-005: 盘后状态机收口 — 全步骤迁移统一执行器 + Stale Watchdog 接线 + 状态合同扩展

- 日期：2026-08-03
- 类型：behavior+contract+architecture
- 领域：盘后编排状态机 / 管理后台 cancel·reconcile API / 状态查询
- 关联 PRD：`docs/prd/30-after-close.md`（AC-02 统一 step 执行器、AC-04 日线 readiness）
- 关联 Maps：`docs/maps/30-after-close.md`
- 关联 Changes：CHANGE-20260803-004（PRD V1.0 收口，本轮在其状态机骨架基础上补齐全步骤迁移与 watchdog）
- 数据操作：**零写入**（未部署、未运行编排、未修改共享开发业务数据库；纯单元测试模式 `PURE_UNIT_TEST=1`）

## 1. 为什么改

`execute_orchestrator_step`（超时 / heartbeat / 结构化 summary / 协作取消 / 可选降级）已在 CHANGE-20260803-004 落地，但只有 `auction_anchor` 一个步骤使用它。其余顶层步骤仍各自手写 heartbeat + try/except，导致：

1. **状态不统一**：步骤级状态（超时 / 不可用 / 取消 / 中断）没有唯一来源，admin 时间线无法一致展示；
2. **Watchdog 不完整**：`get_after_close_run_status` 只有 heartbeat stale 判定，缺少步骤级超时（step_summary.elapsed_seconds vs step timeout）检测；
3. **准入失败处理不一致**：`checking_coverage` 覆盖率不足时原实现直接 `return`（优雅终态），但迁移到执行器后异常向上传播，导致 `execute_after_close_run` 抛出未处理异常（破坏既有"标记 failed 不崩溃"的契约）；
4. **可选阶段降级不透明**：auction / review / aggregation 失败或 stock_core 被 superseded 时，主任务应表达为 `partial_success`（核心已发布、后置降级），而非笼统 `succeeded` 或 `failed`。

## 2. 修改范围

### 2.1 全步骤迁移到 `execute_orchestrator_step`（AC-02）
- `refreshing_daily`：通过 `heartbeat` 参数传入独立 `_job_run_heartbeat_loop`，保留长步骤 heartbeat；
- `syncing_boards`：业务体抽取为模块函数 `_execute_syncing_boards`，执行器以 `optional=True` 包装（软失败不阻断主流程）；
- `checking_coverage`：闭包 `_check_coverage_op` 读取 `batch_result.daily_coverage`；覆盖率不足抛错，由调用点位于 `execute_orchestrator_step` 外捕获，转入优雅终态（标记 failed + `return`），**不向上传播异常**（保持与 HEAD 一致）;
- `computing_features`：闭包 `_compute_features_op` 捕获上下文，外部 try/except 在失败时将 snapshot run 标 failed；
- `publishing`：核心发布闭包 `_run_core_publish_op` 返回 dict（含 `published_run` / `publish_failed` / `stock_core_published` / `stock_core_superseded`）；
- `computing_review`：保留既有分支逻辑；Review 计算/发布失败不再让整 run failed，仅置 `_review_failed=True`（核心已发布），主 run 收尾为 `partial_success`。

### 2.2 Step Contract 扩展（唯一来源）
- 新增步骤终态：`unavailable`（可选步骤无数据）、`timed_out`、`cancelled`、`interrupted`；
- 废弃组合态 `skipped_unavailable`（跳过与不可用是两个独立概念；可选无数据 → `unavailable`，断点恢复显式跳过 → `skipped`）；
- `AfterCloseRunStatus` 新增总任务级终态：`PARTIAL_SUCCESS`（核心已发布、可选阶段失败/降级）、`INTERRUPTED`（Worker 崩溃/租约失效）、`CANCELLED`（管理员协作式取消）。

### 2.3 Stale Watchdog 接线
- `get_after_close_run_status`：在既有 `heartbeat_stale` 判定基础上，新增步骤级超时检查（`step_summary.elapsed_seconds` vs `_step_timeout(step)`），并暴露 `stale` / `step_summary` / `running_steps` / `step_timed_out` / `partial_success` 字段；
- 可选失败集合 `optional_failures` 判定更新为 `{failed, unavailable, timed_out, interrupted}`（去除废弃的 `skipped_unavailable`）。

### 2.4 cancel / reconcile 真实语义
- `cancel_after_close_run`：新增 `actor` / `request_id` 参数，写取消事件，递增 `lease_epoch` 实现 fence；
- `reconcile_after_close_run`：新增 `actor`，running→检测 stale 时标记 `interrupted`，写 reconcile 事件；
- `admin_after_close.py` 的 `cancel` / `reconcile` 端点：传入 `actor=current_user.username` 与 `request_id`。

### 2.5 进度事件事务修复
- `_build_feature_snapshot_progress_callback` 与 step progress 回调：`append_event` / metadata 写入后显式 `await db.commit()`，避免 session 退出回滚导致事件丢失。

## 3. 测试影响与修复

| 测试 | 失败原因 | 修复 |
|---|---|---|
| `test_ac04_daily_missing_blocks` | 迁移后覆盖率不足异常向上传播，`execute_after_close_run` 抛出而非优雅标记 failed | 调用点捕获 `execute_orchestrator_step` 异常 → `daily_coverage_ok=False` → 标记 failed + return |
| `test_ac04_daily_ready_15m_missing_allows_proceed` | 新增 auction_anchor 步骤在 mock 环境下真实 `generate_and_publish_auction_anchors` 失败 → `partial_success` | 测试补充 `generate_and_publish_auction_anchors` mock（测试环境无法跑真实锚点生成） |
| `test_orchestrator_step_pure_unit` (2) | 断言废弃态 `skipped_unavailable` | 改为断言新合同 `unavailable` / `timed_out` |
| `test_worker_idempotency::test_board_sync_registered_in_after_close_orchestrator` | `syncing_boards` 抽取后源码不含字面 `sync_boards` | 静态断言改为匹配 `syncing_boards` / `_execute_syncing_boards` |
| `test_after_close_board_sync` (4) | 源码级断言指向已迁移到 `_execute_syncing_boards` 的逻辑 | 将 BOARD_SYNC_ENABLED / soft-failure / error_code / reused_previous_snapshot 等断言改查 `_execute_syncing_boards` helper |

> 说明：`daily_ready_15m` 测试补 mock 属于测试环境限制（真实 auction anchor 生成依赖外部数据源），不改变产品行为；auction 失败在生产环境经 `optional=True` 降级为 `partial_success`，符合设计。

## 4. 验证结论

- **本地纯单元测试（PURE_UNIT_TEST=1）通过**：`test_after_close_orchestrator.py`（3 passed / 30 skipped-DB）、`test_orchestrator_step_pure_unit.py`（2 passed）、`test_after_close_board_sync.py`（12 passed / 2 skipped）、`test_worker_idempotency::test_board_sync_registered_in_after_close_orchestrator`（passed）；
- **Ruff**：`after_close_orchestrator.py` / `admin_after_close.py` / 相关测试文件全部 `All checks passed!`；
- **未验证项（如实标注，不声称通过）**：
  - 依赖 Postgres 的 after_close 集成测试（`test_after_close_status_detail.py` / `test_after_close_endpoints.py` / `test_after_close_worker.py` 等）在本地未运行（需 SSH 隧道连共享开发库，非本次任务授权范围）；
  - 其他模块既有失败（`test_auction_replay_entitlement` / `test_bars` / `test_calendar_v9_regression` / `test_stock_state_and_events` 等约 20 项）经 `git stash` 对照确认为 dev HEAD 既有失败，与本次修改无关；
  - 未部署、未 push main、未修改共享业务数据。

## 5. 已知偏离与后续

- `computing_review`（约 380 行）因风险与纯单元测试约束，未整体包裹进 `execute_orchestrator_step`，沿用既有分支逻辑但通过 `_review_failed` 接入统一 `partial_success` 收尾；Review service 自身有独立状态机，整体迁移列为后续工作。
- 真实 Feature Snapshot 主链（7 小时）性能与端到端运行验证需在共享开发库 / 生产环境进行，不在本轮本地验证范围。

## 6. Phase 0 修正与收口（2026-08-03）

> 本 CHANGE 原稿基于 `90c3eaa` 后的开发中间态，其中若干"已完成"结论被审阅评估为夸大（见 §6.1）。Phase 0 在保留原改动价值的基础上补齐了这些缺口，并新增了本收口对应的行为测试。以下描述的是 Phase 0 修复后的**真实实现**，与 `after_close_orchestrator.py` 逐字一致。

### 6.1 对原稿结论的修正

| 原稿表述 | 评估 | Phase 0 实际 |
|---|---|---|
| §2.1 标题"全步骤迁移到执行器" | 夸大：`computing_review` 明确未包裹进执行器 | 保持如实标注：`computing_review` 仍不经过 `execute_orchestrator_step`，经内联 `review_orchestrator_service` 逻辑 + `_update_orchestrator_status` 实现，通过 `_review_failed` 接入统一 `partial_success`。**"全步骤迁移"结论不成立**，PRD AC-02 中 `auction_anchor / enqueue_chip_job / watchlist_ready` 中 watchlist_ready 仍未成为正式步骤 |
| §2.2 运行中取消 | 原稿只做开始前一次检查，`_run_with_cancellation` 仅 `wait_for`，无法在运行中终止业务 | 已实现真实运行中取消：`_run_with_cancellation` 把 operation 建为独立 task，周期调用 `cancellation_check`，命中时 `op_task.cancel()` + `await`，`_StepCancelledError` 转 `cancelled` summary 不炸穿 Worker（`execute_orchestrator_step` L273-320/L240-247） |
| §2.3 stale watchdog | 原稿 `elapsed_seconds` 仅 finally 后计算，运行期恒为 None，`step_timed_out` 无法实时触发 | 已实现运行期实时判定：执行器唯一周期循环 `_tick_loop` 每 `_HEARTBEAT_INTERVAL_SECONDS`(10s) 刷新 `elapsed_seconds / heartbeat_at / last_progress_at`，`get_after_close_run_status` 用 `running + elapsed_seconds > timeout` 判定 `step_timed_out`（`stale = heartbeat_stale or step_timed_out`） |
| §2.4 chip 创建时机 | 原稿 chip 在主 run succeeded 之后创建，创建失败不进入 partial_success | 已抽为正式步骤 `_enqueue_chip_job_step`（step=`enqueue_chip_job`），在主任务终态提交**之前**调用（L3126 → L3175），返回 `(status, chip_job_id)`，入队失败纳入 `_optional_failed` 判定 partial_success，metadata 记录 `chip_enqueue_status / chip_job_id` |

### 6.2 心跳契约修正（单一周期循环）

- `_job_run_heartbeat_loop`（无限循环）不再作为 heartbeat 回调传入执行器；改为 `_make_step_heartbeat` 构造**单次 touch** 回调，执行器在唯一周期循环 `_tick_loop` 内每次 touch 一次 `touch_job_run_heartbeat`（fenced UPDATE，检查 lease_epoch + status='running'，失败即停止心跳）。
- 效果：心跳从"独立无限循环"收敛为"执行器单一周期循环内的单次 touch"，避免循环套循环，且运行期心跳持续更新。

### 6.3 syncing_boards 的 result/summary 分离

- 原调用 `board_summary, _ = await execute_orchestrator_step(...)` 把业务 result 误当执行器 summary，导致业务 failed 时 step summary 仍为 succeeded，且超时 result=None 时取下标会二次抛错。
- 修复为 `board_result, board_step_summary`，将业务 `failed/skipped` 如实映射到 step summary 并 `_persist_step_summary`，避免"业务失败 / 步骤 succeeded"的矛盾状态。

### 6.4 API 合同完整透传

- `AfterCloseRunStatusResponse` 新增 `step_summary / running_steps / step_timed_out / stale / partial_success`，并补透传 `skip_reason`；端点（`admin_after_close.py`）完整传入这些字段，修复"service 已计算 → API 丢弃 → 管理后台看不到"的合同断链。

### 6.5 Review 失败不推进检查点

- `_update_heartbeat_and_step` 的 `last_completed_step` 改为 `str | None`；`None` 表示"仅刷新心跳/租约，不推进 last_completed_step 检查点"。
- Review 失败/质量门阻塞时（`_review_failed=True`）调用传 `None`，主任务收 `partial_success` 但 `last_completed_step` 保持不进 `computing_review`，下次 resume 不会跳过失败的 Review。

### 6.6 reconcile 补齐（request_id / lease fencing / 产物核验）

- `reconcile_after_close_run` 接入 `request_id`（端点已生成但此前未传入），metadata 写 `reconcile_request_id`。
- running→interrupted 时：写 `finished_at`、释放 `lease_expires_at`、**递增 `lease_epoch`**（fence 旧 Worker，防止旧 Worker 心跳匹配后继续写业务数据）、把仍 `running` 的 step_summary 收敛为 `interrupted`。
- 新增 `_inspect_run_artifacts`：只读核验 `factor_publications` 表 `scope_type='market'` 且 `publication_kind IN ('stock_core','market_aggregation')` 的真实 pointer，记录 `reconcile_artifacts / reconcile_contradictions`（`STOCK_CORE_PUBLISHED_BUT_RUN_NOT_SUCCEEDED` 等），暴露"产物与任务状态矛盾"。
- reconcile 事件 payload 含 `actor / request_id / artifacts / contradictions / new_lease_epoch`。

### 6.7 补充行为测试

- `tests/test_orchestrator_step_pure_unit.py`：新增 6 项行为测试，覆盖运行中取消、cancellation_check 周期轮询、心跳/运行期 elapsed 持续更新、timeout_seconds 透传、非可选超时抛出、超时后 cancel operation task。
- `tests/test_after_close_phase0_contracts.py`：新增 9 项合同测试，覆盖 board 软失败如实 summary、Review 失败不推进检查点、chip 入队终态前、API 字段序列化等。
- 既有测试调整：`test_change_20260729_003` 源码断言改为检查 `execute_after_close_run` + `_enqueue_chip_job_step` 拼接（chip 逻辑迁移）；`test_ac04_daily_ready_15m_missing_allows_proceed` 用装饰器 mock 掉 chip 入队（避免 20 层嵌套块上限）。

### 6.8 验证结论（PURE_UNIT_TEST=1，未部署/未连接共享库）

- `test_orchestrator_step_pure_unit.py`：9 passed；`test_after_close_phase0_contracts.py`：9 passed。
- 盘后相关套件（`-k "after_close or orchestrator or change_20260729"`）：67 passed / 0 failure。
- 全量纯单元测试：失败数回到 pre-existing 基线（`test_auction_replay_entitlement / test_bars / test_calendar_v9_regression / test_stock_state_and_events` 等约 11 项，经 `git stash` 对照确认为 dev HEAD 既有失败，与盘后无关）。
- Ruff：全部改动文件 `All checks passed!`。
- **关键行为证明**：`test_mid_run_cancel_actually_stops_operation` 在旧 `_run_with_cancellation`（仅 `wait_for`）语义下会失败（业务协程跑完全程、零取消检查），新实现正确终止——验证运行中取消是真实行为而非字符串断言。

### 6.9 仍未验证 / 未完成项（如实标注，不声称通过）

- **`computing_review` 已整体接入 `execute_orchestrator_step`（AC-02 收口，2026-08-03 第二轮）**：复盘业务体抽为模块级协程 `_execute_review_step`，由执行器包装（optional=True，软失败映射 step_summary 收 partial_success）。见 §6.10。
- **`watchlist_ready` 不经过执行器（设计使然）**：它是**派生就绪指示器**（`has_succeeded_snapshot_run` 推导 succeeded+published+full snapshot），非可执行工作步骤，无 operation/timeout/heartbeat；仅作为 admin 流水线可视化终态展示步骤。强制塞进执行器会造出空 operation，违反最小必要修改原则，故如实标注为"非执行器步骤"，AC-02"全步骤迁移"仅对**可执行顶层步骤**成立。
- 依赖 Postgres 的 after_close 集成测试（`test_after_close_status_detail.py / test_after_close_endpoints.py / test_after_close_worker.py` 等）本地未运行（需 SSH 隧道连共享开发库，非本轮授权范围）。
- 未部署、未 push main、未修改共享业务数据；`data_closed=false`。

### 6.10 computing_review 执行器收口（2026-08-03 第二轮）

- 原内联 `computing_review`（约 410 行）抽为模块级协程 `_execute_review_step(...)`，返回业务 result dict（`status/failed/reason/run_id/publication_id/scope_count/signal_count/coverage/blockers/prereq_missing/resume_skipped`），内部保留既有幂等 create_run/compute_run/resume_run/publish_run 与 publication pointer 唯一事实源语义。
- `execute_after_close_run` 改经 `execute_orchestrator_step("computing_review", lambda: _execute_review_step(...), timeout_seconds=_step_timeout("computing_review")=1800, optional=True, ...)` 包装，满足 AC-02；执行器唯一周期循环负责 heartbeat 单次 touch + 运行期 elapsed 刷新 + 运行中取消。
- 软失败映射：`_execute_review_step` 不抛异常，仅 `result["failed"]=True`；调用方在 `_review_step_summary["status"]=="succeeded" and _review_failed` 时改置 `failed`（`REVIEW_SOFT_FAILURE`）并 `_persist_step_summary`，避免"业务 failed / 步骤 succeeded"矛盾（与 syncing_boards 同一合同）。
- 检查点语义不变：`_execute_review_step` 内部失败时传 `None` 不推进 `last_completed_step`，成功才推进 `computing_review`。
- 新增 AC-02 合同测试：`test_execute_after_close_run_wires_computing_review_through_executor`（源码守卫：主编排必须经 `execute_orchestrator_step("computing_review", ...)` 且不内联 import review_orchestrator_service）、`test_review_step_prereq_missing_returns_skipped`、`test_review_step_resume_skip_returns_resume_skipped`。盘后+编排套件 214 passed / 0 failure，Ruff 全绿。
