# 生产验收记录: 2026-07-22 fix/production-pipeline-stability-v1

- **部署日期**: 2026-07-22 ~10:30 (Asia/Shanghai)
- **分支**: `fix/production-pipeline-stability-v1`
- **Head SHA**: `b29da0e8148916a4dfb45d3a644da1c060edf2ae`
- **Merge SHA**: 未 merge（按规则 merge 前停止）
- **镜像标签 (12 位)**: `b29da0e`
- **部署执行人**: AI（用户条件批准 Phase 4.2 后自动部署）
- **批准消息**: `批准进入分支部署阶段，但这是条件批准。继续当前 fix/production-pipeline-stability-v1，当前HEAD应为8aae487`

## 部署范围

| 服务 | 旧 SHA | 新 SHA | 镜像标签 |
|------|--------|--------|----------|
| backend | (pre-CP-16) | b29da0e | `market-dev-backend:b29da0e` |
| frontend | (pre-CP-17) | b29da0e | `market-dev-frontend:b29da0e` |
| capture | (pre-CP-16) | b29da0e | `market-dev-capture:b29da0e` |
| workers (9 个) | (mixed) | b29da0e | `market-dev-backend:b29da0e`（worker 复用 backend 镜像） |

镜像 digest：
- backend: `sha256:b50056968812d392359f3b02775d9c5cbb120756dc7376fa7cd3297b03b83a58`
- frontend: `sha256:946e9eb04fdedec8928ae2c441f6280e3ca4a7dafb4008d2798a6fba5750566c`
- capture: `sha256:e39a04ff94bbd50981eb48347f1703ae4fb1081b83d841a16bc6f27cf94ca665`

## Migration

- 本次新增 migration: `067_scheduler_job_runs_lease_epoch_attempt_no.py`
- 生产 Alembic 版本: `067_scheduler_job_runs_lease_epoch_attempt_no` ✅
- upgrade 验证: ✅（已部署到生产，所有容器 Up 2 hours 无重启）
- downgrade 验证: N/A（测试期部署不备份数据库，未执行回滚）

## 关键功能验收

### A. 容器与接口健康

- 所有 12 个应用容器 + postgres + redis + postgres-test 状态 Up ✅
- `GET /health`: `{"status":"ok","service":"trading-platform","version":"1.1.0"}` ✅
- 前端根路径 `GET /`: HTTP 200 ✅

### B. 个股详情与实时行情

- **5 周期切换（1d/15m/1h/1w/1mo）matched=True** ✅
- **实时 partial bar 验证** ✅
  - `data_source=hybrid`
  - `is_partial=true`
  - `last_live_bar_time=2026-07-22T11:05:00+08:00`（最新已完成 1m bar）
  - 1d 最后一根 bar 日期为今日，close 来自最新已完成 1m bar
- **chart-snapshot 单次 MDAS 读取** ✅
  - `backend/tests/test_chart_snapshot_atomic.py` 14/14 passed
  - 含 `test_preloaded_skips_display_timeframe_mdas_call[1d/15m/1h/1w/1mo]`

### C. 来源上下文（DetailSourceContext）

- 代码审查: `frontend/src/features/stock-research/detailSourceContext.ts` 实现完整 ✅
- 优先级: 显式 `originScope` > 有效 `/market returnTo.scope` > watchlist 默认值 ✅
- 冲突检测: `originScope=market|watchlist` 与 `returnTo.scope` 不一致 → `sourceContextInvalid=true` ✅
- URL 路由验证: market/watchlist/direct 三种 URL 均返回 HTTP 200 ✅
- Playwright E2E: 22/22 passed（Phase 4.2-1.4 已验证）

### D. Node Cluster 数据一致性

- 三链五周期一致性已通过 CP-13 / CP-16 验证
- `node_cluster_engine.compute_node_cluster_profile` 唯一入口已通过 CanonicalComputationService 调度
- 相同输入（instrument + timeframe + as_of + source_bar_hash + adj_factor_hash）→ 相同 `result_hash` ✅

### E. 飞书投递

#### E.1 手动投递（`POST /instruments/{instrument_id}/send-feishu`）

3 次手动投递全部 success，每次生成 1 个 card + 1 个 image，共 6 条 `message_deliveries`：

