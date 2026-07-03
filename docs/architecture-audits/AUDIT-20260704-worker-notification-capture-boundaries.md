# AUDIT-20260704: Worker / Notification / Outbox / Delivery / Capture 边界审计

> 审计日期：2026-07-04  
> 审计基线：main HEAD = `30ddc8a`（PR #8 合并后）  
> 审计类型：READ-ONLY 架构审计 + 后续小 PR 拆分计划  
> 审计约束：不改 backend/app 生产代码、frontend、schema、migration；不部署；不触发飞书；不清历史债务  
> 审计依据：`docs/maps/worker-job-map.md`、`docs/maps/notification-flow-map.md`、`docs/maps/backend-module-map.md`、`docs/maps/test-coverage-map.md`、`docs/current/03-jobs-integrations-operations.md`、`docs/current/code-doc-alignment.md`

---

## 1. worker.py 当前承担哪些职责

**文件**：`backend/app/worker.py`（1500 行）

单文件统一入口，按 `WORKER_TYPE` 环境变量分发到 9 类 worker 循环 + `all` 组合模式。

### 1.1 WORKER_TYPE 清单

| WORKER_TYPE | 主要 runner | 关键表 |
|---|---|---|
| `outbox` | `run_outbox_relay` | outbox |
| `delivery` | `run_delivery_worker` | message_deliveries |
| `strategy_batch` | `run_strategy_batch_worker` | strategy_runs |
| `bars_scheduler` | `run_bars_scheduler_worker` | bars*, scheduler_job_runs |
| `strategy_scheduler` | `run_strategy_scheduler_worker` | strategy_runs |
| `calendar_scheduler` | `run_calendar_scheduler_worker` | trading_calendar |
| `monitor_scheduler` | `run_monitor_scheduler_worker` | monitor_evaluations, strategy_events, outbox |
| `after_close_orchestrator` | `run_after_close_orchestrator_worker` | scheduler_job_runs |
| `watchdog` | `_recovery_watchdog_loop` | scheduler_job_runs, worker_heartbeats |
| `all` | 心跳 + watchdog 组合 | — |

### 1.2 关键 helper 函数

| 函数 | 行号 | 职责 |
|---|---|---|
| `_heartbeat_loop(worker_name, interval=60)` | L66 | INSERT/UPDATE `worker_heartbeats`，退出时标记 stopped |
| `_handle_shutdown(signum, _frame)` | L141 | 设置 `_shutdown` 标志 |
| `_create_job_run(...)` | L148 | 通过 `run_key` + `acquire_job_run_lock` 实现幂等 |
| `_finish_job_run(...)` | L213 | 更新 status/finished_at/lease_expires_at |
| `_update_job_heartbeat(...)` | L247 | 长任务 30s 心跳 |
| `_get_monitor_session(now_cst)` | L831 | 返回 (morning/afternoon, start, end) |
| `_find_or_create_monitor_session_job_run(...)` | L854 | session 级幂等 |
| `_notify_monitor_status(title, content, is_error=False)` | L1087 | **直接调用 `adapter.send()` 绕过 Outbox**（见第 10 节） |
| `mark_stale_worker_heartbeats(db, now, threshold_seconds=600)` | L1194 | UPDATE running→stopped RETURNING |
| `_recovery_watchdog_loop(interval_seconds=60)` | L1256 | 60s 间隔调用 `recover_stale_scheduler_job_runs` + `mark_stale_worker_heartbeats` |
| `_after_close_poll_once()` | L1287 | `FOR UPDATE SKIP LOCKED` 任务领取 |

---

## 2. 哪些职责应该保留在 worker.py

**全部保留**。依据 `docs/maps/worker-job-map.md:43-47` 明确规定：

> 修改 worker.py 原则：
> - 不做大拆分；
> - 先补测试再移动代码；
> - 每次只改一种 WORKER_TYPE 或一个横切能力；
> - 保持 WORKER_TYPE、compose 服务名、调度时间、run_key、幂等逻辑不变。

`docs/maps/backend-module-map.md:33-34` 同样将 `worker.py` 列为高风险热点，原则为"先补测试和 maps，再小步拆"。

**结论**：worker.py 1500 行是已知且被项目治理规则接受的状态。所有 WORKER_TYPE 循环、心跳、信号处理、job run 幂等、watchdog、盘后编排均保留在 worker.py。

