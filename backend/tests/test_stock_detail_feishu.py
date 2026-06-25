"""Phase 8: 个股详情发送飞书测试 - 复用监控链路。

覆盖 3 个场景：
1. 成功路径：4 个布尔全 true（text_ok/screenshot_ok/image_upload_ok/feishu_send_ok）
2. capture 失败：screenshot_ok=false, image_upload_ok=false, feishu_send_ok=false, text_ok=true
3. 飞书发送失败：feishu_send_ok=false（前 3 步 true）

测试策略：
- 复用 conftest.py 的 PostgreSQL 测试库 + db_session fixture
- 创建 admin 用户 + admin 角色以满足 require_roles("admin")
- mock compute_all_indicators（避免依赖真实行情数据）
- mock httpx.AsyncClient（模拟 capture worker 截图 + 图片拉取）
- mock get_adapter（模拟飞书适配器投递结果）
- 通过 dependency_overrides 注入测试会话
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.main import app
from app.models.notification import NotificationChannel
from app.models.user import Role, User, UserRole


# ============================================================
# 测试 fixtures
# ============================================================


@pytest_asyncio.fixture
async def admin_user_with_channel(db_session: AsyncSession):
    """创建 admin 用户 + admin 角色 + active 飞书渠道。

    返回 (admin_user, channel) 元组，供测试使用。

    [个股飞书测试] - 描述: 幂等复用现有 admin 角色，避免重复创建违反 roles_name_key 唯一约束
    （测试库可能因历史测试残留已持久化 admin 角色，与 membership_service.py line 189 同款模式）
    """
    from sqlalchemy import select as sa_select

    # 先查询现有 admin 角色，存在则复用（与 membership_service.py 同款模式）
    role_stmt = sa_select(Role).where(Role.name == "admin")
    role_result = await db_session.execute(role_stmt)
    admin_role = role_result.scalar_one_or_none()
    if admin_role is None:
        admin_role = Role(id=uuid.uuid4(), name="admin", description="管理员")
        db_session.add(admin_role)

    admin_user = User(
        id=uuid.uuid4(),
        email=f"admin_{uuid.uuid4().hex[:8]}@test.com",
        password_hash="$2b$12$dummyhash",
        status="active",
        timezone="Asia/Shanghai",
    )
    db_session.add(admin_user)
    db_session.add(UserRole(user_id=admin_user.id, role_id=admin_role.id))
    await db_session.flush()

    channel = NotificationChannel(
        id=uuid.uuid4(),
        user_id=admin_user.id,
        adapter_type="mock",
        display_name="测试飞书渠道",
        target_config={},
        status="active",
    )
    db_session.add(channel)
    await db_session.flush()

    yield admin_user, channel


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _override_get_db(session: AsyncSession) -> None:
    """覆盖 app 的 get_db 依赖，使其使用测试会话。

    [个股飞书测试] - 描述: 将 endpoint 的 db.commit() 替换为 flush，
    保持 db_session fixture 的 SAVEPOINT（begin_nested）活跃，
    供 teardown 的 nested.rollback() 正常回滚。
    否则 endpoint 的 commit 会关闭 SAVEPOINT，导致 teardown 抛 ResourceClosedError。
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


class TestStockDetailFeishuSuccess:
    """成功路径测试。"""

    @pytest.mark.asyncio
    async def test_send_feishu_success_all_four_booleans_true(
        self, db_session, test_instrument, admin_user_with_channel,
    ) -> None:
        """成功路径：4 个布尔全 true。"""
        admin_user, channel = admin_user_with_channel
        _override_get_db(db_session)

        try:
            fake_indicators = {
                "layers": [],
                "data": {},
                "snapshot": {
                    "current_price": 25.50,
                    "bb_upper": 27.00,
                    "bb_mid": 25.00,
                    "bb_lower": 23.00,
                },
            }

            from app.schemas.notification import DeliveryResult
            from app.services.channel_adapter import ChannelAdapter

            class _FakeAdapter(ChannelAdapter):
                adapter_type = "mock"

                async def send(self, message_dto, target_config):
                    return DeliveryResult(success=True, provider_response={"mock": True})

                async def send_text_message(self, message_dto, target_config):
                    return DeliveryResult(success=True, provider_response={"mock_text": True})

                async def send_image_bytes(self, image_bytes, target_config):
                    return DeliveryResult(success=True, provider_response={"mock_image": True})

                async def verify(self, target_config):
                    return True

            capture_resp = _make_capture_response()
            image_fetch_resp = _make_image_fetch_response()

            with patch(
                "app.services.stock_detail_feishu_service.compute_all_indicators",
                new=AsyncMock(return_value=fake_indicators),
            ), patch(
                "app.services.stock_detail_feishu_service.get_adapter",
                return_value=_FakeAdapter(),
            ), patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=capture_resp)
                mock_client.get = AsyncMock(return_value=image_fetch_resp)
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        f"/admin/instruments/{test_instrument.id}/send-feishu",
                        headers=_auth_headers(admin_user.id),
                        json={"channel_id": str(channel.id)},
                    )

            assert response.status_code == 200, f"响应体: {response.text}"
            data = response.json()
            assert data["text_ok"] is True, f"text_ok 应为 true, 完整响应: {data}"
            assert data["screenshot_ok"] is True, f"screenshot_ok 应为 true, 完整响应: {data}"
            assert data["image_upload_ok"] is True, f"image_upload_ok 应为 true, 完整响应: {data}"
            assert data["feishu_send_ok"] is True, f"feishu_send_ok 应为 true, 完整响应: {data}"
        finally:
            app.dependency_overrides.clear()