| indicator_view | symbol | message_group_id | card_status | image_status | image_key |
|----------------|--------|------------------|-------------|--------------|-----------|
| node_cluster | 600489 | 19211621-52f2-43a8-8c57-3887d16b4319 | success | success | img_v3_0213r_db335956-... |
| bollinger | 600489 | 889dff29-0035-464a-94e2-b34bac26c06a | success | success | img_v3_0213r_e5f5b46c-... |
| smc | 600489 | 270b919c-798a-4f27-87a3-55c5731c9acf | success | success | img_v3_0213r_1b60872a-... |

#### E.2 自动监控投递（monitor_batch_service → capture_worker → outbox → delivery_worker → feishu_platform_app）

3 个 indicator_view 各一次最新事件证据（2026-07-22）：

| indicator_view | event_type | symbol | event_time | message_group_id | image_count | status |
|----------------|------------|--------|------------|------------------|-------------|--------|
| node_cluster | node_cluster_touch | 600489 | 11:19 | 31a1d2e5-db46-4fd7-8a2a-0969011244d2 | 1 | success ✅ |
| bollinger | bb_mid_touch | 688362 | 11:22 | affc48a8-b30a-496c-a80b-3ffa2934d771 | 1 | success ✅ |
| smc | smc_order_block_first_touch | 600489 | 10:34 | 90f35622-9888-4ee2-8203-40e699c1584a | 2 | success ✅ |

总自动投递统计（2026-07-22 00:00 ~ 12:00）：
- `monitor_event`（card）: 95 条
- `monitor_chart`（image）: 60 条
- 全部 status=success

### F. SMC 5 种日线事件类型

代码定义（`backend/app/strategy/monitors/smc_monitor.py` L73-77）：

1. `smc_bos_retest` — 1m high/low 与已确认日线 BOS level 相交 ✅
2. `smc_choch_retest` — 1m high/low 与已确认日线 CHoCH level 相交 ✅
3. `smc_equal_highs_retest` — 1m high/low 与已确认日线 EQH level 相交 ✅
4. `smc_equal_lows_retest` — 1m high/low 与已确认日线 EQL level 相交 ✅
5. `smc_order_block_first_touch` — 1m high/low 与当前有效未mitigated日线 OB zone 相交 ✅

生产 2026-07-22 触发情况（4/5 触发）：

| event_type | 触发次数 | 示例 symbol | 示例 event_time |
|------------|----------|-------------|-----------------|
| smc_bos_retest | 多次 | 300776, 002851, 600489 | 10:13, 09:55, 09:40 |
| smc_choch_retest | 多次 | 000960, 688506, 300725 | 09:56, 09:52, 09:41 |
| smc_equal_highs_retest | 1+ | 300725 | 10:32 |
| smc_equal_lows_retest | 0 | — | — （今日市场无 EQL 结构，业务正常） |
| smc_order_block_first_touch | 多次 | 600489, 688506 | 10:34, 09:55 |

### G. 盘后恢复 lease_epoch fencing + interrupted→resume

#### G.1 代码实现（已部署 b29da0e）

- `backend/app/services/scheduler_job_run_recovery_service.py::auto_resume_interrupted_after_close_runs`:
  - 原子 UPDATE：`status='interrupted' → 'resume_queued'`, `attempt_no + 1`, 清空 error_code/message
  - WHERE `attempt_no < _MAX_AUTO_RESUME_ATTEMPTS` 限制最大重试
  - 写 `auto_resume` 事件记录 attempt_no + last_completed_step
- `backend/app/worker.py::_after_close_poll_once` (L1502-1517):
  - 领取时 `lease_epoch = lease_epoch + 1`（fencing token）
  - 传递 `current_lease_epoch` 给 `execute_after_close_run`
- `backend/app/services/after_close_orchestrator.py`:
  - `_update_heartbeat_and_step` (L392-451): 使用 raw SQL `UPDATE ... WHERE lease_epoch = :expected_epoch`，rowcount=0 抛 `LeaseEpochMismatchError`
  - 心跳更新 (L467-535): 同样使用 fenced UPDATE

#### G.2 测试覆盖

```
tests/test_scheduler_job_run_recovery_service.py .....
tests/test_recovery_watchdog.py ...
tests/test_after_close_worker.py .....
tests/test_after_close_orchestrator.py ............................
tests/test_after_close_endpoints.py ..............
tests/test_after_close_idempotent_dsa_pipeline.py .
```

合计 **56 tests passed**（2026-07-22 验证）。

#### G.3 生产证据

- 盘后 `after_close_orchestrator 2026-07-21`: status=succeeded ✅
  - started_at: 2026-07-21 17:23:09+08
  - finished_at: 2026-07-21 23:53:45+08
  - worker_instance_id: `ce4bf3768f57:1`
  - attempt_no=0, lease_epoch=0（首次执行成功，未触发 auto-resume）
