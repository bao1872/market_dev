"""个股详情发送飞书 API - POST 创建 + GET 状态查询。

普通用户可用，复用监控链路组件（MonitorSnapshotService / build_monitor_event_text /
capture worker / Outbox / MessageDelivery），走正式 Outbox 异步链路。

端点：
- POST /instruments/{instrument_id}/send-feishu
    请求体: {"indicator_view": "node_cluster" | "bollinger" | "smc"}（可选，默认 None 全字段）
    响应: {"test_run_id", "message_group_id", "message_id", "image_message_id", "status"}
- GET /stock-detail-feishu/{test_run_id}/status
    响应: {"test_run_id", "message_group_id", "card_status", "image_status",
           "overall_status", "failed_step", "error_code", "error_message"}
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants.indicator_view import INDICATOR_VIEW_VALUES
from app.core.deps import get_current_active_user, get_db
from app.models.user import User
from app.schemas.notification import MessageDeliveryResponse
from app.services.notification_service import (
    ChannelNotFoundError,
    NotificationServiceError,
    retry_image_delivery,
)
from app.services.stock_detail_feishu_service import (
    InstrumentNotFoundError,
    StockDetailFeishuError,
    get_share_status,
    send_stock_detail_to_feishu,
)

router = APIRouter(tags=["stock-detail-feishu"])


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


class SendFeishuResponse(BaseModel):
    """发送飞书响应 - 走 Outbox 异步链路，返回追踪 ID + 状态机上下文。

    [StockDetailFeishu] - 描述: 响应字段对齐 advice.md 第七节状态机 + CHANGE-20260718-006 Section 3
    - test_run_id: 本次分享唯一标识（用于状态查询）
    - message_group_id: 关联 text+image 两条投递的组 ID
    - message_id: 文本消息 ID（主消息）
    - image_message_id: 图片消息 ID（截图失败时为 None）
    - status: "pending"（截图成功，Outbox 异步投递中）| "failed"（截图或图片 Outbox 失败；
      请求要求图片但未成功，整体标记 failed 而非 partial_failed，触发显式重试与告警）
    - failed_step: 失败步骤（capture | image_outbox | None）
    - error_code: 错误码（NO_IMAGE_URL | CAPTURE_REQUEST_FAILED | IMAGE_OUTBOX_FAILED | None）
    - error_message: 错误详情（包含 worker 返回的响应体，最多 500 字符）
    """

    test_run_id: str = Field(..., description="本次分享唯一标识（用于状态查询）")
    message_group_id: str = Field(..., description="消息组 ID（关联 text+image 投递）")
    message_id: str = Field(..., description="文本消息 ID")
    image_message_id: str | None = Field(None, description="图片消息 ID（截图失败时为 None）")
    status: str = Field(..., description="投递状态（pending|failed）")
    failed_step: str | None = Field(None, description="失败步骤（capture|image_outbox|None）")
    error_code: str | None = Field(None, description="错误码（NO_IMAGE_URL|CAPTURE_REQUEST_FAILED|IMAGE_OUTBOX_FAILED|None）")
    error_message: str | None = Field(None, description="错误详情（最多 500 字符）")


class SendFeishuRequest(BaseModel):
    """发送飞书请求体 - 指标视图选择（PROMPT.md §四）。

    [CHANGE-20260720-003 §四] 详情页手动发送飞书时，用户从弹窗三单选项中选择指标视图：
    - node_cluster: 筹码共识价（默认）
    - bollinger: 布林带
    - smc: SMC 结构

    后端透传到：
    - build_monitor_event_text：文字卡片按 indicator_view 拆分字段
    - capture_payload：截图 URL 加 &indicator_view=... 切换图层组合
    - CaptureJob：记录 indicator_view 便于状态查询区分
    - 图片消息 resource_refs：携带 indicator_view 贯穿状态查询链路

    默认 None 时为全字段（向后兼容旧调用），但前端弹窗始终显式选择一个值。
    """

    indicator_view: str | None = Field(
        default=None,
        description=(
            "指标视图：node_cluster（筹码共识价）| bollinger（布林带）| smc（SMC 结构）；"
            "None 表示全字段（向后兼容）"
        ),
    )

    def normalized_indicator_view(self) -> str | None:
        """校验并归一化 indicator_view，非法值返回 None。"""
        if self.indicator_view is None:
            return None
        if self.indicator_view in INDICATOR_VIEW_VALUES:
            return self.indicator_view
        return None


class ShareStatusResponse(BaseModel):
    """分享状态查询响应。

    [StockDetailFeishu] - 描述: 按 delivery_type 分类返回 card/image 状态，并补充 capture/image_upload 状态
    （advice.md 第一节：delivery_type=card 走 adapter.send → msg_type=interactive）

    - card_status: 卡片投递状态（pending/sending/success/failed/retrying/dead/not_created）
    - capture_status: 截图任务状态（pending/success/failed）
    - image_upload_status: 图片上传状态（pending/success/failed/not_created）
    - image_status: 图片投递状态（同上，未创建截图时为 not_created）
    - overall_status: 汇总状态（pending/success/failed）
      * success: 卡片+图片均成功
      * failed: 卡片成功但图片确定性失败（capture/image_upload/image_delivery 失败），
        或任意投递 failed/dead
      * pending: 图片仍在进行中（pending/sending/retrying，Outbox 尚未 relay 或投递未完成）
      [CHANGE-20260718-006 Section 3] 不再使用 partial_failed：请求要求图片但未成功时
      整体标记 failed，触发显式重试与告警
    - failed_step: 失败步骤（capture/image_upload/image_delivery/card/image，无失败时为 None）
    - error_code: 失败错误码（无失败时为 None）
    - image_message_id: 图片消息 ID（未创建时为 None）
    """

    test_run_id: str = Field(..., description="分享唯一标识")
    message_group_id: str | None = Field(None, description="消息组 ID")
    card_status: str = Field(..., description="卡片投递状态")
    capture_status: str = Field(..., description="截图任务状态")
    image_upload_status: str = Field(..., description="图片上传状态")
    image_status: str = Field(..., description="图片投递状态")
    overall_status: str = Field(..., description="汇总状态")
    failed_step: str | None = Field(None, description="失败步骤")
    error_code: str | None = Field(None, description="失败错误码")
    error_message: str | None = Field(None, description="失败错误信息")
    image_message_id: str | None = Field(None, description="图片消息 ID")


class RetryImageResponse(BaseModel):
    """图片单独重试响应。

    [StockDetailFeishu] - 描述: 仅重试图片投递，不重复发送文字
    """

    retried_count: int = Field(..., description="重试的图片投递数量")
    image_message_id: str | None = Field(None, description="图片消息 ID")
    deliveries: list[MessageDeliveryResponse] = Field(
        default_factory=list, description="被重试的投递记录",
    )


@router.post(
    "/instruments/{instrument_id}/send-feishu",
    response_model=SendFeishuResponse,
)
async def send_stock_detail_feishu_endpoint(
    instrument_id: uuid.UUID,
    payload: SendFeishuRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SendFeishuResponse:
    """发送个股详情到飞书（普通用户可用，走 Outbox 异步链路）。

    [StockDetailFeishu] - 描述: 复用监控链路组件，走 Outbox → MessageDelivery → delivery_worker

    复用链路（禁止另写一套截图和消息拼装逻辑）：
    1. MonitorSnapshotService.get_snapshot（快照 SSOT，返回非空快照）
    2. build_monitor_event_text（消息拼装，与 monitor 链同款）
    3. create_message（消息创建，notification_service 同款）
    4. write_outbox（事务性发件箱，outbox_relay 同款）
    5. capture worker HTTP（截图，test_channel_latest_event 同款）

    [CHANGE-20260720-003 §四] 请求体携带 indicator_view：
    - None: 全字段文案 + 默认截图图层（向后兼容）
    - "node_cluster" | "bollinger" | "smc": 文字卡片只展示该指标对应字段，
      截图 URL 加 &indicator_view=... 切换图层组合，缓存键加 iv=... 维度

    后端自动查找当前用户唯一 active Feishu 渠道；无渠道时返回 404。
    返回 test_run_id/message_group_id/message_id，客户端可通过
    GET /stock-detail-feishu/{test_run_id}/status 查询投递状态。
    """
    settings = get_settings()
    # [CHANGE-20260720-003 §四] 归一化 indicator_view，非法值降级为 None（向后兼容）
    indicator_view: str | None = None
    if payload is not None:
        indicator_view = payload.normalized_indicator_view()

    try:
        result = await send_stock_detail_to_feishu(
            db=db,
            instrument_id=instrument_id,
            user_id=current_user.id,
            frontend_base_url=settings.frontend_base_url,
            capture_worker_url=settings.capture_worker_url,
            capture_token_ttl_seconds=settings.jwt_capture_ttl_seconds,
            indicator_view=indicator_view,
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
    current_user: User = Depends(get_current_active_user),
) -> ShareStatusResponse:
    """查询个股飞书分享的投递状态（当前用户只能查自己的分享）。

    [StockDetailFeishu] - 描述: 通过 test_run_id 查 MessageDelivery 投递状态

    返回 card_status / image_status / overall_status / failed_step / error_code。
    状态值：pending/sending/success/failed/retrying/dead/not_created。
    """
    try:
        result = await get_share_status(
            db=db, test_run_id=test_run_id, user_id=current_user.id
        )
    except NotificationServiceError as e:
        # [StockDetailFeishu] - test_run_id 无对应消息返回 404
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    return ShareStatusResponse(**result)


@router.post(
    "/stock-detail-feishu/{test_run_id}/retry-image",
    response_model=RetryImageResponse,
)
async def retry_image_endpoint(
    test_run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> RetryImageResponse:
    """仅重试图片投递（不重复发送文字）。

    [StockDetailFeishu] - 描述: 按 message_group_id 找到失败的图片 Delivery，
    调用 retry_delivery 复用现有记录；卡片段不会被重发。
    """
    try:
        share_status = await get_share_status(
            db=db, test_run_id=test_run_id, user_id=current_user.id
        )
    except NotificationServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e

    message_group_id = share_status.get("message_group_id")
    if not message_group_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="消息组 ID 不存在，无法重试",
        )

    try:
        retried = await retry_image_delivery(
            db=db, message_group_id=message_group_id, user_id=current_user.id
        )
    except NotificationServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)
        ) from e

    await db.commit()
    return RetryImageResponse(
        retried_count=len(retried),
        image_message_id=share_status.get("image_message_id"),
        deliveries=[MessageDeliveryResponse.model_validate(d) for d in retried],
    )


if __name__ == "__main__":
    # 自测入口：验证路由注册 + 三字段错误响应构造
    # [StockDetailFeishu] - 描述: router.routes 为 BaseRoute 列表，用 getattr 安全取 path
    paths = [p for p in (getattr(r, "path", None) for r in router.routes) if p]
    print(f"router.routes={paths}")
    assert any("/send-feishu" in p for p in paths), "应包含 /send-feishu 路由"
    assert any("/status" in p for p in paths), "应包含 /status 路由"
    assert any("/retry-image" in p for p in paths), "应包含 /retry-image 路由"
    assert router.prefix == "", "prefix 应为空（由主应用直接挂载）"

    # 验证三字段错误响应构造（advice.md 第十一节遗留清理）
    detail = _error_detail("SNAPSHOT_FAILED", "快照获取失败", "snapshot")
    assert detail == {
        "error_code": "SNAPSHOT_FAILED",
        "error_message": "快照获取失败",
        "failed_step": "snapshot",
    }, f"三字段结构不匹配: {detail}"
    print(f"_error_detail={detail} OK")

    # [CHANGE-20260720-003 §四] 验证 SendFeishuRequest body schema
    req_none = SendFeishuRequest()
    assert req_none.indicator_view is None, "默认应为 None"
    assert req_none.normalized_indicator_view() is None, "None 归一化为 None"
    req_nc = SendFeishuRequest(indicator_view="node_cluster")
    assert req_nc.normalized_indicator_view() == "node_cluster"
    req_bb = SendFeishuRequest(indicator_view="bollinger")
    assert req_bb.normalized_indicator_view() == "bollinger"
    req_smc = SendFeishuRequest(indicator_view="smc")
    assert req_smc.normalized_indicator_view() == "smc"
    # 非法值降级为 None（向后兼容）
    req_invalid = SendFeishuRequest(indicator_view="invalid_view")
    assert req_invalid.normalized_indicator_view() is None, "非法值应降级为 None"
    print("SendFeishuRequest body schema OK")

    print("OK")