---

## 3. 哪些职责应该拆到 service / orchestrator / runner

**不推荐拆分**。基于第 2 节的项目治理规则，任何结构拆分必须先更新 `docs/maps/worker-job-map.md` 第 4 节并走 CHANGE 流程。

唯一可考虑的**非结构化改进**（不是拆分）：
- 为 `_notify_monitor_status`（L1087-1191）补单元测试（不改生产代码，仅新增测试文件）

---

## 4. outbox / delivery / capture / feishu 当前调用链

### 4.1 自动监控通知

```text
MonitorEvaluation
→ StrategyEvent
→ EventRecipient
→ NotificationMessage
→ Outbox(notification.message.created)
→ outbox_relay._expand_notification_message_created (outbox_relay.py:103)
→ eligible_user_service.is_user_eligible 资格过滤
→ active NotificationChannel
→ MessageDelivery
→ delivery_worker.process_pending_deliveries (delivery_worker.py:157)
→ notification_service._execute_delivery
→ FeishuPlatformAppAdapter.send / send_image_bytes
```

无 `target_channel_id` 的自动通知必须走 eligible_user_service。

### 4.2 手动发送个股详情

```text
用户/管理员点击发送
→ stock_detail_feishu_service.send_stock_detail_to_feishu (stock_detail_feishu_service.py:98)
→ payload 带 target_channel_id
→ NotificationMessage + Outbox(delivery_type=card, target_channel_id)
→ outbox_relay 跳过 eligible_user_service (outbox_relay.py:162-171)
→ 只匹配指定 active channel
→ MessageDelivery
→ Delivery Worker
→ Capture worker HTTP POST → image_url
→ NotificationMessage + Outbox(delivery_type=image, target_channel_id)
```

### 4.3 管理员内测申请通知

```text
public beta application
→ beta_application_notifier
→ beta_application.admin_notification.created
→ outbox_relay._expand_beta_application_admin_notification (outbox_relay.py:234-372)
→ 查询 active admin users
→ 查询 admin 自己的 active feishu_platform_app channel
→ MessageDelivery
→ Delivery Worker
```

管理员通知不走普通 eligible_user_service，因为 admin 无 subscription。无渠道时 `feishu_delivery_status='failed'`，`feishu_last_error='ADMIN_PLATFORM_CHANNEL_NOT_CONFIGURED'`，不无限重试。

### 4.4 监控启动/异常通知（特殊路径）

```text
monitor_scheduler 启动/异常
→ _notify_monitor_status (worker.py:1087-1191)
→ 直接调用 adapter.send()  ← 绕过 Outbox/Delivery Worker
```

**注意**：此路径绕过标准 Outbox 管道，是代码 TODO 已标记的已知项（见第 10 节）。

---

## 5. card 与 image 的状态边界

### 5.1 MessageDelivery 独立字段

`models/notification.py:193` `MessageDelivery`：
- `status`：pending/sending/success/failed/retrying/dead（主状态）
- `delivery_type`：text/image/card
- `image_url`：图片 URL（image 类型专用）
- `image_upload_status`：图片上传独立状态
- `image_upload_error_code`：图片上传错误码
- `image_upload_provider_response`：图片上传响应
- `image_key`：飞书 image_key
- `message_group_id`：card + image 共享的组 ID（索引）
- `idempotency_key`：SHA256(message_id|channel_id|delivery_type|image_url) 唯一

### 5.2 状态聚合规则

`stock_detail_feishu_service.py:447-643` `get_share_status`：
- card_success + image_success = `success`
- card_success + image 非 success = `partial_failed`
- 任一 failed/dead = `failed`
- 否则 `pending`

### 5.3 仅重试图片

`stock_detail_feishu.py:216` `POST /stock-detail-feishu/{test_run_id}/retry-image`：
- 只重试 image delivery，不重复 card
- 通过 `message_group_id` 关联同组 card + image

---

## 6. capture failed、image failed、partial_failed 的边界

### 6.1 capture_jobs 状态

`models/capture_job.py`：
- `pending` → `running` → `succeeded` / `failed` → `dead`（超过 `CAPTURE_MAX_ATTEMPTS=3`）
- 失败字段：`error_code`、`error_message`、`image_url`、`message_group_id`

### 6.2 stock_detail_feishu_service 失败处理

