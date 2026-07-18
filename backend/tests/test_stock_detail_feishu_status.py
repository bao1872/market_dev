"""Phase 3 Task 3.6: 个股详情飞书发送状态聚合与图片单独重试测试。

覆盖：
1. capture 成功 → 通过 Outbox Relay 扩张为 card + image 两条 Delivery，共享 message_group_id
2. capture 失败 → 卡片段成功、图片段未创建，overall_status=failed，failed_step=capture
   （CHANGE-20260718-006 Section 3：从 partial_failed 升级为 failed）
3. image upload 失败 → 可仅重试图片，不重复发送文字
4. 同一 message_group_id 关联 text + image
5. 用户只能查询自己的发送状态
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.main import app
from app.models.capture_job import CAPTURE_STATUS_FAILED, CaptureJob
from app.models.notification import MessageDelivery, NotificationChannel
from app.models.user import User
from app.schemas.notification import DeliveryResult
from app.services.delivery_worker import process_pending_deliveries
from app.services.outbox_relay import relay_outbox
from tests.conftest import make_asgi_transport

# ============================================================
# 测试 fixtures / helpers
# ============================================================


@pytest_asyncio.fixture
async def user_with_feishu_platform_app_channel(
    db_session: AsyncSession, make_user_eligible
):
    """创建普通用户 + active 飞书平台应用渠道（支持图片投递）。"""
    user = User(
        id=uuid.uuid4(),
        email=f"user_{uuid.uuid4().hex[:8]}@test.com",
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
        display_name="测试飞书平台应用渠道",
        target_config={
            "app_id": "cli_test",
            "app_secret": "test_secret",
            "receive_id": "ou_test",
            "receive_id_type": "user_id",
        },
        status="active",
    )
    db_session.add(channel)
    await db_session.flush()

    yield user, channel


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _override_get_db(session: AsyncSession) -> None:
    """覆盖 app 的 get_db 依赖，使其使用测试会话。

    将 endpoint 的 db.commit() 替换为 flush，保持 db_session fixture 的 SAVEPOINT 活跃。
    """
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        with patch.object(session, "commit", new=AsyncMock(side_effect=session.flush)):
            yield session

    app.dependency_overrides[deps_get_db] = get_test_db
    app.dependency_overrides[db_get_db] = get_test_db


def _make_capture_response(image_url: str = "/static/captures/test.png") -> MagicMock:
    """构造 capture worker 成功响应 mock。"""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"image_url": image_url}
    mock_resp.raise_for_status.return_value = None
    return mock_resp


def _make_capture_failure_response() -> MagicMock:
    """构造 capture worker 失败响应 mock（raise_for_status 抛异常）。"""
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("截图服务不可用")
    return mock_resp


async def _send_stock_detail(
    db_session: AsyncSession,
    client: AsyncClient,
    user: User,
    instrument_id: uuid.UUID,
    capture_resp: MagicMock,
) -> dict:
    """调用发送端点并触发 Outbox Relay 扩张为 Delivery。"""
    with patch(
        "app.services.monitor_snapshot_service.compute_all_indicators",
        new=AsyncMock(
            return_value={
                "layers": [],
                "data": {
                    "watchlist_monitor": {
                        "current_price": [25.50],
                        "bb_upper": [27.00],
                        "bb_mid": [25.00],
                        "bb_lower": [23.00],
                        "upper_node": [{"price_mid": 26.00}],
                        "lower_node": [{"price_mid": 24.00}],
                        "poc_price": [25.00],
                        "position_0_1": [0.50],
                    },
                },
            }
        ),
    ), patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=capture_resp)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        response = await client.post(
            f"/instruments/{instrument_id}/send-feishu",
            headers=_auth_headers(user.id),
            json={},
        )

    assert response.status_code == 200, f"发送失败: {response.text}"
    data = response.json()

    # 触发 Outbox Relay 扩张（mock Redis，避免测试环境无 Redis）
    with patch("app.services.outbox_relay.get_redis") as mock_redis:
        mock_redis.return_value = AsyncMock()
        processed = await relay_outbox(db_session, batch_size=10)
    assert processed >= 1, "应至少处理一条 Outbox 记录"

    return data


# ============================================================
# 测试用例
# ============================================================


class TestStockDetailFeishuStatusAggregation:
    """发送状态聚合接口测试。"""

    @pytest.mark.asyncio
    async def test_capture_success_creates_card_and_image_deliveries(
        self,
        db_session: AsyncSession,
        test_instrument,
        user_with_feishu_platform_app_channel,
    ) -> None:
        """capture 成功 → card + image 两条 Delivery，共享 message_group_id。"""
        user, channel = user_with_feishu_platform_app_channel
        _override_get_db(db_session)

        try:
            transport = make_asgi_transport(app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                data = await _send_stock_detail(
                    db_session, client, user, test_instrument.id, _make_capture_response()
                )

            test_run_id = data["test_run_id"]
            message_group_id = data["message_group_id"]
            assert message_group_id, "应返回 message_group_id"

            # 验证两条 Delivery 存在且共享 message_group_id
            deliveries = (
                await db_session.execute(
                    select(MessageDelivery).where(
                        MessageDelivery.message_group_id == message_group_id
                    )
                )
            ).scalars().all()
            assert len(deliveries) == 2, f"应创建 2 条 Delivery，实际 {len(deliveries)}"
            types = {d.delivery_type for d in deliveries}
            assert types == {"card", "image"}, f"delivery_type 集合应为 {{card, image}}，实际 {types}"
            assert all(d.channel_id == channel.id for d in deliveries), "投递应指向同一渠道"

            # 状态查询端点返回 10 字段
            transport = make_asgi_transport(app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                status_resp = await client.get(
                    f"/stock-detail-feishu/{test_run_id}/status",
                    headers=_auth_headers(user.id),
                )

            assert status_resp.status_code == 200, f"状态查询失败: {status_resp.text}"
            status_data = status_resp.json()
            assert status_data["test_run_id"] == test_run_id
            assert status_data["message_group_id"] == message_group_id
            assert status_data["card_status"] == "pending"
            assert status_data["capture_status"] == "success"
            assert status_data["image_upload_status"] == "pending"
            assert status_data["image_status"] == "pending"
            assert status_data["overall_status"] == "pending"
            assert status_data["image_message_id"] is not None
            assert set(status_data.keys()) == {
                "test_run_id",
                "message_group_id",
                "card_status",
                "capture_status",
                "image_upload_status",
                "image_status",
                "overall_status",
                "failed_step",
                "error_code",
                "error_message",
                "image_message_id",
            }, f"响应字段不匹配: {status_data.keys()}"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_capture_failure_returns_failed(
        self,
        db_session: AsyncSession,
        test_instrument,
        user_with_feishu_platform_app_channel,
    ) -> None:
        """截图失败 → 卡片段仍可成功，overall_status=failed，failed_step=capture。

        [CHANGE-20260718-006 Section 3] 从 partial_failed 升级为 failed：
        请求要求图片但未成功时整体标记 failed，触发显式重试与告警。
        """
        user, channel = user_with_feishu_platform_app_channel
        _override_get_db(db_session)

        try:
            transport = make_asgi_transport(app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                data = await _send_stock_detail(
                    db_session,
                    client,
                    user,
                    test_instrument.id,
                    _make_capture_failure_response(),
                )

            test_run_id = data["test_run_id"]
            message_group_id = data["message_group_id"]
            assert data["image_message_id"] is None, "截图失败时 image_message_id 应为 None"

            # 此时 Outbox Relay 只扩张出 card Delivery
            deliveries = (
                await db_session.execute(
                    select(MessageDelivery).where(
                        MessageDelivery.message_group_id == message_group_id
                    )
                )
            ).scalars().all()
            assert len(deliveries) == 1, f"应只创建 1 条 card Delivery，实际 {len(deliveries)}"
            assert deliveries[0].delivery_type == "card"

            # 模拟卡片段投递成功
            deliveries[0].status = "success"
            await db_session.flush()

            # 查询状态应为 failed
            transport = make_asgi_transport(app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                status_resp = await client.get(
                    f"/stock-detail-feishu/{test_run_id}/status",
                    headers=_auth_headers(user.id),
                )

            assert status_resp.status_code == 200
            status_data = status_resp.json()
            assert status_data["overall_status"] == "failed"
            assert status_data["card_status"] == "success"
            assert status_data["image_status"] == "not_created"
            assert status_data["failed_step"] == "capture"
            assert status_data["error_code"] is not None
            assert status_data["error_message"] is not None
            assert status_data["image_message_id"] is None

            # 验证 CaptureJob 失败记录
            capture_jobs = (
                await db_session.execute(
                    select(CaptureJob).where(
                        CaptureJob.message_group_id == message_group_id,
                        CaptureJob.status == CAPTURE_STATUS_FAILED,
                    )
                )
            ).scalars().all()
            assert len(capture_jobs) == 1, "应创建一条截图失败记录"
        finally:
            app.dependency_overrides.clear()


class TestStockDetailFeishuImageRetry:
    """图片单独重试测试。"""

    @pytest.mark.asyncio
    async def test_image_upload_failure_can_retry_only_image(
        self,
        db_session: AsyncSession,
        test_instrument,
        user_with_feishu_platform_app_channel,
    ) -> None:
        """image upload 失败 → failed；重试图片后 success，且不重复发送文字。

        [CHANGE-20260718-006 Section 3] 从 partial_failed 升级为 failed：
        image_upload_status=failed 视为图片确定性失败，整体标记 failed。
        """
        user, channel = user_with_feishu_platform_app_channel
        _override_get_db(db_session)

        try:
            transport = make_asgi_transport(app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                data = await _send_stock_detail(
                    db_session, client, user, test_instrument.id, _make_capture_response()
                )

            test_run_id = data["test_run_id"]
            message_group_id = data["message_group_id"]

            # 第一次处理：卡片段成功，图片 upload 失败
            with patch(
                "app.services.notification_service._fetch_image_bytes",
                new=AsyncMock(return_value=b"fake-png-bytes"),
            ), patch(
                "app.services.feishu_platform_app_adapter.FeishuPlatformAppAdapter.send",
                new=AsyncMock(
                    return_value=DeliveryResult(
                        success=True, provider_response={"mock": "card_ok"}
                    )
                ),
            ) as mock_send, patch(
                "app.services.feishu_platform_app_adapter.FeishuPlatformAppAdapter.send_image_bytes",
                new=AsyncMock(
                    return_value=DeliveryResult(
                        success=False,
                        error_code="IMAGE_UPLOAD_FAILED",
                        error_message="飞书图片上传失败",
                        image_upload_success=False,
                        image_upload_error_code="IMAGE_UPLOAD_FAILED",
                        image_upload_error_message="飞书图片上传失败",
                        provider_response={"code": 10002, "msg": "飞书图片上传失败"},
                    )
                ),
            ) as mock_send_image:
                processed = await process_pending_deliveries(db_session, batch_size=10)
                assert processed == 1, f"应只有卡片段成功，实际成功 {processed} 条"
                assert mock_send.called, "卡片段应调用 adapter.send"
                assert mock_send_image.called, "图片应调用 adapter.send_image_bytes"

            # 验证状态为 failed / image_upload
            deliveries_before = (
                await db_session.execute(
                    select(MessageDelivery).where(
                        MessageDelivery.message_group_id == message_group_id
                    )
                )
            ).scalars().all()
            card_deliveries = [d for d in deliveries_before if d.delivery_type == "card"]
            image_deliveries = [d for d in deliveries_before if d.delivery_type == "image"]
            assert len(card_deliveries) == 1
            assert len(image_deliveries) == 1
            assert card_deliveries[0].status == "success"
            assert image_deliveries[0].status == "retrying"
            assert image_deliveries[0].image_upload_status == "failed"

            transport = make_asgi_transport(app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                status_resp = await client.get(
                    f"/stock-detail-feishu/{test_run_id}/status",
                    headers=_auth_headers(user.id),
                )
            status_data = status_resp.json()
            assert status_data["overall_status"] == "failed"
            assert status_data["failed_step"] == "image_upload"
            assert status_data["error_code"] == "IMAGE_UPLOAD_FAILED"

            # 重试图片：patch send_image_bytes 为成功
            with patch(
                "app.services.notification_service._fetch_image_bytes",
                new=AsyncMock(return_value=b"fake-png-bytes"),
            ), patch(
                "app.services.feishu_platform_app_adapter.FeishuPlatformAppAdapter.send_image_bytes",
                new=AsyncMock(
                    return_value=DeliveryResult(
                        success=True,
                        provider_response={"mock": "image_ok"},
                        image_upload_success=True,
                        image_key="img_123",
                    )
                ),
            ):
                transport = make_asgi_transport(app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    retry_resp = await client.post(
                        f"/stock-detail-feishu/{test_run_id}/retry-image",
                        headers=_auth_headers(user.id),
                    )

            assert retry_resp.status_code == 200, f"重试失败: {retry_resp.text}"
            retry_data = retry_resp.json()
            assert retry_data["retried_count"] == 1
            assert retry_data["image_message_id"] == data["image_message_id"]
            assert len(retry_data["deliveries"]) == 1

            # 重试后状态变为 success
            transport = make_asgi_transport(app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                status_resp = await client.get(
                    f"/stock-detail-feishu/{test_run_id}/status",
                    headers=_auth_headers(user.id),
                )
            status_data = status_resp.json()
            assert status_data["overall_status"] == "success"
            assert status_data["card_status"] == "success"
            assert status_data["image_status"] == "success"
            assert status_data["failed_step"] is None
            assert status_data["error_code"] is None

            # 验证卡片段没有重复
            deliveries_after = (
                await db_session.execute(
                    select(MessageDelivery).where(
                        MessageDelivery.message_group_id == message_group_id
                    )
                )
            ).scalars().all()
            assert len(deliveries_after) == 2, f"重试后仍应只有 2 条 Delivery，实际 {len(deliveries_after)}"
            assert len([d for d in deliveries_after if d.delivery_type == "card"]) == 1
            assert len([d for d in deliveries_after if d.delivery_type == "image"]) == 1
        finally:
            app.dependency_overrides.clear()


class TestStockDetailFeishuStatusOwnership:
    """状态查询权限隔离测试。"""

    @pytest.mark.asyncio
    async def test_user_can_only_query_own_status(
        self,
        db_session: AsyncSession,
        test_instrument,
        user_with_feishu_platform_app_channel,
        user_factory,
    ) -> None:
        """用户 A 发送后，用户 B 查询状态应返回 404。"""
        user_a, _ = user_with_feishu_platform_app_channel
        user_b = await user_factory()
        _override_get_db(db_session)

        try:
            transport = make_asgi_transport(app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                data = await _send_stock_detail(
                    db_session, client, user_a, test_instrument.id, _make_capture_response()
                )
                test_run_id = data["test_run_id"]

                # 用户 B 查询应 404
                status_resp = await client.get(
                    f"/stock-detail-feishu/{test_run_id}/status",
                    headers=_auth_headers(user_b.id),
                )
                assert status_resp.status_code == 404

                # 用户 B 重试应 404
                retry_resp = await client.post(
                    f"/stock-detail-feishu/{test_run_id}/retry-image",
                    headers=_auth_headers(user_b.id),
                )
                assert retry_resp.status_code == 404
        finally:
            app.dependency_overrides.clear()


if __name__ == "__main__":
    # 自测入口：验证测试模块加载与 helper 签名（不连 DB）
    print(f"_make_capture_response={_make_capture_response}")
    print(f"_make_capture_failure_response={_make_capture_failure_response}")
    print(f"_auth_headers={_auth_headers}")
    print("OK: 测试模块加载通过")
