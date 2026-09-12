"""[G1B-2B] 盘后日线多源编排与 Universe Discovery 解耦单元测试。

覆盖失败矩阵（Section 17）：
- Case A: 全正常（EM discovery 成功，pytdx primary 成功；SH/SZ→pytdx，BJ→EM；EM 拉 1 次，sentinel 2 次）
- Case B: pytdx 整体失败（降级到内存缓存 EM；绝不重复网络拉取 EM）
- Case C: pytdx partial coverage（pytdx 覆盖部分 SH/SZ，缺失的由 EM fallback，BJ 由 EM）
- Case D: EM discovery 失败，pytdx 正常（使用已有 DB universe，SH/SZ pytdx 正常完成，universe_discovery_status="failed"）
- Case E: EM 失败 + pytdx 失败（整体抛 SnapshotProviderError，交由 legacy 处理）
- Case F: pytdx sentinel/verifier 失败（降级到内存缓存 EM）
- Case G: source precedence（pytdx 与 EM 价格不同时以 pytdx 为准，previous_close 来自 pytdx）
- Case H: BJ policy（BJ 不进入 pytdx 请求，BJ row 来自 EM）

以及：
- 性能断言（Section 18）：EM fetch_count == 1，sentinel == 2，无逐股 get_daily_bars 循环
- previous_close evidence 与所选源一致性（Section 10）
- BatchResult observability 字段断言（Section 19）
- refresh_raw_daily_only 已完全删除（Section 14）
- _merge_daily_snapshot_rows 纯函数合并契约
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.core.pytdx_adapter import PytdxCallProvenance
from app.models.instrument import Instrument
from app.services import eod_daily_refresh_service as refresh_mod
from app.services import eod_market_snapshot_provider as prov
from app.services.bars_scheduler_service import BarsSchedulerService, BatchResult
from app.services.eod_daily_refresh_service import (
    InstrumentSyncResult,
)
from app.services.eod_market_snapshot_provider import EodSnapshotRow, SnapshotProviderError
from app.services.pytdx_eod_snapshot_provider import PytdxEodSnapshotError

TRADE_DATE = date(2026, 9, 11)
_SH = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# 测试替身与工厂
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _ScriptedSession:
    def __init__(
        self,
        select_queue: list[list[Any]] | None = None,
        scalar_queue: list[Any] | None = None,
    ) -> None:
        self.insert_statements: list[Any] = []
        self.select_statements: list[Any] = []
        self._select_queue = list(select_queue or [])
        self._scalar_queue = list(scalar_queue or [])
        self.commits = 0

    async def execute(self, stmt: Any) -> _FakeResult:
        self.select_statements.append(stmt)
        rows = self._select_queue.pop(0) if self._select_queue else []
        return _FakeResult(rows)

    async def scalar(self, stmt: Any) -> Any:
        return self._scalar_queue.pop(0) if self._scalar_queue else None

    async def commit(self) -> None:
        self.commits += 1

    async def close(self) -> None:
        pass


def _make_inst(symbol: str, market: str, name: str = "") -> Instrument:
    return Instrument(
        id=uuid.uuid4(),
        symbol=symbol,
        market=market,
        name=name or f"Stock_{symbol}",
        status="active",
    )


def _make_em_row(
    symbol: str,
    market: str,
    trade_date: date = TRADE_DATE,
    *,
    close_p: str = "10.0",
    open_p: str | None = None,
    high_p: str | None = None,
    low_p: str | None = None,
    volume: str = "100000",
    amount: str = "1000000",
    prev_close: str = "9.8",
) -> EodSnapshotRow:
    c = Decimal(close_p)
    o = Decimal(open_p) if open_p is not None else (c * Decimal("0.99"))
    h = Decimal(high_p) if high_p is not None else (max(o, c) * Decimal("1.01"))
    lo = Decimal(low_p) if low_p is not None else (min(o, c) * Decimal("0.99"))
    return EodSnapshotRow(
        symbol=symbol,
        name=f"Stock_{symbol}",
        market=market,
        updated_at=datetime.combine(trade_date, datetime.min.time(), tzinfo=_SH).replace(
            hour=15, minute=1
        ),
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=Decimal(volume),
        amount=Decimal(amount),
        previous_close=Decimal(prev_close),
    )


def _pytdx_quote_dict(
    code: str,
    market_code: int,
    *,
    price: float = 10.0,
    open_p: float | None = None,
    high_p: float | None = None,
    low_p: float | None = None,
    last_close: float = 9.5,
    vol: float = 1000.0,
    amount: float = 10000.0,
    servertime: str = "15:00:30",
) -> dict[str, Any]:
    o = open_p if open_p is not None else (price * 0.99)
    h = high_p if high_p is not None else (max(o, price) * 1.01)
    lo = low_p if low_p is not None else (min(o, price) * 0.99)
    return {
        "code": code,
        "market": market_code,
        "price": price,
        "open": o,
        "high": h,
        "low": lo,
        "last_close": last_close,
        "vol": vol,
        "amount": amount,
        "servertime": servertime,
    }


def _daily_bar_df(
    symbol: str,
    trade_date: date = TRADE_DATE,
    *,
    close_p: float = 10.0,
    open_p: float | None = None,
    high_p: float | None = None,
    low_p: float | None = None,
    volume: float = 100000.0,  # 1000 手 × 100 = 100000 股
    amount: float = 10000.0,
) -> pd.DataFrame:
    c = close_p
    o = open_p if open_p is not None else (c * 0.99)
    h = high_p if high_p is not None else (max(o, c) * 1.01)
    lo = low_p if low_p is not None else (min(o, c) * 0.99)
    return pd.DataFrame([
        {
            "datetime": pd.Timestamp(trade_date),
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": volume,
            "amount": amount,
        }
    ])


# ---------------------------------------------------------------------------
# Section 14: 确认 refresh_raw_daily_only 已彻底删除
# ---------------------------------------------------------------------------

def test_refresh_raw_daily_only_removed() -> None:
    """确认 refresh_raw_daily_only 公开入口已被删除，不留冗余编排。"""
    assert not hasattr(BarsSchedulerService, "refresh_raw_daily_only"), (
        "refresh_raw_daily_only 必须彻底删除，不得保留第二套 raw daily 编排入口"
    )


# ---------------------------------------------------------------------------
# 纯函数合并契约测试 (_merge_daily_snapshot_rows)
# ---------------------------------------------------------------------------

def test_merge_daily_snapshot_rows_precedence() -> None:
    """_merge_daily_snapshot_rows 满足优先级契约：SH/SZ pytdx > EM fallback；BJ 必由 EM 提供。"""
    inst_sh = _make_inst("600519", "SH")
    inst_sz = _make_inst("000001", "SZ")
    inst_sh_missing = _make_inst("600000", "SH")
    inst_bj = _make_inst("830001", "BJ")
    id_by_symbol = {
        inst.symbol: inst.id
        for inst in [inst_sh, inst_sz, inst_sh_missing, inst_bj]
    }

    pytdx_rows = [
        _make_em_row("600519", "SH", close_p="1600.0", prev_close="1580.0"),
        _make_em_row("000001", "SZ", close_p="12.0", prev_close="11.8"),
    ]
    em_rows = [
        _make_em_row("600519", "SH", close_p="1590.0", prev_close="1570.0"),  # pytdx 也有，应被 pytdx 覆盖
        _make_em_row("600000", "SH", close_p="8.0", prev_close="7.9"),        # pytdx 缺失，应走 fallback
        _make_em_row("830001", "BJ", close_p="15.0", prev_close="14.5"),      # BJ 必须由 EM 提供
    ]

    selected, sources = BarsSchedulerService._merge_daily_snapshot_rows(
        TRADE_DATE, id_by_symbol, pytdx_rows, em_rows
    )

    assert len(selected) == 4
    # 600519: pytdx 优先
    assert sources["600519"] == "pytdx"
    assert selected["600519"].close == Decimal("1600.0")
    assert selected["600519"].previous_close == Decimal("1580.0")

    # 000001: pytdx 优先
    assert sources["000001"] == "pytdx"
    assert selected["000001"].close == Decimal("12.0")

    # 600000: pytdx 缺失，走 EM fallback
    assert sources["600000"] == "eastmoney_fallback"
    assert selected["600000"].close == Decimal("8.0")

    # 830001: BJ 必来自 EM
    assert sources["830001"] == "eastmoney_bj"
    assert selected["830001"].close == Decimal("15.0")


# ---------------------------------------------------------------------------
# 失败矩阵测试 (Section 17 Cases A~H)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_a_all_normal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case A — 全正常：
    EM discovery ✅, pytdx ✅
    SH/SZ → pytdx, BJ → EM
    EM fetch count = 1, sentinel daily = 2
    """
    service = BarsSchedulerService(fetch_processes=1)
    inst_sh = _make_inst("600519", "SH")
    inst_sz = _make_inst("000001", "SZ")
    inst_bj = _make_inst("830001", "BJ")
    active_instruments = [inst_sh, inst_sz, inst_bj]
    session = _ScriptedSession(select_queue=[active_instruments])

    em_mock = AsyncMock(
        return_value=[
            {"f12": "600519", "f13": 1},
            {"f12": "000001", "f13": 0},
            {"f12": "830001", "f13": 0},
        ]
    )
    monkeypatch.setattr(prov, "fetch_full_a_share_snapshot", em_mock)
    monkeypatch.setattr(
        prov,
        "normalize_snapshot_rows",
        lambda raw: [
            _make_em_row("600519", "SH", close_p="1590.0", prev_close="1570.0"),
            _make_em_row("000001", "SZ", close_p="11.9", prev_close="11.7"),
            _make_em_row("830001", "BJ", close_p="15.0", prev_close="14.5"),
        ],
    )
    monkeypatch.setattr(
        refresh_mod, "count_active_a_share_instruments", AsyncMock(return_value=3)
    )
    monkeypatch.setattr(
        refresh_mod, "check_snapshot_universe_sanity", lambda *a, **k: None
    )
    monkeypatch.setattr(
        refresh_mod,
        "sync_instruments_from_eod_snapshot",
        AsyncMock(return_value=InstrumentSyncResult()),
    )
    upserted_rows: list[Any] = []

    async def fake_upsert(sess: Any, dt: Any, pairs: Any) -> int:
        upserted_rows.extend(pairs)
        return len(pairs)

    monkeypatch.setattr(refresh_mod, "upsert_raw_daily_snapshot", fake_upsert)
    monkeypatch.setattr(
        refresh_mod, "find_missing_daily_instruments", AsyncMock(return_value=[])
    )

    # Mock pytdx adapter
    mock_adapter = MagicMock()
    mock_adapter.get_security_quotes_with_provenance.return_value = (
        [
            _pytdx_quote_dict("600519", 1, price=1600.0, last_close=1580.0),
            _pytdx_quote_dict("000001", 0, price=12.0, last_close=11.8),
        ],
        PytdxCallProvenance(server=("1.1.1.1", 7709), connection_generation=1),
    )

    def fake_get_daily_bars(symbol: str, start: date, end: date) -> pd.DataFrame:
        if symbol == "600519":
            return _daily_bar_df("600519", close_p=1600.0)
        if symbol == "000001":
            return _daily_bar_df("000001", close_p=12.0)
        return pd.DataFrame()

    mock_adapter.get_daily_bars.side_effect = fake_get_daily_bars

    result = BatchResult()
    prev_close_map = await service._refresh_daily_from_market_snapshot(
        TRADE_DATE,
        session,
        None,
        result,
        adapter=mock_adapter,
        pytdx_batch_interval_seconds=0.0,
    )

    # 性能断言
    assert em_mock.call_count == 1, "Eastmoney snapshot 必须且仅拉取 1 次"
    assert mock_adapter.get_daily_bars.call_count == 2, "正常 pytdx sentinel 必须恰好 2 次"

    # 可观测性断言
    assert result.universe_discovery_status == "success"
    assert result.daily_primary_source == "pytdx"
    assert result.daily_pytdx_rows == 2
    assert result.daily_eastmoney_rows == 1
    assert result.daily_bj_rows == 1
    assert result.snapshot_upserted == 3
    assert result.period_counts["d"] == 3

    # 行情来源与 previous_close 断言
    assert prev_close_map["600519"] == Decimal("1580.0")  # pytdx
    assert prev_close_map["000001"] == Decimal("11.8")    # pytdx
    assert prev_close_map["830001"] == Decimal("14.5")    # EM BJ


