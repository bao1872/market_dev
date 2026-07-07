# Worker & Job Map

## 1. Worker 服务

| Compose 服务 | WORKER_TYPE | 主要表 | 关键风险 |
|---|---|---|---|
| worker-bars-scheduler | bars_scheduler | bars*, strategy_runs, scheduler_job_runs | 行情覆盖不足影响 DSA；复用 `BarsCoverageService` 统一口径，业务日期使用 `shanghai_business_date()` |
| worker-strategy-scheduler | strategy_scheduler | strategy_runs | 重复创建 run |
| worker-calendar | calendar_scheduler | trading_calendar | 交易日错误导致调度错误 |
| worker-monitor | monitor_scheduler | watchlist, monitor_evaluations, strategy_events, outbox | 未完成 Bar 触发正式事件；链路为 `worker-monitor` → `monitor_batch_service.execute_monitor_cycle()` → `MarketDataAggregationService.get_bars(timeframe="1m", include_realtime=True)` → `pytdx_adapter.get_minute_bars`；通知时间使用 `format_shanghai_datetime` |
| worker-strategy-batch | strategy_batch | strategy_runs, strategy_results | 发布残缺结果；run 级总超时 7200s（STRATEGY_RUN_TOTAL_TIMEOUT_SECONDS 可配置），历史不足标的标记 skipped/insufficient_history |
| worker-outbox | outbox | outbox, message_deliveries | 资格过滤或渠道扩张错误 |
| worker-delivery | delivery | message_deliveries, notification_channels | 假成功、吞错误 |
| worker-after-close | after_close_orchestrator | scheduler_job_runs | 盘后链路断点恢复；覆盖率检查复用 `BarsCoverageService`；dsa-only 支持 fallback 到最新交易日 |
| worker-watchdog | watchdog | scheduler_job_runs, worker_heartbeats | 看门狗未运行导致僵尸残留 |
| worker-capture | capture service | capture_jobs, notification_messages | 截图失败但状态不可见 |

## 2. 任务状态

所有重要任务必须可从数据库回答：

```text
谁在跑
跑哪个 Git SHA
什么时候 heartbeat
业务日期是什么
run_key 是什么
成功/失败多少
失败原因是什么
是否可重试
```

## 3. Stale 处理

已有恢复逻辑会处理 stale scheduler_job_runs。生产审计发现：worker_heartbeats 中 status=running 但 heartbeat_at 过旧的记录曾不会自动清理（PR #4 的 `_recovery_watchdog_loop` 因 `WORKER_TYPE` 启动条件未匹配生产 worker 而从未运行）。已新增独立 `worker-watchdog` 生产服务（`WORKER_TYPE=watchdog`）让看门狗在生产运行：

```text
running + heartbeat_at < now - threshold → stopped/stale
```

不得删除历史记录，不得影响 fresh heartbeat。

## 3.1 Admin 可观察性入口

`GET /admin/worker-heartbeats`（admin 只读）返回 `worker_heartbeats` 表 raw 记录，附加后端计算的 `heartbeat_age_seconds` 和 `health_state`：
- fresh：running + age<120s（同 `system_overview_service.WORKER_HEALTH_WINDOW`）
- stale：running + 120s≤age<600s（同 `worker.STALE_HEARTBEAT_THRESHOLD_SECONDS`）
- stopped：status=stopped 或 age≥600s

AdminJobsPage "Worker 心跳" Tab 展示该数据，10 秒轮询。watchdog 服务在 `worker_heartbeats` 表中可见。

## 4. 修改 worker.py 原则

- 不做大拆分；
- 先补测试再移动代码；
- 每次只改一种 WORKER_TYPE 或一个横切能力；
- 保持 WORKER_TYPE、compose 服务名、调度时间、run_key、幂等逻辑不变。

## 5. `_notify_monitor_status` 直接发送路径

`worker.py:1087-1191` 的 `_notify_monitor_status` 用于监控启动/异常通知，**直接调用 `adapter.send()` 绕过 Outbox/Delivery Worker 管道**。代码 TODO 已标记，待产品决策是否保留作为降级路径（监控服务异常时 Outbox/Delivery Worker 可能也不可用）。该路径缺少重试、幂等（除启动通知 Redis 幂等）、静默时段规避，且无单元测试覆盖（ALIGN-025）。
