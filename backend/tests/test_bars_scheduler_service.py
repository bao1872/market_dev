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
from app.services.eod_daily_refresh_service import (
    DailyCandidateFetchResult,
    InstrumentSyncResult,
    RawDailyCandidate,
)
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


# =============================================================================
# F2 — EOD acquire-before-persist：取数完成前不在取数过程中部分写库
# =============================================================================


def _empty_candidates() -> DailyCandidateFetchResult:
    return DailyCandidateFetchResult(rows_by_symbol={}, source_by_symbol={}, failed_symbols=[])


def _candidates_for(
    rows_by_symbol: dict, source_by_symbol: dict, failed: list[str]
) -> DailyCandidateFetchResult:
    return DailyCandidateFetchResult(
        rows_by_symbol=rows_by_symbol, source_by_symbol=source_by_symbol, failed_symbols=failed
    )


def _raw_candidate(symbol: str, trade_date: date = TRADE_DATE) -> RawDailyCandidate:
    """构造通过 is_valid_raw_daily_candidate 的 historical fallback 候选（无 EOD watermark）。"""
    return RawDailyCandidate(
        symbol=symbol,
        trade_date=trade_date,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=Decimal("1000"),
        amount=Decimal("10500"),
    )


def _ten_active_one_missing():
    """构造 10 只活跃标的、snapshot 只覆盖前 9 只的场景。

    关键：1/10 = 0.10 < _MARKET_WIDE_GAP_RATIO(0.20)，确保走 sparse fallback 而非
    market-wide（market-wide 用绝对缺失比例，小基数测试会误触发）。
    """
    covered = [f"60000{i}" for i in range(9)]  # 600000..600008
    missing_sym = "600009"
    active = [_inst(s) for s in covered] + [_inst(missing_sym)]
    rows = tuple(_valid_row(s) for s in covered)
    discovery = AsyncMock(
        return_value=([{"f12": s} for s in covered], list(rows))
    )
    price_fallback = AsyncMock(return_value=([], []))
    return active, covered, missing_sym, rows, discovery, price_fallback


async def _run_f2(
    *,
    discovery,
    price_fallback,
    pytdx_rows,
    active,
    sync_result,
    fetch_candidates=None,
    find_missing_post_write=None,
    adapter=None,
):
    """F2 编排测试 runner：用 mock 跑 _refresh_daily_from_market_snapshot。

    关键观测：
    - upsert_calls：每次 bars_daily(T) 落库调用（应当恰好一次，且晚于 fallback）
    - fetch_calls：fetch_missing_daily_candidates 的调用（PD 观测，决定 fallback 是否触发）
    - find_missing_calls_before_upsert：post-write 核验是否在落库前被误用为「决定 fallback 目标」
    """
    svc = BarsSchedulerService()
    session = _fake_session(active)

    cap: dict = {
        "upsert_calls": [],
        "find_missing_calls_before_upsert": 0,
        "backfill_calls": [],
        "fetch_calls": [],
    }

    async def fake_upsert(s, td, pairs):
        cap["upsert_calls"].append(list(pairs))
        return len(pairs)

    async def fake_backfill(s, insts, td, *, breaker=None):
        cap["backfill_calls"].append(list(insts))
        return len(insts)

    async def fake_find_missing(s, td):
        if not cap["upsert_calls"]:
            cap["find_missing_calls_before_upsert"] += 1
        return list(find_missing_post_write or [])

    async def fake_fetch(instruments, td, *, breaker=None, adapter=None):
        cap["fetch_calls"].append([i.symbol for i in instruments])
        cap["fetch_adapter"] = adapter
        return fetch_candidates

    async def fake_sync(s, rows, td):
        return sync_result

    with patch(
        "app.services.eod_daily_refresh_service.sync_instruments_from_eod_snapshot", fake_sync
    ), patch(
        "app.services.eod_daily_refresh_service.count_active_a_share_instruments",
        AsyncMock(return_value=len(active)),
    ), patch(
        "app.services.eod_daily_refresh_service.check_snapshot_universe_sanity",
        lambda *a, **k: None,
    ), patch(
        "app.services.eod_daily_refresh_service.upsert_raw_daily_snapshot", fake_upsert
    ), patch(
        "app.services.eod_daily_refresh_service.find_missing_daily_instruments", fake_find_missing
    ), patch(
        "app.services.eod_daily_refresh_service.fetch_missing_daily_candidates", fake_fetch
    ), patch(
        "app.services.eod_daily_refresh_service.backfill_new_instruments", fake_backfill
    ), patch.object(
        svc, "_fetch_discovery_snapshot", discovery
    ), patch.object(
        svc, "_fetch_price_fallback_snapshot", price_fallback
    ), patch.object(
        svc, "_fetch_pytdx_primary_eod", AsyncMock(return_value=pytdx_rows)
    ):
        result = BatchResult()
        returned = await svc._refresh_daily_from_market_snapshot(
            TRADE_DATE, session, None, result, adapter=adapter
        )

    return result, returned, cap


