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
