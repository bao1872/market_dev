"""事件接收人服务 - 事件接收人扩展与通知创建。

核心功能：
1. expand_event_recipients(db, event_id):
   根据事件关联的 instrument_id 查询自选股用户，扩展事件接收人（幂等）
2. create_notification_from_event(db, event_id, user_id):
   为指定用户基于事件创建通知消息并写入 Outbox

设计要点：
- expand_event_recipients 使用 ON CONFLICT DO NOTHING 保证幂等
- 同一事件同一用户只接收一次（uq_event_recipients_event_user 唯一约束）
- watchlist_item_id 记录用户通过哪条自选股关联到该事件

用法：
    from app.services.event_recipient_service import expand_event_recipients
    count = await expand_event_recipients(db, event_id)

模块自测：
    python -m app.services.event_recipient_service
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.user_facing_labels import get_event_label
from app.models.event_recipient import StrategyEventRecipient
from app.models.instrument import Instrument
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_event import StrategyEvent
from app.models.watchlist import UserWatchlistItem

logger = logging.getLogger("event_recipient_service")

# [advice.md 第二节] - 事件类型文案已迁移至 app.constants.user_facing_labels
# 原本地 _EVENT_TYPE_LABEL dict 已删除，统一通过 get_event_label 查询，避免重复定义


async def expand_event_recipients(db: AsyncSession, event_id: UUID) -> int:
    """根据事件关联的 instrument_id 查询自选股用户，扩展事件接收人。

    查询 UserWatchlistItem where instrument_id 匹配 AND active=True，
    对每个用户 INSERT INTO strategy_event_recipients ON CONFLICT DO NOTHING。

    Args:
        db: 异步会话
        event_id: 策略事件 ID

    Returns:
        新创建的接收人数量（不含已存在的）

    Raises:
        ValueError: 事件不存在
    """
    # 1. 获取事件
    event = await db.get(StrategyEvent, event_id)
    if event is None:
        raise ValueError(f"事件不存在: event_id={event_id}")

    # 2. 查询持有该标的的活跃自选股用户
    stmt = (
        select(UserWatchlistItem.id, UserWatchlistItem.user_id)
        .where(
            UserWatchlistItem.instrument_id == event.instrument_id,
            UserWatchlistItem.active.is_(True),
        )
    )
    result = await db.execute(stmt)
    watchlist_rows = result.all()

    if not watchlist_rows:
        logger.debug(
            "事件无自选股用户: event_id=%s instrument_id=%s",
            event_id, event.instrument_id,
        )
        return 0

    # [eligible_user_service] - 批量过滤监控有资格用户（admin 也接收自己自选股的事件通知）
    from app.services.eligible_user_service import filter_monitor_eligible_recipients

    all_user_ids = [uid for _, uid in watchlist_rows]
    eligible_user_ids = set(await filter_monitor_eligible_recipients(db, all_user_ids))
    watchlist_rows = [
        (wid, uid) for wid, uid in watchlist_rows
        if uid in eligible_user_ids
    ]

    if not watchlist_rows:
        logger.info(
            "事件自选股用户均无资格，跳过接收人扩展: event_id=%s instrument_id=%s",
            event_id, event.instrument_id,
        )
        return 0

    # 3. 逐用户插入接收人（ON CONFLICT DO NOTHING）
    created_count = 0
    for watchlist_item_id, user_id in watchlist_rows:
        insert_stmt = text(
            """
            INSERT INTO strategy_event_recipients (event_id, user_id, watchlist_item_id)
            VALUES (:event_id, :user_id, :watchlist_item_id)
            ON CONFLICT (event_id, user_id) DO NOTHING
            """
        )
        insert_result = await db.execute(
            insert_stmt,
            {
                "event_id": str(event_id),
                "user_id": str(user_id),
                "watchlist_item_id": str(watchlist_item_id),
            },
        )
        if insert_result.rowcount > 0:
            created_count += 1

    await db.flush()

    logger.info(
        "事件接收人扩展完成: event_id=%s instrument_id=%s "
        "watchlist_users=%d created=%d",
        event_id, event.instrument_id, len(watchlist_rows), created_count,
    )
    return created_count


async def create_notification_from_event(
    db: AsyncSession,
    event_id: UUID,
    user_id: UUID,
) -> StrategyEventRecipient | None:
    """为指定用户基于事件创建通知消息并写入 Outbox。

    Args:
        db: 异步会话
        event_id: 策略事件 ID
        user_id: 用户 ID

    Returns:
        StrategyEventRecipient 记录，失败返回 None
    """
    from app.models.notification import NotificationChannel
    from app.schemas.notification import NotificationMessageDTO
    from app.services.notification_service import create_message, deliver_message
    from app.services.outbox_relay import write_outbox

    # 1. 获取事件和接收人记录
    event = await db.get(StrategyEvent, event_id)
    if event is None:
        logger.warning("事件不存在: event_id=%s", event_id)
        return None

    recipient_stmt = select(StrategyEventRecipient).where(
        StrategyEventRecipient.event_id == event_id,
        StrategyEventRecipient.user_id == user_id,
    )
    recipient_result = await db.execute(recipient_stmt)
    recipient = recipient_result.scalar_one_or_none()
    if recipient is None:
        logger.warning(
            "接收人记录不存在: event_id=%s user_id=%s", event_id, user_id,
        )
        return None

    # 2. 查询策略定义与标的名称，用于填充结构化字段
    strategy_key: str | None = None
    strategy_name: str | None = None
    version = await db.get(StrategyVersion, event.strategy_version_id)
    if version is not None:
        definition = await db.get(StrategyDefinition, version.strategy_definition_id)
        if definition is not None:
            strategy_key = definition.strategy_key
            strategy_name = definition.display_name or strategy_key

    instrument = await db.get(Instrument, event.instrument_id)
    instrument_symbol = instrument.symbol if instrument else ""
    instrument_name = instrument.name if instrument else ""

    # 3. 构建通知消息 DTO - [advice.md 第二节] 事件文案来自 user_facing_labels
    payload = event.payload or {}
    event_label = get_event_label(event.event_type)
    boundary = payload.get("boundary")
    boundary_text = f" · 边界 {boundary:.2f}" if isinstance(boundary, (int, float)) else ""
    event_summary = f"{event_label}{boundary_text}"

    dto = NotificationMessageDTO(
        message_type="MONITOR_EVENT",
        template_key="monitor_event",
        template_version="1.1.0",
        title=f"策略事件: {event_label}",
        summary=payload.get("summary", f"事件类型: {event.event_type}"),
        facts=[],
        timeline=[],
        items=[],
        resource_refs={
            "event_id": str(event_id),
            "event_type": event.event_type,
            "instrument_id": str(event.instrument_id),
            "instruments": [
                {
                    "instrument_id": str(event.instrument_id),
                    "symbol": instrument_symbol,
                    "name": instrument_name,
                },
            ],
        },
        data_time=event.event_time.isoformat() if event.event_time else "",
        # [消息中心] - 结构化字段
        strategy_key=strategy_key,
        strategy_name=strategy_name,
        instrument_count=1,
        primary_instrument={
            "instrument_id": str(event.instrument_id),
            "symbol": instrument_symbol,
            "name": instrument_name,
        },
        event_summary=event_summary,
    )

    # 3. 创建通知消息
    try:
        message = await create_message(
            db=db,
            user_id=user_id,
            message_dto=dto,
            source_type="strategy_event",
            source_id=event_id,
        )
    except Exception as exc:
        logger.warning(
            "创建通知消息失败: event_id=%s user_id=%s: %s",
            event_id, user_id, exc,
        )
        return recipient

    # 4. 写入 Outbox
    try:
        await write_outbox(
            db=db,
            event_type="notification.message.created",
            payload={
                "message_id": str(message.id),
                "event_id": str(event_id),
                "user_id": str(user_id),
            },
            aggregate_type="strategy_event",
            aggregate_id=event_id,
        )
    except Exception as exc:
        logger.warning(
            "写入 Outbox 失败: event_id=%s user_id=%s: %s",
            event_id, user_id, exc,
        )

    # 5. 查询用户活跃通知渠道并投递
    ch_stmt = select(NotificationChannel).where(
        NotificationChannel.user_id == user_id,
        NotificationChannel.status == "active",
    )
    ch_result = await db.execute(ch_stmt)
    channels = list(ch_result.scalars().all())

    for channel in channels:
        try:
            await deliver_message(
                db=db,
                message_id=message.id,
                channel_id=channel.id,
            )
        except Exception as exc:
            logger.warning(
                "投递通知失败: event_id=%s user_id=%s channel=%s: %s",
                event_id, user_id, channel.adapter_type, exc,
            )

    return recipient


if __name__ == "__main__":
    # 自测入口：验证函数可导入（不连接数据库）
    print(f"expand_event_recipients={expand_event_recipients}")
    print(f"create_notification_from_event={create_notification_from_event}")
    assert callable(expand_event_recipients)
    assert callable(create_notification_from_event)
    print("OK")
