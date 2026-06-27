"""个股详情发送飞书 API - POST 创建 + GET 状态查询。

admin only，复用监控链路组件（MonitorSnapshotService / build_monitor_event_text /
capture worker / Outbox / MessageDelivery），走正式 Outbox 异步链路。

端点：
- POST /admin/instruments/{instrument_id}/send-feishu
    请求体: {"channel_id": "<uuid>"}
    响应: {"test_run_id", "message_group_id", "message_id", "image_message_id", "status"}
- GET /admin/stock-detail-feishu/{test_run_id}/status
    响应: {"test_run_id", "message_group_id", "text_status", "image_status",
           "overall_status", "failed_step", "error_code", "error_message"}
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.deps import get_db, require_roles
from app.models.user import User
from app.services.notification_service import (
    ChannelNotFoundError,
    NotificationServiceError,
)
from app.services.stock_detail_feishu_service import (
    InstrumentNotFoundError,
    StockDetailFeishuError,
    get_share_status,
    send_stock_detail_to_feishu,
)

router = APIRouter(prefix="/admin", tags=["stock-detail-feishu"])


def _error_detail(
    error_code: str,
    error_message: str,
    failed_step: str,
) -> dict[str, str]:
    """构造技术错误三字段响应体。

    [StockDetailFeishu] - 描述: HTTP 异常返回 {error_code, error_message, failed_step} 结构
    （advice.md 第十一节遗留清理：技术错误必须返回三字段，不继续只返回布尔值）
    """
    return {
        "error_code": error_code,
        "error_message": error_message,
        "failed_step": failed_step,
    }


class SendFeishuRequest(BaseModel):
    """发送飞书请求体。"""

    channel_id: uuid.UUID = Field(..., description="通知渠道 ID（必须属于当前 admin 用户）")


class SendFeishuResponse(BaseModel):
    """发送飞书响应 - 走 Outbox 异步链路，返回追踪 ID。

    - test_run_id: 本次分享唯一标识（用于状态查询）
    - message_group_id: 关联 text+image 两条投递的组 ID
    - message_id: 文本消息 ID（主消息）
    - image_message_id: 图片消息 ID（截图失败时为 None）
    - status: "pending"（Outbox 异步链路，创建后即为 pending，由 delivery_worker 异步投递）
    """

    test_run_id: str = Field(..., description="本次分享唯一标识（用于状态查询）")
    message_group_id: str = Field(..., description="消息组 ID（关联 text+image 投递）")
    message_id: str = Field(..., description="文本消息 ID")
    image_message_id: str | None = Field(None, description="图片消息 ID（截图失败时为 None）")
    status: str = Field(..., description="投递状态（pending，异步链路）")


class ShareStatusResponse(BaseModel):
    """分享状态查询响应。

    [StockDetailFeishu] - 描述: 按 delivery_type 分类返回 card_status/image_status
    （advice.md 第一节：delivery_type=card 走 adapter.send → msg_type=interactive）

    - card_status: 卡片投递状态（pending/sending/success/failed/retrying/dead/not_created）
    - image_status: 图片投递状态（同上，未创建截图时为 not_created）
    - overall_status: 汇总状态（pending/success/failed）
    - failed_step: 失败步骤（card/image，无失败时为 None）
    - error_code: 失败错误码（无失败时为 None）
    """

    test_run_id: str = Field(..., description="分享唯一标识")
    message_group_id: str | None = Field(None, description="消息组 ID")
    card_status: str = Field(..., description="卡片投递状态")
    image_status: str = Field(..., description="图片投递状态")
    overall_status: str = Field(..., description="汇总状态")
    failed_step: str | None = Field(None, description="失败步骤（card/image）")
    error_code: str | None = Field(None, description="失败错误码")
    error_message: str | None = Field(None, description="失败错误信息")


@router.post(
    "/instruments/{instrument_id}/send-feishu",
    response_model=SendFeishuResponse,
)
async def send_stock_detail_feishu_endpoint(
    instrument_id: uuid.UUID,
    request: SendFeishuRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
) -> SendFeishuResponse:
    """发送个股详情到飞书（admin only，走 Outbox 异步链路）。

    [StockDetailFeishu] - 描述: 复用监控链路组件，走 Outbox → MessageDelivery → delivery_worker

    复用链路（禁止另写一套截图和消息拼装逻辑）：
    1. MonitorSnapshotService.get_snapshot（快照 SSOT，返回非空快照）
    2. build_monitor_event_text（消息拼装，与 monitor 链同款）
    3. create_message（消息创建，notification_service 同款）
    4. write_outbox（事务性发件箱，outbox_relay 同款）
    5. capture worker HTTP（截图，test_channel_latest_event 同款）

    返回 test_run_id/message_group_id/message_id，客户端可通过
    GET /admin/stock-detail-feishu/{test_run_id}/status 查询投递状态。
    """
    settings = get_settings()
    try:
        result = await send_stock_detail_to_feishu(
            db=db,
            instrument_id=instrument_id,
            channel_id=request.channel_id,
            user_id=current_user.id,
            frontend_base_url=settings.frontend_base_url,
            capture_worker_url=settings.capture_worker_url,
            capture_token_ttl_seconds=settings.jwt_capture_ttl_seconds,
        )
    except InstrumentNotFoundError as e:
        # [StockDetailFeishu] - 个股不存在返回 404（三字段结构）
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_error_detail("INSTRUMENT_NOT_FOUND", str(e), "instrument"),
        ) from e
    except ChannelNotFoundError as e:
        # [StockDetailFeishu] - 渠道不存在或不属于当前用户返回 404（三字段结构）
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_error_detail("CHANNEL_NOT_FOUND", str(e), "channel"),
        ) from e
    except StockDetailFeishuError as e:
        # [StockDetailFeishu] - 快照/文本Outbox/截图/图片Outbox 失败返回 502（三字段结构）
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_error_detail(e.error_code, str(e), e.failed_step),
        ) from e
    except NotificationServiceError as e:
        # [StockDetailFeishu] - 其他通知服务错误返回 502（三字段结构）
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_error_detail("NOTIFICATION_SERVICE_ERROR", str(e), "unknown"),
        ) from e
    await db.commit()
    return SendFeishuResponse(**result)


@router.get(
    "/stock-detail-feishu/{test_run_id}/status",
    response_model=ShareStatusResponse,
)
async def get_share_status_endpoint(
    test_run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
) -> ShareStatusResponse:
    """查询个股飞书分享的投递状态（admin only）。

    [StockDetailFeishu] - 描述: 通过 test_run_id 查 MessageDelivery 投递状态

    返回 text_status / image_status / overall_status / failed_step / error_code。
    状态值：pending/sending/success/failed/retrying/dead/not_created。
    """
    try:
        result = await get_share_status(db=db, test_run_id=test_run_id)
    except NotificationServiceError as e:
        # [StockDetailFeishu] - test_run_id 无对应消息返回 404
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    return ShareStatusResponse(**result)


if __name__ == "__main__":
    # 自测入口：验证路由注册 + 三字段错误响应构造
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert any("/send-feishu" in p for p in paths), "应包含 /send-feishu 路由"
    assert any("/status" in p for p in paths), "应包含 /status 路由"
    assert router.prefix == "/admin", "prefix 应为 /admin"

    # 验证三字段错误响应构造（advice.md 第十一节遗留清理）
    detail = _error_detail("SNAPSHOT_FAILED", "快照获取失败", "snapshot")
    assert detail == {
        "error_code": "SNAPSHOT_FAILED",
        "error_message": "快照获取失败",
        "failed_step": "snapshot",
    }, f"三字段结构不匹配: {detail}"
    print(f"_error_detail={detail} OK")

    print("OK")
