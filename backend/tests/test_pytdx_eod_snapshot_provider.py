"""pytdx 批量 EOD 快照 provider 单元测试（G1B-1，纯单元测试，不联网/不连 DB/Redis）。

运行：
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_pytdx_eod_snapshot_provider.py -q

覆盖（用户 G1B-1 第 10 条）：
1. 161 symbol / batch 80 → get_security_quotes 恰好 3 次，每批 <=80
2. BJ 不进入请求
3. 返回顺序变化 → identity 仍按 symbol 对齐
4. 返回未知 symbol → error
5. 同 symbol 重复返回 → error
6. 某请求 symbol 缺失 → snapshot 成功 + missing_symbols，不伪造 row
7. provider 抛 PytdxSourceError → 包装为 typed snapshot error，不返回空 snapshot
8. 不调用 get_daily_bars
9. 不调用 get_xdxr_info
10. 不访问 DB / Redis（provider 无 session/redis 句柄，仅调 get_security_quotes）
11. OHLC 非有限值 / 负值 / high<low → 不 silent 修正
+ A/B 诊断 compare_quote_to_daily_reference（volume/amount ratio）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.core.pytdx_adapter import PytdxSourceError
from app.services.eod_market_snapshot_provider import EodSnapshotRow
from app.services.pytdx_eod_snapshot_provider import (
    PYTDX_QUOTE_BATCH_SIZE,
    PytdxEodSnapshotError,
    compare_quote_to_daily_reference,
    fetch_pytdx_eod_snapshot,
)


@dataclass
class _Inst:
    symbol: str
    market: str
    name: str = ""


def _quote(code: str, market: int = 1, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "code": code,
        "market": market,
        "price": 10.0,
        "open": 9.9,
        "high": 10.1,
        "low": 9.8,
        "last_close": 9.5,
        "vol": 1000.0,
        "amount": 10000.0,
        "servertime": "15:00:03",
    }
    base.update(kw)
    return base


def _row(symbol: str, volume: str, amount: str = "10000") -> EodSnapshotRow:
    return EodSnapshotRow(
        symbol=symbol,
        name="",
        market="SH",
        updated_at=datetime(2026, 9, 11, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        open=Decimal("9.9"),
        high=Decimal("10.1"),
        low=Decimal("9.8"),
        close=Decimal("10.0"),
        volume=Decimal(volume),
        amount=Decimal(amount),
        previous_close=Decimal("9.5"),
    )


def test_batch_count_for_161_symbols() -> None:
    insts = [_Inst(f"{i:06d}", "SH") for i in range(161)]
    calls: list[list[str]] = []

    def fake_get(symbols: list[str]) -> list[dict[str, object]]:
        calls.append(list(symbols))
        return [_quote(s) for s in symbols]

    adapter = MagicMock()
    adapter.get_security_quotes.side_effect = fake_get

    snap = fetch_pytdx_eod_snapshot(adapter, insts, trade_date=date(2026, 9, 11))

    assert len(calls) == 3
    assert all(len(c) <= PYTDX_QUOTE_BATCH_SIZE for c in calls)
    assert snap.requested_count == 161
    assert snap.returned_count == 161


def test_bj_not_requested() -> None:
    insts = [_Inst("600519", "SH"), _Inst("000001", "SZ"), _Inst("920808", "BJ")]
    seen: list[list[str]] = []
    adapter = MagicMock()
    adapter.get_security_quotes.side_effect = (
        lambda syms: seen.append(list(syms)) or [_quote(s) for s in syms]
    )

    snap = fetch_pytdx_eod_snapshot(adapter, insts, trade_date=date(2026, 9, 11))

    assert all("920808" not in batch for batch in seen)
    assert snap.requested_count == 2
    assert snap.returned_count == 2


def test_return_order_shuffled_still_aligned() -> None:
    insts = [_Inst("600519", "SH"), _Inst("000001", "SZ"), _Inst("600000", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [
        _quote("000001", 0),
        _quote("600000", 1),
        _quote("600519", 1),
    ]

    snap = fetch_pytdx_eod_snapshot(adapter, insts, trade_date=date(2026, 9, 11))

    assert {r.symbol for r in snap.rows} == {"600519", "000001", "600000"}


def test_unrequested_symbol_raises() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519"), _quote("000002")]

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(adapter, insts, trade_date=date(2026, 9, 11))


def test_duplicate_symbol_raises() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519"), _quote("600519")]

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(adapter, insts, trade_date=date(2026, 9, 11))


def test_missing_symbol_recorded_not_faked() -> None:
    insts = [_Inst("600519", "SH"), _Inst("000001", "SZ")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519")]

    snap = fetch_pytdx_eod_snapshot(adapter, insts, trade_date=date(2026, 9, 11))

    assert snap.missing_symbols == ("000001",)
    assert [r.symbol for r in snap.rows] == ["600519"]
    assert all(r.symbol != "000001" for r in snap.rows)


def test_provider_source_error_wrapped() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.side_effect = PytdxSourceError(
        operation="get_security_quotes", message="down"
    )

    with pytest.raises(PytdxEodSnapshotError) as ei:
        fetch_pytdx_eod_snapshot(adapter, insts, trade_date=date(2026, 9, 11))

    assert isinstance(ei.value.__cause__, PytdxSourceError)


def test_no_get_daily_bars() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519")]

    fetch_pytdx_eod_snapshot(adapter, insts, trade_date=date(2026, 9, 11))

    adapter.get_daily_bars.assert_not_called()


def test_no_get_xdxr_info() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519")]

    fetch_pytdx_eod_snapshot(adapter, insts, trade_date=date(2026, 9, 11))

    adapter.get_xdxr_info.assert_not_called()


def test_no_db_redis_handle() -> None:
    """provider 既不接收 session/redis 句柄，也不应触达 DB/Redis。"""
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519")]

    snap = fetch_pytdx_eod_snapshot(adapter, insts, trade_date=date(2026, 9, 11))

    # 结构上无 DB/Redis 通道：snapshot 不携带任何 session/redis 句柄。
    assert not hasattr(snap, "session")
    assert not hasattr(snap, "redis")
    # 仅 get_security_quotes 被调用，无额外 provider I/O。
    assert adapter.get_security_quotes.call_count == 1
    adapter.get_daily_bars.assert_not_called()
    adapter.get_xdxr_info.assert_not_called()


def test_malformed_ohlc_not_silently_fixed() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [
        _quote("600519", open=-5.0, high=9.0, low=20.0, price=10.0)
    ]

    snap = fetch_pytdx_eod_snapshot(adapter, insts, trade_date=date(2026, 9, 11))

    row = snap.rows[0]
    assert row.open == Decimal("-5.0")  # 负值未修正为 0/正数
    assert row.high == Decimal("9.0")
    assert row.low == Decimal("20.0")  # high<low 未交换
    assert row.high < row.low  # 结构错误被保留，未 silent 修正


def test_volume_unit_unverified_and_servertime_preserved() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519", servertime="15:00:03")]

    snap = fetch_pytdx_eod_snapshot(adapter, insts, trade_date=date(2026, 9, 11))

    assert snap.volume_unit == "UNVERIFIED"
    # vol 原始值落库，未 ×100
    assert snap.rows[0].volume == Decimal("1000.0")
    assert snap.raw_servertime_by_symbol == {"600519": "15:00:03"}


def test_compare_quote_to_daily_reference_volume_ratio() -> None:
    """A/B 诊断：quote vol(手)=100 vs daily vol(股)=10000 → ratio≈0.01。"""
    q = _row("600519", "100")
    ref = _row("600519", "10000")

    res = compare_quote_to_daily_reference([q], [ref])

    assert res["compared_count"] == 1
    assert res["volume_ratio_median"] == pytest.approx(0.01)
    assert res["amount_ratio_median"] == pytest.approx(1.0)


def test_compare_quote_skips_unmatched_reference() -> None:
    q = _row("600519", "100")
    ref = _row("000001", "10000")  # 不同 symbol

    res = compare_quote_to_daily_reference([q], [ref])

    assert res["compared_count"] == 0
