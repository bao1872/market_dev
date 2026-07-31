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
    """构造最小 mock StrategyEvent（不依赖 DB）。

    [CHANGE-20260728-010] 默认事件类型从 bb_upper_touch 改为 smc_bos_retest：
    BB 事件已不再触发截图（is_supported_event_type 返回 False），
    测试需要使用支持的事件类型才能走完整截图链路。
    """
    return StrategyEvent(
        id=uuid4(),
        event_key=f"test-event-{uuid4().hex}",
        strategy_version_id=uuid4(),
        instrument_id=instrument_id,
        event_type="smc_bos_retest",
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
    """[CHANGE-20260728-010] 监控自动发送时 capture_payload.indicator_view 固定为 structure_node。

    [CHANGE-20260728-010] 截图视图统一为 FEISHU_CAPTURE_VIEW='structure_node'：
    - 任一结构事件或筹码共识事件触发时，截图固定同时展示"结构 + 筹码共识"
    - 事件类别只决定 focus_event 与文字内容，不再决定截图图层组合
    - BB 事件已不再触发截图（is_supported_event_type 返回 False，显式跳过）
    capture_payload.indicator_view 贯穿：截图 URL / 缓存键 / output_filename / CaptureJob 记录。
    """

    @pytest.mark.asyncio
    async def test_capture_payload_includes_indicator_view_for_bb_event(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """[CHANGE-20260728-010] bb_upper_touch 事件已不再触发截图（is_supported_event_type=False）。

        BB 事件被显式跳过：不调用 capture worker，写入 CaptureJob FAILED + indicator_view=None。
        """
        inst_id = test_instrument.id
        user_id = test_user.id
        # 显式构造 bb_upper_touch 事件（_make_event 默认已改为 smc_bos_retest）
        event = StrategyEvent(
            id=uuid4(),
            event_key=f"test-event-{uuid4().hex}",
            strategy_version_id=uuid4(),
            instrument_id=inst_id,
            event_type="bb_upper_touch",
            event_time=datetime(2026, 7, 7, 10, 30, tzinfo=UTC),
            schema_version=1,
            payload={"price": 100.0},
            snapshot={},
        )
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

        # [CHANGE-20260728-010] BB 事件不被支持：capture worker 不应被调用
        assert captured_payload is None
        # CaptureJob 应记录 FAILED + indicator_view=None（未映射事件类型）
        stmt = select(CaptureJob).where(CaptureJob.event_id == event.id)
        result = await db_session.execute(stmt)
        job = result.scalar_one_or_none()
        assert job is not None
        assert job.status == CAPTURE_STATUS_FAILED
        assert job.indicator_view is None

    @pytest.mark.asyncio
    async def test_capture_payload_includes_indicator_view_for_node_event(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """[CHANGE-20260728-010] node_cluster_touch 事件 → indicator_view == 'structure_node'（统一组合视图）。"""
        inst_id = test_instrument.id
        user_id = test_user.id
        # 直接构造 node_cluster_touch 事件
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
        # [CHANGE-20260728-010] 所有支持的事件类型统一映射到 structure_node
        assert captured_payload["indicator_view"] == "structure_node"
        assert "structure_node" in captured_payload["output_filename"]

        # CaptureJob 应记录 indicator_view
        stmt = select(CaptureJob).where(CaptureJob.event_id == event.id)
        result = await db_session.execute(stmt)
        job = result.scalar_one_or_none()
        assert job is not None
        assert job.indicator_view == "structure_node"

    @pytest.mark.asyncio
    async def test_capture_payload_includes_indicator_view_for_smc_event(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """[CHANGE-20260728-010] smc_bos_retest 事件 → indicator_view == 'structure_node'（统一组合视图）。"""
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
            payload={"price": 100.0},
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
        # [CHANGE-20260728-010] 所有支持事件类型统一映射到 structure_node
        assert captured_payload["indicator_view"] == "structure_node"
        assert "structure_node" in captured_payload["output_filename"]

        # CaptureJob 应记录 indicator_view
        stmt = select(CaptureJob).where(CaptureJob.event_id == event.id)
        result = await db_session.execute(stmt)
        job = result.scalar_one_or_none()
        assert job is not None
        assert job.indicator_view == "structure_node"

    @pytest.mark.asyncio
    async def test_capture_failure_records_indicator_view(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """[CHANGE-20260728-010] 截图失败时 CaptureJob 也应记录 indicator_view=structure_node。"""
        inst_id = test_instrument.id
        user_id = test_user.id
        event = _make_event(inst_id)  # event_type=smc_bos_retest → structure_node
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
        # [CHANGE-20260728-010] 即使截图失败，CaptureJob 也应记录 structure_node
        assert job.indicator_view == "structure_node"


class TestMonitorBatchCapturePerEvent:
    """[Gate3 图片修复] 每事件独立截图 + 失败隔离 + 幂等键含 event_id+indicator_view。

    覆盖六类场景：
    1. 一股票 5 类结构事件 → 5 次 capture 请求、5 个 CaptureJob、5 个 image Outbox
    2. 两用户 → 每事件截图 1 次、每用户各有 image Outbox
    3. 一个截图失败 → 文字和其他 4 图继续
    4. 同事件重试无重复（NotificationMessage 幂等）
    5. 同分钟两个事件均发送（幂等键含 event_id，不被分钟级去重吞掉）
    6. 未映射事件类型显式失败，不调用 capture worker、不生成 node 图
    """

    _SMC_EVENT_TYPES: list[str] = [
        "smc_bos_retest",
        "smc_choch_retest",
        "smc_equal_highs_retest",
        "smc_equal_lows_retest",
        "smc_order_block_first_touch",
    ]

    def _make_smc_event(
        self,
        instrument_id: UUID,
        event_type: str,
        event_time: datetime | None = None,
    ) -> StrategyEvent:
        """[CHANGE-20260728-010] 构造 SMC 事件（payload 不再含 indicator_view，固定 structure_node）。"""
        return StrategyEvent(
            id=uuid4(),
            event_key=f"test-event-{uuid4().hex}",
            strategy_version_id=uuid4(),
            instrument_id=instrument_id,
            event_type=event_type,
            event_time=event_time or datetime(2026, 7, 27, 10, 30, tzinfo=UTC),
            schema_version=1,
            payload={"price": 100.0},
            snapshot={},
        )

    def _make_unknown_event(
        self, instrument_id: UUID, event_time: datetime | None = None,
    ) -> StrategyEvent:
        """构造未映射事件类型（不在 EVENT_TYPE_TO_INDICATOR_VIEW 中）。"""
        return StrategyEvent(
            id=uuid4(),
            event_key=f"test-event-{uuid4().hex}",
            strategy_version_id=uuid4(),
            instrument_id=instrument_id,
            event_type="future_unknown_event_type",
            event_time=event_time or datetime(2026, 7, 27, 10, 30, tzinfo=UTC),
            schema_version=1,
            payload={"price": 100.0},
            snapshot={},
        )

    def _build_mock_capture_client(
        self,
        capture_payloads: list[dict] | None = None,
        failure_indices: set[int] | None = None,
    ) -> tuple[AsyncMock, MagicMock, list[dict]]:
        """构造 mock httpx.AsyncClient，记录所有 capture 请求。

        Args:
            capture_payloads: 已有列表（用于跨调用累积）
            failure_indices: 指定哪些请求索引触发 500 错误

        Returns:
            (mock_client, mock_resp, captured_payloads)
        """
        captured: list[dict] = capture_payloads if capture_payloads is not None else []
        failure_idx = failure_indices or set()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"image_url": "/static/captures/monitor-test.png"}
        mock_resp.raise_for_status.return_value = None

        mock_client = AsyncMock()

        async def _capture_post(url: str, json: dict | None = None, **kwargs: object) -> MagicMock:
            nonlocal captured
            idx = len(captured)
            captured.append(json or {})
            if idx in failure_idx:
                # 模拟 500 错误
                err_resp = MagicMock()
                err_resp.status_code = 500
                err_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                    "Internal Server Error",
                    request=httpx.Request("POST", "http://capture/capture"),
                    response=err_resp,
                )
                return err_resp
            return mock_resp

        mock_client.post = _capture_post
        return mock_client, mock_resp, captured

    @pytest.mark.asyncio
    async def test_five_smc_events_generate_five_captures_jobs_outboxes(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """场景 1：一股票同时 5 类结构事件 → 5 次 capture 请求、5 个 CaptureJob、5 个 image Outbox。"""
        inst_id = test_instrument.id
        user_id = test_user.id
        events = [
            self._make_smc_event(inst_id, et) for et in self._SMC_EVENT_TYPES
        ]
        group_id = str(uuid4())

        captured_payloads: list[dict] = []
        mock_client, _, _ = self._build_mock_capture_client(captured_payloads)

        service = MonitorBatchService()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await service._send_chart_images_via_outbox(
                db=db_session,
                instrument_events={inst_id: events},
                instrument_info_cache={inst_id: (test_instrument.symbol, test_instrument.name)},
                instrument_user_map={inst_id: [user_id]},
                message_group_id=group_id,
            )

        # 5 次 capture 请求
        assert len(captured_payloads) == 5, (
            f"应发起 5 次 capture 请求，实际 {len(captured_payloads)}"
        )

        # 每个 capture_run_id 唯一（含 event_id + indicator_view）
        run_ids = {p["capture_run_id"] for p in captured_payloads}
        assert len(run_ids) == 5, f"capture_run_id 应唯一：{run_ids}"

        # 每个事件 ID 都出现在 capture 请求中
        event_ids_in_payload = {p["event_id"] for p in captured_payloads}
        expected_event_ids = {str(e.id) for e in events}
        assert event_ids_in_payload == expected_event_ids, (
            f"capture 请求事件 ID 不匹配：{event_ids_in_payload} vs {expected_event_ids}"
        )

        # 5 个 CaptureJob（全部 SUCCEEDED）
        stmt_jobs = select(CaptureJob).where(CaptureJob.instrument_id == inst_id)
        result_jobs = await db_session.execute(stmt_jobs)
        jobs = list(result_jobs.scalars().all())
        assert len(jobs) == 5
        assert all(j.status == CAPTURE_STATUS_SUCCEEDED for j in jobs)
        assert all(j.indicator_view == "structure_node" for j in jobs)

        # 5 个 image Outbox
        stmt_img = (
            select(Outbox)
            .where(Outbox.event_type == "notification.message.created")
            .where(Outbox.payload["delivery_type"].astext == "image")
        )
        result_img = await db_session.execute(stmt_img)
        outboxes = list(result_img.scalars().all())
        assert len(outboxes) == 5, f"应 5 个 image Outbox，实际 {len(outboxes)}"

    @pytest.mark.asyncio
    async def test_two_users_one_capture_per_event_but_outbox_per_user(
        self, db_session, user_factory, test_instrument,
    ) -> None:
        """场景 2：两用户 → 每事件截图 1 次、每用户各有 image Outbox。"""
        inst_id = test_instrument.id
        user1 = await user_factory()
        user2 = await user_factory()
        user_ids = [user1.id, user2.id]

        # 用 2 个事件，便于断言
        events = [
            self._make_smc_event(inst_id, "smc_bos_retest"),
            self._make_smc_event(inst_id, "smc_choch_retest"),
        ]
        group_id = str(uuid4())

        captured_payloads: list[dict] = []
        mock_client, _, _ = self._build_mock_capture_client(captured_payloads)

        service = MonitorBatchService()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await service._send_chart_images_via_outbox(
                db=db_session,
                instrument_events={inst_id: events},
                instrument_info_cache={inst_id: (test_instrument.symbol, test_instrument.name)},
                instrument_user_map={inst_id: user_ids},
                message_group_id=group_id,
            )

        # 2 个事件 → 2 次 capture 请求（同一事件多用户只截图一次）
        assert len(captured_payloads) == 2, (
            f"2 事件应只发 2 次 capture 请求（多用户共享截图），实际 {len(captured_payloads)}"
        )

        # 2 个 CaptureJob（每事件 1 个）
        stmt_jobs = select(CaptureJob).where(CaptureJob.instrument_id == inst_id)
        result_jobs = await db_session.execute(stmt_jobs)
        jobs = list(result_jobs.scalars().all())
        assert len(jobs) == 2

        # 4 个 image Outbox（2 事件 × 2 用户）
        stmt_img = (
            select(Outbox)
            .where(Outbox.event_type == "notification.message.created")
            .where(Outbox.payload["delivery_type"].astext == "image")
        )
        result_img = await db_session.execute(stmt_img)
        outboxes = list(result_img.scalars().all())
        assert len(outboxes) == 4, f"应 4 个 image Outbox（2 事件 × 2 用户），实际 {len(outboxes)}"

        # 每个用户各有 2 个 Outbox
        outbox_user_ids = {o.payload["user_id"] for o in outboxes}
        assert outbox_user_ids == {str(user1.id), str(user2.id)}, (
            f"Outbox user_id 集合异常：{outbox_user_ids}"
        )

    @pytest.mark.asyncio
    async def test_one_capture_failure_does_not_block_other_events(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """场景 3：一个截图失败 → 文字和其他 4 图继续（失败隔离）。"""
        inst_id = test_instrument.id
        user_id = test_user.id
        events = [
            self._make_smc_event(inst_id, et) for et in self._SMC_EVENT_TYPES
        ]
        group_id = str(uuid4())

        captured_payloads: list[dict] = []
        # 第 0 个事件（BOS）截图失败
        mock_client, _, _ = self._build_mock_capture_client(
            captured_payloads, failure_indices={0},
        )

        service = MonitorBatchService()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            # 不抛异常即通过"失败隔离"最低要求
            await service._send_chart_images_via_outbox(
                db=db_session,
                instrument_events={inst_id: events},
                instrument_info_cache={inst_id: (test_instrument.symbol, test_instrument.name)},
                instrument_user_map={inst_id: [user_id]},
                message_group_id=group_id,
            )

        # 5 次 capture 请求都发起（失败的事件也调用了 capture worker）
        assert len(captured_payloads) == 5

        # 5 个 CaptureJob（1 FAILED + 4 SUCCEEDED）
        stmt_jobs = select(CaptureJob).where(CaptureJob.instrument_id == inst_id)
        result_jobs = await db_session.execute(stmt_jobs)
        jobs = list(result_jobs.scalars().all())
        assert len(jobs) == 5
        failed_jobs = [j for j in jobs if j.status == CAPTURE_STATUS_FAILED]
        succeeded_jobs = [j for j in jobs if j.status == CAPTURE_STATUS_SUCCEEDED]
        assert len(failed_jobs) == 1, f"应 1 个失败 CaptureJob，实际 {len(failed_jobs)}"
        assert len(succeeded_jobs) == 4, f"应 4 个成功 CaptureJob，实际 {len(succeeded_jobs)}"
        # 失败的 CaptureJob 应记录 error_code
        assert failed_jobs[0].error_code == "CAPTURE_REQUEST_FAILED"

        # 4 个 image Outbox（只有成功的 4 个事件生成 Outbox）
        stmt_img = (
            select(Outbox)
            .where(Outbox.event_type == "notification.message.created")
            .where(Outbox.payload["delivery_type"].astext == "image")
        )
        result_img = await db_session.execute(stmt_img)
        outboxes = list(result_img.scalars().all())
        assert len(outboxes) == 4, f"应 4 个 image Outbox（失败事件不生成），实际 {len(outboxes)}"

        # 失败事件的 ID 不应出现在 Outbox 关联的 message 中
        failed_event_id = failed_jobs[0].event_id
        # 通过 CaptureJob 失败的事件 ID 验证：4 个成功 Outbox 对应 4 个不同事件 ID
        succeeded_event_ids = {j.event_id for j in succeeded_jobs}
        assert failed_event_id not in succeeded_event_ids

    @pytest.mark.asyncio
    async def test_same_event_retry_no_duplicate_message(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """场景 4：同事件重试无重复（NotificationMessage 幂等）。

        幂等键含 event_id + indicator_view：
        - 1st 调用：创建 NotificationMessage M1 + image Outbox O1
        - 2nd 调用：create_message 返回现有 M1（fast path），不创建新消息
        """
        inst_id = test_instrument.id
        user_id = test_user.id
        event = self._make_smc_event(inst_id, "smc_bos_retest")
        group_id = str(uuid4())

        captured_payloads: list[dict] = []
        mock_client, _, _ = self._build_mock_capture_client(captured_payloads)

        service = MonitorBatchService()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            # 第 1 次调用
            await service._send_chart_images_via_outbox(
                db=db_session,
                instrument_events={inst_id: [event]},
                instrument_info_cache={inst_id: (test_instrument.symbol, test_instrument.name)},
                instrument_user_map={inst_id: [user_id]},
                message_group_id=group_id,
            )
            # 第 2 次重试（同一事件）
            await service._send_chart_images_via_outbox(
                db=db_session,
                instrument_events={inst_id: [event]},
                instrument_info_cache={inst_id: (test_instrument.symbol, test_instrument.name)},
                instrument_user_map={inst_id: [user_id]},
                message_group_id=group_id,
            )

        # 2 次 capture 请求（CaptureJob 不做幂等，重试时仍会调用 capture worker）
        assert len(captured_payloads) == 2

        # NotificationMessage 只有 1 条（幂等键 monitor-chart:user:inst:event.id:smc）
        from app.models.notification import NotificationMessage
        stmt_msg = select(NotificationMessage).where(
            NotificationMessage.user_id == user_id,
            NotificationMessage.source_type == "monitor_chart",
        )
        result_msg = await db_session.execute(stmt_msg)
        messages = list(result_msg.scalars().all())
        assert len(messages) == 1, (
            f"同事件重试应只创建 1 条 NotificationMessage，实际 {len(messages)}"
        )
        # 幂等键应含 event_id + indicator_view
        assert event.id.hex in messages[0].idempotency_key or str(event.id) in messages[0].idempotency_key
        assert "structure_node" in messages[0].idempotency_key

    @pytest.mark.asyncio
    async def test_same_minute_two_events_both_delivered(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """场景 5：同分钟两个事件均发送（幂等键含 event_id，不被分钟级去重吞掉）。

        旧 idempotency_key=monitor-chart:user:inst:YYYYMMDDHHMM 会吞掉同分钟多事件；
        新 key=monitor-chart:user:inst:event.id:indicator_view 保证不同事件不互相去重。
        """
        inst_id = test_instrument.id
        user_id = test_user.id
        # 两个事件 event_time 完全相同（同一分钟）
        same_minute = datetime(2026, 7, 27, 10, 30, tzinfo=UTC)
        event1 = self._make_smc_event(inst_id, "smc_bos_retest", same_minute)
        event2 = self._make_smc_event(inst_id, "smc_choch_retest", same_minute)
        group_id = str(uuid4())

        captured_payloads: list[dict] = []
        mock_client, _, _ = self._build_mock_capture_client(captured_payloads)

        service = MonitorBatchService()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await service._send_chart_images_via_outbox(
                db=db_session,
                instrument_events={inst_id: [event1, event2]},
                instrument_info_cache={inst_id: (test_instrument.symbol, test_instrument.name)},
                instrument_user_map={inst_id: [user_id]},
                message_group_id=group_id,
            )

        # 2 次 capture 请求（同分钟但不同事件）
        assert len(captured_payloads) == 2

        # 2 个 CaptureJob
        stmt_jobs = select(CaptureJob).where(CaptureJob.instrument_id == inst_id)
        result_jobs = await db_session.execute(stmt_jobs)
        jobs = list(result_jobs.scalars().all())
        assert len(jobs) == 2

        # 2 个 image Outbox（不被分钟级去重吞掉）
        stmt_img = (
            select(Outbox)
            .where(Outbox.event_type == "notification.message.created")
            .where(Outbox.payload["delivery_type"].astext == "image")
        )
        result_img = await db_session.execute(stmt_img)
        outboxes = list(result_img.scalars().all())
        assert len(outboxes) == 2, (
            f"同分钟两事件应均生成 Outbox，实际 {len(outboxes)}"
        )

        # 2 个 NotificationMessage（不同 event_id → 不同幂等键）
        from app.models.notification import NotificationMessage
        stmt_msg = select(NotificationMessage).where(
            NotificationMessage.user_id == user_id,
            NotificationMessage.source_type == "monitor_chart",
        )
        result_msg = await db_session.execute(stmt_msg)
        messages = list(result_msg.scalars().all())
        assert len(messages) == 2, (
            f"同分钟两事件应 2 条 NotificationMessage，实际 {len(messages)}"
        )

    @pytest.mark.asyncio
    async def test_unsupported_event_type_skipped_no_node_fallback(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """场景 6：未映射事件类型显式失败，不调用 capture worker、不生成 node 图。

        旧逻辑：未知事件 → resolve_indicator_view 回退 node_cluster → 生成错误的 node 图
        新逻辑：未知事件 → is_supported_event_type=False → 写 CaptureJob FAILED
                (error_code=UNSUPPORTED_INDICATOR_VIEW)，跳过 capture 请求
        """
        from app.constants.indicator_view import UNSUPPORTED_INDICATOR_VIEW

        inst_id = test_instrument.id
        user_id = test_user.id
        unknown_event = self._make_unknown_event(inst_id)
        group_id = str(uuid4())

        captured_payloads: list[dict] = []
        mock_client, _, _ = self._build_mock_capture_client(captured_payloads)

        service = MonitorBatchService()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await service._send_chart_images_via_outbox(
                db=db_session,
                instrument_events={inst_id: [unknown_event]},
                instrument_info_cache={inst_id: (test_instrument.symbol, test_instrument.name)},
                instrument_user_map={inst_id: [user_id]},
                message_group_id=group_id,
            )

        # 未调用 capture worker（显式跳过，不回退 node_cluster）
        assert len(captured_payloads) == 0, (
            f"未映射事件不应调用 capture worker，实际调用 {len(captured_payloads)} 次"
        )

        # 写入 CaptureJob FAILED + error_code=UNSUPPORTED_INDICATOR_VIEW
        stmt_jobs = select(CaptureJob).where(CaptureJob.event_id == unknown_event.id)
        result_jobs = await db_session.execute(stmt_jobs)
        job = result_jobs.scalar_one_or_none()
        assert job is not None, "未映射事件应写 CaptureJob FAILED"
        assert job.status == CAPTURE_STATUS_FAILED
        assert job.error_code == UNSUPPORTED_INDICATOR_VIEW
        # indicator_view 为 None（未映射，不回退 node_cluster）
        assert job.indicator_view is None, (
            f"未映射事件 indicator_view 应为 None（不回退 node_cluster），实际 {job.indicator_view}"
        )

        # 不生成 image Outbox
        stmt_img = (
            select(Outbox)
            .where(Outbox.event_type == "notification.message.created")
            .where(Outbox.payload["delivery_type"].astext == "image")
        )
        result_img = await db_session.execute(stmt_img)
        assert result_img.scalar_one_or_none() is None, "未映射事件不应生成 image Outbox"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
