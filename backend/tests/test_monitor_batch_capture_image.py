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


class TestMonitorBatchCaptureTimeframe:
    """飞书盘中截图业务默认周期断言（CHANGE-20260710-002）。"""

    @pytest.mark.asyncio
    async def test_capture_payload_timeframe_is_daily(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """自动盘中监控截图 capture_payload 的 timeframe 必须是业务默认 '1d'（非 15m）。

        实时性由 Capture Snapshot 1d + include_realtime=True 的 partial daily 合成保证，
        截图修复不得改变 watchlist_monitor 事件计算口径。
        """
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
        assert captured_payload["timeframe"] == "1d"
        # 截图修复保留的字段不得丢失
        assert captured_payload["capture_run_id"] is not None
        assert captured_payload["source_bar_time"] is not None
        assert captured_payload["disable_cache"] is True


class TestMonitorBatchCaptureIndicatorView:
    """[CHANGE-20260720-003 §三] 监控自动发送时 capture_payload 必须携带 indicator_view。

    监控事件自动发送时不要求用户选择，由 event_type → indicator_view 映射决定。
    - bb_*_touch 事件 → bollinger
    - node_cluster_touch 事件 → node_cluster
    - smc_* 事件 → smc
    capture_payload.indicator_view 贯穿：截图 URL / 缓存键 / output_filename / CaptureJob 记录。
    """

    @pytest.mark.asyncio
    async def test_capture_payload_includes_indicator_view_for_bb_event(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """bb_upper_touch 事件 → capture_payload.indicator_view == 'bollinger'。"""
        inst_id = test_instrument.id
        user_id = test_user.id
        event = _make_event(inst_id)  # event_type=bb_upper_touch
        group_id = str(uuid4())

        captured_payload: dict | None = None

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"image_url": "/static/captures/monitor-bb.png"}
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
        # [CHANGE-20260720-003 §三] bb_upper_touch 应映射到 bollinger
        assert captured_payload["indicator_view"] == "bollinger"
        # output_filename 应含 indicator_view 后缀，防止不同指标复用旧图
        assert "bollinger" in captured_payload["output_filename"], (
            f"output_filename 应含 'bollinger': {captured_payload['output_filename']}"
        )
        # capture_run_id 应含 indicator_view 维度
        assert "bollinger" in captured_payload["capture_run_id"], (
            f"capture_run_id 应含 'bollinger': {captured_payload['capture_run_id']}"
        )

        # CaptureJob 应记录 indicator_view
        stmt = select(CaptureJob).where(CaptureJob.event_id == event.id)
        result = await db_session.execute(stmt)
        job = result.scalar_one_or_none()
        assert job is not None
        assert job.indicator_view == "bollinger"

    @pytest.mark.asyncio
    async def test_capture_payload_includes_indicator_view_for_node_event(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """node_cluster_touch 事件 → capture_payload.indicator_view == 'node_cluster'。"""
        inst_id = test_instrument.id
        user_id = test_user.id
        # 直接构造 node_cluster_touch 事件（_make_event 默认是 bb_upper_touch）
        event = StrategyEvent(
            id=uuid4(),
            event_key=f"test-event-{uuid4().hex}",
            strategy_version_id=uuid4(),
            instrument_id=inst_id,
            event_type="node_cluster_touch",
            event_time=datetime(2026, 7, 7, 10, 30, tzinfo=UTC),
            schema_version=1,
            payload={"price": 100.0},
            snapshot={},
        )
        group_id = str(uuid4())

        captured_payload: dict | None = None

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"image_url": "/static/captures/monitor-node.png"}
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
        assert captured_payload["indicator_view"] == "node_cluster"
        assert "node_cluster" in captured_payload["output_filename"]

        # CaptureJob 应记录 indicator_view
        stmt = select(CaptureJob).where(CaptureJob.event_id == event.id)
        result = await db_session.execute(stmt)
        job = result.scalar_one_or_none()
        assert job is not None
        assert job.indicator_view == "node_cluster"

    @pytest.mark.asyncio
    async def test_capture_payload_includes_indicator_view_for_smc_event(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """smc_bos_retest 事件 → capture_payload.indicator_view == 'smc'。"""
        inst_id = test_instrument.id
        user_id = test_user.id
        event = StrategyEvent(
            id=uuid4(),
            event_key=f"test-event-{uuid4().hex}",
            strategy_version_id=uuid4(),
            instrument_id=inst_id,
            event_type="smc_bos_retest",
            event_time=datetime(2026, 7, 7, 10, 30, tzinfo=UTC),
            schema_version=1,
            payload={"price": 100.0, "indicator_view": "smc"},
            snapshot={},
        )
        group_id = str(uuid4())

        captured_payload: dict | None = None

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"image_url": "/static/captures/monitor-smc.png"}
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
        # payload.indicator_view 优先；缺失时由 event_type=smc_bos_retest → smc 映射
        assert captured_payload["indicator_view"] == "smc"
        assert "smc" in captured_payload["output_filename"]

        # CaptureJob 应记录 indicator_view
        stmt = select(CaptureJob).where(CaptureJob.event_id == event.id)
        result = await db_session.execute(stmt)
        job = result.scalar_one_or_none()
        assert job is not None
        assert job.indicator_view == "smc"

    @pytest.mark.asyncio
    async def test_capture_failure_records_indicator_view(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """截图失败时 CaptureJob 也应记录 indicator_view 便于状态查询区分。"""
        inst_id = test_instrument.id
        user_id = test_user.id
        event = _make_event(inst_id)  # event_type=bb_upper_touch → bollinger
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
        # 即使截图失败，CaptureJob 也应记录 indicator_view
        assert job.indicator_view == "bollinger"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
