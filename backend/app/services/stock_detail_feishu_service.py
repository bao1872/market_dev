"""个股详情发送飞书服务 - 复用监控链路组件（同步执行）。

设计：
- 复用 test_channel_latest_event 链路的组件（截图/消息拼装/投递抽象），
  但采用同步执行模式返回 4 个分步骤布尔结果，便于定位"有文本、没图片"卡在哪一步。
- 禁止另写一套截图和消息拼装逻辑（spec P0-9 约束）。

4 布尔语义（响应字段）：
- text_ok: adapter.send_text_message 成功（文本投递）
- screenshot_ok: capture worker 返回 image_url（截图）
- image_upload_ok: _fetch_image_bytes 返回 bytes（图片拉取）
- feishu_send_ok: adapter.send_image_bytes 成功（飞书图片消息发送）

复用链路组件（与 test_channel_latest_event / notification_service 共享）：
1. compute_all_indicators（指标计算，与 indicators API 同款）
2. build_monitor_event_text（消息拼装，与 monitor 链同款）
3. create_message（消息创建幂等，notification_service 同款）
4. get_adapter + adapter.send_text_message / send_image_bytes（ChannelAdapter 抽象）
5. capture worker HTTP 调用（截图，test_channel_latest_event 同款）
6. _fetch_image_bytes（图片拉取，notification_service 同款）

偏离 spec 说明：
- spec 描述"Outbox → text delivery → image delivery"为异步链路，
  但同时要求"响应返回 4 个分步骤布尔"用于同步定位卡点，两者矛盾。
  本服务采用同步直接调用 adapter 接口（跳过 Outbox/MessageDelivery 异步状态机），
  以满足"同步返回 4 布尔"核心约束，同时复用 ChannelAdapter 抽象不另写投递逻辑。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_capture_token
from app.models.instrument import Instrument
from app.models.notification import NotificationChannel
from app.schemas.notification import NotificationMessageDTO
from app.services.channel_adapter import get_adapter  # noqa: F401  re-export for patch
from app.services.indicator_service import compute_all_indicators  # noqa: F401  re-export for patch
from app.services.message_builder import build_monitor_event_text
from app.services.notification_service import (
    ChannelNotFoundError,
    NotificationServiceError,
    _fetch_image_bytes,
    create_message,
)

logger = logging.getLogger("stock_detail_feishu_service")


class InstrumentNotFoundError(NotificationServiceError):
    """个股不存在。"""


async def send_stock_detail_to_feishu(
    db: AsyncSession,
    instrument_id: UUID,
    channel_id: UUID,
    user_id: UUID,
    frontend_base_url: str,
    capture_worker_url: str,
    capture_token_ttl_seconds: int = 300,
) -> dict[str, bool]:
    """发送个股详情到飞书，复用监控链路组件（同步执行）。

    [个股飞书] - 描述: 复用 test_channel_latest_event 链路组件，同步返回 4 布尔

    执行顺序（每步独立捕获异常，失败时对应布尔为 False 并提前返回）：
    1. 校验 instrument + channel（不属于当前用户则 ChannelNotFoundError）
    2. compute_all_indicators 计算指标快照
    3. build_monitor_event_text + create_message 拼装并创建消息
    4. adapter.send_text_message → text_ok
    5. capture worker HTTP POST → screenshot_ok
    6. _fetch_image_bytes → image_upload_ok
    7. adapter.send_image_bytes → feishu_send_ok

    Args:
        db: 异步会话
        instrument_id: 个股 ID
        channel_id: 通知渠道 ID（必须属于当前用户）
        user_id: 当前用户 ID（admin，由 endpoint 注入）
        frontend_base_url: 前端 base URL（截图服务访问）
        capture_worker_url: 截图 Worker HTTP 服务地址
        capture_token_ttl_seconds: capture token 有效期（秒）

    Returns:
        dict 含 text_ok / screenshot_ok / image_upload_ok / feishu_send_ok

    Raises:
        InstrumentNotFoundError: 个股不存在
        ChannelNotFoundError: 渠道不存在或不属于当前用户
        NotificationServiceError: 其他通知服务异常
    """
    result: dict[str, bool] = {
        "text_ok": False,
        "screenshot_ok": False,
        "image_upload_ok": False,
        "feishu_send_ok": False,
    }

    # 1. 校验个股存在
    instrument = await db.get(Instrument, instrument_id)
    if instrument is None:
        raise InstrumentNotFoundError(f"个股不存在: instrument_id={instrument_id}")

    # 2. 校验渠道存在且属于当前用户
    stmt_ch = select(NotificationChannel).where(
        NotificationChannel.id == channel_id,
        NotificationChannel.user_id == user_id,
    )
    ch_result = await db.execute(stmt_ch)
    channel = ch_result.scalar_one_or_none()
    if channel is None:
        raise ChannelNotFoundError(
            f"渠道不存在或不属于当前用户: channel_id={channel_id}"
        )

    # 3. 复用 compute_all_indicators 计算指标快照（与 indicators API 同款）
    indicators = await compute_all_indicators(
        session=db,
        instrument_id=instrument_id,
        timeframe="1d",
        adj="qfq",
        bars=250,
    )
    snapshot = (indicators or {}).get("snapshot") or {}

    # 4. 复用 build_monitor_event_text 拼装文本消息（与 monitor 链同款）
    # [个股飞书] - 个股详情主动发送无真实事件，event_type 占位为 manual_send
    event_time = datetime.now(UTC).isoformat()
    dto = build_monitor_event_text(
        stock_name=instrument.name or instrument.symbol,
        symbol=instrument.symbol,
        event_type="manual_send",
        event_time=event_time,
        current_price=snapshot.get("current_price"),
        bb_upper=snapshot.get("bb_upper"),
        bb_mid=snapshot.get("bb_mid"),
        bb_lower=snapshot.get("bb_lower"),
        upper_node=snapshot.get("upper_node"),
        lower_node=snapshot.get("lower_node"),
        poc_price=snapshot.get("poc_price"),
        position_0_1=snapshot.get("position_0_1"),
        resource_refs={
            "instrument_id": str(instrument.id),
            "symbol": instrument.symbol,
            "channel_id": str(channel_id),
            "manual_send": True,
        },
    )

    # 5. 复用 create_message 创建消息（幂等，notification_service 同款）
    message = await create_message(
        db=db,
        user_id=user_id,
        message_dto=dto,
        source_type="stock_detail_manual",
        source_id=instrument_id,
        idempotency_key=f"stock-detail-feishu:{instrument_id}:{channel_id}:{uuid4()}",
    )

    # 6. 复用 get_adapter 获取适配器（ChannelAdapter 抽象）
    adapter = get_adapter(channel.adapter_type)

    # 7. 复用 adapter.send_text_message → text_ok（飞书两段式投递 - 文本段）
    try:
        text_result = await adapter.send_text_message(dto, channel.target_config)
        result["text_ok"] = bool(text_result.success)
    except Exception as e:
        # [个股飞书] - 文本投递异常：不吞异常，记录上下文后继续后续步骤
        logger.error(
            "TEXT_SEND_FAILED instrument_id=%s channel_id=%s error=%s",
            instrument_id, channel_id, e,
        )
        result["text_ok"] = False
        return result

    # 8. 复用 capture worker HTTP 调用 → screenshot_ok（与 test_channel_latest_event 同款）
    image_url: str | None = None
    try:
        token = create_capture_token(
            subject=str(user_id),
            event_id=str(instrument_id),
            expires_delta=timedelta(seconds=capture_token_ttl_seconds),
        )
        capture_payload = {
            "symbol": instrument.symbol,
            "event_id": str(instrument_id),
            "token": token,
            "frontend_base_url": frontend_base_url,
            "output_filename": f"stock-detail-{instrument_id}-{uuid4()}",
            "instrument_id": str(instrument_id),
            "chart_version": "v1",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            capture_resp = await client.post(
                f"{capture_worker_url.rstrip('/')}/capture",
                json=capture_payload,
            )
            capture_resp.raise_for_status()
            capture_data = capture_resp.json()
        image_url = capture_data.get("image_url")
        result["screenshot_ok"] = bool(image_url)
    except Exception as e:
        logger.error(
            "CAPTURE_FAILED instrument_id=%s channel_id=%s error=%s",
            instrument_id, channel_id, e,
        )
        result["screenshot_ok"] = False
        return result

    if not image_url:
        result["screenshot_ok"] = False
        return result

    # 9. 复用 _fetch_image_bytes 拉取图片 → image_upload_ok（notification_service 同款）
    image_bytes: bytes | None = None
    try:
        image_bytes = await _fetch_image_bytes(image_url)
        result["image_upload_ok"] = image_bytes is not None
    except Exception as e:
        logger.error(
            "IMAGE_FETCH_FAILED image_url=%s error=%s",
            image_url, e,
        )
        result["image_upload_ok"] = False
        return result

    if not image_bytes:
        result["image_upload_ok"] = False
        return result

    # 10. 复用 adapter.send_image_bytes → feishu_send_ok（飞书两段式投递 - 图片段）
    try:
        image_result = await adapter.send_image_bytes(image_bytes, channel.target_config)
        result["feishu_send_ok"] = bool(image_result.success)
    except Exception as e:
        logger.error(
            "FEISHU_IMAGE_SEND_FAILED instrument_id=%s channel_id=%s error=%s",
            instrument_id, channel_id, e,
        )
        result["feishu_send_ok"] = False

    return result


if __name__ == "__main__":
    # 自测入口：验证模块加载与函数签名（不连接 DB）
    import inspect

    print(f"send_stock_detail_to_feishu={send_stock_detail_to_feishu}")
    sig = inspect.signature(send_stock_detail_to_feishu)
    params = list(sig.parameters.keys())
    expected = [
        "db", "instrument_id", "channel_id", "user_id",
        "frontend_base_url", "capture_worker_url", "capture_token_ttl_seconds",
    ]
    assert params == expected, f"参数不匹配: {params}"
    print(f"params={params} OK")

    # 验证复用组件已导入（patch 点存在）
    assert "get_adapter" in dir(), "get_adapter 应在模块顶层导入"
    assert "compute_all_indicators" in dir(), "compute_all_indicators 应在模块顶层导入"
    assert "build_monitor_event_text" in dir(), "build_monitor_event_text 应导入"
    assert "create_message" in dir(), "create_message 应导入"
    assert "_fetch_image_bytes" in dir(), "_fetch_image_bytes 应导入"
    print("复用组件导入 OK")

    # 验证异常类
    assert issubclass(InstrumentNotFoundError, NotificationServiceError)
    print("异常类 OK")

    print("OK")
