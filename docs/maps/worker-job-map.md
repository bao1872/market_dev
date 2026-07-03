# Worker & Job Map

## 1. Worker 服务

| Compose 服务 | WORKER_TYPE | 主要表 | 关键风险 |
|---|---|---|---|
| worker-bars-scheduler | bars_scheduler | bars*, strategy_runs, scheduler_job_runs | 行情覆盖不足影响 DSA |
| worker-strategy-scheduler | strategy_scheduler | strategy_runs | 重复创建 run |
| worker-calendar | calendar_scheduler | trading_calendar | 交易日错误导致调度错误 |
| worker-monitor | monitor_scheduler | watchlist, monitor_evaluations, strategy_events, outbox | 未完成 Bar 触发正式事件 |
| worker-strategy-batch | strategy_batch | strategy_runs, strategy_results | 发布残缺结果 |
| worker-outbox | outbox | outbox, message_deliveries | 资格过滤或渠道扩张错误 |
| worker-delivery | delivery | message_deliveries, notification_channels | 假成功、吞错误 |
| worker-after-close | after_close_orchestrator | scheduler_job_runs | 盘后链路断点恢复 |
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

已有恢复逻辑会处理 stale scheduler_job_runs。生产审计新增发现：worker_heartbeats 中 status=running 但 heartbeat_at 过旧的记录不会自动清理。应作为独立修复：

```text
running + heartbeat_at < now - threshold → stopped/stale
```

不得删除历史记录，不得影响 fresh heartbeat。

## 4. 修改 worker.py 原则

- 不做大拆分；
- 先补测试再移动代码；
- 每次只改一种 WORKER_TYPE 或一个横切能力；
- 保持 WORKER_TYPE、compose 服务名、调度时间、run_key、幂等逻辑不变。
