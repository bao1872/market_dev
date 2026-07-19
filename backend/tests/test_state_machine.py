"""飞书发送状态机测试（Phase C Task C.11.3 + CHANGE-20260718-006 Section 3）。

测试 send_stock_detail_to_feishu 状态机：
1. 截图失败时返回 status="failed" + failed_step + error_code + error_message
   （CHANGE-20260718-006 Section 3：从 partial_failed 升级为 failed，
   请求要求图片但未成功时整体标记 failed，触发显式重试与告警）
2. 截图成功时返回 status="pending"
3. 模拟 capture worker 返回 502 + 错误详情，验证 error_message 包含详情
4. get_share_status 在 failed 时正确汇总

复用现有 fixture：参考 backend/tests/test_stock_detail_feishu.py 和 backend/tests/conftest.py
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capture_job import CAPTURE_STATUS_FAILED, CaptureJob
from app.models.notification import (
    MessageDelivery,
    NotificationChannel,
)
from app.models.user import User
from app.services.stock_detail_feishu_service import (
    get_share_status,
    send_stock_detail_to_feishu,
)

# ============================================================
# fixtures（参考 test_stock_detail_feishu.py，保持一致）
# ============================================================


@pytest_asyncio.fixture
async def feishu_user_channel(
    db_session: AsyncSession, make_user_eligible
) -> AsyncGenerator[tuple[User, NotificationChannel], None]:
    """创建普通用户 + active 飞书 Platform App 渠道（member + 有效订阅）。

    [TestStateMachine] - 描述: 复用 conftest 的 make_user_eligible 确保用户有资格
    """
    user = User(
        id=uuid.uuid4(),
        email=f"sm_{uuid.uuid4().hex[:8]}@test.com",
        password_hash="$2b$12$dummyhash",
        status="active",
        timezone="Asia/Shanghai",
    )
    db_session.add(user)
    await db_session.flush()
    await make_user_eligible(user)

    channel = NotificationChannel(
        id=uuid.uuid4(),
        user_id=user.id,
        adapter_type="feishu_platform_app",
        display_name="测试飞书渠道",
        target_config={
            "app_id": "cli_test_001",
            "app_secret": "secret_value",
            "receive_id": "bg12345",
            "receive_id_type": "user_id",
        },
        status="active",
    )
    db_session.add(channel)
    await db_session.flush()

    yield user, channel


# ============================================================
# 辅助函数：构造 mock 响应
# ============================================================


def _make_snapshot_mock() -> MagicMock:
    """构造 MonitorSnapshot 非空快照 mock（避免依赖真实行情数据）。

    [TestStateMachine] - 描述: 复用 MonitorSnapshotService.get_snapshot 返回结构
    """
    snapshot = MagicMock()
    snapshot.current_price = 25.50
    snapshot.range_upper = 27.00
    snapshot.range_center = 25.00
    snapshot.range_lower = 23.00
    snapshot.upper_volume_zone = 26.00
    snapshot.lower_volume_zone = 24.00
    snapshot.most_traded_price = 25.00
    snapshot.range_position = 0.50
    return snapshot


def _make_capture_502_response() -> MagicMock:
    """构造 capture worker 502 错误响应 mock（含 JSON 错误详情）。

    [TestStateMachine] - 描述: 模拟 worker 返回 502 + JSON body，验证服务不丢弃响应体
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 502
    mock_resp.json.return_value = {
        "detail": "worker 内部截图超时",
        "error_code": "CAPTURE_TIMEOUT",
    }
    # raise_for_status 抛出 HTTPStatusError，携带 mock_resp 以便服务代码解析响应体
    http_err = httpx.HTTPStatusError(
        "Server error '502 Bad Gateway' for url 'http://capture.test/capture'",
        request=MagicMock(),
        response=mock_resp,
    )
    mock_resp.raise_for_status.side_effect = http_err
    return mock_resp


def _make_capture_success_response(
    image_url: str = "/static/captures/test.png",
) -> MagicMock:
    """构造 capture worker 成功响应 mock。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"image_url": image_url}
    mock_resp.raise_for_status.return_value = None
    return mock_resp


def _make_capture_no_image_url_response() -> MagicMock:
    """构造 capture worker 200 但无 image_url 响应 mock。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {}  # 无 image_url
    mock_resp.raise_for_status.return_value = None
    return mock_resp


