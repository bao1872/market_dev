"""notification_service.test_channel_latest_event 图片链路 capture token 测试。

覆盖：
1. test_channel_latest_event 生成的 capture token 包含完整 claims
   （type/scope/user_id/instrument_id/event_id）
2. token.instrument_id 等于事件对应的 instrument_id
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.deps import CAPTURE_SCOPE_STOCK_DETAIL
from app.core.security import decode_token
from app.models.notification import NotificationChannel
from app.models.strategy_event import StrategyEvent
from app.models.watchlist import UserWatchlistItem
from app.services.notification_service import (
    test_channel_latest_event as _test_channel_latest_event,
)


class TestChannelLatestEventCaptureToken:
    """验证 test_channel_latest_event 调用截图服务时携带的 capture token。"""

    @pytest.mark.asyncio
    async def test_channel_latest_event_capture_token_has_full_claims(
        self, db_session, test_user, test_instrument, test_selector_strategy,
    ) -> None:
        """token 解码后应包含 type/scope/user_id/instrument_id/event_id。"""
        version = test_selector_strategy["version"]
        event = StrategyEvent(
            event_key="test:latest:event:capture:1",
            strategy_version_id=version.id,
            instrument_id=test_instrument.id,
            event_type="bb_upper_touch",
            event_time=datetime.now(UTC),
            schema_version=1,
            payload={"price": 100.0},
        )
        watchlist_item = UserWatchlistItem(
            user_id=test_user.id,
            instrument_id=test_instrument.id,
            source="test",
            active=True,
        )
        channel = NotificationChannel(
            user_id=test_user.id,
            adapter_type="mock",
            display_name="Mock渠道",
            target_config={},
            status="active",
        )
        db_session.add_all([event, watchlist_item, channel])
        await db_session.flush()

        captured_payload: dict | None = None

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"image_url": "/static/captures/latest-test.png"}
        mock_resp.raise_for_status.return_value = None

        mock_client = AsyncMock()

        async def _capture_post(url: str, json: dict | None = None, **kwargs: object) -> MagicMock:
            nonlocal captured_payload
            captured_payload = json
            return mock_resp

        mock_client.post = _capture_post

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await _test_channel_latest_event(
                db=db_session,
                channel_id=channel.id,
                frontend_base_url="http://test",
                capture_worker_url="http://test-capture",
                capture_token_ttl_seconds=300,
            )

        assert captured_payload is not None
        token = captured_payload["token"]
        payload = decode_token(token)
        assert payload["type"] == "capture"
        assert payload["scope"] == CAPTURE_SCOPE_STOCK_DETAIL
        assert payload["user_id"] == str(test_user.id)
        assert payload["instrument_id"] == str(test_instrument.id)
        assert payload["event_id"] == str(event.id)

    @pytest.mark.asyncio
    async def test_channel_latest_event_capture_token_instrument_id_matches_event(
        self, db_session, test_user, test_instrument, test_selector_strategy,
    ) -> None:
        """capture payload 和 token 中的 instrument_id 必须与事件标的一致。"""
        version = test_selector_strategy["version"]
        event = StrategyEvent(
            event_key="test:latest:event:capture:2",
            strategy_version_id=version.id,
            instrument_id=test_instrument.id,
            event_type="bb_lower_touch",
            event_time=datetime.now(UTC),
            schema_version=1,
            payload={"price": 50.0},
        )
        watchlist_item = UserWatchlistItem(
            user_id=test_user.id,
            instrument_id=test_instrument.id,
            source="test",
            active=True,
        )
        channel = NotificationChannel(
            user_id=test_user.id,
            adapter_type="mock",
            display_name="Mock渠道",
            target_config={},
            status="active",
        )
        db_session.add_all([event, watchlist_item, channel])
        await db_session.flush()

        captured_payload: dict | None = None

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"image_url": "/static/captures/latest-test.png"}
        mock_resp.raise_for_status.return_value = None

        mock_client = AsyncMock()

        async def _capture_post(url: str, json: dict | None = None, **kwargs: object) -> MagicMock:
            nonlocal captured_payload
            captured_payload = json
            return mock_resp

        mock_client.post = _capture_post

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await _test_channel_latest_event(
                db=db_session,
                channel_id=channel.id,
                frontend_base_url="http://test",
                capture_worker_url="http://test-capture",
                capture_token_ttl_seconds=300,
            )

        assert captured_payload is not None
        assert captured_payload["instrument_id"] == str(test_instrument.id)
        payload = decode_token(captured_payload["token"])
        assert payload["instrument_id"] == str(test_instrument.id)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
