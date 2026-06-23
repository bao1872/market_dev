"""通知 API 路由 - 消息与渠道管理。

端点：
- GET /messages: 用户消息列表（支持 unread_only 过滤）
- POST /messages/{id}/read: 标记消息已读
- POST /notification-channels: 创建通知渠道
- GET /notification-channels: 用户渠道列表
- POST /notification-channels/{id}/verify: 验证渠道
- POST /notification-channels/{id}/test: 测试渠道投递
- POST /notification-previews: 消息预览（渠道无关 DTO + 站内渲染 + 飞书 card JSON）

说明：
- 当前用户 ID 通过 X-User-Id header 传入（占位，R2 阶段接入 JWT RBAC）
- 渠道配置 GET 返回脱敏后的 target_config（app_secret 仅显示末4位）
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.schemas.notification import (
    ChannelTestResponse,
    CreateChannelRequest,
    NotificationChannelListResponse,
    NotificationChannelResponse,
    NotificationMessageListResponse,
    NotificationMessageResponse,
    NotificationPreviewRequest,
    NotificationPreviewResponse,
    mask_target_config,
)
from app.services.channel_adapter import get_adapter
from app.services.feishu_card_builder import dto_to_feishu_card
from app.services.message_builder import MessageBuilderError, build_message
from app.services.notification_service import (
    ChannelNotFoundError,
    MessageNotFoundError,
    NotificationServiceError,
    create_channel,
    list_user_channels,
    list_user_messages,
    mark_message_read,
    test_channel,
    verify_channel,
)

router = APIRouter(tags=["notifications"])


def _get_user_id(x_user_id: str | None = Header(None)) -> UUID:
    """从 X-User-Id header 获取用户 ID（占位，R2 阶段接入 JWT）。

    Raises:
        HTTPException: 未提供 X-User-Id
    """
    if x_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 X-User-Id header",
        )
    try:
        return UUID(x_user_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"X-User-Id 格式非法: {e}",
        ) from e


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
    user_id: UUID = Depends(_get_user_id),
) -> NotificationMessageListResponse:
    """获取用户消息列表。"""
    messages = await list_user_messages(
        db, user_id, limit=limit, offset=offset, unread_only=unread_only
    )
    items = [NotificationMessageResponse.model_validate(m) for m in messages]
    return NotificationMessageListResponse(items=items, total=len(items))


@router.post("/messages/{message_id}/read", response_model=NotificationMessageResponse)
async def mark_read(
    message_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(_get_user_id),
) -> NotificationMessageResponse:
    """标记消息已读。"""
    try:
        message = await mark_message_read(db, message_id, user_id)
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
    user_id: UUID = Depends(_get_user_id),
) -> NotificationChannelResponse:
    """创建通知渠道。"""
    # 校验 adapter_type 是否支持
    try:
        get_adapter(request.adapter_type)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e

    channel = await create_channel(
        db,
        user_id=user_id,
        adapter_type=request.adapter_type,
        display_name=request.display_name,
        target_config=request.target_config,
        secret_ref=request.secret_ref,
    )
    await db.commit()
    return _channel_response(channel)


@router.post(
    "/notification-channels/{channel_id}/verify",
    response_model=NotificationChannelResponse,
)
async def verify_channel_endpoint(
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(_get_user_id),
) -> NotificationChannelResponse:
    """验证通知渠道配置。"""
    try:
        channel = await verify_channel(db, channel_id)
    except ChannelNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
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
    user_id: UUID = Depends(_get_user_id),
) -> NotificationChannelListResponse:
    """获取用户通知渠道列表。"""
    channels = await list_user_channels(db, user_id)
    items = [_channel_response(ch) for ch in channels]
    return NotificationChannelListResponse(items=items, total=len(items))


@router.post(
    "/notification-channels/{channel_id}/test",
    response_model=ChannelTestResponse,
)
async def test_channel_endpoint(
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(_get_user_id),
) -> ChannelTestResponse:
    """测试渠道投递（发送测试消息到渠道）。

    与 verify 的区别：test 实际发送一条测试消息，验证完整投递链路。
    """
    try:
        channel, delivery_result = await test_channel(db, channel_id)
    except ChannelNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    await db.commit()
    return ChannelTestResponse(
        channel=_channel_response(channel),
        delivery=delivery_result,
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
    assert any("/notification-channels" in p for p in paths)
    assert any("/notification-previews" in p for p in paths)
    assert any("/test" in p for p in paths)
    print("OK")
