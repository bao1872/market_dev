"""Typed point-in-time member facts consumed by the Review metric engine."""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.domain.first_pyramid_semantics import (
    MomentumDirection,
    direction_display,
    direction_from_regime,
    momentum_direction_display,
)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def number(value: Any) -> float | None:
    """Public numeric coercion (finite float or None).  Shared mapper owner.

    Same semantics as the module-private ``_number``: ``None``/non-numeric/NaN/Inf
    all map to ``None``.  Used by the shared source-fact mappers so the DB loader
    path and the Dataset Replay Adapter path coerce bars identically.
    """
    return _number(value)


def _ratio(value: float | None, history: list[float], window: int) -> float | None:
    prior = history[-window - 1 : -1]
    if value is None or not prior:
        return None
    mean = sum(prior) / len(prior)
    return value / mean if abs(mean) > 1e-12 else None


def _percentile(value: float | None, history: list[float], window: int) -> float | None:
    prior = history[-window - 1 : -1]
    if value is None or not prior:
        return None
    return sum(item <= value for item in prior) / len(prior) * 100.0


# [CHANGE-20260808] Review rolling facts 共享纯 SSOT。
# LIVE ReviewMemberFact.build 与 Historical stock-major replay 消费同一公式，
# 确保 parity（窗口边界 19/20/21、119/120/121、199/200/201 行为一致）。
def compute_ratio(
    value: float | None,
    history: list[float],
    window: int,
) -> float | None:
    """current / prior window mean（分母不含 current）。与 _ratio 一致。"""
    return _ratio(value, history, window)


def compute_percentile(
    value: float | None,
    history: list[float],
    window: int,
) -> float | None:
    """prior window 中 <= current 的占比（分母不含 current）。与 _percentile 一致。"""
    return _percentile(value, history, window)


def compute_price_position_120d(
    close: float | None,
    recent_lows: list[float],
    recent_highs: list[float],
) -> float | None:
    """120 日价格位置：(close - low_120) / (high_120 - low_120)，含 current。"""
    if close is None:
        return None
    lows = [v for v in recent_lows if v is not None]
    highs = [v for v in recent_highs if v is not None]
    if not lows or not highs:
        return None
    low_value = min(lows)
    high_value = max(highs)
    if high_value - low_value <= 1e-12:
        return None
    return (close - low_value) / (high_value - low_value)


