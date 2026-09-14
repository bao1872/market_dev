"""[EOD-SNAPSHOT / R3] bars_scheduler 盘后 universe discovery freshness 测试。

R3 合同（新股行情恢复）：
- universe discovery 必须只信任实时 push2 host（REALTIME_CLIST_HOSTS）+ 收盘 watermark；
- push2delay 不再拥有 universe discovery 权（仅作已知标的价格 fallback，不能创建新股）；
- 实时快照发现的新股必须同轮落 T 日日线（sync → 重查 active → merge → upsert）；
- discovery 失败 → universe_discovery_status=failed + 保留 DB 现有 universe +
  延时源仅为已知标的补价（绝不新建 instrument）。

所有测试均为纯单元/编排测试，不触远程 DB：discovery / snapshot fetch / sync / upsert /
missing / backfill 等外部依赖全部 mock。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.models.instrument import Instrument
from app.services.bars_scheduler_service import BarsSchedulerService, BatchResult
from app.services.eod_daily_refresh_service import InstrumentSyncResult
from app.services.eod_market_snapshot_provider import (
    EASTMONEY_CLIST_HOSTS,
    EodSnapshotRow,
    SnapshotFetchBatch,
    SnapshotProviderError,
)
from app.services.realtime_market_snapshot_provider import REALTIME_CLIST_HOSTS

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 9, 14)

pytestmark = pytest.mark.pure_unit


def _valid_row(symbol: str, market: str = "SH") -> EodSnapshotRow:
    """构造一条通过 is_valid_snapshot_daily_row 的有效 snapshot 行。"""
    return EodSnapshotRow(
        symbol=symbol,
        name=f"name-{symbol}",
        market=market,
        updated_at=datetime(
            TRADE_DATE.year, TRADE_DATE.month, TRADE_DATE.day, 15, 0, tzinfo=SHANGHAI
        ),
        open=Decimal("10"),
        high=Decimal("10.5"),
        low=Decimal("9.8"),
        close=Decimal("10.2"),
        volume=Decimal("1000000"),
        amount=Decimal("10200000"),
        previous_close=Decimal("10"),
    )


def _inst(symbol: str, market: str = "SH", status: str = "active"):
    return Instrument(
        id=uuid.uuid4(),
        symbol=symbol,
        name=f"name-{symbol}",
        market=market,
        status=status,
    )


def _fake_session(active: list[Instrument]) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = active
    session.execute = AsyncMock(return_value=result)
    return session


# ---------------------------------------------------------------------------
# R3-A / R3-B：orchestration 把 universe discovery 指向实时 push2 host
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestration_discovery_uses_realtime_host() -> None:
    """盘后 discovery 必须走 REALTIME_CLIST_HOSTS（仅 push2）并要求收盘 watermark。"""
    svc = BarsSchedulerService()
    session = _fake_session([_inst("600000")])
    captured: dict = {}

    async def fake_fetch(client, *, expected_trade_date, hosts, require_eod_watermark, **kw):
        captured["hosts"] = hosts
        captured["require_eod_watermark"] = require_eod_watermark
        captured["expected_trade_date"] = expected_trade_date
        return [{"f12": "600000"}]

    with patch(
        "app.services.eod_market_snapshot_provider.fetch_full_a_share_snapshot",
        fake_fetch,
    ), patch(
        "app.services.eod_market_snapshot_provider.normalize_snapshot_rows",
        return_value=[_valid_row("600000")],
    ), patch(
        "app.services.eod_daily_refresh_service.sync_instruments_from_eod_snapshot",
        AsyncMock(return_value=InstrumentSyncResult()),
    ), patch(
        "app.services.eod_daily_refresh_service.count_active_a_share_instruments",
        AsyncMock(return_value=1),
    ), patch(
        "app.services.eod_daily_refresh_service.check_snapshot_universe_sanity",
        lambda *a, **k: None,
    ), patch(
        "app.services.eod_daily_refresh_service.upsert_raw_daily_snapshot",
        AsyncMock(return_value=1),
    ), patch(
        "app.services.eod_daily_refresh_service.find_missing_daily_instruments",
        AsyncMock(return_value=[]),
    ), patch(
        "app.services.eod_daily_refresh_service.fill_missing_daily_instruments",
        AsyncMock(return_value=0),
    ), patch(
        "app.services.eod_daily_refresh_service.backfill_new_instruments",
        AsyncMock(return_value=0),
    ), patch.object(
        svc, "_fetch_price_fallback_snapshot", AsyncMock(return_value=([], []))
    ), patch.object(
        svc, "_fetch_pytdx_primary_eod", AsyncMock(return_value=())
    ):
        result = BatchResult()
        await svc._refresh_daily_from_market_snapshot(TRADE_DATE, session, None, result)

    assert captured["hosts"] == REALTIME_CLIST_HOSTS
    assert captured["hosts"] == ("push2.eastmoney.com",)
    assert captured["hosts"][0] == "push2.eastmoney.com"
    assert "push2delay" not in captured["hosts"][0]
    assert captured["require_eod_watermark"] is True
    assert captured["expected_trade_date"] == TRADE_DATE
    assert result.universe_discovery_status == "success"


# ---------------------------------------------------------------------------
# R3：价格 fallback 只走延时源，且只用于价格（不拥有 discovery 权）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_price_fallback_uses_delayed_host() -> None:
    """discovery 失败时的价格 fallback 必须用 EASTMONEY_CLIST_HOSTS（含 push2delay）。"""
    svc = BarsSchedulerService()
    captured: dict = {}

    async def fake_fetch(client, *, hosts, expected_trade_date, require_eod_watermark, **kw):
        captured["hosts"] = hosts
        captured["require_eod_watermark"] = require_eod_watermark
        return SnapshotFetchBatch(
            raw_rows=(),
            source_host="push2delay.eastmoney.com",
            captured_at=datetime(2026, 9, 14, 16, 0, tzinfo=SHANGHAI),
            market_watermark=datetime(2026, 9, 14, 15, 0, tzinfo=SHANGHAI),
        )

    with patch(
        "app.services.eod_market_snapshot_provider.fetch_a_share_snapshot_batch",
        fake_fetch,
    ), patch(
        "app.services.eod_market_snapshot_provider.normalize_snapshot_rows",
        return_value=[_valid_row("600000")],
    ):
        raw, rows = await svc._fetch_price_fallback_snapshot(TRADE_DATE)

    assert captured["hosts"] == EASTMONEY_CLIST_HOSTS
    assert captured["require_eod_watermark"] is True
    assert rows == [_valid_row("600000")]


@pytest.mark.asyncio
async def test_price_fallback_swallows_exception() -> None:
    """延时源价格 fallback 失败必须静默返回 ([], [])，不向上抛阻断整轮。"""

    async def boom(*a, **k):
        raise SnapshotProviderError("all down")

    svc = BarsSchedulerService()
    with patch(
        "app.services.eod_market_snapshot_provider.fetch_a_share_snapshot_batch",
        boom,
    ):
        raw, rows = await svc._fetch_price_fallback_snapshot(TRADE_DATE)

    assert raw == []
    assert rows == []


@pytest.mark.asyncio
async def test_discovery_propagates_watermark_failure() -> None:
    """require_eod_watermark=True 时，watermark<15:00 必须失败（不静默接受滞后快照）。"""

    async def low_watermark(*a, **k):
        raise SnapshotProviderError("watermark < 15:00")

    svc = BarsSchedulerService()
    with patch(
        "app.services.eod_market_snapshot_provider.fetch_full_a_share_snapshot",
        low_watermark,
    ):
        with pytest.raises(SnapshotProviderError):
            await svc._fetch_discovery_snapshot(
                TRADE_DATE, hosts=REALTIME_CLIST_HOSTS, require_eod_watermark=True
            )


# ---------------------------------------------------------------------------
# 编排级 R3 测试：discovery 失败 / 新股同轮落库
# ---------------------------------------------------------------------------


async def _run_orchestration(
    *,
    discovery,
    price_fallback,
    pytdx_rows: tuple[EodSnapshotRow, ...],
    active: list[Instrument],
    sync_result: InstrumentSyncResult,
) -> tuple[BatchResult, dict]:
    """用 mock 跑一遍 _refresh_daily_from_market_snapshot，回收关键调用证据。"""
    svc = BarsSchedulerService()
    session = _fake_session(active)

    upsert_calls: list = []
    backfill_calls: list = []
    sync_calls: list = []

    async def fake_upsert(s, td, pairs):
        upsert_calls.append(list(pairs))
        return len(pairs)

    async def fake_backfill(s, insts, td, *, breaker=None):
        backfill_calls.append(list(insts))
        return len(insts)

    async def fake_sync(s, rows, td):
        sync_calls.append(list(rows))
        return sync_result

    with patch(
        "app.services.eod_daily_refresh_service.sync_instruments_from_eod_snapshot",
        fake_sync,
    ), patch(
        "app.services.eod_daily_refresh_service.count_active_a_share_instruments",
        AsyncMock(return_value=len(active)),
    ), patch(
        "app.services.eod_daily_refresh_service.check_snapshot_universe_sanity",
        lambda *a, **k: None,
    ), patch(
        "app.services.eod_daily_refresh_service.upsert_raw_daily_snapshot",
        fake_upsert,
    ), patch(
        "app.services.eod_daily_refresh_service.find_missing_daily_instruments",
        AsyncMock(return_value=[]),
    ), patch(
        "app.services.eod_daily_refresh_service.fill_missing_daily_instruments",
        AsyncMock(return_value=0),
    ), patch(
        "app.services.eod_daily_refresh_service.backfill_new_instruments",
        fake_backfill,
    ), patch.object(svc, "_fetch_discovery_snapshot", discovery), patch.object(
        svc, "_fetch_price_fallback_snapshot", price_fallback
    ), patch.object(
        svc, "_fetch_pytdx_primary_eod", AsyncMock(return_value=pytdx_rows)
    ):
        result = BatchResult()
        await svc._refresh_daily_from_market_snapshot(TRADE_DATE, session, None, result)

    return result, {
        "upsert_calls": upsert_calls,
        "backfill_calls": backfill_calls,
        "sync_calls": sync_calls,
    }


@pytest.mark.asyncio
async def test_orchestration_discovery_failure_keeps_universe_and_falls_back() -> None:
    """R3-E：discovery 失败 → 保留 DB universe + 延时源仅补已知标的价，绝不新建新股。

    延时快照里包含一只 push2 没返回的新股（689009），由于 discovery 失败，
    sync_instruments_from_eod_snapshot 不应被调用，且该新股不应被写入 T 日日线。
    """
    known = _inst("600000")
    new_late = "689009"  # 仅延时源有、DB universe 没有的新股
    discovery = AsyncMock(side_effect=SnapshotProviderError("push2 down"))
    price_fallback = AsyncMock(
        return_value=(
            [{"f12": "600000"}, {"f12": new_late}],
            [_valid_row("600000"), _valid_row(new_late, market="BJ")],
        )
    )

    result, cap = await _run_orchestration(
        discovery=discovery,
        price_fallback=price_fallback,
        pytdx_rows=(),
        active=[known],
        sync_result=InstrumentSyncResult(),
    )

    assert result.universe_discovery_status == "failed"
    # discovery 失败时不应调用 universe sync（延时源的 symbol set 不作为 discovery 输入）
    assert cap["sync_calls"] == []
    # 价格 fallback 只给已知标的补价：upsert 仅含 600000，不含延时源的新股 689009
    assert len(cap["upsert_calls"]) == 1
    upserted_ids = {p[0] for p in cap["upsert_calls"][0]}
    assert upserted_ids == {known.id}
    # 新股既没有进 universe，也没有落 T 日日线
    assert all(p[1].symbol != new_late for p in cap["upsert_calls"][0])


@pytest.mark.asyncio
async def test_orchestration_discovers_new_stock_same_round_bar() -> None:
    """R3-C/D：push2 实时快照发现新股（689001）→ 同轮落 T 日日线 + 触发历史回补。"""
    known = _inst("600000")
    new_inst = _inst("689001", market="BJ")
    discovery = AsyncMock(
        return_value=(
            [{"f12": "600000"}, {"f12": "689001"}],
            [_valid_row("600000"), _valid_row("689001", market="BJ")],
        )
    )
    price_fallback = AsyncMock(return_value=([], []))
    sync_result = InstrumentSyncResult(
        new_instruments=[new_inst], new_symbols=["689001"]
    )

    # active 重查需包含新股（模拟 sync 后 DB 已有该 instrument）
    result, cap = await _run_orchestration(
        discovery=discovery,
        price_fallback=price_fallback,
        pytdx_rows=(),
        active=[known, new_inst],
        sync_result=sync_result,
    )

    assert result.universe_discovery_status == "success"
    assert result.universe_new == 1
    # discovery 输入确实包含新股 symbol
    assert any(r.symbol == "689001" for r in cap["sync_calls"][0])
    # 新股 T 日日线同轮落库
    upserted_ids = {p[0] for p in cap["upsert_calls"][0]}
    assert new_inst.id in upserted_ids
    # 新股触发历史回补
    assert cap["backfill_calls"] and new_inst in cap["backfill_calls"][0]
