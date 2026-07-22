# Runbook: 盘后任务失败恢复

- **触发条件**: after_close 编排任务失败 / `SchedulerJobRun` 状态为 `failed` / 用户报告飞书消息未发送
- **前置条件**: SSH 到生产服务器 / 拥有 docker compose 执行权限 / 已读 AGENTS §七.6 / §七.12
- **影响范围**: `after_close_orchestrator` 链路（raw 日线刷新 → factor 重建 → 覆盖率门禁/DSA → snapshot 发布 → 飞书投递）
- **预计恢复时间**: 15-60 分钟（取决于失败步骤）

## 症状识别

- `SchedulerJobRun.status = failed` 或 `partial_failed`
- `job_run_events` 表最新事件 `event_type = stage_failed`
- 飞书群未收到当日盘后推送
- `/admin/after-close` 页面显示红色阶段

## 排查步骤

1. **定位失败阶段**：

```bash
docker compose exec backend python -c "
from app.db.session import SessionLocal
from app.repositories.scheduler_repository import SchedulerJobRunRepository
db = SessionLocal()
repo = SchedulerJobRunRepository(db)
runs = repo.list_recent_runs(job_name='after_close', limit=3)
for r in runs:
    print(f'{r.id} | {r.trade_date} | {r.status} | started={r.started_at} | finished={r.finished_at}')
    for e in r.events:
        print(f'  {e.event_type} | stage={e.stage} | msg={e.message}')
"
```

**预期输出**: 显示最近 3 次 after_close 运行及其事件流。
**异常处理**: 如无任何 run 记录，说明 orchestrator 未触发，跳到「修复操作 1: 手动触发」。

2. **检查具体阶段日志**：

```bash
docker compose logs backend --since 2h | grep -E "after_close|orchestrator|stage" | tail -100
```

3. **检查 Redis 任务队列**：

```bash
docker compose exec redis redis-cli LLEN after_close_queue
docker compose exec redis redis-cli LRANGE after_close_queue 0 10
```

## 修复操作

### 操作 1: 手动触发 after_close（特定交易日）

⚠️ **破坏性**: 仅在确认当日盘后未运行或失败后执行；禁止重复触发同一交易日（会重复发送飞书消息）。

```bash
docker compose exec backend python -c "
from app.jobs.after_close_orchestrator import run_after_close
# 替换为目标交易日（YYYY-MM-DD）
run_after_close(trade_date='2026-07-21', mode='full')
"
```

**预期输出**: 各阶段成功日志（refreshing_daily → syncing_boards → waiting_dsa_worker → publishing_snapshots → sending_feishu）。
**异常处理**: 如某阶段失败，根据 `job_run_events` 定位具体错误，针对性修复后重跑该阶段。

### 操作 2: 仅重跑 DSA（factor 已成功，DSA 失败）

```bash
docker compose exec backend python -c "
from app.jobs.after_close_orchestrator import run_after_close
run_after_close(trade_date='2026-07-21', mode='dsa_only')
"
```

### 操作 3: 仅重发飞书消息（snapshot 已发布，飞书投递失败）

```bash
docker compose exec backend python -c "
from app.services.feishu_delivery_service import redeliver_after_close_messages
redeliver_after_close_messages(trade_date='2026-07-21')
"
```

## 验证

1. **检查 `SchedulerJobRun` 状态**：

```bash
docker compose exec backend python -c "
from app.db.session import SessionLocal
from app.repositories.scheduler_repository import SchedulerJobRunRepository
db = SessionLocal()
repo = SchedulerJobRunRepository(db)
r = repo.get_latest_run(job_name='after_close')
print(f'status={r.status} | finished={r.finished_at}')
assert r.status == 'success', f'未成功: {r.status}'
print('OK')
"
```

2. **检查飞书群**: 用户确认收到当日盘后推送消息。
3. **检查 `/admin/after-close` 页面**: 所有阶段绿色。

## 防止复发

- 新增 `after_close_stage_failure` 告警，`SchedulerJobRun.status=failed` 时自动通知管理员
- 每个阶段必须独立可重跑（`mode` 参数支持 `full` / `dsa_only` / `feishu_only`）
- `job_run_events` 必须完整记录每个阶段的开始/结束/失败事件，便于排查

## 关联

- CHANGE-20260717-002（MDAS SSOT 与盘后顺序门禁）
- AGENTS §七.6（飞书）+ §七.12（MDAS SSOT）+ §七.19（板块同步降级保护）
- ADR-0002（Node Cluster 输入契约隔离）
