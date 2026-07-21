# PRD V1.0 Phase 1 实施计划（待用户批准后执行）

**前提**：用户明确批准 Phase 0 审计通过 + 决策 main 上 16 个未提交改动的处理方式 + 创建新分支 `fix/production-pipeline-stability-v1`

**实施顺序**：A（飞书状态链）→ C（after-close resume）→ B（chart-snapshot）→ D（DetailEntryContext）→ E（SMC 五类）→ F（文档/记忆 V3）

**全程约束**（来自项目记忆）：
- 串行执行：`COMPOSE_PARALLEL_LIMIT=1`、`NODE_OPTIONS=--max-old-space-size=1536`、`PYTHONDONTWRITEBYTECODE=1`
- 资源门禁：MemAvailable<3GiB、根盘 free<20GiB 或 Swap 持续增加立即停止
- 禁止：并行全市场重算、volume prune、`docker system prune -a`、`git gc --aggressive`、删除生产卷、retag 旧镜像
- 禁止：边看边改、伪造真实回归结果、用 pre-existing 掩盖新增错误
- 部署顺序：backend → 共享 backend worker → frontend；不重建 Capture/Postgres/Redis
- 验收：飞书真实图片可见 + 重启恢复 + 最终 merge SHA 复验三者缺一不可

---

## Phase 1A：飞书状态链（F-01~F-04）

### A.1 数据库迁移

**文件**：`backend/alembic/versions/XXXX_add_message_group_overall_status.py`（新建）

**新增字段**（MessageGroup 表）：
```sql
ALTER TABLE message_groups ADD COLUMN overall_status VARCHAR(32);
ALTER TABLE message_groups ADD COLUMN png_sha256 CHAR(64);
ALTER TABLE message_groups ADD COLUMN image_width INTEGER;
ALTER TABLE message_groups ADD COLUMN image_height INTEGER;
ALTER TABLE message_groups ADD COLUMN build_sha CHAR(40);
ALTER TABLE message_groups ADD COLUMN freshness_seconds INTEGER;
```

**回滚**：downgrade 脚本删除上述字段。

### A.2 监控链路状态升级

**文件**：`backend/app/services/monitor_batch_service.py`

修改点：
1. **L1518 注释**："截图失败不阻塞通知流程" → "截图失败必须升级 MessageGroup.overall_status=failed"
2. **L1604-L1623**（capture 失败）：
   - 删除 `continue`
   - 写 image Outbox with `image_upload_status='failed'`、`error_code='CAPTURE_FAILED'`
   - 更新 `MessageGroup.overall_status='failed'`、`error_code='CAPTURE_FAILED'`
3. **L1626-L1644**（无 image_url）：同上处理
4. 新增：capture 成功后写 `MessageGroup.png_sha256` / `image_width` / `image_height` / `build_sha`

### A.3 MessageGroup 状态机

**文件**：`backend/app/models/notification.py`

新增方法：
```python
def compute_overall_status(self) -> str:
    """PRD 3.1 状态机：card+image success=success; card success+image failed=failed"""
    if self.card_status == 'success' and self.image_status == 'success':
        return 'success'
    if self.card_status == 'success' and self.image_status in ('failed', 'definitively_failed'):
        return 'failed'
    if self.card_status == 'success' and self.image_status in ('pending', 'retrying'):
        return 'pending'
    return 'failed'
```

### A.4 image Outbox 重试扩展

**文件**：`backend/app/services/notification_service.py`

`retry_image_delivery`（L983-L1026）扩展：
- 当前仅支持 manual 链路 → 扩展支持 monitor 链路（按 `source_type='monitor'` 筛选）
- 重试时重新触发 capture（若 png_path 不存在）
- 重试上限 3 次，超过标记 `image_status='definitively_failed'`

### A.5 测试

- 单测：`backend/tests/test_message_group_status.py`（新建）覆盖 19 字段状态机
- 集成测试：`backend/tests/test_monitor_batch_image_failure.py`（新建）注入 capture 失败，验证 MessageGroup.overall_status='failed'
- E2E（生产）：手动 Node/BB/SMC 各 1 次 + 自动盘中各 1 次（PRD §6 验收矩阵）

### A.6 风险

- MessageGroup 表已有数据需 backfill overall_status（按现有 card+image 状态计算）
- 监控链路改动可能影响现有通知流速 → 灰度 1 个 monitor 实例验证

---

## Phase 1C：After-close 自动 resume（J-01~J-03）

### C.1 数据库迁移

**文件**：`backend/alembic/versions/YYYY_add_resume_queued_and_lease_epoch.py`（新建）

