"""Typed point-in-time member facts consumed by the Review metric engine."""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


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


def previous_state_to_flat(state: dict[str, Any] | None) -> dict[str, Any]:
    """Map the history SSOT payload to the canonical flat keys used by semantics."""
    if not state:
        return {}
    regime = _number(state.get("regime_value"))
    if regime is None:
        trend = None
    elif regime > 0:
        trend = "up"
    elif regime < 0:
        trend = "down"
    else:
        trend = "sideways"
    structure_alignment = state.get("structure_alignment")
    if structure_alignment is None:
        swing_b = state.get("swing_bias")
        internal_b = state.get("internal_bias")
        if swing_b != 0 and internal_b != 0:
            structure_alignment = (
                "共振" if swing_b == internal_b else "背离"
            )
    return {
        "fp_trend_direction": trend,
        "fp_swing_direction": state.get("swing_bias"),
        "fp_internal_direction": state.get("internal_bias"),
        "fp_structure_alignment": structure_alignment,
        "fp_momentum_direction": state.get("momentum_direction"),
        "fp_momentum_change": state.get("momentum_change"),
        "fp_volume_ratio20": state.get("volume_ratio_20"),
        "fp_volume_percentile20": state.get("volume_percentile_20"),
        "review_price_position": state.get("price_position_120d"),
        # [CHANGE-20260808] Review 扩展：latest 结构事件（freshness 相对已确认事件时间）
        "fp_latest_bos_direction": state.get("latest_bos_direction"),
        "fp_latest_bos_freshness": state.get("latest_bos_freshness"),
        "fp_latest_choch_direction": state.get("latest_choch_direction"),
        "fp_latest_choch_freshness": state.get("latest_choch_freshness"),
        "fp_latest_ob_direction": state.get("latest_ob_direction"),
        "fp_latest_ob_freshness": state.get("latest_ob_freshness"),
        # [CHANGE-20260808] DSA segment 量能改善（V 的 trend_segment_volume_improvement）
        "fp_segment_volume_ratio": state.get("current_vs_prev_volume_mean_ratio"),
        "fp_prev_segment_volume": state.get("prev_segment_volume_mean"),
    }


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
        price_position = None
        if close is not None and lows and highs:
            low_value = min(lows)
            high_value = max(highs)
            if high_value - low_value > 1e-12:
                price_position = (close - low_value) / (high_value - low_value)
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
            volume_ratio20=_ratio(volume, volumes, 20),
            amount_ratio20=_ratio(amount, amounts, 20),
            volume_percentile20=_percentile(volume, volumes, 20),
            amount_percentile200=_percentile(amount, amounts, 200),
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