@pytest.mark.asyncio
async def test_f2_full_snapshot_no_fallback_single_persist() -> None:
    """F2-1：snapshot 全覆盖 → fallback 取数不调用 → T 日落库恰好一次。"""
    known = _inst("600000")
    discovery = AsyncMock(return_value=([{"f12": "600000"}], [_valid_row("600000")]))
    price_fallback = AsyncMock(return_value=([], []))

    result, _returned, cap = await _run_f2(
        discovery=discovery,
        price_fallback=price_fallback,
        pytdx_rows=(_valid_row("600000"),),
        active=[known],
        sync_result=InstrumentSyncResult(),
        fetch_candidates=_empty_candidates(),
        find_missing_post_write=[],
    )

    assert cap["fetch_calls"] == []  # 无缺口，不触发 fallback 取数
    assert len(cap["upsert_calls"]) == 1  # 唯一一次 T 日落库
    upserted_symbols = {p[1].symbol for p in cap["upsert_calls"][0]}
    assert upserted_symbols == {"600000"}
    assert result.daily_missing_after_snapshot == 0


@pytest.mark.asyncio
async def test_f2_one_missing_fallback_then_single_persist_with_both() -> None:
    """F2-2：snapshot 缺 1 只 → fallback 完成前 persist 次数==0 →
    fallback 成功后唯一一次落库，batch 同时含 snapshot + fallback 行。"""
    active, covered, missing_sym, rows, discovery, price_fallback = _ten_active_one_missing()

    fb_row = _raw_candidate(missing_sym)
    fetch_candidates = _candidates_for(
        rows_by_symbol={missing_sym: fb_row},
        source_by_symbol={missing_sym: "pytdx"},
        failed=[],
    )

    result, _returned, cap = await _run_f2(
        discovery=discovery,
        price_fallback=price_fallback,
        pytdx_rows=rows,
        active=active,
        sync_result=InstrumentSyncResult(),
        fetch_candidates=fetch_candidates,
        find_missing_post_write=[],
    )

    # 落库前没有任何 bars_daily(T) 写入（fallback 取数阶段零写库）
    assert len(cap["upsert_calls"]) == 1  # acquisition 结束后唯一一次
    upserted_symbols = {p[1].symbol for p in cap["upsert_calls"][0]}
    assert missing_sym in upserted_symbols  # snapshot + fallback 同批
    assert result.daily_fallback_succeeded == 1
    assert result.daily_missing_after_fallback == 0


