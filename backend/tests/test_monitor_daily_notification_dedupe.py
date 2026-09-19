"""盘中监控同类型通知当日去重契约测试（PG）。

业务要求：同一用户、同一标的、同一事件类型，在上海自然日内只推送一次，
避免盘中反复触发同类型通知刷屏。

去重 SSOT 是 notification_messages（message_type=='MONITOR_EVENT'），
去重键 = (user_id, instrument_id, event_type, 上海自然日)，由合并卡片 DTO 的
resource_refs.event_keys 精确记录（无笛卡尔积）。

覆盖：
1. 同用户+同标的+同类型：今日已通知 → 当前周期不再推送
2. 不同类型：仍推送
3. 不同标的：仍推送
4. 不同用户：仍推送（各自独立）
5. 次日（上海自然日）允许再次推送
6. 本 cycle 内同一 (user, instrument, type) 仅出现一次（不去重也可，不双发）
7. 某事件去重后无 recipient → 不截图、不发 image（capture 0 次）
8. 合并卡片 resource_refs.event_keys 为精确 (instrument, type) 对
9. image/capture 使用与卡片同一过滤集合（capture 次数 == 去重后剩余事件数）
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models.notification import NotificationMessage
from app.models.outbox import Outbox
from app.services.monitor_batch_service import MonitorBatchService


def _make_event(
    instrument_id: UUID,
    event_type: str,
    event_time: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        instrument_id=instrument_id,
        event_type=event_type,
        event_time=event_time or datetime(2026, 9, 19, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        payload={"price": 100.0},
        snapshot={},
    )


def _seed_notification(db, user_id: UUID, event_keys: list[dict], created_at: datetime | None = None) -> None:
    """预置一条今日（或指定时间）已通知的合并卡片消息，作为去重历史的 SSOT。"""
    ts = created_at or datetime.now(ZoneInfo("Asia/Shanghai"))
    msg = NotificationMessage(
        id=uuid4(),
        user_id=user_id,
        message_type="MONITOR_EVENT",
        template_key="monitor_merged_event",
        template_version="2.1.0",
        source_type="monitor_event",
        source_id=uuid4(),
        body={
            "message_type": "MONITOR_EVENT",
            "resource_refs": {"event_keys": event_keys},
        },
        idempotency_key=f"seed-{uuid4().hex}",
        created_at=ts,
    )
    db.add(msg)


async def _merged_messages(db, user_id: UUID) -> list[NotificationMessage]:
    stmt = select(NotificationMessage).where(
        NotificationMessage.user_id == user_id,
        NotificationMessage.template_key == "monitor_merged_event",
    )
    return list((await db.execute(stmt)).scalars().all())


async def _run_notification(db, all_events: list, instrument_user_map: dict) -> None:
    service = MonitorBatchService()
    result = SimpleNamespace(total_notifications_created=0)
    await service._send_merged_notification(
        db=db,
        all_events=all_events,
        instrument_user_map=instrument_user_map,
        instrument_extra_info={},
        result=result,
        strategy_version=None,
    )


@asynccontextmanager
async def _patch_capture(captured: list):
    """mock capture worker，记录每次 capture POST 的 payload（含 event_id）。"""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"image_url": "/static/captures/monitor-test.png"}
    mock_resp.raise_for_status.return_value = None

    mock_client = AsyncMock()

    async def _post(url: str, json: dict | None = None, **kwargs: object) -> MagicMock:
        captured.append(json)
        return mock_resp

    mock_client.post = _post

    cls = MagicMock()
    cls.return_value.__aenter__.return_value = mock_client
    cls.return_value.__aexit__.return_value = False
    with patch("httpx.AsyncClient", cls):
        yield


@pytest.mark.postgres
class TestMonitorDailyNotificationDedupe:
    """盘中监控同类型通知当日去重。"""

    @pytest.mark.asyncio
    async def test_same_user_same_instrument_same_type_suppressed(
        self, db_session, user_factory, instrument_factory,
    ) -> None:
        """同用户+同标的+同类型今日已通知 → 当前周期不推送；不同类型仍推送。"""
        ua = await user_factory()
        ia = await instrument_factory()
        ev_a = _make_event(ia.id, "node_cluster_touch")        # 今日已通知 → 抑制
        ev_b = _make_event(ia.id, "smc_bos_retest")            # 不同类型 → 保留
        _seed_notification(
            db_session, ua.id,
            [{"instrument_id": str(ia.id), "event_type": "node_cluster_touch"}],
        )
        await db_session.flush()

        captured: list[dict] = []
        async with _patch_capture(captured):
            await _run_notification(db_session, [ev_a, ev_b], {ia.id: [ua.id]})

        merged = await _merged_messages(db_session, ua.id)
        assert len(merged) == 1
        keys = {
            (k["instrument_id"], k["event_type"])
            for k in merged[0].body["resource_refs"]["event_keys"]
        }
        assert (str(ia.id), "node_cluster_touch") not in keys
        assert (str(ia.id), "smc_bos_retest") in keys

    @pytest.mark.asyncio
    async def test_different_instrument_allowed(
        self, db_session, user_factory, instrument_factory,
    ) -> None:
        """同用户不同标的：未通知的标的仍推送，已通知的标的抑制。"""
        ua = await user_factory()
        i1 = await instrument_factory()
        i2 = await instrument_factory()
        ev1 = _make_event(i1.id, "node_cluster_touch")
        ev2 = _make_event(i2.id, "node_cluster_touch")
        _seed_notification(
            db_session, ua.id,
            [{"instrument_id": str(i1.id), "event_type": "node_cluster_touch"}],
        )
        await db_session.flush()

        async with _patch_capture([]):
            await _run_notification(
                db_session, [ev1, ev2], {i1.id: [ua.id], i2.id: [ua.id]},
            )

        merged = await _merged_messages(db_session, ua.id)
        assert len(merged) == 1
        keys = {
            (k["instrument_id"], k["event_type"])
            for k in merged[0].body["resource_refs"]["event_keys"]
        }
        assert (str(i1.id), "node_cluster_touch") not in keys
        assert (str(i2.id), "node_cluster_touch") in keys

    @pytest.mark.asyncio
    async def test_different_user_allowed(
        self, db_session, user_factory, instrument_factory,
    ) -> None:
        """同标的同类型，仅对"已通知用户"抑制，其他用户正常接收。"""
        ua = await user_factory()
        ub = await user_factory()
        i1 = await instrument_factory()
        ev = _make_event(i1.id, "node_cluster_touch")
        _seed_notification(
            db_session, ua.id,
            [{"instrument_id": str(i1.id), "event_type": "node_cluster_touch"}],
        )
        await db_session.flush()

        async with _patch_capture([]):
            await _run_notification(db_session, [ev], {i1.id: [ua.id, ub.id]})

        merged_a = await _merged_messages(db_session, ua.id)
        merged_b = await _merged_messages(db_session, ub.id)
        assert len(merged_a) == 0, "已通知用户不应再收到同类型通知"
        assert len(merged_b) == 1, "其他用户应正常收到"

    @pytest.mark.asyncio
    async def test_next_shanghai_day_allowed(
        self, db_session, user_factory, instrument_factory,
    ) -> None:
        """昨日已通知（上海自然日不同）→ 今日允许再次推送。"""
        ua = await user_factory()
        i1 = await instrument_factory()
        ev = _make_event(i1.id, "node_cluster_touch")
        yesterday = datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=1)
        _seed_notification(
            db_session, ua.id,
            [{"instrument_id": str(i1.id), "event_type": "node_cluster_touch"}],
            created_at=yesterday,
        )
        await db_session.flush()

        async with _patch_capture([]):
            await _run_notification(db_session, [ev], {i1.id: [ua.id]})

        merged = await _merged_messages(db_session, ua.id)
        assert len(merged) == 1
        keys = {
            (k["instrument_id"], k["event_type"])
            for k in merged[0].body["resource_refs"]["event_keys"]
        }
        assert (str(i1.id), "node_cluster_touch") in keys

    @pytest.mark.asyncio
    async def test_same_cycle_duplicate_once(
        self, db_session, user_factory, instrument_factory,
    ) -> None:
        """本 cycle 内同一 (user, instrument, type) 仅出现一次，不去重也可不双发。"""
        ua = await user_factory()
        i1 = await instrument_factory()
        ev1 = _make_event(i1.id, "node_cluster_touch")
        ev2 = _make_event(i1.id, "node_cluster_touch")
        async with _patch_capture([]):
            await _run_notification(db_session, [ev1, ev2], {i1.id: [ua.id]})

        merged = await _merged_messages(db_session, ua.id)
        assert len(merged) == 1
        assert len(merged[0].body["resource_refs"]["event_ids"]) == 1

    @pytest.mark.asyncio
    async def test_event_without_recipient_no_capture(
        self, db_session, user_factory, instrument_factory,
    ) -> None:
        """某事件去重后无 recipient → 不截图、不发 image（capture 0 次，且无合并卡片）。"""
        ua = await user_factory()
        i1 = await instrument_factory()
        ev = _make_event(i1.id, "node_cluster_touch")
        _seed_notification(
            db_session, ua.id,
            [{"instrument_id": str(i1.id), "event_type": "node_cluster_touch"}],
        )
        await db_session.flush()

        captured: list[dict] = []
        async with _patch_capture(captured):
            await _run_notification(db_session, [ev], {i1.id: [ua.id]})

        assert len(captured) == 0, "无 recipient 的事件不应调用 capture worker"
        merged = await _merged_messages(db_session, ua.id)
        assert len(merged) == 0

        # 也不应有任何 image Outbox
        stmt = select(Outbox).where(Outbox.payload["delivery_type"].astext == "image")
        images = list((await db_session.execute(stmt)).scalars().all())
        assert len(images) == 0

    @pytest.mark.asyncio
    async def test_exact_event_keys_in_resource_refs(
        self, db_session, user_factory, instrument_factory,
    ) -> None:
        """合并卡片 resource_refs.event_keys 为精确 (instrument, type) 对，无笛卡尔积。"""
        ua = await user_factory()
        i1 = await instrument_factory()
        i2 = await instrument_factory()
        ev_a = _make_event(i1.id, "node_cluster_touch")
        ev_b = _make_event(i2.id, "smc_bos_retest")
        async with _patch_capture([]):
            await _run_notification(
                db_session, [ev_a, ev_b], {i1.id: [ua.id], i2.id: [ua.id]},
            )

        merged = await _merged_messages(db_session, ua.id)
        assert len(merged) == 1
        keys = merged[0].body["resource_refs"]["event_keys"]
        assert (str(i1.id), "node_cluster_touch") in {
            (k["instrument_id"], k["event_type"]) for k in keys
        }
        assert (str(i2.id), "smc_bos_retest") in {
            (k["instrument_id"], k["event_type"]) for k in keys
        }
        # 精确对数量 == 事件数（不展开为笛卡尔积）
        assert len(keys) == 2

    @pytest.mark.asyncio
    async def test_image_uses_filtered_set(
        self, db_session, user_factory, instrument_factory,
    ) -> None:
        """image/capture 与卡片共用同一过滤集合：capture 次数 == 去重后剩余事件数。"""
        ua = await user_factory()
        i1 = await instrument_factory()
        i2 = await instrument_factory()
        ev_a = _make_event(i1.id, "node_cluster_touch")   # 今日已通知 → 抑制
        ev_b = _make_event(i1.id, "smc_bos_retest")       # 保留
        ev_c = _make_event(i2.id, "node_cluster_touch")   # 保留
        _seed_notification(
            db_session, ua.id,
            [{"instrument_id": str(i1.id), "event_type": "node_cluster_touch"}],
        )
        await db_session.flush()

        captured: list[dict] = []
        async with _patch_capture(captured):
            await _run_notification(
                db_session, [ev_a, ev_b, ev_c],
                {i1.id: [ua.id], i2.id: [ua.id]},
            )

        # ev_a 抑制（0 次），ev_b / ev_c 保留（各 1 次）→ 共 2 次
        assert len(captured) == 2
        captured_event_ids = {c["event_id"] for c in captured}
        assert str(ev_a.id) not in captured_event_ids
        assert str(ev_b.id) in captured_event_ids
        assert str(ev_c.id) in captured_event_ids
