# 竞价分析 Runbook

本 Runbook 描述竞价分析层（auction_anchor / auction_scan / auction_aggregation）的触发、恢复、Canary 和回滚操作。所有操作必须走正式 service / CLI / admin API，禁止裸 SQL、`/tmp` Python、`docker cp`。

## 前置条件

- 已通过 `scripts/ops/panji-prod-preflight` 校验生产服务器入口；
- 已读取目标 trade_date 的当前状态（`auction_anchor_snapshots` / `auction_anchor_publications` / `auction_scan_runs`）；
- 已确认盘后主编排（`after_close_orchestrator`）的 stock_core pointer 已发布；
- 本地 Mac 不启动 Worker；正式 Worker 在 `panji-prod` 服务器上运行。

## 1. 触发

### 1.1 自动触发（生产）

通过现有 `after_close_orchestrator` Worker 自动调度，**不新建容器**：

| 时间（Asia/Shanghai） | 任务 | run_key | 入口 |
|---|---|---|---|
| 盘后 | `auction_anchor` 生成+发布 | （由 orchestrator 触发） | `generate_and_publish_auction_anchors(db, trade_date, worker_id, lease_epoch)` |
| 09:25:05 | `auction_final:{date}` | `auction_final:{date}` | `create_auction_final_job` → `execute_auction_scan_run` |
| 10:00:00 | `auction_open_confirmation:{date}` | `auction_open_confirmation:{date}` | `create_auction_open_confirmation_job` → `execute_auction_open_confirmation_run` |

### 1.2 手动触发（admin API）

```bash
# 1. 锚点生成+发布（统一入口，事务内完成）
curl -X POST https://<api>/api/v1/admin/auction/anchors \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{"trade_date": "2026-07-31"}'

# 2. 竞价扫描+聚合
curl -X POST https://<api>/api/v1/admin/auction/scan \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{"trade_date": "2026-07-31", "auction_type": "final"}'
```

**禁止**：盘后只 generate 不 publish。所有锚点生成必须通过 `generate_and_publish_auction_anchors` 统一入口。

## 2. 恢复

### 2.1 Chip 软失败 → 锚点重建

**场景**：盘后 `chip_consensus` failed/timeout，auction_anchor 只生成 `structure_only` 锚点。chip 后来恢复成功后需要重建完整锚点。

**正式入口**：`after_close_chip_consensus_service.py` 在 chip worker 完成回调中自动触发 `generate_and_publish_auction_anchors`。

**行为**：
1. chip worker 完成后检查是否存在 `structure_only` 状态的 auction_anchor_snapshot
2. 若存在，重新调用 `generate_and_publish_auction_anchors(db, trade_date, worker_id, lease_epoch)`
3. 新 snapshot 生成后，`publish_auction_anchors` 通过 `on_conflict_do_update` 原子切换 publication 指针到新 snapshot
4. 旧 publication 保留审计，pointer 不倒退

**手动恢复**：
```bash
# 调用 admin API 重新生成+发布锚点（同 §1.2）
curl -X POST https://<api>/api/v1/admin/auction/anchors \
  -H "Authorization: Bearer <admin_token>" \
  -d '{"trade_date": "2026-07-31"}'
```

### 2.2 AuctionScanRun 幂等与 fencing 恢复

**场景**：`auction_scan_runs` 状态为 `running` 但租约已过期（worker 失联）。

**行为**（`_acquire_or_recover_scan_run`）：
1. 同 `date/type/version` 已 `succeeded` → 返回现有 run，幂等命中（`AuctionScanAlreadySucceededError`）
2. 同 `date/type/version` 状态 `running` 且租约有效 → 拒绝重复（`AuctionScanConflictError`）
3. 同 `date/type/version` 状态 `failed/partial` → 递增 `attempt_count`，创建新 run
4. 同 `date/type/version` 状态 `running` 但租约过期 → fencing 恢复，原子切换到新 run，旧 run 保留审计

**手动恢复**：
```bash
# 调用 admin API 重新扫描（同 §1.2）
curl -X POST https://<api>/api/v1/admin/auction/scan \
  -H "Authorization: Bearer <admin_token>" \
  -d '{"trade_date": "2026-07-31", "auction_type": "final"}'
```

