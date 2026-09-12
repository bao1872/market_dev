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

import pandas as pd
import pytest

from app.core.pytdx_adapter import PytdxCallProvenance, PytdxSourceError
from app.services.pytdx_eod_snapshot_provider import (
    PYTDX_QUOTE_BATCH_INTERVAL_SECONDS,
    PYTDX_QUOTE_BATCH_SIZE,
    PYTDX_QUOTE_LOT_TO_SHARES,
    PYTDX_QUOTE_VOLUME_UNIT_LOTS,
    PytdxEodSnapshotError,
    VerifiedPytdxEodSnapshot,
    compare_quote_to_daily_reference,
    fetch_pytdx_eod_snapshot,
    to_canonical_eod_rows,
    verify_pytdx_eod_snapshot,
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


_PROV = PytdxCallProvenance(server=("159.75.55.232", 7709), connection_generation=1)


def _mk_adapter() -> MagicMock:
    """provenance-aware MagicMock adapter。

    ``get_security_quotes_with_provenance`` 动态委托给 ``get_security_quotes``，
    沿用各测试已配置的 ``return_value`` / ``side_effect``，并附加固定 provenance。
    """
    adapter = MagicMock()
    adapter.get_security_quotes_with_provenance.side_effect = (
        lambda syms: (adapter.get_security_quotes(syms), _PROV)
    )
    return adapter


def _mk_prov_adapter(provs: list[PytdxCallProvenance]) -> MagicMock:
    """按批返回脚本化 provenance 的 adapter（用于跨 connection coherence 测试）。"""
    adapter = MagicMock()
    pending = list(provs)

    def _call(syms: list[str]):  # noqa: ANN202
        prov = pending.pop(0) if pending else _PROV
        return [_quote(s) for s in syms], prov

    adapter.get_security_quotes_with_provenance.side_effect = _call
    return adapter


def test_batch_count_for_161_symbols() -> None:
    insts = [_Inst(f"{i:06d}", "SH") for i in range(161)]
    calls: list[list[str]] = []

    def fake_get(symbols: list[str]) -> list[dict[str, object]]:
        calls.append(list(symbols))
        return [_quote(s) for s in symbols]

    adapter = _mk_adapter()
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
    adapter = _mk_adapter()

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
    adapter = _mk_adapter()

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
    adapter = _mk_adapter()
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
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("000080")]  # 属于 batch2

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )


# ---- B. code 正确但 market 错误 ----
def test_market_mismatch_raises() -> None:
    insts = [_Inst("600519", "SH")]  # expected market 1
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519", market=0)]  # wrong market

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )


# ---- C. raw market=True（bool 不能当 int）----
def test_raw_market_bool_raises() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519", market=True)]

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )


# ---- D. raw row 非 Mapping ----
def test_raw_row_non_mapping_raises() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = ["not-a-mapping"]

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )


# ---- E. 输入重复 (market, symbol) → 网络 0 次 ----
def test_duplicate_input_identity_raises_before_network() -> None:
    insts = [_Inst("600519", "SH"), _Inst("600519", "SH")]
    adapter = _mk_adapter()

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )

    adapter.get_security_quotes.assert_not_called()


# ---- F. 输入未知 market → 网络 0 次（见 test_unknown_market_raises_before_network）----


def test_duplicate_quote_code_raises() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519"), _quote("600519")]

    with pytest.raises(PytdxEodSnapshotError):
        fetch_pytdx_eod_snapshot(
            adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
        )


