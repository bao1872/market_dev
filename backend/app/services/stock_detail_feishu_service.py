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
- status: "pending"（截图成功，Outbox 异步投递中）| "partial_failed"（截图失败）
- failed_step: 截图失败时的失败步骤（"capture" | "image_outbox" | None）
- error_code: 截图失败时的错误码（NO_IMAGE_URL | CAPTURE_REQUEST_FAILED | IMAGE_OUTBOX_FAILED | None）
- error_message: 截图失败时的错误详情（包含 worker 返回的响应体，最多 500 字符）

状态查询：
- GET /admin/stock-detail-feishu/{test_run_id}/status
- 通过 test_run_id 查 MessageDelivery.message_group_id 关联的 text/image 投递状态
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.capture import CAPTURE_SCOPE_STOCK_DETAIL
from app.core.security import create_capture_token
from app.core.time import format_shanghai_datetime, now_utc, to_shanghai_iso
from app.models.capture_job import (
    CAPTURE_STATUS_FAILED,
    CaptureJob,
)
from app.models.instrument import Instrument
from app.models.notification import MessageDelivery, NotificationChannel
from app.models.stock_memo import StockMemo
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
    user_id: UUID,
    frontend_base_url: str,
    capture_worker_url: str,
    capture_token_ttl_seconds: int = 300,
) -> dict[str, Any]:
    """发送个股详情到飞书，走正式 Outbox 链路（异步投递）。

    [StockDetailFeishu] - 描述: 复用监控链路组件，走 Outbox → MessageDelivery → delivery_worker

    执行顺序：
    1. 校验 instrument 存在
    2. 自动查找当前用户唯一 active 飞书渠道（webhook 或 platform_app）
    3. 生成 test_run_id + message_group_id（幂等 + 关联 text/image）
    4. MonitorSnapshotService.get_snapshot 获取非空快照（SSOT）
    5. build_monitor_event_text + create_message 拼装并创建文本消息（始终附带 StockMemo）
    6. write_outbox(text) → outbox_relay 按 target_channel_id 扩张为单条 MessageDelivery(text)
    7. capture worker HTTP 截图 → create_message + write_outbox(image)
    8. 返回 test_run_id/message_group_id/message_id/status

    Args:
        db: 异步会话
        instrument_id: 个股 ID
        user_id: 当前用户 ID（由 endpoint 注入）
        frontend_base_url: 前端 base URL（截图服务访问）
        capture_worker_url: 截图 Worker HTTP 服务地址
        capture_token_ttl_seconds: capture token 有效期（秒）

    Returns:
        dict 含 test_run_id / message_group_id / message_id / image_message_id /
        status / failed_step / error_code / error_message
        - status="pending": 截图成功，Outbox 异步投递中（终态由 delivery_worker 决定）
        - status="partial_failed": 截图失败，文本已写入 Outbox，支持仅重试图片
        - failed_step/error_code/error_message: 截图成功时为 None，失败时携带上下文

    Raises:
        InstrumentNotFoundError: 个股不存在
        ChannelNotFoundError: 用户无 active 飞书渠道
        StockDetailFeishuError: 快照/文本Outbox 失败（携带 error_code/failed_step）
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

    # [StockDetailFeishu] - 手动发送时只要股票有备忘录，始终附带 memo 内容
    # notify_feishu 开关只控制自动盘中触发是否附带，不影响手动发送
    memo_stmt = select(StockMemo).where(
        StockMemo.user_id == user_id,
        StockMemo.instrument_id == instrument_id,
    )
    memo_result = await db.execute(memo_stmt)
    memo = memo_result.scalar_one_or_none()
    memo_content = memo.content if memo else None

    # 2. 自动查找当前用户唯一 active 飞书渠道（仅 Platform App）
    stmt_ch = select(NotificationChannel).where(
        NotificationChannel.user_id == user_id,
        NotificationChannel.adapter_type == "feishu_platform_app",
        NotificationChannel.status == "active",
    )
    ch_result = await db.execute(stmt_ch)
    channel = ch_result.scalar_one_or_none()
    if channel is None:
        raise ChannelNotFoundError(
            f"用户无 active 飞书渠道: user_id={user_id}"
        )

    # 3. 生成 test_run_id + message_group_id（幂等 + 关联 text/image 两条投递）
    test_run_id = uuid4()
    message_group_id = str(uuid4())

    # 4. 调用 MonitorSnapshotService 获取非空快照（SSOT，禁止另写解析逻辑）
    # [StockDetailFeishu] - 快照来源：MonitorSnapshotService.get_snapshot 返回 MonitorSnapshot
    snapshot_start = time.time()
    try:
        snapshot = await MonitorSnapshotService().get_snapshot(
            db, str(instrument_id), _DEFAULT_TIMEFRAME, force_refresh=True
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
    # event_time 保持 ISO 可解析（build_monitor_event_text 内部解析取 HH:MM）
    # 使用 to_shanghai_iso 生成 +08:00 而非 UTC +00:00
    event_time = to_shanghai_iso(now_utc())
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
            "channel_id": str(channel.id),
            "test_run_id": str(test_run_id),
            "share": True,
        },
        memo=memo_content,
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
            idempotency_key=f"stock-detail-feishu:{instrument_id}:{channel.id}:{test_run_id}:text",
        )

        # 7. 写入 card Outbox（outbox_relay 按 target_channel_id 扩张为单条 MessageDelivery(card)）
        await write_outbox(
            db=db,
            event_type="notification.message.created",
            payload={
                "message_id": str(text_message.id),
                "user_id": str(user_id),
                "delivery_type": "card",
                "message_group_id": message_group_id,
                "target_channel_id": str(channel.id),
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
    # [StockDetailFeishu] - 状态机：在 try 块外维护失败上下文，截图失败时返回 partial_failed
    image_message_id: str | None = None
    image_url: str | None = None
    failed_step_local: str | None = None
    error_code_local: str | None = None
    error_message_local: str | None = None
    capture_start = time.time()
    try:
        # [Capture] - 描述: stock_detail 链路必须传 scope=stock_detail_capture + instrument_id
        # （advice.md 第六节硬规则，由 get_capture_token_payload 校验）
        token = create_capture_token(
            subject=str(user_id),
            event_id=str(instrument_id),
            expires_delta=timedelta(seconds=capture_token_ttl_seconds),
            scope=CAPTURE_SCOPE_STOCK_DETAIL,
            instrument_id=str(instrument_id),
            user_id=str(user_id),
        )
        capture_payload = {
            "symbol": instrument.symbol,
            "event_id": str(instrument_id),
            "token": token,
            "frontend_base_url": frontend_base_url,
            "output_filename": f"stock-detail-{instrument_id}-{test_run_id}",
            "instrument_id": str(instrument_id),
            "chart_version": "v1",
            # [capture-realtime] - 扩展字段：周期透传 + 实时来源 + 运行ID + 缓存旁路
            "timeframe": "15m",
            "capture_run_id": str(test_run_id),
            "source_bar_time": snapshot.as_of.isoformat(),
            "disable_cache": True,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            capture_resp = await client.post(
                f"{capture_worker_url.rstrip('/')}/capture",
                json=capture_payload,
            )
            # [StockDetailFeishu] - 修复: 不丢弃 worker 返回的错误详情（advice.md 第七节状态机）
            try:
                capture_resp.raise_for_status()
            except httpx.HTTPStatusError as http_err:
                # 解析 worker 返回的错误详情，不丢弃响应体
                # error_code 由外层 except 根据 failed_step_local 重新判定，此处只提取 err_msg
                try:
                    err_body = capture_resp.json()
                    err_msg = (
                        err_body.get("detail")
                        or err_body.get("message")
                        or str(http_err)
                    )
                except Exception:
                    err_msg = str(http_err)
                raise RuntimeError(
                    f"截图服务返回 {capture_resp.status_code}: {err_msg}"
                ) from http_err
            capture_data = capture_resp.json()
        # [capture-realtime] - 日志输出实时截图上下文（便于核对不复用旧图）
        logger.info(
            "实时截图请求参数 timeframe=15m source_bar_time=%s capture_run_id=%s "
            "disable_cache=true cache_hit=%s",
            snapshot.as_of.isoformat(), str(test_run_id),
            capture_data.get("cache_hit"),
        )
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
                "channel_id": str(channel.id),
                "test_run_id": str(test_run_id),
                "image_url": image_url,
            },
            data_time=format_shanghai_datetime(),
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
            idempotency_key=f"stock-detail-feishu:{instrument_id}:{channel.id}:{test_run_id}:image",
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
                "target_channel_id": str(channel.id),
            },
            aggregate_type="notification_message",
            aggregate_id=image_message.id,
        )
        image_outbox_ms = (time.time() - image_outbox_start) * 1000
    except Exception as e:
        # 截图或图片 Outbox 失败不阻塞文本投递（文本已写入 Outbox）
        # 不吞异常：写 CaptureJob 失败记录并补充上下文，image_message_id 保持 None
        if capture_ms == 0.0:
            capture_ms = (time.time() - capture_start) * 1000

        # 判定失败阶段：未拿到 image_url 视为 capture 失败；否则为 image_outbox 失败
        failed_step_local = "capture" if not image_url else "image_outbox"
        # [StockDetailFeishu] - 优先复用 worker 返回的 error_code（HTTPStatusError 分支已抛 RuntimeError）
        if (
            isinstance(e, RuntimeError)
            and "截图服务未返回 image_url" in str(e)
        ):
            error_code_local = "NO_IMAGE_URL"
        elif failed_step_local == "capture":
            error_code_local = "CAPTURE_REQUEST_FAILED"
        else:
            error_code_local = "IMAGE_OUTBOX_FAILED"
        error_message_local = str(e)[:500]

        db.add(
            CaptureJob(
                event_id=instrument_id,
                instrument_id=instrument_id,
                user_id=user_id,
                message_group_id=message_group_id,
                status=CAPTURE_STATUS_FAILED,
                attempt_count=1,
                image_url=image_url,
                error_code=error_code_local,
                error_message=error_message_local,
            )
        )
        logger.warning(
            "IMAGE_STEP_FAILED instrument_id=%s channel_id=%s test_run_id=%s "
            "failed_step=%s error_code=%s error=%s",
            instrument_id, channel.id, test_run_id,
            failed_step_local, error_code_local, e,
        )

    total_ms = (time.time() - total_start) * 1000
    logger.info(
        "[StockDetailFeishu] 发送完成 instrument_id=%s channel_id=%s "
        "test_run_id=%s snapshot_ms=%.1f text_outbox_ms=%.1f capture_ms=%.1f "
        "image_outbox_ms=%.1f total_ms=%.1f cache_hit=%s image_message_id=%s "
        "failed_step=%s error_code=%s",
        instrument_id, channel.id, test_run_id,
        snapshot_ms, text_outbox_ms, capture_ms, image_outbox_ms, total_ms,
        cache_hit, image_message_id,
        failed_step_local, error_code_local,
    )

    # [StockDetailFeishu] - 状态机：截图失败时返回 partial_failed（advice.md 第七节）
    # 截图成功时仍返回 pending（Outbox 异步投递尚未完成，由 delivery_worker 处理）
    if failed_step_local is not None:
        final_status = "partial_failed"
    else:
        final_status = "pending"

    return {
        "test_run_id": str(test_run_id),
        "message_group_id": message_group_id,
        "message_id": str(text_message.id),
        "image_message_id": image_message_id,
        "status": final_status,
        "failed_step": failed_step_local,
        "error_code": error_code_local,
        "error_message": error_message_local,
    }


def _extract_delivery_error_message(delivery: MessageDelivery) -> str | None:
    """从 MessageDelivery 的 provider_response / image_upload_provider_response 提取可读错误信息。"""
    for pr in (delivery.image_upload_provider_response, delivery.provider_response):
        if isinstance(pr, dict):
            msg = pr.get("error_message") or pr.get("msg") or pr.get("message")
            if msg:
                return str(msg)
    return None


async def get_share_status(
    db: AsyncSession,
    test_run_id: UUID,
    user_id: UUID,
) -> dict[str, Any]:
    """查询个股飞书分享的投递状态。

    [StockDetailFeishu] - 描述: 通过 test_run_id 查 MessageDelivery 投递状态

    流程：
    1. 通过 test_run_id + user_id 从 NotificationMessage.body.resource_refs 查出消息
    2. 按 message_group_id 查 MessageDelivery（text + image）
    3. 汇总返回 card_status / image_status / overall_status / failed_step / error_code

    Args:
        db: 异步会话
        test_run_id: 分享请求返回的 test_run_id
        user_id: 当前用户 ID（权限隔离）

    Returns:
        dict 含 test_run_id / message_group_id / card_status / image_status /
        overall_status / failed_step / error_code / error_message / image_message_id

    Raises:
        NotificationServiceError: test_run_id 无对应消息
    """
    from sqlalchemy import text as sql_text

    from app.models.notification import MessageDelivery, NotificationMessage

    # 1. 通过 test_run_id + user_id 一次性查询所有关联 MessageDelivery
    # [StockDetailFeishu] - 描述: 文本/图片是两条独立 NotificationMessage，各带一条
    # MessageDelivery，必须跨消息汇总，不能仅查第一条消息的 delivery。
    stmt_del = (
        select(MessageDelivery)
        .where(
            sql_text(
                "notification_message_id IN ("
                "SELECT id FROM notification_messages "
                "WHERE user_id = :user_id AND body->'resource_refs'->>'test_run_id' = :test_run_id"
                ")"
            )
        )
        .params(user_id=str(user_id), test_run_id=str(test_run_id))
    )
    result_del = await db.execute(stmt_del)
    deliveries = list(result_del.scalars().all())

    if not deliveries:
        # 确认消息是否存在：存在但 Outbox 尚未 relay 则返回 pending；不存在则 404
        stmt_msg = (
            select(NotificationMessage)
            .where(
                NotificationMessage.user_id == user_id,
                sql_text("body->'resource_refs'->>'test_run_id' = :test_run_id"),
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

        # Outbox 尚未扩张为 MessageDelivery（relay worker 未轮询到）
        return {
            "test_run_id": str(test_run_id),
            "message_group_id": None,
            "card_status": "pending",
            "capture_status": "pending",
            "image_upload_status": "not_created",
            "image_status": "not_created",
            "overall_status": "pending",
            "failed_step": None,
            "error_code": None,
            "error_message": None,
            "image_message_id": None,
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
    image_message_id = (
        str(image_deliveries[0].notification_message_id) if image_deliveries else None
    )
    # [StockDetailFeishu] - 描述: 图片上传状态取 image delivery 的 image_upload_status
    # 若字段为 None，按 delivery.status 推断：success→success，failed/dead→failed，其他→pending
    if image_deliveries:
        image_upload_status = image_deliveries[0].image_upload_status
        if image_upload_status is None:
            if image_deliveries[0].status == "success":
                image_upload_status = "success"
            elif image_deliveries[0].status in ("failed", "dead"):
                image_upload_status = "failed"
            else:
                image_upload_status = "pending"
    else:
        image_upload_status = "not_created"

    # 查询同 message_group_id 的截图失败记录（capture 失败时不会创建 image delivery）
    stmt_cj = (
        select(CaptureJob)
        .where(
            CaptureJob.message_group_id == message_group_id,
            CaptureJob.status == CAPTURE_STATUS_FAILED,
        )
        .order_by(CaptureJob.created_at.desc())
        .limit(1)
    )
    result_cj = await db.execute(stmt_cj)
    capture_job_failed = result_cj.scalar_one_or_none()

    # [StockDetailFeishu] - 描述: capture_status 反映截图任务状态
    # capture 失败时不会有 image delivery，因此单独查询 CaptureJob 失败记录
    capture_status: str = "pending"
    if capture_job_failed:
        capture_status = "failed"
    elif image_deliveries:
        # image delivery 已创建说明截图已成功返回 image_url
        capture_status = "success"

    # 汇总 overall_status + failed_step + error_code + error_message
    # [StockDetailFeishu] - 描述: 文字成功+图片失败 → overall_status=partial_failed
    failed_step: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    card_success = card_status == "success"
    image_exists = bool(image_deliveries)
    image_success = image_exists and image_status == "success"
    any_failed_or_dead = any(d.status in ("failed", "dead") for d in deliveries)

    if card_success and image_success:
        overall_status = "success"
    elif card_success and not image_success:
        # 卡片段成功，但图片段未成功（pending/sending/retrying/failed/dead/not_created）
        # [StockDetailFeishu] - 描述: 文字成功+图片未成功 → partial_failed
        overall_status = "partial_failed"
        if capture_job_failed:
            failed_step = "capture"
            error_code = capture_job_failed.error_code
            error_message = capture_job_failed.error_message
        elif image_deliveries and image_deliveries[0].status in ("failed", "dead", "retrying"):
            image_delivery = image_deliveries[0]
            if image_delivery.image_upload_status == "failed":
                failed_step = "image_upload"
                error_code = image_delivery.image_upload_error_code
            else:
                failed_step = "image_delivery"
                error_code = image_delivery.last_error_code
            error_message = _extract_delivery_error_message(image_delivery)
        else:
            # 卡片段成功但图片段尚未创建，也视为 partial_failed（等待中）
            # 此时没有具体失败步骤，保持 None
            pass
    elif any_failed_or_dead:
        overall_status = "failed"
        for d in deliveries:
            if d.status in ("failed", "dead"):
                if d.delivery_type == "card":
                    failed_step = "card"
                elif d.delivery_type == "image":
                    if d.image_upload_status == "failed":
                        failed_step = "image_upload"
                    else:
                        failed_step = "image_delivery"
                else:
                    failed_step = d.delivery_type
                error_code = d.last_error_code
                error_message = _extract_delivery_error_message(d)
                break
    else:
        overall_status = "pending"

    return {
        "test_run_id": str(test_run_id),
        "message_group_id": message_group_id,
        "card_status": card_status,
        "capture_status": capture_status,
        "image_upload_status": image_upload_status,
        "image_status": image_status,
        "overall_status": overall_status,
        "failed_step": failed_step,
        "error_code": error_code,
        "error_message": error_message,
        "image_message_id": image_message_id,
    }


if __name__ == "__main__":
    # 自测入口：验证模块加载与函数签名（不连接 DB）
    import inspect

    print(f"send_stock_detail_to_feishu={send_stock_detail_to_feishu}")
    sig = inspect.signature(send_stock_detail_to_feishu)
    params = list(sig.parameters.keys())
    expected = [
        "db", "instrument_id", "user_id",
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
    assert params_status == ["db", "test_run_id", "user_id"], \
        f"get_share_status 参数不匹配: {params_status}"
    print(f"get_share_status params={params_status} OK")

    print("OK")