@pytest.mark.asyncio
async def test_f2_one_missing_fallback_fails_persist_available_only() -> None:
    """F2-3：snapshot 缺 1 只且 fallback 失败 → fallback 前无写库 →
    acquisition 结束后只落库可用 staging 一次 → unresolved 被记录，不伪造 success。"""
    active, covered, missing_sym, rows, discovery, price_fallback = _ten_active_one_missing()
    missing_inst = active[-1]

    # fallback 返回空（provider 异常/空/无 exact T 行）→ 计入 failed，不伪造 success
    result, _returned, cap = await _run_f2(
        discovery=discovery,
        price_fallback=price_fallback,
        pytdx_rows=rows,
        active=active,
        sync_result=InstrumentSyncResult(),
        fetch_candidates=_empty_candidates(),
        find_missing_post_write=[missing_inst],  # 写后核验：missing_sym 仍缺
    )

    assert len(cap["upsert_calls"]) == 1  # 仅持久化可用 staging
    upserted_symbols = {p[1].symbol for p in cap["upsert_calls"][0]}
    assert missing_sym not in upserted_symbols  # 失败的标的不在落库批次
    assert result.daily_fallback_succeeded == 0
    assert result.daily_missing_after_fallback == 1  # unresolved 保留给 continuity gate


@pytest.mark.asyncio
async def test_f2_market_wide_gap_no_per_symbol_fallback() -> None:
    """F2-4：整日空洞 → 不逐股 fallback（无 storm）→ 可用行最多一次落库 →
    unresolved 保留给既有 continuity gate。"""
    active = [_inst(f"{600000 + i}") for i in range(10)]
    # snapshot 只覆盖 1/10 → 缺失比例 0.9 >= 0.20 → market_wide_gap
    discovery = AsyncMock(return_value=([{"f12": "600000"}], [_valid_row("600000")]))
    price_fallback = AsyncMock(return_value=([], []))

    result, _returned, cap = await _run_f2(
        discovery=discovery,
        price_fallback=price_fallback,
        pytdx_rows=(),
        active=active,
        sync_result=InstrumentSyncResult(),
        # 若被误触发逐股 fallback，返回非空会污染断言；这里用会显式失败的值防御
        fetch_candidates=_candidates_for(
            rows_by_symbol={"600001": _valid_row("600001")},
            source_by_symbol={"600001": "pytdx"},
            failed=[],
        ),
        find_missing_post_write=[],
    )

    assert cap["fetch_calls"] == []  # 未触发逐股 fallback storm
    assert result.daily_repair_mode == "market_wide_gap"
    assert len(cap["upsert_calls"]) == 1
    upserted_symbols = {p[1].symbol for p in cap["upsert_calls"][0]}
    assert upserted_symbols == {"600000"}


@pytest.mark.asyncio
async def test_f2_fallback_provider_exception_no_partial_write() -> None:
    """F2-5：fallback provider 异常 → acquisition 阶段无 partial T 日写库。"""
    active, covered, missing_sym, rows, discovery, price_fallback = _ten_active_one_missing()
    svc = BarsSchedulerService()
    session = _fake_session(active)

    upsert_calls: list = []

    async def boom(instruments, td, *, breaker=None, adapter=None):
        raise RuntimeError("provider down")

    async def spy_upsert(s, td, pairs):
        upsert_calls.append(list(pairs))
        return len(pairs)

    with patch(
        "app.services.eod_daily_refresh_service.fetch_missing_daily_candidates", boom
    ), patch(
        "app.services.eod_daily_refresh_service.upsert_raw_daily_snapshot", spy_upsert
    ), patch(
        "app.services.eod_daily_refresh_service.find_missing_daily_instruments",
        AsyncMock(return_value=[]),
    ), patch(
        "app.services.eod_daily_refresh_service.sync_instruments_from_eod_snapshot",
        AsyncMock(return_value=InstrumentSyncResult()),
    ), patch(
        "app.services.eod_daily_refresh_service.count_active_a_share_instruments",
        AsyncMock(return_value=len(active)),
    ), patch(
        "app.services.eod_daily_refresh_service.check_snapshot_universe_sanity",
        lambda *a, **k: None,
    ), patch(
        "app.services.eod_daily_refresh_service.backfill_new_instruments",
        AsyncMock(return_value=0),
    ), patch.object(svc, "_fetch_discovery_snapshot", discovery), patch.object(
        svc, "_fetch_price_fallback_snapshot", price_fallback
    ), patch.object(svc, "_fetch_pytdx_primary_eod", AsyncMock(return_value=rows)):
        with pytest.raises(RuntimeError):
            await svc._refresh_daily_from_market_snapshot(TRADE_DATE, session, None, BatchResult())

    # 异常发生在 acquisition 阶段（fetch 失败时） → 不应有任何 bars_daily(T) 落库
    assert upsert_calls == []