def test_missing_symbol_recorded_not_faked() -> None:
    insts = [_Inst("600519", "SH"), _Inst("000001", "SZ")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519")]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    assert snap.missing_symbols == ("000001",)
    assert [r.symbol for r in snap.rows] == ["600519"]
    assert all(r.symbol != "000001" for r in snap.rows)


def test_provider_source_error_wrapped() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
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
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519")]

    fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    adapter.get_daily_bars.assert_not_called()


def test_no_get_xdxr_info() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519")]

    fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    adapter.get_xdxr_info.assert_not_called()


def test_no_db_redis_handle() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519")]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    assert not hasattr(snap, "session")
    assert not hasattr(snap, "redis")
    assert not hasattr(snap, "raw_servertime_by_symbol")


def test_malformed_ohlc_not_silently_fixed() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
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


def test_b1_raw_volume_unit_lots_and_source_time_preserved() -> None:
    """B1（G1B-2A）：raw quote vol 单位已证实为 **手（LOTS）**；字段仍叫 raw_volume，fetch 不换算。"""
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519", servertime="15:00:03")]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    assert snap.volume_unit == PYTDX_QUOTE_VOLUME_UNIT_LOTS == "LOTS"
    assert snap.rows[0].raw_volume == Decimal("1000.0")  # 字段名 raw_volume，未 ×100
    assert not hasattr(snap.rows[0], "volume")
    assert snap.rows[0].source_time == "15:00:03"
    assert snap.rows[0].raw_payload["vol"] == 1000.0


# ---- H. Decimal("Infinity") → 字段 None，raw_payload 保留证据 ----
def test_non_finite_parsed_none_raw_payload_kept() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
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
    adapter = _mk_adapter()
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
    adapter = _mk_adapter()
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
    adapter = _mk_adapter()
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
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519", vol=100.0)]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    res = compare_quote_to_daily_reference(snap.rows, [_reference_row("600519", "10000")])

    assert res["compared_count"] == 1
    assert res["volume_ratio_median"] == pytest.approx(0.01)


# ══════════════════════════════════════════════════════════════════════
# G1B-2A：verified snapshot + 唯一 canonical converter（B2 ~ B14）
# ══════════════════════════════════════════════════════════════════════
_D = date(2026, 9, 11)


def _daily_ok(dt: str = "2026-09-11 15:00:00", **kw: object) -> pd.DataFrame:
    """与默认 ``_quote()`` 一致的 daily 参考：vol 1000 手 → 100000 股。"""
    base: dict[str, object] = {
        "open": 9.9,
        "high": 10.1,
        "low": 9.8,
        "close": 10.0,
        "volume": 100000.0,
        "amount": 10000.0,
    }
    base.update(kw)
    return pd.DataFrame([{"datetime": pd.Timestamp(dt), **base}])


def _fetch(adapter: MagicMock, insts: list, captured_at: datetime | None = None):  # noqa: ANN202
    return fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=_D, captured_at=captured_at, batch_interval_seconds=0.0
    )


def _both_markets(adapter: MagicMock) -> None:
    adapter.get_security_quotes.return_value = [
        _quote("600519", 1, servertime="15:30:25"),
        _quote("000001", 0, servertime="15:30:26"),
    ]
    adapter.get_daily_bars.return_value = _daily_ok()


# ---- B2. SH + SZ 有效 → Verified；daily 恰好 2 次 ----
def test_b2_sh_sz_valid_produces_verified_with_two_daily_calls() -> None:
    insts = [_Inst("600519", "SH"), _Inst("000001", "SZ")]
    adapter = _mk_adapter()
    _both_markets(adapter)

    snap = _fetch(adapter, insts)
    verified = verify_pytdx_eod_snapshot(adapter, snap)

    assert isinstance(verified, VerifiedPytdxEodSnapshot)
    assert verified.verified_trade_date == _D
    assert verified.sentinel_symbols == ("600519", "000001")
    assert adapter.get_daily_bars.call_count == 2


# ---- B3 / B4. 缺 SH / 缺 SZ sentinel → fail ----
def test_b3_missing_sh_sentinel_fails() -> None:
    insts = [_Inst("000001", "SZ")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("000001", 0, servertime="15:30:26")]
    adapter.get_daily_bars.return_value = _daily_ok()
    snap = _fetch(adapter, insts)

    with pytest.raises(PytdxEodSnapshotError, match="sentinel 不足"):
        verify_pytdx_eod_snapshot(adapter, snap)


def test_b4_missing_sz_sentinel_fails() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519", 1, servertime="15:30:25")]
    adapter.get_daily_bars.return_value = _daily_ok()
    snap = _fetch(adapter, insts)

    with pytest.raises(PytdxEodSnapshotError, match="sentinel 不足"):
        verify_pytdx_eod_snapshot(adapter, snap)