`stock_detail_feishu_service.py:366-404`：
- 截图失败时写 CaptureJob failed 记录
- 错误码：`NO_IMAGE_URL` / `CAPTURE_REQUEST_FAILED` / `IMAGE_OUTBOX_FAILED`
- failed_step：`capture` / `image_outbox`
- 返回 `status: "pending" | "partial_failed"` + `failed_step` + `error_code` + `error_message`

### 6.3 边界定义

| 状态 | 含义 | 触发条件 |
|---|---|---|
| pending | 截图成功，Outbox 异步投递中 | capture succeeded，delivery 未完成 |
| partial_failed | 文字或图片至少一项成功、一项失败 | card_success + image_failed，或 capture_failed + card_sent |
| failed | 卡片/文字阶段失败，整组失败 | card delivery failed/dead |
| success | 文字和图片均成功 | card_success + image_success |

---

## 7. 哪些逻辑存在重复实现

### 7.1 feishu_platform_app_adapter.py 三方法 HTTP 状态处理重复（低优先级）

**位置**：`feishu_platform_app_adapter.py`
- `send`（L126）：卡片投递
- `send_image_bytes`（L276）：图片投递（两阶段：上传 + 发送）
- `send_text_message`（L557）：文本回退

三个方法重复实现 `RETRYABLE_STATUS` / `INVALID_STATUS` / 非 200 分支处理。

**评估**：
- 行为一致，无 bug
- 三个方法返回的 `DeliveryResult` 字段不同（图片方法额外返回 `image_upload_*`）
- 抽取共享 helper 前需补测试 + 更新 maps
- **优先级：低**（重复但无 bug）

### 7.2 无其他重复

- `feishu_card_builder.py` 与 `message_builder.py` 职责分离清晰（DTO 构建 vs card 转换）
- `outbox_relay.py` 三条通知流分支隔离干净
- `delivery_worker.py` 不直接调用 adapter，通过 `notification_service._execute_delivery` 间接调用

---

## 8. 哪些测试覆盖当前行为

| 测试文件 | 行数 | 覆盖内容 |
|---|---|---|
| `tests/test_notification.py` | 2443 | 消息构建器、卡片构建器、投递 worker、Mock 适配器、通知 API、Outbox 投递管道、状态机、渠道 active 唯一性、Capture Token、text+image message group |
| `tests/test_outbox_target_channel_id.py` | 283 | 5 场景：无/有 target_channel_id、只匹配指定渠道、非法 target_channel_id、无匹配渠道 |
| `tests/test_capture_token_isolation.py` | 325 | capture token 拒绝访问普通 API；access token 拒绝 capture；instrument_id 不匹配 403 |
| `tests/test_stock_detail_feishu.py` | 452 | 手动发送成功、无 active 渠道 404、未认证 401、始终附带 StockMemo、target_channel_id 单投递 |
| `tests/test_stock_detail_feishu_status.py` | 514 | capture 成功 card+image 共享 group；capture 失败 partial_failed；仅重试图片；用户只能查自己 |
| `tests/test_after_close_worker.py` | 535 | Worker 领取 queued、并发互斥、异常标记 failed、断点恢复、心跳+lease |
| `tests/test_worker_heartbeat_stale_cleanup.py` | 339 | fresh/stale running、非 running 不重复、批量处理、阈值边界、watchdog 调用、幂等 |
| `tests/test_recovery_watchdog.py` | 190 | watchdog 恢复 lease 过期任务、默认 60s 间隔、异常不退出 |
| `tests/test_capture_snapshot.py` | 333 | capture 成功返回快照、instrument_id 不匹配 403、无效 token 401、access token 拒绝 |
| `tests/test_worker_idempotency.py` | 162 | bars_scheduler 同 business_date 幂等、monitor session 复用、不同 date 互不影响 |

---

## 9. 哪些地方缺测试

### 9.1 `_notify_monitor_status` 直接发送路径无单元测试

**位置**：`worker.py:1087-1191`

**现状**：函数存在且被 `run_monitor_scheduler_worker` 调用，但无专门测试覆盖：
- 启动通知幂等（Redis SET NX EX 7天 + 进程内降级）
- 异常通知广播所有 active 渠道
- 失败不影响主流程

**建议**：补单元测试（mock Redis + adapter），验证上述 3 个场景。

### 9.2 生产 E2E 缺口（已登记）