@pytest.mark.asyncio
async def test_case_b_pytdx_total_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case B — pytdx 整体失败：
    EM discovery ✅, pytdx ❌
    SH/SZ/BJ → cached EM
    EM fetch count = 1（禁止第二次网络拉 EM）
    """
    service = BarsSchedulerService(fetch_processes=1)
    inst_sh = _make_inst("600519", "SH")
    inst_sz = _make_inst("000001", "SZ")
    inst_bj = _make_inst("830001", "BJ")
    active_instruments = [inst_sh, inst_sz, inst_bj]
    session = _ScriptedSession(select_queue=[active_instruments])

    em_mock = AsyncMock(
        return_value=[
            {"f12": "600519", "f13": 1},
            {"f12": "000001", "f13": 0},
            {"f12": "830001", "f13": 0},
        ]
    )
    monkeypatch.setattr(prov, "fetch_full_a_share_snapshot", em_mock)
    monkeypatch.setattr(
        prov,
        "normalize_snapshot_rows",
        lambda raw: [
            _make_em_row("600519", "SH", close_p="1590.0", prev_close="1570.0"),
            _make_em_row("000001", "SZ", close_p="11.9", prev_close="11.7"),
            _make_em_row("830001", "BJ", close_p="15.0", prev_close="14.5"),
        ],
    )
    monkeypatch.setattr(
        refresh_mod, "count_active_a_share_instruments", AsyncMock(return_value=3)
    )
    monkeypatch.setattr(
        refresh_mod, "check_snapshot_universe_sanity", lambda *a, **k: None
    )
    monkeypatch.setattr(
        refresh_mod,
        "sync_instruments_from_eod_snapshot",
        AsyncMock(return_value=InstrumentSyncResult()),
    )
    monkeypatch.setattr(
        refresh_mod, "upsert_raw_daily_snapshot", AsyncMock(return_value=3)
    )
    monkeypatch.setattr(
        refresh_mod, "find_missing_daily_instruments", AsyncMock(return_value=[])
    )

    # pytdx 抛出源异常
    mock_adapter = MagicMock()
    mock_adapter.get_security_quotes_with_provenance.side_effect = PytdxEodSnapshotError("mock connection failed")

    result = BatchResult()
    prev_close_map = await service._refresh_daily_from_market_snapshot(
        TRADE_DATE,
        session,
        None,
        result,
        adapter=mock_adapter,
        pytdx_batch_interval_seconds=0.0,
    )

    # 性能断言：绝不拉取第二次 EM
    assert em_mock.call_count == 1, "pytdx 失败时禁止第二次网络拉取 Eastmoney"

    # 可观测性与降级断言
    assert result.universe_discovery_status == "success"
    assert result.daily_primary_source == "eastmoney"
    assert result.daily_pytdx_rows == 0
    assert result.daily_eastmoney_rows == 3
    assert result.daily_bj_rows == 1
    assert result.snapshot_upserted == 3

    # 全部使用 EM 的 previous_close
    assert prev_close_map["600519"] == Decimal("1570.0")
    assert prev_close_map["000001"] == Decimal("11.7")
    assert prev_close_map["830001"] == Decimal("14.5")


@pytest.mark.asyncio
async def test_case_c_pytdx_partial_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case C — pytdx partial：
    pytdx 大多数 SH/SZ ✅，少数 missing
    missing SH/SZ → cached EM, BJ → EM
    """
    service = BarsSchedulerService(fetch_processes=1)
    inst_sh1 = _make_inst("600519", "SH")
    inst_sh2 = _make_inst("600000", "SH")
    inst_sz = _make_inst("000001", "SZ")
    inst_bj = _make_inst("830001", "BJ")
    active_instruments = [inst_sh1, inst_sh2, inst_sz, inst_bj]
    session = _ScriptedSession(select_queue=[active_instruments])

    em_mock = AsyncMock(
        return_value=[
            {"f12": "600519"}, {"f12": "600000"}, {"f12": "000001"}, {"f12": "830001"}
        ]
    )
    monkeypatch.setattr(prov, "fetch_full_a_share_snapshot", em_mock)
    monkeypatch.setattr(
        prov,
        "normalize_snapshot_rows",
        lambda raw: [
            _make_em_row("600519", "SH", close_p="1590.0", prev_close="1570.0"),
            _make_em_row("600000", "SH", close_p="8.0", prev_close="7.9"),
            _make_em_row("000001", "SZ", close_p="11.9", prev_close="11.7"),
            _make_em_row("830001", "BJ", close_p="15.0", prev_close="14.5"),
        ],
    )
    monkeypatch.setattr(
        refresh_mod, "count_active_a_share_instruments", AsyncMock(return_value=4)
    )
    monkeypatch.setattr(
        refresh_mod, "check_snapshot_universe_sanity", lambda *a, **k: None
    )
    monkeypatch.setattr(
        refresh_mod,
        "sync_instruments_from_eod_snapshot",
        AsyncMock(return_value=InstrumentSyncResult()),
    )
    monkeypatch.setattr(
        refresh_mod, "upsert_raw_daily_snapshot", AsyncMock(return_value=4)
    )
    monkeypatch.setattr(
        refresh_mod, "find_missing_daily_instruments", AsyncMock(return_value=[])
    )

    # pytdx 只返回 600519 和 000001，缺失 600000
    mock_adapter = MagicMock()
    mock_adapter.get_security_quotes_with_provenance.return_value = (
        [
            _pytdx_quote_dict("600519", 1, price=1600.0, last_close=1580.0),
            _pytdx_quote_dict("000001", 0, price=12.0, last_close=11.8),
        ],
        PytdxCallProvenance(server=("1.1.1.1", 7709), connection_generation=1),
    )

    def fake_get_daily_bars(symbol: str, start: date, end: date) -> pd.DataFrame:
        if symbol == "600519":
            return _daily_bar_df("600519", close_p=1600.0)
        if symbol == "000001":
            return _daily_bar_df("000001", close_p=12.0)
        return pd.DataFrame()

    mock_adapter.get_daily_bars.side_effect = fake_get_daily_bars

    result = BatchResult()
    prev_close_map = await service._refresh_daily_from_market_snapshot(
        TRADE_DATE,
        session,
        None,
        result,
        adapter=mock_adapter,
        pytdx_batch_interval_seconds=0.0,
    )

    assert em_mock.call_count == 1
    assert result.daily_primary_source == "mixed"
    assert result.daily_pytdx_rows == 2
    assert result.daily_eastmoney_rows == 2  # 600000 fallback + 830001 BJ
    assert result.daily_bj_rows == 1
    assert result.snapshot_upserted == 4

    # 600519 / 000001 来自 pytdx
    assert prev_close_map["600519"] == Decimal("1580.0")
    assert prev_close_map["000001"] == Decimal("11.8")
    # 600000 来自 EM fallback
    assert prev_close_map["600000"] == Decimal("7.9")
    # 830001 来自 EM BJ
    assert prev_close_map["830001"] == Decimal("14.5")


