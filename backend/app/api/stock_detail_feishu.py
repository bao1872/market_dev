"""个股详情发送飞书 API - POST /admin/instruments/{id}/send-feishu。

admin only，复用监控链路组件（compute_all_indicators / build_monitor_event_text /
capture worker / ChannelAdapter），同步返回 4 个分步骤布尔结果，
用于定位"有文本、没图片"卡在哪一步。

端点：
- POST /admin/instruments/{instrument_id}/send-feishu
    请求体: {"channel_id": "<uuid>"}
    响应: {"text_ok": bool, "screenshot_ok": bool, "image_upload_ok": bool, "feishu_send_ok": bool}
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
    send_stock_detail_to_feishu,
)

router = APIRouter(prefix="/admin", tags=["stock-detail-feishu"])


class SendFeishuRequest(BaseModel):
    """发送飞书请求体。"""

    channel_id: uuid.UUID = Field(..., description="通知渠道 ID（必须属于当前 admin 用户）")


class SendFeishuResponse(BaseModel):
    """发送飞书响应 - 4 个分步骤布尔结果。

    用于定位"有文本、没图片"卡在哪一步：
    - text_ok=True, screenshot_ok=False: 截图服务故障
    - text_ok=True, screenshot_ok=True, image_upload_ok=False: 图片拉取失败
    - text_ok=True, screenshot_ok=True, image_upload_ok=True, feishu_send_ok=False: 飞书发送失败
    """

    text_ok: bool = Field(..., description="文本投递是否成功")
    screenshot_ok: bool = Field(..., description="截图是否成功（capture worker 返回 image_url）")
    image_upload_ok: bool = Field(..., description="图片拉取是否成功（_fetch_image_bytes 返回 bytes）")
    feishu_send_ok: bool = Field(..., description="飞书图片消息发送是否成功（adapter.send_image_bytes）")


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
    """发送个股详情到飞书（admin only）。

    [个股飞书] - 描述: 复用 test_channel_latest_event 链路组件，同步返回 4 布尔

    复用链路（禁止另写一套截图和消息拼装逻辑）：
    1. compute_all_indicators（指标计算，与 indicators API 同款）
    2. build_monitor_event_text（消息拼装，与 monitor 链同款）
    3. create_message（消息创建，notification_service 同款）
    4. capture worker HTTP（截图，test_channel_latest_event 同款）
    5. _fetch_image_bytes（图片拉取，notification_service 同款）
    6. adapter.send_text_message / send_image_bytes（ChannelAdapter 抽象）

    响应 4 布尔字段用于定位卡点，详见 SendFeishuResponse 文档。
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
        # [个股飞书] - 个股不存在返回 404
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    except ChannelNotFoundError as e:
        # [个股飞书] - 渠道不存在或不属于当前用户返回 404
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    except NotificationServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)
        ) from e
    await db.commit()
    return SendFeishuResponse(**result)


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert any("/send-feishu" in p for p in paths), "应包含 /send-feishu 路由"
    assert router.prefix == "/admin", "prefix 应为 /admin"
    print("OK")
