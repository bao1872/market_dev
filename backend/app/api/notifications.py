"""通知 API 路由 - 消息与渠道管理。

端点：
- GET /messages: 用户消息列表（支持 unread_only 过滤）
- GET /messages/unread-count: 未读消息总数（角标专用，避免 list 接口 total 字段语义混淆）
- POST /messages/{id}/read: 标记消息已读
- POST /messages/read-all: 批量标记当前用户所有未读消息为已读
- POST /notification-channels: 创建通知渠道
- GET /notification-channels: 用户渠道列表
- PUT /notification-channels/{id}: 更新通知渠道配置
- DELETE /notification-channels/{id}: 删除通知渠道（软删除）
- POST /notification-channels/{id}/verify: 验证渠道
- POST /notification-channels/{id}/test: 测试渠道投递
- POST /notification-previews: 消息预览（渠道无关 DTO + 站内渲染 + 飞书 card JSON）

说明：
- 当前用户通过 JWT（Authorization: Bearer <token>）认证，由 get_current_active_user 注入
- 渠道配置 GET 返回脱敏后的 target_config（app_secret 仅显示末4位）
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.deps import get_current_active_user, get_db, require_roles
from app.models.user import User
from app.schemas.notification import (
    ChannelLatestEventTestResponse,
    ChannelTestResponse,
    CreateChannelRequest,
    NotificationChannelListResponse,
    NotificationChannelResponse,
    NotificationMessageListResponse,
    NotificationMessageResponse,
    NotificationPreviewRequest,
    NotificationPreviewResponse,
    UpdateChannelRequest,
    mask_target_config,
)
from app.services.channel_adapter import get_adapter
from app.services.feishu_card_builder import dto_to_feishu_card
from app.services.message_builder import MessageBuilderError, build_message
from app.services.notification_service import (
    ChannelNotFoundError,
    ChannelOwnershipError,
    DuplicateActiveChannelError,
    LatestEventNotFoundError,
    MessageNotFoundError,
    NotificationServiceError,
    count_unread_messages,
    create_channel,
    delete_channel,
    list_user_channels,
    list_user_messages,
    mark_all_messages_read,
    mark_message_read,
    test_channel,
    test_channel_latest_event,
    update_channel,
    verify_channel,
)

router = APIRouter(tags=["notifications"])


def _channel_response(channel: object) -> NotificationChannelResponse:
    """构建渠道响应，对 target_config 脱敏。"""
    resp = NotificationChannelResponse.model_validate(channel)
    resp.target_config = mask_target_config(resp.adapter_type, resp.target_config)
    return resp


@router.get("/messages", response_model=NotificationMessageListResponse)
async def get_messages(
    unread_only: bool = Query(False, description="仅返回未读消息"),
    limit: int = Query(50, ge=1, le=200, description="返回条数"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> NotificationMessageListResponse:
    """获取用户消息列表。"""
    messages = await list_user_messages(
        db, current_user.id, limit=limit, offset=offset, unread_only=unread_only
    )
    items = [NotificationMessageResponse.model_validate(m) for m in messages]
    return NotificationMessageListResponse(items=items, total=len(items))


class UnreadCountResponse(BaseModel):
    """未读消息计数响应（角标专用）。"""

    unread_count: int = Field(..., description="未读消息总数")


class ReadAllResponse(BaseModel):
    """批量已读响应。"""

    marked_count: int = Field(..., description="被标记为已读的消息数")


@router.get("/messages/unread-count", response_model=UnreadCountResponse)
async def get_unread_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> UnreadCountResponse:
    """获取当前用户未读消息总数（角标专用）。

    与 GET /messages 的 total 字段区分：list 接口 total 为当前页长度，
    本端点返回真实未读总数，供顶部角标展示。
    """
    count = await count_unread_messages(db, current_user.id)
    return UnreadCountResponse(unread_count=count)


@router.post("/messages/read-all", response_model=ReadAllResponse)
async def mark_all_read(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ReadAllResponse:
    """批量标记当前用户所有未读消息为已读。"""
    marked = await mark_all_messages_read(db, current_user.id)
    await db.commit()
    return ReadAllResponse(marked_count=marked)


@router.post("/messages/{message_id}/read", response_model=NotificationMessageResponse)
async def mark_read(
    message_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> NotificationMessageResponse:
    """标记消息已读。"""
    try:
        message = await mark_message_read(db, message_id, current_user.id)
    except MessageNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    await db.commit()
    return NotificationMessageResponse.model_validate(message)


@router.post(
    "/notification-channels",
    response_model=NotificationChannelResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_channel_endpoint(
    request: CreateChannelRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> NotificationChannelResponse:
    """创建通知渠道。"""
    # 校验 adapter_type 是否支持
    try:
        get_adapter(request.adapter_type)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e

    try:
        channel = await create_channel(
            db,
            user_id=current_user.id,
            adapter_type=request.adapter_type,
            display_name=request.display_name,
            target_config=request.target_config,
        )
    except DuplicateActiveChannelError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    await db.commit()
    return _channel_response(channel)


@router.put("/notification-channels/{channel_id}")
async def update_channel_endpoint(
    channel_id: UUID,
    request: UpdateChannelRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> NotificationChannelResponse:
    """更新通知渠道配置。"""
    try:
        channel = await update_channel(
            db,
            channel_id=channel_id,
            user_id=current_user.id,
            display_name=request.display_name,
            target_config=request.target_config,
        )
    except DuplicateActiveChannelError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    await db.commit()
    return _channel_response(channel)


@router.delete("/notification-channels/{channel_id}")
async def delete_channel_endpoint(
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> NotificationChannelResponse:
    """删除通知渠道（软删除）。"""
    try:
        channel = await delete_channel(
            db,
            channel_id=channel_id,
            user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    await db.commit()
    return _channel_response(channel)


@router.post(
    "/notification-channels/{channel_id}/verify",
    response_model=NotificationChannelResponse,
)
async def verify_channel_endpoint(
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> NotificationChannelResponse:
    """验证通知渠道配置。"""
    try:
        channel = await verify_channel(db, channel_id, user_id=current_user.id)
    except ChannelNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    except ChannelOwnershipError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(e)
        ) from e
    except DuplicateActiveChannelError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except NotificationServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)
        ) from e
    await db.commit()
    return _channel_response(channel)


@router.get(
    "/notification-channels",
    response_model=NotificationChannelListResponse,
)
async def list_channels(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> NotificationChannelListResponse:
    """获取用户通知渠道列表。"""
    channels = await list_user_channels(db, current_user.id)
    items = [_channel_response(ch) for ch in channels]
    return NotificationChannelListResponse(items=items, total=len(items))


@router.post(
    "/notification-channels/{channel_id}/test",
    response_model=ChannelTestResponse,
)
async def test_channel_endpoint(
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ChannelTestResponse:
    """测试渠道投递（发送测试消息到渠道）。

    与 verify 的区别：test 实际发送一条测试消息，验证完整投递链路。
    """
    try:
        channel, delivery_result = await test_channel(
            db, channel_id, user_id=current_user.id
        )
    except ChannelNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    except ChannelOwnershipError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(e)
        ) from e
    except DuplicateActiveChannelError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    await db.commit()
    return ChannelTestResponse(
        channel=_channel_response(channel),
        delivery=delivery_result,
    )


@router.post(
    "/notification-channels/{channel_id}/test-latest-event",
    response_model=ChannelLatestEventTestResponse,
)
async def test_channel_latest_event_endpoint(
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
) -> ChannelLatestEventTestResponse:
    """使用最新真实事件测试渠道图片投递链路（admin only）。

    流程：
    1. 查询渠道与用户
    2. 仅查询当前渠道用户 active watchlist 中股票的最新 StrategyEvent
    3. 无事件返回 409 Conflict
    4. 生成 test_run_id 避免双击重复
    5. 调用截图 Worker 获取图片本地静态 URL
    6. 创建图片消息并写入 Outbox
    7. Outbox Relay 扩张为 MessageDelivery(pending)，Delivery Worker 异步投递

    响应字段：
    - event_id: 事件 ID
    - symbol: 股票代码
    - message_id: 创建的通知消息 ID
    - delivery_status: 投递状态（pending）
    """
    settings = get_settings()
    try:
        channel, message, meta = await test_channel_latest_event(
            db=db,
            channel_id=channel_id,
            frontend_base_url=settings.frontend_base_url,
            capture_worker_url=settings.capture_worker_url,
            capture_token_ttl_seconds=settings.jwt_capture_ttl_seconds,
        )
    except ChannelNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    except LatestEventNotFoundError as e:
        # [test-latest-event] - 无事件返回 409 Conflict
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except NotificationServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)
        ) from e
    await db.commit()
    return ChannelLatestEventTestResponse(
        event_id=meta["event_id"],
        symbol=meta["symbol"],
        message_id=meta["message_id"],
        delivery_status=meta["delivery_status"],
    )


@router.post(
    "/notification-previews",
    response_model=NotificationPreviewResponse,
)
async def preview_message(
    request: NotificationPreviewRequest,
) -> NotificationPreviewResponse:
    """消息预览 - 返回渠道无关 DTO + 站内渲染 + 飞书 card JSON。

    网页预览与真实投递共享同一 DTO，确保内容一致。
    不落库、不投递，仅渲染预览。
    """
    try:
        dto = build_message(
            message_type=request.message_type,
            context=request.context,
            locale=request.locale,
        )
    except MessageBuilderError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e

    # 站内渲染模型（网页展示用，直接用 DTO 的 model_dump）
    in_app = dto.model_dump()

    # 飞书 card JSON（与真实投递共享同一渲染逻辑）
    feishu_card = dto_to_feishu_card(dto)

    return NotificationPreviewResponse(
        dto=dto,
        in_app=in_app,
        feishu_card=feishu_card,
    )


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert "/messages" in paths
    assert "/messages/unread-count" in paths
    assert "/messages/read-all" in paths
    assert any("/notification-channels" in p for p in paths)
    assert any("/notification-previews" in p for p in paths)
    assert any("/test" in p for p in paths)
    print("OK")
