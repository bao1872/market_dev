"""PART 4 — MDAS canonical completed-qfq-minute DTO / 时间 canonicalization / sequence proof 测试。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from uuid import uuid4

import pandas as pd
import pytest

from app.core.time import SHANGHAI_TZ
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
        # 非 bootstrap（带 aware cursor），唯一合法午休 jump
        proven, reason = _proof(
            [_row("11:30"), _row("13:01")],
            after=datetime(2026, 9, 14, 11, 29, tzinfo=SHANGHAI_TZ),
        )
        assert proven is True, reason

    def test_illegal_lunch_jump_fails(self):
        proven, _ = _proof(
            [_row("11:30"), _row("13:02")],
            after=datetime(2026, 9, 14, 11, 29, tzinfo=SHANGHAI_TZ),
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
    _CURSOR = datetime(2026, 9, 14, 14, 59, tzinfo=SHANGHAI_TZ)

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


# ---------------------------------------------------------------------------
# Blocker 1 — cursor → first bar 连续性证明
# ---------------------------------------------------------------------------


class TestCursorToFirstBar:
    def test_contiguous_after_cursor_proven(self):
        proven, reason = _proof(
            [_row("10:02"), _row("10:03")],
            after=datetime(2026, 9, 14, 10, 1, tzinfo=SHANGHAI_TZ),
        )
        assert proven is True, reason

    def test_cursor_gap_fails(self):
        proven, reason = _proof(
            [_row("10:03"), _row("10:04")],
            after=datetime(2026, 9, 14, 10, 1, tzinfo=SHANGHAI_TZ),
        )
        assert proven is False
        assert "cursor-to-first-bar gap" in reason

    def test_cursor_1130_then_1301_proven(self):
        proven, reason = _proof(
            [_row("13:01")], after=datetime(2026, 9, 14, 11, 30, tzinfo=SHANGHAI_TZ)
        )
        assert proven is True, reason

    def test_cursor_1130_then_1302_fails(self):
        proven, reason = _proof(
            [_row("13:02")], after=datetime(2026, 9, 14, 11, 30, tzinfo=SHANGHAI_TZ)
        )
        assert proven is False
        assert "cursor-to-first-bar gap" in reason

    def test_cursor_1500_then_next_open_proven(self):
        svc = MarketDataAggregationService()
        session = _FakeSession([(date(2026, 9, 15), True, "OPEN")])
        bars = (
            CanonicalCompletedQfqMinute(
                bar_time="2026-09-15T09:31:00+08:00", session_key="2026-09-15",
                open=1.0, high=1.0, low=1.0, close=1.0,
            ),
        )
        proven, reason = asyncio.run(
            svc._prove_completed_minute_sequence(
                session,
                after_bar_time=datetime(2026, 9, 14, 15, 0, tzinfo=SHANGHAI_TZ),
                bars=bars,
            )
        )
        assert proven is True, reason

    def test_cursor_1459_then_next_open_fails(self):
        svc = MarketDataAggregationService()
        session = _FakeSession([(date(2026, 9, 15), True, "OPEN")])
        bars = (
            CanonicalCompletedQfqMinute(
                bar_time="2026-09-15T09:31:00+08:00", session_key="2026-09-15",
                open=1.0, high=1.0, low=1.0, close=1.0,
            ),
        )
        proven, reason = asyncio.run(
            svc._prove_completed_minute_sequence(
                session,
                after_bar_time=datetime(2026, 9, 14, 14, 59, tzinfo=SHANGHAI_TZ),
                bars=bars,
            )
        )
        assert proven is False
        assert "cursor-to-first-bar gap" in reason


# ---------------------------------------------------------------------------
# Blocker 2 — owner-level getter 集成（naive 上海 index + aware cursor）
# ---------------------------------------------------------------------------


class _Schedule:
    scanned_as_of = date(2026, 9, 14)
    next_event_date = None


class TestOwnerLevelGetter:
    class _Result:
        def __init__(self, bars):
            self.bars = bars
            self.degraded = False
            self.degraded_reason = None
            self.adj_factor_hash = "adj-hash"
            self.source_bar_hash = "src-hash"

    def _svc(self, monkeypatch, index):
        import app.services.market_data_aggregation_service as mod

        svc = MarketDataAggregationService()
        frame = pd.DataFrame(
            {
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "adj_factor": 1.0,
                "volume": 1.0,
                "amount": 1.0,
            },
            index=pd.DatetimeIndex(index),
        )
        result = self._Result(frame)

        async def _fake_get_bars(**_kwargs):
            return result

        monkeypatch.setattr(svc, "get_bars", _fake_get_bars)
        # 固定 now：这些 fixture 都是"已完成"的历史分钟，必须让 completion cutoff 晚于它们
        monkeypatch.setattr(
            mod, "now_shanghai", lambda: datetime(2026, 9, 14, 15, 30, tzinfo=SHANGHAI_TZ)
        )
        monkeypatch.setattr(
            mod.AdjustmentFactorService,
            "get_corporate_action_schedule_state",
            lambda self, instrument_id: _Schedule(),
        )
        return svc

    def test_naive_index_and_aware_cursor_no_crash(self, monkeypatch):
        svc = self._svc(monkeypatch, ["2026-09-14 10:01:00", "2026-09-14 10:02:00"])
        out = asyncio.run(
            svc.get_completed_qfq_minutes_for_monitor(
                None,
                uuid4(),
                after_bar_time=datetime(2026, 9, 14, 10, 1, tzinfo=SHANGHAI_TZ),
                target_epoch="2026-01-02T00:00:00",
            )
        )
        assert [row.bar_time for row in out.bars] == ["2026-09-14T10:02:00+08:00"]
        assert out.bars[0].session_key == "2026-09-14"
        assert out.sequence_proven is True
        assert out.qfq_proven is True
        assert out.latest_completed_bar_time == "2026-09-14T10:02:00+08:00"

    def test_gap_after_cursor_not_proven(self, monkeypatch):
        svc = self._svc(monkeypatch, ["2026-09-14 10:01:00", "2026-09-14 10:03:00"])
        out = asyncio.run(
            svc.get_completed_qfq_minutes_for_monitor(
                None,
                uuid4(),
                after_bar_time=datetime(2026, 9, 14, 10, 1, tzinfo=SHANGHAI_TZ),
                target_epoch="2026-01-02T00:00:00",
            )
        )
        assert [row.bar_time for row in out.bars] == ["2026-09-14T10:03:00+08:00"]
        assert out.sequence_proven is False
        assert "cursor-to-first-bar gap" in out.sequence_reason

    def test_naive_cursor_rejected(self, monkeypatch):
        svc = self._svc(monkeypatch, ["2026-09-14 10:02:00"])
        with pytest.raises(ValueError):
            asyncio.run(
                svc.get_completed_qfq_minutes_for_monitor(
                    None,
                    uuid4(),
                    after_bar_time=datetime(2026, 9, 14, 10, 1),  # naive → 拒绝
                    target_epoch="2026-01-02T00:00:00",
                )
            )


# ---------------------------------------------------------------------------
# PART 4 — completed-bar authority（forming bar 必须按 timestamp 排除）
# ---------------------------------------------------------------------------


class TestCompletedBarAuthority:
    @staticmethod
    def _svc(monkeypatch, *, now, index):
        import app.services.market_data_aggregation_service as mod

        svc = MarketDataAggregationService()
        frame = pd.DataFrame(
            {
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "adj_factor": 1.0,
                "volume": 1.0,
                "amount": 1.0,
            },
            index=pd.DatetimeIndex(index),
        )

        class _R:
            bars = frame
            degraded = False
            degraded_reason = None
            adj_factor_hash = "adj-hash"
            source_bar_hash = "upstream-hash"

        async def _fake_get_bars(**_kwargs):
            return _R()

        monkeypatch.setattr(svc, "get_bars", _fake_get_bars)
        monkeypatch.setattr(mod, "now_shanghai", lambda: now)
        monkeypatch.setattr(
            mod.AdjustmentFactorService,
            "get_corporate_action_schedule_state",
            lambda self, instrument_id: _Schedule(),
        )
        return svc

    @staticmethod
    def _run(svc, cursor):
        return asyncio.run(
            svc.get_completed_qfq_minutes_for_monitor(
                None, uuid4(), after_bar_time=cursor, target_epoch="2026-01-02T00:00:00"
            )
        )

    def test_case_a_forming_bar_excluded(self, monkeypatch):
        now = datetime(2026, 9, 14, 10, 2, 37, tzinfo=SHANGHAI_TZ)
        svc = self._svc(
            monkeypatch,
            now=now,
            index=["2026-09-14 10:01:00", "2026-09-14 10:02:00", "2026-09-14 10:03:00"],
        )
        out = self._run(svc, datetime(2026, 9, 14, 10, 1, tzinfo=SHANGHAI_TZ))
        assert [r.bar_time for r in out.bars] == ["2026-09-14T10:02:00+08:00"]
        assert out.sequence_proven is True

    def test_case_b_last_completed_bar_not_dropped(self, monkeypatch):
        now = datetime(2026, 9, 14, 10, 2, 37, tzinfo=SHANGHAI_TZ)
        svc = self._svc(
            monkeypatch, now=now, index=["2026-09-14 10:01:00", "2026-09-14 10:02:00"]
        )
        out = self._run(svc, datetime(2026, 9, 14, 10, 1, tzinfo=SHANGHAI_TZ))
        assert [r.bar_time for r in out.bars] == ["2026-09-14T10:02:00+08:00"]

    def test_case_c_no_new_bar_is_noop(self, monkeypatch):
        now = datetime(2026, 9, 14, 10, 2, 37, tzinfo=SHANGHAI_TZ)
        svc = self._svc(
            monkeypatch,
            now=now,
            index=["2026-09-14 10:01:00", "2026-09-14 10:02:00", "2026-09-14 10:03:00"],
        )
        out = self._run(svc, datetime(2026, 9, 14, 10, 2, tzinfo=SHANGHAI_TZ))
        assert out.bars == ()
        assert out.latest_completed_bar_time is None
        assert out.qfq_proven is False
        assert out.sequence_proven is False
        assert out.qfq_reason == "no new completed bars"
        assert out.sequence_reason == "no new completed bars"

    def test_case_d_cursor_gap_fail_closed(self, monkeypatch):
        now = datetime(2026, 9, 14, 10, 3, 37, tzinfo=SHANGHAI_TZ)
        svc = self._svc(
            monkeypatch, now=now, index=["2026-09-14 10:01:00", "2026-09-14 10:03:00"]
        )
        out = self._run(svc, datetime(2026, 9, 14, 10, 1, tzinfo=SHANGHAI_TZ))
        assert [r.bar_time for r in out.bars] == ["2026-09-14T10:03:00+08:00"]
        assert out.sequence_proven is False

    def test_case_e_1301_forming_excluded(self, monkeypatch):
        now = datetime(2026, 9, 14, 13, 0, 20, tzinfo=SHANGHAI_TZ)
        svc = self._svc(monkeypatch, now=now, index=["2026-09-14 13:01:00"])
        out = self._run(svc, None)
        assert out.bars == ()

    def test_case_f_1301_completed_included(self, monkeypatch):
        now = datetime(2026, 9, 14, 13, 1, 20, tzinfo=SHANGHAI_TZ)
        svc = self._svc(monkeypatch, now=now, index=["2026-09-14 13:01:00"])
        out = self._run(svc, datetime(2026, 9, 14, 11, 30, tzinfo=SHANGHAI_TZ))
        assert [r.bar_time for r in out.bars] == ["2026-09-14T13:01:00+08:00"]
        assert out.sequence_proven is True

    def test_case_g_invalid_session_label_rejected(self):
        with pytest.raises(ValueError):
            CanonicalCompletedQfqMinute(
                bar_time="2026-09-14T12:01:00+08:00",
                session_key="2026-09-14",
                open=1.0, high=1.0, low=1.0, close=1.0,
            )

    def test_case_h_session_key_mismatch_rejected(self):
        with pytest.raises(ValueError):
            CanonicalCompletedQfqMinute(
                bar_time="2026-09-14T10:01:00+08:00",
                session_key="2026-09-15",
                open=1.0, high=1.0, low=1.0, close=1.0,
            )
