"""pytdx 批量 EOD 行情快照 provider 单元测试（G1B-1.1，纯单元测试，不联网/不连 DB/Redis）。

运行：
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_pytdx_eod_snapshot_provider.py -q

覆盖：
- 批量 + identity + 边界（用户 G1B-1.1 第 10 条 A~K）
- 不逐股 K / 不 XDXR / 不 DB / 不 Redis / 不 silent 修正
- A/B 诊断 compare_quote_to_daily_reference（volume/amount ratio）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.core.pytdx_adapter import PytdxSourceError
from app.services.pytdx_eod_snapshot_provider import (
    PYTDX_QUOTE_BATCH_INTERVAL_SECONDS,
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


def _captured() -> datetime:
    return datetime(2026, 9, 11, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai"))


def _reference_row(symbol: str, volume: str, amount: str = "10000") -> object:
    """canonical daily K 参考（仅用于 A/B 诊断，不进入 provider）。"""
    from app.services.eod_market_snapshot_provider import EodSnapshotRow

    return EodSnapshotRow(
        symbol=symbol,
        name="",
        market="SH",
        updated_at=_captured(),
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

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    assert len(calls) == 3
    assert all(len(c) <= PYTDX_QUOTE_BATCH_SIZE for c in calls)
    assert snap.requested_count == 161
    assert snap.returned_count == 161
    assert snap.requested_trade_date == date(2026, 9, 11)


def test_bj_not_requested_but_other_market_errors() -> None:
    insts = [_Inst("600519", "SH"), _Inst("000001", "SZ"), _Inst("920808", "BJ")]
    seen: list[list[str]] = []
    adapter = MagicMock()

    def fake(symbols: list[str]) -> list[dict[str, object]]:
        seen.append(list(symbols))
        # 按真实 pytdx market int 返回：SH=1, SZ=0
        return [_quote("600519", 1), _quote("000001", 0)]

    adapter.get_security_quotes.side_effect = fake

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    assert all("920808" not in batch for batch in seen)
    assert snap.requested_count == 2
    assert snap.returned_count == 2


def test_unknown_market_raises_before_network() -> None:
    insts = [_Inst("600519", "XX")]
    adapter = MagicMock()

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )

    adapter.get_security_quotes.assert_not_called()


def test_return_order_shuffled_still_aligned_by_market_code() -> None:
    insts = [
        _Inst("600519", "SH"),
        _Inst("000001", "SZ"),
        _Inst("600000", "SH"),
    ]
    adapter = MagicMock()
    # 返回顺序打乱，且覆盖两种 market 值
    adapter.get_security_quotes.return_value = [
        _quote("000001", 0),
        _quote("600000", 1),
        _quote("600519", 1),
    ]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    assert {r.symbol for r in snap.rows} == {"600519", "000001", "600000"}


# ---- A. 返回 identity 越出当前 batch ----
def test_identity_outside_current_batch_raises() -> None:
    insts = [_Inst(f"{i:06d}", "SH") for i in range(81)]  # batch1:80, batch2:1(000080)
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("000080")]  # 属于 batch2

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )


# ---- B. code 正确但 market 错误 ----
def test_market_mismatch_raises() -> None:
    insts = [_Inst("600519", "SH")]  # expected market 1
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519", market=0)]  # wrong market

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )


# ---- C. raw market=True（bool 不能当 int）----
def test_raw_market_bool_raises() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519", market=True)]

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )


# ---- D. raw row 非 Mapping ----
def test_raw_row_non_mapping_raises() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = ["not-a-mapping"]

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )


# ---- E. 输入重复 (market, symbol) → 网络 0 次 ----
def test_duplicate_input_identity_raises_before_network() -> None:
    insts = [_Inst("600519", "SH"), _Inst("600519", "SH")]
    adapter = MagicMock()

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )

    adapter.get_security_quotes.assert_not_called()


# ---- F. 输入未知 market → 网络 0 次（见 test_unknown_market_raises_before_network）----


def test_duplicate_quote_code_raises() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519"), _quote("600519")]

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )


def test_missing_symbol_recorded_not_faked() -> None:
    insts = [_Inst("600519", "SH"), _Inst("000001", "SZ")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519")]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

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
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )

    assert isinstance(ei.value.__cause__, PytdxSourceError)


def test_no_get_daily_bars() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519")]

    fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    adapter.get_daily_bars.assert_not_called()


def test_no_get_xdxr_info() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519")]

    fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    adapter.get_xdxr_info.assert_not_called()


def test_no_db_redis_handle() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519")]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    assert not hasattr(snap, "session")
    assert not hasattr(snap, "redis")
    assert not hasattr(snap, "raw_servertime_by_symbol")


def test_malformed_ohlc_not_silently_fixed() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [
        _quote("600519", open=-5.0, high=9.0, low=20.0, price=10.0)
    ]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    row = snap.rows[0]
    assert row.open == Decimal("-5.0")  # 负值未修正为 0/正数
    assert row.high == Decimal("9.0")
    assert row.low == Decimal("20.0")  # high<low 未交换
    assert row.high < row.low


def test_raw_volume_unit_unverified_and_source_time_preserved() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519", servertime="15:00:03")]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    assert snap.volume_unit == "UNVERIFIED"
    assert snap.rows[0].raw_volume == Decimal("1000.0")  # 字段名 raw_volume，未 ×100
    assert not hasattr(snap.rows[0], "volume")
    assert snap.rows[0].source_time == "15:00:03"
    assert snap.rows[0].raw_payload["vol"] == 1000.0


# ---- H. Decimal("Infinity") → 字段 None，raw_payload 保留证据 ----
def test_non_finite_parsed_none_raw_payload_kept() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    q = _quote("600519")
    q["price"] = Decimal("Infinity")
    adapter.get_security_quotes.return_value = [q]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    row = snap.rows[0]
    assert row.close is None
    assert row.raw_payload["price"] == Decimal("Infinity")


# ---- I. source_time 仅保存，row 无 trade_date / updated_at ----
def test_no_trade_date_or_updated_at_on_row() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519", servertime="15:00:03")]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    row = snap.rows[0]
    assert row.source_time == "15:00:03"
    assert not hasattr(row, "trade_date")
    assert not hasattr(row, "updated_at")


# ---- J. 161 symbols → 3 个 quote batch，interval=0 不 sleep ----
def test_batch_interval_zero_no_sleep() -> None:
    insts = [_Inst(f"{i:06d}", "SH") for i in range(161)]
    adapter = MagicMock()
    adapter.get_security_quotes.side_effect = lambda syms: [_quote(s) for s in syms]

    with patch("time.sleep") as fake_sleep:
        snap = fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )

    assert len(snap.rows) == 161
    fake_sleep.assert_not_called()


# ---- K. 161 symbols + 真实 interval=0.3 → sleep 恰好 2 次，每次 0.3 ----
def test_batch_interval_sleep_calls() -> None:
    insts = [_Inst(f"{i:06d}", "SH") for i in range(161)]
    adapter = MagicMock()
    adapter.get_security_quotes.side_effect = lambda syms: [_quote(s) for s in syms]

    with patch("time.sleep") as fake_sleep:
        fetch_pytdx_eod_snapshot(
            adapter,
            insts,
            trade_date=date(2026, 9, 11),
            batch_interval_seconds=PYTDX_QUOTE_BATCH_INTERVAL_SECONDS,
        )

    assert fake_sleep.call_count == 2
    assert all(c.args == (PYTDX_QUOTE_BATCH_INTERVAL_SECONDS,) for c in fake_sleep.call_args_list)


def test_compare_quote_to_daily_reference_volume_ratio() -> None:
    """A/B 诊断：raw_volume(手)=100 vs daily volume(股)=10000 → ratio≈0.01。"""
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519", vol=100.0)]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    res = compare_quote_to_daily_reference(snap.rows, [_reference_row("600519", "10000")])

    assert res["compared_count"] == 1
    assert res["volume_ratio_median"] == pytest.approx(0.01)
    assert res["amount_ratio_median"] == pytest.approx(1.0)


def test_compare_quote_skips_unmatched_reference() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = MagicMock()
    adapter.get_security_quotes.return_value = [_quote("600519", vol=100.0)]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    res = compare_quote_to_daily_reference(snap.rows, [_reference_row("000001", "10000")])

    assert res["compared_count"] == 0
