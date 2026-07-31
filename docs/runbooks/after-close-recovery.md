# 盘后失败恢复 Runbook

本 Runbook 描述盘后链路各阶段失败的正式恢复路径。所有恢复操作必须走正式 service / CLI / admin API，禁止裸 SQL、`/tmp` Python、`docker cp` 或 `docker exec ... python -c "..."` 写入。详见 `rules/80-deployment-data-safety.md` "生产修改与部署版本合同"。

## 前置条件

- 已通过 `scripts/ops/panji-prod-preflight` 校验生产服务器入口；
- 已读取目标 run 的当前状态（`scheduler_job_runs` / `strategy_runs` / `factor_publications`）；
- 失败 run 的 `job_run_id` / `snapshot_run_id` / `trade_date` 已确认；
- 恢复操作在 `/root/web_dev` 仓库的容器内执行，禁止在宿主机直接操作 DB。

## 1. DSA 失败恢复

**场景**：after_close 编排中 DSA StrategyRun 状态为 `failed` / `partial_failed` / `max_retries_exceeded`，需要重新计算。

**正式入口**：`backend/app/services/dsa_recovery_service.py::recover_failed_dsa_run`

**行为**：
- 读取 orchestrator `SchedulerJobRun` 的当前 `dsa_run_id`；
- 若 DSA run 为 `completed` / `published` → 直接复用，返回 `(run, False)`；
- 若 DSA run 为 `running` 且 lease 未过期 → 拒绝恢复（正在执行）；
- 若 DSA run 为 `failed` / `partial_failed` → 创建新 DSA run（`create_batch_run` 自动递增 `attempt_no`），原子更新 orchestrator metadata 中的 `dsa_run_id`，返回 `(new_run, True)`；
- 原失败 run 保留审计，**禁止把失败 run 直接改回 `queued`**；
- 恢复次数上限 `_MAX_DSA_RECOVERY_COUNT = 5`，超过抛 `DSARecoveryError`。

**调用方式**（通过正式 CLI / admin API；DSA CLI 已实现，禁止 `docker exec ... python -c`）：

```bash
# [P0-3 2026-07-30] DSA Recovery CLI 默认 dry-run（只读状态），实际执行需显式 --execute
# 只读检查（dry-run）：
docker exec trading-backend python -m scripts.dsa_recovery_cli --job-run-id <job_run_id>

# 实际执行恢复（写库）：
docker exec trading-backend python -m scripts.dsa_recovery_cli --job-run-id <job_run_id> --execute
```

**[P0-3 2026-07-30] fencing 约束**：
- Orchestrator 调用 `recover_failed_dsa_run` 时传入 `worker_id` + `lease_epoch`，新 run 通过 `claim_for_worker=f"orchestrator:{worker_id}"` 绑定当前 orchestrator，generic strategy worker 无法抢占；
- CLI/admin 路径不传 `worker_id`，fallback 使用 `claim_for_worker=f"orchestrator:recovery:{job_run_id}"`，仍归 orchestrator 命名空间；
- 新 run 创建即 `status=running + worker_id`，避免 generic worker 通过 `claim_next_run` 抢走 recovery run。

**禁止**：
- 禁止裸 SQL `UPDATE strategy_runs SET status='queued' WHERE ...`；
- 禁止 DELETE 失败 run；
- 禁止 `/tmp` Python 脚本直接操作 ORM。

**验证**：
- 新 DSA run `status=queued`，`attempt_no` 递增；
- orchestrator metadata `dsa_run_id` 已切换至新 run；
- 旧 run 仍保留 `failed` / `partial_failed` 状态用于审计。

## 2. chip_consensus 恢复

**场景**：`after_close_chip_consensus` job 状态为 `interrupted`（worker 超时或 SIGTERM），需要继续计算未完成 instrument。

**正式入口**：worker 自动领取，无需手工干预。

**行为**：
- `scheduler_job_run_recovery_service.auto_resume_interrupted_after_close_runs` 同时扫描 `after_close_orchestrator` 和 `after_close_chip_consensus` 两类 `interrupted` 任务；
- `interrupted` → `resume_queued`（最多 3 次，超过标记 `failed` 并通知 admin）；
- worker `_chip_consensus_poll_once` 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取 `queued` / `resume_queued` 任务；
- 断点续算：`get_pending_chip_instruments` 过滤已 `succeeded` 的 instrument，`resume_queued` 任务只重试未成功项；
- 部分成功写 `metadata.chip_status=partial`，主 `status=succeeded`；
- `lease_epoch` fencing 防止旧 worker 覆盖新 worker 状态。