```sql
-- 扩展 status 枚举（若是 varchar 则无枚举约束）
ALTER TABLE scheduler_job_runs ADD COLUMN lease_epoch INTEGER DEFAULT 0;
ALTER TABLE scheduler_job_runs ADD COLUMN worker_instance_id_stable VARCHAR(64);
-- metadata_json 保持 text，但新增 jsonb 索引列以便查询
ALTER TABLE scheduler_job_runs ADD COLUMN metadata_jsonb JSONB
  GENERATED ALWAYS AS (metadata_json::jsonb) STORED;
CREATE INDEX ix_scheduler_job_runs_metadata_gin ON scheduler_job_runs USING gin (metadata_jsonb);
```

**回滚**：downgrade 删除列与索引。

### C.2 状态机扩展

**文件**：`backend/app/services/after_close_orchestrator.py`

```python
class AfterCloseRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    RESUME_QUEUED = "resume_queued"  # 新增
```

### C.3 watchdog → resume_queued 转换

**文件**：`backend/app/services/scheduler_job_run_recovery_service.py`

新增逻辑：
- 定时扫描 `status='interrupted'` 且 `lease_expires_at < NOW() - INTERVAL '5 minutes'` 的任务
- 转换 `status='resume_queued'`、`lease_epoch += 1`
- 写入 `metadata_jsonb.resume_from_step = last_completed_step`

### C.4 after-close worker 领取扩展

**文件**：`backend/app/worker.py`

修改 after-close worker 领取逻辑：
- 当前：`WHERE status='queued'`
- 改为：`WHERE status IN ('queued', 'resume_queued') ORDER BY priority`
- 领取时检查 `lease_epoch`，若 `worker_instance_id != 当前实例` 且 `lease_expires_at > NOW()` 则跳过（fencing）

### C.5 execute_after_close_run 断点恢复

**文件**：`backend/app/services/after_close_orchestrator.py`

`execute_after_close_run`（L752-L1661）修改：
- 入口读取 `metadata_jsonb.last_completed_step`
- 跳过已完成的步骤（refreshing_daily / waiting_dsa_worker / feature_snapshot）
- 每完成一步更新 `last_completed_step` + 心跳

### C.6 lease_epoch fencing

**文件**：`backend/app/services/after_close_orchestrator.py`

每次写 DB 前检查：
```python
def _check_lease_epoch(self, run_id: str, expected_epoch: int) -> bool:
    current = db.execute("SELECT lease_epoch FROM scheduler_job_runs WHERE id=%s", run_id)
    return current == expected_epoch
```

不匹配则放弃当前 run（旧进程被新进程接管）。

### C.7 测试

- 单测：`test_after_close_resume.py`（新建）覆盖 RESUME_QUEUED 状态转换
- 集成测试：三阶段 SIGKILL（refreshing_daily / waiting_dsa / feature_snapshot）+ watchdog 识别 + 自动 resume + 最终 published
- E2E（生产）：触发一次 after-close + SIGKILL + 验证自动 resume 完成

### C.8 风险

- lease_epoch fencing 可能误杀合法 worker → 设置 60s grace period
- metadata_jsonb 生成列增加写入开销 → 监控写入延迟

---

## Phase 1B：原子 Chart Snapshot（D-01/D-02/R-01/R-02）

### B.1 新建 chart_snapshot API

**文件**：`backend/app/api/chart_snapshot.py`（新建）

```python
@router.get("/api/v1/instruments/{instrument_id}/chart-snapshot")
async def get_chart_snapshot(
    instrument_id: str,
    timeframe: str,
    bars_count: int = 250,
    include_realtime: bool = False,
    indicator_views: list[str] = Query(default=[]),
    user=Depends(get_current_user),
):
    """PRD 3.4: 一次 MDAS 获取 + 同源指标计算 + 拆分 completed/partial"""
```

### B.2 chart_snapshot_service

**文件**：`backend/app/services/chart_snapshot_service.py`（新建）

```python
class ChartSnapshotService:
    def build_snapshot(self, instrument_id, timeframe, bars_count, include_realtime, indicator_views):
        # 1. 单次 MDAS 调用获取 bars
        bars = MDAS.get_bars(instrument_id, timeframe, bars_count, include_realtime)
        # 2. 同源指标计算（共享同一 bars 引用）
        indicators = compute_all_indicators(bars, indicator_views)
        # 3. 拆分 completed / partial
        completed_bars = [b for b in bars if b.is_completed]
        partial_bar = next((b for b in bars if not b.is_completed), None)
        # 4. 计算 hash
        completed_hash = sha256(completed_bars)
        partial_revision = partial_bar.revision if partial_bar else 0
        return ChartSnapshot(
            bars=bars, indicators=indicators,
            completed_hash=completed_hash,
            partial_revision=partial_revision,
            freshness_seconds=30,
        )
```