def previous_state_to_flat(state: dict[str, Any] | None) -> dict[str, Any]:
    """Map the history SSOT payload to the canonical flat keys used by semantics."""
    if not state:
        return {}
    # [CHANGE-20260808] LIVE parity：fp_trend_direction 用中文标签（与 first_pyramid_flatten
    # _direction_label 一致："上行"/"下行"/"震荡"），复用 direction_display/direction_from_regime。
    trend = direction_display(direction_from_regime(state.get("regime_value")))
    structure_alignment = state.get("structure_alignment")
    if structure_alignment is None:
        # AUDIT-FIX-01 (P1): derive 共振/背离 ONLY from two canonical, valid,
        # non-zero numeric biases.  A missing or non-numeric bias (None, NaN,
        # bool, non-numeric) is NOT a valid signal and must yield alignment=None
        # (unavailable) — NEVER a fake "共振" (e.g. None==None -> 共振).  The old
        # ``swing_b != 0`` compared None (None != 0 is True), so a non-empty state
        # with both biases absent silently produced "共振" (missing->aligned).
        swing_b = state.get("swing_bias")
        internal_b = state.get("internal_bias")

        def _valid_bias(value: Any) -> bool:
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value == value  # exclude NaN
                and value != 0
            )

        if _valid_bias(swing_b) and _valid_bias(internal_b):
            structure_alignment = (
                "共振" if swing_b == internal_b else "背离"
            )
    # [CHANGE-20260808] LIVE parity：fp_swing/internal_direction 输出中文标签
    # （与 first_pyramid_flatten._direction_label 一致："上行"/"下行"/"震荡"）。
    swing_dir = direction_display(direction_from_regime(state.get("swing_bias")))
    internal_dir = direction_display(direction_from_regime(state.get("internal_bias")))
    # [CHANGE-20260808] LIVE parity：momentum direction/change
    #   fp_momentum_direction = "扩张"/"收缩"/"平缓"（从 sqzmom_val 符号，momentum_direction_display）
    #   fp_momentum_change = numeric sqzmom delta（round(sqz_val - sqz_prev, 6)）
    sqz_val = state.get("sqzmom_val")
    if isinstance(sqz_val, (int, float)) and sqz_val == sqz_val:  # 非 NaN
        mom_dir = (
            MomentumDirection.EXPANDING if sqz_val > 0
            else MomentumDirection.CONTRACTING if sqz_val < 0
            else MomentumDirection.FLAT
        )
        momentum_direction = momentum_direction_display(mom_dir)
    else:
        momentum_direction = None
    sqz_delta = state.get("sqzmom_delta")
    momentum_change = round(sqz_delta, 6) if isinstance(sqz_delta, (int, float)) else None
    # [CHANGE-20260808] LIVE parity：latest event direction 用 "bullish"/"bearish"
    # （与 first_pyramid_flatten 的事件 direction 语义一致），daily_state 存 up/down 时映射。
    def _event_dir(val: Any) -> str | None:
        if val in ("up", "bullish"):
            return "bullish"
        if val in ("down", "bearish"):
            return "bearish"
        return None
    return {
        "fp_trend_direction": trend,
        "fp_swing_direction": swing_dir,
        "fp_internal_direction": internal_dir,
        "fp_structure_alignment": structure_alignment,
        "fp_momentum_direction": momentum_direction,
        "fp_momentum_change": momentum_change,
        "fp_volume_ratio20": state.get("volume_ratio_20"),
        "fp_volume_percentile20": state.get("volume_percentile_20"),
        "review_price_position": state.get("price_position_120d"),
        # [CHANGE-20260808] Review rolling facts（共享 SSOT 公式，LIVE/HISTORY parity）
        "review_volume_ratio20": state.get("review_volume_ratio20"),
        "review_amount_ratio20": state.get("review_amount_ratio20"),
        "review_volume_percentile20": state.get("review_volume_percentile20"),
        "review_amount_percentile200": state.get("review_amount_percentile200"),
        # [CHANGE-20260808] Review 扩展：latest 结构事件（freshness 相对已确认事件时间）
        "fp_latest_bos_direction": _event_dir(state.get("latest_bos_direction")),
        "fp_latest_bos_freshness": state.get("latest_bos_freshness"),
        "fp_latest_choch_direction": _event_dir(state.get("latest_choch_direction")),
        "fp_latest_choch_freshness": state.get("latest_choch_freshness"),
        "fp_latest_ob_direction": _event_dir(state.get("latest_ob_direction")),
        "fp_latest_ob_freshness": state.get("latest_ob_freshness"),
        # [CHANGE-20260808] DSA segment 量能改善（V 的 trend_segment_volume_improvement）
        "fp_segment_volume_ratio": state.get("current_vs_prev_volume_mean_ratio"),
        "fp_prev_segment_volume": state.get("prev_segment_volume_mean"),
    }


# Mapping of the history FirstPyramidHistoryDailyState.state_payload keys that
# carry the *continuous* member facts required by PRD §7.3-§7.6 (Trend /
# Structure / Momentum / Volume).  These are present in the history payload but
# were not surfaced through previous_state_to_flat, which only emits categorical
# states.  This passthrough is additive and does NOT alter previous_state_to_flat.
_CONTINUOUS_STATE_KEYS = (
    "regime_strength",
    "dsa_dir_bars",
    "dsa_vwap_dev_pct",
    "segment_id",
    "segment_direction",
    "segment_bars",
    "segment_change_pct",
    "segment_slope",
    "current_vs_prev_volume_mean_ratio",
    "current_vs_prev_amount_mean_ratio",
    "current_segment_volume_mean",
    "prev_segment_volume_mean",
    "structure_alignment",
    "active_internal_ob_count",
    "active_swing_ob_count",
    "volatility_phase",
    "momentum_direction",
    "momentum_change",
    "sqzmom_delta",
    "sqzmom_val",
    "volume_ratio_20",
    "volume_percentile_20",
    "volume_zscore_20",
    "available_bars",
)


def state_to_continuous(state: dict[str, Any] | None) -> dict[str, Any]:
    """Map the history SSOT payload to the continuous member facts for PRD §7.3-§7.6.

    Unlike :func:`previous_state_to_flat` (categorical states only), this surfaces
    the numeric Trend / Structure / Momentum / Volume continuous fields that the
    Scope L1 aggregation consumes.  Missing / non-numeric keys become ``None``
    (unavailable), never 0.  Additive: does not affect existing consumers.
    """
    if not state:
        return dict.fromkeys(_CONTINUOUS_STATE_KEYS)
    return {key: _number(state.get(key)) for key in _CONTINUOUS_STATE_KEYS}


