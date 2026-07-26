# Worker、集成和运维

## Worker

```text
bars_scheduler
strategy_scheduler
calendar_scheduler
monitor_scheduler
strategy_batch
after_close_orchestrator
outbox
delivery
watchdog
capture
```

## 盘后

```text
refreshing_daily
→ factor
→ syncing_boards
→ computing_features
→ publishing
→ succeeded
```

## 飞书

Platform App only，文字和图片独立，支持 partial_failed 和 image-only retry。

## 运维

- `/version` 审计 SHA；
- Worker heartbeat；
- Docker logs；
- resource gate；
- 自动部署失败由 CN 排查；
- DB/Redis 不随应用自动重建。