# ---- B5. daily exact-date 不是 requested date → fail ----
def test_b5_daily_exact_date_missing_fails() -> None:
    insts = [_Inst("600519", "SH"), _Inst("000001", "SZ")]
    adapter = _mk_adapter()
    _both_markets(adapter)
    adapter.get_daily_bars.return_value = _daily_ok(dt="2026-09-10 15:00:00")
    snap = _fetch(adapter, insts)

    with pytest.raises(PytdxEodSnapshotError, match="date mismatch"):
        verify_pytdx_eod_snapshot(adapter, snap)


# ---- B6 / B7 / B8. OHLC / volume / amount 不符 → fail ----
def _verify_with_daily(adapter: MagicMock, daily: pd.DataFrame) -> None:  # noqa: ANN401
    insts = [_Inst("600519", "SH"), _Inst("000001", "SZ")]
    _both_markets(adapter)
    adapter.get_daily_bars.return_value = daily
    snap = _fetch(adapter, insts)
    verify_pytdx_eod_snapshot(adapter, snap)


def test_b6_ohlc_mismatch_fails() -> None:
    adapter = _mk_adapter()
    with pytest.raises(PytdxEodSnapshotError, match="close mismatch"):
        _verify_with_daily(adapter, _daily_ok(close=11.0))


def test_b7_volume_mismatch_fails() -> None:
    adapter = _mk_adapter()
    with pytest.raises(PytdxEodSnapshotError, match="volume mismatch"):
        _verify_with_daily(adapter, _daily_ok(volume=200000.0))


def test_b8_amount_mismatch_fails() -> None:
    adapter = _mk_adapter()
    with pytest.raises(PytdxEodSnapshotError, match="amount mismatch"):
        _verify_with_daily(adapter, _daily_ok(amount=20000.0))


