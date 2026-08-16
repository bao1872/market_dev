"""Tests for Analysis B Historical Position Foundation (PRD §7.9, first layer).

The L1 payloads are produced by the REAL canonical producer
(``scope_observation.compute_scope_observation``) from ``MemberObservation``
inputs — no hand-written, shape-distorted fake payload (Test 9 / series tests).

Covered contract surface:

1.  <60 valid history        -> insufficient_history (position None)
2.  exactly 60               -> ready (position available)
3.  120-window truncation    -> only the latest 120 candidates are used
4.  T excluded               -> T never enters the baseline denominator
5.  future excluded          -> T+1 / T+2 changes never move Position(T)
6.  invalid baseline         -> None / NaN / inf not counted as valid
7.  invalid current          -> unavailable_current, never a position
8.  percentile tie semantics -> exactly scope_evidence.percentile_rank
9.  primitive extraction     -> real canonical payload shape (registry path+extract)
10. current-only primitives  -> bb_position / bb_width never historical-ready
11. ascending date contract  -> non-ascending series fails fast
12. deterministic            -> repeated execution identical

Phase 10 golden leakage test: T+1 / T+2 extremes never change Position(T); a
changed T may change Position(T) but never the baseline population.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.analysis.historical_position import (
    HISTORICAL_READY_PRIMITIVE_KEYS,
    POSITION_MINIMUM_VALID_HISTORY,
    POSITION_WINDOW_SIZE,
    compute_historical_position,
    compute_historical_positions,
    compute_position_series,
    compute_position_series_from_primitive_series,
)
from app.domain.review.analysis.observation_series import build_observation_series
from app.domain.review.observation_primitives import get_primitive
from app.domain.review.scope_evidence import percentile_rank
from app.domain.review.scope_observation import (
    MemberObservation,
    compute_scope_observation,
)

pytestmark = pytest.mark.pure_unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trading_days(start: date, count: int) -> list[date]:
    """Return ``count`` ascending weekdays starting at ``start``."""
    out: list[date] = []
    d = start
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _series_item(
    trade_date: date,
    *,
    ret: float,
    amount: float = 1e6,
    regime: float = 0.5,
    vol20: float = 1.0,
    vol200: float = 2.0,
) -> dict:
    """One real canonical L1 payload wrapped in the reconstruction series shape
    (dict with ISO ``trade_date`` + ``observation`` canonical payload)."""
    member = MemberObservation(
        member_id="m1",
        price_candidate=True,
        return_1d=ret,
        amount=amount,
        trend=Direction.UP,
        swing=Direction.UP,
        internal=Direction.UP,
        momentum=MomentumDirection.EXPANDING,
        regime_strength=regime,
        vol_ratio20=vol20,
        vol_ratio200=vol200,
    )
    payload = compute_scope_observation(
        scope_type="industry",
        scope_key="electronics",
        trade_date=trade_date,
        pit_member_ids=["m1"],
        members=[member],
    )
    return {"trade_date": trade_date.isoformat(), "observation": payload}


def _rich_payload(trade_date: date) -> dict:
    """Multi-member canonical payload giving non-trivial values for all 11
    historical-ready primitives."""
    members = [
        MemberObservation(
            member_id="m1",
            price_candidate=True,
            return_1d=2.0,
            amount=1e6,
            trend=Direction.UP,
            swing=Direction.UP,
            internal=Direction.UP,
            momentum=MomentumDirection.EXPANDING,
            regime_strength=0.3,
            vol_ratio20=1.0,
            vol_ratio200=2.0,
        ),
        MemberObservation(
            member_id="m2",
            price_candidate=True,
            return_1d=-1.0,
            amount=2e6,
            trend=Direction.DOWN,
            swing=Direction.DOWN,
            internal=Direction.DOWN,
            momentum=MomentumDirection.CONTRACTING,
            regime_strength=0.5,
            vol_ratio20=2.0,
            vol_ratio200=3.0,
        ),
        MemberObservation(
            member_id="m3",
            price_candidate=True,
            return_1d=0.0,
            amount=3e6,
            trend=Direction.SIDEWAYS,
            swing=Direction.SIDEWAYS,
            internal=Direction.SIDEWAYS,
            momentum=MomentumDirection.FLAT,
            regime_strength=0.7,
            vol_ratio20=3.0,
            vol_ratio200=4.0,
        ),
    ]
    return compute_scope_observation(
        scope_type="industry",
        scope_key="electronics",
        trade_date=trade_date,
        pit_member_ids=["m1", "m2", "m3"],
        members=members,
    )


def _series(days: list[date], rets: list[float]) -> list[dict]:
    assert len(days) == len(rets)
    return [_series_item(d, ret=r) for d, r in zip(days, rets, strict=True)]


# ---------------------------------------------------------------------------
# Test 1 / 2 — minimum valid history (60)
# ---------------------------------------------------------------------------


def test_min_60_history_insufficient() -> None:
    """59 valid pre-T values -> insufficient_history, position None (never 0)."""
    fact = compute_historical_position(2.0, [1.0] * 59)
    assert fact["status"] == "insufficient_history"
    assert fact["position"] is None
    assert fact["history"]["valid_count"] == 59
    assert fact["history"]["minimum_valid_history"] == POSITION_MINIMUM_VALID_HISTORY


def test_exactly_60_history_ready() -> None:
    """60 valid pre-T values -> ready, position available."""
    fact = compute_historical_position(2.0, [1.0] * 60)
    assert fact["status"] == "ready"
    assert fact["position"] == pytest.approx(100.0)
    assert fact["history"]["valid_count"] == 60


# ---------------------------------------------------------------------------
# Test 3 — 120-window truncation
# ---------------------------------------------------------------------------


def test_window_truncation_uses_latest_120_function() -> None:
    """150 pre-T observations -> only the latest 120 are used."""
    pre = list(range(1, 151))  # 150 candidates
    fact = compute_historical_position(200.0, pre)
    assert fact["history"]["candidate_count"] == POSITION_WINDOW_SIZE == 120
    assert fact["history"]["valid_count"] == 120
    # 200 > all 120 in-window candidates (31..150) -> 100.0.
    assert fact["position"] == pytest.approx(100.0)


def test_window_truncation_uses_latest_120_series() -> None:
    """Series baseline for T is series[max(0, i-120):i] — the latest 120
    observations, never extended further back to gather more valid values."""
    days = _trading_days(date(2026, 1, 5), 150)
    rets = [0.01 * (i % 7) for i in range(150)]
    series = _series(days, rets)
    facts = compute_position_series(series, "equal_weight_return")
    # index 149 (T) has 120 candidates before it, never 150.
    assert facts[-1]["status"] == "ready"
    assert facts[-1]["history"]["candidate_count"] == 120
    assert facts[-1]["history"]["valid_count"] == 120


# ---------------------------------------------------------------------------
# Test 4 — T excluded from baseline
# ---------------------------------------------------------------------------


def test_t_excluded_from_baseline_series() -> None:
    """An extreme T value never enters the baseline denominator."""
    days = _trading_days(date(2026, 1, 5), 121)  # 120 pre-T + T
    series = _series(days, [1.0] * 121)
    series[-1] = _series_item(days[-1], ret=1e9)  # extreme T
    facts = compute_position_series(series, "equal_weight_return")
    t_fact = facts[-1]
    assert t_fact["status"] == "ready"
    assert t_fact["history"]["candidate_count"] == 120
    assert t_fact["history"]["valid_count"] == 120
    assert t_fact["position"] == pytest.approx(100.0)


def test_t_excluded_denominator_function() -> None:
    """The function signature separates current value from the pre-T baseline;
    passing the T value only as ``current_value`` keeps it out of the sample."""
    pre = [1.0, 2.0, 3.0, 4.0] * 15  # 60 finite pre-T values
    t_value = 0.0  # below the entire baseline
    fact = compute_historical_position(t_value, pre)
    assert fact["status"] == "ready"
    assert fact["history"]["valid_count"] == 60
    assert fact["position"] == pytest.approx(percentile_rank(t_value, pre))


# ---------------------------------------------------------------------------
# Test 5 — future excluded
# ---------------------------------------------------------------------------


def test_future_excluded() -> None:
    """Modifying T+1 / T+2 values leaves Position(T) bit-identical."""
    days = _trading_days(date(2026, 1, 5), 125)  # T-120..T-1, T, T+1..T+4
    rets = [0.01 * (i % 7) for i in range(125)]
    series = _series(days, rets)
    t_idx = 120  # index 120 has exactly 120 pre-T observations
    base_t = compute_position_series(series, "equal_weight_return")[t_idx]
    assert base_t["status"] == "ready"

    mutated = list(series)
    mutated[121] = _series_item(days[121], ret=1e9)
    mutated[122] = _series_item(days[122], ret=-1e9)
    after = compute_position_series(mutated, "equal_weight_return")[t_idx]
    assert after == base_t


# ---------------------------------------------------------------------------
# Test 6 — invalid baseline filtering
# ---------------------------------------------------------------------------


def test_invalid_baseline_filtered() -> None:
    """None / NaN / inf never enter valid_count and never become 0."""
    pre = [1.0] * 58 + [None, float("nan"), float("inf")]
    fact = compute_historical_position(2.0, pre)
    assert fact["history"]["candidate_count"] == 61
    assert fact["history"]["valid_count"] == 58
    assert fact["status"] == "insufficient_history"
    assert fact["position"] is None


# ---------------------------------------------------------------------------
# Test 7 — invalid current
# ---------------------------------------------------------------------------


def test_invalid_current_unavailable() -> None:
    """current None / NaN / inf -> unavailable_current, never a position."""
    for bad in (None, float("nan"), float("inf")):
        fact = compute_historical_position(bad, [1.0] * 60)
        assert fact["status"] == "unavailable_current"
        assert fact["position"] is None
        assert fact["value"] is None


# ---------------------------------------------------------------------------
# Test 8 — percentile tie semantics == scope_evidence.percentile_rank
# ---------------------------------------------------------------------------


def test_percentile_tie_semantics_exact() -> None:
    """Position reuses scope_evidence.percentile_rank (ties all count)."""
    # 70 finite pre-T values; the 7-value tie block repeats 10x so valid_count=70.
    pre = [1.0, 2.0, 2.0, 2.0, 3.0, 4.0, 4.0] * 10
    cur = 2.0
    fact = compute_historical_position(cur, pre)
    assert fact["status"] == "ready"
    assert fact["position"] == pytest.approx(percentile_rank(cur, pre))
    assert fact["position"] == pytest.approx(4 / 7 * 100.0)


# ---------------------------------------------------------------------------
# Test 9 — primitive extraction on real canonical payload shape
# ---------------------------------------------------------------------------


def test_primitive_extraction_real_payload() -> None:
    """All 11 historical-ready primitives extract the exact canonical value from
    a payload produced by the real canonical producer (registry path+extract)."""
    payload = _rich_payload(date(2026, 8, 14))
    for key in HISTORICAL_READY_PRIMITIVE_KEYS:
        spec = get_primitive(key)
        node: object = payload
        for part in spec.path:
            assert isinstance(node, dict), (key, spec.path)
            assert part in node, (key, spec.path)
            node = node[part]
        expected = spec.extract(node)
        fact = compute_position_series([{"trade_date": "2026-08-14", "observation": payload}], key)[
            0
        ]
        assert fact["value"] == expected, key
    # Sanity: the rich payload yields non-trivial (non-degenerate) values.
    facts = compute_historical_positions([{"trade_date": "2026-08-14", "observation": payload}])
    assert facts["equal_weight_return"][0]["value"] == pytest.approx(1.0 / 3)
    assert facts["advance_ratio"][0]["value"] == pytest.approx(1.0 / 3)
    assert facts["decline_ratio"][0]["value"] == pytest.approx(1.0 / 3)
    assert facts["unchanged_ratio"][0]["value"] == pytest.approx(1.0 / 3)
    assert facts["trend.continuous.regime_strength"][0]["value"] == pytest.approx(0.5)
    assert facts["participation.volume.ratio20"][0]["value"] == pytest.approx(2.0)
    assert facts["participation.volume.ratio200"][0]["value"] == pytest.approx(3.0)
    assert facts["return_dispersion"][0]["value"] == payload["price"]["return_dispersion"]


# ---------------------------------------------------------------------------
# Test 10 — current-only primitives excluded
# ---------------------------------------------------------------------------


def test_current_only_primitives_excluded() -> None:
    """bb_position / bb_width are current-only and never historical-ready."""
    assert "momentum.bb_position" not in HISTORICAL_READY_PRIMITIVE_KEYS
    assert "momentum.bb_width" not in HISTORICAL_READY_PRIMITIVE_KEYS
    with pytest.raises(KeyError):
        compute_position_series([], "momentum.bb_position")
    with pytest.raises(KeyError):
        compute_position_series([], "momentum.bb_width")
    payload = _rich_payload(date(2026, 8, 14))
    out = compute_historical_positions([{"trade_date": "2026-08-14", "observation": payload}])
    assert set(out) == set(HISTORICAL_READY_PRIMITIVE_KEYS)
    assert "momentum.bb_position" not in out
    assert "momentum.bb_width" not in out


# ---------------------------------------------------------------------------
# Test 11 — ascending date contract
# ---------------------------------------------------------------------------


def test_non_ascending_fails_fast() -> None:
    """Non-ascending input must fail, never silently re-sort."""
    days = _trading_days(date(2026, 1, 5), 4)
    series = _series(days, [1.0] * 4)
    scrambled = [series[2], series[0], series[1], series[3]]
    with pytest.raises(ValueError, match="ascending"):
        compute_position_series(scrambled, "equal_weight_return")
    # Equal (duplicate) dates are also not strictly ascending.
    dup = [series[0], series[0]]
    with pytest.raises(ValueError, match="ascending"):
        compute_position_series(dup, "equal_weight_return")


# ---------------------------------------------------------------------------
# Test 12 — deterministic
# ---------------------------------------------------------------------------


def test_deterministic() -> None:
    """Same input, repeated execution -> identical output, no mutation."""
    days = _trading_days(date(2026, 1, 5), 130)
    rets = [0.01 * (i % 7) for i in range(130)]
    series = _series(days, rets)
    a = compute_position_series(series, "equal_weight_return")
    b = compute_position_series(series, "equal_weight_return")
    assert a == b
    # The module never mutates its input series / payloads.
    assert all(isinstance(s["observation"], dict) for s in series)


# ---------------------------------------------------------------------------
# Phase 10 — golden leakage test
# ---------------------------------------------------------------------------


def test_golden_leakage_future_and_t() -> None:
    """T-120..T-1, T, T+1, T+2:

    - changing T+1 / T+2 to extremes NEVER changes Position(T);
    - changing T itself MAY change Position(T) but NEVER the baseline population
      (history.valid_count / candidate_count are fixed).
    """
    days = _trading_days(date(2026, 1, 5), 123)  # 120 pre-T + T + T+1 + T+2
    rets = [0.01 * (i % 7) for i in range(123)]
    series = _series(days, rets)
    t_idx = 120

    base = compute_position_series(series, "equal_weight_return")[t_idx]
    assert base["status"] == "ready"
    assert base["history"]["valid_count"] == 120
    assert base["history"]["candidate_count"] == 120

    # Future extremes must not move Position(T).
    m1 = list(series)
    m1[121] = _series_item(days[121], ret=1e9)
    m1[122] = _series_item(days[122], ret=-1e9)
    after_future = compute_position_series(m1, "equal_weight_return")[t_idx]
    assert after_future["position"] == base["position"]
    assert after_future["history"] == base["history"]

    # Changing T to an extreme may change Position(T) but not the baseline.
    m2 = list(series)
    m2[t_idx] = _series_item(days[t_idx], ret=1e9)
    after_t = compute_position_series(m2, "equal_weight_return")[t_idx]
    assert after_t["position"] == pytest.approx(100.0)
    assert after_t["position"] != base["position"]
    assert after_t["history"]["valid_count"] == base["history"]["valid_count"]
    assert after_t["history"]["candidate_count"] == base["history"]["candidate_count"]


# ---------------------------------------------------------------------------
# Bridge — Observation Series -> Position canonical path (PRD §7.7.5)
# ---------------------------------------------------------------------------


def _to_observation_series(
    raw_series: list[dict],
    *,
    remove: set[int] | None = None,
) -> dict:
    """Build a formal ObservationSeries from raw canonical payloads.

    ``remove`` optionally drops the snapshot at those indices to create
    missing trading-observation slots (gap preservation must then flow from the
    Builder into Position).
    """
    days = [date.fromisoformat(item["trade_date"]) for item in raw_series]
    removed = remove or set()
    snapshots = [
        {
            "trade_date": item["trade_date"],
            "readiness": "ready",
            "payload": item["observation"],
        }
        for i, item in enumerate(raw_series)
        if i not in removed
    ]
    return build_observation_series(
        scope_type="industry",
        scope_key="electronics",
        from_date=days[0],
        to_date=days[-1],
        trading_dates=days,
        snapshot_series=snapshots,
    )


def _pts(
    dates_iso: list[str],
    values: list[float | None],
    availables: list[bool],
) -> list[dict]:
    return [
        {
            "trade_date": d,
            "readiness": "ready",
            "value": v,
            "available": a,
        }
        for d, v, a in zip(dates_iso, values, availables, strict=True)
    ]


def test_bridge_complete_series_parity() -> None:
    """T1 — NO GAP: canonical Builder path and legacy raw adapter must agree."""
    days = _trading_days(date(2026, 1, 5), 130)
    rets = [0.01 * (i % 7) for i in range(130)]
    raw = _series(days, rets)

    legacy = compute_position_series(raw, "equal_weight_return")
    obs_series = _to_observation_series(raw)
    canonical = compute_position_series_from_primitive_series(
        obs_series["primitives"]["equal_weight_return"]
    )
    assert canonical == legacy


def test_bridge_missing_slot_preserved() -> None:
    """T2 — a missing middle snapshot is preserved (never compressed):
    candidate window stays 120 POINT slots, valid_count drops to 119."""
    days = _trading_days(date(2026, 1, 5), 130)
    rets = [0.01 * (i % 7) for i in range(130)]
    raw = _series(days, rets)
    obs_series = _to_observation_series(raw, remove={60})

    ew = obs_series["primitives"]["equal_weight_return"]
    assert len(ew["points"]) == 130  # date-complete, no compression
    assert ew["points"][60]["available"] is False
    assert ew["points"][60]["value"] is None

    facts = compute_position_series_from_primitive_series(ew)
    assert len(facts) == 130

    gap = facts[60]
    assert gap["trade_date"] == days[60].isoformat()
    assert gap["status"] == "unavailable_current"
    assert gap["position"] is None

    # Post-gap T: the 120-slot window is full; only the one gap is invalid.
    later = facts[120]
    assert later["status"] == "ready"
    assert later["history"]["candidate_count"] == 120
    assert later["history"]["valid_count"] == 119


def test_bridge_readiness_independence() -> None:
    """T3 — readiness must NEVER override available: a partial-snapshot point
    with a finite value is valid Position baseline input."""
    dates = [d.isoformat() for d in _trading_days(date(2026, 1, 5), 125)]
    points = [
        {
            "trade_date": d,
            "readiness": ("partial" if i == 50 else "ready"),
            "value": 1.0,
            "available": True,
        }
        for i, d in enumerate(dates)
    ]
    facts = compute_position_series_from_primitive_series(
        {"key": "equal_weight_return", "points": points}
    )
    # The partial point is valid Position input.
    assert facts[50]["value"] == pytest.approx(1.0)
    # Its finite value counts in a later T baseline (valid_count == 120).
    assert facts[120]["status"] == "ready"
    assert facts[120]["history"]["valid_count"] == 120


def test_bridge_available_true_value_none_fails() -> None:
    pts = _pts(["2026-01-05", "2026-01-06"], [1.0, None], [True, True])
    with pytest.raises(ValueError):
        compute_position_series_from_primitive_series({"key": "equal_weight_return", "points": pts})


def test_bridge_available_true_value_nan_fails() -> None:
    pts = _pts(["2026-01-05", "2026-01-06"], [1.0, float("nan")], [True, True])
    with pytest.raises(ValueError):
        compute_position_series_from_primitive_series({"key": "equal_weight_return", "points": pts})


def test_bridge_available_false_finite_fails() -> None:
    pts = _pts(["2026-01-05", "2026-01-06"], [1.0, 2.0], [True, False])
    with pytest.raises(ValueError):
        compute_position_series_from_primitive_series({"key": "equal_weight_return", "points": pts})


def test_bridge_duplicate_date_fails() -> None:
    pts = _pts(["2026-01-05", "2026-01-05"], [1.0, 2.0], [True, True])
    with pytest.raises(ValueError):
        compute_position_series_from_primitive_series({"key": "equal_weight_return", "points": pts})


def test_bridge_descending_date_fails() -> None:
    pts = _pts(["2026-01-06", "2026-01-05"], [1.0, 2.0], [True, True])
    with pytest.raises(ValueError):
        compute_position_series_from_primitive_series({"key": "equal_weight_return", "points": pts})


def test_bridge_non_historical_ready_key_fails() -> None:
    with pytest.raises(KeyError):
        compute_position_series_from_primitive_series({"key": "momentum.bb_position", "points": []})


def test_bridge_empty_points_valid() -> None:
    facts = compute_position_series_from_primitive_series(
        {"key": "equal_weight_return", "points": []}
    )
    assert facts == []
