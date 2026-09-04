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


# Keys whose values are member / instrument UUID references inside a persisted
# Composition payload.  ``leadership`` stores them as string id arrays; every
# ``member_attribution`` sub-group stores them as a ``member_id`` string field.
_MEMBER_UUID_KEYS: frozenset[str] = frozenset(
    {
        "member_id",
        "current_leader_ids",
        "previous_leader_ids",
        "entrant_ids",
        "exit_ids",
    }
)


def collect_composition_member_ids(composition: dict[str, Any] | None) -> set[str]:
    """Collect every member / instrument UUID referenced by a Composition payload.

    Walks ``leadership`` id arrays (current / previous / entrant / exit) and
    every ``member_id`` inside ``member_attribution`` (breadth / direction /
    capital_tilt / concentration / leadership sub-groups).  Purely additive
    display metadata (REVIEW-PRODUCT-CLOSURE-01 Phase C): does NOT modify the
    persisted canonical Composition and never recomputes any fact.  Unknown /
    non-UUID values are dropped by the caller's ``_is_uuid`` gate.
    """
    if not isinstance(composition, dict):
        return set()
    ids: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _MEMBER_UUID_KEYS:
                    if isinstance(value, list):
                        for item in value:
                            if isinstance(item, str) and item.strip():
                                ids.add(item)
                    elif isinstance(value, str) and value.strip():
                        ids.add(value)
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(composition)
    return ids


def is_uuid(value: Any) -> bool:
    """True when the value is a well-formed UUID string."""
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


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
#
# [REVIEW-PRODUCT-CLOSURE-01 Phase D] TYPED ownership split: the numeric keys are
# coerced through ``_number``; the categorical keys are CANONICAL STRINGS carried
# verbatim.  Previously the whole tuple ran through ``_number``, silently
# nulling real categorical values (e.g. volatility_phase "squeeze_on" -> None),
# which broke momentum.squeeze_state / categorical consumers on any date where
# the history state IS present.  Verified against the real 2026-08-21 history
# state payload (5277 rows): momentum_direction / momentum_change /
# structure_alignment / volatility_phase are JSONB strings, all other keys are
# JSONB numbers.
NUMERIC_STATE_KEYS: tuple[str, ...] = (
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
    "active_internal_ob_count",
    "active_swing_ob_count",
    "sqzmom_delta",
    "sqzmom_val",
    "volume_ratio_20",
    "volume_percentile_20",
    "volume_zscore_20",
    "available_bars",
)

CATEGORICAL_STATE_KEYS: tuple[str, ...] = (
    "momentum_direction",  # "contracting" / "expanding"
    "momentum_change",     # "enhancing" / "weakening"
    "structure_alignment",  # "共振" / "背离"
    "volatility_phase",    # "squeeze_on" / "squeeze_off"
)

_CONTINUOUS_STATE_KEYS = NUMERIC_STATE_KEYS + CATEGORICAL_STATE_KEYS


def state_to_continuous(state: dict[str, Any] | None) -> dict[str, Any]:
    """Map the history SSOT payload to the continuous member facts for PRD §7.3-§7.6.

    Unlike :func:`previous_state_to_flat` (categorical states only), this surfaces
    the numeric Trend / Structure / Momentum / Volume continuous fields that the
    Scope L1 aggregation consumes.  Missing / non-numeric keys become ``None``
    (unavailable), never 0.  Additive: does not affect existing consumers.

    [REVIEW-PRODUCT-CLOSURE-01 Phase D] Typed mapping:
    - ``NUMERIC_STATE_KEYS`` are coerced through ``_number`` (finite float or None);
    - ``CATEGORICAL_STATE_KEYS`` are passed through VERBATIM — a canonical
      categorical string must NEVER be numeric-coerced to None.  The backend does
      not invent category translations; the canonical string is preserved exactly.
    """
    if not state:
        return dict.fromkeys(_CONTINUOUS_STATE_KEYS)
    out: dict[str, Any] = {}
    for key in NUMERIC_STATE_KEYS:
        out[key] = _number(state.get(key))
    for key in CATEGORICAL_STATE_KEYS:
        out[key] = state.get(key)
    return out


# ----------------------------------------------------------------------------
# [CHANGE-20260826-001 Slice 1 CORRECTION] Core(T)-owned Current First Pyramid
# fact adapters.
#
# REVIEW-CURRENT-OWNER-01 freezes: Review(T) Current facts = published Core(T)
# (StockFeatureSnapshot.first_pyramid_flat), NOT History(T). The Core snapshot
# flat already carries the canonical ``fp_*`` keys (produced by
# flatten_first_pyramid — the SAME source the Board producer consumes). These
# adapters map that Core(T) ``fp_*`` flat into the EXACT output-key space that
# previous_state_to_flat / state_to_continuous emit from a History
# ``state_payload``, so the general MemberObservation Trend / Structure State /
# Momentum / state-driven Volume families are now owned by Core(T).
#
# Invariants:
#   * Zero kernel recompute — only the already-materialized Core flat is read.
#   * History(T) is NEVER read for the Current(T) flat_t / continuous.
#   * Keys absent from the Core flat stay None (Current(T) has no History(T)
#     owner for them) — no latest / same-day-other-run fallback.
#   * DSA Trend/Strength/Deviation continuous facts ARE surfaced from the Core
#     ``fp_*`` flat (``fp_trend_strength`` -> ``regime_strength``,
#     ``fp_trend_bars`` -> ``dsa_dir_bars``, ``fp_dsa_vwap_dev_pct`` ->
#     ``dsa_vwap_dev_pct``).  Remaining Historical <T-only keys not yet mapped
#     stay None for Current(T) — Current(T) has no History(T) owner for them —
#     no latest / same-day-other-run fallback.
# ----------------------------------------------------------------------------