**手工触发**（仅在 watchdog 未自动恢复时）：

```bash
# 只读检查：确认有 interrupted 的 chip_consensus 任务
ssh panji-prod 'docker exec trading-postgres psql -U bz -d bz_stock -c "
  SELECT id, status, attempt_no, heartbeat_at
  FROM scheduler_job_runs
  WHERE job_name = '\''after_close_chip_consensus'\''
    AND status = '\''interrupted'\''
  ORDER BY created_at DESC LIMIT 5;"'

# 等待 watchdog 下一次扫描（默认周期），或通过正式 admin API 触发恢复
# （若 admin API 尚未实现 resume 端点，需新增，禁止裸 SQL 改状态）
```

**禁止**：
- 禁止裸 SQL `UPDATE scheduler_job_runs SET status='queued'`；
- 禁止 DELETE 卡住的 chip run；
- chip 失败不反改 core（`execute_after_close_chip_consensus` 内部已隔离）。

**验证**：
- `resume_queued` → `running` → `succeeded`；
- `stock_chip_consensus_snapshots` 中 `core_run_id` 等于当前 `snapshot_run_id`；
- 已 succeeded 的 instrument 未被重算。

## 3. stock_core pointer 恢复

**场景**：DSA 计算成功、snapshot run `succeeded`，但 `factor_publications` 中 `kind=stock_core` 的 pointer 缺失或指向旧 run。

**正式入口**：`backend/app/services/factor_publication_service.py::publish_stock_core`

**行为**：
- 计算 coverage（如未传入）；
- `coverage < CORE_PUBLICATION_MIN_COVERAGE (0.98)` 抛 `CoverageBelowThresholdError`；
- `pg_insert(...).on_conflict_do_update(constraint="uq_factor_publications_scope_date_kind")` 原子切换 pointer；
- 幂等：相同 `trade_date` + `snapshot_run_id` 重复调用只更新 `published_at` 和 `coverage_ratio`，不产生重复行；
- 指针更新失败只重试本函数，不重新计算数据。

**[P0-1+P0-2 2026-07-30] 可见性窗口与 superseded 语义**：
- Orchestrator 发布顺序修正为：pointer 发布 FIRST → snapshot `finish_snapshot_run(succeeded)` SECOND；
- pointer 失败或指向其他 run 时，snapshot 保持 `running` 状态（无 `published_at`），API fallback 不可见，避免读到未确认数据；
- 当 `existing_pub.data_run_id != 当前 snapshot_run_id` 时：
  - 当前 run 标记为 `_stock_core_superseded=True`，**不得** 标记 `_stock_core_published=True`；
  - **不得** 基于当前 run 聚合（market_aggregation / board_analysis）；
  - 写 `suppressed/superseded` 结构化结果（event + payload.superseded=True + superseded_by_run_id）；
  - snapshot 不被标记 `succeeded`（不写 `published_at`），保持 `running`；
  - 禁止用旧 pointer 证明当前 run 发布成功。

**调用方式**（通过正式 CLI / admin API；当前 CLI 尚未实现）：

```python
# 在 backend/scripts/ 下新增 publish_stock_core_cli.py 后执行：
# docker exec trading-backend python -m scripts.publish_stock_core_cli \
#   --trade-date 2026-07-29 --snapshot-run-id <snapshot_run_id>
```

**禁止**：
- 禁止裸 SQL `INSERT INTO factor_publications ...` 或 `UPDATE factor_publications SET data_run_id=...`；
- 禁止绕过 coverage 门禁强制发布；
- 禁止 pointer 倒退到旧 run（`on_conflict_do_update` 已防止，但手工 SQL 不受保护）。

**验证**：
- `factor_publications` 中 `(scope_type='market', scope_key='market', trade_date=<date>, publication_kind='stock_core')` 行存在；
- `data_run_id` 等于目标 `snapshot_run_id`；
- `coverage_ratio >= 0.98`；
- `published_at` 已设置；
- 读取端 `stock_context.py` / `market_stocks` / `watchlist` 优先读 pointer，无 pointer 时才回退 `published_at IS NOT NULL`。

## 4. 聚合失败恢复

**场景**：`market_factor_aggregation` 或 `board_analysis` 失败，但 `stock_core` pointer 已发布。

**正式入口**：
- 市场聚合：`backend/app/services/market_factor_aggregation_service.py::run_market_factor_aggregation`
- 板块分析：`backend/app/services/board_analysis_service.py::compute_board_analysis` / `compute_all_boards` / `publish_board_analysis`

