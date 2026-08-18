"""Tests for Analysis B — Scope Dynamics canonical composition owner.

The canonical input contract is now a formal ObservationSeries (the
``build_observation_series`` output), so every composition test first routes
real canonical payloads through the Builder.  Covers the "Scope Dynamics
composition responsibility" ONLY (No Duplicate Proof: the frozen Position / EMA
/ Velocity / Acceleration / Persistence / Phase math is already proven by their
own durable tests — nothing here re-proves boundaries / mutual exclusion / EMA /
persistence / percentile).

T1. TRUE CANONICAL SHAPE — input is a real canonical Scope L1 payload
    (built via ``compute_scope_observation``) wrapped by the Builder.
T2. MANUAL-CHAIN PARITY — manual chain (compute_position_series_from_primitive_series
    -> compute_historical_dynamics_series -> compute_dynamics_phase_series)
    equals ``compute_scope_dynamics_analysis``.
T3/T7. EW-ONLY OWNERSHIP — over a real two-member canonical payload, perturbing
    the member amount weights genuinely changes the canonical
    ``amount_weighted_return`` input while the member universe and returns (hence
    ``equal_weight_return``) stay identical, leaving the EW-driven chain unchanged
    THROUGH the formal ObservationSeries path.
T4. MISSING EW PRIMITIVE — an ObservationSeries without ``equal_weight_return``
    fails fast (KeyError); no fallback.
T5. END-TO-END PURE CHAIN WITH GAP — a missing trading-observation slot flows
    Builder -> Position (unavailable_current) -> Dynamics Phase (unavailable_current /
    phase None); never compressed, never fallback.
T6. GAP DOES NOT COMPRESS PERSISTENCE — with a gap inside the latest 20 slots,
    Persistence candidate_count stays 20 and valid_count drops by one.
T8. PREFIX INVARIANCE — appending future observations/snapshots never changes
    the 0:T Dynamics Phase output (no future leakage at the composition layer).
Also: duplicate/reversed dates fail fast in the Builder; empty ObservationSeries
returns primitive_key + empty packages.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.analysis.dynamics_phase import (
    VALID_PHASES,
    compute_dynamics_phase_series,
)
from app.domain.review.analysis.historical_dynamics import (
    STATUS_INSUFFICIENT,
    STATUS_READY,
    STATUS_UNAVAILABLE,
    compute_historical_dynamics_series,
)
from app.domain.review.analysis.historical_position import (
    compute_position_series_from_primitive_series,
)
from app.domain.review.analysis.observation_series import build_observation_series
from app.domain.review.analysis.scope_dynamics import (
    DYNAMICS_PHASE_PRIMITIVE_KEY,
    compute_scope_dynamics_analysis,
)
from app.domain.review.scope_observation import (
    MemberObservation,
    compute_scope_observation,
)

pytestmark = pytest.mark.pure_unit


# ---------------------------------------------------------------------------
# Helpers — real canonical Scope L1 payload builder + ObservationSeries wrapper
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


def _real_series_item(
    trade_date: date,
    *,
    ret: float,
    amount: float = 1e6,
    regime: float = 0.5,
    vol20: float = 1.0,
    vol200: float = 2.0,
) -> dict[str, Any]:
    """One real canonical L1 payload in the reconstruction series shape."""
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
        event_coverage_member_ids=None,
    )
    return {"trade_date": trade_date.isoformat(), "observation": payload}


def _real_series(
    days: list[date],
    rets: list[float],
) -> list[dict[str, Any]]:
    """A canonical single-member series with identical returns per day."""
    assert len(days) == len(rets)
    return [
        _real_series_item(
            d,
            ret=r,
        )
        for d, r in zip(days, rets, strict=True)
    ]


def _dual_member_series_item(
    trade_date: date,
    *,
    ret: float,
    spread: float,
    amount_a: float,
    amount_b: float,
) -> dict[str, Any]:
    """One real canonical L1 payload with two price-valid members (T3 fixture).

    member A = ``ret + spread``, member B = ``ret - spread`` with amounts
    ``amount_a`` / ``amount_b``.  Because both members are always present with
    identical returns:
      - ``price.equal_weight_return`` = mean(A, B) = ``ret`` — independent of
        the amounts;
      - ``price.amount_weighted_return`` genuinely moves when the weights move
        (single-member would make AW == member return and hide any change).
    """
    member_a = MemberObservation(
        member_id="m_a",
        price_candidate=True,
        return_1d=ret + spread,
        amount=amount_a,
        trend=Direction.UP,
        swing=Direction.UP,
        internal=Direction.UP,
        momentum=MomentumDirection.EXPANDING,
        regime_strength=0.5,
        vol_ratio20=1.0,
        vol_ratio200=2.0,
    )
    member_b = MemberObservation(
        member_id="m_b",
        price_candidate=True,
        return_1d=ret - spread,
        amount=amount_b,
        trend=Direction.UP,
        swing=Direction.UP,
        internal=Direction.UP,
        momentum=MomentumDirection.EXPANDING,
        regime_strength=0.5,
        vol_ratio20=1.0,
        vol_ratio200=2.0,
    )
    payload = compute_scope_observation(
        scope_type="industry",
        scope_key="electronics",
        trade_date=trade_date,
        pit_member_ids=["m_a", "m_b"],
        members=[member_a, member_b],
        event_coverage_member_ids=None,
    )
    return {"trade_date": trade_date.isoformat(), "observation": payload}


def _returns(count: int) -> list[float]:
    """Deterministic varied return series (variety without external data)."""
    return [0.012 * math.sin(i / 12) + 0.0015 * ((i * 7) % 5 - 2) for i in range(count)]


def _to_observation_series(
    raw_series: list[dict[str, Any]],
    *,
    remove: set[int] | None = None,
    primitive_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Wrap raw canonical payloads into a formal ObservationSeries (PRD §7.7.5).

    ``remove`` optionally drops the snapshot at those indices to create missing
    trading-observation slots that must survive to the Phase layer.  An empty
    raw series yields a valid empty ObservationSeries.
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
        from_date=days[0] if days else date(2026, 1, 5),
        to_date=days[-1] if days else date(2026, 1, 5),
        trading_dates=days,
        snapshot_series=snapshots,
        primitive_keys=primitive_keys,
    )


# ---------------------------------------------------------------------------
# T1 — true canonical shape
# ---------------------------------------------------------------------------


def test_true_canonical_shape_composition() -> None:
    days = _trading_days(date(2026, 1, 5), 130)
    obs = _real_series(days, _returns(len(days)))
    out = compute_scope_dynamics_analysis(_to_observation_series(obs))

    assert out["primitive_key"] == DYNAMICS_PHASE_PRIMITIVE_KEY
    # The historical dynamics package carries the 7 canonical keys.
    assert set(out["historical_dynamics"]) == {
        "position",
        "ema5",
        "ema20",
        "velocity",
        "signal",
        "acceleration",
        "persistence",
    }
    # dynamics_phase is date-aligned with the trading axis, 1 row per day.
    phase = out["dynamics_phase"]
    assert len(phase) == len(days)
    assert [p["trade_date"] for p in phase] == [d.isoformat() for d in days]
    for p in phase:
        assert p["status"] in (STATUS_READY, STATUS_INSUFFICIENT, STATUS_UNAVAILABLE)
        assert p["phase"] is None or p["phase"] in VALID_PHASES


def test_composition_contract_fixed_keys() -> None:
    days = _trading_days(date(2026, 1, 5), 60)
    obs = _real_series(days, _returns(len(days)))
    out = compute_scope_dynamics_analysis(_to_observation_series(obs))
    assert set(out) == {"primitive_key", "historical_dynamics", "dynamics_phase"}
    for key in (
        "score",
        "confidence",
        "trend_label",
        "capital_confirmation",
        "breadth",
        "volume_confirmation",
        "internal_structure",
        "trading_context",
    ):
        assert key not in out


# ---------------------------------------------------------------------------
# T2 — manual-chain parity
# ---------------------------------------------------------------------------


def test_manual_chain_parity() -> None:
    days = _trading_days(date(2026, 1, 5), 130)
    rets = _returns(len(days))
    obs = _real_series(days, rets)
    obs_series = _to_observation_series(obs)

    ew = obs_series["primitives"][DYNAMICS_PHASE_PRIMITIVE_KEY]
    manual_hd = compute_historical_dynamics_series(
        compute_position_series_from_primitive_series(ew)
    )
    manual_phase = compute_dynamics_phase_series(manual_hd)

    composed = compute_scope_dynamics_analysis(obs_series)
    assert composed["historical_dynamics"] == manual_hd
    assert composed["dynamics_phase"] == manual_phase


# ---------------------------------------------------------------------------
# T3 / T7 — EW-only ownership through the formal ObservationSeries path
# ---------------------------------------------------------------------------


def test_ew_only_ownership() -> None:
    """T3/T7 — EW-only ownership on a real two-member canonical payload.

    BASE:    member weights (1, 1) every day  -> AW = ret.
    VARIANT: weights (3, 1) even days / (1, 4) odd days -> AW genuinely moves,
    while the member universe and returns (hence Equal Weight Return) are
    byte-identical.  The Scope Dynamics chain is EW-driven, so it must not move
    even after both sides go through the formal ObservationSeries path.
    """
    days = _trading_days(date(2026, 1, 5), 130)
    rets = _returns(len(days))
    spread = 0.01

    base = [
        _dual_member_series_item(d, ret=r, spread=spread, amount_a=1.0, amount_b=1.0)
        for d, r in zip(days, rets, strict=True)
    ]
    variant = [
        _dual_member_series_item(
            d,
            ret=r,
            spread=spread,
            amount_a=(3.0 if i % 2 == 0 else 1.0),
            amount_b=(1.0 if i % 2 == 0 else 4.0),
        )
        for i, (d, r) in enumerate(zip(days, rets, strict=True))
    ]

    # (1) Equal Weight canonical scalar identical for EVERY date.
    for b, v in zip(base, variant, strict=True):
        assert (
            b["observation"]["price"]["equal_weight_return"]
            == v["observation"]["price"]["equal_weight_return"]
        ), "variant must NOT change the canonical EW input"

    # (2) Amount Weighted canonical scalar genuinely changed on at least one date.
    aw_changed = [
        b["observation"]["price"]["amount_weighted_return"]
        != v["observation"]["price"]["amount_weighted_return"]
        for b, v in zip(base, variant, strict=True)
    ]
    assert any(aw_changed), "variant must perturb the canonical AW input"

    # (3) The EW-driven Scope chain is invariant under the AW-only perturbation.
    base_out = compute_scope_dynamics_analysis(_to_observation_series(base))
    variant_out = compute_scope_dynamics_analysis(_to_observation_series(variant))
    assert base_out["historical_dynamics"] == variant_out["historical_dynamics"]
    assert base_out["dynamics_phase"] == variant_out["dynamics_phase"]


# ---------------------------------------------------------------------------
# T4 — missing EW primitive fails fast (no fallback)
# ---------------------------------------------------------------------------


def test_missing_ew_primitive_fails_fast() -> None:
    days = _trading_days(date(2026, 1, 5), 60)
    obs = _real_series(days, _returns(len(days)))
    obs_series = _to_observation_series(obs, primitive_keys=["advance_ratio"])
    assert DYNAMICS_PHASE_PRIMITIVE_KEY not in obs_series["primitives"]
    with pytest.raises(KeyError):
        compute_scope_dynamics_analysis(obs_series)


# ---------------------------------------------------------------------------
# T5 — end-to-end pure chain: a gap survives to the Phase layer
# ---------------------------------------------------------------------------


def test_e2e_gap_survives_to_phase() -> None:
    days = _trading_days(date(2026, 1, 5), 130)
    obs = _real_series(days, _returns(len(days)))
    obs_series = _to_observation_series(obs, remove={60})

    out = compute_scope_dynamics_analysis(obs_series)
    phase = out["dynamics_phase"]
    assert len(phase) == len(days) == 130

    # gap day -> Position unavailable_current -> Phase unavailable_current / None.
    pos_gap = out["historical_dynamics"]["position"][60]
    assert pos_gap["status"] == STATUS_UNAVAILABLE
    assert pos_gap["position"] is None
    ph_gap = phase[60]
    assert ph_gap["status"] == STATUS_UNAVAILABLE
    assert ph_gap["phase"] is None
    # No fallback: the gap day is not the previous day's phase.
    assert ph_gap["trade_date"] == days[60].isoformat()


# ---------------------------------------------------------------------------
# T6 — a gap does NOT compress the Persistence 20-slot window
# ---------------------------------------------------------------------------


def test_gap_does_not_compress_persistence() -> None:
    days = _trading_days(date(2026, 1, 5), 130)
    obs = _real_series(days, _returns(len(days)))
    # Drop a snapshot inside the latest 20 trading observations.
    obs_series = _to_observation_series(obs, remove={115})

    out = compute_scope_dynamics_analysis(obs_series)
    persistence = out["historical_dynamics"]["persistence"]
    # For T=119 the Persistence window is slots [100, 119] (20 slots, incl T);
    # the single gap at 115 stays a window slot but is not a valid Position.
    fact = persistence[119]
    assert fact["candidate_count"] == 20
    assert fact["valid_count"] == 19


# ---------------------------------------------------------------------------
# T8 — prefix invariance (no future leakage at the composition layer)
# ---------------------------------------------------------------------------


def test_prefix_invariance_no_future_leakage() -> None:
    days = _trading_days(date(2026, 1, 5), 140)
    obs = _real_series(days, _returns(len(days)))

    prefix_out = compute_scope_dynamics_analysis(_to_observation_series(obs[:100]))
    full_out = compute_scope_dynamics_analysis(_to_observation_series(obs))

    assert full_out["dynamics_phase"][:100] == prefix_out["dynamics_phase"]
    assert (
        full_out["historical_dynamics"]["position"][:100]
        == prefix_out["historical_dynamics"]["position"]
    )


# ---------------------------------------------------------------------------
# Series contract — fail fast via the Builder (no silent sort)
# ---------------------------------------------------------------------------


def test_duplicate_date_fails_fast() -> None:
    days = _trading_days(date(2026, 1, 5), 10)
    obs = _real_series(days, [0.01] * len(days))
    obs[3]["trade_date"] = obs[2]["trade_date"]  # duplicate -> not strictly ascending
    with pytest.raises(ValueError):
        _to_observation_series(obs)


def test_reversed_date_fails_fast() -> None:
    days = _trading_days(date(2026, 1, 5), 10)
    obs = _real_series(days, [0.01] * len(days))
    obs.reverse()  # strictly descending
    with pytest.raises(ValueError):
        _to_observation_series(obs)


# ---------------------------------------------------------------------------
# Empty ObservationSeries (follows existing owner ACTUAL behaviour)
# ---------------------------------------------------------------------------


def test_empty_series() -> None:
    out = compute_scope_dynamics_analysis(_to_observation_series([]))
    assert out["primitive_key"] == DYNAMICS_PHASE_PRIMITIVE_KEY
    assert set(out["historical_dynamics"]) == {
        "position",
        "ema5",
        "ema20",
        "velocity",
        "signal",
        "acceleration",
        "persistence",
    }
    for series in out["historical_dynamics"].values():
        assert series == []
    assert out["dynamics_phase"] == []
