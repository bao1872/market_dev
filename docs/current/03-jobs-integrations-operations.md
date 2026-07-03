# 03 后台任务、第三方集成与运维

## 1. Worker 类型

| Compose 服务 | WORKER_TYPE | 职责 |
|---|---|---|
| worker-bars-scheduler | `bars_scheduler` | 更新行情、聚合和触发盘后链路 |
| worker-strategy-scheduler | `strategy_scheduler` | DSA 兜底调度 |
| worker-calendar | `calendar_scheduler` | 更新交易日历 |
| worker-monitor | `monitor_scheduler` | 盘中自选股监控 |
| worker-strategy-batch | `strategy_batch` | 领取并执行 StrategyRun |
| worker-outbox | `outbox` | 扩张 Outbox 为 MessageDelivery |
| worker-delivery | `delivery` | 实际渠道投递、重试、最终状态 |
| worker-after-close | `after_close_orchestrator` | 盘后编排任务 |
| worker-watchdog | `watchdog` | 每 60s 清理 stale scheduler_job_runs 和僵尸 worker_heartbeats |
| worker-capture | capture service | 生成个股详情图片 |

统一 Worker 入口是 `backend/app/worker.py`。服务编排事实源是 `docker-compose.prod.yml`。

## 2. 调度语义

- 日历刷新：约 02:00 Asia/Shanghai；
- 盘后行情：交易日约 16:00；
- DSA 兜底：交易日约 18:30；
- 盘中监控：09:30–11:30、13:00–15:00 按配置轮询；
- Outbox/Delivery：短轮询；
- Worker 心跳：持续更新。

## 3. 任务状态与可观察性

重要任务必须记录：

```text
run_key
business_date
status
scheduled/started/finished
heartbeat
lease
instance
Git SHA
succeeded_count / failed_count
error_code / error_message
```

管理员和运维必须能回答：运行中的 Worker、Git SHA、心跳、next run、当前任务、股票计数、失败阶段、重试状态、发布完整性、文字状态、图片状态和数据新鲜度。

生产只读审计发现：`worker_heartbeats` 存在 stale/running 僵尸记录，导致 Worker 状态可信度不足。代码修复已由 PR #4 实现：`_recovery_watchdog_loop` 每 60 秒调用 `mark_stale_worker_heartbeats`，将 `status='running'` 且 `heartbeat_at` 超过 600 秒的记录标记为 `stopped`。但 PR #4 部署后该 loop 因 `WORKER_TYPE` 启动条件未匹配任何生产 worker 而从未运行；已新增独立 `worker-watchdog` 生产服务（`WORKER_TYPE=watchdog`）使其在生产运行，待部署后验证 stale running 清零（ALIGN-023）。

## 4. 飞书 Platform App

当前唯一飞书接入方式是 Platform App。

```text
Business Event / Manual Share
→ NotificationMessage
→ Outbox
→ Outbox Relay
→ MessageDelivery
→ Delivery Worker
→ FeishuPlatformAppAdapter
```

管理员内测申请通知走专用 `beta_application.admin_notification.created` Outbox 事件，查询 active admin 用户的 active `feishu_platform_app` 渠道，不走普通 eligible_user_service。

普通自动通知仍需要 active member + active subscription 过滤。手动指定 `target_channel_id` 的用户主动通知跳过资格过滤，但只能投递到指定 active channel。

## 5. Capture 与图文投递

Capture Worker 使用短期 Capture Token 访问 `/capture/stock/:symbol`。截图页面不经过普通 ProtectedLayout，不污染普通 Access Token。

文字和图片分开投递，状态分别记录。状态必须可查询，支持仅重试图片。

失败阶段包括：

```text
snapshot
capture
image_outbox
image_upload
image_delivery
card
text_outbox
```

## 6. 部署与健康检查

生产服务：postgres、redis、backend、frontend、多个 worker、capture worker。

部署顺序：

```text
确认 main + 工作区干净
→ 备份数据库
→ 构建 backend/frontend/capture
→ postgres/redis healthy
→ Alembic upgrade head
→ 启动 backend/frontend/workers
→ 验证版本、健康、心跳、任务、行情、发布和投递
```

`CORE_ONLY=1` 只用于受控恢复。需要完整业务能力时必须运行对应 worker：趋势选股需要 strategy_batch/scheduler，飞书图片需要 capture/outbox/delivery。

## 7. Secret 与日志

- Secret 不提交 Git；
- 文档不记录真实 Secret；
- 部署脚本不回显完整连接串或飞书密钥；
- 日志保留 service、git_sha、run_id、run_key、instrument、source_bar_time、error_code、request_id；
- 发现泄露先轮换，再处理历史。