**行为**：
- `run_market_factor_aggregation` 读取已发布 `stock_core` pointer，校验 `source_core_run_id` 一致后切换 `market_aggregation` pointer；
- `board_analysis` 输入门禁要求存在已发布 `stock_core` pointer，否则拒绝计算；
- 聚合失败只重跑聚合，**不影响已发布 stock_core**；
- 板块分析单板块失败不阻塞其他板块；
- `coverage_ratio >= 0.95`（板块）/ 通过门禁（市场聚合）才切 pointer，不足时保存 `partial` 但不发布。

**调用方式**：

```bash
# 板块分析（已有 CLI，CHANGE-20260730-011）
ssh panji-prod "docker exec trading-backend python -m scripts.board_analysis_cli --all --publish"

# 市场聚合（CLI 尚未实现，需新增 scripts/market_factor_aggregation_cli.py）
# docker exec trading-backend python -m scripts.market_factor_aggregation_cli --trade-date 2026-07-29
```

**禁止**：
- 禁止裸 SQL 改 `board_analysis_snapshots.status` 或 `factor_publications` pointer；
- 禁止为绕过 coverage 门禁直接 INSERT pointer；
- 禁止重跑聚合时回退 stock_core pointer。

**验证**：
- `factor_publications` 中 `kind=market_aggregation`（scope_type=`market` 或 `board`）pointer 已切换；
- `source_core_run_id` 等于当前 `stock_core` pointer 的 `data_run_id`；
- `stock_core` pointer 未被改动；
- 板块分析 `coverage_ratio >= 0.95`。

## 5. Review 冷启动

**场景**：复盘模块刚上线，无 `market_review_runs` 历史，`metric_engine` 因缺少 ≥60 个交易日 scope snapshot 历史无法归一化。

**正式入口**：`backend/app/services/review_bootstrap_service.py::bootstrap_history`

**行为**：
- 从已发布 `stock_core` 历史（`factor_publications where kind=stock_core`）回填；
- 对每个历史交易日：读取 stock_core snapshot → 解析 market 范围成员 → 计算 P/Q/U/C/V 原始值（raw values，无需归一化）→ 存储为 scope snapshot（`metadata.bootstrap=True` 标记）；
- 可重复执行：相同 `trade_date` 已有 bootstrap snapshot 时跳过；
- `dry_run=True` 时只计算不写入（canary 用）；
- 默认回填 `DEFAULT_BOOTSTRAP_DAYS=120` 日，最低 `MIN_BOOTSTRAP_DAYS=60`；
- Bootstrap 专用版本 `BOOTSTRAP_ALGORITHM_VERSION="bootstrap-1.0.0"`，与正式 review 算法版本隔离。

**调用方式**（通过正式 CLI / admin API；当前 CLI 尚未实现）：

```python
# 在 backend/scripts/ 下新增 review_bootstrap_cli.py 后执行：
# docker exec trading-backend python -m scripts.review_bootstrap_cli --days-back 120 --no-dry-run
```

**禁止**：
- 禁止修改 `stock_core` 数据（bootstrap 只读）；
- 禁止修改现有 review run（只创建 bootstrap run）；
- 禁止绕过 publish gate（bootstrap 只补历史，不 force publish）；
- 禁止裸 SQL 写 `market_review_scope_snapshots`。

**验证**：
- `market_review_scope_snapshots` 中 `metadata.bootstrap=true` 的记录数 ≥ 60 个交易日；
- 每个 bootstrap snapshot 的 `source_core_run_id` 等于对应 `trade_date` 的 `stock_core` pointer `data_run_id`；
- `_build_scope_history` 能拾取 bootstrap 写入的历史 raw values；
- 正式 review run 创建后 `normalizedValue` 不再为 NULL。

## 安全边界

- 所有恢复操作必须走正式 service / CLI / admin API，**禁止裸 SQL、`/tmp` Python、`docker cp`、`docker exec ... python -c "..." 写入**（详见 `rules/80-deployment-data-safety.md` "生产修改与部署版本合同"）；
- 恢复前必须先只读确认失败 run 的当前状态和根因；
- 恢复后必须按 `rules/70-trae-cn.md` "闭环恢复与成功判定硬约束" 三要素验证：pointer + 版本 + 真实数据证据；
- pointer 切换失败只重试发布，不重算数据；
- 已 succeeded 且 `input_hash` + `algorithm_version` 一致的 item 不得重算；
- chip / aggregation / review 等 optional 任务失败只重试自身，不反改 core；
- 当前未实现 CLI 的恢复路径（DSA 恢复、stock_core 发布、市场聚合、Review bootstrap）需先在 `backend/scripts/` 下新增正式 CLI 包装再执行，**禁止用 `/tmp` Python 绕过**。
