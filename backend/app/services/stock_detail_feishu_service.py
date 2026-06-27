"""个股详情发送飞书服务 - 复用监控链路组件 + 走正式 Outbox 链路。

设计：
- 复用 test_channel_latest_event / monitor_batch_service 的 Outbox 链路组件：
  1. MonitorSnapshotService.get_snapshot（快照 SSOT，禁止另写解析逻辑）
  2. build_monitor_event_text（消息拼装，与 monitor 链同款）
  3. create_message（消息创建幂等，notification_service 同款）
  4. write_outbox（事务性发件箱，outbox_relay 同款）
  5. capture worker HTTP（截图，test_channel_latest_event 同款）
- 不再同步直调 adapter，改走 Outbox → MessageDelivery → delivery_worker 异步链路。
- 飞书两段式投递（text + image）共享同一 message_group_id。

返回字段（响应）：
- test_run_id: 本次分享的唯一标识（幂等键组成部分）
- message_group_id: 关联 text+image 两条 Outbox/MessageDelivery 的组 ID
- message_id: 文本消息 ID（主消息）
- image_message_id: 图片消息 ID（截图失败时为 None）
- status: "pending"（Outbox 异步链路，创建后即为 pending）

状态查询：
- GET /admin/stock-detail-feishu/{test_run_id}/status
- 通过 test_run_id 查 MessageDelivery.message_group_id 关联的 text/image 投递状态
"""

from __future__ import annotations

import logging
import time
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
from app.services.monitor_snapshot_service import MonitorSnapshotService
from app.services.notification_service import (
    ChannelNotFoundError,
    NotificationServiceError,
    _fetch_image_bytes,  # noqa: F401  re-export for patch
    create_message,
)
from app.services.outbox_relay import write_outbox

logger = logging.getLogger("stock_detail_feishu_service")

# [StockDetailFeishu] - 事件类型：个股快照主动分享（不暴露内部 manual_send 代码）
_EVENT_TYPE_STOCK_SNAPSHOT_SHARE = "STOCK_SNAPSHOT_SHARE"

# [StockDetailFeishu] - 默认周期（与 MonitorSnapshotService 默认一致）
_DEFAULT_TIMEFRAME = "1d"


class InstrumentNotFoundError(NotificationServiceError):
    """个股不存在。"""


