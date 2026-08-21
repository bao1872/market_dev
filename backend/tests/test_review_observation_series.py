"""Tests for Analysis Foundation — Observation Series Builder (PRD §7.7.5).

Proves ONLY the Builder responsibility (date alignment + gap preservation +
primitive extraction).  Deliberately does NOT re-prove Position / EMA /
Persistence / Phase (durable evidence already CLOSED).

Coverage (task spec A-J):
A. complete series — every primitive gets one point per trading date;
B. missing middle date — preserved as an ``unavailable`` slot, points stay 5;
C. no compression — T4/T5 keep their original axis indices;
D. readiness independence — a ``partial`` snapshot can still carry finite value;
E. ready but primitive missing — ``value=None`` / ``available=False``;
F. no zero-fill — missing snapshots never emit ``value == 0``;
G. strict date validation — duplicate / descending / outside-axis fail fast;
H. registry ownership — direct scalar AND distribution extractor both used;
I. primitive subset / unknown key fail fast;
J. empty trading window + from_date > to_date fail fast.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.analysis.observation_series import (
    READINESS_UNAVAILABLE,
    build_observation_series,
)
from app.domain.review.observation_primitives import (
    OBSERVATION_PRIMITIVES,
    get_primitive,
    list_primitive_keys,
)
from app.domain.review.scope_observation import (
    MemberObservation,
    compute_scope_observation,
)

pytestmark = pytest.mark.pure_unit


# ---------------------------------------------------------------------------
# Helpers — real canonical L1 payloads (never a fake {"price": ...} shape)
# ---------------------------------------------------------------------------


def _trading_days(start: date, count: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _real_payload(
    trade_date: date,
    *,
    member_count: int = 2,
    ret: float = 0.01,
    amount: float = 1e6,
    vol20: float = 1.5,
    vol200: float = 2.0,
) -> dict[str, Any]:
    members = [
        MemberObservation(
            member_id=f"m{i}",
            price_candidate=True,
            return_1d=ret,
            amount=amount,
            trend=Direction.UP,
            swing=Direction.UP,
            internal=Direction.UP,
            momentum=MomentumDirection.EXPANDING,
            regime_strength=0.5,
            vol_ratio20=vol20,
            vol_ratio200=vol200,
        )
        for i in range(member_count)
    ]
    return compute_scope_observation(
        scope_type="industry",
        scope_key="electronics",
        trade_date=trade_date,
        pit_member_ids=[f"m{i}" for i in range(member_count)],
        members=members,
        event_coverage_member_ids=None,
    )


def _snapshot(
    trade_date: date,
    payload: dict[str, Any],
    readiness: str = "ready",
) -> dict[str, Any]:
    return {"trade_date": trade_date.isoformat(), "readiness": readiness, "payload": payload}


def _build(
    days: list[date],
    snapshots: list[dict[str, Any]],
    **kwargs: Any,
) -> dict[str, Any]:
    return build_observation_series(
        scope_type="industry",
        scope_key="electronics",
        from_date=days[0],
        to_date=days[-1],
        trading_dates=days,
        snapshot_series=snapshots,
        availability={"status": "partial", "total_snapshots": len(snapshots)},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# A — complete series
# ---------------------------------------------------------------------------


def test_a_complete_series_every_primitive_5_points() -> None:
    days = _trading_days(date(2026, 1, 5), 5)
    snapshots = [_snapshot(d, _real_payload(d)) for d in days]
    out = _build(days, snapshots)

    assert set(out) == {
        "scope_type",
        "scope_key",
        "query_window",
        "availability",
        "primitives",
    }
    assert out["query_window"] == {
        "from_date": days[0].isoformat(),
        "to_date": days[-1].isoformat(),
    }
    expected_dates = [d.isoformat() for d in days]
    assert set(out["primitives"]) == set(OBSERVATION_PRIMITIVES)
    for key in list_primitive_keys():
        series = out["primitives"][key]
        assert series["key"] == key
        assert series["l1_path"] == get_primitive(key).path
        assert len(series["points"]) == 5
        assert [p["trade_date"] for p in series["points"]] == expected_dates
    assert out["availability"]["trading_observation_count"] == 5
    assert out["availability"]["snapshot_count"] == 5
    assert out["availability"]["missing_snapshot_count"] == 0


# ---------------------------------------------------------------------------
# B — missing middle date stays an unavailable slot
# ---------------------------------------------------------------------------


def test_b_missing_middle_date_is_unavailable_slot() -> None:
    days = _trading_days(date(2026, 1, 5), 5)
    snapshots = [_snapshot(d, _real_payload(d)) for d in (days[0], days[1], days[3], days[4])]
    out = _build(days, snapshots)

    for key in list_primitive_keys():
        points = out["primitives"][key]["points"]
        assert len(points) == 5, f"{key} must keep 5 points, got {len(points)}"
        gap = points[2]
        assert gap["trade_date"] == days[2].isoformat()
        assert gap["readiness"] == READINESS_UNAVAILABLE
        assert gap["value"] is None
        assert gap["available"] is False

    # transparent source-coverage metadata (not a score / gate)
    assert out["availability"]["trading_observation_count"] == 5
    assert out["availability"]["snapshot_count"] == 4
    assert out["availability"]["missing_snapshot_count"] == 1
    # input availability metadata preserved
    assert out["availability"]["status"] == "partial"
    assert out["availability"]["total_snapshots"] == 4


# ---------------------------------------------------------------------------
# C — no compression: T4/T5 keep their original axis index
# ---------------------------------------------------------------------------


def test_c_no_compression_t4_t5_keep_axis_index() -> None:
    days = _trading_days(date(2026, 1, 5), 5)
    snapshots = [_snapshot(d, _real_payload(d)) for d in (days[0], days[1], days[3], days[4])]
    out = _build(days, snapshots)

    for key in list_primitive_keys():
        points = out["primitives"][key]["points"]
        # Never compressed into a 4-point [T1,T2,T4,T5] timeline.
        assert [p["trade_date"] for p in points] == [d.isoformat() for d in days]
        assert points[3]["trade_date"] == days[3].isoformat()
        assert points[4]["trade_date"] == days[4].isoformat()
        assert len(points) == 5


# ---------------------------------------------------------------------------
# D — readiness independence: partial snapshot can still be available
# ---------------------------------------------------------------------------


def test_d_partial_snapshot_can_still_be_available() -> None:
    days = _trading_days(date(2026, 1, 5), 1)
    payload = _real_payload(days[0], ret=0.01, member_count=2)
    snapshots = [_snapshot(days[0], payload, readiness="partial")]
    out = _build(days, snapshots)

    pt = out["primitives"]["equal_weight_return"]["points"][0]
    assert pt["readiness"] == "partial"
    assert pt["available"] is True
    assert pt["value"] == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# E — ready but primitive missing: value=None, available=False
# ---------------------------------------------------------------------------


def test_e_ready_snapshot_can_still_be_unavailable() -> None:
    days = _trading_days(date(2026, 1, 5), 1)
    # Single member -> price.concentration.normalized_hhi is None (member_count<=1).
    payload = _real_payload(days[0], ret=0.01, member_count=1)
    assert payload["price"]["concentration"]["normalized_hhi"] is None
    snapshots = [_snapshot(days[0], payload, readiness="ready")]
    out = _build(days, snapshots)

    pt = out["primitives"]["price_normalized_hhi"]["points"][0]
    assert pt["readiness"] == "ready"
    assert pt["value"] is None
    assert pt["available"] is False

    # The same ready snapshot still exposes a finite primitive -> readiness
    # never gates availability.
    ew = out["primitives"]["equal_weight_return"]["points"][0]
    assert ew["available"] is True
    assert ew["value"] == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# F — no zero-fill for missing snapshots
# ---------------------------------------------------------------------------


def test_f_no_zero_fill_for_missing_snapshots() -> None:
    days = _trading_days(date(2026, 1, 5), 5)
    snapshots = [_snapshot(d, _real_payload(d)) for d in (days[0], days[2], days[4])]
    out = _build(days, snapshots)

    missing_indices = [1, 3]
    for key in list_primitive_keys():
        points = out["primitives"][key]["points"]
        for idx in missing_indices:
            pt = points[idx]
            assert pt["value"] is None
            assert pt["available"] is False
            assert pt["value"] != 0  # never zero-filled


# ---------------------------------------------------------------------------
# G — strict date validation (fail fast, never silent sort / dedupe)
# ---------------------------------------------------------------------------


def test_g_duplicate_trading_dates_fail_fast() -> None:
    days = _trading_days(date(2026, 1, 5), 3)
    dup = [days[0], days[1], days[1]]
    snapshots = [_snapshot(d, _real_payload(d)) for d in days]
    with pytest.raises(ValueError):
        _build(dup, snapshots)


def test_g_descending_trading_dates_fail_fast() -> None:
    days = _trading_days(date(2026, 1, 5), 3)
    desc = list(reversed(days))
    snapshots = [_snapshot(d, _real_payload(d)) for d in days]
    with pytest.raises(ValueError):
        _build(desc, snapshots)


def test_g_snapshot_outside_trading_axis_fails_fast() -> None:
    days = _trading_days(date(2026, 1, 5), 3)
    outside = date(2026, 2, 2)  # a real weekday not on the axis
    snapshots = [_snapshot(d, _real_payload(d)) for d in days]
    snapshots.append(_snapshot(outside, _real_payload(outside)))
    with pytest.raises(ValueError):
        _build(days, snapshots)


def test_g_duplicate_snapshot_date_fails_fast() -> None:
    days = _trading_days(date(2026, 1, 5), 3)
    snapshots = [
        _snapshot(days[1], _real_payload(days[1])),
        _snapshot(days[1], _real_payload(days[1])),
        _snapshot(days[2], _real_payload(days[2])),
    ]
    with pytest.raises(ValueError):
        _build(days, snapshots)


def test_g_trading_date_out_of_window_fails_fast() -> None:
    days = _trading_days(date(2026, 1, 5), 3)
    # to_date = days[1] -> days[2] falls outside the query window.
    with pytest.raises(ValueError):
        build_observation_series(
            scope_type="industry",
            scope_key="electronics",
            from_date=days[0],
            to_date=days[1],
            trading_dates=days,
            snapshot_series=[],
        )


def test_g_from_after_to_fails_fast() -> None:
    days = _trading_days(date(2026, 1, 5), 3)
    with pytest.raises(ValueError):
        build_observation_series(
            scope_type="industry",
            scope_key="electronics",
            from_date=days[1],
            to_date=days[0],
            trading_dates=[],
            snapshot_series=[],
        )


# ---------------------------------------------------------------------------
# H — registry ownership: direct scalar AND distribution extractor
# ---------------------------------------------------------------------------


def test_h_registry_extraction_direct_and_distribution() -> None:
    days = _trading_days(date(2026, 1, 5), 1)
    payload = _real_payload(days[0], ret=0.01, vol20=1.5, member_count=2)
    # real L1 distribution node consumed via the registry central-tendency rule
    assert payload["participation"]["volume"]["ratio20"]["p50"] == pytest.approx(1.5)

    snapshots = [_snapshot(days[0], payload, readiness="ready")]
    out = _build(days, snapshots)

    ew = out["primitives"]["equal_weight_return"]["points"][0]
    assert ew["value"] == pytest.approx(payload["price"]["equal_weight_return"])
    assert ew["available"] is True

    r20 = out["primitives"]["participation.volume.ratio20"]["points"][0]
    assert r20["value"] == pytest.approx(payload["participation"]["volume"]["ratio20"]["p50"])
    assert r20["available"] is True
    assert out["primitives"]["participation.volume.ratio20"]["l1_path"] == (
        "participation",
        "volume",
        "ratio20",
    )


# ---------------------------------------------------------------------------
# I — primitive subset / unknown key
# ---------------------------------------------------------------------------


def test_i_primitive_subset() -> None:
    days = _trading_days(date(2026, 1, 5), 3)
    snapshots = [_snapshot(d, _real_payload(d)) for d in days]
    out = _build(days, snapshots, primitive_keys=["equal_weight_return"])

    assert set(out["primitives"]) == {"equal_weight_return"}
    assert len(out["primitives"]["equal_weight_return"]["points"]) == 3


def test_i_unknown_primitive_key_fails_fast() -> None:
    days = _trading_days(date(2026, 1, 5), 1)
    snapshots = [_snapshot(days[0], _real_payload(days[0]))]
    with pytest.raises(KeyError):
        _build(days, snapshots, primitive_keys=["does_not_exist"])


# ---------------------------------------------------------------------------
# J — empty trading window
# ---------------------------------------------------------------------------


def test_j_empty_trading_window() -> None:
    out = build_observation_series(
        scope_type="industry",
        scope_key="electronics",
        from_date=date(2026, 1, 5),
        to_date=date(2026, 1, 9),
        trading_dates=[],
        snapshot_series=[],
    )
    assert out["availability"]["trading_observation_count"] == 0
    assert out["availability"]["snapshot_count"] == 0
    assert out["availability"]["missing_snapshot_count"] == 0
    for key in list_primitive_keys():
        assert out["primitives"][key]["points"] == []
