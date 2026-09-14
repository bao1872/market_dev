"""PART 4 — MDAS canonical completed-qfq-minute DTO / 时间 canonicalization / sequence proof 测试。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

from app.services.calendar_service import get_next_authoritative_trading_day_async
from app.services.market_data_aggregation_service import (
    CanonicalCompletedQfqMinute,
    CanonicalCompletedQfqMinutes,
    MarketDataAggregationService,
    _minute_index_to_aware_iso,
)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """仅用于 strict calendar helper 的只读桩（返回 (trade_date, is_trading_day, status)）。"""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(self._rows)


def _row(hhmm, session="2026-09-14"):
    return CanonicalCompletedQfqMinute(
        bar_time=f"{session}T{hhmm}:00+08:00",
        session_key=session,
        open=104.0,
        high=106.0,
        low=103.0,
        close=105.0,
    )


def _proof(bars, after=None):
    svc = MarketDataAggregationService()
    return asyncio.run(
        svc._prove_completed_minute_sequence(None, after_bar_time=after, bars=tuple(bars))
    )


class TestMinuteIndexCanonicalization:
    def test_naive_index_localized_to_shanghai(self):
        assert _minute_index_to_aware_iso("2026-09-14 09:31:00") == "2026-09-14T09:31:00+08:00"

    def test_aware_index_converted_to_shanghai(self):
        assert _minute_index_to_aware_iso("2026-09-14T01:31:00+00:00") == "2026-09-14T09:31:00+08:00"


class TestCanonicalCompletedQfqMinutesValidation:
    def test_valid(self):
        dto = CanonicalCompletedQfqMinutes(
            bars=(_row("09:31"),),
            qfq_proven=True,
            qfq_reason="",
            sequence_proven=True,
            sequence_reason="",
            source_bar_hash="s",
            adj_factor_hash="a",
            latest_completed_bar_time="2026-09-14T09:31:00+08:00",
        )
        assert len(dto.bars) == 1

    def test_bars_must_be_tuple(self):
        with pytest.raises(ValueError):
            CanonicalCompletedQfqMinutes(
                bars=[_row("09:31")],  # type: ignore[arg-type]
                qfq_proven=True, qfq_reason="", sequence_proven=True, sequence_reason="",
                source_bar_hash="s", adj_factor_hash="a", latest_completed_bar_time=None,
            )

    def test_qfq_proven_must_be_bool(self):
        with pytest.raises(ValueError):
            CanonicalCompletedQfqMinutes(
                bars=(), qfq_proven=1, qfq_reason="", sequence_proven=True, sequence_reason="",
                source_bar_hash="s", adj_factor_hash="a", latest_completed_bar_time=None,
            )

    def test_latest_completed_bar_time_naive_rejected(self):
        with pytest.raises(ValueError):
            CanonicalCompletedQfqMinutes(
                bars=(), qfq_proven=True, qfq_reason="", sequence_proven=True, sequence_reason="",
                source_bar_hash="s", adj_factor_hash="a",
                latest_completed_bar_time="2026-09-14T09:31:00",
            )


class TestSequenceProof:
    def test_contiguous_minutes_proven(self):
        proven, reason = _proof([_row("09:31"), _row("09:32"), _row("09:33")], after=None)
        assert proven is True, reason

    def test_missing_minute_fails(self):
        proven, _ = _proof([_row("09:31"), _row("09:32"), _row("09:34")], after=None)
        assert proven is False

    def test_lunch_jump_1130_to_1301_proven(self):
        # 非 bootstrap（带 cursor），唯一合法午休 jump
        proven, reason = _proof(
            [_row("11:30"), _row("13:01")], after=datetime(2026, 9, 14, 11, 29)
        )
        assert proven is True, reason

    def test_illegal_lunch_jump_fails(self):
        proven, _ = _proof(
            [_row("11:30"), _row("13:02")], after=datetime(2026, 9, 14, 11, 29)
        )
        assert proven is False

    def test_bootstrap_then_lunch_still_requires_open_start(self):
        proven, reason = _proof([_row("11:30"), _row("13:01")], after=None)
        assert proven is False
        assert "session open" in reason

    def test_bootstrap_must_start_at_0931(self):
        proven, reason = _proof([_row("09:32")], after=None)
        assert proven is False
        assert "session open" in reason

    def test_empty_bars_not_proven(self):
        proven, _ = _proof([], after=None)
        assert proven is False

    def test_out_of_order_fails(self):
        proven, _ = _proof([_row("09:32"), _row("09:31")], after=None)
        assert proven is False


class TestStrictCalendarHelper:
    def test_open_next_day_returned(self):
        session = _FakeSession([(date(2026, 9, 15), True, "OPEN")])
        assert asyncio.run(
            get_next_authoritative_trading_day_async(session, date(2026, 9, 14))
        ) == date(2026, 9, 15)

    def test_closed_then_open_skips_closed(self):
        session = _FakeSession([(date(2026, 9, 15), False, "CLOSED"), (date(2026, 9, 16), True, "OPEN")])
        assert asyncio.run(
            get_next_authoritative_trading_day_async(session, date(2026, 9, 14))
        ) == date(2026, 9, 16)

    def test_missing_row_unprovable(self):
        session = _FakeSession([(date(2026, 9, 16), True, "OPEN")])  # 09-15 缺记录
        assert asyncio.run(
            get_next_authoritative_trading_day_async(session, date(2026, 9, 14))
        ) is None

    def test_unknown_status_unprovable(self):
        session = _FakeSession([(date(2026, 9, 15), False, "UNKNOWN")])
        assert asyncio.run(
            get_next_authoritative_trading_day_async(session, date(2026, 9, 14))
        ) is None

    def test_open_with_is_trading_false_unprovable(self):
        session = _FakeSession([(date(2026, 9, 15), False, "OPEN")])
        assert asyncio.run(
            get_next_authoritative_trading_day_async(session, date(2026, 9, 14))
        ) is None


class TestCrossDaySequence:
    _CROSS = (
        CanonicalCompletedQfqMinute(
            bar_time="2026-09-14T15:00:00+08:00", session_key="2026-09-14",
            open=1.0, high=1.0, low=1.0, close=1.0,
        ),
        CanonicalCompletedQfqMinute(
            bar_time="2026-09-15T09:31:00+08:00", session_key="2026-09-15",
            open=1.0, high=1.0, low=1.0, close=1.0,
        ),
    )
    _CURSOR = datetime(2026, 9, 14, 14, 59)

    def test_cross_day_authoritative_proven(self):
        svc = MarketDataAggregationService()
        session = _FakeSession([(date(2026, 9, 15), True, "OPEN")])
        proven, reason = asyncio.run(
            svc._prove_completed_minute_sequence(
                session, after_bar_time=self._CURSOR, bars=self._CROSS
            )
        )
        assert proven is True, reason

    def test_cross_day_unprovable_fails(self):
        svc = MarketDataAggregationService()
        session = _FakeSession([])  # 无权威日历 → None
        proven, reason = asyncio.run(
            svc._prove_completed_minute_sequence(
                session, after_bar_time=self._CURSOR, bars=self._CROSS
            )
        )
        assert proven is False
        assert "unprovable" in reason

    def test_cross_day_wrong_target_day_fails(self):
        svc = MarketDataAggregationService()
        # 09-15 存在但 CLOSED，权威下一日是 09-16 ≠ bar 的 session_key(09-15)
        session = _FakeSession([(date(2026, 9, 15), False, "CLOSED"), (date(2026, 9, 16), True, "OPEN")])
        proven, reason = asyncio.run(
            svc._prove_completed_minute_sequence(
                session, after_bar_time=self._CURSOR, bars=self._CROSS
            )
        )
        assert proven is False
        assert "not the authoritative next trading day" in reason