@pytest.mark.asyncio
async def test_case_d_em_discovery_failed_pytdx_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case D — EM discovery 失败，pytdx 正常：
    EM ❌
    使用当前 DB universe
    SH/SZ → pytdx ✅
    证明：EM discovery failure 不会阻断 SH/SZ pytdx primary
    状态必须明确：universe discovery failed，不得伪装成功。
    """
    service = BarsSchedulerService(fetch_processes=1)
    inst_sh = _make_inst("600519", "SH")
    inst_sz = _make_inst("000001", "SZ")
    active_instruments = [inst_sh, inst_sz]
    session = _ScriptedSession(select_queue=[active_instruments])

    # Eastmoney discovery 失败
    em_mock = AsyncMock(side_effect=Exception("Eastmoney gateway timeout"))
    monkeypatch.setattr(prov, "fetch_full_a_share_snapshot", em_mock)

    monkeypatch.setattr(
        refresh_mod, "count_active_a_share_instruments", AsyncMock(return_value=2)
    )
    monkeypatch.setattr(
        refresh_mod, "upsert_raw_daily_snapshot", AsyncMock(return_value=2)
    )
    monkeypatch.setattr(
        refresh_mod, "find_missing_daily_instruments", AsyncMock(return_value=[])
    )

    # pytdx 正常
    mock_adapter = MagicMock()
    mock_adapter.get_security_quotes_with_provenance.return_value = (
        [
            _pytdx_quote_dict("600519", 1, price=1600.0, last_close=1580.0),
            _pytdx_quote_dict("000001", 0, price=12.0, last_close=11.8),
        ],
        PytdxCallProvenance(server=("1.1.1.1", 7709), connection_generation=1),
    )

    def fake_get_daily_bars(symbol: str, start: date, end: date) -> pd.DataFrame:
        if symbol == "600519":
            return _daily_bar_df("600519", close_p=1600.0)
        if symbol == "000001":
            return _daily_bar_df("000001", close_p=12.0)
        return pd.DataFrame()

    mock_adapter.get_daily_bars.side_effect = fake_get_daily_bars

    result = BatchResult()
    prev_close_map = await service._refresh_daily_from_market_snapshot(
        TRADE_DATE,
        session,
        None,
        result,
        adapter=mock_adapter,
        pytdx_batch_interval_seconds=0.0,
    )

    # 明确状态：discovery 失败，但 pytdx 正常执行
    assert result.universe_discovery_status == "failed", (
        "Eastmoney discovery 失败时不得伪装成功"
    )
    assert result.daily_primary_source == "pytdx"
    assert result.daily_pytdx_rows == 2
    assert result.daily_eastmoney_rows == 0
    assert result.snapshot_upserted == 2
    assert prev_close_map["600519"] == Decimal("1580.0")


@pytest.mark.asyncio
async def test_case_e_both_failed_raises_snapshot_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case E — EM failed + pytdx failed：
    快速路径不能假成功，raise SnapshotProviderError 让 outer legacy 接手。
    """
    service = BarsSchedulerService(fetch_processes=1)
    inst_sh = _make_inst("600519", "SH")
    session = _ScriptedSession(select_queue=[[inst_sh]])

    # EM 失败
    monkeypatch.setattr(
        prov,
        "fetch_full_a_share_snapshot",
        AsyncMock(side_effect=Exception("Eastmoney down")),
    )

    # pytdx 失败
    mock_adapter = MagicMock()
    mock_adapter.get_security_quotes_with_provenance.side_effect = PytdxEodSnapshotError(
        "pytdx all servers unreachable"
    )

    result = BatchResult()
    with pytest.raises(SnapshotProviderError, match="both pytdx primary and Eastmoney"):
        await service._refresh_daily_from_market_snapshot(
            TRADE_DATE,
            session,
            None,
            result,
            adapter=mock_adapter,
            pytdx_batch_interval_seconds=0.0,
        )


