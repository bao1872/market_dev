"""factor_source_compatibility 只读诊断单元测试（纯单元，无网络）。

验证：
- 同一 symbol 多个除权事件必须按 (symbol, event_date) 分别命中 preclose，不互相覆盖；
- provider 未抓到某事件 preclose 时不得计入 fetch_success；
- 多事件 / 单 provider 成功 → sample_requested / fetch_success / comparable 计数正确。
"""

from datetime import date
from decimal import Decimal

from app.services.factor_source_compatibility import (
    FactorEventSample,
    compare_factor_events,
)


def _sample(symbol: str, market: str, trade_date: date) -> FactorEventSample:
    # stored event factor = prev_factor / event_day_factor = 2 / 1 = 2
    return FactorEventSample(
        instrument_id="x",
        trade_date=trade_date,
        close=None,
        adj_factor=Decimal("1"),
        prev_close=Decimal("10"),
        prev_factor=Decimal("2"),
        symbol=symbol,
        market=market,
    )


def test_multiple_events_same_symbol_use_distinct_preclose() -> None:
    samples = [
        _sample("600519", "SH", date(2025, 6, 1)),
        _sample("600519", "SH", date(2026, 6, 1)),
    ]
    preclose_map = {
        ("600519", date(2025, 6, 1)): Decimal("20"),  # provider 20/10 = 2 == stored
        ("600519", date(2026, 6, 1)): Decimal("40"),  # provider 40/10 = 4 != stored(2)
    }
    stats = compare_factor_events(samples, preclose_map)
    assert stats.sample_requested == 2
    assert stats.fetch_success == 2  # 两个事件都各有 provider 数据
    assert stats.comparable == 2
    # 第一个精确相等；第二个 provider=4 与 stored=2 差 2，不 within_1e6
    assert stats.within_1e6 == 1


def test_provider_missing_not_counted_as_fetch_success() -> None:
    samples = [_sample("600519", "SH", date(2025, 6, 1))]
    # 该事件 key 不存在于 preclose_map → 不计入 fetch_success
    stats = compare_factor_events(samples, {})
    assert stats.sample_requested == 1
    assert stats.fetch_success == 0
    assert stats.comparable == 0


def test_two_events_one_provider_success() -> None:
    samples = [
        _sample("600519", "SH", date(2025, 6, 1)),
        _sample("000001", "SZ", date(2026, 6, 1)),
    ]
    preclose_map = {("600519", date(2025, 6, 1)): Decimal("20")}
    stats = compare_factor_events(samples, preclose_map)
    assert stats.sample_requested == 2
    assert stats.fetch_success == 1
    assert stats.comparable == 1