def _configure_mock_httpx(mock_client_cls, capture_resp: MagicMock) -> None:
    """配置 mock AsyncClient 的 post 方法返回指定响应。

    [TestStateMachine] - 描述: 统一构造 httpx.AsyncClient mock，避免重复代码
    """
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=capture_resp)
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)


# ============================================================
# 状态机测试：send_stock_detail_to_feishu
# ============================================================


class TestStateMachineSend:
    """send_stock_detail_to_feishu 状态机测试（C.11.3）。

    [TestStateMachine] - 描述: 验证截图失败/成功时返回的状态机字段
    """

    @pytest.mark.asyncio
    async def test_capture_502_returns_failed_with_error_details(
        self,
        db_session: AsyncSession,
        test_instrument,
        feishu_user_channel: tuple[User, NotificationChannel],
    ) -> None:
        """截图 worker 返回 502 + JSON 详情 → status=failed + error_message 含详情。

        [TestStateMachine] - 描述: 验证 502 响应体不被丢弃，error_message 包含 worker 返回的详情。
        [CHANGE-20260718-006 Section 3] 截图失败时 status 从 partial_failed 升级为 failed。
        """
        user, _ = feishu_user_channel
        capture_resp = _make_capture_502_response()
        snapshot_mock = _make_snapshot_mock()

        with patch(
            "app.services.stock_detail_feishu_service.MonitorSnapshotService.get_snapshot",
            new=AsyncMock(return_value=snapshot_mock),
        ), patch("httpx.AsyncClient") as mock_client_cls:
            _configure_mock_httpx(mock_client_cls, capture_resp)

            result = await send_stock_detail_to_feishu(
                db=db_session,
                instrument_id=test_instrument.id,
                user_id=user.id,
                frontend_base_url="http://frontend.test",
                capture_worker_url="http://capture.test",
            )

        # 验证状态机：截图失败 → failed（CHANGE-20260718-006 Section 3）
        assert result["status"] == "failed"
        assert result["failed_step"] == "capture"
        assert result["error_code"] == "CAPTURE_REQUEST_FAILED"
        assert result["image_message_id"] is None
        # 验证 error_message 包含 worker 返回的详情（不丢弃响应体）
        assert "502" in result["error_message"]
        assert "worker 内部截图超时" in result["error_message"]

        # 验证 CaptureJob 失败记录已写入
        await db_session.flush()
        capture_jobs = (
            await db_session.execute(
                select(CaptureJob).where(CaptureJob.user_id == user.id)
            )
        ).scalars().all()
        assert len(capture_jobs) == 1
        assert capture_jobs[0].status == CAPTURE_STATUS_FAILED
        assert capture_jobs[0].error_code == "CAPTURE_REQUEST_FAILED"
        assert capture_jobs[0].error_message is not None
        assert "worker 内部截图超时" in capture_jobs[0].error_message

    @pytest.mark.asyncio
    async def test_capture_no_image_url_returns_failed_no_image_url(
        self,
        db_session: AsyncSession,
        test_instrument,
        feishu_user_channel: tuple[User, NotificationChannel],
    ) -> None:
        """截图 worker 返回 200 但无 image_url → failed + NO_IMAGE_URL。

        [TestStateMachine] - 描述: 验证 200 但缺 image_url 字段时的错误码判定。
        [CHANGE-20260718-006 Section 3] status 从 partial_failed 升级为 failed。
        """
        user, _ = feishu_user_channel
        capture_resp = _make_capture_no_image_url_response()
        snapshot_mock = _make_snapshot_mock()

        with patch(
            "app.services.stock_detail_feishu_service.MonitorSnapshotService.get_snapshot",
            new=AsyncMock(return_value=snapshot_mock),
        ), patch("httpx.AsyncClient") as mock_client_cls:
            _configure_mock_httpx(mock_client_cls, capture_resp)

            result = await send_stock_detail_to_feishu(
                db=db_session,
                instrument_id=test_instrument.id,
                user_id=user.id,
                frontend_base_url="http://frontend.test",
                capture_worker_url="http://capture.test",
            )

        assert result["status"] == "failed"
        assert result["failed_step"] == "capture"
        assert result["error_code"] == "NO_IMAGE_URL"
        assert result["image_message_id"] is None
        assert "image_url" in result["error_message"]

    @pytest.mark.asyncio
    async def test_image_outbox_failure_returns_failed_image_outbox(
        self,
        db_session: AsyncSession,
        test_instrument,
        feishu_user_channel: tuple[User, NotificationChannel],
    ) -> None:
        """截图成功但图片 Outbox 失败 → failed + IMAGE_OUTBOX_FAILED。

        [TestStateMachine] - 描述: 验证截图成功后 image_outbox 失败时的错误码判定。
        [CHANGE-20260718-006 Section 3] status 从 partial_failed 升级为 failed。
        """
        user, _ = feishu_user_channel
        capture_resp = _make_capture_success_response()
        snapshot_mock = _make_snapshot_mock()

        # mock write_outbox：第一次（card）成功，第二次（image）抛异常
        mock_write_outbox = AsyncMock(
            side_effect=[None, RuntimeError("图片 Outbox 写入失败")]
        )

        with patch(
            "app.services.stock_detail_feishu_service.MonitorSnapshotService.get_snapshot",
            new=AsyncMock(return_value=snapshot_mock),
        ), patch(
            "app.services.stock_detail_feishu_service.write_outbox",
            new=mock_write_outbox,
        ), patch("httpx.AsyncClient") as mock_client_cls:
            _configure_mock_httpx(mock_client_cls, capture_resp)

            result = await send_stock_detail_to_feishu(
                db=db_session,
                instrument_id=test_instrument.id,
                user_id=user.id,
                frontend_base_url="http://frontend.test",
                capture_worker_url="http://capture.test",
            )

        assert result["status"] == "failed"
        assert result["failed_step"] == "image_outbox"
        assert result["error_code"] == "IMAGE_OUTBOX_FAILED"
        assert "图片 Outbox 写入失败" in result["error_message"]

    @pytest.mark.asyncio
    async def test_capture_success_returns_pending(
        self,
        db_session: AsyncSession,
        test_instrument,
        feishu_user_channel: tuple[User, NotificationChannel],
    ) -> None:
        """截图 + 图片 Outbox 全部成功 → status=pending + failed_step=None。

        [TestStateMachine] - 描述: 验证全部成功时返回 pending（Outbox 异步投递尚未完成）
        """
        user, _ = feishu_user_channel
        capture_resp = _make_capture_success_response()
        snapshot_mock = _make_snapshot_mock()

        with patch(
            "app.services.stock_detail_feishu_service.MonitorSnapshotService.get_snapshot",
            new=AsyncMock(return_value=snapshot_mock),
        ), patch("httpx.AsyncClient") as mock_client_cls:
            _configure_mock_httpx(mock_client_cls, capture_resp)

            result = await send_stock_detail_to_feishu(
                db=db_session,
                instrument_id=test_instrument.id,
                user_id=user.id,
                frontend_base_url="http://frontend.test",
                capture_worker_url="http://capture.test",
            )

        assert result["status"] == "pending"
        assert result["failed_step"] is None
        assert result["error_code"] is None
        assert result["error_message"] is None
        assert result["image_message_id"] is not None
        assert result["message_id"] != result["image_message_id"]