@pytest.mark.asyncio
async def test_case_f_pytdx_verifier_sentinel_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case F — pytdx verifier/coherence 失败（sentinel mismatch）：
    行为与 source failure 一样：使用 cached EM fallback。
    """
    service = BarsSchedulerService(fetch_processes=1)
    inst_sh = _make_inst("600519", "SH")
    inst_sz = _make_inst("000001", "SZ")
    session = _ScriptedSession(select_queue=[[inst_sh, inst_sz]])

    em_mock = AsyncMock(
        return_value=[{"f12": "600519"}, {"f12": "000001"}]
    )
    monkeypatch.setattr(prov, "fetch_full_a_share_snapshot", em_mock)
    monkeypatch.setattr(
        prov,
        "normalize_snapshot_rows",
        lambda raw: [
            _make_em_row("600519", "SH", close_p="1590.0", prev_close="1570.0"),
            _make_em_row("000001", "SZ", close_p="11.9", prev_close="11.7"),
        ],
    )
    monkeypatch.setattr(
        refresh_mod, "count_active_a_share_instruments", AsyncMock(return_value=2)
    )
    monkeypatch.setattr(
        refresh_mod, "check_snapshot_universe_sanity", lambda *a, **k: None
    )
    monkeypatch.setattr(
        refresh_mod,
        "sync_instruments_from_eod_snapshot",
        AsyncMock(return_value=InstrumentSyncResult()),
    )
    monkeypatch.setattr(
        refresh_mod, "upsert_raw_daily_snapshot", AsyncMock(return_value=2)
    )
    monkeypatch.setattr(
        refresh_mod, "find_missing_daily_instruments", AsyncMock(return_value=[])
    )

    # pytdx quotes 返回，但 sentinel 校验时价格不符
    mock_adapter = MagicMock()
    mock_adapter.get_security_quotes_with_provenance.return_value = (
        [
            _pytdx_quote_dict("600519", 1, price=1600.0),
            _pytdx_quote_dict("000001", 0, price=12.0),
        ],
        PytdxCallProvenance(server=("1.1.1.1", 7709), connection_generation=1),
    )

    # daily sentinel 价格差异过大（1500 vs 1600 > 0.01）→ 触发 verifier fail-closed
    def bad_sentinel_bars(symbol: str, start: date, end: date) -> pd.DataFrame:
        return _daily_bar_df(symbol, close_p=1500.0)

    mock_adapter.get_daily_bars.side_effect = bad_sentinel_bars

    result = BatchResult()
    prev_close_map = await service._refresh_daily_from_market_snapshot(
        TRADE_DATE,
        session,
        None,
        result,
        adapter=mock_adapter,
        pytdx_batch_interval_seconds=0.0,
    )

    # 应降级到 EM 缓存
    assert em_mock.call_count == 1
    assert result.daily_primary_source == "eastmoney"
    assert result.daily_pytdx_rows == 0
    assert result.daily_eastmoney_rows == 2
    assert prev_close_map["600519"] == Decimal("1570.0")


@pytest.mark.asyncio
async def test_case_g_source_precedence_and_previous_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case G — source precedence：
    同一个 SH symbol：pytdx row 与 EM row 值故意不同
    最终 upsert 必须使用 pytdx，并且 previous_close evidence 也必须来自 pytdx。
    """
    service = BarsSchedulerService(fetch_processes=1)
    inst_sh = _make_inst("600519", "SH")
    inst_sz = _make_inst("000001", "SZ")
    session = _ScriptedSession(select_queue=[[inst_sh, inst_sz]])

    # EM 中 600519 close=1590, prev=1570
    monkeypatch.setattr(
        prov,
        "fetch_full_a_share_snapshot",
        AsyncMock(return_value=[{"f12": "600519"}, {"f12": "000001"}]),
    )
    monkeypatch.setattr(
        prov,
        "normalize_snapshot_rows",
        lambda raw: [
            _make_em_row("600519", "SH", close_p="1590.0", prev_close="1570.0"),
            _make_em_row("000001", "SZ", close_p="11.9", prev_close="11.7"),
        ],
    )
    monkeypatch.setattr(
        refresh_mod, "count_active_a_share_instruments", AsyncMock(return_value=2)
    )
    monkeypatch.setattr(
        refresh_mod, "check_snapshot_universe_sanity", lambda *a, **k: None
    )
    monkeypatch.setattr(
        refresh_mod,
        "sync_instruments_from_eod_snapshot",
        AsyncMock(return_value=InstrumentSyncResult()),
    )
    upserted_rows: list[tuple[Any, EodSnapshotRow]] = []

    async def fake_upsert(sess: Any, dt: Any, pairs: Any) -> int:
        upserted_rows.extend(pairs)
        return len(pairs)

    monkeypatch.setattr(refresh_mod, "upsert_raw_daily_snapshot", fake_upsert)
    monkeypatch.setattr(
        refresh_mod, "find_missing_daily_instruments", AsyncMock(return_value=[])
    )

    # pytdx 中 600519 close=1600, prev=1580
    mock_adapter = MagicMock()
    mock_adapter.get_security_quotes_with_provenance.return_value = (
        [
            _pytdx_quote_dict("600519", 1, price=1600.0, last_close=1580.0),
            _pytdx_quote_dict("000001", 0, price=12.0, last_close=11.8),
        ],
        PytdxCallProvenance(server=("1.1.1.1", 7709), connection_generation=1),
    )

    def fake_get_daily_bars(symbol: str, start: date, end: date) -> pd.DataFrame:
        if symbol == "600519":
            return _daily_bar_df("600519", close_p=1600.0)
        return _daily_bar_df("000001", close_p=12.0)

    mock_adapter.get_daily_bars.side_effect = fake_get_daily_bars

    result = BatchResult()
    prev_close_map = await service._refresh_daily_from_market_snapshot(
        TRADE_DATE,
        session,
        None,
        result,
        adapter=mock_adapter,
        pytdx_batch_interval_seconds=0.0,
    )

    # 检查落库的 row 价格是 pytdx 的 1600.0，而非 EM 的 1590.0
    upserted_by_sym = {row.symbol: row for _, row in upserted_rows}
    assert upserted_by_sym["600519"].close == Decimal("1600.0")
    assert upserted_by_sym["600519"].previous_close == Decimal("1580.0")

    # 检查 previous_close evidence 映射来自 pytdx
    assert prev_close_map["600519"] == Decimal("1580.0")


