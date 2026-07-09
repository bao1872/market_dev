"""Phase 5 Task 16/17: 个股详情发送飞书测试。

覆盖：
1. 普通用户无需 channel_id 即可发送，后端自动查找唯一 active Feishu 渠道
2. 无 active Feishu 渠道时返回 404
3. Outbox payload 携带 target_channel_id，只创建一条目标投递
4. 手动发送始终附带 StockMemo 内容（不受 notify_feishu 开关影响）
5. 截图失败时文本 Outbox 仍成功，响应 status=pending
6. 状态查询端点返回当前用户自己的分享状态
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
from app.models.notification import MessageDelivery, NotificationChannel, NotificationMessage
from app.models.stock_memo import StockMemo
from app.models.user import User
from app.services.outbox_relay import relay_outbox
from tests.conftest import make_asgi_transport

# ============================================================
# 测试 fixtures
# ============================================================


@pytest_asyncio.fixture
async def user_with_feishu_channel(db_session: AsyncSession, make_user_eligible):
    """创建普通用户 + active 飞书 Platform App 渠道（member 角色 + 有效订阅，有资格进入监控 universe）。"""
    user = User(
        id=uuid.uuid4(),
        email=f"user_{uuid.uuid4().hex[:8]}@test.com",
        password_hash="$2b$12$dummyhash",
        status="active",
        timezone="Asia/Shanghai",
    )
    db_session.add(user)
    await db_session.flush()

    # [eligible_user_service] - 普通用户应有资格进入监控 universe（member + 有效订阅）
    await make_user_eligible(user)

    channel = NotificationChannel(
        id=uuid.uuid4(),
        user_id=user.id,
        adapter_type="feishu_platform_app",
        display_name="测试飞书渠道",
        target_config={"app_id": "cli_test_001", "app_secret": "secret_value", "receive_id": "bg12345", "receive_id_type": "user_id"},
        status="active",
    )
    db_session.add(channel)
    await db_session.flush()

    yield user, channel


@pytest_asyncio.fixture
async def user_with_memo(db_session: AsyncSession, user_with_feishu_channel, test_instrument):
    """为飞书渠道用户创建一条 StockMemo（notify_feishu=False，验证手动发送不受影响）。"""
    user, _ = user_with_feishu_channel
    memo = StockMemo(
        id=uuid.uuid4(),
        user_id=user.id,
        instrument_id=test_instrument.id,
        content="这是测试备忘录内容",
        notify_feishu=False,
    )
    db_session.add(memo)
    await db_session.flush()
    yield memo


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


def _make_image_fetch_response(content: bytes = b"fake-png-bytes") -> MagicMock:
    """构造图片拉取成功响应 mock。"""
    mock_resp = MagicMock()
    mock_resp.content = content
    mock_resp.raise_for_status.return_value = None
    return mock_resp


# ============================================================
# 测试用例
# ============================================================


class TestStockDetailFeishuManualSend:
    """手动发送接口测试（Task 16 + Task 17）。"""

    @pytest.mark.asyncio
    async def test_success_without_channel_id(
        self, db_session, test_instrument, user_with_feishu_channel,
    ) -> None:
        """普通用户不带 channel_id 发送成功，返回 Outbox 追踪 ID。"""
        user, channel = user_with_feishu_channel
        _override_get_db(db_session)

        try:
            capture_resp = _make_capture_response()
            image_fetch_resp = _make_image_fetch_response()

            with patch(
                "app.services.monitor_snapshot_service.compute_all_indicators",
                new=AsyncMock(return_value={
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
                }),
            ), patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=capture_resp)
                mock_client.get = AsyncMock(return_value=image_fetch_resp)
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                transport = make_asgi_transport(app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        f"/instruments/{test_instrument.id}/send-feishu",
                        headers=_auth_headers(user.id),
                        json={},
                    )

            assert response.status_code == 200, f"响应体: {response.text}"
            data = response.json()
            assert "test_run_id" in data
            assert "message_group_id" in data
            assert "message_id" in data
            assert data["status"] == "pending"

            # 验证 Outbox 记录携带 target_channel_id
            from app.models.outbox import Outbox
            outbox_records = (
                await db_session.execute(
                    select(Outbox).where(
                        Outbox.event_type == "notification.message.created",
                        Outbox.payload["target_channel_id"].astext == str(channel.id),
                    )
                )
            ).scalars().all()
            assert len(outbox_records) >= 1, "Outbox 应包含 target_channel_id"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_no_active_feishu_channel_returns_404(
        self, db_session, test_instrument, test_user,
    ) -> None:
        """用户无 active Feishu 渠道时返回 404。"""
        _override_get_db(db_session)

        try:
            transport = make_asgi_transport(app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/instruments/{test_instrument.id}/send-feishu",
                    headers=_auth_headers(test_user.id),
                    json={},
                )

            assert response.status_code == 404, f"响应体: {response.text}"
            data = response.json()
            assert data["detail"]["error_code"] == "CHANNEL_NOT_FOUND"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_endpoint_requires_auth(self, db_session, test_instrument) -> None:
        """未认证访问应返回 401。"""
        transport = make_asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/instruments/{test_instrument.id}/send-feishu",
                json={},
            )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_instrument_not_found_returns_404(
        self, db_session, user_with_feishu_channel,
    ) -> None:
        """不存在的 instrument_id 应返回 404。"""
        user, _ = user_with_feishu_channel
        _override_get_db(db_session)

        try:
            fake_instrument_id = uuid.uuid4()
            transport = make_asgi_transport(app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/instruments/{fake_instrument_id}/send-feishu",
                    headers=_auth_headers(user.id),
                    json={},
                )
            assert response.status_code == 404, f"响应体: {response.text}"
            data = response.json()
            assert data["detail"]["error_code"] == "INSTRUMENT_NOT_FOUND"
        finally:
            app.dependency_overrides.clear()


class TestStockDetailFeishuMemoAndTargetChannel:
    """备忘录附带与单目标投递测试（Task 17）。"""

    @pytest.mark.asyncio
    async def test_manual_send_always_includes_memo(
        self, db_session, test_instrument, user_with_feishu_channel, user_with_memo,
    ) -> None:
        """手动发送时始终附带 StockMemo，notify_feishu=False 不影响。"""
        user, _ = user_with_feishu_channel
        _override_get_db(db_session)

        try:
            capture_resp = _make_capture_response()
            image_fetch_resp = _make_image_fetch_response()

            with patch(
                "app.services.monitor_snapshot_service.compute_all_indicators",
                new=AsyncMock(return_value={
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
                }),
            ), patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=capture_resp)
                mock_client.get = AsyncMock(return_value=image_fetch_resp)
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                transport = make_asgi_transport(app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        f"/instruments/{test_instrument.id}/send-feishu",
                        headers=_auth_headers(user.id),
                        json={},
                    )

            assert response.status_code == 200
            data = response.json()

            # 查询文本消息 body 是否包含备忘录
            result = await db_session.execute(
                select(NotificationMessage).where(NotificationMessage.id == data["message_id"])
            )
            message = result.scalar_one()
            text_content = message.body.get("text_content", "")
            assert "这是测试备忘录内容" in text_content, f"文本内容未包含备忘录: {text_content}"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_target_channel_id_creates_single_delivery(
        self, db_session, test_instrument, user_with_feishu_channel,
    ) -> None:
        """手动发送只创建一个目标投递，不会为其他 active 渠道创建。"""
        user, channel = user_with_feishu_channel

        # 再创建一个非飞书 active 渠道，验证不会被投递
        other_channel = NotificationChannel(
            id=uuid.uuid4(),
            user_id=user.id,
            adapter_type="email",
            display_name="测试邮箱渠道",
            target_config={},
            status="active",
        )
        db_session.add(other_channel)
        await db_session.flush()

        _override_get_db(db_session)

        try:
            capture_resp = _make_capture_response()
            image_fetch_resp = _make_image_fetch_response()

            with patch(
                "app.services.monitor_snapshot_service.compute_all_indicators",
                new=AsyncMock(return_value={
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
                }),
            ), patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=capture_resp)
                mock_client.get = AsyncMock(return_value=image_fetch_resp)
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                transport = make_asgi_transport(app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        f"/instruments/{test_instrument.id}/send-feishu",
                        headers=_auth_headers(user.id),
                        json={},
                    )

            assert response.status_code == 200
            data = response.json()

            # 触发 Outbox Relay 扩张（mock Redis，避免测试环境无 Redis）
            with patch("app.services.outbox_relay.get_redis") as mock_redis:
                mock_redis.return_value = AsyncMock()
                processed = await relay_outbox(db_session, batch_size=10)
            assert processed == 2, "应处理 text + image 两条 Outbox 记录"

            deliveries = (
                await db_session.execute(
                    select(MessageDelivery).where(
                        MessageDelivery.notification_message_id.in_(
                            [data["message_id"], data.get("image_message_id")]
                        )
                    )
                )
            ).scalars().all()

            # image_message_id 可能为 None（截图失败时），但此处截图成功
            assert len(deliveries) == 2, f"应只创建 2 条目标投递，实际 {len(deliveries)}"
            assert all(d.channel_id == channel.id for d in deliveries), "投递应指向唯一飞书渠道"
        finally:
            app.dependency_overrides.clear()


class TestStockDetailFeishuStatus:
    """状态查询端点测试。"""

    @pytest.mark.asyncio
    async def test_status_returns_pending_after_create(
        self, db_session, test_instrument, user_with_feishu_channel,
    ) -> None:
        """创建后立即查询状态应为 pending。"""
        user, _ = user_with_feishu_channel
        _override_get_db(db_session)

        try:
            capture_resp = _make_capture_response()
            image_fetch_resp = _make_image_fetch_response()

            with patch(
                "app.services.monitor_snapshot_service.compute_all_indicators",
                new=AsyncMock(return_value={
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
                }),
            ), patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=capture_resp)
                mock_client.get = AsyncMock(return_value=image_fetch_resp)
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                transport = make_asgi_transport(app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    create_resp = await client.post(
                        f"/instruments/{test_instrument.id}/send-feishu",
                        headers=_auth_headers(user.id),
                        json={},
                    )
                    assert create_resp.status_code == 200
                    test_run_id = create_resp.json()["test_run_id"]

                    status_resp = await client.get(
                        f"/stock-detail-feishu/{test_run_id}/status",
                        headers=_auth_headers(user.id),
                    )

            assert status_resp.status_code == 200
            status_data = status_resp.json()
            assert status_data["overall_status"] == "pending"
            assert status_data["card_status"] == "pending"
        finally:
            app.dependency_overrides.clear()
