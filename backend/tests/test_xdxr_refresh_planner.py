"""G1B-3B1: XDXR 安全刷新 planner 纯单元测试（无 IO / 无 DB / 无 Redis）。

覆盖 previous_close signal、stable rotation bucket、四类刷新原因。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.adjustment_factor_service import CorporateActionScheduleState
from app.services.xdxr_refresh_planner import (
    PreviousCloseSignal,
    classify_previous_close_signal,
    plan_xdxr_refresh,
    rotation_bucket,
)

# =============================================================================
# 16-20: previous_close signal
# =============================================================================


def test_previous_close_equal_no_signal() -> None:
    assert (
        classify_previous_close_signal(
            snapshot_previous_close=Decimal("10.00"),
            prior_raw_close=Decimal("10.00"),
        )
        is PreviousCloseSignal.NO_ACTION_SIGNAL
    )


def test_previous_close_mismatch_candidate() -> None:
    assert (
        classify_previous_close_signal(
            snapshot_previous_close=Decimal("9.98"),
            prior_raw_close=Decimal("10.00"),
        )
        is PreviousCloseSignal.CORPORATE_ACTION_CANDIDATE
    )


def test_previous_close_any_none_unknown() -> None:
    assert (
        classify_previous_close_signal(
            snapshot_previous_close=None, prior_raw_close=Decimal("10.00"),
        )
        is PreviousCloseSignal.UNKNOWN
    )
    assert (
        classify_previous_close_signal(
            snapshot_previous_close=Decimal("10.00"), prior_raw_close=None,
        )
        is PreviousCloseSignal.UNKNOWN
    )


def test_previous_close_non_positive_unknown() -> None:
    assert (
        classify_previous_close_signal(
            snapshot_previous_close=Decimal("0"), prior_raw_close=Decimal("10.00"),
        )
        is PreviousCloseSignal.UNKNOWN
    )
    assert (
        classify_previous_close_signal(
            snapshot_previous_close=Decimal("-1.0"), prior_raw_close=Decimal("10.00"),
        )
        is PreviousCloseSignal.UNKNOWN
    )


def test_previous_close_nan_inf_unknown() -> None:
    assert (
        classify_previous_close_signal(
            snapshot_previous_close=Decimal("NaN"), prior_raw_close=Decimal("10.00"),
        )
        is PreviousCloseSignal.UNKNOWN
    )
    assert (
        classify_previous_close_signal(
            snapshot_previous_close=Decimal("Infinity"), prior_raw_close=Decimal("10.00"),
        )
        is PreviousCloseSignal.UNKNOWN
    )


# =============================================================================
# 21-30: planner + rotation
# =============================================================================


def test_schedule_unknown_forces_refresh() -> None:
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=0,
        schedule=None,
        schedule_age_trade_days=0,
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is True
    assert decision.reasons == ("schedule_unknown",)


def test_known_event_due_equal_trade_date() -> None:
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 8, 1), next_event_date=date(2026, 9, 1),
    )
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=rotation_bucket("600000", 3) + 1,  # 非 rotation 命中
        schedule=schedule,
        schedule_age_trade_days=1,  # 已证明 age，避免 schedule_age_unknown
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is True
    assert "known_event_due" in decision.reasons
    assert "schedule_unknown" not in decision.reasons


def test_known_event_past_still_refresh() -> None:
    # 事件早于 trade_date（missed / delayed）→ 不能静默跳过，仍刷新
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 8, 1), next_event_date=date(2026, 8, 20),
    )
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=rotation_bucket("600000", 3) + 1,
        schedule=schedule,
        schedule_age_trade_days=1,
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is True
    assert "known_event_due" in decision.reasons


def test_previous_close_mismatch_refresh() -> None:
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 9, 1), next_event_date=date(2026, 9, 10),
    )
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=rotation_bucket("600000", 3) + 1,  # 非 rotation 命中
        schedule=schedule,
        schedule_age_trade_days=0,  # 同日扫描，age 直接为 0
        previous_close_signal=PreviousCloseSignal.CORPORATE_ACTION_CANDIDATE,
        rotation_size=3,
    )
    assert decision.refresh is True
    assert "previous_close_mismatch" in decision.reasons
    assert "schedule_unknown" not in decision.reasons


def test_previous_close_unknown_refresh() -> None:
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 9, 1), next_event_date=date(2026, 9, 10),
    )
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=rotation_bucket("600000", 3) + 1,
        schedule=schedule,
        schedule_age_trade_days=0,
        previous_close_signal=PreviousCloseSignal.UNKNOWN,
        rotation_size=3,
    )
    assert decision.refresh is True
    assert "previous_close_unknown" in decision.reasons


def test_rotation_only_refresh() -> None:
    # schedule fresh + 无动作信号，但 rotation bucket 命中 → 刷新
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 9, 1), next_event_date=date(2026, 9, 10),
    )
    bucket = rotation_bucket("600000", 3)
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=bucket,  # == bucket → rotation 命中
        schedule=schedule,
        schedule_age_trade_days=0,
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is True
    assert "rotation_refresh" in decision.reasons


def test_fresh_schedule_no_signal_no_rotation_no_refresh() -> None:
    # schedule fresh + 无动作信号 + 非 rotation bucket → 不刷新
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 9, 1), next_event_date=date(2026, 9, 10),
    )
    bucket = rotation_bucket("600000", 3)
    ordinal = (bucket + 1) % 3  # 确保 != bucket
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=ordinal,
        schedule=schedule,
        schedule_age_trade_days=0,
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is False
    assert decision.reasons == ()


# =============================================================================
# E-N: schedule 交易日 age / stale（系统停跑恢复语义）
# =============================================================================


def _non_rotation_ordinal(symbol: str, rotation_size: int = 3) -> int:
    """返回一个必然不命中 rotation 的 ordinal。"""
    return (rotation_bucket(symbol, rotation_size) + 1) % rotation_size


def test_same_day_schedule_age_none_no_age_unknown() -> None:
    # E: 同日扫描 + age=None → 不产生 schedule_age_unknown（age 可直接证明为 0）
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 9, 1), next_event_date=date(2026, 9, 10),
    )
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=_non_rotation_ordinal("600000"),
        schedule=schedule,
        schedule_age_trade_days=None,
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is False
    assert "schedule_age_unknown" not in decision.reasons
    assert "schedule_stale" not in decision.reasons


def test_older_schedule_age_none_forces_refresh() -> None:
    # F: 旧 schedule + age=None → schedule_age_unknown → 强制刷新
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 8, 1), next_event_date=None,
    )
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=_non_rotation_ordinal("600000"),
        schedule=schedule,
        schedule_age_trade_days=None,
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is True
    assert "schedule_age_unknown" in decision.reasons


def test_older_schedule_age_negative_forces_refresh() -> None:
    # G: age=-1 → schedule_age_unknown → 强制刷新
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 8, 1), next_event_date=None,
    )
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=_non_rotation_ordinal("600000"),
        schedule=schedule,
        schedule_age_trade_days=-1,
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is True
    assert "schedule_age_unknown" in decision.reasons


def test_older_schedule_age_bool_forces_refresh() -> None:
    # H: age=True（bool 不得当 int）→ schedule_age_unknown → 强制刷新
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 8, 1), next_event_date=None,
    )
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=_non_rotation_ordinal("600000"),
        schedule=schedule,
        schedule_age_trade_days=True,  # type: ignore[arg-type]
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is True
    assert "schedule_age_unknown" in decision.reasons


def test_age_1_no_refresh() -> None:
    # I: age=1 + 无事件 + 无信号 + 非 rotation → refresh=False
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 8, 1), next_event_date=None,
    )
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=_non_rotation_ordinal("600000"),
        schedule=schedule,
        schedule_age_trade_days=1,
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is False
    assert decision.reasons == ()


def test_age_2_no_refresh() -> None:
    # J: age=2 → refresh=False
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 8, 1), next_event_date=None,
    )
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=_non_rotation_ordinal("600000"),
        schedule=schedule,
        schedule_age_trade_days=2,
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is False
    assert decision.reasons == ()


def test_age_3_stale_refresh() -> None:
    # K: age=3 >= rotation_size → schedule_stale → 刷新
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 8, 1), next_event_date=None,
    )
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=_non_rotation_ordinal("600000"),
        schedule=schedule,
        schedule_age_trade_days=3,
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is True
    assert "schedule_stale" in decision.reasons


def test_age_10_stale_refresh() -> None:
    # L: age=10 → schedule_stale → 刷新
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 8, 1), next_event_date=None,
    )
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=_non_rotation_ordinal("600000"),
        schedule=schedule,
        schedule_age_trade_days=10,
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is True
    assert "schedule_stale" in decision.reasons


def test_stale_and_rotation_both_reasons() -> None:
    # M: stale + rotation 同时命中 → 两个 reason 共存
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 8, 1), next_event_date=None,
    )
    bucket = rotation_bucket("600000", 3)
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=bucket,  # rotation 命中
        schedule=schedule,
        schedule_age_trade_days=3,  # stale
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is True
    assert "schedule_stale" in decision.reasons
    assert "rotation_refresh" in decision.reasons


def test_known_event_due_and_stale_both_reasons() -> None:
    # N: known event due + stale → 两个 reason 均保留
    schedule = CorporateActionScheduleState(
        scanned_as_of=date(2026, 8, 1), next_event_date=date(2026, 8, 20),
    )
    decision = plan_xdxr_refresh(
        symbol="600000",
        trade_date=date(2026, 9, 1),
        trade_day_ordinal=_non_rotation_ordinal("600000"),
        schedule=schedule,
        schedule_age_trade_days=3,  # stale
        previous_close_signal=PreviousCloseSignal.NO_ACTION_SIGNAL,
        rotation_size=3,
    )
    assert decision.refresh is True
    assert "schedule_stale" in decision.reasons
    assert "known_event_due" in decision.reasons


def test_rotation_bucket_deterministic() -> None:
    a = rotation_bucket("600000", 3)
    b = rotation_bucket("600000", 3)
    assert a == b
    assert 0 <= a < 3


def test_rotation_covers_each_symbol_once_per_size() -> None:
    # rotation_size=3 时，连续 ordinal 0/1/2 中每个 symbol 恰好命中一次
    symbol = "600000"
    bucket = rotation_bucket(symbol, 3)
    hits = sum(
        1 for ordinal in range(3)
        if rotation_bucket(symbol, 3) == ordinal % 3
    )
    assert hits == 1
    assert bucket in (0, 1, 2)


def test_rotation_size_invalid_raises() -> None:
    with pytest.raises(ValueError):
        rotation_bucket("600000", 0)
    with pytest.raises(ValueError):
        rotation_bucket("600000", -1)
