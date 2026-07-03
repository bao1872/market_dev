# Notification / Outbox / Feishu Flow Map

## 1. 自动监控通知

```text
MonitorEvaluation
→ StrategyEvent
→ EventRecipient
→ NotificationMessage
→ Outbox(notification.message.created)
→ outbox_relay
→ eligible_user_service 资格过滤
→ active NotificationChannel
→ MessageDelivery
→ delivery_worker
→ FeishuPlatformAppAdapter
```

无 `target_channel_id` 的自动通知必须走 eligible_user_service。

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