### 2.3 锚点 publication 失败

**场景**：`generate_auction_anchors` 成功但 `publish_auction_anchors` 抛异常（版本不一致 / coverage=0 / snapshot 不存在）。

**行为**：
- `generate_and_publish_auction_anchors` 返回 `status=publish_failed`，不抛异常
- 已生成的 snapshot 保留审计，**不回滚**
- error_message 含失败原因（如 `publish_failed: AnchorVersionMismatchError: stale source run`）

**检查**：
```bash
# 查询当日 snapshot 状态和 publication 指针
curl https://<api>/api/v1/auction/anchors/2026-07-31 \
  -H "Authorization: Bearer <token>"
```

## 3. Canary（小批量验证）

### 3.1 前置条件

- 已有 dev/staging 环境（**禁止把 dev 直接部署生产"看效果"**）
- 应用 Migration 077/078
- 已确认 09:25 BarMinute 数据源覆盖率（不足时禁止扫描，建立 `auction_final_quotes` 表或接入现有行情 Provider 的最终竞价 DTO）

### 3.2 Canary 样本

| 维度 | 样本 |
|---|---|
| 股票 | 少量（5-10 只，含 1 个除权股票） |
| 行业 | 1 个 |
| 概念 | 1 个 |
| 覆盖场景 | 完整 chip / structure_only / 多个 OB / 无 09:25 / 除权 / 小样本 |

### 3.3 验证步骤

1. **锚点 publication**：`/auction/anchors/{trade_date}` 返回 `publication_id` 非空，`status=succeeded` 或 `structure_only`
2. **09:25 扫描**：`auction_scan_runs` 状态 `succeeded`，`coverage_ratio` > 0
3. **聚合**：`auction_scope_results` 含 market/industry/concept 三级
4. **10:00 生命周期**：`auction_event_trackings.lifecycle` 从 `formed` 转为 `confirmed/weakened/failed`
5. **/auction 三级页面**：`/auction`、`/auction/board/:boardId`、`/auction/stock/:symbol` 均可访问，DTO 含 `symbol` 和 `name`
6. **/review 回流**：`/review` 第6阶段 AuctionBackflowPanel 展示五维度数据

**禁止全量运行**：Canary 仅限小样本，验证通过后才可全量。

## 4. 回滚

### 4.1 锚点 publication 回滚

**场景**：当日锚点 publication 指向有问题的 snapshot，需要回退到前一日 publication。

**行为**：
- `auction_anchor_publications` 表的 `trade_date` 是唯一键
- 通过 `on_conflict_do_update` 原子切换 `snapshot_id`
- 旧 snapshot 保留审计，**不删除**

**正式入口**：调用 `generate_and_publish_auction_anchors` 重建（会创建新 snapshot 并原子切换 publication 指针）

### 4.2 Migration 回滚

**场景**：Migration 077/078 引入问题需要回滚。

**行为**：
- `alembic downgrade -1` 回滚到 076（`076_market_review_workbench`）
- 077 含 7 张表 `drop_table`，回滚会删除所有 auction 数据
- **回滚前必须备份**（如 `pg_dump --table=auction_*`）

### 4.3 前端回滚

**场景**：`/auction` 或 `/review` AuctionBackflowPanel 引入问题。

**行为**：
- 回滚到不含 `/auction` 入口的镜像
- 或在 `appNavigation.ts` 临时移除 `auction` 路由（保留后端 API 不影响其他页面）

## 5. 监控

### 5.1 关键指标

- `auction_anchor_snapshots.status` 分布（succeeded/structure_only/failed）
- `auction_anchor_publications` 当日是否存在
- `auction_scan_runs.status` 分布（succeeded/partial/failed）
- `auction_event_trackings.lifecycle` 分布
- 09:25 BarMinute 覆盖率

### 5.2 失败告警

- `auction_anchor_snapshots.status=failed` → 检查 stock_core pointer 是否发布
- `auction_scan_runs.status=failed` → 检查 09:25 数据源和锚点 publication
- `auction_anchor_snapshots.status=publish_failed` → 检查 source_core_run_id 与 stock_core pointer 一致性