### B.3 前端切换

**文件**：`frontend/src/features/stock-research/useStockResearchData.ts`

- **删除 L132** `mergeRealtimeQuoteIntoBars` 调用（PRD R-01）
- 改为：`const { data: snapshot } = useChartSnapshot(instrumentId, timeframe, indicatorViews)`
- `displayBars = snapshot.bars`、`indicators = snapshot.indicators`
- `displayFrame = { completed_hash: snapshot.completed_hash, partial_revision: snapshot.partial_revision }`

### B.4 quote overlay 保留

quote 仍独立请求，但仅用于顶部价格摘要 overlay，不修改 K 线数据。

### B.5 测试

- 单测：`test_chart_snapshot_service.py` 验证 completed_hash 稳定、partial_revision 递增
- 集成测试：交易时段连续 10 次 chart-snapshot 请求，验证 completed_hash 一致
- E2E：五周期（1d/15m/1h/1w/1mo）切换 + 30 秒后 K 线由 MDAS 更新

### B.6 风险

- chart-snapshot 单请求延迟可能高于双请求 → 性能压测
- 旧 bars/indicators API 保留作为 fallback（feature flag `USE_CHART_SNAPSHOT=true/false`）

---

## Phase 1D-F：业务合同 + 文档（待 A/B/C 完成后启动）

### D. DetailEntryContext（L-01~L-04）
- 新建 `frontend/src/features/stock-research/detailEntryContext.ts` 唯一 context 对象
- 新建 `frontend/src/features/stock-research/buildDetailEntry.ts` 唯一 builder
- 15 处 `/stock/` 入口改用 builder
- AST/grep 契约测试禁止其他文件手工拼接 `/stock/`

### E. SMC 五类事件（S-01~S-04）
- `smc_monitor.py` 新增 `smc_eqh_touch` / `smc_eql_touch` 事件
- BOS/CHoCH/OB 改用 `bar.low <= level <= bar.high`（影线触碰）
- `canonical_adapters.py` 暴露 EQH/EQL entity
- dry-run 1 周对比 close-only vs 影线触碰事件数

### F. 文档/记忆 V3（M-01~M-04）
- AGENTS.md 缩减到约 200 行（仅稳定护栏）
- `docs/contracts/` 新建 5 个 schema 文件
- `docs/decisions/` `docs/runbooks/` `docs/acceptance/` `docs/work/` `docs/evidence/` 新建
- `tools/check_docs_consistency.py` 增加路径级 MANIFEST 检查
- `contract-tests/` 提升到根目录，覆盖 10 项架构不变量

---

## 部署与验收（最终阶段）

### 部署顺序
1. backend 镜像构建（含 A/C/B 后端改动）
2. backend 容器滚动部署
3. 共享 backend worker 镜像部署（worker-bars-scheduler / worker-strategy-scheduler / worker-after-close / worker-monitor / worker-watchdog 等）
4. frontend 镜像构建（含 B/D 前端改动）
5. frontend 容器部署
6. **不重建** Capture / Postgres / Redis

### 验收清单（PRD §6 + L753）
- [ ] 数据库迁移成功（Alembic upgrade + downgrade 验证）
- [ ] backend 单测 + 集成测试全通过
- [ ] frontend typecheck + test + eslint 全通过
- [ ] 契约测试 10 项架构不变量全通过
- [ ] **生产飞书真实图片可见**（Node/BB/SMC 各 1 次手动 + 1 次盘中自动）
- [ ] **生产 after-close 重启恢复**（SIGKILL + 自动 resume + 最终 published）
- [ ] **最终 merge SHA 复验**（main HEAD = 部署镜像 SHA = 验收 SHA）
- [ ] git status clean
- [ ] 根盘 used 不高于起始基线

### PR 流程
1. 分支部署验收通过后 push
2. 开独立 PR（含完整 CHANGE-20260722-XXX.md 记录）
3. merge commit 合并 main
4. 切最新 main 再完整部署一次
5. 最终生产验收

---

## 待用户批准的具体问题

1. Phase 0 审计是否通过？允许进入 Phase 1？
2. main 上 16 个未提交改动如何处理？（commit 到 main / stash / cherry-pick 到新分支）
3. 新分支 `fix/production-pipeline-stability-v1` 何时创建？
4. Phase 1A/C/B 实施顺序是否同意？
5. 是否需要先手动触发 2026-07-21 09:10 那次 failed after-close 的 resume？（`resume_requested_at` 已等 6 小时）