# ---- B9 / B10. source_time 畸形 / < 15:00 → fail ----
@pytest.mark.parametrize("bad", ["not-a-time", "15", "15:30", ""])
def test_b9_source_time_malformed_fails(bad: str) -> None:
    insts = [_Inst("600519", "SH"), _Inst("000001", "SZ")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519", 1, servertime=bad)]
    adapter.get_daily_bars.return_value = _daily_ok()
    snap = _fetch(adapter, insts)

    with pytest.raises(PytdxEodSnapshotError):
        verify_pytdx_eod_snapshot(adapter, snap)


def test_b10_source_time_before_15_fails() -> None:
    insts = [_Inst("600519", "SH"), _Inst("000001", "SZ")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519", 1, servertime="14:59:59")]
    adapter.get_daily_bars.return_value = _daily_ok()
    snap = _fetch(adapter, insts)

    with pytest.raises(PytdxEodSnapshotError):
        verify_pytdx_eod_snapshot(adapter, snap)


# ---- B9b. converter 对 returned row 的 source_time 一律 fail-closed ----
@pytest.mark.parametrize("bad", ["not-a-time", "14:59:59", None, ""])
def test_b9b_converter_fails_closed_on_unusable_source_time(bad: object) -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519", 1, servertime=bad)]
    snap = _fetch(adapter, insts)
    verified = VerifiedPytdxEodSnapshot(
        raw_snapshot=snap, verified_trade_date=_D, sentinel_symbols=()
    )

    with pytest.raises(PytdxEodSnapshotError):
        to_canonical_eod_rows(verified)


# ---- B11. converter：123 手 → 12300 股 ----
def test_b11_converter_volume_lots_to_shares() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [
        _quote("600519", 1, vol=123.0, servertime="15:30:25")
    ]
    snap = _fetch(adapter, insts)
    verified = VerifiedPytdxEodSnapshot(
        raw_snapshot=snap, verified_trade_date=_D, sentinel_symbols=("600519",)
    )

    rows = to_canonical_eod_rows(verified)

    assert PYTDX_QUOTE_LOT_TO_SHARES == Decimal("100")
    assert rows[0].volume == Decimal("12300")
    assert rows[0].trade_date == _D
    assert rows[0].updated_at.hour == 15
    assert rows[0].updated_at.minute == 30


# ---- B12. captured_at 跨日不得泄漏进 canonical 日期 ----
def test_b12_captured_at_cross_day_does_not_leak_into_canonical_date() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [
        _quote("600519", 1, servertime="15:30:25")
    ]
    captured = datetime(2026, 9, 12, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    snap = _fetch(adapter, insts, captured_at=captured)
    assert snap.captured_at.date() == date(2026, 9, 12)

    verified = VerifiedPytdxEodSnapshot(
        raw_snapshot=snap, verified_trade_date=_D, sentinel_symbols=("600519",)
    )
    rows = to_canonical_eod_rows(verified)

    assert rows[0].trade_date == _D  # 2026-09-11，不是 captured_at 的 09-12


# ---- B13. converter 拒绝未验证的 raw snapshot ----
def test_b13_converter_rejects_raw_snapshot() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519", 1)]
    snap = _fetch(adapter, insts)

    with pytest.raises(TypeError):
        to_canonical_eod_rows(snap)  # type: ignore[arg-type]


# ---- B14. raw fetch 绝不调用 get_daily_bars ----
def test_b14_fetch_raw_never_calls_daily() -> None:
    insts = [_Inst("600519", "SH"), _Inst("000001", "SZ")]
    adapter = _mk_adapter()
    _both_markets(adapter)

    _fetch(adapter, insts)

    adapter.get_daily_bars.assert_not_called()


def test_compare_quote_to_daily_reference_amount_ratio() -> None:
    """A/B 诊断：amount ratio ≈ 1（元/元）。"""
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519", vol=100.0)]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    res = compare_quote_to_daily_reference(snap.rows, [_reference_row("600519", "10000")])

    assert res["compared_count"] == 1
    assert res["amount_ratio_median"] == pytest.approx(1.0)


def test_compare_quote_skips_unmatched_reference() -> None:
    insts = [_Inst("600519", "SH")]
    adapter = _mk_adapter()
    adapter.get_security_quotes.return_value = [_quote("600519", vol=100.0)]

    snap = fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=date(2026, 9, 11), batch_interval_seconds=0.0
    )

    res = compare_quote_to_daily_reference(snap.rows, [_reference_row("000001", "10000")])

    assert res["compared_count"] == 0


# ══════════════════════════════════════════════════════════════════════
# G1B-2A coherence：全市场 snapshot 禁止跨 connection generation 拼接（C1 ~ C4）
# ══════════════════════════════════════════════════════════════════════
_P_A7 = PytdxCallProvenance(server=("A", 7709), connection_generation=7)
_P_B8 = PytdxCallProvenance(server=("B", 7709), connection_generation=8)
_P_J7 = PytdxCallProvenance(server=("jstdx.gtjas.com", 7709), connection_generation=7)
_P_J8 = PytdxCallProvenance(server=("jstdx.gtjas.com", 7709), connection_generation=8)


def _fetch_161(adapter: MagicMock):  # noqa: ANN202
    insts = [_Inst(f"{i:06d}", "SH") for i in range(161)]
    return fetch_pytdx_eod_snapshot(
        adapter, insts, trade_date=_D, batch_interval_seconds=0.0
    )


def test_c1_same_connection_multi_batch_passes() -> None:
    """同一 connection 的 3 个 batch → PASS，provenance == A/gen7。"""
    adapter = _mk_prov_adapter([_P_A7, _P_A7, _P_A7])

    snap = _fetch_161(adapter)

    assert snap.returned_count == 161
    assert snap.provenance == _P_A7


def test_c2_server_switch_mid_snapshot_fails() -> None:
    """batch1 A/gen7 → batch2 B/gen8 → 整体作废，不返回半截 snapshot。"""
    adapter = _mk_prov_adapter([_P_A7, _P_B8, _P_B8])

    with pytest.raises(PytdxEodSnapshotError, match="connection changed"):
        _fetch_161(adapter)


def test_c3_same_hostname_reconnect_fails() -> None:
    """同 hostname 但 generation 变化（DNS 集群后台 IP 可能已变）→ FAIL。"""
    adapter = _mk_prov_adapter([_P_J7, _P_J8, _P_J8])

    with pytest.raises(PytdxEodSnapshotError, match="connection changed"):
        _fetch_161(adapter)


def test_c4_first_batch_internal_failover_then_stable_passes() -> None:
    """首批内部 failover（A fail → B success）→ provenance 从最终成功的 B 建立；后续稳定 → PASS。"""
    adapter = _mk_prov_adapter([_P_B8, _P_B8, _P_B8])

    snap = _fetch_161(adapter)

    assert snap.returned_count == 161
    assert snap.provenance == _P_B8
    assert snap.provenance.connection_generation == 8