@pytest.mark.asyncio
async def test_case_h_bj_never_requested_from_pytdx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case H — BJ policy：
    断言：BJ 从未出现在 pytdx request symbols
    BJ row 来自 EM
    """
    service = BarsSchedulerService(fetch_processes=1)
    inst_sh = _make_inst("600519", "SH")
    inst_sz = _make_inst("000001", "SZ")
    inst_bj1 = _make_inst("830001", "BJ")
    inst_bj2 = _make_inst("920002", "BJ")
    session = _ScriptedSession(select_queue=[[inst_sh, inst_sz, inst_bj1, inst_bj2]])

    monkeypatch.setattr(
        prov,
        "fetch_full_a_share_snapshot",
        AsyncMock(return_value=[
            {"f12": "600519"}, {"f12": "000001"},
            {"f12": "830001"}, {"f12": "920002"}
        ]),
    )
    monkeypatch.setattr(
        prov,
        "normalize_snapshot_rows",
        lambda raw: [
            _make_em_row("600519", "SH", close_p="1600.0", prev_close="1580.0"),
            _make_em_row("000001", "SZ", close_p="12.0", prev_close="11.8"),
            _make_em_row("830001", "BJ", close_p="20.0", prev_close="19.5"),
            _make_em_row("920002", "BJ", close_p="30.0", prev_close="29.0"),
        ],
    )
    monkeypatch.setattr(
        refresh_mod, "count_active_a_share_instruments", AsyncMock(return_value=4)
    )
    monkeypatch.setattr(
        refresh_mod, "check_snapshot_universe_sanity", lambda *a, **k: None
    )
    monkeypatch.setattr(
        refresh_mod,
        "sync_instruments_from_eod_snapshot",
        AsyncMock(return_value=InstrumentSyncResult()),
    )
    upserted_rows: list[tuple[Any, EodSnapshotRow]] = []

    async def fake_upsert(sess: Any, dt: Any, pairs: Any) -> int:
        upserted_rows.extend(pairs)
        return len(pairs)

    monkeypatch.setattr(refresh_mod, "upsert_raw_daily_snapshot", fake_upsert)
    monkeypatch.setattr(
        refresh_mod, "find_missing_daily_instruments", AsyncMock(return_value=[])
    )

    requested_pytdx_symbols: list[str] = []

    mock_adapter = MagicMock()

    def record_quotes(symbols: list[str]) -> tuple[list[dict], PytdxCallProvenance]:
        requested_pytdx_symbols.extend(symbols)
        return (
            [
                _pytdx_quote_dict("600519", 1, price=1600.0, last_close=1580.0),
                _pytdx_quote_dict("000001", 0, price=12.0, last_close=11.8),
            ],
            PytdxCallProvenance(server=("1.1.1.1", 7709), connection_generation=1),
        )

    mock_adapter.get_security_quotes_with_provenance.side_effect = record_quotes

    def fake_get_daily_bars(symbol: str, start: date, end: date) -> pd.DataFrame:
        if symbol == "600519":
            return _daily_bar_df("600519", close_p=1600.0)
        return _daily_bar_df("000001", close_p=12.0)

    mock_adapter.get_daily_bars.side_effect = fake_get_daily_bars

    result = BatchResult()
    prev_close_map = await service._refresh_daily_from_market_snapshot(
        TRADE_DATE,
        session,
        None,
        result,
        adapter=mock_adapter,
        pytdx_batch_interval_seconds=0.0,
    )

    # 关键断言：BJ 标的绝不出现在 pytdx 请求参数中
    assert "830001" not in requested_pytdx_symbols
    assert "920002" not in requested_pytdx_symbols
    assert set(requested_pytdx_symbols) == {"600519", "000001"}

    # BJ row 来自 EM
    upserted_by_sym = {row.symbol: row for _, row in upserted_rows}
    assert upserted_by_sym["830001"].close == Decimal("20.0")
    assert upserted_by_sym["920002"].close == Decimal("30.0")
    assert prev_close_map["830001"] == Decimal("19.5")
    assert prev_close_map["920002"] == Decimal("29.0")
    assert result.daily_bj_rows == 2