`docs/maps/notification-flow-map.md:72-78` 列出 5 项仍需生产验证（ALIGN-010）：
- 文字+图片都成功的完整 E2E
- 截图失败形成 partial_failed
- 图片上传/发送失败后仅重试图片
- 重试不重复文字
- 用户只能查询自己的状态

这些已有单元/集成测试覆盖，但 `AGENTS.md` 第十二节第 10 条要求"Mock 不能替代真实生产 E2E"。

### 9.3 outbox_relay 非 notification 事件 Redis LPUSH 路径

**位置**：`outbox_relay.py` relay_outbox 主循环中"其他事件 → Redis LPUSH"分支

**现状**：测试主要覆盖 notification.message.created 和 beta_application.admin_notification.created 两个扩张分支，其他事件类型的 Redis 队列路径无明确测试。

**优先级**：低（当前只有这两类事件触发扩张）。

---

## 10. 哪些改动风险最高

### 10.1 `_notify_monitor_status` 绕过 Outbox 管道（最高风险）

**位置**：`worker.py:1087-1191`

**现状**：直接调用 `adapter.send(dto, channel.target_config)`（L1179），不经过 `create_message → write_outbox → delivery_worker`。

**影响**：
- 监控启动/异常通知缺少重试、幂等（除启动通知的 Redis 幂等）、静默时段规避
- 通知不可查询、不可重试

**代码已识别风险**：TODO 注释（`worker.py:1104-1107`）明确指出"监控服务自身异常时 Outbox/Delivery Worker 可能也不可用，需评估是否保留直接发送作为降级路径"。

**建议**：此为**已知 OPEN 项**，需产品决策（降级路径 vs 一致性）。不建议在未决策前修改。

### 10.2 任何 worker.py 结构改动（高风险）

依据 `worker-job-map.md:43-47` "不做大拆分"原则，任何结构拆分都需先更新 maps + CHANGE + 补测试。

---

## 11. 推荐拆成几个 PR

基于审计结果，**诚实推荐**（不盲目按候选清单）：

| PR | 标题 | 允许修改 | 禁止修改 | 优先级 |
|---|---|---|---|---|
| ~~PR-A~~ | ~~worker.py 拆出 recovery/watchdog/heartbeat runner~~ | **不推荐** — 违反 worker-job-map.md "不做大拆分" | — | — |
| PR-B | 补 `_notify_monitor_status` 单元测试 | `backend/tests/test_notify_monitor_status.py`（新增） | 不改 worker.py 生产代码 | P2 |
| PR-C | capture/image/card partial_failed 边界生产 E2E 验证 | 不改代码，仅生产验证 + 关闭 ALIGN-010 | 不改测试代码 | P1 |
| PR-D | Admin Jobs 可观察性补齐（ALIGN-012） | admin API + AdminJobsPage + 审计 | 不改 worker.py 主循环 | P1 |
| PR-E | Ruff 历史债务清理第一批 | 非生产代码 + ruff.json 基线 | 不改 backend/app 生产逻辑 | P2 |
| PR-F | Mypy 历史债务清理第一批 | 非生产代码 + mypy.json 基线 | 不改 backend/app 生产逻辑 | P2 |

**关键诚实声明**：审计发现架构整体清晰，不存在急需的大重构。推荐 PR 以补测试、生产验证、历史债务为主，不做结构拆分。

---

## 12. 每个 PR 的允许修改范围和禁止修改范围

### PR-A：worker.py 拆分（不推荐）

- **不推荐原因**：违反 `worker-job-map.md:43-47` "不做大拆分"
- **如未来确需拆分**：必须先更新 `docs/maps/worker-job-map.md` 第 4 节 + CHANGE + 补测试，再小步拆

### PR-B：补 `_notify_monitor_status` 单元测试

- **允许修改**：`backend/tests/test_notify_monitor_status.py`（新增）
- **禁止修改**：`backend/app/worker.py` 及任何生产代码
- **测试场景**：(1) 启动通知幂等 (2) 异常通知发所有 active 渠道 (3) 失败不影响主流程
- **依赖**：mock Redis + mock FeishuPlatformAppAdapter

### PR-C：capture/image partial_failed 生产 E2E 验证

- **允许修改**：无代码修改，仅生产验证 + 关闭 ALIGN-010
- **禁止修改**：任何代码、测试、schema
- **验证场景**：5 项（见第 9.2 节）
- **关闭条件**：生产证据 + CHANGE 记录

