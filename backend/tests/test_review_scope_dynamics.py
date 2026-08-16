"""Tests for Analysis B — Scope Dynamics canonical composition owner.

Covers the "Scope Dynamics composition responsibility" ONLY (No Duplicate
Proof: the frozen Position / EMA / Velocity / Acceleration / Persistence /
Phase math is already proven by their own durable tests — nothing here
re-proves boundaries / mutual exclusion / EMA / persistence / percentile).

T1. TRUE CANONICAL SHAPE — input is a real canonical Scope L1 payload
    (built via ``compute_scope_observation``), never a fake simplified shape.
T2. MANUAL-CHAIN PARITY — manual chain (compute_position_series ->
    compute_historical_dynamics_series -> compute_dynamics_phase_series)
    equals ``compute_scope_dynamics_analysis``.
T3. EW-ONLY OWNERSHIP — perturbing non-EW primitives (amount / volume) leaves
    the EW Dynamics Phase series unchanged.
T4. PREFIX INVARIANCE — appending future observations never changes the
    0:T Dynamics Phase output (no future leakage at the composition layer).
T5. SERIES CONTRACT — non-ascending dates fail fast via the existing owner
    (no silent sort in the composer).
T6. EMPTY SERIES — returns primitive_key + empty historical dynamics package +
    empty dynamics_phase series (following existing owner ACTUAL behaviour).
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
from app.domain.review.analysis.historical_position import compute_position_series
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
# Helpers — real canonical Scope L1 payload builder
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
    )
    return {"trade_date": trade_date.isoformat(), "observation": payload}


def _real_series(
    days: list[date],
    rets: list[float],
    *,
    amounts: list[float] | None = None,
    vol20s: list[float] | None = None,
    vol200s: list[float] | None = None,
) -> list[dict[str, Any]]:
    """A canonical series with identical returns and optional per-day amount /
    volume perturbations (used by the EW-only ownership test)."""
    assert len(days) == len(rets)
    return [
        _real_series_item(
            d,
            ret=r,
            amount=(amounts[i] if amounts else 1e6),
            vol20=(vol20s[i] if vol20s else 1.0),
            vol200=(vol200s[i] if vol200s else 2.0),
        )
        for i, (d, r) in enumerate(zip(days, rets, strict=True))
    ]


def _returns(count: int) -> list[float]:
    """Deterministic varied return series (variety without external data)."""
    return [0.012 * math.sin(i / 12) + 0.0015 * ((i * 7) % 5 - 2) for i in range(count)]


# ---------------------------------------------------------------------------
# T1 — true canonical shape
# ---------------------------------------------------------------------------


def test_true_canonical_shape_composition() -> None:
    days = _trading_days(date(2026, 1, 5), 130)
    obs = _real_series(days, _returns(len(days)))
    out = compute_scope_dynamics_analysis(obs)

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
    # dynamics_phase is date-aligned with the observation series, 1 row per day.
    phase = out["dynamics_phase"]
    assert len(phase) == len(days)
    assert [p["trade_date"] for p in phase] == [d.isoformat() for d in days]
    for p in phase:
        assert p["status"] in (STATUS_READY, STATUS_INSUFFICIENT, STATUS_UNAVAILABLE)
        assert p["phase"] is None or p["phase"] in VALID_PHASES


def test_composition_contract_fixed_keys() -> None:
    days = _trading_days(date(2026, 1, 5), 60)
    obs = _real_series(days, _returns(len(days)))
    out = compute_scope_dynamics_analysis(obs)
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

    manual_hd = compute_historical_dynamics_series(
        compute_position_series(obs, DYNAMICS_PHASE_PRIMITIVE_KEY)
    )
    manual_phase = compute_dynamics_phase_series(manual_hd)

    composed = compute_scope_dynamics_analysis(obs)
    assert composed["historical_dynamics"] == manual_hd
    assert composed["dynamics_phase"] == manual_phase


# ---------------------------------------------------------------------------
# T3 — EW-only ownership (non-EW primitives never drive Dynamics Phase)
# ---------------------------------------------------------------------------


def test_ew_only_ownership() -> None:
    days = _trading_days(date(2026, 1, 5), 130)
    rets = _returns(len(days))

    base = _real_series(days, rets)
    variant = _real_series(
        days,
        rets,
        amounts=[1e5 + (i % 9) * 1e4 for i in range(len(days))],
        vol20s=[0.5 + (i % 5) * 0.2 for i in range(len(days))],
        vol200s=[1.0 + (i % 4) * 0.5 for i in range(len(days))],
    )

    # Prove the perturbation really changed a non-EW primitive's dynamics...
    base_aw = compute_position_series(base, "amount_weighted_return")
    variant_aw = compute_position_series(variant, "amount_weighted_return")
    assert base_aw != variant_aw
    # ...while leaving the EW Position series byte-identical.
    assert compute_position_series(base, "equal_weight_return") == compute_position_series(
        variant, "equal_weight_return"
    )

    base_out = compute_scope_dynamics_analysis(base)
    variant_out = compute_scope_dynamics_analysis(variant)
    assert base_out["dynamics_phase"] == variant_out["dynamics_phase"]


# ---------------------------------------------------------------------------
# T4 — prefix invariance (no future leakage at the composition layer)
# ---------------------------------------------------------------------------


def test_prefix_invariance_no_future_leakage() -> None:
    days = _trading_days(date(2026, 1, 5), 140)
    obs = _real_series(days, _returns(len(days)))

    prefix_out = compute_scope_dynamics_analysis(obs[:100])
    full_out = compute_scope_dynamics_analysis(obs)

    assert full_out["dynamics_phase"][:100] == prefix_out["dynamics_phase"]
    assert (
        full_out["historical_dynamics"]["position"][:100]
        == prefix_out["historical_dynamics"]["position"]
    )


# ---------------------------------------------------------------------------
# T5 — series contract (fail fast on non-ascending dates; no silent sort)
# ---------------------------------------------------------------------------


def test_duplicate_date_fails_fast() -> None:
    days = _trading_days(date(2026, 1, 5), 10)
    obs = _real_series(days, [0.01] * len(days))
    obs[3]["trade_date"] = obs[2]["trade_date"]  # duplicate -> not strictly ascending
    with pytest.raises(ValueError):
        compute_scope_dynamics_analysis(obs)


def test_reversed_date_fails_fast() -> None:
    days = _trading_days(date(2026, 1, 5), 10)
    obs = _real_series(days, [0.01] * len(days))
    obs.reverse()  # strictly descending
    with pytest.raises(ValueError):
        compute_scope_dynamics_analysis(obs)


# ---------------------------------------------------------------------------
# T6 — empty series (follows existing owner ACTUAL behaviour)
# ---------------------------------------------------------------------------


def test_empty_series() -> None:
    out = compute_scope_dynamics_analysis([])
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