# Core(T) ``fp_*`` flat key -> previous_state_to_flat output key (1:1, same name).
_SNAPSHOT_FLAT_T_KEYS: tuple[str, ...] = (
    "fp_trend_direction",
    "fp_swing_direction",
    "fp_internal_direction",
    "fp_structure_alignment",
    "fp_momentum_direction",
    "fp_momentum_change",
    "fp_volume_ratio20",
    "fp_volume_percentile20",
    "review_price_position",
    "review_volume_ratio20",
    "review_amount_ratio20",
    "review_volume_percentile20",
    "review_amount_percentile200",
    "fp_latest_bos_direction",
    "fp_latest_bos_freshness",
    "fp_latest_choch_direction",
    "fp_latest_choch_freshness",
    "fp_latest_ob_direction",
    "fp_latest_ob_freshness",
    "fp_segment_volume_ratio",
    "fp_prev_segment_volume",
)


def snapshot_flat_to_flat_t(snapshot_flat: object) -> dict[str, Any]:
    """Map the Core(T) ``first_pyramid_flat`` (``fp_*`` keys) to the
    previous_state_to_flat output-key space for the Current(T) categorical state.

    Pure pass-through of the canonical ``fp_*`` keys the Core snapshot already
    carries (flatten_first_pyramid emits them as finalized categorical strings,
    so no direction-label re-derivation is needed). Missing keys -> None.
    """
    flat = snapshot_flat if isinstance(snapshot_flat, dict) else None
    if not flat:
        return dict.fromkeys(_SNAPSHOT_FLAT_T_KEYS)
    return {key: flat.get(key) for key in _SNAPSHOT_FLAT_T_KEYS}


# Core(T) ``fp_*`` flat key -> state_to_continuous output key. Only the keys the
# Core flat actually carries are mapped; remaining raw History-only keys stay
# None (Current(T) has no History(T) owner for them).
_SNAPSHOT_CONTINUOUS_MAP: tuple[tuple[str, str], ...] = (
    ("fp_sqzmom_value", "sqzmom_val"),
    ("fp_volume_ratio20", "volume_ratio_20"),
    ("fp_volume_percentile20", "volume_percentile_20"),
    ("fp_volume_zscore20", "volume_zscore_20"),
    ("fp_segment_volume_ratio", "current_vs_prev_volume_mean_ratio"),
    ("fp_prev_segment_volume", "prev_segment_volume_mean"),
    # Segment Current structure facts (Core-owned). [REVIEW SEGMENT MAPPER GAP FIX
    # 2026-09-04] first_pyramid_flat emits fp_segment_bars / fp_segment_change_pct /
    # fp_segment_slope but the continuous map omitted them, so Current(T) segment_*
    # stayed None. Same mapping-gap class as the DSA fix.
    ("fp_segment_bars", "segment_bars"),
    ("fp_segment_change_pct", "segment_change_pct"),
    ("fp_segment_slope", "segment_slope"),
    # DSA Trend/Strength/Deviation facts — the Core flat DOES carry these
    # (flatten_first_pyramid emits fp_trend_strength / fp_trend_bars /
    # fp_dsa_vwap_dev_pct), so Current(T) DSA is owned by Core(T).  Previously
    # omitted -> regime_strength / dsa_dir_bars / dsa_vwap_dev_pct were always
    # None even when the Core snapshot was present (mapping-gap bug, 2026-09-04).
    ("fp_trend_strength", "regime_strength"),
    ("fp_trend_bars", "dsa_dir_bars"),
    ("fp_dsa_vwap_dev_pct", "dsa_vwap_dev_pct"),
)


def snapshot_flat_to_continuous(snapshot_flat: object) -> dict[str, Any]:
    """Map the Core(T) ``first_pyramid_flat`` to the state_to_continuous
    output-key space for Current(T) continuous Trend / Structure / Momentum /
    Volume facts.

    Only keys present in the Core ``fp_*`` flat are surfaced.  DSA Trend/Strength/
    Deviation facts (``fp_trend_strength`` / ``fp_trend_bars`` / ``fp_dsa_vwap_dev_pct``)
    ARE mapped from Core; other Historical <T-only keys not yet mapped stay None —
    Current(T) has no History(T) owner for them.
    """
    out: dict[str, Any] = dict.fromkeys(_CONTINUOUS_STATE_KEYS)
    flat = snapshot_flat if isinstance(snapshot_flat, dict) else None
    if not flat:
        return out
    for src_key, dst_key in _SNAPSHOT_CONTINUOUS_MAP:
        val = flat.get(src_key)
        out[dst_key] = _number(val) if dst_key in NUMERIC_STATE_KEYS else val
    # categorical mapped keys carried verbatim
    for src_key, dst_key in (
        ("fp_momentum_direction", "momentum_direction"),
        ("fp_momentum_change", "momentum_change"),
        ("fp_structure_alignment", "structure_alignment"),
    ):
        out[dst_key] = flat.get(src_key)
    return out


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