@dataclass(frozen=True)
class DailyBarFact:
    trade_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    amount: float | None

    @classmethod
    def from_row(cls, row: Any) -> DailyBarFact:
        return cls(
            trade_date=row.trade_date,
            open=_number(row.open),
            high=_number(row.high),
            low=_number(row.low),
            close=_number(row.close),
            volume=_number(row.volume),
            amount=_number(row.amount),
        )


@dataclass(frozen=True)
class ReviewMemberFact:
    instrument_id: uuid.UUID
    symbol: str
    name: str
    snapshot_id: uuid.UUID | None
    trade_date: date
    first_pyramid: dict[str, Any]
    previous_first_pyramid: dict[str, Any]
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    prev_close: float | None
    return_1d: float | None
    price_position: float | None
    volume: float | None
    amount: float | None
    volume_ratio20: float | None
    amount_ratio20: float | None
    volume_percentile20: float | None
    amount_percentile200: float | None
    weight: float
    weight_mode: str

    @classmethod
    def build(
        cls,
        *,
        instrument_id: uuid.UUID,
        symbol: str,
        name: str,
        snapshot_id: uuid.UUID | None,
        trade_date: date,
        first_pyramid: dict[str, Any],
        bars: Iterable[DailyBarFact],
        previous_state: dict[str, Any] | None,
        weight: float = 1.0,
        weight_mode: str = "equal_weight",
    ) -> ReviewMemberFact:
        ordered = sorted((bar for bar in bars if bar.trade_date <= trade_date), key=lambda b: b.trade_date)
        current = ordered[-1] if ordered and ordered[-1].trade_date == trade_date else None
        previous = ordered[-2] if current is not None and len(ordered) >= 2 else None
        close = current.close if current is not None else None
        prev_close = previous.close if previous is not None else None
        return_1d = (
            (close - prev_close) / prev_close * 100.0
            if close is not None and prev_close is not None and abs(prev_close) > 1e-12
            else None
        )
        recent = ordered[-120:] if current is not None else []
        lows = [bar.low for bar in recent if bar.low is not None]
        highs = [bar.high for bar in recent if bar.high is not None]
        # [CHANGE-20260808] 复用共享纯 SSOT，确保与 Historical replay parity
        price_position = compute_price_position_120d(close, lows, highs)
        volumes = [bar.volume for bar in ordered if bar.volume is not None]
        amounts = [bar.amount for bar in ordered if bar.amount is not None]
        volume = current.volume if current is not None else None
        amount = current.amount if current is not None else None
        return cls(
            instrument_id=instrument_id,
            symbol=symbol,
            name=name,
            snapshot_id=snapshot_id,
            trade_date=trade_date,
            first_pyramid=dict(first_pyramid),
            previous_first_pyramid=previous_state_to_flat(previous_state),
            open=current.open if current is not None else None,
            high=current.high if current is not None else None,
            low=current.low if current is not None else None,
            close=close,
            prev_close=prev_close,
            return_1d=return_1d,
            price_position=price_position,
            volume=volume,
            amount=amount,
            volume_ratio20=compute_ratio(volume, volumes, 20),
            amount_ratio20=compute_ratio(amount, amounts, 20),
            volume_percentile20=compute_percentile(volume, volumes, 20),
            amount_percentile200=compute_percentile(amount, amounts, 200),
            weight=weight,
            weight_mode=weight_mode,
        )

    def to_metric_input(self) -> dict[str, Any]:
        result = dict(self.first_pyramid)
        result.update(
            {
                "_instrument_id": str(self.instrument_id),
                "_instrument_symbol": self.symbol,
                "_instrument_name": self.name,
                "_snapshot_id": self.snapshot_id,
                "review_trade_date": self.trade_date.isoformat(),
                "review_open": self.open,
                "review_high": self.high,
                "review_low": self.low,
                "review_close": self.close,
                "review_prev_close": self.prev_close,
                "review_return_1d": self.return_1d,
                "review_price_position": self.price_position,
                "review_volume": self.volume,
                "review_amount": self.amount,
                "review_volume_ratio20": self.volume_ratio20,
                "review_amount_ratio20": self.amount_ratio20,
                "review_volume_percentile20": self.volume_percentile20,
                "review_amount_percentile200": self.amount_percentile200,
                "review_previous_first_pyramid": self.previous_first_pyramid,
                "review_weight": self.weight,
                "review_weight_mode": self.weight_mode,
            }
        )
        return result
