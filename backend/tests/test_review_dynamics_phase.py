"""Tests for Analysis B — Dynamics Phase pure-domain classifier (PRD §7.11 FROZEN).

Covers the frozen PRD §7.11 contracts:

- exact state helpers boundaries (V_NEG / V_MID / V_POS, A_NEG / A_ZERO / A_POS);
- exact context helpers boundaries (HIGH_REGIME / BOTTOM_RECOVERY_CONTEXT);
- exact classifier boundaries / deterministic cases A-I (PRD §7.11);
- availability propagation FIRST (unavailable_current > insufficient_history >
  ready), driven by upstream status only;
- ready-value contract violations fail fast (non-finite / out-of-range);
- ready but unclassified -> status = ready / phase = None (no seventh phase);
- mutual exclusion property (priority = NONE) over a boundary grid;
- series integration: date alignment, no future leakage, contract violations,
  real production-chain package (compute_historical_dynamics_series).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from app.domain.review.analysis.dynamics_phase import (
    A_STATE_NEG,
    A_STATE_POS,
    A_STATE_ZERO,
    DYNAMICS_PHASE_ACCELERATION_GATE,
    DYNAMICS_PHASE_VELOCITY_GATE,
    PHASE_DECELERATING,
    PHASE_EARLY_LIFT,
    PHASE_REPAIRING,
    PHASE_STRENGTHENING,
    PHASE_SUSTAINED,
    PHASE_WEAKENING,
    V_STATE_MID,
    V_STATE_NEG,
    V_STATE_POS,
    VALID_PHASES,
    acceleration_state,
    bottom_recovery_context,
    compute_dynamics_phase,
    compute_dynamics_phase_series,
    high_regime,
    velocity_state,
)
from app.domain.review.analysis.historical_dynamics import (
    STATUS_INSUFFICIENT,
    STATUS_READY,
    STATUS_UNAVAILABLE,
    compute_historical_dynamics_series,
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


def _ready_inputs(
    *,
    position: float = 50.0,
    velocity: float = 0.0,
    acceleration: float = 0.0,
    upper_occupancy: float = 0.1,
    lower_occupancy: float = 0.1,
) -> dict[str, Any]:
    """One fully-``ready`` input bundle (exact production keyword signature)."""
    return {
        "position": position,
        "velocity": velocity,
        "acceleration": acceleration,
        "upper_occupancy": upper_occupancy,
        "lower_occupancy": lower_occupancy,
        "position_status": STATUS_READY,
        "velocity_status": STATUS_READY,
        "acceleration_status": STATUS_READY,
        "persistence_status": STATUS_READY,
    }


def _position_fact(
    td: date,
    position: float | None,
    status: str,
    *,
    primitive: str = "equal_weight_return",
) -> dict[str, Any]:
    """A Position fact in the exact shape produced by ``compute_position_series``."""
    return {
        "primitive_key": primitive,
        "trade_date": td.isoformat(),
        "value": position,
        "position": position,
        "history": {
            "window_size": 120,
            "minimum_valid_history": 60,
            "candidate_count": 60,
            "valid_count": 60,
        },
        "status": status,
    }


def _velocity_fact(td: date, value: float | None, status: str) -> dict[str, Any]:
    return {"trade_date": td.isoformat(), "value": value, "status": status}


def _acceleration_fact(td: date, value: float | None, status: str) -> dict[str, Any]:
    return {"trade_date": td.isoformat(), "value": value, "status": status}


def _persistence_fact(
    td: date,
    upper_occupancy: float | None,
    lower_occupancy: float | None,
    status: str,
) -> dict[str, Any]:
    """A Persistence fact in the exact shape produced by ``compute_persistence_series``."""
    return {
        "trade_date": td.isoformat(),
        "window_size": 20,
        "minimum_valid_count": 15,
        "candidate_count": 20,
        "valid_count": 15,
        "coverage": 0.75,
        "upper_count": 3,
        "lower_count": 3,
        "upper_occupancy": upper_occupancy,
        "lower_occupancy": lower_occupancy,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Exact state helper boundaries
# ---------------------------------------------------------------------------


def test_velocity_state_boundaries() -> None:
    assert velocity_state(-2.001) == V_STATE_NEG
    assert velocity_state(-2.0) == V_STATE_NEG
    assert velocity_state(-1.999) == V_STATE_MID
    assert velocity_state(2.0) == V_STATE_MID
    assert velocity_state(2.001) == V_STATE_POS


def test_acceleration_state_boundaries() -> None:
    assert acceleration_state(-1.001) == A_STATE_NEG
    assert acceleration_state(-1.0) == A_STATE_NEG
    assert acceleration_state(-0.999) == A_STATE_ZERO
    assert acceleration_state(1.0) == A_STATE_ZERO
    assert acceleration_state(1.001) == A_STATE_POS


# ---------------------------------------------------------------------------
# Exact context helper boundaries
# ---------------------------------------------------------------------------


def test_high_regime_boundaries() -> None:
    assert high_regime(70.0, 0.20) is True  # position == 70 belongs to HIGH_REGIME
    assert high_regime(69.999, 0.20) is False
    assert high_regime(70.0, 0.199) is False
    assert high_regime(70.0, 0.201) is True


def test_bottom_recovery_context_boundaries() -> None:
    assert bottom_recovery_context(69.999, 0.30) is True
    assert bottom_recovery_context(70.0, 0.30) is False  # position < 70 required
    assert bottom_recovery_context(69.999, 0.299) is False
    assert bottom_recovery_context(69.999, 0.301) is True


# ---------------------------------------------------------------------------
# Exact classifier — deterministic cases / boundaries (PRD §7.11, A-I)
# ---------------------------------------------------------------------------


def test_boundary_a_velocity_minus_2_is_weakening() -> None:
    """velocity == -2.0 -> Weakening only (no context gate)."""
    fact = compute_dynamics_phase(**_ready_inputs(velocity=-2.0))
    assert fact["status"] == STATUS_READY
    assert fact["phase"] == PHASE_WEAKENING
    assert fact["velocity_state"] == V_STATE_NEG


def test_boundary_b_high_regime_acc_minus_1_is_decelerating() -> None:
    """HIGH_REGIME + velocity just above -2 + acceleration == -1 -> Decelerating."""
    fact = compute_dynamics_phase(
        **_ready_inputs(position=75.0, upper_occupancy=0.25, velocity=-1.999, acceleration=-1.0)
    )
    assert fact["status"] == STATUS_READY
    assert fact["phase"] == PHASE_DECELERATING
    assert fact["high_regime"] is True
    assert fact["velocity_state"] == V_STATE_MID
    assert fact["acceleration_state"] == A_STATE_NEG


def test_boundary_c_high_regime_acc_just_above_minus_1_is_sustained() -> None:
    """HIGH_REGIME + velocity > -2 + acceleration just above -1 -> Sustained."""
    fact = compute_dynamics_phase(
        **_ready_inputs(position=75.0, upper_occupancy=0.25, velocity=0.0, acceleration=-0.999)
    )
    assert fact["status"] == STATUS_READY
    assert fact["phase"] == PHASE_SUSTAINED
    assert fact["acceleration_state"] == A_STATE_ZERO


def test_boundary_d_high_regime_acc_plus_1_is_sustained() -> None:
    """HIGH_REGIME + velocity > -2 + acceleration == +1 -> Sustained (Case C)."""
    fact = compute_dynamics_phase(
        **_ready_inputs(position=75.0, upper_occupancy=0.25, velocity=0.0, acceleration=1.0)
    )
    assert fact["status"] == STATUS_READY
    assert fact["phase"] == PHASE_SUSTAINED
    assert fact["acceleration_state"] == A_STATE_ZERO


def test_boundary_e_brc_acc_plus_1_is_repairing() -> None:
    """BRC + velocity > 2 + acceleration == +1 -> Repairing (Case E)."""
    fact = compute_dynamics_phase(
        **_ready_inputs(position=40.0, lower_occupancy=0.40, velocity=2.001, acceleration=1.0)
    )
    assert fact["status"] == STATUS_READY
    assert fact["phase"] == PHASE_REPAIRING
    assert fact["bottom_recovery_context"] is True
    assert fact["velocity_state"] == V_STATE_POS
    assert fact["acceleration_state"] == A_STATE_ZERO


def test_boundary_f_brc_acc_just_above_plus_1_is_early_lift() -> None:
    """BRC + velocity > 2 + acceleration just above +1 -> Early Lift (Case D)."""
    fact = compute_dynamics_phase(
        **_ready_inputs(position=40.0, lower_occupancy=0.40, velocity=2.001, acceleration=1.001)
    )
    assert fact["status"] == STATUS_READY
    assert fact["phase"] == PHASE_EARLY_LIFT
    assert fact["acceleration_state"] == A_STATE_POS


def test_boundary_g_not_brc_strengthening() -> None:
    """velocity > 2 + acceleration > 1 + NOT BRC -> Strengthening (Case F)."""
    fact = compute_dynamics_phase(
        **_ready_inputs(
            position=75.0,
            upper_occupancy=0.1,
            lower_occupancy=0.1,
            velocity=2.001,
            acceleration=1.001,
        )
    )
    assert fact["status"] == STATUS_READY
    assert fact["phase"] == PHASE_STRENGTHENING
    assert fact["high_regime"] is False
    assert fact["bottom_recovery_context"] is False
    assert fact["velocity_state"] == V_STATE_POS
    assert fact["acceleration_state"] == A_STATE_POS


def test_boundary_h_position_70_upper_20_high_regime_sustained() -> None:
    """position == 70 + upper == 0.20 + neutral V/A -> HIGH_REGIME + Sustained."""
    fact = compute_dynamics_phase(**_ready_inputs(position=70.0, upper_occupancy=0.20))
    assert fact["status"] == STATUS_READY
    assert fact["high_regime"] is True
    assert fact["phase"] == PHASE_SUSTAINED


def test_boundary_i_position_just_below_70_brc_early_lift() -> None:
    """position just below 70 + lower == 0.30 + V>2 + A>1 -> Early Lift."""
    fact = compute_dynamics_phase(
        **_ready_inputs(position=69.999, lower_occupancy=0.30, velocity=2.001, acceleration=1.001)
    )
    assert fact["status"] == STATUS_READY
    assert fact["bottom_recovery_context"] is True
    assert fact["phase"] == PHASE_EARLY_LIFT


def test_boundary_velocity_just_above_minus_2_not_weakening() -> None:
    """velocity just above -2 is NOT Weakening; no context -> unclassified."""
    fact = compute_dynamics_phase(
        **_ready_inputs(position=50.0, velocity=-1.999, lower_occupancy=0.1)
    )
    assert fact["status"] == STATUS_READY
    assert fact["phase"] is None


# ---------------------------------------------------------------------------
# Availability propagation FIRST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_key",
    ["position_status", "velocity_status", "acceleration_status", "persistence_status"],
)
def test_any_unavailable_current_propagates(input_key: str) -> None:
    inputs = _ready_inputs()
    inputs[input_key] = STATUS_UNAVAILABLE
    fact = compute_dynamics_phase(**inputs)
    assert fact["status"] == STATUS_UNAVAILABLE
    assert fact["phase"] is None
    assert fact["position"] is None  # evidence never populated on non-ready


@pytest.mark.parametrize(
    "input_key",
    ["position_status", "velocity_status", "acceleration_status", "persistence_status"],
)
def test_any_insufficient_history_propagates(input_key: str) -> None:
    inputs = _ready_inputs()
    inputs[input_key] = STATUS_INSUFFICIENT
    fact = compute_dynamics_phase(**inputs)
    assert fact["status"] == STATUS_INSUFFICIENT
    assert fact["phase"] is None


def test_unavailable_current_wins_over_insufficient_history() -> None:
    """One unavailable_current + others insufficient_history -> unavailable_current."""
    inputs = _ready_inputs()
    inputs["position_status"] = STATUS_UNAVAILABLE
    inputs["velocity_status"] = STATUS_INSUFFICIENT
    inputs["acceleration_status"] = STATUS_INSUFFICIENT
    inputs["persistence_status"] = STATUS_INSUFFICIENT
    fact = compute_dynamics_phase(**inputs)
    assert fact["status"] == STATUS_UNAVAILABLE
    assert fact["phase"] is None


def test_unknown_status_fails_fast() -> None:
    inputs = _ready_inputs()
    inputs["position_status"] = "not_a_status"
    with pytest.raises(ValueError):
        compute_dynamics_phase(**inputs)


# ---------------------------------------------------------------------------
# Ready-value contract violations fail fast
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"position": None},
        {"position": float("nan")},
        {"position": float("inf")},
        {"position": -0.1},
        {"position": 100.1},
        {"velocity": None},
        {"velocity": float("nan")},
        {"velocity": float("inf")},
        {"velocity": float("-inf")},
        {"acceleration": None},
        {"acceleration": float("nan")},
        {"acceleration": float("inf")},
        {"upper_occupancy": None},
        {"upper_occupancy": float("nan")},
        {"upper_occupancy": -0.1},
        {"upper_occupancy": 1.1},
        {"lower_occupancy": None},
        {"lower_occupancy": float("nan")},
        {"lower_occupancy": -0.1},
        {"lower_occupancy": 1.1},
    ],
)
def test_ready_invalid_value_fails_fast(override: dict[str, Any]) -> None:
    inputs = _ready_inputs()
    inputs.update(override)
    with pytest.raises(ValueError):
        compute_dynamics_phase(**inputs)


def test_ready_position_bounds_inclusive() -> None:
    """position == 0 and == 100 are valid ready values (no fail-fast)."""
    for pos in (0.0, 100.0):
        fact = compute_dynamics_phase(**_ready_inputs(position=pos))
        assert fact["status"] == STATUS_READY


# ---------------------------------------------------------------------------
# Ready but unclassified
# ---------------------------------------------------------------------------


def test_ready_unclassified() -> None:
    """All inputs ready, no phase condition matches -> ready / phase None."""
    fact = compute_dynamics_phase(
        **_ready_inputs(
            position=50.0,
            velocity=0.0,
            acceleration=0.0,
            upper_occupancy=0.1,
            lower_occupancy=0.1,
        )
    )
    assert fact["status"] == STATUS_READY
    assert fact["phase"] is None


# ---------------------------------------------------------------------------
# Result contract: no score fields, evidence populated on ready
# ---------------------------------------------------------------------------


def test_no_score_fields() -> None:
    fact = compute_dynamics_phase(
        **_ready_inputs(position=75.0, upper_occupancy=0.25, velocity=0.0, acceleration=0.0)
    )
    for key in ("phase_score", "confidence_score", "strength_score", "composite_score"):
        assert key not in fact


def test_ready_fact_contract_exact_keys() -> None:
    fact = compute_dynamics_phase(
        **_ready_inputs(position=75.0, upper_occupancy=0.25, velocity=0.0, acceleration=0.0)
    )
    assert set(fact) == {
        "phase",
        "status",
        "position",
        "velocity",
        "acceleration",
        "upper_occupancy",
        "lower_occupancy",
        "velocity_state",
        "acceleration_state",
        "high_regime",
        "bottom_recovery_context",
    }


# ---------------------------------------------------------------------------
# Mutual exclusion property (priority = NONE) over a boundary grid
# ---------------------------------------------------------------------------


def test_mutual_exclusion_grid() -> None:
    """For every boundary combination at most one raw predicate is True, and the
    classifier result equals the single matched predicate (or None)."""
    positions = [69.999, 70.0, 70.001]
    velocities = [-2.001, -2.0, -1.999, 2.0, 2.001]
    accelerations = [-1.001, -1.0, -0.999, 1.0, 1.001]
    uppers = [0.199, 0.20, 0.201]
    lowers = [0.299, 0.30, 0.301]
    for pos in positions:
        for vel in velocities:
            for acc in accelerations:
                for upper in uppers:
                    for lower in lowers:
                        hr = high_regime(pos, upper)
                        brc = bottom_recovery_context(pos, lower)
                        weakening = vel <= -DYNAMICS_PHASE_VELOCITY_GATE
                        decelerating = (
                            hr
                            and vel > -DYNAMICS_PHASE_VELOCITY_GATE
                            and acc <= -DYNAMICS_PHASE_ACCELERATION_GATE
                        )
                        sustained = (
                            hr
                            and vel > -DYNAMICS_PHASE_VELOCITY_GATE
                            and -DYNAMICS_PHASE_ACCELERATION_GATE
                            < acc
                            <= DYNAMICS_PHASE_ACCELERATION_GATE
                        )
                        early_lift = (
                            brc
                            and vel > DYNAMICS_PHASE_VELOCITY_GATE
                            and acc > DYNAMICS_PHASE_ACCELERATION_GATE
                        )
                        repairing = (
                            brc
                            and vel > -DYNAMICS_PHASE_VELOCITY_GATE
                            and acc <= DYNAMICS_PHASE_ACCELERATION_GATE
                        )
                        strengthening = (
                            vel > DYNAMICS_PHASE_VELOCITY_GATE
                            and acc > DYNAMICS_PHASE_ACCELERATION_GATE
                            and not brc
                        )
                        matched = [
                            phase
                            for phase, cond in (
                                (PHASE_WEAKENING, weakening),
                                (PHASE_DECELERATING, decelerating),
                                (PHASE_SUSTAINED, sustained),
                                (PHASE_EARLY_LIFT, early_lift),
                                (PHASE_REPAIRING, repairing),
                                (PHASE_STRENGTHENING, strengthening),
                            )
                            if cond
                        ]
                        assert len(matched) <= 1, (
                            f"non-exclusive at pos={pos} vel={vel} acc={acc} "
                            f"upper={upper} lower={lower}: {matched}"
                        )
                        fact = compute_dynamics_phase(
                            **_ready_inputs(
                                position=pos,
                                velocity=vel,
                                acceleration=acc,
                                upper_occupancy=upper,
                                lower_occupancy=lower,
                            )
                        )
                        assert fact["phase"] == (matched[0] if matched else None), (
                            f"classifier mismatch at pos={pos} vel={vel} acc={acc} "
                            f"upper={upper} lower={lower}"
                        )
                        assert fact["status"] == STATUS_READY


# ---------------------------------------------------------------------------
# Series integration
# ---------------------------------------------------------------------------


def _sample_package() -> dict[str, list[dict[str, Any]]]:
    """A 3-day production-shape dynamics package (position / velocity /
    acceleration / persistence) covering Sustained / Early Lift / Weakening."""
    d1, d2, d3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
    return {
        "position": [
            _position_fact(d1, 75.0, STATUS_READY),
            _position_fact(d2, 40.0, STATUS_READY),
            _position_fact(d3, 50.0, STATUS_READY),
        ],
        "velocity": [
            _velocity_fact(d1, 0.0, STATUS_READY),
            _velocity_fact(d2, 2.001, STATUS_READY),
            _velocity_fact(d3, -3.0, STATUS_READY),
        ],
        "acceleration": [
            _acceleration_fact(d1, 0.0, STATUS_READY),
            _acceleration_fact(d2, 1.001, STATUS_READY),
            _acceleration_fact(d3, -2.0, STATUS_READY),
        ],
        "persistence": [
            _persistence_fact(d1, 0.20, 0.10, STATUS_READY),
            _persistence_fact(d2, 0.10, 0.40, STATUS_READY),
            _persistence_fact(d3, 0.10, 0.10, STATUS_READY),
        ],
    }


def test_series_date_aligned_classification() -> None:
    out = compute_dynamics_phase_series(_sample_package())
    assert len(out) == 3
    assert [o["trade_date"] for o in out] == [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
    ]
    assert out[0]["phase"] == PHASE_SUSTAINED  # HIGH_REGIME + neutral V/A
    assert out[1]["phase"] == PHASE_EARLY_LIFT  # BRC + V>2 + A>1
    assert out[2]["phase"] == PHASE_WEAKENING  # velocity <= -2
    for o in out:
        assert o["status"] == STATUS_READY
        assert o["phase"] in VALID_PHASES


def test_series_availability_propagation_per_row() -> None:
    package = _sample_package()
    package["velocity"][1]["status"] = STATUS_UNAVAILABLE
    package["acceleration"][1]["status"] = STATUS_INSUFFICIENT
    out = compute_dynamics_phase_series(package)
    assert out[0]["status"] == STATUS_READY
    assert out[0]["phase"] == PHASE_SUSTAINED
    assert out[1]["status"] == STATUS_UNAVAILABLE  # precedence wins
    assert out[1]["phase"] is None
    assert out[2]["status"] == STATUS_READY
    assert out[2]["phase"] == PHASE_WEAKENING


def test_series_no_future_leakage() -> None:
    """Mutating a T+1 row never changes the T output (same-day consumption only)."""
    package = _sample_package()
    baseline = compute_dynamics_phase_series(package)
    # Drastically change the last row's inputs (would flip its own phase).
    package["velocity"][2]["value"] = 5.0
    package["acceleration"][2]["value"] = 5.0
    package["position"][2]["position"] = 80.0
    package["persistence"][2]["upper_occupancy"] = 0.5
    mutated = compute_dynamics_phase_series(package)
    assert mutated[0]["phase"] == baseline[0]["phase"]
    assert mutated[1]["phase"] == baseline[1]["phase"]


def test_series_missing_key_fails_fast() -> None:
    package = _sample_package()
    del package["persistence"]
    with pytest.raises(KeyError):
        compute_dynamics_phase_series(package)


def test_series_length_mismatch_fails_fast() -> None:
    package = _sample_package()
    package["velocity"] = package["velocity"][:2]
    with pytest.raises(ValueError):
        compute_dynamics_phase_series(package)


def test_series_misaligned_dates_fail_fast() -> None:
    package = _sample_package()
    package["velocity"][1]["trade_date"] = "2026-01-08"
    with pytest.raises(ValueError):
        compute_dynamics_phase_series(package)


def test_series_non_ascending_fails_fast() -> None:
    package = _sample_package()
    package["position"][2]["trade_date"] = "2026-01-04"
    with pytest.raises(ValueError):
        compute_dynamics_phase_series(package)


# ---------------------------------------------------------------------------
# Real production-chain package (true canonical fixtures)
# ---------------------------------------------------------------------------


def test_series_over_real_production_package() -> None:
    """Feed a real Position series through the canonical Historical Dynamics
    chain (compute_historical_dynamics_series) and classify the tail rows."""
    days = _trading_days(date(2026, 1, 5), 120)
    position_series = [
        _position_fact(d, 5.0 + (i % 100) * 0.9, STATUS_READY) for i, d in enumerate(days)
    ]
    package = compute_historical_dynamics_series(position_series)
    out = compute_dynamics_phase_series(package)
    assert len(out) == len(days)
    for o in out:
        assert o["status"] in (
            STATUS_READY,
            STATUS_INSUFFICIENT,
            STATUS_UNAVAILABLE,
        )
        assert o["phase"] is None or o["phase"] in VALID_PHASES
    # The tail is fully warm: all four frozen inputs are ready -> classified.
    tail = out[-1]
    assert tail["status"] == STATUS_READY
    assert tail["phase"] in VALID_PHASES
    # Evidence is populated on the ready path.
    assert tail["position"] is not None
    assert tail["velocity"] is not None
    assert tail["acceleration"] is not None
    assert tail["upper_occupancy"] is not None
    assert tail["lower_occupancy"] is not None
    assert tail["velocity_state"] in (V_STATE_NEG, V_STATE_MID, V_STATE_POS)
    assert tail["acceleration_state"] in (A_STATE_NEG, A_STATE_ZERO, A_STATE_POS)