class StockDetailFeishuError(NotificationServiceError):
    """个股飞书发送失败，携带 error_code/failed_step 上下文。

    [StockDetailFeishu] - 描述: 技术错误返回三字段（advice.md 第十一节遗留清理）
    所有 send_stock_detail_to_feishu 内失败步骤抛此异常，由 API 层转为
    {"error_code":..., "error_message":..., "failed_step":...} HTTP 响应。

    Attributes:
        error_code: 错误码（如 SNAPSHOT_FAILED/TEXT_OUTBOX_FAILED/CAPTURE_FAILED）
        failed_step: 失败步骤（如 snapshot/text_outbox/capture/image_outbox）
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        failed_step: str,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.failed_step = failed_step


async def send_stock_detail_to_feishu(
    db: AsyncSession,
    instrument_id: UUID,
    channel_id: UUID,
    user_id: UUID,
    frontend_base_url: str,
    capture_worker_url: str,
    capture_token_ttl_seconds: int = 300,
) -> dict[str, Any]:
    """发送个股详情到飞书，走正式 Outbox 链路（异步投递）。

    [StockDetailFeishu] - 描述: 复用监控链路组件，走 Outbox → MessageDelivery → delivery_worker

    执行顺序：
    1. 校验 instrument + channel（不属于当前用户则 ChannelNotFoundError）
    2. 生成 test_run_id + message_group_id（幂等 + 关联 text/image）
    3. MonitorSnapshotService.get_snapshot 获取非空快照（SSOT）
    4. build_monitor_event_text + create_message 拼装并创建文本消息
    5. write_outbox(text) → outbox_relay 扩张为 MessageDelivery(text)
    6. capture worker HTTP 截图 → create_message + write_outbox(image)
    7. 返回 test_run_id/message_group_id/message_id/status

    Args:
        db: 异步会话
        instrument_id: 个股 ID
        channel_id: 通知渠道 ID（必须属于当前用户，用于身份校验）
        user_id: 当前用户 ID（admin，由 endpoint 注入）
        frontend_base_url: 前端 base URL（截图服务访问）
        capture_worker_url: 截图 Worker HTTP 服务地址
        capture_token_ttl_seconds: capture token 有效期（秒）

    Returns:
        dict 含 test_run_id / message_group_id / message_id / image_message_id / status

    Raises:
        InstrumentNotFoundError: 个股不存在
        ChannelNotFoundError: 渠道不存在或不属于当前用户
        StockDetailFeishuError: 快照/文本Outbox/截图/图片Outbox 失败（携带 error_code/failed_step）
        NotificationServiceError: 其他消息创建失败
    """
    total_start = time.time()
    snapshot_ms = 0.0
    text_outbox_ms = 0.0
    capture_ms = 0.0
    image_outbox_ms = 0.0
    cache_hit = False

    # 1. 校验个股存在
    instrument = await db.get(Instrument, instrument_id)
    if instrument is None:
        raise InstrumentNotFoundError(f"个股不存在: instrument_id={instrument_id}")

    # 2. 校验渠道存在且属于当前用户（用于身份校验，实际投递由 Outbox 扩张到所有 active 渠道）
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

    # 3. 生成 test_run_id + message_group_id（幂等 + 关联 text/image 两条投递）
    test_run_id = uuid4()
    message_group_id = str(uuid4())

    # 4. 调用 MonitorSnapshotService 获取非空快照（SSOT，禁止另写解析逻辑）
    # [StockDetailFeishu] - 快照来源：MonitorSnapshotService.get_snapshot 返回 MonitorSnapshot
    snapshot_start = time.time()
    try:
        snapshot = await MonitorSnapshotService().get_snapshot(
            db, str(instrument_id), _DEFAULT_TIMEFRAME
        )
    except (ValueError, KeyError, RuntimeError) as e:
        # 不吞异常：补上下文后 re-raise 为 StockDetailFeishuError（携带 error_code/failed_step）
        raise StockDetailFeishuError(
            f"获取监控快照失败 instrument_id={instrument_id}: {e}",
            error_code="SNAPSHOT_FAILED",
            failed_step="snapshot",
        ) from e
    snapshot_ms = (time.time() - snapshot_start) * 1000

    # 5. 复用 build_monitor_event_text 拼装文本消息（与 monitor 链同款）
    # [StockDetailFeishu] - event_type=STOCK_SNAPSHOT_SHARE，不暴露内部 manual_send 代码
    event_time = datetime.now(UTC).isoformat()
    dto = build_monitor_event_text(
        stock_name=instrument.name or instrument.symbol,
        symbol=instrument.symbol,
        event_type=_EVENT_TYPE_STOCK_SNAPSHOT_SHARE,
        event_time=event_time,
        current_price=snapshot.current_price,
        bb_upper=snapshot.range_upper,
        bb_mid=snapshot.range_center,
        bb_lower=snapshot.range_lower,
        upper_node=snapshot.upper_volume_zone,
        lower_node=snapshot.lower_volume_zone,
        poc_price=snapshot.most_traded_price,
        position_0_1=snapshot.range_position,
        resource_refs={
            "instrument_id": str(instrument.id),
            "symbol": instrument.symbol,
            "channel_id": str(channel_id),
            "test_run_id": str(test_run_id),
            "share": True,
        },
    )

    # 6. 复用 create_message 创建文本消息 + 写入文本 Outbox
    # [StockDetailFeishu] - 卡片段：delivery_type=card（→ msg_type=interactive），共享 message_group_id
    text_outbox_start = time.time()
    try:
        text_message = await create_message(
            db=db,
            user_id=user_id,
            message_dto=dto,
            source_type="stock_detail_share",
            source_id=instrument_id,
            idempotency_key=f"stock-detail-feishu:{instrument_id}:{channel_id}:{test_run_id}:text",
        )

        # 7. 写入 card Outbox（outbox_relay 扩张为 MessageDelivery(card) → delivery_worker → adapter.send → msg_type=interactive）
        await write_outbox(
            db=db,
            event_type="notification.message.created",
            payload={
                "message_id": str(text_message.id),
                "user_id": str(user_id),
                "delivery_type": "card",
                "message_group_id": message_group_id,
            },
            aggregate_type="notification_message",
            aggregate_id=text_message.id,
        )
    except Exception as e:
        # 不吞异常：文本 Outbox 失败属于关键步骤，携带 error_code/failed_step 抛出
        raise StockDetailFeishuError(
            f"文本消息创建/Outbox 写入失败 instrument_id={instrument_id}: {e}",
            error_code="TEXT_OUTBOX_FAILED",
            failed_step="text_outbox",
        ) from e
    text_outbox_ms = (time.time() - text_outbox_start) * 1000

    # 8. 截图 → 创建图片消息 → 写入图片 Outbox（截图失败不阻塞文本投递）
    # [StockDetailFeishu] - 图片段：delivery_type=image，与 text 共享 message_group_id
    image_message_id: str | None = None
    capture_start = time.time()
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
            "output_filename": f"stock-detail-{instrument_id}-{test_run_id}",
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
        if not image_url:
            raise RuntimeError("截图服务未返回 image_url")
        capture_ms = (time.time() - capture_start) * 1000

        # 构建图片消息 DTO（与 monitor_batch_service._create_chart_image_message 同款）
        image_dto = NotificationMessageDTO(
            message_type="MONITOR_EVENT",
            template_key="monitor_event",
            template_version="1.1.0",
            title=f"个股截图｜{instrument.name or instrument.symbol}",
            summary=f"{instrument.symbol} 个股详情截图，详见附图",
            resource_refs={
                "instrument_id": str(instrument.id),
                "symbol": instrument.symbol,
                "channel_id": str(channel_id),
                "test_run_id": str(test_run_id),
                "image_url": image_url,
            },
            data_time=event_time,
            primary_instrument={
                "instrument_id": str(instrument.id),
                "symbol": instrument.symbol,
                "name": instrument.name or "",
            },
            event_summary=_EVENT_TYPE_STOCK_SNAPSHOT_SHARE,
        )

        # 图片 Outbox 写入分段计时
        image_outbox_start = time.time()
        image_message = await create_message(
            db=db,
            user_id=user_id,
            message_dto=image_dto,
            source_type="stock_detail_share",
            source_id=instrument_id,
            idempotency_key=f"stock-detail-feishu:{instrument_id}:{channel_id}:{test_run_id}:image",
        )
        image_message_id = str(image_message.id)

        await write_outbox(
            db=db,
            event_type="notification.message.created",
            payload={
                "message_id": str(image_message.id),
                "user_id": str(user_id),
                "delivery_type": "image",
                "image_url": image_url,
                "message_group_id": message_group_id,
            },
            aggregate_type="notification_message",
            aggregate_id=image_message.id,
        )
        image_outbox_ms = (time.time() - image_outbox_start) * 1000
    except Exception as e:
        # 截图或图片 Outbox 失败不阻塞文本投递（文本已写入 Outbox）
        # 不吞异常：记录上下文与失败步骤，image_message_id 保持 None
        if capture_ms == 0.0:
            capture_ms = (time.time() - capture_start) * 1000
        logger.warning(
            "IMAGE_STEP_FAILED instrument_id=%s channel_id=%s test_run_id=%s "
            "failed_step=image error_code=IMAGE_STEP_FAILED error=%s",
            instrument_id, channel_id, test_run_id, e,
        )

    total_ms = (time.time() - total_start) * 1000
    logger.info(
        "[StockDetailFeishu] 发送完成 instrument_id=%s channel_id=%s "
        "test_run_id=%s snapshot_ms=%.1f text_outbox_ms=%.1f capture_ms=%.1f "
        "image_outbox_ms=%.1f total_ms=%.1f cache_hit=%s image_message_id=%s",
        instrument_id, channel_id, test_run_id,
        snapshot_ms, text_outbox_ms, capture_ms, image_outbox_ms, total_ms,
        cache_hit, image_message_id,
    )

    return {
        "test_run_id": str(test_run_id),
        "message_group_id": message_group_id,
        "message_id": str(text_message.id),
        "image_message_id": image_message_id,
        "status": "pending",
    }


async def get_share_status(
    db: AsyncSession,
    test_run_id: UUID,
) -> dict[str, Any]:
    """查询个股飞书分享的投递状态。

    [StockDetailFeishu] - 描述: 通过 test_run_id 查 MessageDelivery 投递状态

    流程：
    1. 通过 test_run_id 从 NotificationMessage.body.resource_refs 查出 message_group_id
    2. 按 message_group_id 查 MessageDelivery（text + image）
    3. 汇总返回 text_status / image_status / overall_status / failed_step / error_code

    Args:
        db: 异步会话
        test_run_id: 分享请求返回的 test_run_id

    Returns:
        dict 含 test_run_id / message_group_id / text_status / image_status /
        overall_status / failed_step / error_code / error_message

    Raises:
        NotificationServiceError: test_run_id 无对应消息
    """
    from sqlalchemy import text as sql_text

    from app.models.notification import NotificationMessage

    # 1. 通过 test_run_id 查 message_group_id（存于 body.resource_refs.test_run_id）
    # JSONB 查询：body->'resource_refs'->>'test_run_id' = :test_run_id
    stmt_msg = (
        select(NotificationMessage)
        .where(
            sql_text("body->'resource_refs'->>'test_run_id' = :test_run_id")
        )
        .params(test_run_id=str(test_run_id))
        .limit(1)
    )
    result_msg = await db.execute(stmt_msg)
    message = result_msg.scalar_one_or_none()
    if message is None:
        raise NotificationServiceError(
            f"未找到 test_run_id 对应的消息: test_run_id={test_run_id}"
        )

    # 从 Outbox payload 或 MessageDelivery.message_group_id 获取组 ID
    # 优先从 MessageDelivery 查
    from app.models.notification import MessageDelivery

    stmt_del = (
        select(MessageDelivery)
        .where(
            MessageDelivery.notification_message_id == message.id
        )
    )
    result_del = await db.execute(stmt_del)
    deliveries = list(result_del.scalars().all())

    # 若本消息无 delivery，可能 delivery 在关联的 image 消息上，通过 message_group_id 查
    if not deliveries:
        # 尝试从同 test_run_id 的其他消息查 delivery
        stmt_group = (
            select(MessageDelivery)
            .where(
                sql_text(
                    "notification_message_id IN ("
                    "SELECT id FROM notification_messages "
                    "WHERE body->'resource_refs'->>'test_run_id' = :test_run_id"
                    ")"
                )
            )
            .params(test_run_id=str(test_run_id))
        )
        result_group = await db.execute(stmt_group)
        deliveries = list(result_group.scalars().all())

    if not deliveries:
        # Outbox 尚未扩张为 MessageDelivery（relay worker 未轮询到）
        return {
            "test_run_id": str(test_run_id),
            "message_group_id": None,
            "card_status": "pending",
            "image_status": "pending",
            "overall_status": "pending",
            "failed_step": None,
            "error_code": None,
            "error_message": None,
        }

    # 取 message_group_id（所有 delivery 共享）
    message_group_id = deliveries[0].message_group_id

    # 按 delivery_type 分类
    # [StockDetailFeishu] - 文本/卡片投递 delivery_type=card（advice.md 第一节：恢复 interactive card），
    # 图片投递 delivery_type=image。状态查询按 card/image 分类返回。
    card_deliveries = [d for d in deliveries if d.delivery_type == "card"]
    image_deliveries = [d for d in deliveries if d.delivery_type == "image"]

    card_status = card_deliveries[0].status if card_deliveries else "not_created"
    image_status = image_deliveries[0].status if image_deliveries else "not_created"

    # 汇总 overall_status + failed_step + error_code + error_message
    # [StockDetailFeishu] - 描述: 从 MessageDelivery.last_error_code + provider_response
    #   提取 error_message（advice.md 第十一节遗留清理：技术错误返回三字段）
    failed_step: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    all_statuses = [d.status for d in deliveries]
    if all(s == "success" for s in all_statuses):
        overall_status = "success"
    elif any(s in ("failed", "dead") for s in all_statuses):
        overall_status = "failed"
        # 取第一个失败的 delivery 作为 failed_step，并提取 error_code + error_message
        for d in deliveries:
            if d.status in ("failed", "dead"):
                failed_step = d.delivery_type
                error_code = d.last_error_code
                # 从 provider_response 提取 error_message
                # provider_response 结构示例：{"code": 230002, "msg": "IP 不在白名单"}
                # 或 {"error_message": "...", "msg": "..."}，兼容多种渠道返回格式
                pr = d.provider_response
                if isinstance(pr, dict):
                    error_message = (
                        pr.get("error_message")
                        or pr.get("msg")
                        or pr.get("message")
                    )
                break
    else:
        overall_status = "pending"

    return {
        "test_run_id": str(test_run_id),
        "message_group_id": message_group_id,
        "card_status": card_status,
        "image_status": image_status,
        "overall_status": overall_status,
        "failed_step": failed_step,
        "error_code": error_code,
        "error_message": error_message,
    }


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
    assert "write_outbox" in dir(), "write_outbox 应导入"
    assert "MonitorSnapshotService" in dir(), "MonitorSnapshotService 应导入"
    print("复用组件导入 OK")

    # 验证异常类
    assert issubclass(InstrumentNotFoundError, NotificationServiceError)
    print("异常类 OK")

    # 验证 StockDetailFeishuError 携带 error_code/failed_step（advice.md 第十一节遗留清理）
    assert issubclass(StockDetailFeishuError, NotificationServiceError), \
        "StockDetailFeishuError 应继承 NotificationServiceError"
    err = StockDetailFeishuError(
        "测试错误", error_code="SNAPSHOT_FAILED", failed_step="snapshot"
    )
    assert err.error_code == "SNAPSHOT_FAILED", "error_code 应正确存储"
    assert err.failed_step == "snapshot", "failed_step 应正确存储"
    assert "测试错误" in str(err), "错误消息应保留"
    print(f"StockDetailFeishuError error_code={err.error_code} failed_step={err.failed_step} OK")

    # 验证 event_type 常量
    assert _EVENT_TYPE_STOCK_SNAPSHOT_SHARE == "STOCK_SNAPSHOT_SHARE"
    assert _EVENT_TYPE_STOCK_SNAPSHOT_SHARE != "manual_send"
    print(f"event_type={_EVENT_TYPE_STOCK_SNAPSHOT_SHARE} OK")

    # 验证 get_share_status 函数存在
    assert callable(get_share_status), "get_share_status 应可调用"
    sig_status = inspect.signature(get_share_status)
    params_status = list(sig_status.parameters.keys())
    assert params_status == ["db", "test_run_id"], \
        f"get_share_status 参数不匹配: {params_status}"
    print(f"get_share_status params={params_status} OK")

    print("OK")
