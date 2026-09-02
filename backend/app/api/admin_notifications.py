"""管理员代管用户通知渠道 API（薄包装，不含第二套业务逻辑）。

背景
----
``NotificationChannel`` 本身就是 **per-user** 的数据 owner
（``user_id`` FK + ``target_config`` JSONB + ``status``），发送链路
（``notification_service`` → ``ChannelAdapter``）也早已消费用户级配置。

缺口只在"管理员代管"这一层：现有 ``/v1/notification-channels/*`` 全部以
``current_user.id`` 为作用域，管理员无法读写他人渠道，且
``update_channel`` / ``verify_channel`` / ``test_channel`` 内部有
``user_id`` 所有权校验（``ValueError`` / ``ChannelOwnershipError``）。

本模块只做一件事：把作用域从 ``current_user.id`` 换成管理员指定的
``target user_id``，**然后调用完全相同的 notification_service 函数**。

严格禁止：
- 绕过 service 层直接 CRUD ``NotificationChannel``
- 复制/改写 ``create/update/delete/verify/test`` 的业务与校验
- 自己实现脱敏（唯一 owner 是 ``mask_target_config``）
- 新增数据库表 / 修改 NotificationChannel model / 改动 sender、worker、adapter

业务约束（沿用现有 service contract，不重新设计）：
- 同一用户下最多一条 ``active`` 飞书渠道，违反时抛 ``DuplicateActiveChannelError``
  （见 ``notification_service._ensure_no_active_feishu_conflict``）
- ``adapter_type='feishu_webhook'`` 已被 service 显式拒绝
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.schemas.notification import (
    ChannelTestResponse,
    CreateChannelRequest,
    NotificationChannelListResponse,
    NotificationChannelResponse,
    UpdateChannelRequest,
    mask_target_config,
)
from app.services.notification_service import (
    ChannelNotFoundError,
    ChannelOwnershipError,
    DuplicateActiveChannelError,
    NotificationServiceError,
    create_channel,
    delete_channel,
    list_user_channels,
    test_channel,
    update_channel,
    verify_channel,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin-notifications"])


def _channel_response(channel: object) -> NotificationChannelResponse:
    """构建渠道响应，对 target_config 脱敏。

    脱敏的唯一 owner 是 ``mask_target_config``（schemas/notification.py），
    此处只调用、不重新实现。
    """
    resp = NotificationChannelResponse.model_validate(channel)
    resp.target_config = mask_target_config(resp.adapter_type, resp.target_config)
    return resp


@router.get(
    "/users/{user_id}/notification-channels",
    response_model=NotificationChannelListResponse,
)
async def admin_list_user_channels(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    _current_user=Depends(require_roles("admin")),
) -> NotificationChannelListResponse:
    """管理员查看指定用户的通知渠道列表（target_config 已脱敏）。"""
    channels = await list_user_channels(db, user_id)
    items = [_channel_response(ch) for ch in channels]
    return NotificationChannelListResponse(items=items, total=len(items))


@router.post(
    "/users/{user_id}/notification-channels",
    response_model=NotificationChannelResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_user_channel(
    user_id: UUID,
    request: CreateChannelRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> NotificationChannelResponse:
    """管理员为指定用户创建通知渠道（初始 status=pending，需 verify 后启用）。"""
    try:
        channel = await create_channel(
            db,
            user_id=user_id,
            adapter_type=request.adapter_type,
            display_name=request.display_name,
            target_config=request.target_config,
        )
    except DuplicateActiveChannelError as e:
        # 必须**先于** NotificationServiceError：DuplicateActiveChannelError 是其子类，
        # 若顺序颠倒会被 400 分支吞掉，导致"已存在 active 渠道"被误报为通用错误。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except NotificationServiceError as e:
        # 含 feishu_webhook 已废弃、adapter_type 不支持等
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e

    logger.info(
        "[admin-notifications] create channel user_id=%s channel_id=%s adapter=%s actor=%s",
        user_id, channel.id, request.adapter_type, getattr(current_user, "id", None),
    )
    await db.commit()
    return _channel_response(channel)


@router.put(
    "/users/{user_id}/notification-channels/{channel_id}",
    response_model=NotificationChannelResponse,
)
async def admin_update_user_channel(
    user_id: UUID,
    channel_id: UUID,
    request: UpdateChannelRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> NotificationChannelResponse:
    """管理员更新指定用户的通知渠道配置。

    ``update_channel`` 内部按 ``user_id`` 做所有权校验：渠道不属于该
    target user 时抛 ``ValueError``（映射为 404），不泄露渠道归属。
    """
    try:
        channel = await update_channel(
            db,
            channel_id=channel_id,
            user_id=user_id,
            display_name=request.display_name,
            target_config=request.target_config,
        )
    except DuplicateActiveChannelError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except ValueError as e:
        # 渠道不存在 / 不属于该 user / 已删除
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    logger.info(
        "[admin-notifications] update channel user_id=%s channel_id=%s actor=%s",
        user_id, channel_id, getattr(current_user, "id", None),
    )
    await db.commit()
    return _channel_response(channel)


@router.delete(
    "/users/{user_id}/notification-channels/{channel_id}",
    response_model=NotificationChannelResponse,
)
async def admin_delete_user_channel(
    user_id: UUID,
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> NotificationChannelResponse:
    """管理员删除指定用户的通知渠道（软删除：status=inactive）。"""
    try:
        channel = await delete_channel(db, channel_id=channel_id, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    logger.info(
        "[admin-notifications] delete channel user_id=%s channel_id=%s actor=%s",
        user_id, channel_id, getattr(current_user, "id", None),
    )
    await db.commit()
    return _channel_response(channel)


@router.post(
    "/users/{user_id}/notification-channels/{channel_id}/verify",
    response_model=NotificationChannelResponse,
)
async def admin_verify_user_channel(
    user_id: UUID,
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> NotificationChannelResponse:
    """管理员验证指定用户的通知渠道（成功→active，失败→invalid）。"""
    try:
        channel = await verify_channel(db, channel_id, user_id=user_id)
    except ChannelNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    except ChannelOwnershipError as e:
        # 渠道不属于该 target user：fail-closed，不回退到其它用户
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    except DuplicateActiveChannelError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except NotificationServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)
        ) from e

    logger.info(
        "[admin-notifications] verify channel user_id=%s channel_id=%s status=%s actor=%s",
        user_id, channel_id, channel.status, getattr(current_user, "id", None),
    )
    await db.commit()
    return _channel_response(channel)


@router.post(
    "/users/{user_id}/notification-channels/{channel_id}/test",
    response_model=ChannelTestResponse,
)
async def admin_test_user_channel(
    user_id: UUID,
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> ChannelTestResponse:
    """管理员对指定用户的通知渠道发送一条测试消息。"""
    try:
        channel, delivery_result = await test_channel(db, channel_id, user_id=user_id)
    except ChannelNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    except ChannelOwnershipError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    except DuplicateActiveChannelError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except NotificationServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)
        ) from e

    logger.info(
        "[admin-notifications] test channel user_id=%s channel_id=%s success=%s actor=%s",
        user_id, channel_id, delivery_result.success, getattr(current_user, "id", None),
    )
    await db.commit()
    return ChannelTestResponse(
        channel=_channel_response(channel),
        delivery=delivery_result,
    )