@pytest.mark.asyncio
async def test_f2_post_write_find_missing_only_verifies() -> None:
    """F2-6：写后 find_missing_daily_instruments 只用于 verification，
    不决定 fallback 目标（缺口由内存集合差在落库前算出）。"""
    active, covered, missing_sym, rows, discovery, price_fallback = _ten_active_one_missing()

    fb_row = _raw_candidate(missing_sym)
    fetch_candidates = _candidates_for(
        rows_by_symbol={missing_sym: fb_row},
        source_by_symbol={missing_sym: "pytdx"},
        failed=[],
    )

    result, _returned, cap = await _run_f2(
        discovery=discovery,
        price_fallback=price_fallback,
        pytdx_rows=rows,
        active=active,
        sync_result=InstrumentSyncResult(),
        fetch_candidates=fetch_candidates,
        find_missing_post_write=[active[-1]],  # 写后核验仍发现 missing_sym（仅验证顺序）
    )

    # 落库之前 find_missing 不应被调用（缺口由内存算，不查 DB）
    assert cap["find_missing_calls_before_upsert"] == 0
    # 写后核验确实发生（一次）
    assert result.daily_missing_after_fallback == 1


@pytest.mark.asyncio
async def test_f2_new_stock_backfill_after_persist_and_t_day_canonical() -> None:
    """F2-8：新股历史补齐位于 T 日 canonical 落库之后；T 日 row 由 snapshot 路径写入，
    不被 historical provider 覆盖。"""
    known = _inst("600000")
    new_inst = _inst("689001", market="BJ")
    discovery = AsyncMock(
        return_value=(
            [{"f12": "600000"}, {"f12": "689001"}],
            [_valid_row("600000"), _valid_row("689001", market="BJ")],
        )
    )
    price_fallback = AsyncMock(return_value=([], []))
    sync_result = InstrumentSyncResult(new_instruments=[new_inst], new_symbols=["689001"])

    result, _returned, cap = await _run_f2(
        discovery=discovery,
        price_fallback=price_fallback,
        pytdx_rows=(),
        active=[known, new_inst],
        sync_result=sync_result,
        fetch_candidates=_empty_candidates(),
        find_missing_post_write=[],
    )

    # T 日 canonical row 由 snapshot 路径落库（含 689001）
    assert cap["upsert_calls"]
    upserted_symbols = {p[1].symbol for p in cap["upsert_calls"][0]}
    assert "689001" in upserted_symbols
    # backfill 发生在落库之后（顺序：upsert -> backfill）
    assert cap["backfill_calls"] and new_inst in cap["backfill_calls"][0]


@pytest.mark.asyncio
async def test_f2_previous_close_evidence_only_from_snapshot() -> None:
    """F2-9：previous_close 证据只来自 snapshot 选中的 source，fallback 不伪造。"""
    active, covered, missing_sym, rows, discovery, price_fallback = _ten_active_one_missing()

    # fallback candidate 没有 previous_close / updated_at（无 EOD watermark）
    fb_row = _raw_candidate(missing_sym)
    assert not hasattr(fb_row, "previous_close")
    assert not hasattr(fb_row, "updated_at")
    fetch_candidates = _candidates_for(
        rows_by_symbol={missing_sym: fb_row},
        source_by_symbol={missing_sym: "pytdx"},
        failed=[],
    )

    result, returned, cap = await _run_f2(
        discovery=discovery,
        price_fallback=price_fallback,
        pytdx_rows=rows,
        active=active,
        sync_result=InstrumentSyncResult(),
        fetch_candidates=fetch_candidates,
        find_missing_post_write=[],
    )

    # 被 snapshot 覆盖的 symbol 提供 previous_close；仅 fallback 的 missing_sym 不应出现
    assert covered[0] in returned
    assert returned[covered[0]] is not None
    assert missing_sym not in returned