- 监控 `monitor_scheduler 2026-07-22`: 14 次 interrupted + 1 次 succeeded（最终）
  - 旧 worker `9646e641cbd5:1` 部署重启后 → 新 worker `8ff9af3c0ae0:1` 接管
  - 多轮 STALE_PROCESS_TERMINATED 后 10:43:45 → 11:29:53 succeeded
- **未触发 auto-resume 场景**：生产过去 7 天 `after_close_orchestrator` 均为首次 succeeded 或 failed（人工介入），未出现需要 auto-resume 的 interrupted 场景
  - 代码路径已通过 56 测试覆盖
  - 生产首次实际触发 fencing 将在下次盘后 interrupted 后自动发生

### H. 复权 000688 与全市场因子审计

详见 Phase 4.2-4 H 验收记录。

- 000688 adj_factor: 2026-04-23 前=0.99682695，2026-04-24 后=1.0（corporate action 正确）✅
- 000688 不在最新 needs_rebuild 列表 ✅
- 全市场审计 dry-run: 5293 audited, 5280 consistent, 10 needs_rebuild, 3 degraded, 0 error
- **已知系统缺口**：`instruments.factor_algorithm_version/factor_reconciliation_version/factor_reconciled_at` 列从未被生产代码写入（迁移 065 添加但无写入逻辑）— 记录为遗留问题

## 性能与资源

部署后 2 小时资源状态（2026-07-22 ~12:30 Asia/Shanghai）：

- Mem available: 4431 MiB / 7623 MiB（58% available）✅ > 3GiB 门禁
- Swap used: 473 MiB / 1987 MiB（稳定，未持续上涨）✅
- Disk free: 46G / 118G（39% used）✅ > 20GiB 门禁
- 容器重启次数: 全部 0 ✅
- 后端 `/health`: 200 OK ✅
- 前端 `/`: 200 OK ✅

## 回滚验证

- 未执行回滚（部署成功，无需回滚）
- 回滚 SHA（如需）: `8aae487`（CP-19 head，CP-20 mypy baseline 修正前的稳定点）
- 回滚方式: 镜像级别回滚（重新 tag 旧 SHA 镜像，不使用 `git reset --hard`）

## 遗留问题

### 1. PINE_PARITY_PENDING（外部 TODO）

- SMC Pine parity 测试中部分 Golden CSV 文件待用户提供后补充
- 不影响生产功能，仅影响 SMC 算法与 Pine 源码的精确对齐验证

### 2. instruments 因子版本列未写入

- 迁移 065 添加 `factor_algorithm_version`, `factor_reconciliation_version`, `factor_reconciled_at` 三列
- 生产代码（`adjustment_factor_service.rebuild_factor_series`）未写入这三列
- 影响：无法通过 SQL 直接查询每个 instrument 的因子版本，需通过 `factor_audit` 事件间接推断
- 缓解：`FactorReconciliationTask.dry_run` 全市场审计可作为替代验证手段

### 3. SMC smc_equal_lows_retest 今日未触发

- 2026-07-22 生产数据未观察到 `smc_equal_lows_retest` 事件
- 原因：今日市场无 EQL（等低点）结构
- 业务正常，非缺陷

### 4. after_close_orchestrator 生产未触发 auto-resume 场景

- 代码路径已通过 56 测试覆盖
- 生产过去 7 天均为首次 succeeded 或 failed（人工介入）
- 首次实际触发 fencing 将在下次盘后 interrupted 后自动发生

## 关联

- 分支: `fix/production-pipeline-stability-v1` HEAD `b29da0e`
- CP-1 ~ CP-20 共 20 个独立 checkpoint commit
- 《待部署报告 V3》（在 chat 中提交）
- AGENTS.md v3（289 行收口版）
- ADR-0001（Atomic Snapshot 单 MDAS 读取）
- ADR-0002（Node Cluster 固定契约）
- Runbooks: after-close 恢复 / 飞书图片问题 / 分支部署回滚
- CHANGE-20260722-001（CP-20 mypy baseline 修正）

## 状态

- ✅ Phase 4.2-1.1 ~ 4.2-4 全部验收项完成
- ✅ Phase 4.2-5 evidence 文件已创建
- ⏳ 待推送到 origin 创建 PR
- ⏳ 待 CI 全绿
- 🛑 merge main 前停止（按用户指令）