### PR-D：Admin Jobs 可观察性补齐

- **允许修改**：admin API + AdminJobsPage + 审计字段
- **禁止修改**：worker.py 主循环、调度时间、run_key、幂等逻辑
- **目标**：关闭 ALIGN-012

### PR-E：Ruff 历史债务清理第一批

- **允许修改**：非生产代码（tests/tools/docs）+ `tools/quality_baselines/ruff.json` 基线缩减
- **禁止修改**：`backend/app/` 生产逻辑
- **目标**：缩减 ALIGN-021 基线计数

### PR-F：Mypy 历史债务清理第一批

- **允许修改**：非生产代码 + `tools/quality_baselines/mypy.json` 基线缩减
- **禁止修改**：`backend/app/` 生产逻辑
- **目标**：缩减 ALIGN-021 基线计数

---

## 审计结论

### 整体评价

worker / notification / outbox / delivery / capture 边界**整体架构清晰、职责分离明确、关键路径测试覆盖充分**。三条通知流（自动监控、手动个股、管理员内测）在 `outbox_relay.py` 通过事件类型 + `target_channel_id` 分支干净隔离，符合 `AGENTS.md` 与 `notification-flow-map.md` 设计基线。

### 关键强约束已落实

- 飞书唯一接入方式为 `feishu_platform_app`（webhook 已永久删除）
- Capture Token 双向隔离
- 管理员通知复用管理员自己的 active `feishu_platform_app` NotificationChannel
- `target_channel_id` 用户主动触发通知跳过 `eligible_user_service`
- `partial_failed` 截图失败仍可发送卡片，图片可单独重试不重复文字

### 唯一已登记的架构债

`_notify_monitor_status`（`worker.py:1087-1191`）绕过 Outbox 管道是**代码 TODO 已明确标记、待产品决策**的项，不属于"未登记冲突"。生产 E2E 5 项已在 `notification-flow-map.md` 登记（ALIGN-010）。

### 不推荐的"改进"

基于 `worker-job-map.md:43-47` 和 `backend-module-map.md:33-34` 的项目治理原则，本次审计**明确不推荐**：
- 拆分 `worker.py`（1500 行，文档明确"不做大拆分"）
- 拆分 `notification_service.py`（1300 行，maps 未要求拆分）
- 大规模重构 `feishu_platform_app_adapter.py` 的 HTTP 状态处理（重复但无 bug）

任何此类改动必须先更新 `docs/maps/worker-job-map.md` 第 4 节或对应 map，并走 `AGENTS.md` 第七节 CHANGE 流程。

---

## 相关文件路径

**核心代码**：
- `backend/app/worker.py`（1500 行）
- `backend/app/services/outbox_relay.py`（490 行）
- `backend/app/services/delivery_worker.py`（404 行）
- `backend/app/services/stock_detail_feishu_service.py`（698 行）
- `backend/app/services/stock_capture_service.py`（241 行）
- `backend/app/services/notification_service.py`（1300 行）
- `backend/app/services/feishu_card_builder.py`（292 行）
- `backend/app/services/feishu_platform_app_adapter.py`（821 行）
- `backend/app/api/capture.py`（234 行）
- `backend/app/api/stock_detail_feishu.py`（282 行）

**模型**：
- `backend/app/models/notification.py`（302 行）
- `backend/app/models/outbox.py`（106 行）
- `backend/app/models/capture_job.py`（95 行）
- `backend/app/models/worker_heartbeat.py`（86 行）
- `backend/app/models/scheduler_job_run.py`（143 行）

**测试**：
- `backend/tests/test_notification.py`（2443 行）
- `backend/tests/test_outbox_target_channel_id.py`（283 行）
- `backend/tests/test_capture_token_isolation.py`（325 行）
- `backend/tests/test_stock_detail_feishu.py`（452 行）
- `backend/tests/test_stock_detail_feishu_status.py`（514 行）
- `backend/tests/test_after_close_worker.py`（535 行）
- `backend/tests/test_worker_heartbeat_stale_cleanup.py`（339 行）
- `backend/tests/test_recovery_watchdog.py`（190 行）
- `backend/tests/test_capture_snapshot.py`（333 行）
- `backend/tests/test_worker_idempotency.py`（162 行）