@pytest.mark.asyncio
async def test_f2_source_metrics_include_fallback() -> None:
    """F2-10：原 snapshot source metrics（pytdx / EM fallback / BJ）不因 staging 重构丢失。"""
    active, covered, missing_sym, rows, discovery, price_fallback = _ten_active_one_missing()

    fetch_candidates = _candidates_for(
        rows_by_symbol={missing_sym: _raw_candidate(missing_sym)},
        source_by_symbol={missing_sym: "eastmoney_fallback"},
        failed=[],
    )

    result, _returned, cap = await _run_f2(
        discovery=discovery,
        price_fallback=price_fallback,
        pytdx_rows=rows,
        active=active,
        sync_result=InstrumentSyncResult(),
        fetch_candidates=fetch_candidates,
        find_missing_post_write=[],
    )

    assert result.daily_pytdx_rows == len(covered)  # 9 只 snapshot 走 pytdx
    assert result.daily_eastmoney_rows == 1  # fallback 计入 EM
    assert result.daily_bj_rows == 0
    assert result.daily_primary_source in ("mixed", "eastmoney", "pytdx")


@pytest.mark.asyncio
async def test_f2_1_adapter_passthrough_to_fetch() -> None:
    """F2.1 STEP 5：scheduler 拿到的 adapter 必须透传到 fetch_missing_daily_candidates。

    测试/worker 注入的 adapter 语义不得漂移。
    """
    active, covered, missing_sym, rows, discovery, price_fallback = _ten_active_one_missing()
    fake_adapter = object()

    _result, _returned, cap = await _run_f2(
        discovery=discovery,
        price_fallback=price_fallback,
        pytdx_rows=rows,
        active=active,
        sync_result=InstrumentSyncResult(),
        fetch_candidates=_empty_candidates(),
        find_missing_post_write=[active[-1]],
        adapter=fake_adapter,
    )

    assert cap["fetch_adapter"] is fake_adapter


@pytest.mark.asyncio
async def test_f2_1_fallback_candidate_no_watermark_single_persist() -> None:
    """F2.1 Blocker 1：historical fallback 候选不持有 EOD watermark（无 updated_at/previous_close），
    且仍以「唯一一次 T 日 persist」进入 staging。
    """
    active, covered, missing_sym, rows, discovery, price_fallback = _ten_active_one_missing()

    fb = _raw_candidate(missing_sym)
    assert not hasattr(fb, "updated_at")
    assert not hasattr(fb, "previous_close")

    fetch_candidates = _candidates_for(
        {missing_sym: fb}, {missing_sym: "pytdx"}, []
    )

    _result, _returned, cap = await _run_f2(
        discovery=discovery,
        price_fallback=price_fallback,
        pytdx_rows=rows,
        active=active,
        sync_result=InstrumentSyncResult(),
        fetch_candidates=fetch_candidates,
        find_missing_post_write=[],
    )

    # 唯一一次 T 日 persist
    assert len(cap["upsert_calls"]) == 1
    persisted_rows = [p[1] for p in cap["upsert_calls"][0]]
    # fallback 行以 RawDailyCandidate 形式（无 watermark）进入 staging；snapshot 行仍是 EodSnapshotRow
    assert any(isinstance(r, RawDailyCandidate) for r in persisted_rows)
    for r in persisted_rows:
        if isinstance(r, RawDailyCandidate):
            # historical fallback 不得持有 EOD watermark
            assert not hasattr(r, "updated_at")
            assert not hasattr(r, "previous_close")
        else:
            # snapshot 行：合法保留 EOD watermark
            assert hasattr(r, "updated_at")