class TestStockDetailFeishuCaptureFailure:
    """capture 失败测试。"""

    @pytest.mark.asyncio
    async def test_capture_failure_only_text_ok(
        self, db_session, test_instrument, admin_user_with_channel,
    ) -> None:
        """capture 失败：screenshot_ok=false, image_upload_ok=false, feishu_send_ok=false, text_ok=true。"""
        admin_user, channel = admin_user_with_channel
        _override_get_db(db_session)

        try:
            from app.schemas.notification import DeliveryResult
            from app.services.channel_adapter import ChannelAdapter

            class _FakeAdapter(ChannelAdapter):
                adapter_type = "mock"

                async def send(self, message_dto, target_config):
                    return DeliveryResult(success=True, provider_response={"mock": True})

                async def send_text_message(self, message_dto, target_config):
                    return DeliveryResult(success=True, provider_response={"mock_text": True})

                async def send_image_bytes(self, image_bytes, target_config):
                    return DeliveryResult(success=True, provider_response={"mock_image": True})

                async def verify(self, target_config):
                    return True

            fake_indicators = {"layers": [], "data": {}, "snapshot": {}}

            with patch(
                "app.services.stock_detail_feishu_service.compute_all_indicators",
                new=AsyncMock(return_value=fake_indicators),
            ), patch(
                "app.services.stock_detail_feishu_service.get_adapter",
                return_value=_FakeAdapter(),
            ), patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(side_effect=Exception("capture worker unreachable"))
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        f"/admin/instruments/{test_instrument.id}/send-feishu",
                        headers=_auth_headers(admin_user.id),
                        json={"channel_id": str(channel.id)},
                    )

            assert response.status_code == 200, f"响应体: {response.text}"
            data = response.json()
            assert data["text_ok"] is True, "文本投递应先完成"
            assert data["screenshot_ok"] is False, "截图失败应为 false"
            assert data["image_upload_ok"] is False, "无截图 URL 时图片拉取应为 false"
            assert data["feishu_send_ok"] is False, "无图片时飞书发送应为 false"
        finally:
            app.dependency_overrides.clear()


class TestStockDetailFeishuSendFailure:
    """飞书发送失败测试。"""

    @pytest.mark.asyncio
    async def test_feishu_send_failure_first_three_ok(
        self, db_session, test_instrument, admin_user_with_channel,
    ) -> None:
        """飞书发送失败：feishu_send_ok=false（前 3 步 true）。"""
        admin_user, channel = admin_user_with_channel
        _override_get_db(db_session)

        try:
            from app.schemas.notification import DeliveryResult
            from app.services.channel_adapter import ChannelAdapter

            class _FakeAdapter(ChannelAdapter):
                adapter_type = "mock"

                async def send(self, message_dto, target_config):
                    return DeliveryResult(success=True, provider_response={"mock": True})

                async def send_text_message(self, message_dto, target_config):
                    return DeliveryResult(success=True, provider_response={"mock_text": True})

                async def send_image_bytes(self, image_bytes, target_config):
                    return DeliveryResult(
                        success=False,
                        error_code="FEISHU_SEND_FAILED",
                        error_message="飞书图片消息发送失败",
                    )

                async def verify(self, target_config):
                    return True

            fake_indicators = {"layers": [], "data": {}, "snapshot": {}}
            capture_resp = _make_capture_response()
            image_fetch_resp = _make_image_fetch_response()

            with patch(
                "app.services.stock_detail_feishu_service.compute_all_indicators",
                new=AsyncMock(return_value=fake_indicators),
            ), patch(
                "app.services.stock_detail_feishu_service.get_adapter",
                return_value=_FakeAdapter(),
            ), patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=capture_resp)
                mock_client.get = AsyncMock(return_value=image_fetch_resp)
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        f"/admin/instruments/{test_instrument.id}/send-feishu",
                        headers=_auth_headers(admin_user.id),
                        json={"channel_id": str(channel.id)},
                    )

            assert response.status_code == 200, f"响应体: {response.text}"
            data = response.json()
            assert data["text_ok"] is True, "文本投递应成功"
            assert data["screenshot_ok"] is True, "截图应成功"
            assert data["image_upload_ok"] is True, "图片拉取应成功"
            assert data["feishu_send_ok"] is False, "飞书图片消息发送应失败"
        finally:
            app.dependency_overrides.clear()


class TestStockDetailFeishuAuthAndValidation:
    """认证与参数校验测试。"""

    @pytest.mark.asyncio
    async def test_endpoint_requires_auth(self, db_session, test_instrument) -> None:
        """未认证访问应返回 401。"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/admin/instruments/{test_instrument.id}/send-feishu",
                json={"channel_id": str(uuid.uuid4())},
            )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_instrument_not_found_returns_404(
        self, db_session, admin_user_with_channel,
    ) -> None:
        """不存在的 instrument_id 应返回 404。"""
        admin_user, channel = admin_user_with_channel
        _override_get_db(db_session)

        try:
            fake_instrument_id = uuid.uuid4()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/admin/instruments/{fake_instrument_id}/send-feishu",
                    headers=_auth_headers(admin_user.id),
                    json={"channel_id": str(channel.id)},
                )
            assert response.status_code == 404, f"响应体: {response.text}"
        finally:
            app.dependency_overrides.clear()
