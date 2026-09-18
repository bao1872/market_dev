"""Monitor status notifier (startup / error Feishu notifications).

This module owns the Monitor Scheduler's notification logic, extracted from the
composition root (``app.worker``) in PANJI-GOV-W4B2.  It is a dedicated owner, separate
from the Monitor polling runtime (:mod:`app.services.monitor_scheduler_worker_runtime`),
which only *calls* this notifier and does not duplicate the notification logic.

Ownership:
* Redis 7-day startup idempotency (``monitor-start:{git_sha}``, SET NX EX) with an
  in-process fallback set.
* Admin-only channel filtering for startup; all active channels for errors.
* SYSTEM_ALERT DTO construction and per-channel Feishu delivery with isolation.

The runtime keeps ownership of the session factory and logger, so this notifier never
hardcodes a database entry point or logger.  All heavy dependencies (sqlalchemy, the
notification/user models, the Redis client, the channel adapter) stay as *function-local*
lazy imports so the worker startup dependency graph is unchanged.

This is a pure structural extraction (PANJI-GOV-W4B2); no Redis TTL, GIT_SHA fallback,
audience, DTO schema, template version, summary length, log level, retry, or adapter
behavior changed.  The Outbox/Delivery Worker migration note remains a TODO.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

# [monitor_scheduler] - 启动通知幂等降级缓存：Redis 不可用时使用进程内 set
_monitor_start_notified: set[str] = set()

SessionFactory = Callable[[], Any]


async def notify_monitor_status(
    title: str,
    content: str,
    *,
    is_error: bool = False,
    session_factory: SessionFactory,
    logger: logging.Logger,
) -> None:
    """发送监控状态通知（启动/异常）。

    启动通知（is_error=False）：
    - 幂等：基于 monitor-start:{git_sha} 键（Redis SET NX EX 7天，降级进程内 set）
      避免每次 Worker 重启都给管理员发重复启动通知
    - 仅发送给 admin 角色用户的渠道（运维事件不混淆为交易信号）

    异常通知（is_error=True）：
    - 发送给所有活跃飞书渠道（监控异常影响所有用户信号生成）
    - 不做幂等（每次异常都应通知）

    通知失败不影响主流程（仅记录警告）。
    使用 message_type="SYSTEM_ALERT" + template_key="system_alert" 构造通知。

    TODO: [monitor_scheduler] 当前直接调用 adapter.send() 绕过 Outbox 管道。
    应改为 create_message → write_outbox(notification.message.created) → Delivery Worker 投递，
    与业务通知保持一致的投递语义（重试、幂等、静默时段）。风险：监控服务自身异常时
    Outbox/Delivery Worker 可能也不可用，需评估是否保留直接发送作为降级路径。
    """
    try:
        from sqlalchemy import select

        from app.core.time import now_shanghai
        from app.models.notification import NotificationChannel
        from app.models.user import Role, UserRole
        from app.schemas.notification import NotificationMessageDTO
        from app.services.channel_adapter import get_adapter

        emoji = "❌" if is_error else "✅"

        # 启动通知幂等检查：monitor-start:{git_sha}（7 天 TTL）
        if not is_error:
            git_sha = os.environ.get("GIT_SHA", "unknown")
            idem_key = f"monitor-start:{git_sha}"
            try:
                from app.core.redis_client import get_redis

                redis = get_redis()
                acquired = await redis.set(idem_key, "1", nx=True, ex=7 * 86400)
                if not acquired:
                    logger.info(
                        "monitor startup notification already sent for %s", git_sha
                    )
                    return
            except Exception as e:
                logger.warning("Redis 幂等检查失败，降级为进程内幂等: %s", e)
                if git_sha in _monitor_start_notified:
                    logger.info(
                        "monitor startup notification already sent for %s (in-process)",
                        git_sha,
                    )
                    return
                _monitor_start_notified.add(git_sha)

        async with session_factory() as db:
            # 查询活跃的飞书平台应用渠道
            # 启动通知仅发送给 admin 角色用户；异常通知发送给所有用户
            stmt = select(NotificationChannel).where(
                NotificationChannel.adapter_type == "feishu_platform_app",
                NotificationChannel.status == "active",
            )
            if not is_error:
                admin_user_ids_subq = (
                    select(UserRole.user_id)
                    .join(Role, Role.id == UserRole.role_id)
                    .where(Role.name == "admin")
                )
                stmt = stmt.where(
                    NotificationChannel.user_id.in_(admin_user_ids_subq)
                )
            result = await db.execute(stmt)
            channels = list(result.scalars().all())

            if not channels:
                logger.debug("无活跃飞书渠道，跳过监控状态通知")
                return

            for channel in channels:
                try:
                    adapter = get_adapter(channel.adapter_type)
                    dto = NotificationMessageDTO(
                        title=f"{emoji} {title}",
                        message_type="SYSTEM_ALERT",
                        template_key="system_alert",
                        template_version="1.1.0",
                        summary=content[:200],
                        data_time=now_shanghai().isoformat(),
                        resource_refs={},
                    )
                    delivery = await adapter.send(dto, channel.target_config)
                    if delivery.success:
                        logger.info("监控状态通知已发送: %s -> user=%s", title, channel.user_id)
                    else:
                        logger.warning(
                            "监控状态通知发送失败: %s -> user=%s: %s",
                            title, channel.user_id, delivery.error_message,
                        )
                except Exception as e:
                    logger.warning("监控状态通知发送异常: user=%s: %s", channel.user_id, e)

    except Exception as e:
        logger.warning("监控状态通知整体失败: %s", e)


__all__ = ["notify_monitor_status"]
