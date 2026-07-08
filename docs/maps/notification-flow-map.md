# Notification / Outbox / Feishu Flow Map

## 1. 自动监控通知

自动监控通知分为**文字/卡片**和**图片**两段独立链路，通过 `message_group_id` 关联成同一事件的图文消息组。文字通知成功不代表图片通知一定成功。

### 1.1 文字/卡片链路

```text
MonitorEvaluation
→ StrategyEvent
→ EventRecipient
→ NotificationMessage (source_type=monitor_event / strategy_event)
→ Outbox(notification.message.created)
→ outbox_relay
→ eligible_user_service 资格过滤（`filter_monitor_eligible_recipients`/`is_user_eligible_for_monitor`，MONITOR_SOURCE_TYPES 真源见 `app/constants/monitor_source_types.py`）
→ active NotificationChannel
→ MessageDelivery(delivery_type=text/card)
→ delivery_worker
  → 对 monitor_event / strategy_event 再次调用 `is_user_eligible_for_monitor` 复核
→ FeishuPlatformAppAdapter 发送文字/卡片
```

### 1.2 图片链路

```text
worker-monitor
→ monitor_batch_service._send_chart_images_via_outbox()
  → 生成短期 capture token（必须含 scope=stock_detail_capture / user_id / instrument_id / event_id）
  → HTTP POST worker-capture /capture
  → capture_jobs (status=succeeded/failed)
→ NotificationMessage (source_type=monitor_chart)
→ Outbox(notification.message.created) payload:
     { delivery_type: "image", image_url: "...", message_group_id: "..." }
→ outbox_relay
→ eligible_user_service 资格过滤
→ active NotificationChannel
→ MessageDelivery(delivery_type=image)
→ delivery_worker
  → 对 monitor_chart 调用 `is_user_eligible_for_monitor` 复核
→ FeishuPlatformAppAdapter 上传/发送图片
```

- `message_group_id` 与文字/卡片链路共享，用于图文状态机关联；
- capture token 字段缺失会导致 worker-capture 返回 401/403，`image_url` 为空，不写 image Outbox，但**不阻塞文字通知**；
- 图片截图失败时整体消息组可能标记为 `partial_failed`。

无 `target_channel_id` 的自动通知必须走 eligible_user_service；delivery_worker 是 monitor source 的最后资格防线。

## 2. 手动发送个股详情

```text
用户/管理员点击发送
→ stock_detail_feishu_service
→ payload 带 target_channel_id
→ NotificationMessage + Outbox
→ outbox_relay 跳过 eligible_user_service
→ 只匹配指定 active channel
→ MessageDelivery
→ Delivery Worker
```

这是 PR #3 闭环的 ddca659 行为：手动指定渠道的通知不受订阅状态限制，自动通知仍过滤资格。

## 3. 管理员内测申请通知

```text
public beta application
→ beta_application_notifier
→ beta_application.admin_notification.created
→ outbox_relay 专用分支
→ 查询 active admin users
→ 查询 admin 自己的 active feishu_platform_app channel
→ MessageDelivery
```

管理员通知不走普通 eligible_user_service，因为 admin 无 subscription。

## 4. 图文状态机

消息体中的 `data_time` 与文本段触发时间统一格式化为 Asia/Shanghai（CST），由 `app.core.time.format_shanghai_datetime` 处理，避免 UTC/`+00:00` 展示。

| 状态 | 含义 |
|---|---|
| pending | 截图成功，Outbox 异步投递中 |
| partial_failed | 文字或图片至少一项成功、一项失败 |
| failed | 卡片/文字阶段失败，整组失败 |
| success | 文字和图片均成功 |

失败字段：

```text
failed_step
error_code
error_message
image_message_id
message_group_id
card_status
image_status
overall_status
```

## 5. 仍需生产验证

- 文字+图片都成功的完整 E2E；
- 截图失败形成 partial_failed；
- 图片上传/发送失败后仅重试图片；
- 重试不重复文字；
- 用户只能查询自己的状态。

## 6. 监控启动/异常通知（直接发送路径）

`worker.py:1087-1191` 的 `_notify_monitor_status` 用于监控启动/异常通知，**直接调用 `adapter.send()` 绕过 Outbox/Delivery Worker 管道**：

```text
monitor_scheduler 启动/异常
→ _notify_monitor_status (worker.py:1087)
→ Redis SET NX EX 7天 幂等（启动通知）
→ 直接调用 FeishuPlatformAppAdapter.send()
```

此路径不经 `create_message → write_outbox → delivery_worker`，缺少重试、静默时段规避、可查询状态。代码 TODO 已标记，待产品决策（ALIGN-025）。