# ============================================================
# 状态机测试：get_share_status
# ============================================================


class TestStateMachineGetShareStatus:
    """get_share_status 状态机汇总测试（C.11.3 + CHANGE-20260718-006 Section 3）。

    [TestStateMachine] - 描述: 验证 get_share_status 在 failed/success/pending 时的汇总逻辑
    """

    @pytest.mark.asyncio
    async def test_get_share_status_failed_with_capture_failure(
        self,
        db_session: AsyncSession,
        test_instrument,
        feishu_user_channel: tuple[User, NotificationChannel],
    ) -> None:
        """get_share_status 在 capture 失败时返回 failed + capture 失败信息。

        [TestStateMachine] - 描述: 卡片段 success + 图片段未创建（capture 失败）→ overall_status=failed。
        [CHANGE-20260718-006 Section 3] 从 partial_failed 升级为 failed。
        """
        user, channel = feishu_user_channel
        capture_resp = _make_capture_502_response()
        snapshot_mock = _make_snapshot_mock()

        with patch(
            "app.services.stock_detail_feishu_service.MonitorSnapshotService.get_snapshot",
            new=AsyncMock(return_value=snapshot_mock),
        ), patch("httpx.AsyncClient") as mock_client_cls:
            _configure_mock_httpx(mock_client_cls, capture_resp)

            send_result = await send_stock_detail_to_feishu(
                db=db_session,
                instrument_id=test_instrument.id,
                user_id=user.id,
                frontend_base_url="http://frontend.test",
                capture_worker_url="http://capture.test",
            )

        test_run_id = uuid.UUID(send_result["test_run_id"])
        message_group_id = send_result["message_group_id"]
        await db_session.flush()

        # 手动为 card 消息创建一条 success MessageDelivery（模拟 outbox_relay 已处理）
        # capture 失败时没有 image message，只有 text message
        card_msg_id = uuid.UUID(send_result["message_id"])
        card_delivery = MessageDelivery(
            id=uuid.uuid4(),
            notification_message_id=card_msg_id,
            channel_id=channel.id,
            status="success",
            delivery_type="card",
            attempt_count=1,
            message_group_id=message_group_id,
            idempotency_key=f"test-card-delivery-{uuid.uuid4().hex}",
        )
        db_session.add(card_delivery)
        await db_session.flush()

        # 调用 get_share_status 查询
        status = await get_share_status(
            db=db_session, test_run_id=test_run_id, user_id=user.id
        )

        # 验证状态机汇总
        assert status["overall_status"] == "failed"
        assert status["card_status"] == "success"
        assert status["capture_status"] == "failed"
        assert status["image_upload_status"] == "not_created"
        assert status["image_status"] == "not_created"
        assert status["failed_step"] == "capture"
        assert status["error_code"] == "CAPTURE_REQUEST_FAILED"
        assert "worker 内部截图超时" in (status["error_message"] or "")
        assert status["message_group_id"] == message_group_id

    @pytest.mark.asyncio
    async def test_get_share_status_success_when_both_deliveries_success(
        self,
        db_session: AsyncSession,
        test_instrument,
        feishu_user_channel: tuple[User, NotificationChannel],
    ) -> None:
        """get_share_status 在 card + image 两条投递都 success 时返回 success。

        [TestStateMachine] - 描述: 验证全部成功时 overall_status=success
        """
        user, channel = feishu_user_channel
        capture_resp = _make_capture_success_response()
        snapshot_mock = _make_snapshot_mock()

        with patch(
            "app.services.stock_detail_feishu_service.MonitorSnapshotService.get_snapshot",
            new=AsyncMock(return_value=snapshot_mock),
        ), patch("httpx.AsyncClient") as mock_client_cls:
            _configure_mock_httpx(mock_client_cls, capture_resp)

            send_result = await send_stock_detail_to_feishu(
                db=db_session,
                instrument_id=test_instrument.id,
                user_id=user.id,
                frontend_base_url="http://frontend.test",
                capture_worker_url="http://capture.test",
            )

        test_run_id = uuid.UUID(send_result["test_run_id"])
        message_group_id = send_result["message_group_id"]
        await db_session.flush()

        # 为 card + image 两条消息各创建一条 success MessageDelivery
        card_msg_id = uuid.UUID(send_result["message_id"])
        image_msg_id = uuid.UUID(send_result["image_message_id"])

        card_delivery = MessageDelivery(
            id=uuid.uuid4(),
            notification_message_id=card_msg_id,
            channel_id=channel.id,
            status="success",
            delivery_type="card",
            attempt_count=1,
            message_group_id=message_group_id,
            idempotency_key=f"test-card-delivery-{uuid.uuid4().hex}",
        )
        image_delivery = MessageDelivery(
            id=uuid.uuid4(),
            notification_message_id=image_msg_id,
            channel_id=channel.id,
            status="success",
            delivery_type="image",
            attempt_count=1,
            message_group_id=message_group_id,
            idempotency_key=f"test-image-delivery-{uuid.uuid4().hex}",
        )
        db_session.add(card_delivery)
        db_session.add(image_delivery)
        await db_session.flush()

        # 调用 get_share_status 查询
        status = await get_share_status(
            db=db_session, test_run_id=test_run_id, user_id=user.id
        )

        # 验证状态机汇总
        assert status["overall_status"] == "success"
        assert status["card_status"] == "success"
        assert status["capture_status"] == "success"
        assert status["image_upload_status"] == "success"
        assert status["image_status"] == "success"
        assert status["failed_step"] is None
        assert status["error_code"] is None
        assert status["message_group_id"] == message_group_id

    @pytest.mark.asyncio
    async def test_get_share_status_pending_when_no_deliveries(
        self,
        db_session: AsyncSession,
        test_instrument,
        feishu_user_channel: tuple[User, NotificationChannel],
    ) -> None:
        """get_share_status 在 Outbox 尚未 relay 时返回 pending。

        [TestStateMachine] - 描述: 消息已创建但 Outbox 未扩张为 MessageDelivery → pending
        """
        user, _ = feishu_user_channel
        capture_resp = _make_capture_success_response()
        snapshot_mock = _make_snapshot_mock()

        with patch(
            "app.services.stock_detail_feishu_service.MonitorSnapshotService.get_snapshot",
            new=AsyncMock(return_value=snapshot_mock),
        ), patch("httpx.AsyncClient") as mock_client_cls:
            _configure_mock_httpx(mock_client_cls, capture_resp)

            send_result = await send_stock_detail_to_feishu(
                db=db_session,
                instrument_id=test_instrument.id,
                user_id=user.id,
                frontend_base_url="http://frontend.test",
                capture_worker_url="http://capture.test",
            )

        test_run_id = uuid.UUID(send_result["test_run_id"])
        await db_session.flush()

        # 不创建任何 MessageDelivery（模拟 Outbox 尚未 relay）

        # 调用 get_share_status 查询
        status = await get_share_status(
            db=db_session, test_run_id=test_run_id, user_id=user.id
        )

        # 验证状态机汇总：消息存在但无 delivery → pending
        assert status["overall_status"] == "pending"
        assert status["card_status"] == "pending"
        assert status["capture_status"] == "pending"
        assert status["image_upload_status"] == "not_created"
        assert status["image_status"] == "not_created"
        assert status["failed_step"] is None
        assert status["error_code"] is None

    @pytest.mark.asyncio
    async def test_get_share_status_pending_when_card_success_image_in_progress(
        self,
        db_session: AsyncSession,
        test_instrument,
        feishu_user_channel: tuple[User, NotificationChannel],
    ) -> None:
        """get_share_status 在 card 成功但图片仍在进行中时返回 pending（非 failed）。

        [TestStateMachine] - 描述: 卡片段 success + 图片段 pending/sending/retrying
        （image_upload 未失败）→ overall_status=pending，等待图片投递完成。
        [CHANGE-20260718-006 Section 3] 区分"图片确定性失败"与"图片仍在进行中"：
        前者 → failed，后者 → pending。避免误判进行中投递为失败。
        """
        user, channel = feishu_user_channel
        capture_resp = _make_capture_success_response()
        snapshot_mock = _make_snapshot_mock()

        with patch(
            "app.services.stock_detail_feishu_service.MonitorSnapshotService.get_snapshot",
            new=AsyncMock(return_value=snapshot_mock),
        ), patch("httpx.AsyncClient") as mock_client_cls:
            _configure_mock_httpx(mock_client_cls, capture_resp)

            send_result = await send_stock_detail_to_feishu(
                db=db_session,
                instrument_id=test_instrument.id,
                user_id=user.id,
                frontend_base_url="http://frontend.test",
                capture_worker_url="http://capture.test",
            )

        test_run_id = uuid.UUID(send_result["test_run_id"])
        message_group_id = send_result["message_group_id"]
        await db_session.flush()

        # 创建 card delivery（success）+ image delivery（pending，image_upload 也 pending）
        card_msg_id = uuid.UUID(send_result["message_id"])
        image_msg_id = uuid.UUID(send_result["image_message_id"])

        card_delivery = MessageDelivery(
            id=uuid.uuid4(),
            notification_message_id=card_msg_id,
            channel_id=channel.id,
            status="success",
            delivery_type="card",
            attempt_count=1,
            message_group_id=message_group_id,
            idempotency_key=f"test-card-delivery-{uuid.uuid4().hex}",
        )
        # image delivery 处于 pending（正在上传中），image_upload_status 也为 pending
        image_delivery = MessageDelivery(
            id=uuid.uuid4(),
            notification_message_id=image_msg_id,
            channel_id=channel.id,
            status="pending",
            delivery_type="image",
            attempt_count=0,
            message_group_id=message_group_id,
            idempotency_key=f"test-image-delivery-{uuid.uuid4().hex}",
            image_upload_status="pending",
        )
        db_session.add(card_delivery)
        db_session.add(image_delivery)
        await db_session.flush()

        # 调用 get_share_status 查询
        status = await get_share_status(
            db=db_session, test_run_id=test_run_id, user_id=user.id
        )

        # 验证状态机汇总：card 成功 + 图片仍在进行中 → pending（不是 failed）
        assert status["overall_status"] == "pending"
        assert status["card_status"] == "success"
        assert status["image_status"] == "pending"
        assert status["image_upload_status"] == "pending"
        assert status["failed_step"] is None
        assert status["error_code"] is None
        assert status["error_message"] is None

    @pytest.mark.asyncio
    async def test_get_share_status_failed_when_card_success_image_delivery_dead(
        self,
        db_session: AsyncSession,
        test_instrument,
        feishu_user_channel: tuple[User, NotificationChannel],
    ) -> None:
        """get_share_status 在 card 成功但图片投递 dead 时返回 failed。

        [TestStateMachine] - 描述: 卡片段 success + 图片段 dead（投递已放弃）→ overall_status=failed。
        [CHANGE-20260718-006 Section 3] image_delivery 状态为 dead 视为确定性失败。
        """
        user, channel = feishu_user_channel
        capture_resp = _make_capture_success_response()
        snapshot_mock = _make_snapshot_mock()

        with patch(
            "app.services.stock_detail_feishu_service.MonitorSnapshotService.get_snapshot",
            new=AsyncMock(return_value=snapshot_mock),
        ), patch("httpx.AsyncClient") as mock_client_cls:
            _configure_mock_httpx(mock_client_cls, capture_resp)

            send_result = await send_stock_detail_to_feishu(
                db=db_session,
                instrument_id=test_instrument.id,
                user_id=user.id,
                frontend_base_url="http://frontend.test",
                capture_worker_url="http://capture.test",
            )

        test_run_id = uuid.UUID(send_result["test_run_id"])
        message_group_id = send_result["message_group_id"]
        await db_session.flush()

        # 创建 card delivery（success）+ image delivery（dead，投递已放弃）
        card_msg_id = uuid.UUID(send_result["message_id"])
        image_msg_id = uuid.UUID(send_result["image_message_id"])

        card_delivery = MessageDelivery(
            id=uuid.uuid4(),
            notification_message_id=card_msg_id,
            channel_id=channel.id,
            status="success",
            delivery_type="card",
            attempt_count=1,
            message_group_id=message_group_id,
            idempotency_key=f"test-card-delivery-{uuid.uuid4().hex}",
        )
        image_delivery = MessageDelivery(
            id=uuid.uuid4(),
            notification_message_id=image_msg_id,
            channel_id=channel.id,
            status="dead",
            delivery_type="image",
            attempt_count=5,
            message_group_id=message_group_id,
            idempotency_key=f"test-image-delivery-{uuid.uuid4().hex}",
            image_upload_status="pending",
            last_error_code="IMAGE_DELIVERY_DEAD",
            provider_response={"error_message": "图片投递已达最大重试次数"},
        )
        db_session.add(card_delivery)
        db_session.add(image_delivery)
        await db_session.flush()

        # 调用 get_share_status 查询
        status = await get_share_status(
            db=db_session, test_run_id=test_run_id, user_id=user.id
        )

        # 验证状态机汇总：card 成功 + 图片 dead → failed
        assert status["overall_status"] == "failed"
        assert status["card_status"] == "success"
        assert status["image_status"] == "dead"
        assert status["failed_step"] == "image_delivery"
        assert status["error_code"] == "IMAGE_DELIVERY_DEAD"
        assert "图片投递已达最大重试次数" in (status["error_message"] or "")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
