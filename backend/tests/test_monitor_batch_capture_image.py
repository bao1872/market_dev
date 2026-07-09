"""盘中监控图片链路 capture token 与 Outbox 测试。

覆盖：
1. monitor_batch 生成的 capture token 包含完整 claims（type/scope/user_id/instrument_id/event_id）
2. token.instrument_id 与触发股票 inst_id 一致
3. capture worker 返回 image_url 时，生成 delivery_type=image 的 Outbox，含 image_url 与 message_group_id
4. capture 失败（401/403/无 image_url）时，写入 CaptureJob FAILED，不生成 image Outbox
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.core.deps import CAPTURE_SCOPE_STOCK_DETAIL
from app.core.security import decode_token
from app.models.capture_job import CAPTURE_STATUS_FAILED, CAPTURE_STATUS_SUCCEEDED, CaptureJob
from app.models.outbox import Outbox
from app.models.strategy_event import StrategyEvent
from app.services.monitor_batch_service import MonitorBatchService


def _make_event(instrument_id: UUID) -> StrategyEvent:
    """构造最小 mock StrategyEvent（不依赖 DB）。"""
    return StrategyEvent(
        id=uuid4(),
        event_key=f"test-event-{uuid4().hex}",
        strategy_version_id=uuid4(),
        instrument_id=instrument_id,
        event_type="bb_upper_touch",
        event_time=datetime(2026, 7, 7, 10, 30, tzinfo=UTC),
        schema_version=1,
        payload={"price": 100.0},
        snapshot={},
    )


class TestMonitorBatchCaptureTokenClaims:
    """验证 _send_chart_images_via_outbox 生成的 capture token 字段。"""

    @pytest.mark.asyncio
    async def test_capture_token_contains_required_claims(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """token 解码后应包含 type/scope/user_id/instrument_id/event_id。"""
        inst_id = test_instrument.id
        user_id = test_user.id
        event = _make_event(inst_id)
        group_id = str(uuid4())

        captured_payload: dict | None = None

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"image_url": "/static/captures/monitor-test.png"}
        mock_resp.raise_for_status.return_value = None

        mock_client = AsyncMock()

        async def _capture_post(url: str, json: dict | None = None, **kwargs: object) -> MagicMock:
            nonlocal captured_payload
            captured_payload = json
            return mock_resp

        mock_client.post = _capture_post

        service = MonitorBatchService()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await service._send_chart_images_via_outbox(
                db=db_session,
                instrument_events={inst_id: [event]},
                instrument_info_cache={inst_id: (test_instrument.symbol, test_instrument.name)},
                instrument_user_map={inst_id: [user_id]},
                message_group_id=group_id,
            )

        assert captured_payload is not None
        token = captured_payload["token"]
        payload = decode_token(token)
        assert payload["type"] == "capture"
        assert payload["scope"] == CAPTURE_SCOPE_STOCK_DETAIL
        assert payload["user_id"] == str(user_id)
        assert payload["instrument_id"] == str(inst_id)
        assert payload["event_id"] == str(event.id)

    @pytest.mark.asyncio
    async def test_capture_token_instrument_id_matches_request(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """token.instrument_id 必须等于请求中的 instrument_id（capture.py path 一致性校验）。"""
        inst_id = test_instrument.id
        user_id = test_user.id
        event = _make_event(inst_id)
        group_id = str(uuid4())

        captured_payload: dict | None = None

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"image_url": "/static/captures/monitor-test.png"}
        mock_resp.raise_for_status.return_value = None

        mock_client = AsyncMock()

        async def _capture_post(url: str, json: dict | None = None, **kwargs: object) -> MagicMock:
            nonlocal captured_payload
            captured_payload = json
            return mock_resp

        mock_client.post = _capture_post

        service = MonitorBatchService()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await service._send_chart_images_via_outbox(
                db=db_session,
                instrument_events={inst_id: [event]},
                instrument_info_cache={inst_id: (test_instrument.symbol, test_instrument.name)},
                instrument_user_map={inst_id: [user_id]},
                message_group_id=group_id,
            )

        assert captured_payload is not None
        assert captured_payload["instrument_id"] == str(inst_id)
        payload = decode_token(captured_payload["token"])
        assert payload["instrument_id"] == str(inst_id)

    @pytest.mark.asyncio
    async def test_image_outbox_generated_when_image_url_exists(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """截图成功时，应写入 source_type=monitor_chart、delivery_type=image 的 Outbox。"""
        inst_id = test_instrument.id
        user_id = test_user.id
        event = _make_event(inst_id)
        group_id = str(uuid4())
        image_url = "/static/captures/monitor-test.png"

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"image_url": image_url}
        mock_resp.raise_for_status.return_value = None

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        service = MonitorBatchService()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await service._send_chart_images_via_outbox(
                db=db_session,
                instrument_events={inst_id: [event]},
                instrument_info_cache={inst_id: (test_instrument.symbol, test_instrument.name)},
                instrument_user_map={inst_id: [user_id]},
                message_group_id=group_id,
            )

        stmt = (
            select(Outbox)
            .where(Outbox.event_type == "notification.message.created")
            .where(Outbox.payload["delivery_type"].astext == "image")
        )
        result = await db_session.execute(stmt)
        outbox = result.scalar_one_or_none()
        assert outbox is not None
        assert outbox.payload["image_url"] == image_url
        assert outbox.payload["message_group_id"] == group_id
        assert outbox.payload["user_id"] == str(user_id)

    @pytest.mark.asyncio
    async def test_capture_failure_does_not_block_text_notification(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """capture worker 返回 401/403 时，应写 CaptureJob FAILED，不生成 image Outbox，且不抛异常。"""
        inst_id = test_instrument.id
        user_id = test_user.id
        event = _make_event(inst_id)
        group_id = str(uuid4())

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"detail": "Capture Token scope 错误"}
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized",
            request=httpx.Request("POST", "http://capture/capture"),
            response=mock_resp,
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        service = MonitorBatchService()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            # 不抛异常即通过“不影响文字通知”的最低要求
            await service._send_chart_images_via_outbox(
                db=db_session,
                instrument_events={inst_id: [event]},
                instrument_info_cache={inst_id: (test_instrument.symbol, test_instrument.name)},
                instrument_user_map={inst_id: [user_id]},
                message_group_id=group_id,
            )

        stmt = select(CaptureJob).where(CaptureJob.event_id == event.id)
        result = await db_session.execute(stmt)
        job = result.scalar_one_or_none()
        assert job is not None
        assert job.status == CAPTURE_STATUS_FAILED
        assert job.error_code is not None

        stmt_img = (
            select(Outbox)
            .where(Outbox.event_type == "notification.message.created")
            .where(Outbox.payload["delivery_type"].astext == "image")
        )
        result_img = await db_session.execute(stmt_img)
        assert result_img.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_capture_success_writes_capture_job_succeeded(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """截图成功时应写入 capture_jobs=SUCCEEDED 并记录 image_url。"""
        inst_id = test_instrument.id
        user_id = test_user.id
        event = _make_event(inst_id)
        group_id = str(uuid4())
        image_url = "/static/captures/monitor-test.png"

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"image_url": image_url}
        mock_resp.raise_for_status.return_value = None

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        service = MonitorBatchService()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await service._send_chart_images_via_outbox(
                db=db_session,
                instrument_events={inst_id: [event]},
                instrument_info_cache={inst_id: (test_instrument.symbol, test_instrument.name)},
                instrument_user_map={inst_id: [user_id]},
                message_group_id=group_id,
            )

        stmt = select(CaptureJob).where(CaptureJob.event_id == event.id)
        result = await db_session.execute(stmt)
        job = result.scalar_one_or_none()
        assert job is not None
        assert job.status == CAPTURE_STATUS_SUCCEEDED
        assert job.image_url == image_url
        assert job.message_group_id == group_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
